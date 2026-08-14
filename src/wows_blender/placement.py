"""Transform math — glTF placements → Blender world transforms.

The pipeline emits 4x4 matrices in glTF convention: column-major
storage, right-handed coords, +Y up, +Z forward. Blender uses
right-handed coords, +Z up, -Y forward.

Two-step conversion:

1. The glTF matrix gets reinterpreted into Blender's matrix layout
   (column-major → mathutils.Matrix is row-major-init; transpose).
2. The matrix is conjugated by ``B = diag(+X, -Z, +Y)`` so it lives
   in Blender's basis.

Mirrors:

* webview ``placement.ts:applyPlacementMatrix`` — straight decompose
  (three.js uses glTF's own basis, no conjugation needed).
* Unity ``ShipPrefabBuilder.cs`` — applies its own gltFast-side X-flip
  during import.

Attached-child rule (post-mul by ``diag(-1, +1, +1)``):

   webview's ``applyAttachedMatrix`` post-multiplies attached
   children by a local-X flip to match gltFast's vertex X-negation
   on import. Blender's stock glTF importer follows the three.js
   convention (no X-negation), so we DO NOT apply that post-mul
   here. The attached_y_flip placements still need correct
   handling per the schema_v6 baked-conjugation rule
   (`project_variant_swap_bone_mismatch.md`): the producer has
   already folded basis conversion into the matrix; consumer
   decomposes verbatim.

This module is pure stdlib + math; the Blender side wraps it with
mathutils.Matrix at call time so the conversion logic stays unit-
testable outside Blender.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

# Change-of-basis matrix between the pipeline's glTF frame and
# Blender's frame. The pipeline emits matrices in BigWorld/WG's
# convention (which the toolkit pins to glTF column-major + the same
# +Y-up basis three.js / gltFast consume natively):
#     +X = right
#     +Y = up
#     -Z = bow / forward
#
# Blender's frame:
#     +X = right
#     +Z = up
#     -Y = forward
#
# The conversion MUST be the same one Blender's stock glTF importer
# applies to the hull/accessory meshes — the proper rotation X+90°
# (det = +1):
#     X_b = +X_g
#     Y_b = -Z_g
#     Z_b = +Y_g
#
# NOT the naive Y↔Z swap (x, z, y). The swap is a reflection
# (det = -1): positions land fore-aft reversed relative to the
# imported hull and every non-0/180° yaw comes out mirrored — front
# turrets on the stern. (A 180° yaw is invariant under the mirrored
# conjugation, which is why relative fore↔aft probes pass under the
# broken swap; only comparison against the hull mesh catches it.)
GLTF_TO_BLENDER_BASIS: tuple[tuple[float, ...], ...] = (
    (1.0, 0.0,  0.0, 0.0),
    (0.0, 0.0, -1.0, 0.0),
    (0.0, 1.0,  0.0, 0.0),
    (0.0, 0.0,  0.0, 1.0),
)

# Inverse of the above (rotation X-90°). A rotation's inverse is its
# transpose — no longer involutive like the old (wrong) swap basis.
BLENDER_TO_GLTF_BASIS: tuple[tuple[float, ...], ...] = (
    (1.0,  0.0, 0.0, 0.0),
    (0.0,  0.0, 1.0, 0.0),
    (0.0, -1.0, 0.0, 0.0),
    (0.0,  0.0, 0.0, 1.0),
)


def column_major_to_row_major(m16: Sequence[float]) -> list[list[float]]:
    """Reshape a 16-float column-major matrix into a 4x4 row-major
    list-of-lists. The glTF convention is column-major storage; mathutils
    .Matrix takes a 4x4 row-major list-of-rows constructor."""
    if len(m16) != 16:
        raise ValueError(f"expected 16 floats, got {len(m16)}")
    cols = [
        m16[0:4],
        m16[4:8],
        m16[8:12],
        m16[12:16],
    ]
    rows = [
        [cols[c][r] for c in range(4)]
        for r in range(4)
    ]
    return rows


def matmul4(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[list[float]]:
    """Multiply two 4x4 row-major matrices."""
    out = [[0.0] * 4 for _ in range(4)]
    for i in range(4):
        for j in range(4):
            out[i][j] = sum(a[i][k] * b[k][j] for k in range(4))
    return out


def gltf_matrix_to_blender_rows(m16: Sequence[float]) -> list[list[float]]:
    """Convert a glTF column-major 16-float matrix into a 4x4 Blender
    row-major matrix.

    Conjugation form: ``M_b = GB · M_g · BG`` where ``GB`` is the
    glTF→Blender rotation (X+90°) and ``BG`` its inverse (X-90°).
    The conjugation rotates the entire transform — translation,
    rotation, and scale — from glTF's +Y-up basis into Blender's
    +Z-up basis, matching what the stock glTF importer does to the
    mesh data the placements must line up with.
    """
    rows = column_major_to_row_major(m16)
    return matmul4(GLTF_TO_BLENDER_BASIS, matmul4(rows, BLENDER_TO_GLTF_BASIS))


def gltf_position_to_blender(p_gltf: Sequence[float]) -> tuple[float, float, float]:
    """Apply the (+Y up → +Z up) X+90° rotation to a position vector.

    Mirrors the basis rotation in :data:`GLTF_TO_BLENDER_BASIS`.
    """
    return (p_gltf[0], -p_gltf[2], p_gltf[1])


def is_finite_matrix(m16: Sequence[float]) -> bool:
    """Sanity check — reject NaN / infinity matrices the toolkit
    occasionally emits when a bone is missing. Caller should log + skip."""
    return all(math.isfinite(v) for v in m16)


__all__ = [
    "GLTF_TO_BLENDER_BASIS",
    "BLENDER_TO_GLTF_BASIS",
    "column_major_to_row_major",
    "matmul4",
    "gltf_matrix_to_blender_rows",
    "gltf_position_to_blender",
    "is_finite_matrix",
]
