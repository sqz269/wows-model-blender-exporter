"""Armatures → poseable pivot transforms (turret yaw / gun pitch).

WG accessory rigs are (near-)rigid: each part binds to one bone —
``Rotate_Y*`` (turret yaw), ``Rotate_X*`` (gun elevation),
``Roll_BackN*`` (recoil). A skinned mesh cannot survive into a static
consumer (Unity strips the rig and re-roots the mesh to the model
root), but the SAME articulation expressed as a plain transform
hierarchy passes through everything verbatim — and pose tools that
rotate item child transforms (KKPE-style) can then aim the turrets.

Blender's glTF import already keeps most of WG's node tree as Empties
and folds only the skin-joint nodes into an armature (whose OBJECT is
the yaw node itself). :func:`armatures_to_pivots` finishes the job:

* the armature object becomes an Empty with the same name, transform,
  parent and children — the yaw pivot;
* every bone becomes a child Empty at its rest matrix (bone axes equal
  the source node axes, so "rotate about local X/Y" stays meaningful);
* every skinned mesh is split by dominant vertex group into rigid
  pieces parented to their bone's Empty (identical at rest; the few
  soft-weighted mantlet verts snap to their dominant bone);
* bundled attachments (turret-roof AA) re-hang onto their ``HP_*``
  hardpoint Empty when the host carries one, so they ride the yaw.

Every created/repurposed pivot Empty is stamped ``wows_pivot_node`` so
the combine pass can preserve the articulation (join per pivot and
re-hang the joined meshes under recreated pivots).
"""
from __future__ import annotations

import re
import struct
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

import bpy
import mathutils

from .wreck import SEGMENT_PROP

#: Custom-prop marker on every pivot Empty this pass creates.
PIVOT_PROP = "wows_pivot_node"

#: Node-name stems that get FK-facing frame canonicalization: turret
#: yaw, gun pitch (Rotate_X1 on quads), per-barrel Roll_Back joints.
_FK_STEM_RE = re.compile(r"Rotate_Y|Rotate_X\d*|Roll_Back\d+")


@dataclass
class PivotStats:
    armatures: int = 0
    bone_pivots: int = 0
    meshes_split: int = 0
    pieces: int = 0
    attachments_rehung: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [
            f"{self.armatures} rigs -> pivots",
            f"{self.bone_pivots} bone pivots",
            f"{self.meshes_split} meshes split into {self.pieces} pieces",
        ]
        if self.attachments_rehung:
            bits.append(f"{self.attachments_rehung} attachments re-hung on HPs")
        return ", ".join(bits)


