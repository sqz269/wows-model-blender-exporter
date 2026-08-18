"""Ship + accessory import operators, and FBX export.

Four Blender Operators:

* :class:`WOWS_OT_import_ship` — pick a ``<Ship>.meta.json``, get the
  hull GLB + every accessory placement instantiated at the correct
  transform with materials wired to PNG textures (and, with a non-default
  ``skin_id``, the camo overlay on top).
* :class:`WOWS_OT_import_accessory` — pick one ``<asset_id>.glb`` and
  import it standalone (useful for inspecting library assets).
* :class:`WOWS_OT_list_skins` — report the camo / permoflage skins a
  sidecar offers, so ``skin_id`` can be filled in without leaving Blender.
* :class:`WOWS_OT_export_fbx` — write the current scene to FBX after
  running the material-prep pass that makes WoWS graphs legible to
  Blender's FBX exporter.

The scene-building itself lives in :mod:`wows_blender.build` so the
headless CLI can reach it without a window manager. Operators here are
thin wrappers that translate arguments and report results.
"""
from __future__ import annotations

import logging
from pathlib import Path

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper, ImportHelper

from .build import (
    DEFAULT_SKIN_ID,
    build_ship,
    list_skins,
    reconcile_mirrored_skin_normals,
)
from .fbx_prep import PrepCounts, bake_base_color, prep_all_materials, write_material_manifest

logger = logging.getLogger(__name__)


def _import_glb(glb_path: Path) -> list[bpy.types.Object]:
    """Import a GLB and return the newly created top-level objects."""
    before = set(bpy.context.scene.objects)
    bpy.ops.import_scene.gltf(filepath=str(glb_path))
    after = set(bpy.context.scene.objects)
    new = list(after - before)
    fixed = reconcile_mirrored_skin_normals(
        [obj for obj in new if obj.type == "MESH"])
    if fixed:
        logger.info(
            "reconciled inside-out skin normals on %d mesh(es) from %s",
            fixed, glb_path.name)
    return [obj for obj in new if obj.parent not in new]


class WOWS_OT_import_ship(Operator, ImportHelper):
    """Import a WoWS ship (hull + all accessories) into the scene."""

    bl_idname = "wows.import_ship"
    bl_label = "Import WoWS Ship"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.meta.json;*.json", options={"HIDDEN"})

    import_accessories: BoolProperty(
        name="Import Accessories",
        description="Walk the accessories.json and instantiate every library "
                    "placement (turrets, secondaries, antiair, torpedoes, "
                    "decoratives). Disable for a hull-only import.",
        default=True,
    )

    bind_materials: BoolProperty(
        name="Bind Materials",
        description="Wire sidecar texture_sets into each material's Principled "
                    "BSDF. Disable for an untextured import.",
        default=True,
    )

    skin_id: StringProperty(
        name="Skin",
        description="Camo / permoflage skin_id to apply. Leave as 'default' "
                    "for the bare ship. Use the List Skins button to see what "
                    "this ship offers.",
        default=DEFAULT_SKIN_ID,
    )

    lod_policy: StringProperty(
        name="LOD",
        description="'lod0' keeps the high-detail meshes, 'lodN' only level "
                    "N, 'all' every level. A hull GLB ships its coarser "
                    "substitutes alongside the real mesh, so 'all' gives you "
                    "overlapping copies",
        default="lod0",
    )

    damage_variants: BoolProperty(
        name="Damage Variants",
        description="Show the crack / patch damage-state meshes. They sit on "
                    "top of the intact geometry",
        default=False,
    )

    overlays: BoolProperty(
        name="Armor / Hitbox Overlays",
        description="Show the collision volumes — solid shells around the "
                    "ship, useful only for inspection",
        default=False,
    )

    library_root_override: StringProperty(
        name="Library Override",
        description="Path to libraries/accessories/. When empty, looks "
                    "alongside the sidecar's parent directory for "
                    "../accessories/.",
        default="",
        subtype="DIR_PATH",
    )

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            result = build_ship(
                Path(self.filepath),
                import_accessories=self.import_accessories,
                bind_materials=self.bind_materials,
                library_root_override=self.library_root_override or None,
                skin_id=self.skin_id or DEFAULT_SKIN_ID,
                lod_policy=self.lod_policy or "lod0",
                damage_variants=self.damage_variants,
                overlays=self.overlays,
                # Hide, don't delete — the user can toggle these back on
                # in the outliner. The FBX path prunes instead.
                prune_filtered=False,
                on_warning=lambda m: self.report({"WARNING"}, m),
            )
        except FileNotFoundError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        except Exception as e:  # noqa: BLE001
            logger.exception("ship import failed")
            self.report({"ERROR"}, f"import failed: {e}")
            return {"CANCELLED"}

        # Select + frame the ship root for usability.
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        if result.root is not None:
            result.root.select_set(True)
            bpy.context.view_layer.objects.active = result.root

        self.report({"INFO"}, f"imported {result.summary()}")
        return {"FINISHED"}


