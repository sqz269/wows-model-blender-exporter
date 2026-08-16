"""Static-bake + material-dedup + join for draw-call-bound consumers.

A fully-assembled battleship is ~615 mesh objects with ~570 material
instances. That's fine for Blender and modern engines; it's hostile to a
consumer with no batching (Koikatsu is Unity 5.6 — one-plus draw call
per renderer, per pass). Measured on AL Massachusetts, the 571 materials
collapse to 94 unique (material class, texture set) pairs, so combining
gets ~6.5x fewer renderers with zero visual change.

Three steps, all destructive (run on a throwaway scene right before FBX
export — after camo application, before ``fbx_prep``):

1. **Bake to world**: every mesh is evaluated through the depsgraph
   (applies armature deform at rest — safe because WG bind pose equals
   rest pose, verified empirically: disabling all armature modifiers on
   Massachusetts changes nothing) and transformed into world space. The
   same move the prop pipeline uses (`export_prop_fbx.py`), for the same
   reason: parented/skinned transform chains through FBX are fragile.
2. **Dedup materials**: canonical key = (name stripped of Blender's
   ``.NNN`` suffix, the set of images bound to its WoWS slots). Two
   turrets' materials merge; two parts of the same WG class with
   different textures stay separate.
3. **Join by material**: single-material meshes sharing a canonical
   material become one object named after it. Groups exceeding Unity
   5.6's 65k-vertex ceiling are left as multiple objects (a join would
   only be auto-split at import anyway, and the split children lose
   their name).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import bpy
import mathutils

logger = logging.getLogger(__name__)

#: Unity 5.6 mesh import ceiling (16-bit index buffers).
_VERT_LIMIT = 65_000

_SUFFIX_RE = re.compile(r"\.\d+$")

#: Image-bearing node names that define a material's identity.
_SLOT_NODES = (
    "WoWS_baseColor", "WoWS_camo_matAlbedo", "WoWS_baseColor_baked",
    "WoWS_metallicRoughness", "WoWS_normal", "WoWS_occlusion",
    "WoWS_emissive",
)


@dataclass
class CombineStats:
    meshes_in:     int = 0
    meshes_out:    int = 0
    materials_in:  int = 0
    materials_out: int = 0
    baked:         int = 0
    over_limit_groups: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"meshes {self.meshes_in}->{self.meshes_out}  "
                f"materials {self.materials_in}->{self.materials_out}"
                + (f"  ({self.over_limit_groups} groups kept split for 65k)"
                   if self.over_limit_groups else ""))


def _material_key(mat: bpy.types.Material) -> tuple:
    """Identity of a material: stripped name + slot images + camo identity.

    The camo props are part of the identity: two instances of one class
    with the SAME slot images can carry DIFFERENT Path-A/B paint
    (category tile / mode / skin). Without them the merge keeps an
    arbitrary scene-order winner, so the paint a given part receives
    differed between exports of the same ship (caught as "the wreck's
    superstructure paint doesn't match the intact ship's").
    """
    name = _SUFFIX_RE.sub("", mat.name)
    slots = []
    if mat.use_nodes:
        for node_name in _SLOT_NODES:
            node = mat.node_tree.nodes.get(node_name)
            img = getattr(node, "image", None) if node else None
            if img is not None:
                slots.append((node_name, img.filepath or img.name))
    camo = tuple(
        str(mat.get(k, "")) for k in
        ("wows_camo_path", "wows_camo_category", "wows_camo_skin", "wows_camo_mode")
    )
    return (name, tuple(sorted(slots)), camo)


def _bake_to_world(obj: bpy.types.Object, depsgraph) -> None:
    """Replace the object's mesh with its evaluated, world-space bake."""
    mesh = bpy.data.meshes.new_from_object(
        obj.evaluated_get(depsgraph), depsgraph=depsgraph,
    )
    mesh.transform(obj.matrix_world)
    old = obj.data
    obj.modifiers.clear()
    obj.parent = None
    obj.matrix_parent_inverse = mathutils.Matrix.Identity(4)
    obj.matrix_world = mathutils.Matrix.Identity(4)
    obj.data = mesh
    if old.users == 0:
        bpy.data.meshes.remove(old)


def combine_for_export(
    stats: CombineStats | None = None,
    *,
    per_segment: bool = False,
    keep_skinned: bool = False,
) -> CombineStats:
    """Run the bake + dedup + join over the whole scene.

    ``per_segment=True`` (wreck exports) joins per (``wows_segment``
    custom prop, material) instead of per material alone — otherwise the
    join would weld the separated wreck pieces back into one ship — and
    re-hangs the joined meshes under fresh ``seg_<Section>`` root
    Empties, since step 1 deletes all scaffolding.

    ``keep_skinned=True`` (skinned-FK exports) exempts every armature,
    everything beneath one, and every armature-deformed mesh from the
    bake/join/purge: those subtrees ship rigged so the consumer's
    skinning can blend them when posed. Material dedup still covers
    them (visual no-op, big bundle win).
    """
    from .pivot_rig import PIVOT_PROP
    from .wreck import SEGMENT_PREFIX, SEGMENT_PROP

    stats = stats or CombineStats()

    all_meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    protected: set[str] = set()
    if keep_skinned:
        for arm in [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]:
            protected.add(arm.name)
            for c in arm.children_recursive:
                protected.add(c.name)
        for o in all_meshes:
            if any(m.type == "ARMATURE" for m in o.modifiers):
                protected.add(o.name)

    meshes = [o for o in all_meshes if o.name not in protected]
    stats.meshes_in = len(all_meshes)

    # ---- 0. record the pivot skeleton (poseable turret transforms) ----
    # Step 1 deletes every Empty, so capture each pivot's world matrix,
    # its nearest pivot ancestor and its segment now; meshes remember
    # their nearest pivot so the join keeps articulated parts separate
    # and step 4 can re-hang them on recreated pivots.
    def _nearest_pivot(o: bpy.types.Object) -> bpy.types.Object | None:
        p = o.parent
        while p is not None and not (p.type == "EMPTY" and p.get(PIVOT_PROP)):
            p = p.parent
        return p

    pivot_rec: dict[str, tuple[mathutils.Matrix, str | None, str]] = {}
    for o in bpy.context.scene.objects:
        if o.type == "EMPTY" and o.get(PIVOT_PROP):
            anc = _nearest_pivot(o)
            pivot_rec[o.name] = (
                o.matrix_world.copy(),
                anc.name if anc is not None else None,
                str(o.get(SEGMENT_PROP, "")),
            )
    for o in meshes:
        anc = _nearest_pivot(o)
        o["wows_pivot"] = anc.name if anc is not None else ""

    # ---- 1. bake ------------------------------------------------------
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in meshes:
        _bake_to_world(obj, depsgraph)
        stats.baked += 1

    # Armatures and the (now transform-free) empty scaffolding are dead
    # weight for a static export — except protected (skinned) subtrees,
    # which keep their rigs. Protected objects whose ancestor scaffolding
    # is about to go are orphaned world-preserving first.
    orphans: list[tuple[bpy.types.Object, mathutils.Matrix]] = []
    if protected:
        for name in protected:
            o = bpy.data.objects.get(name)
            if o is not None and o.parent is not None \
                    and o.parent.name not in protected:
                orphans.append((o, o.matrix_world.copy()))
    for obj in [o for o in list(bpy.data.objects) if o.type == "ARMATURE"]:
        if obj.name in protected:
            continue
        bpy.data.objects.remove(obj, do_unlink=True)
    for obj in [o for o in list(bpy.data.objects) if o.type == "EMPTY"]:
        if obj.name in protected:
            continue
        bpy.data.objects.remove(obj, do_unlink=True)
    for o, w in orphans:
        try:
            o.parent = None
            o.matrix_world = w
        except ReferenceError:
            pass

    # ---- 2. dedup materials ------------------------------------------
    canonical: dict[tuple, bpy.types.Material] = {}
    seen_mats: set[str] = set()
    for obj in [o for o in bpy.context.scene.objects if o.type == "MESH"]:
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None:
                continue
            seen_mats.add(mat.name)
            key = _material_key(mat)
            keep = canonical.setdefault(key, mat)
            if keep is not mat:
                slot.material = keep
    stats.materials_in = len(seen_mats)
    stats.materials_out = len(canonical)

    # ---- 3. join by material (per segment / per pivot) ---------------
    groups: dict[tuple[str, str, str], list[bpy.types.Object]] = {}
    for obj in [o for o in bpy.context.scene.objects if o.type == "MESH"]:
        if obj.name in protected:
            continue
        mats = [s.material for s in obj.material_slots if s.material]
        if len(mats) != 1:
            # Multi-material meshes are foreign to the producer's output;
            # leave them alone rather than joining across materials.
            continue
        seg = str(obj.get(SEGMENT_PROP, "")) if per_segment else ""
        pivot = str(obj.get("wows_pivot", ""))
        groups.setdefault((seg, pivot, mats[0].name), []).append(obj)

    out_count = 0
    for (seg, pivot, mat_name), objs in groups.items():
        if len(objs) == 1:
            out_count += 1
            continue
        # Respect the 65k ceiling: greedily bucket members.
        buckets: list[list[bpy.types.Object]] = []
        cur: list[bpy.types.Object] = []
        cur_v = 0
        for o in sorted(objs, key=lambda o: len(o.data.vertices), reverse=True):
            v = len(o.data.vertices)
            if cur and cur_v + v > _VERT_LIMIT:
                buckets.append(cur)
                cur, cur_v = [], 0
            cur.append(o)
            cur_v += v
        if cur:
            buckets.append(cur)
        if len(buckets) > 1:
            stats.over_limit_groups += 1

        base = _SUFFIX_RE.sub("", mat_name)
        if pivot:
            base = f"{pivot}__{base}"
        if seg:
            base = f"{seg}__{base}"
        for i, bucket in enumerate(buckets):
            if len(bucket) == 1:
                out_count += 1
                continue
            bpy.ops.object.select_all(action="DESELECT")
            for o in bucket:
                o.select_set(True)
            bpy.context.view_layer.objects.active = bucket[0]
            bpy.ops.object.join()
            joined = bpy.context.view_layer.objects.active
            joined.name = base if i == 0 else f"{base}_{i + 1:02d}"
            joined.data.name = joined.name
            out_count += 1

    # ---- 4. re-hang meshes: segment roots + recreated pivots ---------
    seg_roots: dict[str, bpy.types.Object] = {}

    def get_seg_root(seg: str) -> bpy.types.Object | None:
        if not (per_segment and seg):
            return None
        root = seg_roots.get(seg)
        if root is None:
            root = bpy.data.objects.new(f"{SEGMENT_PREFIX}{seg}", None)
            root.empty_display_type = "PLAIN_AXES"
            bpy.context.scene.collection.objects.link(root)
            seg_roots[seg] = root
        return root

    recreated: dict[str, bpy.types.Object] = {}

    def ensure_pivot(name: str) -> bpy.types.Object:
        e = recreated.get(name)
        if e is not None:
            return e
        world, parent_name, seg = pivot_rec[name]
        e = bpy.data.objects.new(name, None)
        e.empty_display_type = "ARROWS"
        e.empty_display_size = 0.5
        bpy.context.scene.collection.objects.link(e)
        e[PIVOT_PROP] = True
        if seg:
            e[SEGMENT_PROP] = seg
        parent = (
            ensure_pivot(parent_name) if parent_name else get_seg_root(seg)
        )
        if parent is not None:
            e.parent = parent
        e.matrix_world = world
        recreated[name] = e
        return e

    for obj in [o for o in bpy.context.scene.objects if o.type == "MESH"]:
        if obj.name in protected:
            continue
        pivot = str(obj.get("wows_pivot", ""))
        if pivot and pivot in pivot_rec:
            parent = ensure_pivot(pivot)
        else:
            seg = str(obj.get(SEGMENT_PROP, ""))
            if per_segment and not seg:
                stats.notes.append(
                    f"combine: mesh {obj.name!r} has no segment tag; left at root"
                )
            parent = get_seg_root(seg)
        if parent is not None:
            # world-baked meshes: preserve identity world under any parent
            obj.parent = parent
            obj.matrix_parent_inverse = parent.matrix_world.inverted()

    # Orphaned protected roots (skinned rigs whose instance scaffolding
    # was purged) re-hang under their wreck segment root.
    if per_segment and protected:
        for name in protected:
            o = bpy.data.objects.get(name)
            if o is None or o.parent is not None:
                continue
            seg = str(o.get(SEGMENT_PROP, ""))
            root = get_seg_root(seg)
            if root is not None:
                w = o.matrix_world.copy()
                o.parent = root
                o.matrix_parent_inverse = mathutils.Matrix.Identity(4)
                o.matrix_world = w

    stats.meshes_out = len([o for o in bpy.context.scene.objects if o.type == "MESH"])
    return stats


__all__ = ["CombineStats", "combine_for_export"]
