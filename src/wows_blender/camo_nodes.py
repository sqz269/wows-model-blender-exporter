"""Camo overlay as Blender shader nodes (Path A palette + Path B swap).

:mod:`wows_blender.camo` decides *which* mask / palette / atlas drives a
material; this module builds the node graph that applies it. Split that
way so the resolution half stays pure-stdlib and unit-testable outside
Blender.

**Path B** (``mat_textures``) is a straight albedo swap — WG pre-baked
the camo into a per-category atlas, so we repoint the material's base
colour at it. Tried first: the engine prefers B wherever a part carries
both (memory ``project_camo_hybrid_path_ab``).

**Path A** (``color_scheme`` + ``categories[cat].mask``) is the 4-row
palette lerp, ported from the webview GLSL::

    baseRgb = albedo * (1 - mg.G)                        # mg.G = metallic
    Pi      = lerp(baseRgb, colors[i].rgb, colors[i].a)  # i = 0..3
    step1   = lerp(P0,    P1, mask.r)
    step2   = lerp(step1, P2, mask.g)
    step3   = lerp(step2, P3, mask.b)
    final   = lerp(baseRgb, step3, mg.B)                 # mg.B = gate

``mg.G`` is the metallic channel — in the producer's conformant ``_mr``
sibling that is the BLUE channel (glTF convention: G=roughness,
B=metallic), which is why we tap ``WoWS_mr_separate``'s Blue output and
not its Green. ``mg.B`` is the paint gate the producer extracts into the
``camoExclusionMask`` texture's RED channel.

Every node created here is named ``WoWS_camo_*`` so
:func:`~wows_blender.materials.bind_material`'s ``WoWS_``-prefixed
cleanup sweeps it on a re-bind — camo never accumulates across imports.
"""
from __future__ import annotations

import logging
from pathlib import Path

import bpy

from .camo import PathAResolved, resolve_path_b
from .materials import load_image, resolve_texture_png
from .sidecar import MaterialEntry, Skin

logger = logging.getLogger(__name__)

#: Left edge of the camo sub-graph. The PBR binder occupies x in
#: [-900, -300]; camo sits further left and higher so the two don't
#: overlap in the shader editor.
_X0 = -2100
_Y0 = 1100


def _find(mat: bpy.types.Material, name: str) -> bpy.types.Node | None:
    if not mat.use_nodes:
        return None
    return mat.node_tree.nodes.get(name)


def _principled(mat: bpy.types.Material) -> bpy.types.Node | None:
    if not mat.use_nodes:
        return None
    for n in mat.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            return n
    return None


def _new_mix(
    nt: bpy.types.NodeTree, *, location: tuple[int, int], name: str,
) -> tuple[bpy.types.Node, object, object, object, object]:
    """Create a colour-mix node, returning ``(node, fac, a, b, out)``.

    ``ShaderNodeMixRGB`` is legacy-but-present through Blender 5.x and has
    stable socket names; ``ShaderNodeMix`` is the modern replacement but
    exposes several same-named sockets (one per data type) that can only
    be told apart positionally. Prefer the legacy node and fall back so
    the add-on keeps working if it is ever removed.
    """
    try:
        node = nt.nodes.new("ShaderNodeMixRGB")
    except RuntimeError:
        node = nt.nodes.new("ShaderNodeMix")
        node.data_type = "RGBA"
        # ShaderNodeMix exposes one A/B pair per data type, all sharing
        # the same display names; pick the colour pair by socket type.
        colors_in = [s for s in node.inputs if s.type == "RGBA"]
        fac_in = next(s for s in node.inputs if s.type == "VALUE")
        color_out = next(s for s in node.outputs if s.type == "RGBA")
        node.location = location
        node.name = name
        return node, fac_in, colors_in[0], colors_in[1], color_out

    node.blend_type = "MIX"
    node.location = location
    node.name = name
    return (
        node,
        node.inputs["Fac"],
        node.inputs["Color1"],
        node.inputs["Color2"],
        node.outputs["Color"],
    )


def _albedo_sink(mat: bpy.types.Material, bsdf: bpy.types.Node):
    """The socket that should receive the final (camo'd) albedo.

    When the AO pass spliced a multiply in front of Base Color the camo
    must land on that node's ``Color1`` — writing straight to the BSDF
    would be overwritten by the AO link and silently drop the paint.
    """
    ao = _find(mat, "WoWS_ao_multiply")
    if ao is not None:
        return ao.inputs["Color1"]
    return bsdf.inputs["Base Color"]


