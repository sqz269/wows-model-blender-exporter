# wows-model-blender-exporter

Blender consumer for the
[wows-model-export](https://github.com/sqz269/wows-model-export)
pipeline. Two pieces ship together:

1. **Publisher CLI** (`wows-export-blender`) — copies pipeline output
   into a Blender-friendly directory and converts every DDS to a PNG
   sibling so Blender's built-in image loader can pick them up.
2. **Blender add-on** (`wows_blender`) — pure-stdlib add-on that
   reads the published sidecar + library index and instantiates a
   fully-placed ship inside Blender, with materials wired and the
   producer's coord conventions converted to Blender's +Z-up basis.

```
┌──────────────────────────────┐       ┌──────────────────────────┐
│  PRODUCER                    │       │  CONSUMER                │
│  wows-model-export           │       │  Blender + wows_blender  │
│  (Python + Rust toolkit)     │       │  add-on                  │
│                              │       │                          │
│  Reads:  WoWS install        │       │  Reads:  <dest>/         │
│  Writes: GLB + sidecar +     │       │          <Ship>/...      │
│          library JSON +      │       │          accessories/... │
│          DDS textures        │       │  Builds: scene tree      │
└──────────────┬───────────────┘       │          with materials  │
               │                       └────────────▲─────────────┘
               │                                    │
               │     ┌──────────────────────────┐   │
               └────▶│ wows-export-blender CLI  │───┘
                     │ (this package)           │
                     │                          │
                     │ 1. compose.publish copy  │
                     │ 2. DDS→PNG via Pillow    │
                     └──────────────────────────┘
```

## Install

Two parts. Install the Python publisher into your normal Python; the
Blender add-on installs into Blender via its own UI.

### Publisher (Python CLI)

Requires Python 3.11+. From a checkout of both this repo and
`wows-model-export`:

```bash
pip install -e ../wows-model-export   # producer (one-time)
pip install -e .                      # this package
```

Or, once both are released to PyPI:

```bash
pip install wows-model-blender-exporter
```

### Blender add-on

```bash
# From a checkout:
wows-pack-blender-addon
# -> writes dist/wows_blender-0.1.0a1.zip

# Then in Blender:
#   Edit > Preferences > Add-ons > Install...
#   pick dist/wows_blender-0.1.0a1.zip
#   tick "WoWS Model Importer"
```

## Configure

Two paths matter:

| Path | What | How to set |
|---|---|---|
| **Workspace** | Where the producer wrote its per-ship dirs + libraries | `$WOWS_WORKSPACE`, `--workspace`, or the wows-model-export Settings page |
| **Blender destination** | Where to land the published artifacts | `$WOWS_BLENDER_LIBRARY`, or `--dest` |

The Blender destination defaults to `J:\BlenderShips`. Change with
`--dest "<your path>"` per invocation or set the env var once.

## Use

```bash
# Publish one ship — fastest dev cycle.
wows-export-blender BA_Montana

# Multiple ships.
wows-export-blender BA_Montana Baltimore_AzurLane

# Add the shared accessories + camo libraries (Blender needs these
# to instantiate turrets / directors / decoratives on top of the hull).
wows-export-blender BA_Montana --accessories

# Add projectiles too.
wows-export-blender BA_Montana --accessories --projectiles

# Publish every ship + every shared library.
wows-export-blender --all

# Override destination.
wows-export-blender BA_Montana --accessories --dest "D:\BlenderProjects\Ships"

# Skip the DDS→PNG conversion (re-import textures only — fast).
wows-export-blender BA_Montana --no-convert

# Force-rebuild everything.
wows-export-blender --all --force
```

Then in Blender, **3D Viewport > N-panel > WoWS tab > Import WoWS
Ship...** → pick the published `<dest>/<Ship>/<Ship>.meta.json`.

## What gets imported

```
<Ship>_root              ← Empty (ship root; has wows_ship_name custom prop)
├── <Ship>_hull          ← Empty wrapping the hull GLB
│   └── …mesh objects, with materials wired to PNG textures
├── Turrets              ← Empty group
│   ├── turret_AGM3019_..._HP_AGM_1   ← Empty (instance root)
│   │   └── …mesh objects
│   └── turret_..._HP_AGM_2
├── Secondaries
│   └── …
├── AntiAir
│   └── …
├── Torpedoes
│   └── …
└── Accessories          ← decorative non-gun mounts
    └── …
```

Every instance root carries custom properties for asset_id, instance_id,
hp_name, parent_section, and role — pick one in the outliner and the
Properties panel will show its sidecar identity.

## Camo / permoflage skins

Pass a `skin_id` to paint the ship. Both WG camo paths are implemented:
Path A (a 4-row palette lerp gated by the `camoExclusionMask`) and
Path B (a pre-baked per-category albedo atlas). The engine prefers B
wherever a part carries both, and so does the importer.

```bash
wows-export-blender BA_Montana --list-skins    # what does this ship offer?
```

In Blender, set **Skin** on the import operator (or the `--skin` flag
for FBX). `default` is the bare ship. Transparent materials never take
paint, and a material with no `camoExclusionMask` is left unpainted
rather than tinted ungated.

## FBX export

```bash
wows-export-blender BA_Montana --accessories --fbx
```

Drives a headless Blender to assemble the ship and write `.fbx` +
textures + a `<Ship>.materials.json` manifest under `<dest>/fbx/`. There
is also an **Export FBX** button in the N-panel for a scene you have
already imported and tweaked.

Two things worth knowing before you use it, both covered in
[`docs/fbx.md`](docs/fbx.md):

- FBX materials are Phong and cannot express the producer's channel
  packing, ambient occlusion, or camo masks. The manifest carries what
  the FBX cannot; bind from it for a faithful PBR result.
- **Camo needs `--bake`.** A per-pixel palette lerp has no FBX
  representation, so it has to be flattened into an albedo map.

By default only LOD 0, undamaged, non-collision geometry is exported —
a hull GLB also carries coarser LOD substitutes, damage-state variants
and armour volumes, which exported verbatim give you overlapping copies
of the ship inside a solid shell. `--lod all`, `--damage-variants` and
`--overlays` opt them back in.

## What's NOT covered today

- **Damage state cascade** — the crack / patch meshes are classified and
  hidden by default (and can be shown), but there is no per-seam state
  machine like the webview's `damage_cascade.ts` or Unity's
  `HullDamageState.cs`.
- **Turret rotation** — accessory armatures are imported but no IK /
  driver bindings; you can rotate the Yaw/Elev bones manually.
- **Particle FX** — out of scope; the producer's `assets.bin` particle
  extraction is still pre-release.

## Repo layout

```
wows-model-blender-exporter/
├── README.md
├── LICENSE
├── pyproject.toml                ← Python publisher package
├── src/
│   ├── wows_model_blender_exporter/   ← publisher (Pillow-backed)
│   │   ├── __init__.py
│   │   ├── dds_to_png.py              ← DDS→PNG conversion pass
│   │   ├── blender_runner.py          ← find Blender, drive it headlessly
│   │   └── cli/
│   │       ├── __init__.py
│   │       ├── export_blender.py      ← wows-export-blender CLI
│   │       └── pack_addon.py          ← wows-pack-blender-addon CLI
│   └── wows_blender/                  ← Blender add-on
│       ├── __init__.py                ← bl_info + register/unregister
│       │   # pure stdlib — importable outside Blender, unit-testable
│       ├── sidecar.py                 ← sidecar v3 reader (+ skins)
│       ├── library_index.py           ← accessory library index reader
│       ├── placement.py               ← gltf→Blender transform math
│       ├── camo.py                    ← which mask / palette / atlas applies
│       ├── visibility.py              ← LOD / damage / overlay classification
│       │   # bpy-dependent — only runs inside Blender
│       ├── materials.py               ← Principled BSDF binder
│       ├── camo_nodes.py              ← camo overlay as shader nodes
│       ├── build.py                   ← ship assembly (headless-callable)
│       ├── fbx_prep.py                ← FBX-legible rewrite + bake + manifest
│       ├── headless.py                ← `blender --background` entry point
│       ├── importer.py                ← operators (import, skins, export FBX)
│       └── panel.py                   ← N-panel UI
└── docs/
    ├── README.md                      ← docs index
    ├── coord_conventions.md
    ├── material_binding.md
    └── fbx.md                         ← FBX export
```

The `wows_blender` package is split so the decision-making half
(`sidecar`, `camo`, `visibility`, `placement`, `library_index`) imports
without `bpy` and can be exercised from a normal Python interpreter;
only the half that builds datablocks needs Blender.

## License

MIT.
