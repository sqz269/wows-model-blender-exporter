"""Accessory library index reader.

The pipeline emits ``libraries/accessories/index.json`` mapping every
``asset_id`` to its on-disk GLB + material metadata. The Blender
add-on uses this to resolve a placement's ``asset_id`` to the actual
GLB file when instantiating accessories.

Pure stdlib so the index can be parsed outside Blender too.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LibraryAsset:
    """One per-asset entry from ``index.json``."""

    asset_id:    str
    scope:       str | None
    category:    str | None
    subcategory: str | None
    glb:         str            # path relative to the library root
    textures:    str | None     # PNG mirror dir (legacy producer; may be None)
    textures_dds: str | None    # raw DDS dir (always present for new producer)
    materials:   tuple[dict[str, Any], ...] = ()
    raw:         dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LibraryIndex:
    """Parsed view over ``libraries/accessories/index.json``."""

    version:      str | None
    asset_count:  int
    assets:       dict[str, LibraryAsset]


def load_library_index(path: Path) -> LibraryIndex:
    """Parse the accessory ``index.json``."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    assets_raw = raw.get("assets") or {}
    assets: dict[str, LibraryAsset] = {}
    for asset_id, entry in assets_raw.items():
        if not isinstance(entry, dict):
            continue
        materials = tuple(
            m for m in (entry.get("materials") or [])
            if isinstance(m, dict)
        )
        assets[asset_id] = LibraryAsset(
            asset_id=str(asset_id),
            scope=entry.get("scope"),
            category=entry.get("category"),
            subcategory=entry.get("subcategory"),
            glb=str(entry.get("glb") or ""),
            textures=entry.get("textures"),
            textures_dds=entry.get("textures_dds"),
            materials=materials,
            raw=entry,
        )
    return LibraryIndex(
        version=raw.get("version"),
        asset_count=int(raw.get("asset_count") or len(assets)),
        assets=assets,
    )


def resolve_asset_glb(library_root: Path, asset: LibraryAsset) -> Path | None:
    """Resolve an asset's ``glb`` field to an absolute path.

    Returns ``None`` if the GLB is missing on disk (the library may be
    partially published — e.g. the user ran ``wows-export-blender``
    on one ship without ``--accessories``).
    """
    if not asset.glb:
        return None
    p = library_root / asset.glb
    return p if p.is_file() else None


__all__ = [
    "LibraryAsset",
    "LibraryIndex",
    "load_library_index",
    "resolve_asset_glb",
]
