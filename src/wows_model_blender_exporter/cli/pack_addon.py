"""``wows-pack-blender-addon`` — bundle ``src/wows_blender/`` into a ZIP.

Blender add-ons are installed via ``Edit > Preferences > Add-ons >
Install...`` which expects a single ``.zip`` whose contents unpack
into ``<blender_user_dir>/scripts/addons/wows_blender/``. This CLI
walks the in-tree ``src/wows_blender/`` directory, drops the
``__pycache__`` cruft, and writes the ZIP.

Default output: ``<repo_root>/dist/wows_blender-<version>.zip``.
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

from .. import __version__


def _repo_root() -> Path:
    """Walk up from this file to the repo root.

    Layout: ``<repo>/src/wows_model_blender_exporter/cli/pack_addon.py``,
    so the repo root is four parents up.
    """
    return Path(__file__).resolve().parents[3]


def _addon_source(repo_root: Path) -> Path:
    """Path to the in-tree add-on source directory."""
    src = repo_root / "src" / "wows_blender"
    if not src.is_dir():
        raise SystemExit(
            f"add-on source not found at {src}. Are you running from a "
            f"checkout? The packaging CLI needs the in-tree "
            f"`src/wows_blender/` directory."
        )
    return src


def _iter_addon_files(src: Path) -> list[Path]:
    """Every file under ``src`` that should ship in the ZIP.

    Drops ``__pycache__``, ``.pyc``, and editor cruft. Walks alphabetically
    so the ZIP is reproducible across runs.
    """
    out: list[Path] = []
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        # Skip caches + compiled bytecode + editor swap files.
        if any(part == "__pycache__" for part in rel.parts):
            continue
        if path.suffix in (".pyc", ".pyo", ".swp", ".swo"):
            continue
        if path.name in (".DS_Store",):
            continue
        out.append(path)
    return out


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wows-pack-blender-addon",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Destination ZIP path. Default: "
             "<repo_root>/dist/wows_blender-<version>.zip",
    )
    ap.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Override the add-on source directory. Defaults to the "
             "in-tree src/wows_blender/.",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    repo_root = _repo_root()
    src = args.source or _addon_source(repo_root)
    if not src.is_dir():
        print(f"error: add-on source not a directory: {src}", file=sys.stderr)
        return 1

    output = args.output
    if output is None:
        dist = repo_root / "dist"
        dist.mkdir(exist_ok=True)
        output = dist / f"wows_blender-{__version__}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)

    files = _iter_addon_files(src)
    if not files:
        print(f"error: no files to pack under {src}", file=sys.stderr)
        return 1

    # Blender expects the ZIP root to contain the package directory
    # itself (the install dialog will copy the WHOLE folder, including
    # the directory name, into scripts/addons/). So arcnames are
    # rooted at the package name `wows_blender/...`.
    pkg_name = src.name
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as zf:
        for path in files:
            rel = path.relative_to(src)
            arc = Path(pkg_name) / rel
            zf.write(path, arcname=str(arc).replace("\\", "/"))

    size_kb = output.stat().st_size / 1024
    print(
        f"packed {len(files)} file(s) -> {output} ({size_kb:.1f} KiB)",
        file=sys.stderr,
    )
    return 0


__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())
