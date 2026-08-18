"""Headless-callable ship construction.

Includes the WG-runtime attachment composition (webview parity): hosts
whose library entry names an ``attached_accessories.json`` get their
bundled misc placements (rangefinders, searchlights, ammo boxes...)
instantiated under the host instance, gated by the per-HP ``misc_filter``
whitelist. Attachment matrices are host-local and post-multiplied by
``diag(-1,1,1,1)`` — the producer authors them for an X-negating
consumer (gltFast); unmirrored importers (three.js, Blender) apply the
local X-flip explicitly. See ``webview/src/lib/ship/placement.ts``.

The scene-building logic used to live inside
:class:`~wows_blender.importer.WOWS_OT_import_ship.execute`, which made it
unreachable from a ``blender --background`` run (an Operator needs a
window manager and a filepath dialog). :func:`build_ship` is that same
logic as a plain function; the operator is now a thin wrapper over it and
the headless FBX driver calls it directly.

Everything here still imports ``bpy`` — it builds real Blender datablocks.
The pure-stdlib readers (``sidecar``, ``library_index``, ``placement``,
``camo``) stay bpy-free so they remain testable outside Blender.
"""
from __future__ import annotations

import json
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import bpy
import mathutils

from .camo import resolve_path_a
from .camo_nodes import apply_path_a, apply_path_b
from .library_index import LibraryAsset, LibraryIndex, load_library_index, resolve_asset_glb
from .materials import DEFAULT_SCHEME, bind_material
from .placement import gltf_matrix_to_blender_rows, is_finite_matrix
from .sidecar import (
    Exterior,
    ExteriorMount,
    MaterialEntry,
    Placement,
    Skin,
    coerce_material,
    load_decoratives,
    load_ship,
    load_skins,
)
from .visibility import HULL_HIDDEN_GROUPS, keeps_mesh, short_mesh_name

logger = logging.getLogger(__name__)

#: Skin id of the bare ship (no camo). Always available.
DEFAULT_SKIN_ID = "default"

# Overlay groups the producer bakes into gun/turret accessory GLBs (per-mount
# armor for the webview's rotating armor view). Blender has no armor-view
# toggle, so importing them would render a solid shell over every turret and
# bind stub materials to the `Armor_*` meshes. Strip them on accessory import.
# (Hull-side Armor/Hitboxes are imported as-is, unchanged by this.)
_OVERLAY_GROUP_NAMES = frozenset({"Armor", "Hitboxes"})

_ROLE_GROUP_NAMES: dict[str, str] = {
    "turret":    "Turrets",
    "secondary": "Secondaries",
    "antiair":   "AntiAir",
    "torpedo":   "Torpedoes",
    "accessory": "Accessories",
}


@dataclass
class BuildResult:
    """Outcome of one :func:`build_ship` call."""

    root:        bpy.types.Object | None = None
    ship_name:   str = ""
    placed:      int = 0
    skipped:     int = 0
    attached:    int = 0
    attached_filtered: int = 0
    slots_bound: int = 0
    camo_applied: int = 0
    skin_id:     str = DEFAULT_SKIN_ID
    exterior_id: str | None = None
    mounts_swapped:    int = 0
    decoratives_placed: int = 0
    base_decoratives_dropped: int = 0
    meshes_kept:     int = 0
    meshes_filtered: int = 0
    warnings:    list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [
            f"{self.ship_name}: hull + {self.placed} placements",
            f"{self.attached} attached ({self.attached_filtered} misc-dropped)",
            f"skipped {self.skipped}",
            f"{self.slots_bound} texture slots",
        ]
        if self.exterior_id:
            bits.append(
                f"exterior {self.exterior_id}: {self.mounts_swapped} mounts "
                f"swapped, {self.decoratives_placed} decoratives "
                f"(+{self.base_decoratives_dropped} base dropped)"
            )
        if self.meshes_filtered:
            bits.append(
                f"{self.meshes_kept} meshes kept / {self.meshes_filtered} filtered"
            )
        if self.skin_id != DEFAULT_SKIN_ID:
            bits.append(f"skin {self.skin_id} on {self.camo_applied} materials")
        return ", ".join(bits)


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
    roots = [obj for obj in new if obj.parent not in new]
    fixed = reconcile_mirrored_skin_normals(
        [obj for obj in new if obj.type == "MESH"])
    if fixed:
        logger.info(
            "reconciled inside-out skin normals on %d mesh(es) from %s",
            fixed, glb_path.name)
    return roots


