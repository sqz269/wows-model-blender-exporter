"""Skinned FK rig preparation — real deformation for poseable turrets.

The pivot-rig pass (:mod:`wows_blender.pivot_rig`) converts skinning to
rigid pieces, which snaps WG's soft-weighted blend regions (the canvas
blast bags between gun barrels and the turret face) to one bone — posing
the gun then rotates the fabric as a slab instead of stretching it.

This module is the skinning-preserving alternative: armatures and
vertex weights ship through to the consumer, whose engine blends them
(a static importer must keep rigs — Unity: ``animationType=Generic``).
What still has to happen at export time:

* **Unique bone names.** FK-by-name tooling resolves bones by a scene-
  wide name search; separate armatures otherwise all carry a bone
  called ``Rotate_X``. Bones are renamed ``<name>.NNN`` per armature
  index (Blender syncs the meshes' vertex-group names automatically).
* **Canonical FK bone frames.** Ring-style FK UIs rotate about bone
  local axes; conversion chains leave those inconsistent per mount.
  Re-orienting a bone's REST in edit mode is bind-safe (Blender's bind
  pose IS the rest pose), so every FK bone gets the level,
  heading-aligned frame (Y up after the FBX axis map: yaw ring
  horizontal, pitch ring on X).
* **Attachment ride-along.** Bundled attachments re-hang on the node
  their ``p1`` Murmur3 hash names — as a plain child when it is an
  object, bone-parented when it is a bone.
* **FK bone manifest.** Writes which FK-stem bones exist and whether
  they influence any vertices (``Roll_Back`` joints only earn an FK
  handle when they deform geometry) — consumed by the ItemBoneList
  generator so the studio lists come from ground truth, not a prefab
  dump.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import bpy
import mathutils

from .pivot_rig import _FK_STEM_RE, murmur3_32
from .wreck import SEGMENT_PROP

#: Custom-prop key on armature objects: JSON list of this rig's FK bone
#: records, harvested into the ship-level manifest at export.
FK_BONES_PROP = "wows_fk_bones"


@dataclass
class SkinnedRigStats:
    armatures: int = 0
    bones_renamed: int = 0
    bones_canonicalized: int = 0
    attachments_rehung: int = 0
    fk_bones: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.armatures} rigs kept skinned, "
                f"{self.bones_renamed} bones uniquified, "
                f"{self.bones_canonicalized} FK frames canonicalized, "
                f"{self.fk_bones} FK bones, "
                f"{self.attachments_rehung} attachments re-hung")


def _weighted_bones(arm: bpy.types.Object) -> set[str]:
    """Names of bones that influence at least one vertex."""
    out: set[str] = set()
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        if not any(m.type == "ARMATURE" and m.object == arm for m in obj.modifiers):
            continue
        gidx = {vg.index: vg.name for vg in obj.vertex_groups}
        for v in obj.data.vertices:
            for g in v.groups:
                if g.weight > 1e-4 and g.group in gidx:
                    out.add(gidx[g.group])
    return out


def _canonical_bone_matrix(m: mathutils.Matrix) -> mathutils.Matrix:
    """Level, heading-aligned, det=+1 frame at ``m``'s position.

    Same convention as the pivot-rig canonicalization: X = the bone's
    world X projected horizontal (the trunnion axis for pitch bones),
    world-up in the Y column so the FBX→Unity index-for-index axis map
    puts the yaw ring on Unity local Y.
    """
    t = m.translation.copy()
    x = mathutils.Vector((m[0][0], m[1][0], m[2][0]))
    x.z = 0.0
    if x.length < 1e-4:
        x = mathutils.Vector((1.0, 0.0, 0.0))
    x.normalize()
    up = mathutils.Vector((0.0, 0.0, 1.0))
    z = x.cross(up)
    return mathutils.Matrix((
        (x.x, up.x, z.x, t.x),
        (x.y, up.y, z.y, t.y),
        (x.z, up.z, z.z, t.z),
        (0.0, 0.0, 0.0, 1.0),
    ))


def prepare_skinned_fk(
    *, on_warning: Callable[[str], None] | None = None,
) -> SkinnedRigStats:
    """Prepare every armature for skinned-FK consumption (see module doc)."""
    warn = on_warning or (lambda m: None)
    stats = SkinnedRigStats()
    view = bpy.context.view_layer

    armatures = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
    for idx, arm in enumerate(armatures):
        stats.armatures += 1
        weighted = _weighted_bones(arm)
        # "_bNNN" — never collides with Blender's ".NNN" object dedup, so
        # bone names stay scene-unique against armature-OBJECT names too.
        suffix = f"_b{idx:03d}"
        fk_records: list[dict] = []

        # --- the armature OBJECT itself can be an FK node (AGM3113-style
        #     rigs fold the yaw node into the armature). Canonicalize it
        #     FIRST — bones live in armature space, so the bone pass below
        #     must see the final armature frame. Children keep world; the
        #     armature deform is identity at rest regardless of object
        #     orientation, so skinned meshes are unaffected. Bones are
        #     compensated below (bone_fix) so their WORLD rest placements
        #     stay bit-exact — without it every bone orbits the armature
        #     origin and the pitch pivots float above the turret. --------
        obj_stem = arm.name.split(".")[0]
        bone_fix = mathutils.Matrix.Identity(4)
        if _FK_STEM_RE.fullmatch(obj_stem):
            old_world = arm.matrix_world.copy()
            new_world = _canonical_bone_matrix(old_world)
            children = [(c, c.matrix_world.copy()) for c in arm.children]
            arm.matrix_world = new_world
            for c, cw in children:
                c.matrix_world = cw
            bone_fix = new_world.inverted() @ old_world
            fk_records.append({
                "name": arm.name,
                "stem": obj_stem,
                "weighted": True,   # rotating the object moves the whole rig
            })
            stats.bones_canonicalized += 1

        # --- rename bones scene-unique (vertex groups sync automatically),
        #     canonicalize FK bone rest frames (edit mode, bind-safe) ----
        bpy.ops.object.select_all(action="DESELECT")
        arm.select_set(True)
        view.objects.active = arm
        bpy.ops.object.mode_set(mode="EDIT")
        arm_world = arm.matrix_world.copy()
        arm_world_inv = arm_world.inverted()
        if bone_fix != mathutils.Matrix.Identity(4):
            for eb in arm.data.edit_bones:
                length = eb.length
                eb.matrix = bone_fix @ eb.matrix
                eb.length = max(length, 1e-4)
        for eb in arm.data.edit_bones:
            base = eb.name.split(".")[0]
            is_fk = bool(_FK_STEM_RE.fullmatch(base))
            new_name = f"{base}{suffix}"
            had_weights = eb.name in weighted or base in weighted
            if eb.name != new_name:
                eb.name = new_name
                stats.bones_renamed += 1
            if is_fk:
                # Re-orient the REST frame: world-canonical, mapped back
                # into armature space. Length preserved for sanity.
                length = eb.length
                world = arm_world @ eb.matrix
                eb.matrix = arm_world_inv @ _canonical_bone_matrix(world)
                eb.length = max(length, 1e-4)
                stats.bones_canonicalized += 1
                fk_records.append({
                    "name": new_name,
                    "stem": base,
                    "weighted": bool(had_weights),
                })
        bpy.ops.object.mode_set(mode="OBJECT")

        arm[FK_BONES_PROP] = json.dumps(fk_records)
        stats.fk_bones += len(fk_records)

    # --- attachments ride the node their p1 hash names -------------------
    name_hash_cache: dict[str, int] = {}

    def nh(name: str) -> int:
        h = name_hash_cache.get(name)
        if h is None:
            h = murmur3_32(name.split(".")[0].encode("utf-8"))
            name_hash_cache[name] = h
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
        mw = obj.matrix_world.copy()
        done = False
        for cand in host.children_recursive:
            # object-level match (plain empties or armature objects)
            inside_attachment = False
            p: bpy.types.Object | None = cand
            while p is not None and p is not host:
                if p.get("wows_attached_placement_id"):
                    inside_attachment = True
                    break
                p = p.parent
            if inside_attachment or p is None:
                continue
            if nh(cand.name) == target and cand is not obj \
                    and cand not in obj.children_recursive:
                obj.parent = cand
                obj.parent_type = "OBJECT"
                obj.parent_bone = ""
                obj.matrix_parent_inverse = mathutils.Matrix.Identity(4)
                obj.matrix_world = mw
                stats.attachments_rehung += 1
                done = True
                break
            if cand.type == "ARMATURE":
                for bone in cand.data.bones:
                    if nh(bone.name) == target:
                        obj.parent = cand
                        obj.parent_type = "BONE"
                        obj.parent_bone = bone.name
                        obj.matrix_parent_inverse = mathutils.Matrix.Identity(4)
                        obj.matrix_world = mw
                        stats.attachments_rehung += 1
                        done = True
                        break
            if done:
                break

    return stats


def write_fk_manifest(path: Path) -> int:
    """Write the ship-level FK bone manifest as JSON.

    Each handle records its name and its wreck segment (empty for intact
    exports) so the ItemBoneList generator can key wreck-segment items.
    ``Roll_Back`` joints without weights are dropped — a sphere that
    moves nothing is noise; ``Rotate_*`` handles always ship.
    """
    handles: list[dict] = []
    for arm in bpy.context.scene.objects:
        if arm.type != "ARMATURE":
            continue
        raw = arm.get(FK_BONES_PROP)
        if not raw:
            continue
        segment = str(arm.get(SEGMENT_PROP, "") or "")
        for rec in json.loads(raw):
            if rec["stem"].startswith("Roll_Back") and not rec["weighted"]:
                continue
            handles.append({"name": rec["name"], "segment": segment})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fk_bones": handles}, indent=1), encoding="utf-8")
    return len(handles)


__all__ = [
    "FK_BONES_PROP",
    "SkinnedRigStats",
    "prepare_skinned_fk",
    "write_fk_manifest",
]
