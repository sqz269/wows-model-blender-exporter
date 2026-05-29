# Material binding

The pipeline emits one material per `material_id` (matching WG's
shader naming — `TL2_SHIPMAT_PBS_Hull`, `SHIPMAT_PBS_Crack`, etc.).
The GLB carries these as Blender Material names; the sidecar carries
the matching texture bindings per slot per scheme.

## Slot → Principled BSDF input

| Sidecar slot | WG file suffix | glTF role | Principled input | Color space |
|---|---|---|---|---|
| `baseColor` | `_a` | base color | Base Color | sRGB |
| `metallicRoughness` | `_mr` | MR packed | Metallic (B) + Roughness (G) via Separate Color | Non-Color |
| `normal` | `_normal` | tangent normal | Normal Map | Non-Color |
| `occlusion` | `_ao` | AO | Multiplies into Base Color via MixRGB | Non-Color |
| `camoMask` | `_nbmask` | per-pixel paint zones (runtime camo) | (stored as custom property; not rendered) | n/a |

The MR routing matches glTF's spec exactly (B=metallic, G=roughness,
R=AO). The WG-pack quirk (raw `_mg.B` = emissive mask for
ARP/AL/Sabaton crossovers; see memory
`project_wg_emissive_mg_b_channel`) is NOT handled by the add-on
yet — the producer pipeline already emits glTF-conformant `_mr`
siblings via `wowsunpack swizzle-dir`, so consumers get conformant MR
out of the box.

## Scheme selection

Materials are bound from `texture_sets[<scheme>]`. The default
scheme is `"main"` (vanilla camo). Per-skin schemes (`azur_lane`,
`hist_ms22`, `camo_01`, etc.) are present in the sidecar but the
add-on currently does not expose a UI to switch them. Future work:
populate a drop-down from `sidecar.skins[*]` and rebind on selection.

## Idempotency

Re-binding the same material wipes prior WoWS-tagged nodes
(`node.name.startswith("WoWS_")`) before re-wiring. Manual edits to
non-WoWS nodes are preserved.

Each bound material gets three custom properties:

* `wows_material_id`   — sidecar material_id used for the binding
* `wows_scheme`        — which texture_set scheme was selected
* `wows_shader_intent` — `opaque_pbr` / `alpha_clip` / etc. from the sidecar