def reconcile_mirrored_skin_normals(objs: list[bpy.types.Object]) -> int:
    """Fix inside-out custom normals on Z-mirror (det<0) skinned imports.

    det<0 rigs store bind-frame vertex data whose triangle winding is
    source-order — the runtime skin un-mirrors it, so glTF consumers that
    honour the mirrored joints render correctly. Blender's glTF importer
    instead parks the mirror in the rig (bone rest != bind): on evaluation
    the positions mirror and the apparent winding lands outward-correct,
    but the custom split normals decode relative to the *unflipped* loop
    fans, so every normal comes out pointing INTO the surface. Everything
    downstream (renders, bakes, mesh exports) then lights the
    armature-deformed meshes inside-out while static meshes stay healthy.

    Detection runs on the EVALUATED mesh: the fraction of faces whose
    stored corner normals agree with the winding-derived face normal.
    Below 0.5 the normals sit on the wrong side of their own faces —
    winding is authoritative post-evaluation — so negate the raw custom
    normals (winding untouched); re-evaluation then lands outward.
    Meshes without custom normals cannot trigger (their normals follow
    the loop fans by construction), and det>0 rigs pass untouched.
    """
    candidates: list[bpy.types.Object] = []
    seen_data: set[str] = set()
    for obj in objs:
        if obj.type != "MESH" or len(obj.data.polygons) == 0:
            continue
        if not any(m.type == "ARMATURE" and m.object is not None
                   for m in obj.modifiers):
            continue
        if obj.data.name in seen_data:
            continue
        seen_data.add(obj.data.name)
        candidates.append(obj)
    if not candidates:
        return 0

    deps = bpy.context.evaluated_depsgraph_get()
    fixed = 0
    for obj in candidates:
        ev = obj.evaluated_get(deps)
        me = ev.to_mesh()
        try:
            agree = total = 0
            for poly in me.polygons:
                if poly.normal.length < 1e-9:
                    continue
                acc = mathutils.Vector((0.0, 0.0, 0.0))
                for li in range(poly.loop_start,
                                poly.loop_start + poly.loop_total):
                    acc += me.loops[li].normal
                if acc.length < 1e-9:
                    continue
                total += 1
                if poly.normal.dot(acc) > 0.0:
                    agree += 1
        finally:
            ev.to_mesh_clear()
        if total == 0 or agree / total >= 0.5:
            continue
        raw = obj.data
        raw.normals_split_custom_set(
            [(-l.normal.x, -l.normal.y, -l.normal.z) for l in raw.loops])
        fixed += 1
    return fixed


def _glb_mesh_node_names(glb_path: Path) -> list[tuple[str, int]]:
    """``(node_name, vertex_count)`` for every mesh-bearing GLB node.

    Pure-stdlib GLB header walk (the JSON chunk is always first); vertex
    count is the summed POSITION accessor count over the mesh's
    primitives. Used only to repair Blender's 63-char name truncation,
    so a malformed file degrades to "no repairs", never to a failure.
    """
    try:
        with open(glb_path, "rb") as f:
            if f.read(4) != b"glTF":
                return []
            f.read(8)  # version + total length
            chunk_len, chunk_type = struct.unpack("<2I", f.read(8))
            if chunk_type != 0x4E4F534A:  # 'JSON'
                return []
            doc = json.loads(f.read(chunk_len))
    except (OSError, ValueError, struct.error):
        return []
    accessors = doc.get("accessors") or []
    meshes = doc.get("meshes") or []
    out: list[tuple[str, int]] = []
    for node in doc.get("nodes") or []:
        mi = node.get("mesh")
        if mi is None or not (0 <= mi < len(meshes)):
            continue
        name = node.get("name") or meshes[mi].get("name") or ""
        verts = 0
        for prim in meshes[mi].get("primitives") or []:
            ai = (prim.get("attributes") or {}).get("POSITION")
            if ai is not None and 0 <= ai < len(accessors):
                verts += int(accessors[ai].get("count") or 0)
        if name:
            out.append((name, verts))
    return out


#: Blender clamps datablock names to 63 bytes; colliding truncations get
#: the base cut to 59 to fit a ``.NNN`` suffix.
_BL_NAME_MAX = 63
_BL_NAME_SUFFIXED_MAX = 59


