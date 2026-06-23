"""Sidecar-driven material binding.

For each Blender material that matches a sidecar ``material_id``, build
a Principled-BSDF node graph wired to PNG sibling textures of the
publisher's DDS files. WG slot -> Principled input map:

    sidecar slot          glTF role            Principled input
    -----------------     ------------------   ------------------------
    baseColor             baseColorTexture     Base Color (+ Alpha when
                                                cutout/transparent)
    metallicRoughness     metallicRoughness    Metallic (B) / Roughness (G)
    normal                normalTexture        Normal Map (tangent)
    occlusion             occlusionTexture     Multiply into Base Color
                                                (default_ao placeholder
                                                is skipped)
    emissive              emissiveTexture      Emission Color (+ Strength
                                                from factors)
    camoMask              custom               Path-B camo deny mask;
                                                NOT a surface texture —
                                                consumed by the skin
                                                overlay (Phase 3).
    camoExclusionMask     custom               Path-A mg.B paint gate;
                                                same — not bound here.
    detail                custom               Detail micro-normal atlas;
                                                blend deferred (Phase 1b).

Material-level ``factors`` (baseColor tint, metallic, roughness,
emissive, emissive_strength) drive the BSDF directly when the matching
texture slot is absent, and emissive_strength always. ``shader_intent``
/ ``render_queue`` select the alpha mode (opaque / cutout / transparent)
and ``double_sided`` toggles backface culling.

Render-state portability: alpha is driven primarily through the BSDF
``Alpha`` input (honored by Cycles AND EEVEE-Next), with a graph-level
``GREATER_THAN`` node for renderer-agnostic hard cutout. The EEVEE
surface method uses ``surface_render_method`` on Blender 4.2+ / 5.x
(EEVEE-Next) and falls back to ``blend_method`` on 4.0/4.1.

This module imports ``bpy`` and only runs inside Blender. Keep the
pure-Python helpers (sidecar, library_index, placement) free of bpy
imports so they stay testable outside Blender.
"""
from __future__ import annotations

import logging
from pathlib import Path

import bpy  # noqa: F401 — registered as a Blender add-on; ignore in tooling

from .sidecar import MaterialEntry, TextureRef

logger = logging.getLogger(__name__)


# Order matters: when a glTF importer auto-creates a material we want
# to know which sidecar slot wins. Webview uses ``main`` as the
# default scheme; we mirror that here.
DEFAULT_SCHEME: str = "main"

# Slots bound as visible PBR surface inputs. ``camoMask`` /
# ``camoExclusionMask`` are camo-system masks (Path A/B) consumed by the
# skin overlay, not surface textures; ``detail`` is a micro-normal atlas
# whose tangent-space blend is deferred. None are bound as a Base Color.
_PBR_BIND_ORDER: tuple[str, ...] = (
    "baseColor", "metallicRoughness", "normal", "occlusion", "emissive",
)

# AO maps whose stem matches one of these are 16x16 mid-grey placeholders
# the producer emits for materials with no real AO; binding them just
# uniformly dims the surface. Skip (mirrors Unity's ResolveTopMip).
_AO_PLACEHOLDER_STEMS: frozenset[str] = frozenset({"default_ao"})


def _resolve_png_for_texture(model_root: Path, ref: TextureRef) -> Path | None:
    """Resolve a TextureRef to a PNG file on disk.

    ``ref.dds_mips`` is a path list relative to the SHIP'S model
    directory (for hull materials) or the LIBRARY ASSET's directory
    (for accessory materials). The caller passes ``model_root`` to
    match.

    The publisher's DDS->PNG pass writes PNGs next to the DDS files
    with the same stem. We probe each mip in priority order and
    return the first PNG that exists.
    """
    for rel in ref.dds_mips:
        dds = model_root / rel
        png = dds.with_suffix(".png")
        if png.is_file():
            return png
    # Fallback: hand Blender the DDS directly. Newer Blender + OpenImageIO
    # builds can sometimes load .dds; .dd0 / .dd1 / .dd2 will fail but
    # the call site logs the miss.
    for rel in ref.dds_mips:
        dds = model_root / rel
        if dds.is_file() and dds.suffix.lower() == ".dds":
            return dds
    return None


