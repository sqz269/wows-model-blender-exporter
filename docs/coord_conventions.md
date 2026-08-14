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
Blender's basis. That's a basis conjugation by the same **proper
rotation (X+90°, det = +1)** the stock glTF importer applies to the
mesh data:

    M_blender = B · M_gltf · B_inv

    B = [[1,  0,  0, 0],      X_b = +X_g
         [0,  0, -1, 0],      Y_b = -Z_g   (glTF forward → Blender -Y)
         [0,  1,  0, 0],      Z_b = +Y_g   (up stays up)
         [0,  0,  0, 1]]

Do **not** substitute the naive Y↔Z swap `(x, z, y)`: that matrix is
a reflection (det = -1). Under it, positions land fore-aft reversed
relative to the imported hull and every non-0/180° yaw mirrors —
front turrets on the stern. A 180° yaw is *invariant* under the
mirrored conjugation, so relative fore↔aft rotation probes pass; the
error only shows against the hull mesh. (This bug shipped once —
caught 2026-08-14 in the KK port.)

(See `src/wows_blender/placement.py:GLTF_TO_BLENDER_BASIS` for the
concrete matrices.) The Python helpers do the conjugation in
stdlib-only math; `mathutils.Matrix` wraps the result inside the
Blender importer.

## Attached children — local X-flip (applied, webview parity)

The webview's `ship.ts:applyAttachedMatrix` post-multiplies
attached-child matrices by `diag(-1, 1, 1, 1)`. The producer authors
attachment transforms against gltFast's X-negating import; an
importer that does NOT X-negate (three.js, Blender's stock glTF
importer) must post-mul the local X-flip or asymmetric children
(e.g. AM6068_Cartridges_Hoshino) extend inward instead of outward
from their anchor. `build.py:_ATTACHED_X_FLIP` applies it. ~99% of
attached children are X-symmetric and visually unaffected either
way. The flip is axis-aligned on X, so it commutes with the basis
conjugation above and can be applied after conversion.

## attached_y_flip — already baked into the matrix

A placement may carry `attached_y_flip: true` on the sidecar. The
producer has already folded the convention-B basis conjugation into
the matrix; the consumer decomposes verbatim. Do not pre-multiply
`Ry(180°)` — that's an over-correction from the schema_v3 era.