def _repair_truncated_names(
    roots: list[bpy.types.Object], glb_path: Path,
) -> int:
    """Undo Blender's 63-char truncation of long hull node names.

    ``<Model>_<Section> / <Mesh>`` node names routinely exceed Blender's
    63-byte limit for the crack / patch-wire LOD variants — truncation
    eats the ``_lodN`` suffix and colliding stems gain ``.NNN``, so the
    LOD/damage filters see e.g. four "identical" lod0 cracks stacked in
    place. Match each truncated object back to the GLB's real node names
    (unique-prefix first, vertex-count rank inside ambiguous clusters)
    and rename it to the node's MESH part, which always fits.

    Returns the number of objects renamed.
    """
    fulls = [(n, v) for n, v in _glb_mesh_node_names(glb_path)
             if len(n) > _BL_NAME_MAX]
    if not fulls:
        return 0
    mesh_objs: list[bpy.types.Object] = []
    for r in roots:
        for o in (r, *r.children_recursive):
            if o.type == "MESH":
                mesh_objs.append(o)

    # Cluster on the shortest surviving prefix so the 63-char first copy
    # and its 59-char ``.NNN`` siblings land in the same bucket.
    clusters: dict[str, list[tuple[str, int]]] = {}
    for n, v in fulls:
        clusters.setdefault(n[:_BL_NAME_SUFFIXED_MAX], []).append((n, v))

    repaired = 0
    for key, cands in clusters.items():
        suspects = [
            o for o in mesh_objs
            if len(o.name.split(".")[0]) >= _BL_NAME_SUFFIXED_MAX
            and o.name[:_BL_NAME_SUFFIXED_MAX] == key
        ]
        if not suspects:
            continue
        if len(suspects) != len(cands):
            logger.warning(
                "name repair: cluster %r has %d objects vs %d GLB nodes; "
                "pairing best-effort", key, len(suspects), len(cands),
            )
        # LODs shrink monotonically, so vertex-count rank pairs them even
        # if Blender's import dedup nudged the absolute counts.
        suspects.sort(key=lambda o: len(o.data.vertices), reverse=True)
        ordered = sorted(cands, key=lambda nv: nv[1], reverse=True)
        for obj, (full, _v) in zip(suspects, ordered):
            new = short_mesh_name(full)
            if obj.name != new:
                obj.name = new
                repaired += 1
    return repaired


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


#: Local X-mirror post-multiplied onto every attachment matrix. The producer
#: authors attachment transforms for an X-negating importer (gltFast);
#: unmirrored importers apply this explicitly (webview `applyAttachedMatrix`).
#: diag(-1,1,1) is axis-aligned, so it commutes with the glTF→Blender axis
#: rebase and can be applied after conversion.
_ATTACHED_X_FLIP = mathutils.Matrix.Scale(-1.0, 4, (1.0, 0.0, 0.0))


def _attached_to_matrix(matrix16) -> mathutils.Matrix | None:
    """Translate one attachment's host-local matrix to Blender local."""
    m = tuple(float(v) for v in matrix16)
    if not is_finite_matrix(m):
        return None
    return mathutils.Matrix(gltf_matrix_to_blender_rows(m)) @ _ATTACHED_X_FLIP


def _load_attached_doc(library_root: Path, asset: "LibraryAsset") -> dict[str, Any] | None:
    """Read a host's ``attached_accessories.json``; None when absent."""
    rel = asset.attached_accessories
    if not rel:
        return None
    path = library_root / rel
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("attached_accessories unreadable for %s: %s", asset.asset_id, e)
        return None


def _misc_filter_from_sidecar(raw: dict[str, Any]) -> dict[str, list[str] | None]:
    """Per-instance ``misc_filter`` from the sidecar's typed mount groups.

    The sidecar's Phase-6 autofill is the authoritative source (webview
    parity — it takes precedence over any value accessories.json carries).
    Three states matter, so ``None``-vs-``[]`` must be preserved:
      absent → render every live attachment; ``[]`` → drop all;
      ``[ids…]`` → whitelist by ``placement_id``.
    """
    out: dict[str, list[str] | None] = {}
    for group in ("turrets", "secondaries", "antiair", "torpedoes", "accessories"):
        for m in raw.get(group) or []:
            if not isinstance(m, dict):
                continue
            iid = m.get("instance_id")
            if iid and "misc_filter" in m:
                mf = m.get("misc_filter")
                out[str(iid)] = [str(x) for x in mf] if isinstance(mf, list) else None
    return out


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
    skin: Skin | None = None,
    publish_root: Path | None = None,
) -> tuple[int, int]:
    """Walk every material under ``root`` and bind sidecar textures.

    Materials are matched by name — GLB material names equal sidecar
    ``material_id``. Returns ``(slots_bound, camo_applied)``.

    When ``skin`` is a non-default skin, the camo overlay is layered on
    top of the freshly-bound PBR graph: Path B (a pre-baked albedo swap
    for the part category) wins where the skin provides one, else Path A
    (the 4-row palette lerp gated by the ``camoExclusionMask``).
    """
    bound = 0
    camo = 0
    for mat in _materials_for_root(root):
        # Blender suffixes duplicates with ``.001`` etc.; trim back to
        # the original name for lookup.
        key = mat.name.split(".")[0]
        entry = sidecar_materials.get(key)
        if entry is None:
            continue
        bound += bind_material(mat, entry, model_root, scheme=scheme)
        if skin is None or skin.skin_id == DEFAULT_SKIN_ID:
            continue
        if apply_path_b(mat, entry, skin, publish_root or model_root, model_root):
            camo += 1
            continue
        if skin.color_scheme is not None:
            resolved = resolve_path_a(
                entry, skin, publish_root or model_root, model_root,
            )
            if apply_path_a(mat, entry, resolved, model_root, scheme=scheme):
                camo += 1
    return bound, camo


