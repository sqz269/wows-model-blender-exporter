"""Hull modules — which hull a build shows, its mounts and its linked skin.

Pure stdlib (no ``bpy``), like :mod:`wows_blender.sidecar`, so the
publisher CLI can list hulls without starting Blender.

The producer exports ONE hull GLB per ship (the model of the lowest
upgrade id) and places the mounts of one hull tier on it: the
**rendered hull**, ``variants.rendered_hull``. The render doc
``models/<Ship>_accessories.json`` holds those mounts. Every hull the
ship lists has a mount snapshot in the sidecar's ``hulls.<H>`` block
(METADATA_SPEC.md §11.2), so another hull is shown by swapping the
render doc's HP-bound mounts for that snapshot; the hull mesh and the
skel_ext decoratives stay.

A hull may also link a skin: ``hulls.<H>.linked_skin`` is the
``skins[].skin_id`` of the Exterior its GameParams component names
through ``linkedCamouflage`` (the event ships' per-branch Bermuda tiles,
Obsession's bot tile). The game shows that skin whenever the hull is
the selected one, so a build that is not told which skin to paint
paints the shown hull's linked skin. Every other skin stays selectable.

Webview parity: ``webview/src/lib/ship/viewer.ts`` (``defaultHullName``,
``applyHullSnapshot`` and the hull-linked skin step of ``loadShip``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The placement sections a hull snapshot carries (same as the render doc).
HULL_SECTIONS: tuple[str, ...] = (
    "turrets", "secondaries", "antiair", "torpedoes", "accessories",
)

#: The sections the rendered-hull fallback compares. The top-level
#: ``accessories`` also carry the hull decoratives, so the producer leaves
#: them out (``scaffold_ship._exact_mount_hull``).
_WEAPON_SECTIONS: tuple[str, ...] = ("turrets", "secondaries", "antiair", "torpedoes")


class UnknownHull(LookupError):
    """A requested hull is not in the sidecar's ``hulls`` block."""


@dataclass(frozen=True)
class HullOption:
    """One ``hulls.<H>`` entry, as a hull picker shows it."""

    name:               str
    module_id:          str | None
    is_stock:           bool
    is_active:          bool
    #: The hull whose mounts the render doc holds (the default build).
    is_rendered:        bool
    linked_exterior_id: str | None
    linked_skin:        str | None
    #: Distinct main-battery asset ids, to tell the hulls apart.
    main_assets:        tuple[str, ...]