def _albedo_source(mat: bpy.types.Material, bsdf: bpy.types.Node):
    """The socket currently producing the un-camo'd albedo, or None when
    the material is textureless (then the sink's constant is the albedo)."""
    bc = _find(mat, "WoWS_baseColor")
    if bc is not None:
        return bc.outputs["Color"]
    return None


def _uv_mapped_texture(
    mat: bpy.types.Material,
    image: bpy.types.Image,
    *,
    uv_scale: tuple[float, float],
    uv_offset: tuple[float, float],
    location: tuple[int, int],
    name: str,
) -> bpy.types.Node:
    """Image texture sampled at ``vMapUv * scale + offset``.

    The Mapping node is only inserted when the transform is non-identity —
    an identity Mapping is pure overhead and makes the graph harder for
    the FBX prep pass to read.
    """
    nt = mat.node_tree
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = image
    tex.location = location
    tex.name = name
    tex.label = name.replace("WoWS_camo_", "camo ")

    if uv_scale == (1.0, 1.0) and uv_offset == (0.0, 0.0):
        return tex

    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.location = (location[0] - 300, location[1])
    mapping.name = f"{name}_mapping"
    mapping.inputs["Scale"].default_value = (uv_scale[0], uv_scale[1], 1.0)
    mapping.inputs["Location"].default_value = (uv_offset[0], uv_offset[1], 0.0)

    texcoord = nt.nodes.new("ShaderNodeTexCoord")
    texcoord.location = (location[0] - 500, location[1])
    texcoord.name = f"{name}_texcoord"

    nt.links.new(texcoord.outputs["UV"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
    return tex


def apply_path_b(
    mat: bpy.types.Material,
    entry: MaterialEntry,
    skin: Skin,
    publish_root: Path,
    model_root: Path,
) -> bool:
    """Swap in the skin's pre-baked albedo atlas. Returns True if applied.

    Returns False when the skin carries no Path-B entry for this
    material's category, which is the caller's signal to try Path A.
    """
    resolved = resolve_path_b(entry, skin, publish_root, model_root)
    if resolved.albedo_png is None:
        return False

    bsdf = _principled(mat)
    if bsdf is None:
        return False

    img = load_image(resolved.albedo_png, colorspace="sRGB")
    if img is None:
        return False

    nt = mat.node_tree
    tex = _uv_mapped_texture(
        mat, img,
        uv_scale=resolved.uv_scale, uv_offset=resolved.uv_offset,
        location=(_X0 + 900, _Y0), name="WoWS_camo_matAlbedo",
    )

    sink = _albedo_sink(mat, bsdf)
    for link in list(nt.links):
        if link.to_socket == sink:
            nt.links.remove(link)
    nt.links.new(tex.outputs["Color"], sink)

    mat["wows_camo_path"] = "B"
    mat["wows_camo_category"] = resolved.category
    mat["wows_camo_skin"] = skin.skin_id
    return True


def apply_path_a(
    mat: bpy.types.Material,
    entry: MaterialEntry,
    resolved: PathAResolved,
    model_root: Path,
    *,
    scheme: str = "main",
) -> bool:
    """Build the 4-row palette lerp. Returns True if applied.

    Bails (returning False, leaving the base PBR intact) when the skin has
    no usable mask, when fewer than two palette rows were authored, or
    when the material carries no ``camoExclusionMask``. That last case is
    deliberate: ``mg.B`` is the engine's paint gate, and painting a
    material that never opted in would tint geometry WG leaves bare.
    """
    if resolved.mask_png is None:
        return False
    colors = resolved.colors
    if len(colors) < 2:
        return False

    bsdf = _principled(mat)
    if bsdf is None:
        return False

    # The paint gate. Producer extracts WG's mg.B into this texture's RED.
    gate_ref = (entry.texture_sets.get(scheme) or entry.texture_sets.get("main") or {}).get(
        "camoExclusionMask"
    )
    gate_png = resolve_texture_png(model_root, gate_ref) if gate_ref else None
    if gate_png is None:
        logger.info(
            "material %s: skin has a mask but the material has no "
            "camoExclusionMask; skipping Path A (ungated paint would tint "
            "geometry WG leaves bare)",
            entry.material_id,
        )
        return False

    mask_img = load_image(resolved.mask_png, colorspace="Non-Color")
    gate_img = load_image(gate_png, colorspace="Non-Color")
    if mask_img is None or gate_img is None:
        return False

    nt = mat.node_tree
    sink = _albedo_sink(mat, bsdf)
    src = _albedo_source(mat, bsdf)

    # ---- baseRgb = albedo * (1 - mg.G) --------------------------------
    # mg.G is metallic; in the conformant _mr sibling that is BLUE.
    mr_sep = _find(mat, "WoWS_mr_separate")
    base_socket = src
    if base_socket is None:
        # Textureless material: lift the sink's current constant into a
        # real RGB node so the mix chain has something to read.
        rgb = nt.nodes.new("ShaderNodeRGB")
        rgb.location = (_X0, _Y0 + 300)
        rgb.name = "WoWS_camo_baseConst"
        val = tuple(sink.default_value)
        rgb.outputs["Color"].default_value = (val[0], val[1], val[2], 1.0)
        base_socket = rgb.outputs["Color"]

    if mr_sep is not None:
        inv = nt.nodes.new("ShaderNodeMath")
        inv.operation = "SUBTRACT"
        inv.location = (_X0 + 300, _Y0 + 500)
        inv.name = "WoWS_camo_oneMinusMetal"
        inv.inputs[0].default_value = 1.0
        nt.links.new(mr_sep.outputs["Blue"], inv.inputs[1])

        demetal, d_fac, d_a, d_b, d_out = _new_mix(
            nt, location=(_X0 + 550, _Y0 + 300), name="WoWS_camo_demetal",
        )
        demetal.blend_type = "MULTIPLY"
        d_fac.default_value = 1.0
        nt.links.new(base_socket, d_a)
        # d_b is a colour socket driven by a scalar — Blender broadcasts
        # the float across RGB, which is exactly the (1 - mg.G) scale.
        nt.links.new(inv.outputs["Value"], d_b)
        base_socket = d_out

    # ---- palette rows: Pi = lerp(baseRgb, colors[i].rgb, colors[i].a) --
    rows = []
    for i, c in enumerate(colors[:4]):
        node, fac, a_in, b_in, out = _new_mix(
            nt, location=(_X0 + 900, _Y0 - i * 220), name=f"WoWS_camo_row{i}",
        )
        fac.default_value = float(c[3])
        nt.links.new(base_socket, a_in)
        b_in.default_value = (float(c[0]), float(c[1]), float(c[2]), 1.0)
        rows.append(out)

    # A skin may author fewer than four rows; repeat the last so the
    # mask.r/g/b chain below always has three steps to walk.
    while len(rows) < 4:
        rows.append(rows[-1])

    # ---- mask channel split -------------------------------------------
    mask_tex = _uv_mapped_texture(
        mat, mask_img,
        uv_scale=resolved.uv_scale, uv_offset=resolved.uv_offset,
        location=(_X0 + 900, _Y0 - 1000), name="WoWS_camo_mask",
    )
    mask_sep = nt.nodes.new("ShaderNodeSeparateColor")
    mask_sep.mode = "RGB"
    mask_sep.location = (_X0 + 1200, _Y0 - 1000)
    mask_sep.name = "WoWS_camo_mask_separate"
    nt.links.new(mask_tex.outputs["Color"], mask_sep.inputs["Color"])

    # ---- step chain ----------------------------------------------------
    step = rows[0]
    for i, ch in enumerate(("Red", "Green", "Blue")):
        node, fac, a_in, b_in, out = _new_mix(
            nt, location=(_X0 + 1450 + i * 260, _Y0 - 200), name=f"WoWS_camo_step{i + 1}",
        )
        nt.links.new(step, a_in)
        nt.links.new(rows[i + 1], b_in)
        nt.links.new(mask_sep.outputs[ch], fac)
        step = out

    # ---- final = lerp(baseRgb, step3, gate) -----------------------------
    gate_tex = _uv_mapped_texture(
        mat, gate_img,
        uv_scale=(1.0, 1.0), uv_offset=(0.0, 0.0),
        location=(_X0 + 1450, _Y0 - 1300), name="WoWS_camo_gate",
    )
    gate_sep = nt.nodes.new("ShaderNodeSeparateColor")
    gate_sep.mode = "RGB"
    gate_sep.location = (_X0 + 1750, _Y0 - 1300)
    gate_sep.name = "WoWS_camo_gate_separate"
    nt.links.new(gate_tex.outputs["Color"], gate_sep.inputs["Color"])

    final, f_fac, f_a, f_b, f_out = _new_mix(
        nt, location=(_X0 + 2250, _Y0 - 400), name="WoWS_camo_final",
    )
    nt.links.new(base_socket, f_a)
    nt.links.new(step, f_b)
    nt.links.new(gate_sep.outputs["Red"], f_fac)

    for link in list(nt.links):
        if link.to_socket == sink:
            nt.links.remove(link)
    nt.links.new(f_out, sink)

    mat["wows_camo_path"] = "A"
    mat["wows_camo_category"] = resolved.category
    mat["wows_camo_source"] = resolved.source
    return True


__all__ = ["apply_path_a", "apply_path_b"]