def _instantiate_attachments(
    host_root: bpy.types.Object,
    host_asset: LibraryAsset,
    library: LibraryIndex,
    library_root: Path,
    misc_filter: list[str] | None,
) -> tuple[int, int]:
    """Instantiate a host's bundled attachments under ``host_root``.

    Returns ``(attached, filtered)``. Live list only — the Studio item is
    the intact ship. ``misc_filter`` semantics (WG runtime, webview
    parity): ``None`` = keep all, ``[]`` = drop all, else whitelist by
    ``placement_id``.
    """
    doc = _load_attached_doc(library_root, host_asset)
    att_list = (doc or {}).get("attachments_live") or []
    if not att_list:
        return 0, 0

    attached = filtered = 0
    filter_set = set(misc_filter) if misc_filter else None
    drop_all = misc_filter is not None and len(misc_filter) == 0

    for att in att_list:
        pid = str(att.get("placement_id") or "")
        if drop_all or (filter_set is not None and pid not in filter_set):
            filtered += 1
            continue
        child_id = str(att.get("asset_id") or "")
        child = library.assets.get(child_id)
        glb = resolve_asset_glb(library_root, child) if child else None
        if glb is None:
            logger.info("attachment %s: asset %r unresolved", pid, child_id)
            continue
        matrix16 = (att.get("transform") or {}).get("matrix")
        if not (isinstance(matrix16, list) and len(matrix16) == 16):
            continue
        mat = _attached_to_matrix(matrix16)
        if mat is None:
            continue

        roots = _strip_overlay_groups(_import_glb(glb))
        if not roots:
            continue
        child_root = bpy.data.objects.new(f"attached_{child_id}_{pid}", None)
        child_root.empty_display_type = "ARROWS"
        child_root.empty_display_size = 0.2
        bpy.context.scene.collection.objects.link(child_root)
        child_root.parent = host_root       # host-LOCAL matrix
        child_root.matrix_local = mat
        _link_under(child_root, roots)

        child_root["wows_asset_id"] = child_id
        child_root["wows_attached_placement_id"] = pid
        child_root["wows_attached_to"] = host_root.name
        # Murmur3_32(seed=0) of the host-model node this attachment is
        # authored on ("Rotate_Y" for turret-roof gear) — lets the pivot
        # rig re-hang it so it rides the yaw.
        child_root["wows_attached_p1_hash"] = str(att.get("p1_hash") or "")
        attached += 1

    return attached, filtered


@dataclass
class _PlaceStats:
    placed:   int = 0
    skipped:  int = 0
    attached: int = 0
    attached_filtered: int = 0
    mounts_swapped:    int = 0
    skel_ext_dropped:  int = 0


