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
from .sidecar import MaterialEntry, Placement, Skin, coerce_material, load_ship, load_skins
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
    return [obj for obj in new if obj.parent not in new]


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
        attached += 1

    return attached, filtered


def _import_accessory_placements(
    placements: tuple[Placement, ...],
    library: LibraryIndex,
    library_root: Path,
    parent_root: bpy.types.Object,
    *,
    misc_filters: dict[str, list[str] | None] | None = None,
    role_filter: tuple[str, ...] | None = None,
) -> tuple[int, int, int, int]:
    """Instantiate every placement under ``parent_root``.

    Returns ``(placed, skipped, attached, attached_filtered)`` — placed =
    successfully instantiated, skipped = either the library entry was
    missing, the GLB was missing from disk, or the matrix was non-finite.
    """
    placed = 0
    skipped = 0
    attached_total = 0
    filtered_total = 0
    misc_filters = misc_filters or {}
    # Group placements by role into a parent Empty per role for
    # outliner sanity — ships have 4 turrets + 80 antiair mounts and
    # interleaving them in a flat hierarchy is unreadable.
    role_parents: dict[str, bpy.types.Object] = {}
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

        # Bundled attachments — sidecar misc_filter wins over the
        # accessories.json copy (webview parity: Phase-6 autofill is
        # authoritative), falling back to the placement's own field.
        mf = misc_filters.get(p.instance_id)
        if mf is None and p.instance_id not in misc_filters:
            mf = list(p.misc_filter) if p.misc_filter is not None else None
        att, filt = _instantiate_attachments(
            instance_root, asset, library, library_root, mf,
        )
        attached_total += att
        filtered_total += filt

    return placed, skipped, attached_total, filtered_total


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
    lod_policy: str = "lod0",
    damage_variants: bool = False,
    overlays: bool = False,
    prune_filtered: bool = False,
    on_warning: Callable[[str], None] | None = None,
) -> BuildResult:
    """Build one ship into the current scene and return what happened.

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
    if not hull_glb.is_file():
        raise FileNotFoundError(f"hull GLB not found: {hull_glb}")

    # The publisher puts ``accessories/`` + the camo atlases alongside each
    # ship folder under the dest root: dest/<Ship>/, dest/accessories/,
    # dest/camo_masks/, dest/camo_mat/. Walk up one level.
    publish_root = ship_dir.parent

    skins = load_skins(sidecar.skins)
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

    # Ship root empty — everything lands underneath it for easy
    # selection + cleanup.
    root = bpy.data.objects.new(f"{sidecar.ship_name}_root", None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 5.0
    bpy.context.scene.collection.objects.link(root)
    root["wows_ship_name"]      = sidecar.ship_name
    root["wows_schema_version"] = sidecar.schema_version
    root["wows_skin_id"]        = skin.skin_id if skin is not None else DEFAULT_SKIN_ID

    # Hull.
    hull_objs = _import_glb(hull_glb)
    hull_root = bpy.data.objects.new(f"{sidecar.ship_name}_hull", None)
    hull_root.empty_display_type = "PLAIN_AXES"
    bpy.context.scene.collection.objects.link(hull_root)
    hull_root.parent = root
    _link_under(hull_root, hull_objs)

    material_lookup = {m.material_id: m for m in sidecar.materials}

    bound_count = 0
    camo_count = 0
    if bind_materials:
        bound_count, camo_count = _bind_materials(
            hull_root, material_lookup, models_dir,
            scheme=scheme, skin=skin, publish_root=publish_root,
        )

    if library_root_override:
        library_root = Path(library_root_override)
    else:
        library_root = publish_root / "accessories"

    placed = skipped = attached = attached_filtered = 0
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
                placed, skipped, attached, attached_filtered = _import_accessory_placements(
                    sidecar.placements, library, library_root, root,
                    misc_filters=_misc_filter_from_sidecar(sidecar.raw),
                )
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
                            scheme=scheme, skin=skin, publish_root=publish_root,
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
        placed=placed,
        skipped=skipped,
        attached=attached,
        attached_filtered=attached_filtered,
        slots_bound=bound_count,
        camo_applied=camo_count,
        skin_id=skin.skin_id if skin is not None else DEFAULT_SKIN_ID,
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
    "resolve_skin",
]
