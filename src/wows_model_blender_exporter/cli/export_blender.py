"""``wows-export-blender`` — publish + DDS→PNG for Blender consumers.

Three phases, the last opt-in:

1. **Copy** — same idempotent ship + library copy as
   :func:`wows_model_export.compose.publish.publish`, into a
   Blender-friendly destination directory.
2. **Convert** — walk the destination's DDS files and emit PNG
   siblings via :mod:`wows_model_blender_exporter.dds_to_png`. The
   Blender add-on prefers PNG over DDS when loading textures, so
   this is the step that makes the published artifacts directly
   importable without any in-Blender DDS dependency.
3. **FBX** (``--fbx``) — drive a headless Blender to assemble the
   published ship and write ``.fbx`` + textures + a material manifest.
   See :mod:`wows_model_blender_exporter.blender_runner`.

Argv shape mirrors ``wows-export-unity`` for muscle-memory parity:

    wows-export-blender BA_Montana
    wows-export-blender BA_Montana --accessories
    wows-export-blender --all
    wows-export-blender --all --dest "J:/Blender/Ships"
    wows-export-blender --all --force                 # rebuild every PNG too
    wows-export-blender BA_Montana --no-convert       # skip the DDS pass

    # FBX with textures + placed accessories:
    wows-export-blender BA_Montana --accessories --fbx

    # A camo scheme, flattened so it survives FBX's Phong materials:
    wows-export-blender BA_Montana --accessories --fbx \\
        --skin camo_permanent_1__USNP01 --bake

    wows-export-blender BA_Montana --list-skins       # what can --skin take?
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

from wows_model_export.compose.publish import publish
from wows_model_export.errors import ConfigError, StepError, ToolkitError
from wows_model_export.types import PublishResult
from wows_model_export.cli._args import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_STEP_ERROR,
    EXIT_UNEXPECTED,
    add_common_args,
    build_printer,
    resolve_config,
)

from ..dds_to_png import ConvertCounts, convert_tree

# Default destination is intentionally generic — Blender does not have a
# canonical "asset folder" the way Unity has Assets/. Users typically
# park published ships under a project-specific directory and point the
# Blender add-on at it. The env var name mirrors the Unity exporter's
# WOWS_UNITY_PIPELINE for symmetry.
_DEFAULT_DEST: Path = Path(
    os.environ.get(
        "WOWS_BLENDER_LIBRARY",
        r"J:\BlenderShips",
    )
)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wows-export-blender",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "ships",
        nargs="*",
        help="Ship names to publish (e.g. BA_Montana). When empty, at "
             "least one of --all / --accessories / --projectiles must "
             "be set.",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="Publish every ship that has a sidecar, plus all four "
             "shared libraries.",
    )
    ap.add_argument(
        "--accessories",
        action="store_true",
        help="Also publish the shared accessories/ library "
             "(camo_masks/ + camo_mat/ ride along under 'decals').",
    )
    ap.add_argument(
        "--projectiles",
        action="store_true",
        help="Also publish the shared projectiles/ library.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Copy every file AND re-convert every DDS regardless of "
             "mtime comparisons.",
    )
    ap.add_argument(
        "--dest",
        type=Path,
        default=_DEFAULT_DEST,
        dest="dest",
        help=f"Blender-side destination root (default: {_DEFAULT_DEST}; "
             f"override via $WOWS_BLENDER_LIBRARY).",
    )
    ap.add_argument(
        "--no-convert",
        action="store_true",
        help="Skip the DDS→PNG conversion pass. Useful when you only "
             "want to refresh JSON sidecars or are running the "
             "conversion separately.",
    )

    fbx = ap.add_argument_group("FBX export (headless Blender)")
    fbx.add_argument(
        "--fbx",
        action="store_true",
        help="After publishing, drive Blender headlessly to assemble each "
             "ship and write <dest>/fbx/<Ship>.fbx with copied textures "
             "and a <Ship>.materials.json manifest.",
    )
    fbx.add_argument(
        "--fbx-out",
        type=Path,
        default=None,
        help="Directory for the .fbx files (default: <dest>/fbx).",
    )
    fbx.add_argument(
        "--blender",
        default=None,
        help="Path to the Blender executable. Falls back to "
             "$WOWS_BLENDER_EXE, then PATH, then the usual install "
             "locations (including Steam libraries).",
    )
    fbx.add_argument(
        "--skin",
        default="default",
        help="Camo / permoflage skin_id to apply (default: the bare ship). "
             "Use --list-skins to see what a ship offers.",
    )
    fbx.add_argument(
        "--list-skins",
        action="store_true",
        help="Print the skins available for the named ship(s) and exit. "
             "Reads the published sidecar; no export is run.",
    )
    fbx.add_argument(
        "--bake",
        action="store_true",
        help="Bake each material's evaluated base colour to a PNG before "
             "export. Required for camo to survive: FBX materials cannot "
             "express the Path-A palette lerp. Costs the PBR separation "
             "(ambient occlusion and paint are burned into the albedo).",
    )
    fbx.add_argument(
        "--bake-size",
        type=int,
        default=2048,
        help="Bake resolution per material (default: 2048).",
    )
    fbx.add_argument(
        "--embed-textures",
        action="store_true",
        help="Pack textures inside the .fbx instead of copying them "
             "alongside it. Larger file, single artifact.",
    )
    fbx.add_argument(
        "--fbx-no-accessories",
        action="store_true",
        help="Export the hull only — skip turrets, secondaries, AA, "
             "torpedoes and decoratives.",
    )
    fbx.add_argument(
        "--lod",
        default="lod0",
        help="Which LOD survives: 'lod0' (default, the high-detail ship), "
             "'lodN' for only level N, or 'all' to keep every level. A hull "
             "GLB ships its coarser substitutes alongside the real mesh, so "
             "'all' gives you overlapping copies.",
    )
    fbx.add_argument(
        "--damage-variants",
        action="store_true",
        help="Keep the crack / patch damage-state meshes. They sit on top "
             "of the intact geometry, so this is off by default.",
    )
    fbx.add_argument(
        "--overlays",
        action="store_true",
        help="Keep the Armor / Hitboxes collision volumes — solid shells "
             "around the ship, useful only for inspection.",
    )
    fbx.add_argument(
        "--axis-up",
        default="Y",
        help="FBX up axis (default: Y — what Maya / Unity / Unreal expect).",
    )
    fbx.add_argument(
        "--axis-forward",
        default="-Z",
        help="FBX forward axis (default: -Z). Pass a negative axis with an "
             "equals sign — `--axis-forward=-X` — or argparse reads the "
             "value as another flag.",
    )
    fbx.add_argument(
        "--fbx-scale",
        type=float,
        default=1.0,
        help="Global scale applied on FBX export (default: 1.0).",
    )
    fbx.add_argument(
        "--save-blend",
        action="store_true",
        help="Also save the assembled scene as <Ship>.blend next to the FBX "
             "(useful for inspecting what was exported).",
    )

    add_common_args(ap)
    return ap


def _resolve_domains(args: argparse.Namespace) -> tuple[str, ...]:
    if args.all:
        return ("ships", "library", "projectiles", "decals")
    domains: list[str] = []
    if args.ships:
        domains.append("ships")
    if args.accessories:
        domains.extend(["library", "decals"])
    if args.projectiles:
        domains.append("projectiles")
    return tuple(domains)


def _summarize_publish(result: PublishResult) -> str:
    bits = [f"published -> {result.target_dir}"]
    for name in ("ships", "library", "projectiles", "decals"):
        counts = getattr(result, name)
        if counts.copied == 0 and counts.skipped == 0 and counts.deleted == 0:
            continue
        bits.append(
            f"{name}=copied:{counts.copied}/skipped:{counts.skipped}"
            + (f"/deleted:{counts.deleted}" if counts.deleted else "")
        )
    if result.warnings:
        bits.append(f"warnings={len(result.warnings)}")
    return "  ".join(bits)


def _summarize_convert(counts: ConvertCounts) -> str:
    bits = [
        f"converted:{counts.converted}",
        f"skipped:{counts.skipped}",
    ]
    if counts.failed:
        bits.append(f"failed:{counts.failed}")
    return "  ".join(bits)


def _sidecar_for(ship: str, dest: Path, workspace: Path) -> Path | None:
    """Locate a ship's sidecar — published copy first, workspace second.

    ``--list-skins`` is useful before anything has been published, so it
    falls back to the producer's own output directory.
    """
    for cand in (
        dest / ship / f"{ship}.meta.json",
        workspace / "ships" / ship / f"{ship}.meta.json",
    ):
        if cand.is_file():
            return cand
    return None


def _published_ships(dest: Path, only: tuple[str, ...] | None) -> list[tuple[str, Path]]:
    """``(ship_name, sidecar_path)`` for every published ship under ``dest``."""
    out: list[tuple[str, Path]] = []
    if not dest.is_dir():
        return out
    for child in sorted(dest.iterdir()):
        if not child.is_dir():
            continue
        if only is not None and child.name not in only:
            continue
        sidecar = child / f"{child.name}.meta.json"
        if sidecar.is_file():
            out.append((child.name, sidecar))
    return out


def _run_list_skins(args: argparse.Namespace, dest: Path, workspace: Path) -> int:
    from ..blender_runner import BlenderNotFound, list_skins

    ships = tuple(args.ships)
    if not ships:
        ships = tuple(name for name, _ in _published_ships(dest, None))
    if not ships:
        print("no ships named and none published under "
              f"{dest} — name a ship or publish first", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    rc = EXIT_OK
    for ship in ships:
        sidecar = _sidecar_for(ship, dest, workspace)
        if sidecar is None:
            print(f"{ship}: no sidecar found (looked in {dest} and {workspace})",
                  file=sys.stderr)
            rc = EXIT_STEP_ERROR
            continue
        try:
            skins = list_skins(sidecar, blender=args.blender)
        except BlenderNotFound as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        print(f"\n{ship} — {len(skins)} skin(s):")
        for s in skins:
            path = s.get("path") or "none"
            label = s.get("display_name") or s.get("skin_id")
            print(f"  {s.get('skin_id',''):<44} path {path:<4} {label}")
    return rc


def _run_fbx(args: argparse.Namespace, dest: Path) -> int:
    from ..blender_runner import BlenderNotFound, export_fbx, find_blender

    only = tuple(args.ships) if args.ships and not args.all else None
    ships = _published_ships(dest, only)
    if not ships:
        print(f"--fbx: no published ships under {dest}", file=sys.stderr)
        return EXIT_STEP_ERROR

    try:
        exe = find_blender(args.blender)
    except BlenderNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    print(f"\nFBX via {exe}", file=sys.stderr)

    out_dir = args.fbx_out or (dest / "fbx")
    rc = EXIT_OK
    for ship, sidecar in ships:
        out_fbx = out_dir / f"{ship}.fbx"
        print(f"  {ship} -> {out_fbx}", file=sys.stderr)
        result = export_fbx(
            sidecar,
            out_fbx,
            blender=exe,
            skin=args.skin,
            accessories=not args.fbx_no_accessories,
            lod=args.lod,
            damage_variants=args.damage_variants,
            overlays=args.overlays,
            bake=args.bake,
            bake_size=args.bake_size,
            axis_up=args.axis_up,
            axis_forward=args.axis_forward,
            scale=args.fbx_scale,
            embed_textures=args.embed_textures,
            save_blend=(out_dir / f"{ship}.blend") if args.save_blend else None,
        )
        if result.ok:
            mb = result.size / (1024 * 1024)
            d = result.detail
            extra = ""
            if d:
                extra = (f" — {d.get('meshes_kept', '?')} meshes, "
                         f"{d.get('placed', 0)} placements, "
                         f"{d.get('slots_bound', '?')} texture slots")
                if d.get("camo_applied"):
                    extra += f", camo on {d['camo_applied']} materials"
            print(f"    ok — {mb:.1f} MB{extra}", file=sys.stderr)
            for w in result.warnings:
                print(f"    warn: {w}", file=sys.stderr)
        else:
            rc = EXIT_STEP_ERROR
            print(f"    FAILED (exit {result.returncode})", file=sys.stderr)
            tail = [ln for ln in result.stderr.splitlines() if ln.strip()][-12:]
            for ln in tail:
                print(f"      {ln}", file=sys.stderr)
    return rc


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list_skins:
        try:
            cfg = resolve_config(args)
        except ConfigError as e:
            print(f"config error: {e}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        return _run_list_skins(args, args.dest, cfg.workspace)

    domains = _resolve_domains(args)
    if not domains:
        print(
            "nothing to publish — name a ship, use --all, --accessories, "
            "or --projectiles",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR

    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    printer = build_printer(args)

    only_ships: tuple[str, ...] | None
    if args.all:
        only_ships = None
    else:
        only_ships = tuple(args.ships) if args.ships else None

    try:
        publish_result = publish(
            target_dir=args.dest,
            workspace=cfg.workspace,
            config=cfg,
            only_ships=only_ships,
            domains=domains,
            force=args.force,
            on_event=printer,
        )
    except StepError as e:
        print(f"\nerror: step {e.step!r} failed: {e.detail or e}", file=sys.stderr)
        return EXIT_STEP_ERROR
    except (ConfigError, ToolkitError) as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(f"\nunexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_UNEXPECTED

    print(_summarize_publish(publish_result), file=sys.stderr)
    for warn in publish_result.warnings:
        print(f"  warn: {warn}", file=sys.stderr)

    if args.no_convert:
        print("(--no-convert: skipping DDS→PNG pass)", file=sys.stderr)
    else:
        try:
            convert_counts = convert_tree(args.dest, force=args.force)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            print(f"\nDDS conversion failed: {type(e).__name__}: {e}", file=sys.stderr)
            return EXIT_UNEXPECTED

        print(f"DDS→PNG  {_summarize_convert(convert_counts)}", file=sys.stderr)

    if args.fbx:
        return _run_fbx(args, args.dest)

    return EXIT_OK


__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())