def _import_accessory_placements(
    placements: tuple[Placement, ...],
    library: LibraryIndex,
    library_root: Path,
    parent_root: bpy.types.Object,
    *,
    misc_filters: dict[str, list[str] | None] | None = None,
    role_filter: tuple[str, ...] | None = None,
    mount_swaps: dict[str, ExteriorMount] | None = None,
    skip_skel_ext: bool = False,
    on_warning: Callable[[str], None] | None = None,
) -> _PlaceStats:
    """Instantiate every placement under ``parent_root``.

    ``mount_swaps`` (exterior mode) maps ``hp_name`` → the exterior's
    variant mount record: the swapped asset replaces the base one, the
    record's own matrix and TRI-STATE misc_filter apply VERBATIM (Unity
    ExteriorComposer parity), and an unresolvable variant asset degrades
    to the base mount — never to a hole in the ship (§12c).

    ``skip_skel_ext`` drops placements with ``source == 'skel_ext_hash'``
    — the hull-baked decoratives layer a hull-swap exterior replaces
    wholesale with its own decoratives file.
    """
    stats = _PlaceStats()
    misc_filters = misc_filters or {}
    mount_swaps = mount_swaps or {}
    warn = on_warning or (lambda m: logger.warning("%s", m))
    # Group placements by role into a parent Empty per role for
    # outliner sanity — ships have 4 turrets + 80 antiair mounts and
    # interleaving them in a flat hierarchy is unreadable.
    role_parents: dict[str, bpy.types.Object] = {}
    # hp_name → instance_root, for the rider re-host pass below.
    instance_by_hp: dict[str, bpy.types.Object] = {}
    # (rider_root, host_hp, child_node_name) collected during the loop.
    riders: list[tuple[bpy.types.Object, str, str]] = []
    for p in placements:
        if role_filter is not None and p.role not in role_filter:
            continue
        if skip_skel_ext and p.source == "skel_ext_hash":
            stats.skel_ext_dropped += 1
            continue

        # Exterior mount swap: keyed by hardpoint. The swap applies its
        # own asset / matrix / misc_filter; a missing variant asset falls
        # back to the base mount below.
        swap = mount_swaps.get(p.hp_name) if p.hp_name else None
        asset_id = p.asset_id
        matrix16 = p.matrix
        swap_active = False
        if swap is not None:
            cand = swap.asset_id or p.asset_id
            cand_asset = library.assets.get(cand)
            if cand_asset is not None and resolve_asset_glb(library_root, cand_asset):
                asset_id = cand
                if swap.matrix is not None:
                    matrix16 = swap.matrix
                swap_active = True
            else:
                warn(
                    f"exterior mount {p.hp_name}: variant asset {cand!r} not in "
                    f"the accessory library; keeping the base mount "
                    f"(publish the library — the producer harvests "
                    f"exteriors[].mounts assets)"
                )

        asset = library.assets.get(asset_id)
        if asset is None:
            logger.info("placement %s: asset_id %r missing from library", p.instance_id, asset_id)
            stats.skipped += 1
            continue
        glb = resolve_asset_glb(library_root, asset)
        if glb is None:
            logger.info(
                "placement %s: asset %s GLB not found at %s",
                p.instance_id, asset_id, library_root / asset.glb,
            )
            stats.skipped += 1
            continue
        if not is_finite_matrix(matrix16):
            logger.warning("placement %s has non-finite matrix; skipping", p.instance_id)
            stats.skipped += 1
            continue
        mat = mathutils.Matrix(gltf_matrix_to_blender_rows(matrix16))

        # Group parent (one Empty per role).
        rp_name = _ROLE_GROUP_NAMES.get(p.role, p.role)
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
            stats.skipped += 1
            continue
        instance_root = bpy.data.objects.new(
            f"{p.role}_{asset_id}_{p.hp_name or p.instance_id}",
            None,
        )
        instance_root.empty_display_type = "ARROWS"
        instance_root.empty_display_size = 0.3
        bpy.context.scene.collection.objects.link(instance_root)
        instance_root.parent = rp
        instance_root.matrix_local = mat

        _link_under(instance_root, roots)

        instance_root["wows_asset_id"]    = asset_id
        instance_root["wows_instance_id"] = p.instance_id
        instance_root["wows_hp_name"]     = p.hp_name or ""
        instance_root["wows_parent_section"] = p.parent_section or ""
        instance_root["wows_role"]        = p.role
        stats.placed += 1
        if p.hp_name:
            instance_by_hp[p.hp_name] = instance_root
        if swap_active:
            stats.mounts_swapped += 1
            instance_root["wows_exterior_swap"] = True
            if swap.attach_to:
                child = (
                    p.hp_name[len(swap.attach_to) + 1:]
                    if p.hp_name and p.hp_name.startswith(swap.attach_to + "_")
                    else ""
                )
                if child:
                    riders.append((instance_root, swap.attach_to, child))

        # Bundled attachments. Exterior swaps carry the nodesConfig
        # misc_filter VERBATIM (None = all, [] = drop all); otherwise the
        # sidecar's Phase-6 autofill wins over the accessories.json copy
        # (webview parity), falling back to the placement's own field.
        if swap_active:
            mf = list(swap.misc_filter) if swap.misc_filter is not None else None
        else:
            mf = misc_filters.get(p.instance_id)
            if mf is None and p.instance_id not in misc_filters:
                mf = list(p.misc_filter) if p.misc_filter is not None else None
        att, filt = _instantiate_attachments(
            instance_root, asset, library, library_root, mf,
        )
        stats.attached += att
        stats.attached_filtered += filt

    # Rider re-host (Unity ExteriorComposer.RehostRider parity): a rider
    # mount's composite hp names the child node it hangs from inside the
    # host turret; the variant rider parents to the VARIANT host's
    # matching node at identity local. No matching node = WG's model-level
    # way of excluding the rider on this skin → drop it.
    for rider_root, host_hp, child_name in riders:
        host = instance_by_hp.get(host_hp)
        node = _find_shallowest_named(host, child_name) if host else None
        if node is None:
            warn(
                f"exterior rider {rider_root.name}: host {host_hp!r} has no "
                f"child node {child_name!r}; dropping (visual exclusion)"
            )
            for o in [*rider_root.children_recursive, rider_root]:
                try:
                    bpy.data.objects.remove(o, do_unlink=True)
                except (ReferenceError, RuntimeError):
                    pass
            stats.placed -= 1
            continue
        rider_root.parent = node
        rider_root.matrix_parent_inverse = mathutils.Matrix.Identity(4)
        rider_root.matrix_local = mathutils.Matrix.Identity(4)

    return stats


