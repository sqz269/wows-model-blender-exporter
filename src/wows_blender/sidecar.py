"""Sidecar v3 reader — minimum subset the Blender add-on cares about.

The add-on reads only what it needs to instantiate a ship:

* ``materials[]`` — material_id → texture_sets per scheme
* ``turrets[]`` / ``secondaries[]`` / ``antiair[]`` / ``torpedoes[]``
  / ``accessories[]`` — placements (asset_id + transform.matrix +
  parent_section + attached_y_flip + parent_mesh)
* ``skins[]`` — optional per-skin material overrides

This module is pure stdlib (no Pillow, no Blender bpy) so the
publisher tests can exercise it without spinning up Blender. The
authoritative producer-side schema lives at
``wows-model-export/reference/contracts/METADATA_SPEC.md`` — read that
for the full field-by-field reference; this loader is intentionally
permissive about extra fields (newer producers + older add-on
should still partially work).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TextureRef:
    """One per-slot texture binding. ``dds_mips`` is the on-disk path
    list; the highest-priority entry (typically ``.dd0``) becomes the
    Blender Image source after the publisher's PNG-conversion pass."""

    dds_mips: tuple[str, ...] = ()


@dataclass(frozen=True)
class MaterialEntry:
    """One material from sidecar's ``materials[]`` block.

    The Blender add-on matches by ``material_id`` against the GLB's
    material names, so the GLB importer + sidecar binding stay
    independent — the importer doesn't care about the sidecar's
    ``mesh_slots`` reverse index, that's the producer's concern.
    """

    material_id:    str
    display_name:   str
    shader_intent:  str = "opaque_pbr"
    render_queue:   str = "opaque"
    double_sided:   bool = False
    texture_sets:   dict[str, dict[str, TextureRef]] = field(default_factory=dict)
    factors:        dict[str, Any] = field(default_factory=dict)
    uv_channels:    dict[str, int] = field(default_factory=dict)
    detail_params:  dict[str, Any] = field(default_factory=dict)
    emission_anim:  dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Placement:
    """One mount or decorative placement.

    The five sidecar arrays (``turrets`` / ``secondaries`` /
    ``antiair`` / ``torpedoes`` / ``accessories``) all share this
    shape — only the semantic role differs. ``role`` is set by the
    loader based on which array the placement came from.
    """

    role:            str   # "turret" | "secondary" | "antiair" | "torpedo" | "accessory"
    instance_id:     str
    asset_id:        str
    hp_name:         str | None
    parent_section:  str | None
    parent_mesh:     str | None
    scope:           str | None
    category:        str | None
    subcategory:     str | None
    matrix:          tuple[float, ...]   # 16 floats, column-major glTF convention
    attached_y_flip: bool = False
    dead_asset_id:   str | None = None
    misc_filter:     tuple[str, ...] | None = None


@dataclass(frozen=True)
class Sidecar:
    """Parsed view over a ``<Ship>.meta.json`` document.

    ``raw`` is the un-touched JSON dict — escape hatch for any field
    not modeled here (the add-on uses it for skins[] today).
    """

    ship_name:    str
    schema_version: int
    materials:    tuple[MaterialEntry, ...]
    placements:   tuple[Placement, ...]
    skins:        tuple[dict[str, Any], ...] = ()
    raw:          dict[str, Any] = field(default_factory=dict)


_PLACEMENT_ROLES: tuple[tuple[str, str], ...] = (
    ("turrets",     "turret"),
    ("secondaries", "secondary"),
    ("antiair",     "antiair"),
    ("torpedoes",   "torpedo"),
    ("accessories", "accessory"),
)


def _coerce_texture_ref(raw: Any) -> TextureRef:
    if not isinstance(raw, dict):
        return TextureRef()
    mips = raw.get("dds_mips") or ()
    if isinstance(mips, str):
        mips = (mips,)
    else:
        mips = tuple(mips)
    return TextureRef(dds_mips=mips)


def coerce_material(raw: dict[str, Any]) -> MaterialEntry:
    """Public alias for :func:`_coerce_material`; also used by the
    library-index lift (accessory materials follow the same shape but
    arrive embedded inside the index, not the top-level sidecar)."""
    return _coerce_material(raw)


def _coerce_material(raw: dict[str, Any]) -> MaterialEntry:
    ts_raw = raw.get("texture_sets") or {}
    texture_sets: dict[str, dict[str, TextureRef]] = {}
    for scheme, slots in ts_raw.items():
        if not isinstance(slots, dict):
            continue
        texture_sets[scheme] = {
            slot: _coerce_texture_ref(ref) for slot, ref in slots.items()
        }
    return MaterialEntry(
        material_id=raw.get("material_id", ""),
        display_name=raw.get("display_name", "") or raw.get("material_id", ""),
        shader_intent=raw.get("shader_intent", "opaque_pbr"),
        render_queue=raw.get("render_queue", "opaque"),
        double_sided=bool(raw.get("double_sided", False)),
        texture_sets=texture_sets,
        factors=dict(raw.get("factors") or {}),
        uv_channels=dict(raw.get("uv_channels") or {}),
        detail_params=dict(raw.get("detail_params") or {}),
        emission_anim=dict(raw.get("emission_anim") or {}),
    )


