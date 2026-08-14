"""Locate Blender and drive it headlessly for FBX export.

The publisher runs in a normal Python interpreter (it needs Pillow for
the DDS->PNG pass), while the scene assembly needs ``bpy``. Rather than
require a ``bpy`` wheel that matches the user's Blender, we shell out to
the Blender they already have and run
:mod:`wows_blender.headless` inside it.

Discovery order for the executable:

1. an explicit path (``--blender``)
2. ``$WOWS_BLENDER_EXE``
3. ``blender`` on ``PATH``
4. platform install globs — including Steam libraries on every drive,
   since Steam is a common way to get Blender on Windows
"""
from __future__ import annotations

import json
import os
import shutil
import string
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

#: Emitted by the headless driver on success; the run is only believed
#: when this shows up on stdout. Blender's own exit code is not a
#: reliable failure signal for ``--python`` scripts.
_OK_SENTINEL = "WOWS_FBX_OK"
_SKINS_SENTINEL = "WOWS_SKINS_JSON"


class BlenderNotFound(RuntimeError):
    """No Blender executable could be located."""


@dataclass
class FbxResult:
    """Outcome of one headless export."""

    ok:       bool
    fbx_path: Path | None
    size:     int
    stdout:   str
    stderr:   str
    returncode: int
    detail:   dict = field(default_factory=dict)

    @property
    def warnings(self) -> list[str]:
        return list(self.detail.get("warnings") or ())


def _windows_candidates() -> list[Path]:
    out: list[Path] = []
    for base in (
        Path(r"C:\Program Files\Blender Foundation"),
        Path(r"C:\Program Files (x86)\Blender Foundation"),
    ):
        if base.is_dir():
            out.extend(sorted(base.glob("Blender*/blender.exe"), reverse=True))
    # Steam installs Blender flat under steamapps/common/Blender. Steam
    # libraries live on arbitrary drives, so sweep them.
    for drive in string.ascii_uppercase:
        root = Path(f"{drive}:\\")
        if not root.exists():
            continue
        for lib in ("SteamLibrary", r"Program Files (x86)\Steam"):
            exe = root / lib / "steamapps" / "common" / "Blender" / "blender.exe"
            if exe.is_file():
                out.append(exe)
    return out


def _posix_candidates() -> list[Path]:
    out: list[Path] = []
    if sys.platform == "darwin":
        base = Path("/Applications")
        out.extend(sorted(base.glob("Blender*.app/Contents/MacOS/Blender"), reverse=True))
    for p in (
        Path("/usr/bin/blender"),
        Path("/usr/local/bin/blender"),
        Path("/snap/bin/blender"),
        Path.home() / ".local/bin/blender",
    ):
        if p.is_file():
            out.append(p)
    return out


def find_blender(explicit: str | Path | None = None) -> Path:
    """Resolve the Blender executable, or raise :class:`BlenderNotFound`."""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise BlenderNotFound(f"--blender path does not exist: {p}")

    env = os.environ.get("WOWS_BLENDER_EXE")
    if env:
        p = Path(env)
        if p.is_file():
            return p
        raise BlenderNotFound(f"$WOWS_BLENDER_EXE does not exist: {p}")

    which = shutil.which("blender")
    if which:
        return Path(which)

    candidates = _windows_candidates() if os.name == "nt" else _posix_candidates()
    for c in candidates:
        if c.is_file():
            return c

    raise BlenderNotFound(
        "could not find Blender. Pass --blender <path to blender executable>, "
        "set $WOWS_BLENDER_EXE, or put blender on PATH."
    )


def _headless_script() -> Path:
    """Path to the in-Blender driver script.

    Resolved from the installed ``wows_blender`` package so a pip install
    and a source checkout both work.
    """
    import wows_blender

    return Path(wows_blender.__file__).resolve().parent / "headless.py"


