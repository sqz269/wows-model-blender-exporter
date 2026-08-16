"""Camo / permoflage resolution — pure stdlib (no bpy).

This module is the consumer-side port of the webview's camo dispatch
(`webview/src/lib/ship/textures/manager.ts::updateCamoUniforms` +
`lib/types/categories.ts`). It decides, for a given hull material and an
active skin, WHICH mask + palette + UV drive the Path A paint — without
touching Blender, so it stays unit-testable.

The shader math itself (the 4-row palette lerp, the mg.B gate) lives in
the bpy-side node-group builder; this module only resolves the inputs.

Path A (palette hull tint), from the webview GLSL:
    baseRgb = albedo * (1 - mg.G)                     # mg.G = metallic mask
    Pi      = lerp(baseRgb, colors[i].rgb, colors[i].a)   i = 0..3
    step1   = lerp(P0, P1, mask.r)
    step2   = lerp(step1, P2, mask.g)
    step3   = lerp(step2, P3, mask.b)
    final   = lerp(baseRgb, step3, mg.B)              # mg.B = camoExclusion.R
mask is sampled at rot(vMapUv) * uv.scale + uv.offset (rotate about the
tile center first); mask.a is unused.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .sidecar import CamoCategory, MaterialEntry, MatTexture, Skin

# Hull-side stems classify into one of these; the rest are accessory-side.
HULL_CATEGORIES: frozenset[str] = frozenset({"tile", "deckhouse", "bulge"})

# Albedo texture suffix to strip when recovering the MFM stem from a
# baseColor filename (e.g. ``..._bulge_a.dd0`` -> ``..._bulge``).
_ALBEDO_SUFFIX = "_a"


def classify_part_category(stem: str) -> str:
    """Classify an MFM stem / asset_id into a camo part category.

    Direct port of webview ``classifyPartCategory`` (itself a port of the
    toolkit's ``camouflage.rs``). Hull rules read the suffix; accessory
    rules read the 2-letter type code at positions 1-2. Falls back to
    ``tile`` (WG runtime's "default to hull mask").
    """
    lower = stem.lower()
    if lower.endswith("_hull") or lower.endswith("_hull_wire"):
        return "tile"
    if lower.endswith("_deckhouse"):
        return "deckhouse"
    if "_bulge" in lower:
        return "bulge"
    if len(stem) >= 4 and "A" <= stem[0] <= "Z":
        cat = stem[1:3]
        if cat in ("GM", "GS", "GA"):
            return "gun"
        if cat in ("D0", "D1", "F0", "F1"):
            return "director"
        if cat == "RS":
            return "misc"
        if stem[1] == "M" and len(stem) >= 3 and "0" <= stem[2] <= "9":
            return "misc"
        if cat in ("C0", "C1"):
            return "gun"
        if cat == "GT":
            return "misc"
    if "_hull" in lower:
        return "tile"
    return "tile"


def mfm_stem_from_basecolor(dds_mips: Sequence[str]) -> str | None:
    """Recover the MFM stem from a material's baseColor mip list.

    ``textures_dds/RSB026_Admiral_Ushakov_1955_bulge_a.dd0`` ->
    ``RSB026_Admiral_Ushakov_1955_bulge``. Strips the directory, the mip
    extension, and a trailing ``_a`` albedo suffix.
    """
    if not dds_mips:
        return None
    name = Path(str(dds_mips[0]).replace("\\", "/")).name
    stem = name.split(".")[0]  # drop .dd0 / .dds
    if stem.lower().endswith(_ALBEDO_SUFFIX):
        stem = stem[: -len(_ALBEDO_SUFFIX)]
    return stem


def material_category(entry: MaterialEntry) -> str:
    """Category for a hull material, classified from its main baseColor stem."""
    main = entry.texture_sets.get("main", {})
    bc = main.get("baseColor")
    stem = mfm_stem_from_basecolor(bc.dds_mips) if bc else None
    return classify_part_category(stem or entry.material_id)


def _candidate_rels(rel: str) -> list[str]:
    """On-disk-relative candidates for a camo library path.

    Sidecar camo paths carry a ``libraries/`` prefix
    (``libraries/camo_masks/X.dd0``) but the Blender publish places the
    atlases at the publish root (``camo_masks/X``). Try the stripped form
    first, then the literal path.
    """
    rel = rel.replace("\\", "/")
    out = []
    if rel.startswith("libraries/"):
        out.append(rel[len("libraries/"):])
    out.append(rel)
    return out


def resolve_camo_png(publish_root: Path, dds_mips: Sequence[str]) -> Path | None:
    """Resolve a camo mask/atlas mip list to a PNG on disk.

    ``publish_root`` is the dest root (the ship folder's parent), under
    which ``camo_masks/`` and ``camo_mat/`` live. Probes PNG siblings in
    mip priority order; falls back to a ``.dds`` if no PNG exists.
    """
    for rel in dds_mips:
        for cand in _candidate_rels(rel):
            png = (publish_root / cand).with_suffix(".png")
            if png.is_file():
                return png
    for rel in dds_mips:
        for cand in _candidate_rels(rel):
            dds = publish_root / cand
            if dds.is_file() and dds.suffix.lower() == ".dds":
                return dds
    return None


@dataclass(frozen=True)
class PathAResolved:
    """Everything the node-group builder needs to paint one material via
    Path A. ``mask_png`` is None when the skin has no usable mask for this
    material (then no camo is applied — the base PBR shows)."""

    category:    str
    mask_png:    Path | None
    uv_scale:    tuple[float, float]
    uv_offset:   tuple[float, float]
    colors:      tuple[tuple[float, float, float, float], ...]
    source:      str  # "category" (Step 1) | "per_stem" (Step 2) | "none"
    # Radians about UV center (0.5, 0.5), applied BEFORE scale+offset.
    uv_rotate:   float = 0.0


def resolve_path_a(
    entry: MaterialEntry,
    skin: Skin,
    publish_root: Path,
    model_root: Path,
) -> PathAResolved:
    """Resolve Path A inputs for one hull material + active skin.

    Mirrors manager.ts:1048-1067:
      * Step 1 — category override: if ``entry.category in skin.categories``
        with a ``mask`` and NO ``mgn``, use the category mask + its UV.
      * Step 2 — per-stem cascade: else use the material's
        ``texture_sets[scheme_key].baseColor`` as the mask (identity UV).

    ``skin.color_scheme`` must be present (Path A is palette-driven);
    callers gate on that. ``publish_root`` resolves camo-library masks;
    ``model_root`` resolves the per-stem fallback (ship-local textures).
    """
    category = material_category(entry)
    colors = skin.color_scheme.colors if skin.color_scheme else ()

    # noCamo: transparent materials (glass, periscopes) never take paint
    # (webview manager `noCamoKeys`). Skip before resolving any mask.
    if "transparent" in (entry.shader_intent or "").lower():
        return PathAResolved(category, None, (1.0, 1.0), (0.0, 0.0), colors, "none")

    # Step 1: category override (only when no MGN — Path B MGN wins).
    cat: CamoCategory | None = skin.categories.get(category)
    if cat is not None and cat.mask and not cat.mgn:
        png = resolve_camo_png(publish_root, cat.mask)
        if png is not None:
            return PathAResolved(category, png, cat.uv_scale, cat.uv_offset, colors,
                                 "category", uv_rotate=cat.uv_rotate)

    # Step 2: per-stem cascade — texture_sets[scheme_key].baseColor.
    # Ship-local (no libraries/ prefix); resolve_camo_png handles the
    # literal path against the model root just as well.
    scheme = skin.scheme_key
    per_stem = entry.texture_sets.get(scheme, {}).get("baseColor")
    if per_stem is not None and per_stem.dds_mips:
        png = resolve_camo_png(model_root, per_stem.dds_mips)
        if png is not None:
            return PathAResolved(category, png, (1.0, 1.0), (0.0, 0.0), colors, "per_stem")

    return PathAResolved(category, None, (1.0, 1.0), (0.0, 0.0), colors, "none")


@dataclass(frozen=True)
class PathBResolved:
    """Path B (``mat_textures``) — a pre-baked albedo atlas for a part
    category. ``albedo_png`` is None when the skin carries no Path-B entry
    for this material's category, which is the signal to fall through to
    Path A."""

    category:  str
    albedo_png: Path | None
    mgn_png:   Path | None
    uv_scale:  tuple[float, float]
    uv_offset: tuple[float, float]
    uv_rotate: float = 0.0  # radians about (0.5, 0.5), pre-scale/offset


def resolve_path_b(
    entry: MaterialEntry,
    skin: Skin,
    publish_root: Path,
    model_root: Path,
) -> PathBResolved:
    """Resolve Path B inputs for one material + active skin.

    Path B is a straight albedo swap: WG pre-baked the camo into a per-
    category atlas, so the consumer replaces the material's base colour
    with it rather than compositing a palette. The engine prefers B over
    A wherever a part carries both (memory
    ``project_camo_hybrid_path_ab``), so callers try this first.

    ``mat_textures`` paths carry the same ``libraries/`` prefix as the
    Path-A masks and resolve against ``publish_root``; the ship-local
    fallback uses ``model_root``.
    """
    category = material_category(entry)

    # noCamo: transparent materials (glass, periscopes) never take paint.
    if "transparent" in (entry.shader_intent or "").lower():
        return PathBResolved(category, None, None, (1.0, 1.0), (0.0, 0.0))

    mt: MatTexture | None = skin.mat_textures.get(category)
    if mt is None or not mt.albedo:
        return PathBResolved(category, None, None, (1.0, 1.0), (0.0, 0.0))

    albedo = resolve_camo_png(publish_root, mt.albedo)
    if albedo is None:
        albedo = resolve_camo_png(model_root, mt.albedo)
    if albedo is None:
        return PathBResolved(category, None, None, mt.uv_scale, mt.uv_offset,
                             uv_rotate=mt.uv_rotate)

    mgn = None
    if mt.mgn:
        mgn = resolve_camo_png(publish_root, mt.mgn) or resolve_camo_png(model_root, mt.mgn)

    return PathBResolved(category, albedo, mgn, mt.uv_scale, mt.uv_offset,
                         uv_rotate=mt.uv_rotate)


__all__ = [
    "HULL_CATEGORIES",
    "classify_part_category",
    "mfm_stem_from_basecolor",
    "material_category",
    "resolve_camo_png",
    "PathAResolved",
    "resolve_path_a",
    "PathBResolved",
    "resolve_path_b",
]
