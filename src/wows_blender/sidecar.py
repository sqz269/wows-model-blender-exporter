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
    #: Placement provenance. ``"skel_ext_hash"`` marks the hull's baked
    #: decoratives layer — the set a hull-swap exterior replaces wholesale
    #: with its own decoratives file (Unity ExteriorComposer parity).
    source:          str | None = None


@dataclass(frozen=True)
class ColorScheme:
    """A skin's 4-row palette (Path A). ``colors`` are linear RGBA; the
    ``.a`` of each row is its premix weight against the base."""

    name:   str
    colors: tuple[tuple[float, float, float, float], ...] = ()


@dataclass(frozen=True)
class CamoCategory:
    """One ``skin.categories[cat]`` entry — Path A mask and/or Path B mgn
    for a part category (tile/deckhouse/bulge/gun/...)."""

    mask:      tuple[str, ...] = ()   # Path A zone-mask dds_mips
    mgn:       tuple[str, ...] = ()   # Path B mgn dds_mips
    uv_scale:  tuple[float, float] = (1.0, 1.0)
    uv_offset: tuple[float, float] = (0.0, 0.0)
    # Radians about UV center (0.5, 0.5), applied BEFORE scale+offset
    # (engine camoRepeatsRotate order). Absent on pre-2026-08 sidecars.
    uv_rotate: float = 0.0
    params:    dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MatTexture:
    """One ``skin.mat_textures[cat]`` entry — Path B pre-baked albedo
    (+ optional mgn) for a part category."""

    albedo:    tuple[str, ...] = ()
    mgn:       tuple[str, ...] = ()
    uv_scale:  tuple[float, float] = (1.0, 1.0)
    uv_offset: tuple[float, float] = (0.0, 0.0)
    uv_rotate: float = 0.0            # radians about (0.5, 0.5), pre-scale/offset
    params:    dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Skin:
    """One ``skins[]`` entry. ``skin_id == 'default'`` is the bare ship.

    Path A is driven by ``color_scheme`` + ``categories[cat].mask``;
    Path B by ``mat_textures[cat]``. ``scheme_key`` selects the material
    ``texture_sets`` block to sample (usually ``main``)."""

    skin_id:      str
    scheme_key:   str
    display_name: str
    kind:         str | None = None
    exterior_id:  str | None = None
    color_scheme: ColorScheme | None = None
    categories:   dict[str, CamoCategory] = field(default_factory=dict)
    mat_textures: dict[str, MatTexture] = field(default_factory=dict)
    params:       dict[str, Any] = field(default_factory=dict)
    raw:          dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExteriorMount:
    """One ``exteriors[].mounts[]`` record — a per-hardpoint asset swap.

    ``misc_filter`` is TRI-STATE, mirroring the WG nodesConfig override
    (Unity ExteriorComposer parity): ``None`` = field absent = keep every
    attached child; ``()`` = drop all; non-empty = whitelist by
    ``placement_id``. It replaces the vanilla whitelist VERBATIM.

    ``matrix`` is the exterior's own placement matrix with the schema_v6
    bone-mismatch Ry(180°) conjugation already baked in — consumers
    decompose verbatim, never re-derive from the base placement. ``None``
    for transform-less swaps (asset/misc-filter-only): mirror the base.

    ``attach_to`` marks turret-rider mounts (composite hp names like
    ``HP_AGM_3_HP_AGA_4``): the rider parents to the variant host
    turret's matching child node at identity local, or is dropped when
    the variant model has no such node (WG's visual exclusion).
    """

    hp_name:         str
    asset_id:        str | None
    base_asset_id:   str | None = None
    dead_asset_id:   str | None = None
    attach_to:       str | None = None
    matrix:          tuple[float, ...] | None = None
    misc_filter:     tuple[str, ...] | None = None
    attached_y_flip: bool = False


@dataclass(frozen=True)
class ExteriorHull:
    """The ``exteriors[].hull`` block — present when the exterior swaps
    hull geometry (re-ingested with ``--exterior-hulls``)."""

    hull_glb:          str | None = None
    material_mappings: str | None = None
    decoratives:       str | None = None
    materials:         tuple[MaterialEntry, ...] = ()


@dataclass(frozen=True)
class Exterior:
    """One ``exteriors[]`` entry — a permoflage that may swap hull
    geometry, mounts, and decoratives on top of the canonical ship."""

    exterior_id:     str
    display_name:    str
    wg_asset_id:     str | None = None
    peculiarity:     str | None = None
    camo_scheme_key: str | None = None
    is_native:       bool = False
    hull:            ExteriorHull | None = None
    mounts:          tuple[ExteriorMount, ...] = ()
    variant_swapped_asset_ids: tuple[str, ...] = ()


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
    exteriors:    tuple[Exterior, ...] = ()
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
        source=raw.get("source"),
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


def _coerce_exterior_mount(raw: dict[str, Any]) -> ExteriorMount | None:
    hp = raw.get("hp_name")
    if not hp:
        return None
    matrix_raw = (raw.get("transform") or {}).get("matrix")
    matrix: tuple[float, ...] | None = None
    if isinstance(matrix_raw, list) and len(matrix_raw) == 16:
        matrix = tuple(float(v) for v in matrix_raw)
    mf_raw = raw.get("misc_filter")
    misc_filter = tuple(str(x) for x in mf_raw) if isinstance(mf_raw, list) else None
    return ExteriorMount(
        hp_name=str(hp),
        asset_id=raw.get("asset_id") or None,
        base_asset_id=raw.get("base_asset_id") or None,
        dead_asset_id=raw.get("dead_asset_id") or None,
        attach_to=raw.get("attach_to") or None,
        matrix=matrix,
        misc_filter=misc_filter,
        attached_y_flip=bool(raw.get("attached_y_flip", False)),
    )