def _hulls(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    hulls = raw.get("hulls")
    if not isinstance(hulls, dict):
        return {}
    return {str(k): v for k, v in hulls.items() if isinstance(v, dict)}


def _mount_keys(block: dict[str, Any]) -> frozenset[tuple[str, str, str]]:
    """``(section, hp_name, asset_id)`` of every weapon mount in a block."""
    out: set[tuple[str, str, str]] = set()
    for section in _WEAPON_SECTIONS:
        for m in block.get(section) or []:
            if isinstance(m, dict):
                out.add((section, str(m.get("hp_name")), str(m.get("asset_id"))))
    return frozenset(out)


def _active_hull_name(raw: dict[str, Any], hulls: dict[str, dict[str, Any]]) -> str:
    named = (raw.get("variants") or {}).get("active_hull")
    if isinstance(named, str) and named in hulls:
        return named
    for n, h in hulls.items():
        if h.get("is_active"):
            return n
    return next(iter(hulls))


def rendered_hull_name(raw: dict[str, Any]) -> str | None:
    """The hull whose mounts the export placed (the render doc's hull).

    ``variants.rendered_hull`` when the sidecar names it. Sidecars made
    before 2026-11-10 do not; then, as the producer's own
    ``scaffold_ship.rendered_hull_name``: the hull whose weapon mounts
    equal the top-level ones exactly (spec §11.1; the active hull on a
    tie, else the first name in sort order), else the hull sharing the
    most mounts with the top level (the active hull unless another
    shares more), else the active hull. None when the sidecar has no
    ``hulls`` block.
    """
    hulls = _hulls(raw)
    if not hulls:
        return None
    named = (raw.get("variants") or {}).get("rendered_hull")
    if isinstance(named, str) and named in hulls:
        return named
    active = _active_hull_name(raw, hulls)
    top = _mount_keys(raw)
    exact = [n for n, h in hulls.items() if _mount_keys(h) == top]
    if exact:
        return active if active in exact else sorted(exact)[0]
    if top and len(hulls) >= 2:
        scores = {n: len(top & _mount_keys(h)) for n, h in hulls.items()}
        best = max(scores, key=lambda k: scores[k])
        if best != active and scores[best] > scores.get(active, -1):
            return best
    return active


def hull_options(raw: dict[str, Any]) -> tuple[HullOption, ...]:
    """Every hull of the sidecar, in sidecar order."""
    rendered = rendered_hull_name(raw)
    out: list[HullOption] = []
    for name, h in _hulls(raw).items():
        mains: list[str] = []
        for m in h.get("turrets") or []:
            aid = str((m or {}).get("asset_id") or "")
            if aid and aid not in mains:
                mains.append(aid)
        out.append(HullOption(
            name=name,
            module_id=h.get("module_id") or None,
            is_stock=bool(h.get("is_stock")),
            is_active=bool(h.get("is_active")),
            is_rendered=name == rendered,
            linked_exterior_id=h.get("linked_exterior_id") or None,
            linked_skin=h.get("linked_skin") or None,
            main_assets=tuple(mains),
        ))
    return tuple(out)


def resolve_hull_name(raw: dict[str, Any], requested: str) -> str:
    """Resolve a hull request to a ``hulls`` key.

    Accepts the key itself (``C_Hull``), any case of it, or the hull's
    upgrade module id (``PXUH803_HULL_C``). Raises :class:`UnknownHull`
    with the available names when nothing matches, and when the sidecar
    has no ``hulls`` block at all.
    """
    hulls = _hulls(raw)
    if requested in hulls:
        return requested
    lowered = requested.lower()
    for name, h in hulls.items():
        if name.lower() == lowered or str(h.get("module_id") or "").lower() == lowered:
            return name
    available = ", ".join(hulls) or "(none: the sidecar has no hulls block)"
    raise UnknownHull(f"hull {requested!r} not in this sidecar; available: {available}")


def linked_skin_for(raw: dict[str, Any], hull: str | None) -> str | None:
    """``hulls.<hull>.linked_skin``, or None."""
    if not hull:
        return None
    h = _hulls(raw).get(hull)
    if h is None:
        return None
    return h.get("linked_skin") or None


def apply_hull_snapshot(
    doc: dict[str, Any], raw: dict[str, Any], hull: str,
) -> dict[str, Any]:
    """Return a copy of the render doc showing ``hull``'s mounts.

    Typed sections are replaced wholesale by the hull's snapshot.
    Accessories split like the producer's active-hull alias: an instance
    any hull's snapshot lists is HP-bound (replaced), everything else is
    a decorative (kept). A render-doc entry for the same instance at the
    same hardpoint and asset keeps the fields the snapshot lacks
    (``dead_asset_id``, ``source``, ...), with the snapshot's values on
    top. The input ``doc`` is not modified.
    """
    hulls = _hulls(raw)
    target = hulls[hull]
    hp_bound: set[str] = set()
    for h in hulls.values():
        for a in h.get("accessories") or []:
            if isinstance(a, dict) and a.get("instance_id"):
                hp_bound.add(str(a["instance_id"]))
    out = dict(doc)
    for section in HULL_SECTIONS:
        current = [m for m in (doc.get(section) or []) if isinstance(m, dict)]
        by_id = {str(m.get("instance_id") or ""): m for m in current}
        snapshot: list[dict[str, Any]] = []
        for m in target.get(section) or []:
            if not isinstance(m, dict):
                continue
            prev = by_id.get(str(m.get("instance_id") or ""))
            if (prev is not None and prev.get("hp_name") == m.get("hp_name")
                    and prev.get("asset_id") == m.get("asset_id")):
                snapshot.append({**prev, **m})
            else:
                snapshot.append(dict(m))
        kept = (
            [m for m in current if str(m.get("instance_id") or "") not in hp_bound]
            if section == "accessories" else []
        )
        out[section] = [*snapshot, *kept]
    return out


def hull_misc_filters(
    raw: dict[str, Any], hull: str,
) -> dict[str, list[str] | None]:
    """Per-instance ``misc_filter`` from ``hulls.<hull>``'s own records.

    The snapshot fields come from the hull's own components (spec §11.3
    pass 3b), so they win over the top-level values, which describe the
    rendered hull. TRI-STATE like everywhere else: only entries that
    carry the key are returned (``None`` = keep every attachment,
    ``[]`` = drop all, ``[ids]`` = whitelist).
    """
    out: dict[str, list[str] | None] = {}
    h = _hulls(raw).get(hull) or {}
    for section in HULL_SECTIONS:
        for m in h.get(section) or []:
            if not isinstance(m, dict):
                continue
            iid = m.get("instance_id")
            if iid and "misc_filter" in m:
                mf = m.get("misc_filter")
                out[str(iid)] = [str(x) for x in mf] if isinstance(mf, list) else None
    return out


__all__ = [
    "HULL_SECTIONS",
    "HullOption",
    "UnknownHull",
    "apply_hull_snapshot",
    "hull_misc_filters",
    "hull_options",
    "linked_skin_for",
    "rendered_hull_name",
    "resolve_hull_name",
]
