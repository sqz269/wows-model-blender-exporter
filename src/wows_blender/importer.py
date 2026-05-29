"""Ship + accessory import operators.

Two Blender Operators:

* :class:`WOWS_OT_import_ship` — pick a ``<Ship>.meta.json``, get the
  hull GLB + every accessory placement instantiated at the correct
  transform with materials wired to PNG textures.
* :class:`WOWS_OT_import_accessory` — pick one ``<asset_id>.glb`` and
  import it standalone (useful for inspecting library assets).

Both run synchronously inside the operator invocation. The ship
importer's heaviest pass is the accessory walk — Baltimore-class
ships have ~100 placements; expect 5-20 s on a warm cache.
"""
from __future__ import annotations

import logging
from pathlib import Path

import bpy
import mathutils
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator
from bpy_extras.io_utils import ImportHelper

from .library_index import LibraryAsset, LibraryIndex, load_library_index, resolve_asset_glb
from .materials import DEFAULT_SCHEME, bind_material
from .placement import gltf_matrix_to_blender_rows, is_finite_matrix
from .sidecar import MaterialEntry, Placement, Sidecar, coerce_material, load_ship

logger = logging.getLogger(__name__)


def _import_glb(glb_path: Path) -> list[bpy.types.Object]:
    """Import a GLB and return the newly created top-level objects.

    Blender's glTF importer creates one parent Empty per scene root
    plus children for each mesh. We capture the delta in
    ``bpy.context.scene.objects`` to know exactly which objects this
    import added.
    """
    before = set(bpy.context.scene.objects)
    bpy.ops.import_scene.gltf(filepath=str(glb_path))
    after = set(bpy.context.scene.objects)
    new = list(after - before)
    return [obj for obj in new if obj.parent not in new]


# Overlay groups the producer bakes into gun/turret accessory GLBs (per-mount
# armor for the webview's rotating armor view). Blender has no armor-view
# toggle, so importing them would render a solid shell over every turret and
# bind stub materials to the `Armor_*` meshes. Strip them on accessory import.
# (Hull-side Armor/Hitboxes are imported as-is, unchanged by this.)
_OVERLAY_GROUP_NAMES = frozenset({"Armor", "Hitboxes"})


def _strip_overlay_groups(roots: list[bpy.types.Object]) -> list[bpy.types.Object]:
    """Delete any top-level `Armor` / `Hitboxes` overlay group (+ subtree)
    from a freshly-imported accessory's roots; return the kept roots. Blender
    suffixes duplicate names (`Armor.001`), so match on the pre-dot stem."""
    kept: list[bpy.types.Object] = []
    for obj in roots:
        if obj.name.split(".")[0] in _OVERLAY_GROUP_NAMES:
            for o in [*obj.children_recursive, obj]:
                try:
                    bpy.data.objects.remove(o, do_unlink=True)
                except (ReferenceError, RuntimeError):
                    pass
        else:
            kept.append(obj)
    return kept


def _placement_to_matrix(p: Placement) -> mathutils.Matrix | None:
    """Translate one sidecar placement to a Blender world matrix."""
    if not is_finite_matrix(p.matrix):
        logger.warning("placement %s has non-finite matrix; skipping", p.instance_id)
        return None
    rows = gltf_matrix_to_blender_rows(p.matrix)
    return mathutils.Matrix(rows)


def _link_under(parent: bpy.types.Object, children: list[bpy.types.Object]) -> None:
    """Re-parent ``children`` under ``parent`` while preserving each
    child's local transform (so the placement matrix we just applied
    isn't double-counted)."""
    for child in children:
        # Only reparent objects that have no existing parent — children
        # of an imported armature should stay parented to it.
        if child.parent is None:
            child.parent = parent
            child.matrix_parent_inverse = parent.matrix_world.inverted()


def _materials_for_root(root: bpy.types.Object) -> list[bpy.types.Material]:
    """Collect unique materials assigned to meshes under ``root``."""
    seen: set[str] = set()
    out: list[bpy.types.Material] = []
    stack: list[bpy.types.Object] = [root]
    while stack:
        obj = stack.pop()
        stack.extend(obj.children)
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            if slot.material is None:
                continue
            if slot.material.name in seen:
                continue
            seen.add(slot.material.name)
            out.append(slot.material)
    return out


