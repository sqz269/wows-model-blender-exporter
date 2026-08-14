# Docs

Design notes and reference material for the Blender consumer.

| Doc | What |
|---|---|
| [`coord_conventions.md`](coord_conventions.md) | glTF → Blender axis swap + WG/toolkit basis conjugation. |
| [`material_binding.md`](material_binding.md) | How sidecar `texture_sets[<scheme>]` maps to Principled BSDF inputs. |
| [`fbx.md`](fbx.md) | FBX export — why materials need a prep pass, what gets filtered out, and why camo needs `--bake`. |

## Where the producer schema lives

The authoritative pipeline schema lives in the producer repo:
`wows-model-export/reference/contracts/` — `METADATA_SPEC.md`,
`example.meta.json`, `turret_rig_spec.md`, `damage_state_semantics.md`.
This consumer reads a small subset of that schema (materials,
turrets/secondaries/antiair/torpedoes/accessories placements, skins).