def _read_result(path: Path) -> dict:
    """Read the driver's result file, tolerating a run that never wrote one."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _run(blender: Path, script_args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    cmd = [
        str(blender),
        "--background",
        "--factory-startup",
        "--python", str(_headless_script()),
        "--",
        *script_args,
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def export_fbx(
    sidecar: Path,
    out_fbx: Path,
    *,
    blender: str | Path | None = None,
    skin: str = "default",
    library_root: str | Path | None = None,
    accessories: bool = True,
    materials: bool = True,
    lod: str = "lod0",
    damage_variants: bool = False,
    overlays: bool = False,
    bake: bool = False,
    bake_size: int = 2048,
    axis_up: str = "Y",
    axis_forward: str = "-Z",
    scale: float = 1.0,
    embed_textures: bool = False,
    save_blend: Path | None = None,
    timeout: int = 3600,
) -> FbxResult:
    """Assemble ``sidecar``'s ship in Blender and write ``out_fbx``."""
    exe = find_blender(blender)

    script_args = [
        "--sidecar", str(sidecar),
        "--out", str(out_fbx),
        "--skin", skin,
        # `--opt=value` form, not `--opt value`: the default forward axis
        # is "-Z", which argparse would otherwise read as an option flag
        # and reject with "expected one argument".
        f"--axis-up={axis_up}",
        f"--axis-forward={axis_forward}",
        "--scale", str(scale),
        "--bake-size", str(bake_size),
        f"--lod={lod}",
    ]
    if library_root:
        script_args += ["--library", str(library_root)]
    if not accessories:
        script_args.append("--no-accessories")
    if not materials:
        script_args.append("--no-materials")
    if damage_variants:
        script_args.append("--damage-variants")
    if overlays:
        script_args.append("--overlays")
    if bake:
        script_args.append("--bake")
    if embed_textures:
        script_args.append("--embed-textures")
    if save_blend:
        script_args += ["--save-blend", str(save_blend)]

    with tempfile.TemporaryDirectory(prefix="wows-fbx-") as tmp:
        result_json = Path(tmp) / "result.json"
        proc = _run(
            exe, [*script_args, "--result-json", str(result_json)], timeout=timeout,
        )
        detail = _read_result(result_json)

    ok = bool(detail.get("ok"))
    size = int(detail.get("size") or 0)
    if not detail:
        # Fallback: the driver died before writing the result file, or an
        # older driver is installed. Scrape the sentinel instead.
        for line in proc.stdout.splitlines():
            if line.startswith(_OK_SENTINEL):
                ok = True
                if out_fbx.is_file():
                    size = out_fbx.stat().st_size

    return FbxResult(
        ok=ok,
        fbx_path=out_fbx if ok else None,
        size=size,
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
        detail=detail,
    )


def list_skins(
    sidecar: Path,
    *,
    blender: str | Path | None = None,
    timeout: int = 300,
) -> list[dict]:
    """Read the sidecar's ``skins[]`` through Blender.

    Goes through Blender purely so there is one code path for parsing —
    the reader itself is stdlib and would work here, but keeping the
    single entry point avoids the two drifting.
    """
    exe = find_blender(blender)
    with tempfile.TemporaryDirectory(prefix="wows-skins-") as tmp:
        result_json = Path(tmp) / "result.json"
        proc = _run(
            exe,
            ["--sidecar", str(sidecar), "--list-skins",
             "--result-json", str(result_json)],
            timeout=timeout,
        )
        detail = _read_result(result_json)

    if "skins" in detail:
        return detail["skins"]

    # Fallback for an older driver. Blender glues its version banner onto
    # the tail of the line, so slice to the last ']' rather than trusting
    # the line boundary.
    for line in proc.stdout.splitlines():
        if line.startswith(_SKINS_SENTINEL):
            body = line[len(_SKINS_SENTINEL):].strip()
            end = body.rfind("]")
            if end >= 0:
                try:
                    return json.loads(body[: end + 1])
                except ValueError:
                    pass
    return []


__all__ = [
    "BlenderNotFound",
    "FbxResult",
    "find_blender",
    "export_fbx",
    "list_skins",
]
