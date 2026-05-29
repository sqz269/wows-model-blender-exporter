"""Entry-point hook for the wows-model-export webview consumer dispatch.

Registered in ``pyproject.toml`` as::

    [project.entry-points."wows_model_export.consumers"]
    blender = "wows_model_blender_exporter.consumer:descriptor"

The webview's ``GET /api/consumers`` discovers this descriptor and
renders a "Blender" card. The handler runs the same two-phase publish
(``compose.publish.publish`` + DDS→PNG conversion) the standalone
``wows-export-blender`` CLI does — see ``cli/export_blender.py``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from wows_model_export.compose.publish import publish
from wows_model_export.extensions import (
    ConsumerAction,
    ConsumerDescriptor,
    ConsumerParam,
)

from .cli.export_blender import _DEFAULT_DEST
from .dds_to_png import convert_tree


def _publish(
    *,
    config: Any,
    ships: list[str] | None = None,
    all: bool = False,
    accessories: bool = False,
    projectiles: bool = False,
    force: bool = False,
    no_convert: bool = False,
    on_event: Any = None,
    cancel: Any = None,
) -> dict[str, Any]:
    """Publish + DDS→PNG handler. Mirrors ``cli/export_blender.py:main``."""
    ships = ships or []
    if all:
        domains: tuple[str, ...] = ("ships", "library", "projectiles", "decals")
        only_ships: tuple[str, ...] | None = None
    else:
        chosen: list[str] = []
        if ships:
            chosen.append("ships")
        if accessories:
            chosen.extend(["library", "decals"])
        if projectiles:
            chosen.append("projectiles")
        if not chosen:
            raise ValueError(
                "pick at least one of: ships (list), all, accessories, projectiles"
            )
        domains = tuple(chosen)
        only_ships = tuple(ships) if ships else None

    publish_result = publish(
        target_dir=_DEFAULT_DEST,
        workspace=config.workspace,
        config=config,
        only_ships=only_ships,
        domains=domains,
        force=force,
        on_event=on_event,
        cancel=cancel,
    )

    # DDS→PNG conversion is the Blender-specific extra step. The CLI
    # gates it behind --no-convert; mirror that here. convert_tree
    # doesn't take on_event today — when it grows one, wire it through.
    convert_summary: dict[str, int] | None = None
    if not no_convert:
        counts = convert_tree(_DEFAULT_DEST, force=force)
        convert_summary = {
            "converted": counts.converted,
            "skipped":   counts.skipped,
            "failed":    counts.failed,
        }

    return {
        "publish": publish_result,
        "convert": convert_summary,
    }


def _resolve_blender_exe(explicit: str) -> Path:
    """Find the Blender executable.

    Resolution order: explicit arg > ``WOWS_BLENDER_EXE`` env > PATH
    lookup for ``blender``. Raises ``FileNotFoundError`` with all three
    sources echoed when nothing resolves — debugging a missing Blender
    install is annoying enough without having to guess what was tried.
    """
    candidates: list[str | None] = [
        explicit or None,
        os.environ.get("WOWS_BLENDER_EXE"),
        shutil.which("blender"),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    raise FileNotFoundError(
        "blender executable not found. Tried: "
        f"arg={explicit!r}, "
        f"$WOWS_BLENDER_EXE={os.environ.get('WOWS_BLENDER_EXE')!r}, "
        f"PATH lookup for 'blender'. Set $WOWS_BLENDER_EXE or pass an "
        "absolute path."
    )


def _open_ship(
    *,
    config: Any,
    ship: list[str] | None = None,
    blender_exe: str = "",
    on_event: Any = None,
    cancel: Any = None,
) -> dict[str, Any]:
    """Launch Blender with the wows_blender add-on importing ``ship``.

    Fail-fast: requires exactly one ship, a resolvable blender.exe, and
    the ship already published to ``WOWS_BLENDER_LIBRARY``. Returns
    immediately after :func:`subprocess.Popen` succeeds — Blender keeps
    running independently of the webview; the job terminates as soon as
    the spawn succeeds.

    The add-on's ``wows.import_ship`` operator is an ``ImportHelper``
    keyed on the sidecar JSON, so we point ``filepath`` at
    ``<published>/<ship>/<ship>.meta.json``. The startup script enables
    the add-on first — idempotent if the user pre-enabled it.
    """
    ship = ship or []
    if len(ship) != 1:
        raise ValueError(
            f"open_ship expects exactly one ship, got {len(ship)}: {ship!r}"
        )
    name = ship[0]
    exe = _resolve_blender_exe(blender_exe)

    # Published-ship layout: <_DEFAULT_DEST>/<ship>/<ship>.meta.json.
    ship_dir = _DEFAULT_DEST / name
    sidecar = ship_dir / f"{name}.meta.json"
    if not sidecar.is_file():
        raise FileNotFoundError(
            f"sidecar not found at {sidecar}. Run the Publish action "
            f"first (or `wows-export-blender {name}`)."
        )

    # `repr()` quotes the path safely for embedding inside the Python
    # source we hand Blender via --python-expr (handles backslashes on
    # Windows + quote-escapes uniformly across platforms).
    script = (
        "import bpy\n"
        "try:\n"
        "    bpy.ops.preferences.addon_enable(module='wows_blender')\n"
        "except RuntimeError as e:\n"
        "    raise SystemExit("
        "f'wows_blender add-on not installed: {e}. "
        "Install the ZIP built by wows-pack-blender-addon via "
        "Edit > Preferences > Add-ons > Install.')\n"
        f"bpy.ops.wows.import_ship(filepath={str(sidecar)!r})\n"
    )

    # Detach so Blender outlives this job. DEVNULL on the three streams
    # keeps the child from inheriting our open files / sockets — without
    # this the webview's uvicorn socket would survive in Blender's fd
    # table on POSIX, blocking port reuse on a server restart.
    proc = subprocess.Popen(
        [str(exe), "--python-expr", script],
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return {
        "launched": str(exe),
        "ship":     name,
        "sidecar":  str(sidecar),
        "pid":      proc.pid,
    }


def descriptor() -> ConsumerDescriptor:
    return ConsumerDescriptor(
        id="blender",
        display_name="Blender",
        description=f"Publish + DDS→PNG to {_DEFAULT_DEST}.",
        actions=(
            ConsumerAction(
                id="publish",
                label="Publish to Blender",
                description=(
                    "Copy producer artifacts to the Blender destination "
                    "folder, then emit PNG siblings next to every DDS so "
                    "the add-on's image loader can pick them up without "
                    "a DDS dependency."
                ),
                params=(
                    ConsumerParam(
                        id="ships",
                        label="Ships",
                        kind="ships_picker",
                        default=[],
                        description="Optional. Empty + --all = every ship.",
                    ),
                    ConsumerParam(
                        id="all",
                        label="Publish everything",
                        kind="bool",
                        default=False,
                        description="Every ship + all four shared libraries.",
                    ),
                    ConsumerParam(
                        id="accessories",
                        label="Include accessories library",
                        kind="bool",
                        default=False,
                        description="Ride-alongs: camo_masks + camo_mat.",
                    ),
                    ConsumerParam(
                        id="projectiles",
                        label="Include projectiles library",
                        kind="bool",
                        default=False,
                    ),
                    ConsumerParam(
                        id="force",
                        label="Force re-copy + re-convert",
                        kind="bool",
                        default=False,
                        description="Ignore mtime+size compare for copy AND PNG conversion.",
                    ),
                    ConsumerParam(
                        id="no_convert",
                        label="Skip DDS→PNG conversion",
                        kind="bool",
                        default=False,
                        description="Refresh sidecars + GLBs only; leave PNGs untouched.",
                    ),
                ),
                handler=_publish,
            ),
            ConsumerAction(
                id="open_ship",
                label="Open ship in Blender",
                description=(
                    "Launch Blender and import a single published ship via "
                    "the wows_blender add-on. Detached — Blender keeps "
                    "running after the job completes. Publish the ship "
                    "first; this action does not auto-publish."
                ),
                params=(
                    ConsumerParam(
                        id="ship",
                        label="Ship",
                        kind="ships_picker",
                        default=[],
                        description="Pick exactly one ship.",
                    ),
                    ConsumerParam(
                        id="blender_exe",
                        label="Blender executable (optional)",
                        kind="string",
                        default="",
                        description=(
                            "Defaults to $WOWS_BLENDER_EXE or `blender` on PATH."
                        ),
                    ),
                ),
                handler=_open_ship,
            ),
        ),
    )


__all__ = ["descriptor"]
