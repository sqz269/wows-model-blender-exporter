"""Make a WoWS material graph legible to Blender's FBX exporter.

``io_scene_fbx`` does not read arbitrary node graphs. It reads materials
through :class:`bpy_extras.node_shader_utils.PrincipledBSDFWrapper`,
whose ``ShaderImageTextureWrapper.node_image`` follows exactly one link::

    node_image = socket.links[0].from_node
    if node_image.bl_idname == 'ShaderNodeTexImage': ...

— i.e. the image node must sit **directly** on the Principled socket
(the sole exception being Normal, which is read through the intervening
``ShaderNodeNormalMap``). Anything else yields ``None`` and the texture
is silently dropped from the FBX.

The binder in :mod:`wows_blender.materials` deliberately does *not*
satisfy that: it splices a MixRGB in front of Base Color for AO, routes
metallic/roughness through a SeparateColor (the producer packs both into
one ``_mr`` image), and puts a GREATER_THAN in front of Alpha for hard
cutout. Faithful for rendering, invisible to FBX. So an FBX export
without this pass loses base colour on every AO-bound material and
loses metallic + roughness on every material, everywhere.

:func:`prep_material_for_fbx` rewires each material to the shape the
wrapper can read, and :func:`write_material_manifest` writes the ground
truth the FBX format cannot carry (channel packing, the AO map, camo
provenance) to a JSON sidecar next to the .fbx.

The rewrite is destructive. The headless driver runs it in a throwaway
process; the interactive operator marks itself ``UNDO`` so Ctrl+Z
restores the render-accurate graph.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import bpy

logger = logging.getLogger(__name__)

#: Candidate albedo image nodes, MOST authoritative first.
#:
#: Order is load-bearing. A bake flattens the camo composite and the AO
#: multiply into one image and renames its node ``WoWS_baseColor_baked``;
#: if the raw ``WoWS_baseColor`` were preferred, the prep pass would
#: rewire Base Color straight back to the unpainted albedo and silently
#: throw the bake away. Path-B's atlas likewise replaces the original
#: albedo wholesale, so it outranks it too.
_ALBEDO_NODES = ("WoWS_baseColor_baked", "WoWS_camo_matAlbedo", "WoWS_baseColor")


@dataclass
class PrepCounts:
    """What the prep pass changed, for the CLI summary."""

    materials:   int = 0
    base_color:  int = 0
    mr_split:    int = 0
    alpha:       int = 0
    baked:       int = 0
    notes:       list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [
            f"{self.materials} materials",
            f"baseColor {self.base_color}",
            f"metal/rough {self.mr_split}",
            f"alpha {self.alpha}",
        ]
        if self.baked:
            bits.append(f"baked {self.baked}")
        return "  ".join(bits)


def _node(mat: bpy.types.Material, name: str) -> bpy.types.Node | None:
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


def _clear_links_to(nt: bpy.types.NodeTree, socket) -> None:
    for link in list(nt.links):
        if link.to_socket == socket:
            nt.links.remove(link)


def _socket(bsdf: bpy.types.Node, *names: str):
    for n in names:
        if n in bsdf.inputs:
            return bsdf.inputs[n]
    return None


def _albedo_node(mat: bpy.types.Material) -> bpy.types.Node | None:
    """The image node holding this material's albedo.

    Path-B camo replaces the albedo wholesale, so its atlas wins over the
    original baseColor map when both are present.
    """
    for name in _ALBEDO_NODES:
        n = _node(mat, name)
        if n is not None and getattr(n, "image", None) is not None:
            return n
    return None


def prep_material_for_fbx(mat: bpy.types.Material, counts: PrepCounts) -> None:
    """Rewire one material into the shape ``PrincipledBSDFWrapper`` reads.

    Idempotent — re-running on an already-prepped material is a no-op
    beyond re-asserting the same links.
    """
    bsdf = _principled(mat)
    if bsdf is None:
        return
    nt = mat.node_tree
    counts.materials += 1

    # ---- Base Color: bypass the AO multiply / camo composite ----------
    albedo = _albedo_node(mat)
    bc_socket = _socket(bsdf, "Base Color")
    if albedo is not None and bc_socket is not None:
        current = bc_socket.links[0].from_node if bc_socket.links else None
        if current is not albedo:
            _clear_links_to(nt, bc_socket)
            nt.links.new(albedo.outputs["Color"], bc_socket)
            counts.base_color += 1

    # ---- Metallic + Roughness: bypass the SeparateColor ---------------
    # Both channels live in one packed `_mr` image (glTF convention:
    # G=roughness, B=metallic). FBX has no channel-packing concept, so
    # the same image lands on both sockets and the manifest records
    # which channel a consumer should actually read.
    mr_tex = _node(mat, "WoWS_metallicRoughness")
    if mr_tex is not None and getattr(mr_tex, "image", None) is not None:
        touched = False
        for name in ("Roughness", "Metallic"):
            sock = _socket(bsdf, name)
            if sock is None:
                continue
            current = sock.links[0].from_node if sock.links else None
            if current is not mr_tex:
                _clear_links_to(nt, sock)
                nt.links.new(mr_tex.outputs["Color"], sock)
                touched = True
        if touched:
            counts.mr_split += 1

    # ---- Alpha: bypass the cutout GREATER_THAN ------------------------
    # The wrapper needs the image node itself on the Alpha socket; the
    # threshold node in front makes it invisible. Cutout intent is
    # preserved in the manifest instead.
    alpha_socket = _socket(bsdf, "Alpha")
    if alpha_socket is not None and alpha_socket.links:
        from_node = alpha_socket.links[0].from_node
        if from_node.bl_idname != "ShaderNodeTexImage" and albedo is not None:
            _clear_links_to(nt, alpha_socket)
            nt.links.new(albedo.outputs["Alpha"], alpha_socket)
            counts.alpha += 1


def scene_materials() -> list[bpy.types.Material]:
    """Materials actually reachable from objects in the scene.

    ``bpy.data.materials`` is the wrong set: pruning the Armor / Hitboxes
    and LOD meshes leaves their materials behind as orphans until the
    file is purged, so iterating the datablock list reports (and preps)
    ~40 materials for an export that contains 4.
    """
    seen: set[str] = set()
    out: list[bpy.types.Material] = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        for mat in obj.data.materials:
            if mat is None or mat.name in seen:
                continue
            seen.add(mat.name)
            out.append(mat)
    return out


def prep_all_materials(counts: PrepCounts | None = None) -> PrepCounts:
    """Run :func:`prep_material_for_fbx` over every material being exported."""
    counts = counts or PrepCounts()
    mats = scene_materials()
    for mat in mats:
        prep_material_for_fbx(mat, counts)

    # Portability guard. The binder falls back to handing Blender the raw
    # DDS when no PNG sibling exists, which is fine inside Blender and
    # useless outside it — Maya, 3ds Max and Unreal will not read a DDS
    # referenced from an FBX. That happens when the publisher's DDS->PNG
    # pass has not run over the library the textures came from.
    dds = {
        Path(bpy.path.abspath(img.filepath)).name
        for mat in mats
        if mat.use_nodes
        for node in mat.node_tree.nodes
        if node.bl_idname == "ShaderNodeTexImage"
        and (img := getattr(node, "image", None)) is not None
        and img.filepath
        and Path(bpy.path.abspath(img.filepath)).suffix.lower().startswith(".dd")
    }
    if dds:
        counts.notes.append(
            f"{len(dds)} texture(s) are still DDS — most DCCs cannot read a "
            f"DDS referenced from an FBX. Publish with `wows-export-blender "
            f"--accessories` (its DDS->PNG pass) before exporting, or pass "
            f"--embed-textures if your target does read DDS."
        )
    return counts


# ---------------------------------------------------------------------------
# Bake — the only faithful way to carry camo / AO through FBX
# ---------------------------------------------------------------------------


def _mesh_objects(root: bpy.types.Object | None) -> list[bpy.types.Object]:
    if root is None:
        return [o for o in bpy.context.scene.objects if o.type == "MESH"]
    return [o for o in root.children_recursive if o.type == "MESH"]


def bake_base_color(
    root: bpy.types.Object | None,
    out_dir: Path,
    *,
    size: int = 2048,
    counts: PrepCounts | None = None,
) -> PrepCounts:
    """Bake each material's evaluated Base Color into its own PNG.

    This is what makes camo survive the trip: Path A is a per-pixel
    palette lerp against a mask, which no FBX material slot can express.
    Baking flattens it into a plain albedo map that any DCC will show
    correctly.

    AO is deliberately EXCLUDED from the bake: folding occlusion into
    albedo double-counts against the consumer's own ambient occlusion
    (KK runs SSAO) and dims surfaces under direct light. Every
    ``WoWS_ao_multiply`` is Fac=0'd for the bake (a MULTIPLY mix at
    Fac 0 passes Color1 — the albedo/camo chain — straight through)
    and restored afterwards.

    Runs the Cycles ``DIFFUSE`` pass with direct and indirect light
    disabled, which evaluates the Base Color input only: no lighting,
    no samples needed.
    """
    counts = counts or PrepCounts()
    meshes = _mesh_objects(root)
    if not meshes:
        counts.notes.append("bake skipped: no mesh objects in the scene")
        return counts

    out_dir.mkdir(parents=True, exist_ok=True)

    scene = bpy.context.scene
    prev_engine = scene.render.engine
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    scene.render.bake.use_pass_direct = False
    scene.render.bake.use_pass_indirect = False
    scene.render.bake.use_pass_color = True
    # Flood-fill the space between UV islands instead of leaving it
    # black: materials that use a thin slice of a shared atlas (wire
    # strips, gun subparts) rasterize ~1% of their bake target, and
    # black filler poisons every mip level — the part renders near-black
    # at any distance (caught on Azur Baltimore: 12/85 bakes < 5% mean
    # luminance). EXTEND pushes island edge colors outward; a margin
    # larger than any target floods completely.
    scene.render.bake.margin = 4096
    scene.render.bake.margin_type = "EXTEND"
    # Don't clear between objects — a material shared by several combined
    # meshes must accumulate all their islands into one target.
    scene.render.bake.use_clear = False

    # One bake target per material, made the active node so Cycles writes
    # into it. Materials with no UV-mapped mesh are skipped by Blender.
    # Each target is sized to the material's SOURCE albedo (capped at
    # ``size``): a 256² fitting must not become a 2048² bake — measured
    # on Azur Baltimore, uniform 2048² bakes ballooned the Unity bundle
    # from ~70 MB to 516 MB. The camo overlay is low-frequency, so
    # clamping to the part's native texel density loses nothing visible.
    targets: dict[str, tuple[bpy.types.Material, bpy.types.Image, bpy.types.Node]] = {}
    for mat in {m for o in meshes for m in o.data.materials if m is not None}:
        if not mat.use_nodes:
            continue
        # Cutout / transparent materials keep their SOURCE texture: the
        # diffuse bake writes UV coverage into alpha, not the texture's
        # own alpha, so a baked net renders as a solid panel (or, with
        # alpha-test, disappears entirely). Their camo contribution is
        # negligible; alpha fidelity is not.
        intent = str(mat.get("wows_shader_intent") or "").lower()
        if "cutout" in intent or "transparent" in intent:
            continue
        # SELECTIVE BAKE: only materials carrying a camo composite need
        # flattening — their paint is a node graph no FBX slot can
        # express. Everything else (bespoke exterior hulls, plain parts)
        # already has its final albedo as ONE image; baking would merely
        # resample it, and on segment-heavy wreck scenes the 4096 hull
        # bake silently came back mostly unbaked (margin-flood flats) —
        # binding the source image sidesteps that entire failure mode
        # and keeps intact/wreck exports pixel-identical by construction.
        if not mat.get("wows_camo_path"):
            continue
        mat_size = _bake_size_for(mat, size)
        img = bpy.data.images.new(
            f"{mat.name}_baked", width=mat_size, height=mat_size, alpha=True,
        )
        node = mat.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = img
        node.name = "WoWS_fbx_bake_target"
        node.location = (600, 400)
        node.select = True
        mat.node_tree.nodes.active = node
        targets[mat.name] = (mat, img, node)

    if not targets:
        scene.render.engine = prev_engine
        counts.notes.append("bake skipped: no node-based materials")
        return counts

    # AO stays OUT of the bake (see docstring): Fac=0 on the multiply
    # passes the albedo/camo chain through untouched; restored after.
    ao_disabled: list[bpy.types.Node] = []
    for mat, _img, _node in targets.values():
        ao = mat.node_tree.nodes.get("WoWS_ao_multiply")
        if ao is not None and ao.inputs["Fac"].default_value > 0.0:
            ao.inputs["Fac"].default_value = 0.0
            ao_disabled.append(ao)

    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    baked_objects = 0
    for obj in meshes:
        if not obj.data.uv_layers:
            continue
        obj.select_set(True)
        baked_objects += 1
    if baked_objects == 0:
        scene.render.engine = prev_engine
        counts.notes.append("bake skipped: no mesh carries a UV layer")
        return counts
    bpy.context.view_layer.objects.active = next(
        o for o in meshes if o.data.uv_layers
    )

    try:
        bpy.ops.object.bake(type="DIFFUSE")
    except RuntimeError as e:
        scene.render.engine = prev_engine
        counts.notes.append(f"bake failed: {e}")
        logger.warning("bake failed: %s", e)
        return counts
    finally:
        for ao in ao_disabled:
            ao.inputs["Fac"].default_value = 1.0

    # Save each baked image and repoint Base Color at it. The bake target
    # node becomes the albedo source, which the prep pass above then sees
    # as an already-direct TexImage.
    for name, (mat, img, node) in targets.items():
        path = out_dir / f"{_safe_name(name)}_baked.png"
        img.filepath_raw = str(path)
        img.file_format = "PNG"
        try:
            img.save()
        except RuntimeError as e:
            counts.notes.append(f"bake save failed for {name}: {e}")
            continue
        bsdf = _principled(mat)
        bc = _socket(bsdf, "Base Color") if bsdf else None
        if bc is not None:
            _clear_links_to(mat.node_tree, bc)
            mat.node_tree.links.new(node.outputs["Color"], bc)
        node.name = "WoWS_baseColor_baked"
        mat["wows_fbx_baked"] = True
        counts.baked += 1

    scene.render.engine = prev_engine
    return counts


def bake_camo_mgn_mr(
    out_dir: Path,
    *,
    size: int = 2048,
    counts: PrepCounts | None = None,
) -> PrepCounts:
    """EMIT-bake the camo-MGN-blended metallic/roughness for skins that
    override the surface response (``mgn_influence`` metal/gloss > 0).

    The Path B camo nodes fold ``camoMGN.rg`` into the Metallic /
    Roughness chains, but FBX carries one packed ``_mr`` image per
    material — without this bake the prep pass rewires MR straight back
    to the base image and a polished-metal skin (Ranked gold/silver/
    bronze) exports matte. Bakes the final BSDF Metallic/Roughness
    inputs into a glTF-layout image (G=roughness, B=metallic) and swaps
    it onto the material's ``WoWS_metallicRoughness`` node IN PLACE, so
    prep, the manifest and the uMG repack consume it unchanged.

    Must run AFTER :func:`bake_base_color` (shares its scene bake
    settings) and BEFORE :func:`prep_all_materials`.
    """
    counts = counts or PrepCounts()
    targets: dict[str, tuple[bpy.types.Material, bpy.types.Image, bpy.types.Node]] = {}
    mats = [
        m for m in scene_materials()
        if m.use_nodes
        and (inf := m.get("wows_camo_mgn_influence")) is not None
        and (float(inf[0]) > 0.0 or float(inf[1]) > 0.0)
    ]
    if not mats:
        return counts
    out_dir.mkdir(parents=True, exist_ok=True)

    scene = bpy.context.scene
    prev_engine = scene.render.engine
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    scene.render.bake.margin = 4096
    scene.render.bake.margin_type = "EXTEND"
    scene.render.bake.use_clear = False

    # Rig every target material for the EMIT pass: pack the FINAL
    # Metallic/Roughness inputs (camo mixes included) into an emission
    # colour, temporarily routed to the Material Output.
    restore: list[tuple[bpy.types.Material, list[bpy.types.Node], object]] = []
    for mat in mats:
        bsdf = _principled(mat)
        nt = mat.node_tree
        out_node = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
        if bsdf is None or out_node is None:
            continue
        surf = out_node.inputs["Surface"]
        prev_src = surf.links[0].from_socket if surf.links else None

        combine = nt.nodes.new("ShaderNodeCombineColor")
        combine.name = "WoWS_mgn_mr_pack"
        combine.location = (900, -1900)
        combine.inputs["Red"].default_value = 0.0
        for channel, sock_name in (("Green", "Roughness"), ("Blue", "Metallic")):
            sock = _socket(bsdf, sock_name)
            if sock is None:
                continue
            if sock.links:
                nt.links.new(sock.links[0].from_socket, combine.inputs[channel])
            else:
                combine.inputs[channel].default_value = float(sock.default_value)
        emit = nt.nodes.new("ShaderNodeEmission")
        emit.name = "WoWS_mgn_mr_emit"
        emit.location = (1150, -1900)
        nt.links.new(combine.outputs["Color"], emit.inputs["Color"])
        for link in list(nt.links):
            if link.to_socket == surf:
                nt.links.remove(link)
        nt.links.new(emit.outputs["Emission"], surf)

        mr_size = _bake_size_for(mat, size)
        img = bpy.data.images.new(
            f"{mat.name}_mrbaked", width=mr_size, height=mr_size, alpha=False,
        )
        img.colorspace_settings.name = "Non-Color"
        node = nt.nodes.new("ShaderNodeTexImage")
        node.image = img
        node.name = "WoWS_fbx_mrbake_target"
        node.location = (1150, -2150)
        node.select = True
        nt.nodes.active = node
        targets[mat.name] = (mat, img, node)
        restore.append((mat, [combine, emit, node], prev_src))

    if not targets:
        scene.render.engine = prev_engine
        return counts

    target_names = set(targets)
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    picked = 0
    for obj in _mesh_objects(None):
        if not obj.data.uv_layers:
            continue
        mats_of = [s.material.name for s in obj.material_slots if s.material]
        if mats_of and all(n in target_names for n in mats_of):
            obj.select_set(True)
            picked += 1
    ok = picked > 0
    if ok:
        bpy.context.view_layer.objects.active = next(
            o for o in bpy.context.selected_objects
        )
        try:
            bpy.ops.object.bake(type="EMIT")
        except RuntimeError as e:
            counts.notes.append(f"mgn mr bake failed: {e}")
            logger.warning("mgn mr bake failed: %s", e)
            ok = False

    for mat, scaffolding, prev_src in restore:
        nt = mat.node_tree
        out_node = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
        if out_node is not None:
            surf = out_node.inputs["Surface"]
            for link in list(nt.links):
                if link.to_socket == surf:
                    nt.links.remove(link)
            if prev_src is not None:
                nt.links.new(prev_src, surf)
        if ok and mat.name in targets:
            _mat, img, _node = targets[mat.name]
            path = out_dir / f"{_safe_name(mat.name)}_mrbaked.png"
            img.filepath_raw = str(path)
            img.file_format = "PNG"
            try:
                img.save()
            except RuntimeError as e:
                counts.notes.append(f"mgn mr bake save failed for {mat.name}: {e}")
            else:
                # Swap the baked MR onto the material's existing packed-MR
                # node IN PLACE — prep/manifest/repack read it unchanged.
                mr_node = _node_or_new_teximage(mat, "WoWS_metallicRoughness")
                mr_node.image = img
                mat["wows_mgn_mr_baked"] = True
                counts.notes.append(f"mgn mr baked: {mat.name}")
        for n in scaffolding:
            try:
                nt.nodes.remove(n)
            except (ReferenceError, RuntimeError):
                pass

    scene.render.engine = prev_engine
    return counts


def _node_or_new_teximage(mat: bpy.types.Material, name: str) -> bpy.types.Node:
    node = mat.node_tree.nodes.get(name)
    if node is None or node.bl_idname != "ShaderNodeTexImage":
        node = mat.node_tree.nodes.new("ShaderNodeTexImage")
        node.name = name
        node.location = (-600, -400)
    return node


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def _bake_size_for(mat: bpy.types.Material, cap: int) -> int:
    """Bake resolution for one material: the source albedo's larger
    dimension rounded up to a power of two, capped at ``cap``, floored
    at 256 (materials with no albedo image bake at min(cap, 1024))."""
    src = 0
    for name in ("WoWS_baseColor", "WoWS_camo_matAlbedo"):
        node = mat.node_tree.nodes.get(name)
        img = getattr(node, "image", None) if node else None
        if img is not None and img.size[0] > 0:
            src = max(img.size[0], img.size[1])
            break
    if src <= 0:
        return min(cap, 1024)
    p = 256
    while p < src:
        p *= 2
    return min(cap, p)


# ---------------------------------------------------------------------------
# Manifest — everything FBX cannot express
# ---------------------------------------------------------------------------


_MANIFEST_SLOTS = (
    ("WoWS_baseColor",         "baseColor",         "RGB=albedo, A=opacity"),
    ("WoWS_camo_matAlbedo",    "camoAlbedo",        "RGB=pre-baked camo albedo (Path B)"),
    ("WoWS_metallicRoughness", "metallicRoughness", "G=roughness, B=metallic (glTF packing)"),
    ("WoWS_normal",            "normal",            "tangent-space normal"),
    ("WoWS_occlusion",         "occlusion",         "R=ambient occlusion"),
    ("WoWS_emissive",          "emissive",          "RGB=emissive colour"),
    ("WoWS_camo_mask",         "camoMask",          "RGB=Path A palette-row weights"),
    ("WoWS_camo_gate",         "camoExclusionMask", "R=paint gate (WG mg.B)"),
    ("WoWS_baseColor_baked",   "bakedBaseColor",    "RGB=flattened albedo (camo burned in; AO excluded)"),
)


def write_material_manifest(path: Path, *, fbx_name: str, axis_up: str, axis_forward: str) -> int:
    """Write the slot map FBX cannot carry. Returns the material count.

    FBX materials are Phong: the exporter maps roughness to ``Shininess``
    and metallic to ``ReflectionFactor``, both of which are scalar
    concepts that lose the producer's channel packing, and it has no slot
    at all for AO or the camo masks. A consumer that wants the real PBR
    binding reads this file and ignores the FBX materials.
    """
    materials = []
    for mat in scene_materials():
        if not mat.use_nodes:
            continue
        slots = {}
        for node_name, slot_name, semantics in _MANIFEST_SLOTS:
            node = _node(mat, node_name)
            img = getattr(node, "image", None) if node else None
            if img is None:
                continue
            fp = img.filepath_from_user() if hasattr(img, "filepath_from_user") else img.filepath
            slots[slot_name] = {
                "texture":   Path(bpy.path.abspath(fp)).name if fp else img.name,
                "semantics": semantics,
                "colorspace": img.colorspace_settings.name,
            }
        entry = {
            "blender_material": mat.name,
            "material_id":      mat.get("wows_material_id", ""),
            "shader_intent":    mat.get("wows_shader_intent", ""),
            "scheme":           mat.get("wows_scheme", ""),
            "double_sided":     not mat.use_backface_culling,
            "slots":            slots,
        }
        for key in (
            "wows_camo_path", "wows_camo_category", "wows_camo_skin",
            "wows_fbx_baked", "wows_roughness", "wows_metallic",
            "wows_emissive_strength",
        ):
            if key in mat:
                entry[key.removeprefix("wows_")] = mat[key]
        materials.append(entry)

    doc = {
        "format":     "wows-fbx-material-manifest",
        "version":    1,
        "fbx":        fbx_name,
        "axis_up":    axis_up,
        "axis_forward": axis_forward,
        "note":
            "FBX materials are Phong and cannot express the producer's "
            "channel packing, ambient occlusion, or camo masks. Bind "
            "textures from this manifest for a faithful PBR result; the "
            "embedded FBX materials are a lowest-common-denominator "
            "fallback. Paths are file names relative to the FBX's "
            "texture directory.",
        "materials":  sorted(materials, key=lambda m: m["blender_material"]),
    }
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return len(materials)


__all__ = [
    "PrepCounts",
    "scene_materials",
    "prep_material_for_fbx",
    "prep_all_materials",
    "bake_base_color",
    "write_material_manifest",
]
