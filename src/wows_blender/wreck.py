"""Wreck split — the assembled ship re-hung as free damage sections.

WoWS ships are authored in hull sections (typically ``Bow`` /
``MidFront`` / ``MidBack`` / ``Stern``). Every hull mesh name carries its
section, each seam ships both a ``_patch_`` bridge (intact) and
``_crack_`` torn-edge meshes (broken, incl. ``_in`` interior faces), and
every accessory placement records ``parent_section`` — so a "ship broken
at every seam" is fully data-driven.

:func:`split_wreck_segments` runs on a scene :func:`~wows_blender.build
.build_ship` just built **with** ``damage_variants=True`` (cracks kept)
and re-hangs it as one top-level Empty per section:

* hull meshes re-parent to their section root (name-classified);
* ``_patch_`` seam bridges are deleted — a separated piece shows its
  cracks, the exact inverse of the intact export's seam law;
* placement instances re-parent by their ``wows_parent_section`` custom
  prop (their attachments and riders follow), falling back to the
  nearest section along the ship's length axis;
* every kept mesh is stamped with a ``wows_segment`` custom prop so the
  combine pass can join per (segment, material) instead of welding the
  ship back together.

All segment roots stay at the SHIP origin: spawning every piece at the
same position/rotation reassembles the broken-seam ship, and pieces are
then dragged apart to stage the sinking.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

import bpy
import mathutils

from .visibility import PATCH_RE, short_mesh_name

#: Custom-prop key stamped on every kept mesh; consumed by
#: :func:`wows_blender.combine.combine_for_export` (``per_segment``).
SEGMENT_PROP = "wows_segment"

#: Segment root Empties are named ``seg_<Section>`` — the FBX contract
#: the Unity builder's segmentPrefabs mode keys on.
SEGMENT_PREFIX = "seg_"

_CRACK_PAIR_RE = re.compile(r"([A-Za-z0-9]+)_(?:crack|patch)_([A-Za-z0-9]+)")


@dataclass
class WreckStats:
    """Outcome of one :func:`split_wreck_segments` call."""

    sections: list[str] = field(default_factory=list)
    hull_meshes: dict[str, int] = field(default_factory=dict)
    placements: dict[str, int] = field(default_factory=dict)
    patches_removed: int = 0
    cracks_kept: int = 0
    fallback_assigned: int = 0
    unclassified_dropped: int = 0

    def summary(self) -> str:
        per = ", ".join(
            f"{s}: {self.hull_meshes.get(s, 0)}h+{self.placements.get(s, 0)}p"
            for s in self.sections
        )
        bits = [
            f"{len(self.sections)} segments ({per})",
            f"{self.patches_removed} patches removed",
            f"{self.cracks_kept} cracks shown",
        ]
        if self.fallback_assigned:
            bits.append(f"{self.fallback_assigned} placements section-guessed")
        if self.unclassified_dropped:
            bits.append(f"{self.unclassified_dropped} unclassified meshes dropped")
        return ", ".join(bits)


def _section_vocab(
    hull_meshes: list[bpy.types.Object],
    placement_sections: set[str],
) -> list[str]:
    """Section names, longest first (``MidFront`` must beat ``Mid``).

    Data-driven: both sides of every ``A_crack_B`` / ``A_patch_B`` pair,
    plus whatever the placements' ``parent_section`` fields name. The
    bare-seam flavour glues the far section onto Maya's ``Shape`` suffix
    (``…_crack_BowShape``), so tokens are de-``Shape``d.
    """
    sections = {s for s in placement_sections if s}
    for obj in hull_meshes:
        m = _CRACK_PAIR_RE.search(short_mesh_name(obj.name).split(".")[0])
        if m:
            for tok in m.groups():
                tok = tok.removesuffix("Shape")
                if tok:
                    sections.add(tok)
    return sorted(sections, key=len, reverse=True)


def _classify_mesh(name: str, sections_desc: list[str]) -> str | None:
    """Section of a hull mesh, from its name.

    The producer names hull nodes ``<Model>_<Section> / <Mesh>`` and the
    mesh part itself starts with the section token; the name-repair pass
    in :mod:`wows_blender.build` may have trimmed to the mesh part, so
    both spellings are tried (group suffix first — it is unambiguous).
    """
    if " / " in name:
        grp = name.split(" / ", 1)[0]
        for s in sections_desc:
            if grp.endswith("_" + s):
                return s
    short = short_mesh_name(name).split(".")[0]
    for s in sections_desc:
        if short.startswith(s):
            return s
    return None


def _reparent_keep_world(obj: bpy.types.Object, parent: bpy.types.Object) -> None:
    mw = obj.matrix_world.copy()
    obj.parent = parent
    obj.matrix_parent_inverse = mathutils.Matrix.Identity(4)
    obj.matrix_world = mw


def _stamp_segment(obj: bpy.types.Object, section: str) -> None:
    """Mark ``obj`` and everything under it as belonging to ``section``.

    Meshes drive the combine grouping; empties/armatures carry the tag
    so derived objects (pivot Empties from the rig conversion) inherit
    the right segment.
    """
    for o in (obj, *obj.children_recursive):
        o[SEGMENT_PROP] = section


def split_wreck_segments(
    root: bpy.types.Object,
    *,
    on_warning: Callable[[str], None] | None = None,
) -> WreckStats:
    """Re-hang the built ship as per-section wreck pieces.

    ``root`` is the ship root Empty from :func:`build_ship` (which must
    have run with ``damage_variants=True`` so the cracks survived the
    content filter). On return the scene's only top-level objects are
    the ``seg_<Section>`` Empties; the original scaffolding (ship root,
    hull group, role groups) is gone.
    """
    warn = on_warning or (lambda m: None)
    stats = WreckStats()

    everything = list(root.children_recursive)
    instances = [
        o for o in everything
        if o.type == "EMPTY" and o.get("wows_instance_id")
    ]
    under_instance: set[str] = set()
    for inst in instances:
        for o in inst.children_recursive:
            under_instance.add(o.name)
    # Riders were re-hosted under other instances' child nodes, so an
    # instance can itself sit inside another instance's subtree — those
    # must follow their host, not be re-parented independently.
    top_instances = [i for i in instances if i.name not in under_instance]
    hull_meshes = [
        o for o in everything
        if o.type == "MESH" and o.name not in under_instance
    ]

    placement_sections = {
        str(i.get("wows_parent_section") or "") for i in instances
    }
    sections_desc = _section_vocab(hull_meshes, placement_sections)
    if len(sections_desc) < 2:
        raise RuntimeError(
            "wreck split found no hull sections — the hull carries no "
            "crack/patch seam meshes (no damage model?)"
        )

    # ---- hull meshes: classify, drop patches, keep cracks -------------
    assigned: dict[str, list[bpy.types.Object]] = {s: [] for s in sections_desc}
    unclassified: list[bpy.types.Object] = []
    for obj in hull_meshes:
        short = short_mesh_name(obj.name).split(".")[0]
        if PATCH_RE.search(short):
            # A separated piece shows its torn seam, so the intact
            # bridge (and its _wire twin) goes away entirely.
            stats.patches_removed += 1
            bpy.data.objects.remove(obj, do_unlink=True)
            continue
        section = _classify_mesh(obj.name, sections_desc)
        if section is None:
            unclassified.append(obj)
            continue
        if "_crack_" in short:
            stats.cracks_kept += 1
        assigned[section].append(obj)

    # ---- section extents along the length axis (for fallbacks) -------
    centers: dict[str, mathutils.Vector] = {}
    ranges: dict[str, tuple[mathutils.Vector, mathutils.Vector]] = {}
    for section, objs in assigned.items():
        pts = []
        for o in objs:
            pts.extend(o.matrix_world @ mathutils.Vector(c)
                       for c in o.bound_box)
        if not pts:
            continue
        lo = mathutils.Vector(map(min, zip(*pts)))
        hi = mathutils.Vector(map(max, zip(*pts)))
        centers[section] = (lo + hi) / 2
        ranges[section] = (lo, hi)
    live_sections = [s for s in sections_desc if s in centers]
    if len(live_sections) < 2:
        raise RuntimeError(
            "wreck split classified hull meshes into fewer than two "
            f"sections ({live_sections}) — naming scheme unrecognised"
        )
    axis = max(
        range(3),
        key=lambda i: (max(c[i] for c in centers.values())
                       - min(c[i] for c in centers.values())),
    )

    def nearest_section(world_pos: mathutils.Vector) -> str:
        x = world_pos[axis]
        for s in live_sections:
            lo, hi = ranges[s]
            if lo[axis] <= x <= hi[axis]:
                return s
        return min(live_sections, key=lambda s: abs(centers[s][axis] - x))

    for obj in unclassified:
        s = nearest_section(obj.matrix_world.translation)
        warn(f"wreck: hull mesh {obj.name!r} has no section in its name; "
             f"assigned to {s} by position")
        assigned[s].append(obj)

    # ---- build segment roots and re-hang everything -------------------
    seg_roots: dict[str, bpy.types.Object] = {}
    for section in live_sections:
        seg = bpy.data.objects.new(f"{SEGMENT_PREFIX}{section}", None)
        seg.empty_display_type = "PLAIN_AXES"
        seg.empty_display_size = 2.0
        bpy.context.scene.collection.objects.link(seg)
        seg[SEGMENT_PROP] = section
        seg_roots[section] = seg

    for section, objs in assigned.items():
        if section not in seg_roots:
            continue
        for obj in objs:
            _reparent_keep_world(obj, seg_roots[section])
            _stamp_segment(obj, section)
        stats.hull_meshes[section] = len(objs)

    placed: dict[str, int] = {s: 0 for s in live_sections}
    for inst in top_instances:
        section = str(inst.get("wows_parent_section") or "")
        if section not in seg_roots:
            guess = nearest_section(inst.matrix_world.translation)
            if section:
                warn(f"wreck: placement {inst.name!r} names unknown section "
                     f"{section!r}; assigned to {guess} by position")
            stats.fallback_assigned += 1
            section = guess
        _reparent_keep_world(inst, seg_roots[section])
        _stamp_segment(inst, section)
        placed[section] += 1
    stats.placements = placed

    # ---- sweep the old scaffolding ------------------------------------
    # Everything still under the ship root is scaffolding (hull group,
    # role-group Empties) or geometry nothing claimed; remove it all.
    for obj in [*root.children_recursive, root]:
        try:
            if obj.type == "MESH":
                stats.unclassified_dropped += 1
            bpy.data.objects.remove(obj, do_unlink=True)
        except (ReferenceError, RuntimeError):
            pass

    stats.sections = live_sections
    return stats


#: An orphan must sit this close to a pivot bone's head to be skinned
#: onto it; gunhouses/cradles share their pivot's origin, so real
#: matches are ~0.
_BONE_SNAP_MAX_M = 3.0


def _skin_mesh_to_bone(
    obj: bpy.types.Object,
    arm: bpy.types.Object,
    bone_name: str,
) -> None:
    """Turn a rigid bone-riding mesh into a genuinely SKINNED mesh.

    ``parent_type='BONE'`` objects do not survive the FBX round trip —
    both Unity and Blender re-root them to the model root, where a
    segment-wise consumer prunes them. A full-weight vertex group + an
    armature modifier IS preserved (SkinnedMeshRenderer bound to the
    bone), renders identically at rest, and follows the bone under FK.

    The mesh data is baked into ARMATURE space and the object parented
    to the armature at identity — the layout every skinning importer
    expects.
    """
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    mw = obj.matrix_world.copy()
    obj.parent = None
    obj.parent_type = "OBJECT"
    obj.matrix_parent_inverse = mathutils.Matrix.Identity(4)
    obj.matrix_world = mw
    local = arm.matrix_world.inverted() @ mw
    obj.data.transform(local)
    obj.parent = arm
    obj.matrix_parent_inverse = mathutils.Matrix.Identity(4)
    obj.matrix_local = mathutils.Matrix.Identity(4)
    for vg in list(obj.vertex_groups):
        obj.vertex_groups.remove(vg)
    vg = obj.vertex_groups.new(name=bone_name)
    vg.add(list(range(len(obj.data.vertices))), 1.0, "REPLACE")
    mod = obj.modifiers.new("Armature", "ARMATURE")
    mod.object = arm


def unparent_deformed_from_rigs() -> int:
    """Move skinned meshes off their own deforming armature object.

    At fleet scale, Blender's FBX exporter silently drops the parent
    link of SOME skinned meshes that are OBJECT-children of their
    deforming armature; importers then re-root them, which prunes them
    from segment prefabs and degrades their shading on whole-model
    imports. Skinned rendering follows the BONES, so hanging them one
    level up is visually and FK-identical. Runs for every skinned-FK
    export; the wreck rehome pass calls it implicitly via class 0.
    """
    moved = 0
    for obj in list(bpy.context.scene.objects):
        if (obj.type == "MESH" and obj.parent is not None
                and obj.parent.type == "ARMATURE"
                and obj.parent_type == "OBJECT"
                and any(m.type == "ARMATURE" for m in obj.modifiers)):
            _reparent_keep_world(obj, obj.parent.parent)
            moved += 1
    return moved


def rehome_strays(*, on_warning: Callable[[str], None] | None = None) -> int:
    """Repair every mesh that a segment-wise consumer would lose.

    Two failure classes, both shipped once as wreck pieces missing
    their main-turret gunhouses / cradles / radars:

    * meshes ``parent_type='BONE'`` on a kept armature — legal in
      Blender, but BOTH Unity and Blender re-root them to the model
      root on FBX import. Converted to genuinely skinned meshes
      (:func:`_skin_mesh_to_bone`), which survive and follow FK.
    * objects orphaned to the scene root by the rig passes / combine's
      scaffolding purge — skinned onto the nearest ``Rotate_*`` /
      ``Roll_*`` pivot bone when one sits at their origin, else re-hung
      on their segment root (tag first, position fallback).

    Segment roots are recognised by NAME — the roots combine recreates
    carry no segment prop. Returns the number of objects repaired.
    """
    warn = on_warning or (lambda m: None)
    scene_objs = list(bpy.context.scene.objects)
    seg_roots: dict[str, bpy.types.Object] = {}
    for o in scene_objs:
        stem = o.name.split(".")[0]
        if o.type == "EMPTY" and o.parent is None and stem.startswith(SEGMENT_PREFIX):
            seg_roots[stem[len(SEGMENT_PREFIX):]] = o
    if not seg_roots:
        return 0
    seg_root_set = set(seg_roots.values())

    repaired = 0

    # ---- class 0: skinned meshes parented under their own armature ----
    # At fleet scale, Blender's FBX exporter silently drops the parent
    # link of SOME skinned meshes that are OBJECT-children of their
    # deforming armature (15 of 37 on Massachusetts; which ones is
    # scene-dependent) — both Unity and Blender then re-root them and a
    # segment-wise consumer prunes them. Skinned rendering follows the
    # BONES, not the node parent, so hanging them one level up is
    # visually and FK-identical and round-trips cleanly (verified on
    # the full scene: orphans 15 -> 0).
    for obj in scene_objs:
        if (obj.type == "MESH" and obj.parent is not None
                and obj.parent.type == "ARMATURE"
                and obj.parent_type == "OBJECT"
                and any(m.type == "ARMATURE" for m in obj.modifiers)):
            _reparent_keep_world(obj, obj.parent.parent)
            repaired += 1
            if obj.parent is None:
                warn(f"wreck rehome: skinned {obj.name!r} had a rootless "
                     f"armature parent; now a stray (segment fallback)")

    # ---- class 1: rigid bone-children on kept armatures ---------------
    for obj in scene_objs:
        if (obj.type == "MESH" and obj.parent_type == "BONE"
                and obj.parent is not None and obj.parent.type == "ARMATURE"
                and obj.parent_bone):
            arm, bone = obj.parent, obj.parent_bone
            _skin_mesh_to_bone(obj, arm, bone)
            warn(f"wreck rehome: {obj.name!r} skinned onto {arm.name}/{bone}")
            repaired += 1

    # ---- class 2: scene-root orphans ----------------------------------
    strays = [
        o for o in scene_objs
        if o.parent is None and o not in seg_root_set
        and o.type in {"MESH", "ARMATURE", "EMPTY"}
    ]
    if not strays:
        return repaired

    # Pivot-bone candidates: (armature, bone name, world head position).
    pivot_bones: list[tuple[bpy.types.Object, str, mathutils.Vector]] = []
    for arm in (o for o in scene_objs if o.type == "ARMATURE"):
        for bone in arm.data.bones:
            stem = bone.name.split(".")[0]
            if stem.startswith(("Rotate_", "Roll_")):
                pivot_bones.append(
                    (arm, bone.name, arm.matrix_world @ bone.head_local)
                )

    # Segment extents for the position fallback.
    centers: dict[str, mathutils.Vector] = {}
    for section, root in seg_roots.items():
        pts = []
        for o in root.children_recursive:
            if o.type == "MESH":
                pts.extend(o.matrix_world @ mathutils.Vector(c)
                           for c in o.bound_box)
        if pts:
            lo = mathutils.Vector(map(min, zip(*pts)))
            hi = mathutils.Vector(map(max, zip(*pts)))
            centers[section] = (lo + hi) / 2
    axis = max(
        range(3),
        key=lambda i: (max(c[i] for c in centers.values())
                       - min(c[i] for c in centers.values())),
    ) if len(centers) > 1 else 1

    for stray in strays:
        pos = stray.matrix_world.translation
        if stray.type == "MESH" and pivot_bones:
            arm, bone_name, head = min(
                pivot_bones, key=lambda pb: (pb[2] - pos).length,
            )
            if (head - pos).length <= _BONE_SNAP_MAX_M:
                _skin_mesh_to_bone(stray, arm, bone_name)
                warn(f"wreck rehome: orphan {stray.name!r} skinned onto "
                     f"{arm.name}/{bone_name}")
                repaired += 1
                continue
        section = str(stray.get(SEGMENT_PROP) or "")
        if section not in seg_roots:
            for o in stray.children_recursive:
                s = str(o.get(SEGMENT_PROP) or "")
                if s in seg_roots:
                    section = s
                    break
        if section not in seg_roots:
            section = min(centers, key=lambda s: abs(centers[s][axis] - pos[axis]))
            warn(f"wreck rehome: {stray.name!r} has no segment tag; "
                 f"assigned to {section} by position")
        _reparent_keep_world(stray, seg_roots[section])
        warn(f"wreck rehome: {stray.name!r} re-hung on seg_{section}")
        repaired += 1
    return repaired


__all__ = [
    "SEGMENT_PREFIX",
    "SEGMENT_PROP",
    "WreckStats",
    "rehome_strays",
    "split_wreck_segments",
    "unparent_deformed_from_rigs",
]