def _bind_materials(
    root: bpy.types.Object,
    sidecar_materials: dict[str, MaterialEntry],
    model_root: Path,
    *,
    scheme: str,
) -> int:
    """Walk every material under ``root`` and bind sidecar textures.

    Materials are matched by name — GLB material names equal sidecar
    ``material_id``. Returns the total number of slots bound across
    every material under ``root``.
    """
    bound = 0
    for mat in _materials_for_root(root):
        # Blender suffixes duplicates with ``.001`` etc.; trim back to
        # the original name for lookup.
        key = mat.name.split(".")[0]
        entry = sidecar_materials.get(key)
        if entry is None:
            continue
        bound += bind_material(mat, entry, model_root, scheme=scheme)
    return bound


def _import_accessory_placements(
    placements: tuple[Placement, ...],
    library: LibraryIndex,
    library_root: Path,
    parent_root: bpy.types.Object,
    *,
    scheme: str = DEFAULT_SCHEME,
    role_filter: tuple[str, ...] | None = None,
) -> tuple[int, int]:
    """Instantiate every placement under ``parent_root``.

    Returns ``(placed, skipped)`` — placed = successfully instantiated,
    skipped = either the library entry was missing, the GLB was
    missing from disk, or the matrix was non-finite.
    """
    placed = 0
    skipped = 0
    # Group placements by role into a parent Empty per role for
    # outliner sanity — ships have 4 turrets + 80 antiair mounts and
    # interleaving them in a flat hierarchy is unreadable.
    role_parents: dict[str, bpy.types.Object] = {}
    role_names = {
        "turret":    "Turrets",
        "secondary": "Secondaries",
        "antiair":   "AntiAir",
        "torpedo":   "Torpedoes",
        "accessory": "Accessories",
    }
    for p in placements:
        if role_filter is not None and p.role not in role_filter:
            continue
        asset = library.assets.get(p.asset_id)
        if asset is None:
            logger.info("placement %s: asset_id %r missing from library", p.instance_id, p.asset_id)
            skipped += 1
            continue
        glb = resolve_asset_glb(library_root, asset)
        if glb is None:
            logger.info(
                "placement %s: asset %s GLB not found at %s",
                p.instance_id, p.asset_id, library_root / asset.glb,
            )
            skipped += 1
            continue
        mat = _placement_to_matrix(p)
        if mat is None:
            skipped += 1
            continue

        # Group parent (one Empty per role).
        rp_name = role_names.get(p.role, p.role)
        rp = role_parents.get(rp_name)
        if rp is None:
            rp = bpy.data.objects.new(rp_name, None)
            rp.empty_display_type = "PLAIN_AXES"
            rp.empty_display_size = 0.5
            bpy.context.scene.collection.objects.link(rp)
            rp.parent = parent_root
            role_parents[rp_name] = rp

        # Instantiate the asset; wrap its top-level objects in one Empty
        # so the placement's matrix applies cleanly.
        roots = _strip_overlay_groups(_import_glb(glb))
        if not roots:
            skipped += 1
            continue
        instance_root = bpy.data.objects.new(
            f"{p.role}_{p.asset_id}_{p.hp_name or p.instance_id}",
            None,
        )
        instance_root.empty_display_type = "ARROWS"
        instance_root.empty_display_size = 0.3
        bpy.context.scene.collection.objects.link(instance_root)
        instance_root.parent = rp
        instance_root.matrix_local = mat

        _link_under(instance_root, roots)

        instance_root["wows_asset_id"]    = p.asset_id
        instance_root["wows_instance_id"] = p.instance_id
        instance_root["wows_hp_name"]     = p.hp_name or ""
        instance_root["wows_parent_section"] = p.parent_section or ""
        instance_root["wows_role"]        = p.role
        placed += 1

    return placed, skipped


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


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

    library_root_override: StringProperty(
        name="Library Override",
        description="Path to libraries/accessories/. When empty, looks "
                    "alongside the sidecar's parent directory for "
                    "../accessories/.",
        default="",
        subtype="DIR_PATH",
    )

    def execute(self, context: bpy.types.Context) -> set[str]:
        sidecar_path = Path(self.filepath)
        if not sidecar_path.is_file():
            self.report({"ERROR"}, f"sidecar not found: {sidecar_path}")
            return {"CANCELLED"}

        try:
            sidecar = load_ship(sidecar_path)
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, f"sidecar parse failed: {e}")
            return {"CANCELLED"}

        ship_dir = sidecar_path.parent
        ship_stem = sidecar_path.name.removesuffix(".meta.json")
        models_dir = ship_dir / "models"
        hull_glb = models_dir / f"{ship_stem}_hull.glb"
        if not hull_glb.is_file():
            self.report({"ERROR"}, f"hull GLB not found: {hull_glb}")
            return {"CANCELLED"}

        # Ship root empty — everything lands underneath it for easy
        # selection + cleanup.
        root = bpy.data.objects.new(f"{sidecar.ship_name}_root", None)
        root.empty_display_type = "PLAIN_AXES"
        root.empty_display_size = 5.0
        bpy.context.scene.collection.objects.link(root)
        root["wows_ship_name"]      = sidecar.ship_name
        root["wows_schema_version"] = sidecar.schema_version

        # Hull.
        hull_objs = _import_glb(hull_glb)
        hull_root = bpy.data.objects.new(f"{sidecar.ship_name}_hull", None)
        hull_root.empty_display_type = "PLAIN_AXES"
        bpy.context.scene.collection.objects.link(hull_root)
        hull_root.parent = root
        _link_under(hull_root, hull_objs)

        material_lookup = {m.material_id: m for m in sidecar.materials}

        bound_count = 0
        if self.bind_materials:
            bound_count = _bind_materials(
                hull_root, material_lookup, models_dir, scheme=DEFAULT_SCHEME,
            )

        # Library root resolution.
        if self.library_root_override:
            library_root = Path(self.library_root_override)
        else:
            # The publisher puts ``accessories/`` alongside each ship
            # folder under the dest root: dest/<Ship>/ and
            # dest/accessories/. Walk up one level.
            library_root = ship_dir.parent / "accessories"

        placed = skipped = 0
        if self.import_accessories and library_root.is_dir():
            index_path = library_root / "index.json"
            if not index_path.is_file():
                self.report(
                    {"WARNING"},
                    f"library index.json not found at {index_path}; "
                    f"importing hull only.",
                )
            else:
                try:
                    library = load_library_index(index_path)
                except Exception as e:  # noqa: BLE001
                    self.report({"WARNING"}, f"library parse failed: {e}")
                else:
                    placed, skipped = _import_accessory_placements(
                        sidecar.placements,
                        library,
                        library_root,
                        root,
                        scheme=DEFAULT_SCHEME,
                    )
                    if self.bind_materials:
                        # Bind materials on every instantiated accessory.
                        # Accessory materials live inside the library
                        # index entry, not the ship sidecar. Cache per
                        # asset_id so we don't re-coerce N times when
                        # the same asset is mounted on multiple HPs.
                        per_asset_materials: dict[str, dict[str, MaterialEntry]] = {}
                        for child in root.children_recursive:
                            if child.type != "EMPTY":
                                continue
                            asset_id = child.get("wows_asset_id")
                            if not asset_id:
                                continue
                            asset = library.assets.get(asset_id)
                            if asset is None:
                                continue
                            asset_root = (library_root / asset.glb).parent
                            cache = per_asset_materials.get(asset_id)
                            if cache is None:
                                cache = {
                                    m.get("material_id", ""): coerce_material(m)
                                    for m in asset.materials
                                }
                                per_asset_materials[asset_id] = cache
                            _bind_materials(child, cache, asset_root, scheme=DEFAULT_SCHEME)
        elif self.import_accessories:
            self.report(
                {"WARNING"},
                f"accessory library not found at {library_root}; "
                f"importing hull only. Publish accessories with "
                f"`wows-export-blender --accessories` first, or set the "
                f"Library Override path.",
            )

        # Select + frame the ship root for usability.
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        root.select_set(True)
        bpy.context.view_layer.objects.active = root

        self.report(
            {"INFO"},
            f"imported {sidecar.ship_name}: hull + {placed} placements "
            f"(skipped {skipped}, materials bound {bound_count} slots)",
        )
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


classes = (
    WOWS_OT_import_ship,
    WOWS_OT_import_accessory,
)


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


__all__ = ["WOWS_OT_import_ship", "WOWS_OT_import_accessory", "register", "unregister"]
