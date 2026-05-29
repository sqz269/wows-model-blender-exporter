"""``wows-export-blender`` — publish + DDS→PNG for Blender consumers.

Two-phase publish:

1. **Copy** — same idempotent ship + library copy as
   :func:`wows_model_export.compose.publish.publish`, into a
   Blender-friendly destination directory.
2. **Convert** — walk the destination's DDS files and emit PNG
   siblings via :mod:`wows_model_blender_exporter.dds_to_png`. The
   Blender add-on prefers PNG over DDS when loading textures, so
   this is the step that makes the published artifacts directly
   importable without any in-Blender DDS dependency.

Argv shape mirrors ``wows-export-unity`` for muscle-memory parity:

    wows-export-blender BA_Montana
    wows-export-blender BA_Montana --accessories
    wows-export-blender --all
    wows-export-blender --all --dest "J:/Blender/Ships"
    wows-export-blender --all --force                 # rebuild every PNG too
    wows-export-blender BA_Montana --no-convert       # skip the DDS pass
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

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
        return EXIT_OK

    try:
        convert_counts = convert_tree(args.dest, force=args.force)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(f"\nDDS conversion failed: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_UNEXPECTED

    print(f"DDS→PNG  {_summarize_convert(convert_counts)}", file=sys.stderr)
    return EXIT_OK


__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())