class WOWS_OT_import_accessory(Operator, ImportHelper):
    """Import one library accessory GLB (no placement, no sidecar)."""

    bl_idname = "wows.import_accessory"
    bl_label = "Import WoWS Accessory"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".glb"
    filter_glob: StringProperty(default="*.glb", options={"HIDDEN"})

    def execute(self, context: bpy.types.Context) -> set[str]:
        glb = Path(self.filepath)
        if not glb.is_file():
            self.report({"ERROR"}, f"GLB not found: {glb}")
            return {"CANCELLED"}
        objects = _import_glb(glb)
        self.report({"INFO"}, f"imported {glb.name}: {len(objects)} root object(s)")
        return {"FINISHED"}


class WOWS_OT_list_skins(Operator, ImportHelper):
    """Report the camo / permoflage skins a sidecar offers."""

    bl_idname = "wows.list_skins"
    bl_label = "List WoWS Skins"
    bl_options = {"REGISTER"}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.meta.json;*.json", options={"HIDDEN"})

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            skins = list_skins(Path(self.filepath))
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, f"sidecar parse failed: {e}")
            return {"CANCELLED"}

        # The info log is the only multi-line surface an operator has;
        # print to the console too so the list is copy-pasteable.
        print(f"\n=== {len(skins)} skin(s) in {Path(self.filepath).name} ===")
        for s in skins:
            path = "B" if s.mat_textures else ("A" if s.color_scheme else "-")
            print(f"  [{path}] {s.skin_id:<44} {s.display_name}")
        self.report(
            {"INFO"},
            f"{len(skins)} skins — see the System Console for the list",
        )
        return {"FINISHED"}


class WOWS_OT_export_fbx(Operator, ExportHelper):
    """Export the scene to FBX with WoWS materials made FBX-legible.

    Blender's FBX exporter only sees an image texture wired DIRECTLY to a
    Principled socket. The WoWS binder routes base colour through an AO
    multiply, metallic/roughness through a channel split, and alpha
    through a cutout threshold — all invisible to the exporter. This
    operator rewires them first, so the FBX actually carries its
    textures.

    The rewrite is undoable (Ctrl+Z) — the render-accurate graph comes
    straight back.
    """

    bl_idname = "wows.export_fbx"
    bl_label = "Export WoWS FBX"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={"HIDDEN"})

    # Named `do_bake` rather than `bake_base_color` so it does not read as
    # a shadow of the imported helper of that name.
    do_bake: BoolProperty(
        name="Bake Base Colour",
        description="Bake each material's evaluated base colour to a PNG "
                    "first. Required for camo to survive — FBX materials "
                    "cannot express the Path-A palette lerp. Burns ambient "
                    "occlusion and paint into the albedo",
        default=False,
    )

    bake_size: IntProperty(
        name="Bake Size",
        description="Bake resolution per material",
        default=2048, min=64, max=8192,
    )

    embed_textures: BoolProperty(
        name="Embed Textures",
        description="Pack textures inside the .fbx instead of copying them "
                    "alongside it",
        default=False,
    )

    write_manifest: BoolProperty(
        name="Write Material Manifest",
        description="Write <name>.materials.json describing the channel "
                    "packing, ambient occlusion and camo masks that FBX "
                    "materials cannot carry",
        default=True,
    )

    axis_up: StringProperty(name="Up Axis", default="Y")
    axis_forward: StringProperty(name="Forward Axis", default="-Z")

    def execute(self, context: bpy.types.Context) -> set[str]:
        out = Path(self.filepath)
        counts = PrepCounts()

        if self.do_bake:
            # Bake BEFORE the prep rewrite — baking evaluates the real
            # render graph, which the prep pass is about to bypass.
            counts = bake_base_color(None, out.parent / f"{out.stem}_baked",
                                     size=self.bake_size, counts=counts)
            for note in counts.notes:
                self.report({"WARNING"}, note)

        counts = prep_all_materials(counts)

        try:
            bpy.ops.export_scene.fbx(
                filepath=str(out),
                use_selection=False,
                object_types={"EMPTY", "MESH", "ARMATURE"},
                use_mesh_modifiers=True,
                mesh_smooth_type="FACE",
                use_tspace=True,
                add_leaf_bones=False,
                bake_anim=False,
                path_mode="COPY",
                embed_textures=self.embed_textures,
                axis_up=self.axis_up,
                axis_forward=self.axis_forward,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("FBX export failed")
            self.report({"ERROR"}, f"FBX export failed: {e}")
            return {"CANCELLED"}

        if self.write_manifest:
            try:
                n = write_material_manifest(
                    out.with_suffix(".materials.json"),
                    fbx_name=out.name,
                    axis_up=self.axis_up,
                    axis_forward=self.axis_forward,
                )
                self.report({"INFO"}, f"manifest: {n} materials")
            except Exception as e:  # noqa: BLE001
                self.report({"WARNING"}, f"manifest write failed: {e}")

        self.report({"INFO"}, f"exported {out.name} — prep: {counts.summary()}")
        return {"FINISHED"}


classes = (
    WOWS_OT_import_ship,
    WOWS_OT_import_accessory,
    WOWS_OT_list_skins,
    WOWS_OT_export_fbx,
)


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


__all__ = [
    "WOWS_OT_import_ship",
    "WOWS_OT_import_accessory",
    "WOWS_OT_list_skins",
    "WOWS_OT_export_fbx",
    "register",
    "unregister",
]