def _load_image(path: Path, *, colorspace: str = "sRGB") -> bpy.types.Image | None:
    """Wrap ``bpy.data.images.load`` with idempotent reuse and the
    correct color space.

    Blender re-imports an image even when the same path is loaded a
    second time, ballooning the .blend size. Look up by absolute path
    first and reuse — applying the requested colorspace on BOTH the
    fresh-load and reuse paths so a per-call colorspace is never
    silently dropped on the reuse branch.
    """
    abs_path = str(path.resolve())
    for img in bpy.data.images:
        if img.filepath and str(Path(bpy.path.abspath(img.filepath)).resolve()) == abs_path:
            if img.colorspace_settings.name != colorspace:
                img.colorspace_settings.name = colorspace
            return img
    try:
        img = bpy.data.images.load(abs_path, check_existing=True)
    except RuntimeError as e:
        logger.warning("image load failed: %s — %s", abs_path, e)
        return None
    img.colorspace_settings.name = colorspace
    return img


def _ensure_principled(mat: bpy.types.Material) -> tuple[bpy.types.Node, bpy.types.Node]:
    """Reset ``mat`` to use nodes and return its Principled BSDF +
    Material Output. The function is idempotent — calling it twice on
    the same material returns the same node graph instead of stacking
    duplicates.
    """
    mat.use_nodes = True
    nt = mat.node_tree

    # Locate an existing Principled BSDF; if none, drop a fresh one.
    bsdf = None
    output = None
    for n in nt.nodes:
        if bsdf is None and n.type == "BSDF_PRINCIPLED":
            bsdf = n
        elif output is None and n.type == "OUTPUT_MATERIAL":
            output = n
    if bsdf is None:
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (0, 0)
    if output is None:
        output = nt.nodes.new("ShaderNodeOutputMaterial")
        output.location = (300, 0)
    # Wire BSDF -> Output if not already linked.
    if not any(
        link.from_node is bsdf and link.to_node is output
        for link in nt.links
    ):
        nt.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
    return bsdf, output


def _bsdf_input(bsdf: bpy.types.Node, *names: str):
    """Return the first matching BSDF input socket by display name, or
    None. Principled input names drifted across Blender versions (e.g.
    ``Emission`` -> ``Emission Color`` in 4.0), so callers pass the
    candidates newest-first."""
    for name in names:
        if name in bsdf.inputs:
            return bsdf.inputs[name]
    return None


def _find_node(mat: bpy.types.Material, name: str) -> bpy.types.Node | None:
    if not mat.use_nodes:
        return None
    for n in mat.node_tree.nodes:
        if n.name == name:
            return n
    return None


def _add_tex_node(
    mat: bpy.types.Material,
    image: bpy.types.Image,
    *,
    location: tuple[int, int],
    name: str,
) -> bpy.types.Node:
    """Add a fresh Image Texture node — caller is responsible for
    deduplicating by sidecar slot."""
    nt = mat.node_tree
    node = nt.nodes.new("ShaderNodeTexImage")
    node.image = image
    node.location = location
    node.label = name
    node.name = f"WoWS_{name}"
    return node


def _bind_basecolor(
    mat: bpy.types.Material,
    bsdf: bpy.types.Node,
    img: bpy.types.Image,
) -> None:
    nt = mat.node_tree
    node = _add_tex_node(mat, img, location=(-600, 200), name="baseColor")
    nt.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
    # Alpha is wired only for cutout/transparent intents — see
    # _apply_render_state, which links node.outputs["Alpha"] then.