def _find_shallowest_named(
    root: bpy.types.Object | None, name: str,
) -> bpy.types.Object | None:
    """Shallowest descendant OBJECT whose pre-dot name matches ``name``.

    Blender suffixes duplicate names (``HP_AGA_4.001``), so match on the
    stem. Bone-hosted nodes inside an armature are not reachable this way
    — riders on skinned variant turrets whose mount node imported as a
    bone are dropped by the caller with a warning (none in the corpus so
    far; revisit with a bone-parent path if one appears).
    """
    if root is None:
        return None
    best: bpy.types.Object | None = None
    best_depth = 1 << 30
    for obj in root.children_recursive:
        if obj.name.split(".")[0] != name:
            continue
        depth = 0
        parent = obj.parent
        while parent is not None and parent != root:
            depth += 1
            parent = parent.parent
        if depth < best_depth:
            best_depth = depth
            best = obj
    return best


def apply_content_filter(
    root: bpy.types.Object,
    *,
    lod_policy: str = "lod0",
    damage_variants: bool = False,
    overlays: bool = False,
    prune: bool = False,
) -> tuple[int, int]:
    """Hide (or delete) everything that is not the ship itself.

    A hull GLB carries coarser LOD substitutes, damage-state variants and
    Armor / Hitboxes collision volumes alongside the real geometry — see
    :mod:`wows_blender.visibility` for why importing all of it verbatim
    gives you overlapping copies inside a solid shell.

    ``prune=True`` deletes rather than hides. The FBX path uses that
    because "hidden" is not a concept every DCC honours on import; the
    interactive importer hides so the user can toggle things back on.

    Returns ``(filtered, kept)`` mesh counts.
    """
    everything = [root, *root.children_recursive]

    # Pass 1: whole overlay groups (the Empty plus its subtree). Collect
    # them first so pass 2 does not also count their meshes as kept —
    # they are already condemned.
    doomed: list[bpy.types.Object] = []
    condemned: set[str] = set()
    if not overlays:
        for obj in everything:
            if obj.type != "EMPTY":
                continue
            if obj.name.split(".")[0] not in HULL_HIDDEN_GROUPS:
                continue
            for o in [obj, *obj.children_recursive]:
                if o.name not in condemned:
                    condemned.add(o.name)
                    doomed.append(o)

    # Pass 2: per-mesh LOD / damage rules over what is left.
    kept = 0
    for obj in everything:
        if obj.type != "MESH" or obj.name in condemned:
            continue
        if keeps_mesh(
            short_mesh_name(obj.name),
            lod_policy=lod_policy,
            damage_variants=damage_variants,
        ):
            kept += 1
        else:
            condemned.add(obj.name)
            doomed.append(obj)

    # Count meshes only — the overlay group Empties are bookkeeping, and
    # reporting them would inflate the ratio the caller prints.
    filtered = sum(1 for o in doomed if o.type == "MESH")
    for obj in doomed:
        if prune:
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except (ReferenceError, RuntimeError):
                pass
        else:
            obj.hide_viewport = True
            obj.hide_render = True

    return filtered, kept


def resolve_exterior(
    exteriors: tuple[Exterior, ...], exterior_id: str,
) -> Exterior | None:
    """Find an exterior by id (exact, else case-insensitive), else by
    display_name. Returns None when not found — caller reports the
    available ids."""
    for e in exteriors:
        if e.exterior_id == exterior_id:
            return e
    lowered = exterior_id.lower()
    for e in exteriors:
        if e.exterior_id.lower() == lowered or e.display_name.lower() == lowered:
            return e
    return None


def resolve_skin(skins: tuple[Skin, ...], skin_id: str) -> Skin | None:
    """Find a skin by ``skin_id``, else by ``display_name`` (case-insensitive).

    Returns None for the default skin or an unknown id — callers treat
    None as "no camo overlay", which is exactly the bare-ship render.
    """
    if not skin_id or skin_id == DEFAULT_SKIN_ID:
        return None
    for s in skins:
        if s.skin_id == skin_id:
            return s
    lowered = skin_id.lower()
    for s in skins:
        if s.display_name.lower() == lowered:
            return s
    return None


