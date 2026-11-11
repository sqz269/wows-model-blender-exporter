"""Resolve sidecar / library-index texture paths to files on disk.

Every texture path the producer writes is one of two kinds, told apart
by its prefix (``METADATA_SPEC.md`` section 9.1, "Texture paths"):

* ``libraries/...`` is relative to the **workspace root**. In a publish
  tree the ``libraries/`` prefix maps to the publish root itself
  (``libraries/camo_masks/X`` -> ``<dest>/camo_masks/X``,
  ``libraries/textures_shared/<key>/X`` -> ``<dest>/textures_shared/<key>/X``).
  This covers the camo libraries and the shared texture store, which
  holds every texture set that several export folders bind byte for byte
  (the ship detail atlas, a turret's maps shared by its alive / dead /
  variant models, a skin repeating its ship's hull maps, ...), for any
  slot.
* anything else is relative to the directory of the model that owns it
  (the ship's ``models/`` dir, the accessory's asset dir).

The publisher's DDS -> PNG pass writes a PNG next to each DDS; the
add-on prefers the PNG.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

WORKSPACE_PATH_PREFIX = "libraries/"


def _workspace_roots(model_root: Path, publish_root: Path | None) -> list[Path]:
    """Directories a ``libraries/`` path may be relative to: the publish
    root when known, then the model root and its ancestors (the publish
    or workspace root is one of them)."""
    roots: list[Path] = []
    if publish_root is not None:
        roots.append(Path(publish_root))
    roots.append(Path(model_root))
    roots.extend(Path(model_root).parents)
    return roots


def candidate_paths(
    rel: str,
    model_root: Path,
    publish_root: Path | None = None,
) -> list[Path]:
    """On-disk candidates for one texture path, most likely first."""
    rel = str(rel).replace("\\", "/")
    if not rel.startswith(WORKSPACE_PATH_PREFIX):
        return [Path(model_root) / rel]
    stripped = rel[len(WORKSPACE_PATH_PREFIX):]
    out: list[Path] = []
    for root in _workspace_roots(model_root, publish_root):
        out.append(root / stripped)  # publish tree: libraries/ -> root
        out.append(root / rel)       # workspace tree: literal path
    return out


def resolve_png(
    model_root: Path,
    dds_mips: Sequence[str],
    publish_root: Path | None = None,
) -> Path | None:
    """Resolve a mip list to the PNG the publisher wrote (top mip first),
    falling back to a ``.dds`` file Blender may be able to load."""
    for rel in dds_mips:
        for cand in candidate_paths(rel, model_root, publish_root):
            png = cand.with_suffix(".png")
            if png.is_file():
                return png
    for rel in dds_mips:
        for cand in candidate_paths(rel, model_root, publish_root):
            if cand.suffix.lower() == ".dds" and cand.is_file():
                return cand
    return None


__all__ = ["WORKSPACE_PATH_PREFIX", "candidate_paths", "resolve_png"]
