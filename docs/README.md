# Docs

Design notes and reference material for the Blender consumer.

| Doc | What |
|---|---|
| [`coord_conventions.md`](coord_conventions.md) | glTF → Blender axis swap + WG/toolkit basis conjugation. |
| [`material_binding.md`](material_binding.md) | How sidecar `texture_sets[<scheme>]` maps to Principled BSDF inputs. |
| [`fbx.md`](fbx.md) | FBX export — why materials need a prep pass, what gets filtered out, and why camo needs `--bake`. |
| [`hulls.md`](hulls.md) | Hull modules — which hull a build shows (`variants.rendered_hull`, `--hull`), how another hull's mounts replace the rendered ones, and the hull-linked skin a build paints by default. |

## Where the producer schema lives

The authoritative pipeline schema lives in the producer repo:
`wows-model-export/reference/contracts/` — `METADATA_SPEC.md`,
`example.meta.json`, `turret_rig_spec.md`, `damage_state_semantics.md`.
This consumer reads a subset of that schema (materials,
turrets/secondaries/antiair/torpedoes/accessories placements, skins,
exteriors, `variants.rendered_hull` and the `hulls` block).