def _bind_metallicroughness(
    mat: bpy.types.Material,
    bsdf: bpy.types.Node,
    img: bpy.types.Image,
) -> None:
    """Map metallicRoughness DDS to Principled inputs.

    WG's conformant ``_mr`` sibling follows the glTF convention: G =
    roughness, B = metallic. (R is unused here — NOT occlusion; the
    occlusion slot carries its own AO map.) Separate Color + per-channel
    route. The image is loaded Non-Color by the caller.
    """
    nt = mat.node_tree
    tex_node = _add_tex_node(mat, img, location=(-900, -100), name="metallicRoughness")
    sep = nt.nodes.new("ShaderNodeSeparateColor")
    sep.location = (-600, -100)
    sep.name = "WoWS_mr_separate"
    sep.mode = "RGB"
    nt.links.new(tex_node.outputs["Color"], sep.inputs["Color"])
    nt.links.new(sep.outputs["Green"], bsdf.inputs["Roughness"])
    nt.links.new(sep.outputs["Blue"], bsdf.inputs["Metallic"])


def _bind_normal(
    mat: bpy.types.Material,
    bsdf: bpy.types.Node,
    img: bpy.types.Image,
) -> None:
    nt = mat.node_tree
    tex_node = _add_tex_node(mat, img, location=(-900, -400), name="normal")
    nm = nt.nodes.new("ShaderNodeNormalMap")
    nm.location = (-600, -400)
    nm.name = "WoWS_normal_map"
    nt.links.new(tex_node.outputs["Color"], nm.inputs["Color"])
    nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])


def _bind_occlusion(
    mat: bpy.types.Material,
    bsdf: bpy.types.Node,
    img: bpy.types.Image,
) -> None:
    """Multiply AO into Base Color via a MixRGB node.

    Blender's Principled BSDF has no AO input (AO is a baking concern,
    not a runtime one for true PBR). We multiply into Base Color which
    matches how Unity's URP shader graph wires it.

    Works whether Base Color comes from a texture (splice the existing
    link) OR from a ``factors`` constant (seed Color1 with the BSDF's
    current Base Color; _apply_factors writes the real tint into the
    mix node afterward). This avoids silently dropping AO + orphaning
    the texture node on textureless materials.
    """
    nt = mat.node_tree
    tex_node = _add_tex_node(mat, img, location=(-900, 500), name="occlusion")

    bc_link = None
    for link in nt.links:
        if link.to_node is bsdf and link.to_socket.identifier == "Base Color":
            bc_link = link
            break

    mix = nt.nodes.new("ShaderNodeMixRGB")
    mix.blend_type = "MULTIPLY"
    mix.location = (-300, 200)
    mix.name = "WoWS_ao_multiply"
    mix.inputs["Fac"].default_value = 1.0

    if bc_link is not None:
        src_socket = bc_link.from_socket
        nt.links.remove(bc_link)
        nt.links.new(src_socket, mix.inputs["Color1"])
    else:
        # No baseColor texture: seed Color1 with the BSDF's current Base
        # Color constant. _apply_factors writes the authored tint here
        # when baseColor is unbound (it targets WoWS_ao_multiply.Color1).
        bc_in = _bsdf_input(bsdf, "Base Color")
        if bc_in is not None:
            mix.inputs["Color1"].default_value = tuple(bc_in.default_value)

    nt.links.new(tex_node.outputs["Color"], mix.inputs["Color2"])
    nt.links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])


def _bind_emissive(
    mat: bpy.types.Material,
    bsdf: bpy.types.Node,
    img: bpy.types.Image,
) -> None:
    """Bind the synthesized emissive map to Principled Emission Color.

    The producer's emission synth already baked the mg.B mask and the
    per-material emissive power into the texture (memory
    ``project_emissive_synth_two_bugs``), so the consumer binds the map
    straight to Emission Color. Emission Strength is set authoritatively
    in _apply_factors (a fresh Principled defaults Emission Strength to
    0.0 on Blender 5.x, which would render the bound map black).
    """
    nt = mat.node_tree
    node = _add_tex_node(mat, img, location=(-600, 500), name="emissive")
    color_in = _bsdf_input(bsdf, "Emission Color", "Emission")
    if color_in is not None:
        nt.links.new(node.outputs["Color"], color_in)


