"""Headless FBX export driver — runs INSIDE Blender.

Invoked by :mod:`wows_model_blender_exporter.blender_runner` as::

    blender --background --factory-startup \\
            --python .../wows_blender/headless.py -- \\
            --sidecar <Ship>.meta.json --out <Ship>.fbx [...]

Everything after the bare ``--`` is ours; Blender consumes the rest.

This file is run as a *script*, not imported as a package module, so it
cannot use relative imports — it puts its own grandparent (``src/``) on
``sys.path`` and imports ``wows_blender.*`` absolutely. That also means a
checkout works without the add-on being installed in the target Blender.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

import bpy

# --- make `wows_blender` importable when run as a loose script ---------
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from wows_blender.build import build_ship, list_skins  # noqa: E402
from wows_blender.fbx_prep import (  # noqa: E402
    PrepCounts,
    bake_base_color,
    prep_all_materials,
    write_material_manifest,
)

EXIT_OK = 0
EXIT_ARGS = 2
EXIT_FAILED = 3


def _split_argv(argv: list[str]) -> list[str]:
    """Return only the args after Blender's ``--`` separator."""
    if "--" in argv:
        return argv[argv.index("--") + 1:]
    return []


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="wows_blender.headless")
    ap.add_argument("--sidecar", required=True, type=Path)
    ap.add_argument("--out", type=Path, help="Destination .fbx path.")
    ap.add_argument("--manifest", type=Path, help="Destination material-manifest JSON.")
    ap.add_argument("--skin", default="default")
    ap.add_argument("--exterior", default="",
                    help="Build a mesh-swap exterior from the sidecar's "
                         "exteriors[] (id or display name): variant hull, "
                         "mount swaps, decoratives, and its paint scheme "
                         "(--skin still overrides the paint).")
    ap.add_argument("--library", default="", help="Override accessories/ library root.")
    ap.add_argument("--no-accessories", action="store_true")
    ap.add_argument("--no-materials", action="store_true")
    ap.add_argument("--lod", default="lod0",
                    help="'lod0' (default) keeps the high-detail meshes; "
                         "'lodN' keeps only level N; 'all' keeps every LOD.")
    ap.add_argument("--damage-variants", action="store_true",
                    help="Keep the broken-seam crack meshes too (the intact "
                         "seam patches are always kept).")
    ap.add_argument("--wreck-segments", action="store_true",
                    help="Export the ship as separated wreck pieces: one "
                         "top-level seg_<Section> root per hull section "
                         "(Bow/MidFront/MidBack/Stern), torn crack faces "
                         "shown, intact seam patches removed, and every "
                         "placement re-hung on its parent section. All "
                         "segments share the ship origin, so spawning them "
                         "at one position reassembles the broken ship. "
                         "Implies --damage-variants.")
    ap.add_argument("--overlays", action="store_true",
                    help="Keep the Armor / Hitboxes collision volumes.")
    ap.add_argument("--pivot-rig", action="store_true",
                    help="Convert accessory armatures (turret yaw / gun "
                         "pitch / recoil bones) into plain pivot transform "
                         "hierarchies with rigidly-bound meshes: identical "
                         "at rest, survives static importers, and pose "
                         "tools can rotate the named Rotate_Y / Rotate_X "
                         "nodes. Default in --wreck-segments mode unless "
                         "--skinned-fk is given.")
    ap.add_argument("--skinned-fk", action="store_true",
                    help="Keep accessory armatures SKINNED (consumer must "
                         "import with the rig intact, e.g. Unity Generic): "
                         "soft-weighted parts (gun blast bags) deform when "
                         "posed instead of creasing. Bones are renamed "
                         "scene-unique, FK bone rest frames canonicalized, "
                         "and a <out>.fk_bones.json manifest written for "
                         "studio bone-list generation. Supersedes "
                         "--pivot-rig.")
    ap.add_argument("--combine", action="store_true",
                    help="Static-bake, dedup materials by (class, texture "
                         "set), and join meshes per material — ~6.5x fewer "
                         "renderers for draw-call-bound consumers. Flattens "
                         "the placement hierarchy and drops armatures.")
    ap.add_argument("--bake", action="store_true")
    ap.add_argument("--bake-size", type=int, default=2048)
    ap.add_argument("--axis-up", default="Y")
    ap.add_argument("--axis-forward", default="-Z")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--embed-textures", action="store_true")
    ap.add_argument("--save-blend", type=Path, help="Also save the assembled .blend.")
    ap.add_argument("--list-skins", action="store_true",
                    help="Print the sidecar's skins as JSON and exit.")
    ap.add_argument("--list-exteriors", action="store_true",
                    help="Print the sidecar's exteriors[] as JSON and exit.")
    ap.add_argument("--result-json", type=Path,
                    help="Write the machine-readable result here. Preferred "
                         "over scraping stdout: Blender interleaves its own "
                         "C-level output with Python's buffered stdout, so a "
                         "sentinel line can end up with the version banner "
                         "glued to it.")
    return ap


