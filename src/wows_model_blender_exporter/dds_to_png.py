"""DDS → PNG conversion pass for the Blender consumer.

Blender's bundled image loader does not recognize the ``.dd0`` / ``.dd1``
/ ``.dd2`` file extensions the WoWS pipeline emits, and even for stock
``.dds`` files the loader's coverage of compressed BC formats is
limited across Blender versions. Convert once at publish time using
Pillow's DDS reader; the resulting PNGs sit next to the DDS files
under ``textures_dds/`` and the Blender add-on prefers PNG over DDS
when both are present.

Conversion rules:

* Only the highest-resolution mip is converted — ``<stem>.dd0`` if it
  exists, else ``<stem>.dds``. Blender will generate its own mip
  pyramid at render time; shipping all four mips as PNG would 4× the
  texture payload for no benefit.
* Output filename mirrors the input stem: ``foo.dd0`` → ``foo.png``,
  ``foo.dds`` → ``foo.png``. Collisions (``foo.dd0`` and ``foo.dds``
  both present) prefer the ``.dd0`` source — it is the highest mip
  in the WG mip chain.
* Idempotent — mtime-compared; if the PNG is newer than the source
  DDS it is left alone. Force a rebuild with ``force=True``.
* Bit-exact would require RGBA mode; we use Pillow's native decode
  which gives RGBA for BC1/BC3/BC7 and LA for BC5 (R/G channels in
  the alpha plane is a known Pillow quirk — we normalize to RGBA
  here for downstream simplicity).

Why not embed a pure-Python BC decoder in the Blender add-on? It would
work without an extra publish step, but BC7 in particular is ~200×
slower in pure Python than Pillow's C decoder; converting Baltimore
(~2k textures) would take 20+ minutes in-Blender vs ~20 s at publish
time. The add-on stays stdlib-only this way too.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

# Extensions that participate in the DDS family. Order matters: when
# multiple variants share a stem, the first match wins. ``.dd0`` is the
# uncropped full-res mip in the WoWS pipeline; ``.dds`` is the legacy
# mip chain. Other consumers (Unity) read all four; Blender only needs
# the top mip.
_DDS_SOURCE_PRIORITY: tuple[str, ...] = (".dd0", ".dds")

# Files we never try to convert. The pipeline emits the lower mips as
# .dd1 / .dd2 — keep them on disk for the Unity consumer but skip them
# here. ``.png`` is our own output.
_SKIP_EXTENSIONS: frozenset[str] = frozenset((".dd1", ".dd2", ".png"))


@dataclass(frozen=True)
class ConvertCounts:
    """Per-tree counts for one :func:`convert_tree` run."""

    converted: int = 0
    skipped:   int = 0  # up-to-date by mtime compare
    failed:    int = 0


def _find_source(stem: Path) -> Path | None:
    """Pick the highest-priority DDS source for ``stem``.

    ``stem`` is the path WITHOUT extension — e.g.
    ``.../textures_dds/foo``. We probe each entry in
    :data:`_DDS_SOURCE_PRIORITY` and return the first that exists.
    """
    for ext in _DDS_SOURCE_PRIORITY:
        candidate = stem.with_suffix(ext)
        if candidate.is_file():
            return candidate
    return None


def _png_up_to_date(src: Path, dst: Path) -> bool:
    """True if ``dst`` exists and is newer than ``src``.

    The mtime compare keeps re-publishes cheap without a content
    hash; the only failure mode is a clock-skewed filesystem, which
    is rare on a single-machine Blender workflow.
    """
    if not dst.exists():
        return False
    try:
        return dst.stat().st_mtime >= src.stat().st_mtime
    except OSError:
        return False


def _convert_one(src: Path, dst: Path) -> bool:
    """Decode one DDS into ``dst`` as PNG.

    Returns ``True`` on success, ``False`` on a decode failure (logged
    at WARNING level and reported in the counts; we don't abort the
    whole tree on one bad texture).
    """
    try:
        with Image.open(src) as im:
            # Normalize to RGBA — Pillow returns LA / RGB / RGBA
            # depending on the BC format; PNG writers handle all of
            # them but Blender's loader is happiest with RGBA.
            if im.mode != "RGBA":
                im = im.convert("RGBA")
            dst.parent.mkdir(parents=True, exist_ok=True)
            im.save(dst, format="PNG", optimize=False, compress_level=1)
        return True
    except Exception as e:  # noqa: BLE001 — bubble up only via the count
        logger.warning("convert failed: %s -> %s: %s", src.name, dst.name, e)
        return False


def _iter_dds_stems(root: Path) -> Iterable[Path]:
    """Yield the stem (extensionless path) of every convertible DDS in
    ``root`` and its descendants. Deduplicates when both ``.dd0`` and
    ``.dds`` exist for the same texture.
    """
    seen: set[Path] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext in _SKIP_EXTENSIONS:
            continue
        if ext not in _DDS_SOURCE_PRIORITY:
            continue
        stem = path.with_suffix("")
        if stem in seen:
            continue
        seen.add(stem)
        yield stem


def convert_tree(
    root: Path,
    *,
    force: bool = False,
) -> ConvertCounts:
    """Walk ``root`` recursively and emit a PNG sibling for every DDS.

    The PNG lands next to the source — same directory, same stem, with
    a ``.png`` extension. Idempotent unless ``force=True``.

    Used by the Blender publisher after the file-copy phase. The
    publisher passes ``root`` = the published destination directory so
    the conversion runs once per publish, not on every Blender import.
    """
    converted = 0
    skipped = 0
    failed = 0

    for stem in _iter_dds_stems(root):
        src = _find_source(stem)
        if src is None:
            continue
        dst = stem.with_suffix(".png")
        if not force and _png_up_to_date(src, dst):
            skipped += 1
            continue
        if _convert_one(src, dst):
            converted += 1
        else:
            failed += 1

    return ConvertCounts(converted=converted, skipped=skipped, failed=failed)


__all__ = ["ConvertCounts", "convert_tree"]