def _apply_factors(
    mat: bpy.types.Material,
    bsdf: bpy.types.Node,
    factors: dict,
    bound: set[str],
) -> None:
    """Drive BSDF scalar inputs from material ``factors``.

    Texture slots win where bound; factors fill the gaps. A fresh
    Principled is Metallic 0.0 / Roughness 0.5 — when MR is unbound we
    still prefer the authored factor (WG hull factors are typically
    roughness ~0.8 / metallic 0.0) for a closer matte-dielectric match.
    """
    factors = factors or {}

    if "metallicRoughness" not in bound:
        m = factors.get("metallic")
        r = factors.get("roughness")
        mi = _bsdf_input(bsdf, "Metallic")
        if mi is not None and m is not None:
            mi.default_value = float(m)
        ri = _bsdf_input(bsdf, "Roughness")
        if ri is not None and r is not None:
            ri.default_value = float(r)

    if "baseColor" not in bound:
        bc = factors.get("baseColor")
        if isinstance(bc, (list, tuple)) and len(bc) >= 3:
            val = (
                float(bc[0]), float(bc[1]), float(bc[2]),
                float(bc[3]) if len(bc) > 3 else 1.0,
            )
            # If occlusion spliced a multiply in front of Base Color, the
            # tint must land on the mix's Color1, not the (now-linked)
            # BSDF Base Color socket.
            ao = _find_node(mat, "WoWS_ao_multiply")
            if ao is not None:
                ao.inputs["Color1"].default_value = val
            else:
                bi = _bsdf_input(bsdf, "Base Color")
                if bi is not None:
                    bi.default_value = val

    # Emission strength — single source of truth (the bind step only
    # wires the color). A bound emissive map defaults to strength 1.0 so
    # it is visible (5.x fresh default is 0.0); an explicit factor wins.
    si = _bsdf_input(bsdf, "Emission Strength")
    strength = factors.get("emissive_strength")
    if "emissive" in bound:
        if si is not None:
            si.default_value = float(strength) if strength is not None else 1.0
    else:
        em = factors.get("emissive")
        if isinstance(em, (list, tuple)) and len(em) >= 3 and any(em[:3]):
            ci = _bsdf_input(bsdf, "Emission Color", "Emission")
            if ci is not None:
                ci.default_value = (float(em[0]), float(em[1]), float(em[2]), 1.0)
            if si is not None:
                si.default_value = float(strength) if strength is not None else 1.0


def _apply_render_state(
    mat: bpy.types.Material,
    bsdf: bpy.types.Node,
    entry: MaterialEntry,
    bound: set[str],
) -> None:
    """Set alpha mode + backface culling from shader_intent / double_sided.

    Without this, cutout meshes (railings, nets, AA gun shields) and
    transparent meshes (glass, periscopes) render as solid panels.

    Alpha is driven through the BSDF ``Alpha`` input (honored by Cycles
    AND EEVEE-Next). Hard cutout uses a graph-level GREATER_THAN node so
    it is renderer- and version-agnostic. The EEVEE surface method is
    set via ``surface_render_method`` (4.2+/5.x EEVEE-Next) or
    ``blend_method`` (4.0/4.1); Cycles ignores both.
    """
    intent = (entry.shader_intent or "").lower()
    rq = (entry.render_queue or "").lower()
    is_cutout = "cutout" in intent or "clip" in intent or rq == "cutout"
    is_transparent = "transparent" in intent or rq == "transparent"

    if (is_cutout or is_transparent) and "baseColor" in bound:
        bc_node = _find_node(mat, "WoWS_baseColor")
        alpha_in = _bsdf_input(bsdf, "Alpha")
        if bc_node is not None and alpha_in is not None:
            nt = mat.node_tree
            src = bc_node.outputs["Alpha"]
            if is_cutout:
                thr = nt.nodes.new("ShaderNodeMath")
                thr.operation = "GREATER_THAN"
                thr.inputs[1].default_value = 0.5
                thr.location = (-300, -250)
                thr.name = "WoWS_alpha_clip"
                nt.links.new(src, thr.inputs[0])
                src = thr.outputs["Value"]
            nt.links.new(src, alpha_in)

    if hasattr(mat, "surface_render_method"):
        # EEVEE-Next (Blender 4.2+, 5.x). DITHERED gives a clip-like
        # opaque pass that honors the (now binary) alpha for cutout;
        # BLENDED for true transparency.
        mat.surface_render_method = "BLENDED" if is_transparent else "DITHERED"
    elif hasattr(mat, "blend_method"):
        # EEVEE-Legacy (4.0/4.1).
        mat.blend_method = "BLEND" if is_transparent else ("CLIP" if is_cutout else "OPAQUE")
        if is_cutout and hasattr(mat, "alpha_threshold"):
            mat.alpha_threshold = 0.5

    # Blender culls backfaces when use_backface_culling is True; a
    # double-sided WG material wants both faces shown.
    mat.use_backface_culling = not bool(entry.double_sided)