def murmur3_32(data: bytes, seed: int = 0) -> int:
    """MurmurHash3_x86_32 — WG's node-name hash (toolkit parity)."""
    c1, c2 = 0xCC9E2D51, 0x1B873593
    h = seed & 0xFFFFFFFF
    n = len(data)
    rounded = n - (n % 4)
    for i in range(0, rounded, 4):
        k = struct.unpack_from("<I", data, i)[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = ((h << 13) | (h >> 19)) & 0xFFFFFFFF
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF
    k = 0
    tail = data[rounded:]
    if len(tail) >= 3:
        k ^= tail[2] << 16
    if len(tail) >= 2:
        k ^= tail[1] << 8
    if len(tail) >= 1:
        k ^= tail[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
    h ^= n
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


def _reparent_keep_world(obj: bpy.types.Object, parent: bpy.types.Object | None) -> None:
    mw = obj.matrix_world.copy()
    obj.parent = parent
    # Children of skin-joint nodes import BONE-parented; a stale
    # parent_type='BONE' pointing at a deleted armature breaks the
    # depsgraph ("Failed to add relation Bone Parent") and the combine
    # bake then evaluates garbage transforms — Baltimore's secondaries
    # went missing this way. Always demote to plain object parenting.
    obj.parent_type = "OBJECT"
    obj.parent_bone = ""
    obj.matrix_parent_inverse = mathutils.Matrix.Identity(4)
    obj.matrix_world = mw


def _copy_tracking_props(src: bpy.types.Object, dst: bpy.types.Object) -> None:
    for key in (SEGMENT_PROP,):
        if src.get(key) is not None:
            dst[key] = src[key]


def _face_dominant_bones(
    obj: bpy.types.Object, bone_names: set[str],
) -> tuple[list[str | None], set[str]]:
    """Per-face dominant deform bone (majority vote of vertex dominants)."""
    me = obj.data
    gidx_to_bone = {
        vg.index: vg.name for vg in obj.vertex_groups if vg.name in bone_names
    }
    vert_dom: list[str | None] = [None] * len(me.vertices)
    for v in me.vertices:
        best_w = 0.0
        best: str | None = None
        for g in v.groups:
            name = gidx_to_bone.get(g.group)
            if name is not None and g.weight > best_w:
                best_w = g.weight
                best = name
        vert_dom[v.index] = best
    face_dom: list[str | None] = [None] * len(me.polygons)
    used: set[str] = set()
    for poly in me.polygons:
        votes = Counter(
            vert_dom[vi] for vi in poly.vertices if vert_dom[vi] is not None
        )
        if votes:
            dom = votes.most_common(1)[0][0]
            face_dom[poly.index] = dom
            used.add(dom)
    return face_dom, used


def _split_mesh_by_bone(
    obj: bpy.types.Object,
    bone_empties: dict[str, bpy.types.Object],
    fallback_parent: bpy.types.Object,
    stats: PivotStats,
) -> None:
    """Replace a skinned mesh with per-bone rigid copies.

    Uses object-mode face selection + edit-mode delete (NOT a bmesh
    round-trip) so the corpus' custom split normals survive on the
    kept faces — this runs after the packed-normal fix and must not
    regress it.
    """
    face_dom, used = _face_dominant_bones(obj, set(bone_empties))
    if not used:
        # Statically bound or unweighted: just de-rig in place.
        obj.modifiers.clear()
        _reparent_keep_world(obj, fallback_parent)
        return
    if len(used) == 1:
        # Whole mesh rides one bone — no split needed.
        bone = next(iter(used))
        obj.modifiers.clear()
        _reparent_keep_world(obj, bone_empties[bone])
        return

    stats.meshes_split += 1
    view = bpy.context.view_layer
    for bone in sorted(used):
        copy = obj.copy()
        copy.data = obj.data.copy()
        copy.name = f"{obj.name.split('.')[0]}__{bone.removesuffix('_BlendBone')}"
        bpy.context.scene.collection.objects.link(copy)
        copy.modifiers.clear()
        _copy_tracking_props(obj, copy)

        # Keep only this bone's faces: select the others, delete them.
        me = copy.data
        for poly in me.polygons:
            poly.select = face_dom[poly.index] != bone
        for e in me.edges:
            e.select = False
        for v in me.vertices:
            v.select = False
        bpy.ops.object.select_all(action="DESELECT")
        copy.select_set(True)
        view.objects.active = copy
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.context.tool_settings.mesh_select_mode = (False, False, True)
        bpy.ops.mesh.delete(type="FACE")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.delete_loose()
        bpy.ops.object.mode_set(mode="OBJECT")

        _reparent_keep_world(copy, bone_empties[bone])
        stats.pieces += 1
    bpy.data.objects.remove(obj, do_unlink=True)


def armatures_to_pivots(
    *, on_warning: Callable[[str], None] | None = None,
) -> PivotStats:
    """Convert every armature in the scene into a pivot-Empty hierarchy."""
    warn = on_warning or (lambda m: None)
    stats = PivotStats()

    for arm in [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]:
        stats.armatures += 1
        original_name = arm.name
        arm.name = original_name + "__rig"

        pivot_root = bpy.data.objects.new(original_name, None)
        pivot_root.empty_display_type = "ARROWS"
        pivot_root.empty_display_size = 0.5
        bpy.context.scene.collection.objects.link(pivot_root)
        pivot_root[PIVOT_PROP] = True
        _copy_tracking_props(arm, pivot_root)
        _reparent_keep_world(pivot_root, arm.parent)
        pivot_root.matrix_world = arm.matrix_world.copy()

        # Bones → Empties (parents first so the chain hooks up).
        bone_empties: dict[str, bpy.types.Object] = {}
        bones = list(arm.data.bones)
        pending = [b for b in bones if b.parent is None]
        ordered: list[bpy.types.Bone] = []
        while pending:
            b = pending.pop(0)
            ordered.append(b)
            pending.extend(b.children)
        for bone in ordered:
            e = bpy.data.objects.new(bone.name, None)
            e.empty_display_type = "ARROWS"
            e.empty_display_size = 0.4
            bpy.context.scene.collection.objects.link(e)
            e[PIVOT_PROP] = True
            _copy_tracking_props(arm, e)
            parent = (
                bone_empties[bone.parent.name] if bone.parent else pivot_root
            )
            e.parent = parent
            e.matrix_world = arm.matrix_world @ bone.matrix_local
            bone_empties[bone.name] = e
            stats.bone_pivots += 1

        # The armature object's children move over. Plain children go to
        # the pivot root; BONE-parented children (nodes authored under a
        # skin joint — muzzle HPs, rigidly-bound barrel meshes on
        # Roll_Back-jointed rigs) go to THEIR bone's Empty so posing that
        # bone still carries them.
        skinned: list[bpy.types.Object] = []
        fallback_for: dict[str, bpy.types.Object] = {}
        for child in list(arm.children):
            target = pivot_root
            if child.parent_type == "BONE" and child.parent_bone in bone_empties:
                target = bone_empties[child.parent_bone]
            if child.type != "MESH":
                _reparent_keep_world(child, target)
                continue
            if any(m.type == "ARMATURE" for m in child.modifiers):
                skinned.append(child)
                fallback_for[child.name] = target
            else:
                # Rigid mesh riding a joint node (per-barrel geometry):
                # no split needed — keep it whole on its bone's Empty.
                _reparent_keep_world(child, target)
        # Meshes deformed by this armature but parented elsewhere.
        for o in bpy.context.scene.objects:
            if o.type != "MESH" or o in skinned:
                continue
            if any(
                m.type == "ARMATURE" and m.object == arm for m in o.modifiers
            ):
                skinned.append(o)
                fallback_for.setdefault(o.name, pivot_root)

        for obj in skinned:
            _split_mesh_by_bone(
                obj, bone_empties, fallback_for.get(obj.name, pivot_root), stats,
            )

        try:
            bpy.data.objects.remove(arm, do_unlink=True)
        except (ReferenceError, RuntimeError):
            warn(f"pivot rig: could not remove armature {original_name!r}")

    # Canonicalize the FK-facing pivot frames. The conversion chains
    # (glTF→Blender axis rebase, the attached-accessory X-flip, WG's
    # baked Y180 mirror frames) leave each pivot with a different local
    # frame — an FK ring UI that rotates about node-local axes then
    # behaves differently per mount (some yaw rings ROLL, mirrored
    # mounts rotate backwards). Replace every Rotate_Y/Rotate_X pivot's
    # orientation with a level, heading-aligned, det=+1 frame — position
    # kept, children's world transforms preserved, so the rest pose is
    # untouched — making "horizontal ring = traverse, side ring =
    # elevate" uniform across the ship.
    for pivot in [
        o for o in bpy.context.scene.objects
        if o.type == "EMPTY" and o.get(PIVOT_PROP)
        and _FK_STEM_RE.fullmatch(o.name.split(".")[0])
    ]:
        w = pivot.matrix_world
        t = w.translation.copy()
        x = mathutils.Vector((w[0][0], w[1][0], w[2][0]))
        x.z = 0.0
        if x.length < 1e-4:
            x = mathutils.Vector((1.0, 0.0, 0.0))
        x.normalize()
        # Column layout chosen for the FBX→Unity round trip, which maps
        # Blender local axes to Unity local axes index-for-index (world
        # directions preserved): up goes in the Y column so Unity's
        # local Y — the FK yaw ring — is world-up, and X stays the
        # (heading-projected) trunnion axis for the pitch ring.
        up = mathutils.Vector((0.0, 0.0, 1.0))
        z = x.cross(up)
        canon = mathutils.Matrix((
            (x.x, up.x, z.x, t.x),
            (x.y, up.y, z.y, t.y),
            (x.z, up.z, z.z, t.z),
            (0.0, 0.0, 0.0, 1.0),
        ))
        children = [(c, c.matrix_world.copy()) for c in pivot.children]
        pivot.matrix_world = canon
        for c, cw in children:
            c.matrix_world = cw

    # Attachments (turret-roof AA, deck gear) record the Murmur3_32 hash
    # of the host-model node they were authored on (p1). Re-hang each on
    # the matching node — after conversion those are plain objects (the
    # "Rotate_Y" pivot for turret-roof gear), so posed yaw carries them.
    # World transform is preserved, so the rest pose is unchanged.
    hash_cache: dict[str, int] = {}

    def name_hash(name: str) -> int:
        h = hash_cache.get(name)
        if h is None:
            h = murmur3_32(name.encode("utf-8"))
            hash_cache[name] = h
        return h

    for obj in list(bpy.context.scene.objects):
        raw = obj.get("wows_attached_p1_hash")
        if not raw or obj.parent is None:
            continue
        try:
            target = int(str(raw), 16)
        except ValueError:
            continue
        host = obj.parent
        best: bpy.types.Object | None = None
        best_depth = 1 << 30
        for cand in host.children_recursive:
            if name_hash(cand.name.split(".")[0]) != target:
                continue
            # The p1 node belongs to the HOST model — never to a sibling
            # attachment (whose own converted rig also has a Rotate_Y).
            depth = 0
            p: bpy.types.Object | None = cand
            inside_attachment = False
            while p is not None and p is not host:
                if p.get("wows_attached_placement_id"):
                    inside_attachment = True
                    break
                p = p.parent
                depth += 1
            if inside_attachment or p is None:
                continue
            if depth < best_depth:
                best_depth = depth
                best = cand
        if best is None or best in obj.children_recursive:
            continue
        _reparent_keep_world(obj, best)
        stats.attachments_rehung += 1

    return stats


__all__ = ["PIVOT_PROP", "PivotStats", "armatures_to_pivots"]