def _coerce_placement(role: str, raw: dict[str, Any]) -> Placement | None:
    transform = raw.get("transform") or {}
    matrix = transform.get("matrix")
    if not (isinstance(matrix, list) and len(matrix) == 16):
        # Some species carry only a position vector — skip; the add-on
        # would have no orientation to apply.
        return None
    misc_filter_raw = raw.get("misc_filter")
    misc_filter: tuple[str, ...] | None
    if isinstance(misc_filter_raw, list):
        misc_filter = tuple(str(x) for x in misc_filter_raw)
    else:
        misc_filter = None
    return Placement(
        role=role,
        instance_id=str(raw.get("instance_id") or ""),
        asset_id=str(raw.get("asset_id") or ""),
        hp_name=raw.get("hp_name"),
        parent_section=raw.get("parent_section"),
        parent_mesh=raw.get("parent_mesh"),
        scope=raw.get("scope"),
        category=raw.get("category"),
        subcategory=raw.get("subcategory"),
        matrix=tuple(float(v) for v in matrix),
        attached_y_flip=bool(raw.get("attached_y_flip", False)),
        dead_asset_id=raw.get("dead_asset_id"),
        misc_filter=misc_filter,
    )


def _resolve_ship_name(raw: dict[str, Any], fallback: str) -> str:
    """Extract a single display-friendly ship name from the sidecar's
    ``ship`` field.

    Older sidecars used a string; current schema is a nested dict with
    ``display_name`` / ``ship_key`` / ``wg_ship_id`` etc. Preference
    order: ``display_name`` (user-facing), ``ship_key`` (pipeline-
    canonical), ``wg_ship_id`` (raw WG asset ID), then the filename
    stem as last resort.
    """
    ship = raw.get("ship")
    if isinstance(ship, str) and ship:
        return ship
    if isinstance(ship, dict):
        for key in ("display_name", "ship_key", "wg_ship_id"):
            v = ship.get(key)
            if isinstance(v, str) and v:
                return v
    return fallback


def load_sidecar(path: Path) -> Sidecar:
    """Parse a ``<Ship>.meta.json`` file from disk.

    Note: placements live in the SIBLING
    ``models/<Ship>_accessories.json``, not in the sidecar itself.
    See :func:`load_accessories` for that side of the schema.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    materials = tuple(
        _coerce_material(m) for m in (raw.get("materials") or [])
        if isinstance(m, dict)
    )
    return Sidecar(
        ship_name=_resolve_ship_name(raw, path.stem.removesuffix(".meta")),
        schema_version=int(raw.get("schema_version") or 0),
        materials=materials,
        placements=(),  # filled by load_accessories merge
        skins=tuple(raw.get("skins") or ()),
        raw=raw,
    )


def load_accessories(path: Path) -> tuple[Placement, ...]:
    """Parse the sibling ``<Ship>_accessories.json``.

    Returns the merged placement stream across all five role arrays.
    Mirrors what the webview's `ship.ts` does — flatten + tag with role
    so a single loop can drive instantiation.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[Placement] = []
    for key, role in _PLACEMENT_ROLES:
        for entry in raw.get(key) or []:
            if not isinstance(entry, dict):
                continue
            p = _coerce_placement(role, entry)
            if p is not None:
                out.append(p)
    return tuple(out)


def load_ship(sidecar_path: Path) -> Sidecar:
    """Convenience: load sidecar + paired accessories.json in one call.

    ``sidecar_path`` points at ``<Ship>.meta.json``; the paired
    accessories file is discovered at
    ``<sidecar_dir>/models/<Ship>_accessories.json``.
    """
    sidecar_path = Path(sidecar_path)
    sc = load_sidecar(sidecar_path)
    ship_dir = sidecar_path.parent
    ship_stem = sidecar_path.name.removesuffix(".meta.json")
    accessories_path = ship_dir / "models" / f"{ship_stem}_accessories.json"
    placements: tuple[Placement, ...] = ()
    if accessories_path.is_file():
        placements = load_accessories(accessories_path)
    return Sidecar(
        ship_name=sc.ship_name,
        schema_version=sc.schema_version,
        materials=sc.materials,
        placements=placements,
        skins=sc.skins,
        raw=sc.raw,
    )


__all__ = [
    "TextureRef",
    "MaterialEntry",
    "Placement",
    "Sidecar",
    "coerce_material",
    "load_sidecar",
    "load_accessories",
    "load_ship",
]