def bind_material(
    mat: bpy.types.Material,
    entry: MaterialEntry,
    model_root: Path,
    *,
    scheme: str = DEFAULT_SCHEME,
) -> int:
    """Wire ``mat``'s node graph from the sidecar's ``texture_sets[scheme]``.

    Returns the number of surface slots successfully bound. Skipping
    slots that fail to resolve a PNG is intentional — Blender renders
    the material with whatever bound, rather than aborting the whole
    import.

    Idempotent: re-binding the same material wipes prior WoWS-tagged
    nodes (`name.startswith('WoWS_')`) before re-wiring. Manual user
    edits to non-WoWS nodes are preserved.
    """
    slots = entry.texture_sets.get(scheme) or entry.texture_sets.get(DEFAULT_SCHEME) or {}

    # Strip prior WoWS-tagged nodes so re-binding stays clean.
    if mat.use_nodes:
        nt = mat.node_tree
        for node in list(nt.nodes):
            if node.name.startswith("WoWS_"):
                nt.nodes.remove(node)

    bsdf, _ = _ensure_principled(mat)

    binders = {
        "baseColor":         (_bind_basecolor,         "sRGB"),
        "metallicRoughness": (_bind_metallicroughness, "Non-Color"),
        "normal":            (_bind_normal,            "Non-Color"),
        "occlusion":         (_bind_occlusion,         "Non-Color"),
        "emissive":          (_bind_emissive,          "sRGB"),
    }

    bound: set[str] = set()
    for slot in _PBR_BIND_ORDER:
        ref = slots.get(slot)
        if ref is None:
            continue
        png = _resolve_png_for_texture(model_root, ref)
        if png is None:
            logger.info("material %s slot %s: no PNG/DDS resolved", entry.material_id, slot)
            continue
        if slot == "occlusion" and any(s in png.stem.lower() for s in _AO_PLACEHOLDER_STEMS):
            # 16x16 mid-grey placeholder — binding it just dims uniformly.
            continue
        binder, colorspace = binders[slot]
        img = _load_image(png, colorspace=colorspace)
        if img is None:
            continue
        binder(mat, bsdf, img)
        bound.add(slot)

    if "detail" in slots:
        # Tangent-space detail-normal blend is deferred (Phase 1b); the
        # atlas + detail_params are present but not yet composited.
        logger.debug("material %s has a detail slot (blend deferred)", entry.material_id)

    _apply_factors(mat, bsdf, entry.factors, bound)
    _apply_render_state(mat, bsdf, entry, bound)

    # Stash sidecar metadata for downstream inspection.
    mat["wows_material_id"]   = entry.material_id
    mat["wows_scheme"]        = scheme
    mat["wows_shader_intent"] = entry.shader_intent
    return len(bound)


__all__ = ["DEFAULT_SCHEME", "bind_material"]