def _write_result(path: Path | None, payload: dict) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"warn: could not write --result-json: {e}", file=sys.stderr)


def _clear_scene() -> None:
    """Empty the factory-startup scene (cube, camera, light)."""
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _ensure_io_addons() -> None:
    """Belt-and-braces: --factory-startup should already enable the core
    glTF/FBX IO add-ons, but a stripped build might not."""
    import addon_utils

    for name in ("io_scene_gltf2", "io_scene_fbx"):
        try:
            addon_utils.enable(name, default_set=False, persistent=False)
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    args = _build_parser().parse_args(_split_argv(sys.argv))

    if args.list_skins:
        payload = [
            {
                "skin_id": s.skin_id,
                "display_name": s.display_name,
                "scheme_key": s.scheme_key,
                "kind": s.kind,
                "path": "B" if s.mat_textures else ("A" if s.color_scheme else "none"),
            }
            for s in list_skins(args.sidecar)
        ]
        _write_result(args.result_json, {"skins": payload})
        print("WOWS_SKINS_JSON " + json.dumps(payload))
        return EXIT_OK

    if args.list_exteriors:
        from wows_blender.sidecar import load_exteriors, load_sidecar

        payload = [
            {
                "exterior_id":   e.exterior_id,
                "display_name":  e.display_name,
                "peculiarity":   e.peculiarity,
                "camo_scheme":   e.camo_scheme_key,
                "hull_glb":      e.hull.hull_glb if e.hull else None,
                "decoratives":   e.hull.decoratives if e.hull else None,
                "mounts":        len(e.mounts),
            }
            for e in load_exteriors(load_sidecar(args.sidecar).raw.get("exteriors"))
        ]
        _write_result(args.result_json, {"exteriors": payload})
        print("WOWS_EXTERIORS_JSON " + json.dumps(payload))
        return EXIT_OK

    if args.out is None:
        print("error: --out is required unless --list-skins/--list-exteriors", file=sys.stderr)
        return EXIT_ARGS

    _clear_scene()
    _ensure_io_addons()

    try:
        result = build_ship(
            args.sidecar,
            import_accessories=not args.no_accessories,
            bind_materials=not args.no_materials,
            library_root_override=args.library or None,
            skin_id=args.skin,
            exterior_id=args.exterior or None,
            lod_policy=args.lod,
            damage_variants=args.damage_variants or args.wreck_segments,
            overlays=args.overlays,
            # Delete rather than hide: "hidden" is not a concept every
            # DCC honours on FBX import, so filtered geometry must not
            # be in the file at all.
            prune_filtered=True,
            on_warning=lambda m: print(f"warn: {m}", file=sys.stderr),
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FAILED
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print(f"error: ship build failed: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_FAILED

    print(f"built {result.summary()}")

    wreck_stats = None
    if args.wreck_segments:
        from wows_blender.wreck import split_wreck_segments

        try:
            wreck_stats = split_wreck_segments(
                result.root,
                on_warning=lambda m: print(f"warn: {m}", file=sys.stderr),
            )
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_FAILED
        result.root = None  # the ship root is gone; segments own the scene
        print(f"wreck: {wreck_stats.summary()}")

    pivot_stats = None
    skinned_stats = None
    if args.skinned_fk:
        from wows_blender.skinned_rig import prepare_skinned_fk

        skinned_stats = prepare_skinned_fk(
            on_warning=lambda m: print(f"warn: {m}", file=sys.stderr),
        )
        print(f"skinned fk: {skinned_stats.summary()}")
        from wows_blender.wreck import unparent_deformed_from_rigs

        moved = unparent_deformed_from_rigs()
        print(f"skinned fk: {moved} deformed meshes unparented from rigs")
    elif args.pivot_rig or args.wreck_segments:
        from wows_blender.pivot_rig import armatures_to_pivots

        pivot_stats = armatures_to_pivots(
            on_warning=lambda m: print(f"warn: {m}", file=sys.stderr),
        )
        print(f"pivot rig: {pivot_stats.summary()}")

    combine_stats = None
    if args.combine:
        from wows_blender.combine import combine_for_export

        combine_stats = combine_for_export(
            per_segment=args.wreck_segments,
            keep_skinned=args.skinned_fk,
        )
        print(f"combine: {combine_stats.summary()}")

    if args.wreck_segments:
        # Safety net: the rig passes and combine's scaffolding purge can
        # orphan bone-riding meshes to the scene root, where a
        # segment-wise consumer prunes them. Runs after every mutating
        # pass so any stray is caught.
        from wows_blender.wreck import rehome_strays

        rehomed = rehome_strays(
            on_warning=lambda m: print(f"warn: {m}", file=sys.stderr),
        )
        print(f"wreck rehome: {rehomed} strays re-hung")

    counts = PrepCounts()
    if not args.no_materials:
        if args.bake:
            # Bake BEFORE the prep rewrite — baking evaluates the real
            # render graph (the camo composite; AO is excluded), which
            # the prep pass is about to bypass. After --combine the ship root
            # empty no longer exists (combine deletes the scaffolding),
            # so fall back to the whole-scene walk — which also bakes
            # the deduped 129 materials instead of the raw 571.
            bake_dir = args.out.parent / f"{args.out.stem}_baked"
            counts = bake_base_color(
                result.root if combine_stats is None else None,
                bake_dir, size=args.bake_size, counts=counts,
            )
            # Metal-finish skins (camo MGN with nonzero metal/gloss
            # influence) also need their blended metallic/roughness
            # flattened — FBX carries one packed _mr per material.
            from wows_blender.fbx_prep import bake_camo_mgn_mr

            counts = bake_camo_mgn_mr(bake_dir, size=args.bake_size, counts=counts)
            for note in counts.notes:
                print(f"warn: {note}", file=sys.stderr)
        before = len(counts.notes)
        counts = prep_all_materials(counts)
        print(f"fbx prep: {counts.summary()}")
        for note in counts.notes[before:]:
            print(f"warn: {note}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)

    try:
        bpy.ops.export_scene.fbx(
            filepath=str(args.out),
            use_selection=False,
            use_visible=False,
            object_types={"EMPTY", "MESH", "ARMATURE"},
            use_mesh_modifiers=True,
            mesh_smooth_type="FACE",
            use_tspace=True,
            add_leaf_bones=False,
            bake_anim=False,
            path_mode="COPY",
            embed_textures=bool(args.embed_textures),
            axis_up=args.axis_up,
            axis_forward=args.axis_forward,
            global_scale=args.scale,
            apply_unit_scale=True,
        )
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print(f"error: FBX export failed: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_FAILED

    if args.skinned_fk:
        from wows_blender.skinned_rig import write_fk_manifest

        fk_path = args.out.with_suffix(".fk_bones.json")
        n_fk = write_fk_manifest(fk_path)
        print(f"fk manifest: {n_fk} handles -> {fk_path}")

    manifest_path = args.manifest or args.out.with_suffix(".materials.json")
    try:
        n = write_material_manifest(
            manifest_path,
            fbx_name=args.out.name,
            axis_up=args.axis_up,
            axis_forward=args.axis_forward,
        )
        print(f"manifest: {n} materials -> {manifest_path}")
    except Exception as e:  # noqa: BLE001
        print(f"warn: manifest write failed: {e}", file=sys.stderr)

    if args.save_blend:
        args.save_blend.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(args.save_blend))
        print(f"blend: {args.save_blend}")

    size = args.out.stat().st_size if args.out.is_file() else 0
    _write_result(args.result_json, {
        "ok":       True,
        "fbx":      str(args.out),
        "size":     size,
        "manifest": str(manifest_path),
        "skin":     result.skin_id,
        "exterior": result.exterior_id,
        "mounts_swapped":     result.mounts_swapped,
        "decoratives_placed": result.decoratives_placed,
        "ship":     result.ship_name,
        "placed":   result.placed,
        "meshes_kept":     result.meshes_kept,
        "meshes_filtered": result.meshes_filtered,
        "slots_bound":     result.slots_bound,
        "camo_applied":    result.camo_applied,
        "combine": (
            {
                "meshes_in": combine_stats.meshes_in,
                "meshes_out": combine_stats.meshes_out,
                "materials_in": combine_stats.materials_in,
                "materials_out": combine_stats.materials_out,
            }
            if combine_stats is not None else None
        ),
        "wreck_segments": (
            {
                s: {
                    "hull_meshes": wreck_stats.hull_meshes.get(s, 0),
                    "placements": wreck_stats.placements.get(s, 0),
                }
                for s in wreck_stats.sections
            }
            if wreck_stats is not None else None
        ),
        "pivot_rig": (
            {
                "armatures": pivot_stats.armatures,
                "bone_pivots": pivot_stats.bone_pivots,
                "meshes_split": pivot_stats.meshes_split,
                "pieces": pivot_stats.pieces,
                "attachments_rehung": pivot_stats.attachments_rehung,
            }
            if pivot_stats is not None else None
        ),
        "prep": {
            "materials":  counts.materials,
            "base_color": counts.base_color,
            "mr_split":   counts.mr_split,
            "alpha":      counts.alpha,
            "baked":      counts.baked,
        },
        "warnings": [*result.warnings, *counts.notes],
    })
    print(f"WOWS_FBX_OK {args.out} {size}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
