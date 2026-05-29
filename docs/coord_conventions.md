# Coord conventions

Three coord systems meet in the import path:

| Frame | Up | Forward | Handed | Notes |
|---|---|---|---|---|
| **glTF** (pipeline output) | +Y | +Z | right | producer / toolkit emits all matrices here |
| **three.js** (webview) | +Y | +Z | right | matches glTF natively — no axis swap |
| **Blender** | +Z | -Y | right | add-on lives here |

## Hull GLB import

Blender's built-in glTF importer applies the `+Y up → +Z up` axis
swap during mesh import. So the hull GLB's vertex positions land in
Blender's `+Z up` frame without us doing anything. We just call
`bpy.ops.import_scene.gltf(...)`.

## Placement matrices

The sidecar's `transform.matrix` is a column-major 16-float matrix in
glTF's `+Y up` basis. The transform composes: `M_world = M_placement
· M_local`, where `M_local` is the asset's own root transform.

To apply it inside Blender we need to express the same transform in
Blender's basis. That's a basis conjugation:

    M_blender = B_inv · M_gltf · B

where

    B = [[1,  0,  0, 0],
         [0,  0, -1, 0],     ← +Y_blender = -Z_gltf?  No — +Z_blender = +Y_gltf
         [0,  1,  0, 0],     ← +Y_blender = -Z_gltf (forward becomes -Y)
         [0,  0,  0, 1]]

(See `src/wows_blender/placement.py:GLTF_TO_BLENDER_BASIS` for the
concrete matrices.) The Python helpers do the conjugation in
stdlib-only math; `mathutils.Matrix` wraps the result inside the
Blender importer.

## Attached children — gltFast parity (NOT applied in Blender)

The webview's `placement.ts:applyAttachedMatrix` post-multiplies
attached-child matrices by `diag(-1, 1, 1, 1)` to mirror local-X.
That fix exists because Unity's gltFast importer X-negates glTF
vertex positions on import while three.js doesn't — see memory
`project_webview_xflip_asymmetric_meshes`. Blender's stock glTF
importer follows the three.js convention (no X-negation), so we DO
NOT apply the attached-child X-flip here. The schema_v6 baked
basis conjugation (memory `project_variant_swap_bone_mismatch`)
means the consumer decomposes the matrix verbatim regardless.

## attached_y_flip — already baked into the matrix

A placement may carry `attached_y_flip: true` on the sidecar. The
producer has already folded the convention-B basis conjugation into
the matrix; the consumer decomposes verbatim. Do not pre-multiply
`Ry(180°)` — that's an over-correction from the schema_v3 era.