def _coerce_exterior(raw: dict[str, Any]) -> Exterior | None:
    ext_id = raw.get("exterior_id")
    if not ext_id:
        return None
    hull_raw = raw.get("hull")
    hull: ExteriorHull | None = None
    if isinstance(hull_raw, dict):
        hull = ExteriorHull(
            hull_glb=hull_raw.get("hull_glb") or None,
            material_mappings=hull_raw.get("material_mappings") or None,
            decoratives=hull_raw.get("decoratives") or None,
            materials=tuple(
                _coerce_material(m) for m in (hull_raw.get("materials") or [])
                if isinstance(m, dict)
            ),
        )
    mounts = tuple(
        m for m in (
            _coerce_exterior_mount(r) for r in (raw.get("mounts") or [])
            if isinstance(r, dict)
        )
        if m is not None
    )
    return Exterior(
        exterior_id=str(ext_id),
        display_name=str(raw.get("display_name") or ext_id),
        wg_asset_id=raw.get("wg_asset_id") or None,
        peculiarity=raw.get("peculiarity") or None,
        camo_scheme_key=raw.get("camo_scheme_key") or None,
        is_native=bool(raw.get("is_native", False)),
        hull=hull,
        mounts=mounts,
        variant_swapped_asset_ids=tuple(
            str(x) for x in (raw.get("variant_swapped_asset_ids") or [])
        ),
    )


def load_exteriors(raw_exteriors: Any) -> tuple[Exterior, ...]:
    """Parse the sidecar's ``exteriors[]`` into typed :class:`Exterior`s."""
    return tuple(
        e for e in (
            _coerce_exterior(r) for r in (raw_exteriors or [])
            if isinstance(r, dict)
        )
        if e is not None
    )


def load_decoratives(path: Path) -> tuple[Placement, ...]:
    """Parse an exterior's ``*_decoratives.json``.

    The file shares the accessories.json shape (five typed placement
    groups), so this is :func:`load_accessories` under a name that says
    what it's for.
    """
    return load_accessories(path)


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
        exteriors=load_exteriors(sc.raw.get("exteriors")),
        raw=sc.raw,
    )


def _mips(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, dict):
        return ()
    m = raw.get("dds_mips") or ()
    if isinstance(m, str):
        return (m,)
    return tuple(str(x) for x in m)


def _uv(raw: Any) -> tuple[tuple[float, float], tuple[float, float], float]:
    uv = raw.get("uv") if isinstance(raw, dict) else None
    uv = uv or {}
    scale = uv.get("scale") or (1.0, 1.0)
    offset = uv.get("offset") or (0.0, 0.0)
    return (
        (float(scale[0]), float(scale[1])),
        (float(offset[0]), float(offset[1])),
        float(uv.get("rotate") or 0.0),
    )


def _coerce_color_scheme(raw: Any) -> ColorScheme | None:
    if not isinstance(raw, dict):
        return None
    cols: list[tuple[float, float, float, float]] = []
    for c in raw.get("colors") or []:
        if isinstance(c, (list, tuple)) and len(c) >= 3:
            cols.append((
                float(c[0]), float(c[1]), float(c[2]),
                float(c[3]) if len(c) > 3 else 1.0,
            ))
    return ColorScheme(name=str(raw.get("name") or ""), colors=tuple(cols))


def _coerce_camo_category(raw: dict[str, Any]) -> CamoCategory:
    scale, offset, rotate = _uv(raw)
    return CamoCategory(
        mask=_mips(raw.get("mask")),
        mgn=_mips(raw.get("mgn")),
        uv_scale=scale,
        uv_offset=offset,
        uv_rotate=rotate,
        params=dict(raw.get("params") or {}),
    )


def _coerce_mat_texture(raw: dict[str, Any]) -> MatTexture:
    scale, offset, rotate = _uv(raw)
    return MatTexture(
        albedo=_mips(raw.get("albedo")),
        mgn=_mips(raw.get("mgn")),
        uv_scale=scale,
        uv_offset=offset,
        uv_rotate=rotate,
        params=dict(raw.get("params") or {}),
    )


def coerce_skin(raw: dict[str, Any]) -> Skin:
    cats = {
        k: _coerce_camo_category(v)
        for k, v in (raw.get("categories") or {}).items()
        if isinstance(v, dict)
    }
    mts = {
        k: _coerce_mat_texture(v)
        for k, v in (raw.get("mat_textures") or {}).items()
        if isinstance(v, dict)
    }
    return Skin(
        skin_id=str(raw.get("skin_id") or ""),
        scheme_key=str(raw.get("scheme_key") or "main"),
        display_name=str(raw.get("display_name") or raw.get("skin_id") or ""),
        kind=raw.get("kind"),
        exterior_id=raw.get("exterior_id"),
        color_scheme=_coerce_color_scheme(raw.get("color_scheme")),
        categories=cats,
        mat_textures=mts,
        params=dict(raw.get("params") or {}),
        raw=dict(raw),
    )


def load_skins(raw_skins: Any) -> tuple[Skin, ...]:
    """Parse the sidecar's ``skins[]`` into typed :class:`Skin` objects."""
    return tuple(
        coerce_skin(s) for s in (raw_skins or []) if isinstance(s, dict)
    )


__all__ = [
    "TextureRef",
    "MaterialEntry",
    "Placement",
    "ColorScheme",
    "CamoCategory",
    "MatTexture",
    "Skin",
    "ExteriorMount",
    "ExteriorHull",
    "Exterior",
    "Sidecar",
    "coerce_material",
    "coerce_skin",
    "load_skins",
    "load_exteriors",
    "load_decoratives",
    "load_sidecar",
    "load_accessories",
    "load_ship",
]