def build_ship(
    sidecar_path: Path,
    *,
    import_accessories: bool = True,
    bind_materials: bool = True,
    library_root_override: str | Path | None = None,
    skin_id: str = DEFAULT_SKIN_ID,
    exterior_id: str | None = None,
    lod_policy: str = "lod0",
    damage_variants: bool = False,
    overlays: bool = False,
    prune_filtered: bool = False,
    on_warning: Callable[[str], None] | None = None,
) -> BuildResult:
    """Build one ship into the current scene and return what happened.

    ``exterior_id`` selects a mesh-swap exterior from ``exteriors[]``
    (Unity ExteriorComposer parity): the exterior's hull GLB replaces the
    base hull, its ``mounts[]`` swap per-hardpoint assets with their own
    matrices + misc filters, its decoratives file replaces the base
    hull's ``skel_ext_hash`` decoratives layer wholesale, and its
    ``camo_scheme_key`` auto-selects the paint skin unless ``skin_id``
    names one explicitly.

    Raises ``FileNotFoundError`` when the sidecar or hull GLB is absent —
    those are hard errors that leave nothing usable in the scene. Softer
    problems (missing library, unparseable index, individual placements
    that fail to resolve) are reported through ``on_warning`` and
    accumulated on the result.
    """
    sidecar_path = Path(sidecar_path)
    warnings: list[str] = []

    def warn(msg: str) -> None:
        warnings.append(msg)
        logger.warning("%s", msg)
        if on_warning is not None:
            on_warning(msg)

    if not sidecar_path.is_file():
        raise FileNotFoundError(f"sidecar not found: {sidecar_path}")

    sidecar = load_ship(sidecar_path)

    ship_dir = sidecar_path.parent
    ship_stem = sidecar_path.name.removesuffix(".meta.json")
    models_dir = ship_dir / "models"
    hull_glb = models_dir / f"{ship_stem}_hull.glb"

    # ---- exterior resolution -----------------------------------------
    exterior: Exterior | None = None
    if exterior_id:
        exterior = resolve_exterior(sidecar.exteriors, exterior_id)
        if exterior is None:
            available = ", ".join(e.exterior_id for e in sidecar.exteriors) or "(none)"
            raise FileNotFoundError(
                f"exterior {exterior_id!r} not in this sidecar; available: {available}"
            )
    ext_hull = exterior.hull if exterior is not None else None
    if ext_hull is not None and ext_hull.hull_glb:
        ext_hull_path = ship_dir / ext_hull.hull_glb
        if ext_hull_path.is_file():
            hull_glb = ext_hull_path
        else:
            warn(
                f"exterior hull GLB not found: {ext_hull_path} — re-ingest the "
                f"ship with --exterior-hulls; building on the BASE hull."
            )
            ext_hull = None
    if not hull_glb.is_file():
        raise FileNotFoundError(f"hull GLB not found: {hull_glb}")

    # The publisher puts ``accessories/`` + the camo atlases alongside each
    # ship folder under the dest root: dest/<Ship>/, dest/accessories/,
    # dest/camo_masks/, dest/camo_mat/. Walk up one level.
    publish_root = ship_dir.parent

    skins = load_skins(sidecar.skins)
    # An exterior names its paint scheme; an explicit --skin still wins.
    if exterior is not None and (not skin_id or skin_id == DEFAULT_SKIN_ID):
        skin_id = exterior.camo_scheme_key or DEFAULT_SKIN_ID
    skin = resolve_skin(skins, skin_id)
    if skin_id and skin_id != DEFAULT_SKIN_ID and skin is None:
        warn(
            f"skin {skin_id!r} not found in this sidecar "
            f"({len(skins)} available); building the default ship."
        )
    # A Path-A/B skin selects which material ``texture_sets`` block to
    # sample. Skins whose scheme is absent from a material fall back to
    # ``main`` inside bind_material, so this is safe to pass through.
    scheme = skin.scheme_key if skin is not None else DEFAULT_SCHEME

    # Camo opt-out set: bespoke variant assets carry their own themed
    # albedos — the flat mat tile / palette would clobber the detail
    # (webview `variantSwappedAssetIds` + ship-level camo_skip rule).
    camo_skip: set[str] = set()
    ship_block = sidecar.raw.get("ship")
    if isinstance(ship_block, dict):
        camo_skip.update(str(x) for x in ship_block.get("camo_skip_asset_ids") or [])
        camo_skip.update(str(x) for x in ship_block.get("variant_swapped_asset_ids") or [])
    if exterior is not None:
        camo_skip.update(exterior.variant_swapped_asset_ids)

    # Ship root empty — everything lands underneath it for easy
    # selection + cleanup.
    root = bpy.data.objects.new(f"{sidecar.ship_name}_root", None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 5.0
    bpy.context.scene.collection.objects.link(root)
    root["wows_ship_name"]      = sidecar.ship_name
    root["wows_schema_version"] = sidecar.schema_version
    root["wows_skin_id"]        = skin.skin_id if skin is not None else DEFAULT_SKIN_ID
    if exterior is not None:
        root["wows_exterior_id"] = exterior.exterior_id

    # Hull.
    hull_objs = _import_glb(hull_glb)
    repaired = _repair_truncated_names(hull_objs, hull_glb)
    if repaired:
        logger.info("repaired %d truncated hull mesh names", repaired)
    hull_root = bpy.data.objects.new(f"{sidecar.ship_name}_hull", None)
    hull_root.empty_display_type = "PLAIN_AXES"
    bpy.context.scene.collection.objects.link(hull_root)
    hull_root.parent = root
    _link_under(hull_root, hull_objs)

    material_lookup = {m.material_id: m for m in sidecar.materials}

    bound_count = 0
    camo_count = 0
    if bind_materials:
        if ext_hull is not None:
            # Exterior hull: its OWN material block (base entries fill any
            # id the exterior doesn't carry), and NO camo overlay — the
            # bespoke albedo IS the skin; engine-side, mg.B is authored 0
            # on bespoke variant geometry so the paint never lands there.
            ext_lookup = dict(material_lookup)
            ext_lookup.update({m.material_id: m for m in ext_hull.materials})
            bound_count, camo_count = _bind_materials(
                hull_root, ext_lookup, models_dir,
                scheme=DEFAULT_SCHEME, skin=None, publish_root=publish_root,
            )
        else:
            bound_count, camo_count = _bind_materials(
                hull_root, material_lookup, models_dir,
                scheme=scheme, skin=skin, publish_root=publish_root,
            )

    if library_root_override:
        library_root = Path(library_root_override)
    else:
        library_root = publish_root / "accessories"

    stats = _PlaceStats()
    deco_placed = 0
    if import_accessories and library_root.is_dir():
        index_path = library_root / "index.json"
        if not index_path.is_file():
            warn(f"library index.json not found at {index_path}; importing hull only.")
        else:
            try:
                library = load_library_index(index_path)
            except Exception as e:  # noqa: BLE001
                warn(f"library parse failed: {e}")
            else:
                # A hull-swap exterior with a decoratives file replaces
                # the base hull's skel_ext decoratives layer wholesale.
                mount_swaps = (
                    {m.hp_name: m for m in exterior.mounts}
                    if exterior is not None else None
                )
                replaces_deco = bool(
                    ext_hull is not None and ext_hull.decoratives
                )
                stats = _import_accessory_placements(
                    sidecar.placements, library, library_root, root,
                    misc_filters=_misc_filter_from_sidecar(sidecar.raw),
                    mount_swaps=mount_swaps,
                    skip_skel_ext=replaces_deco,
                    on_warning=warn,
                )
                if replaces_deco:
                    deco_path = ship_dir / ext_hull.decoratives
                    if deco_path.is_file():
                        deco_stats = _import_accessory_placements(
                            load_decoratives(deco_path), library,
                            library_root, root,
                            on_warning=warn,
                        )
                        deco_placed = deco_stats.placed
                        stats.placed += deco_stats.placed
                        stats.skipped += deco_stats.skipped
                        stats.attached += deco_stats.attached
                        stats.attached_filtered += deco_stats.attached_filtered
                    else:
                        warn(f"exterior decoratives file not found: {deco_path}")
                if bind_materials:
                    # Bind materials on every instantiated accessory.
                    # Accessory materials live inside the library index
                    # entry, not the ship sidecar. Cache per asset_id so
                    # we don't re-coerce N times when the same asset is
                    # mounted on multiple HPs.
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
                        a_bound, a_camo = _bind_materials(
                            child, cache, asset_root,
                            scheme=scheme,
                            skin=None if asset_id in camo_skip else skin,
                            publish_root=publish_root,
                        )
                        bound_count += a_bound
                        camo_count += a_camo
    elif import_accessories:
        warn(
            f"accessory library not found at {library_root}; importing hull "
            f"only. Publish accessories with `wows-export-blender "
            f"--accessories` first, or set the Library Override path."
        )

    # Content filter last, so it also sweeps accessory LOD/damage meshes.
    filtered, kept = apply_content_filter(
        root,
        lod_policy=lod_policy,
        damage_variants=damage_variants,
        overlays=overlays,
        prune=prune_filtered,
    )

    return BuildResult(
        root=root,
        ship_name=sidecar.ship_name,
        placed=stats.placed,
        skipped=stats.skipped,
        attached=stats.attached,
        attached_filtered=stats.attached_filtered,
        slots_bound=bound_count,
        camo_applied=camo_count,
        skin_id=skin.skin_id if skin is not None else DEFAULT_SKIN_ID,
        exterior_id=exterior.exterior_id if exterior is not None else None,
        mounts_swapped=stats.mounts_swapped,
        decoratives_placed=deco_placed,
        base_decoratives_dropped=stats.skel_ext_dropped,
        meshes_kept=kept,
        meshes_filtered=filtered,
        warnings=warnings,
    )


def list_skins(sidecar_path: Path) -> tuple[Skin, ...]:
    """Read just the ``skins[]`` block — used by the operator's enum and
    the ``--list-skins`` CLI path without building any geometry."""
    return load_skins(load_ship(Path(sidecar_path)).skins)


__all__ = [
    "DEFAULT_SKIN_ID",
    "BuildResult",
    "apply_content_filter",
    "build_ship",
    "list_skins",
    "resolve_exterior",
    "resolve_skin",
]
