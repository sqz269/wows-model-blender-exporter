"""Sidecar-driven material binding.

For each Blender material that matches a sidecar ``material_id``, build
a Principled-BSDF node graph wired to PNG sibling textures of the
publisher's DDS files. WG slot → glTF slot map:

    sidecar slot          glTF role            Principled input
    -----------------     ------------------   ------------------------
    baseColor             baseColorTexture     Base Color
    metallicRoughness     metallicRoughness    Metallic / Roughness
                                                (B / G channels)
    normal                normalTexture        Normal Map (tangent)
    occlusion             occlusionTexture     Mix into Base Color via
                                                Multiply node (or skip)
    camoMask              custom               Stored as a custom property;
                                                used by future camo work,
                                                not rendered today.

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


def _resolve_png_for_texture(model_root: Path, ref: TextureRef) -> Path | None:
    """Resolve a TextureRef to a PNG file on disk.

    ``ref.dds_mips`` is a path list relative to the SHIP'S model
    directory (for hull materials) or the LIBRARY ASSET's directory
    (for accessory materials). The caller passes ``model_root`` to
    match.

    The publisher's DDS→PNG pass writes PNGs next to the DDS files
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
    first and reuse.
    """
    abs_path = str(path.resolve())
    for img in bpy.data.images:
        if img.filepath and str(Path(bpy.path.abspath(img.filepath)).resolve()) == abs_path:
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
    # Alpha — leave unlinked unless the material is double-sided
    # alpha-test. WG ship materials with alpha use the ``alpha_clip``
    # shader intent, but the add-on does not currently inspect that;
    # users can wire alpha manually for those rare materials.


def _bind_metallicroughness(
    mat: bpy.types.Material,
    bsdf: bpy.types.Node,
    img: bpy.types.Image,
) -> None:
    """Map metallicRoughness DDS to Principled inputs.

    WG's MR DDS follows the glTF convention: G channel = roughness,
    B channel = metallic. (R is AO and ignored here — the occlusion
    slot has its own AO texture.) We add a Separate Color node and
    route per-channel.
    """
    nt = mat.node_tree
    tex_node = _add_tex_node(mat, img, location=(-900, -100), name="metallicRoughness")
    tex_node.image.colorspace_settings.name = "Non-Color"
    sep = nt.nodes.new("ShaderNodeSeparateColor")
    sep.location = (-600, -100)
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
    tex_node.image.colorspace_settings.name = "Non-Color"
    nm = nt.nodes.new("ShaderNodeNormalMap")
    nm.location = (-600, -400)
    nt.links.new(tex_node.outputs["Color"], nm.inputs["Color"])
    nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])


def _bind_occlusion(
    mat: bpy.types.Material,
    bsdf: bpy.types.Node,
    img: bpy.types.Image,
) -> None:
    """Optional — multiply AO into Base Color via a MixRGB node.

    Blender's Principled BSDF has no AO input (AO is a baking concern,
    not a runtime one for true PBR). We multiply into Base Color which
    matches how Unity's URP shader graph wires it.
    """
    nt = mat.node_tree
    tex_node = _add_tex_node(mat, img, location=(-900, 500), name="occlusion")
    tex_node.image.colorspace_settings.name = "Non-Color"
    # The basecolor->BSDF link may already exist; if so, splice the
    # AO multiply in between.
    bc_link = None
    for link in nt.links:
        if link.to_node is bsdf and link.to_socket.identifier == "Base Color":
            bc_link = link
            break
    if bc_link is None:
        # No baseColor texture: nothing meaningful to multiply.
        return
    src_node = bc_link.from_node
    src_socket = bc_link.from_socket
    mix = nt.nodes.new("ShaderNodeMixRGB")
    mix.blend_type = "MULTIPLY"
    mix.location = (-300, 200)
    mix.inputs["Fac"].default_value = 1.0
    nt.links.remove(bc_link)
    nt.links.new(src_socket, mix.inputs["Color1"])
    nt.links.new(tex_node.outputs["Color"], mix.inputs["Color2"])
    nt.links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])


def bind_material(
    mat: bpy.types.Material,
    entry: MaterialEntry,
    model_root: Path,
    *,
    scheme: str = DEFAULT_SCHEME,
) -> int:
    """Wire ``mat``'s node graph from the sidecar's ``texture_sets[scheme]``.

    Returns the number of slots successfully bound. Skipping slots that
    fail to resolve a PNG is intentional — Blender renders the material
    with whatever bound, rather than aborting the whole import.

    Idempotent: re-binding the same material wipes prior WoWS-tagged
    nodes (`name.startswith('WoWS_')`) before re-wiring. Manual
    user edits to non-WoWS nodes are preserved.
    """
    slots = entry.texture_sets.get(scheme) or entry.texture_sets.get(DEFAULT_SCHEME) or {}
    if not slots:
        return 0

    # Strip prior WoWS-tagged nodes so re-binding stays clean.
    if mat.use_nodes:
        nt = mat.node_tree
        for node in list(nt.nodes):
            if node.name.startswith("WoWS_"):
                nt.nodes.remove(node)

    bsdf, _ = _ensure_principled(mat)

    bound = 0
    # Order matters: baseColor first so occlusion can splice into its link.
    bind_order = ("baseColor", "metallicRoughness", "normal", "occlusion")
    binders = {
        "baseColor":         (_bind_basecolor,        "sRGB"),
        "metallicRoughness": (_bind_metallicroughness, "Non-Color"),
        "normal":            (_bind_normal,            "Non-Color"),
        "occlusion":         (_bind_occlusion,         "Non-Color"),
    }
    for slot in bind_order:
        ref = slots.get(slot)
        if ref is None:
            continue
        png = _resolve_png_for_texture(model_root, ref)
        if png is None:
            logger.info("material %s slot %s: no PNG/DDS resolved", entry.material_id, slot)
            continue
        binder, colorspace = binders[slot]
        img = _load_image(png, colorspace=colorspace)
        if img is None:
            continue
        binder(mat, bsdf, img)
        bound += 1

    # Stash sidecar metadata for downstream inspection.
    mat["wows_material_id"]  = entry.material_id
    mat["wows_scheme"]       = scheme
    mat["wows_shader_intent"] = entry.shader_intent
    return bound


__all__ = ["DEFAULT_SCHEME", "bind_material"]
