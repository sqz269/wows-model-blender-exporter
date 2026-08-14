"""Which hull meshes are actually the ship — LOD / damage / overlay rules.

A WoWS hull GLB is not just the ship. Massachusetts' hull carries 146
meshes, of which only 23 are the intact high-detail hull: the rest are
79 coarser LOD substitutes, 69 damage-state variants (cracks + patches)
co-located with the intact geometry, and 37 Armor / Hitboxes collision
volumes. Import all of it verbatim and you get four overlapping copies
of the ship inside a solid armour shell.

The webview solves this at render time by toggling visibility. These are
the same rules, ported verbatim from
``webview/src/lib/ship/visibility.ts`` + ``classify_hull.ts`` — including
the detail that LOD suffixes come in **two** flavours (``_lod1Shape``
and ``_lodShape1``); matching only the first silently keeps a second set
of duplicates.

Keep in sync with the webview if a third flavour ever appears.
"""
from __future__ import annotations

import re

#: Non-LOD0 marker. Two naming flavours observed post-load:
#:   ``<base>_lod1Shape`` / ``_lod2Shape`` / ...
#:   ``<base>_lodShape1`` / ``_lodShape2`` / ...
LOD_RE = re.compile(r"_lod(?:[1-9]|Shape[1-9])", re.I)
_LOD_LEVEL_RE = re.compile(r"_lod(?:Shape)?([1-9][0-9]*)", re.I)

PATCH_RE = re.compile(r"_patch_", re.I)
CRACK_RE = re.compile(r"_crack_", re.I)

#: Debug / collision overlays the user opts into, never part of the ship.
HULL_HIDDEN_GROUPS: frozenset[str] = frozenset({"Armor", "Hitboxes"})


def lod_level_of_name(name: str) -> int:
    """LOD level from a mesh name; 0 for the default high-detail mesh."""
    if not name:
        return 0
    m = _LOD_LEVEL_RE.search(name)
    return int(m.group(1)) if m else 0


def is_damage_variant(name: str) -> bool:
    """True for crack / patch meshes — hidden unless explicitly asked for."""
    return bool(PATCH_RE.search(name) or CRACK_RE.search(name))


def short_mesh_name(raw: str) -> str:
    """Strip a parent-group prefix some importers glue onto mesh names.

    ``<Group>__<Mesh>`` (gltFast) or ``<Group> / <Mesh>`` (GLTFLoader).
    Blender's glTF importer does neither, but names arriving from other
    tools may, and WG mesh names never contain ``__`` themselves.
    """
    if not raw:
        return raw
    dd = raw.rfind("__")
    if dd >= 0:
        return raw[dd + 2:]
    slash = raw.rfind(" / ")
    return raw[slash + 3:] if slash >= 0 else raw


def lod_policy_level(policy: str) -> int | None:
    """Parse ``'lod0'`` / ``'lod2'`` to a level; None for ``'all'``."""
    if not policy or policy == "all":
        return None
    m = re.fullmatch(r"lod([0-9]+)", policy.strip(), re.I)
    return int(m.group(1)) if m else None


def keeps_mesh(
    name: str,
    *,
    lod_policy: str = "lod0",
    damage_variants: bool = False,
) -> bool:
    """Whether a mesh survives the content filter.

    ``lod_policy`` of ``'all'`` keeps every level; ``'lodN'`` keeps only
    level N (so ``'lod0'``, the default, keeps the high-detail hull).
    """
    target = lod_policy_level(lod_policy)
    if target is not None and lod_level_of_name(name) != target:
        return False
    if not damage_variants and is_damage_variant(name):
        return False
    return True


__all__ = [
    "LOD_RE",
    "PATCH_RE",
    "CRACK_RE",
    "HULL_HIDDEN_GROUPS",
    "lod_level_of_name",
    "is_damage_variant",
    "short_mesh_name",
    "lod_policy_level",
    "keeps_mesh",
]
