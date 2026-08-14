# FBX export

`wows-export-blender --fbx` drives a headless Blender to assemble a
published ship and write `.fbx` + textures + a material manifest.

```bash
# hull + every placed accessory, textures copied alongside the .fbx
wows-export-blender BA_Montana --accessories --fbx

# a camo scheme, flattened so it survives FBX's material model
wows-export-blender BA_Montana --accessories --fbx \
    --skin camo_permanent_1__USNP01 --bake

wows-export-blender BA_Montana --list-skins   # what can --skin take?
```

Output per ship, under `<dest>/fbx/` (override with `--fbx-out`):

```
<Ship>.fbx                  the model
<Ship>.fbm/                 textures, copied by Blender
<Ship>.materials.json       the slot map FBX cannot carry (see below)
<Ship>_baked/               baked base-colour PNGs, only with --bake
```

## Why a prep pass exists

Blender's FBX exporter does not read arbitrary node graphs. It reads
materials through `bpy_extras.node_shader_utils.PrincipledBSDFWrapper`,
which follows **exactly one link** off each Principled socket and
requires the node it lands on to be a `ShaderNodeTexImage`:

```python
node_image = socket.links[0].from_node
if node_image.bl_idname == 'ShaderNodeTexImage': ...
```

The material binder (`materials.py`) deliberately does not satisfy that.
It splices a MixRGB in front of Base Color to multiply in ambient
occlusion, routes metallic + roughness through a SeparateColor (the
producer packs both into one `_mr` image), and puts a GREATER_THAN in
front of Alpha for hard cutout. All correct for rendering, all invisible
to the FBX exporter.

`fbx_prep.py` rewires each material into the shape the wrapper can read
before export. Measured on Massachusetts' hull:

| Principled socket | with prep | without prep |
|---|---:|---:|
| base colour | 5 | 4 |
| roughness | 3 | **0** |
| metallic | 3 | **0** |
| normal | 2 | 2 |
| emissive | 2 | 2 |
| alpha | 5 | 4 |

Without the prep every material loses metallic and roughness, and each
AO-bound material also loses its base colour and alpha.

The rewrite is destructive — it drops the AO multiply and the camo
composite. The headless driver runs in a throwaway process; the
interactive **Export FBX** operator is marked `UNDO`, so Ctrl+Z restores
the render-accurate graph.

## Content filtering

A hull GLB is not just the ship. Massachusetts' hull carries 146 meshes:

| | meshes |
|---|---:|
| intact high-detail hull | 12 |
| coarser LOD substitutes (`_lod1`…`_lod4`) | 79 |
| damage-state variants (`_crack_`, `_patch_`) | 69 |
| `Armor` + `Hitboxes` collision volumes | 37 |

Exported verbatim you get four overlapping copies of the ship, cracked
and patched, inside a solid armour shell. So by default only LOD 0,
undamaged, non-overlay geometry is exported — 12 meshes / 71k triangles
instead of 146 / 188k. Opt the rest back in with `--lod all`,
`--damage-variants`, `--overlays`.

The classification rules are ported verbatim from the webview
(`webview/src/lib/ship/visibility.ts`), including the detail that LOD
suffixes come in **two** flavours — `_lod1Shape` *and* `_lodShape1`.
Matching only the first silently keeps a second set of duplicates. Keep
`visibility.py` in sync if a third flavour appears.

## Camo needs `--bake`

Path A camo is a per-pixel palette lerp: four palette rows blended by a
mask's R/G/B channels, gated by the `camoExclusionMask` (WG's `mg.B`).
No FBX material slot can express that — FBX materials are Phong, with
one texture per channel.

`--bake` flattens it. Each material's evaluated Base Color is baked to a
PNG (Cycles `DIFFUSE` pass with direct + indirect light disabled, so it
evaluates the colour input only) and that PNG becomes the exported base
colour. Verified on Massachusetts with `camo_permanent_1__USNP01`:
97.9% of the hull material's texels change versus the default skin,
glass is correctly excluded (transparent materials never take paint),
and the wire material barely moves because its gate is near zero.

The cost is PBR separation — ambient occlusion and paint are burned into
the albedo. Without `--bake` a camo skin still builds correctly in
Blender, but the FBX carries the unpainted albedo.

Path B camo (`mat_textures`, a pre-baked per-category atlas) is a
straight albedo swap and needs no bake. The engine prefers B wherever a
part carries both, so the builder tries it first.

## What the FBX cannot carry

FBX materials are Phong. Blender's exporter maps roughness to
`Shininess`, metallic to `ReflectionFactor`, and has no slot at all for
ambient occlusion or the camo masks. The packed `_mr` image lands on
both the roughness and metallic slots because FBX has no concept of
channel packing.

`<Ship>.materials.json` is the ground truth a consumer should bind
from:

```json
{
  "blender_material": "TL2_SHIPMAT_EMISSIVE_PBS_Hull",
  "material_id": "TL2_SHIPMAT_EMISSIVE_PBS_Hull",
  "shader_intent": "opaque_pbr",
  "double_sided": false,
  "slots": {
    "baseColor":         {"texture": "..._a.png",        "semantics": "RGB=albedo, A=opacity"},
    "metallicRoughness": {"texture": "..._mr.png",       "semantics": "G=roughness, B=metallic (glTF packing)"},
    "normal":            {"texture": "..._normal.png",   "semantics": "tangent-space normal"},
    "occlusion":         {"texture": "..._ao.png",       "semantics": "R=ambient occlusion"},
    "emissive":          {"texture": "..._emissive.png", "semantics": "RGB=emissive colour"}
  }
}
```

## Textures must be PNG

The binder falls back to handing Blender the raw DDS when no PNG sibling
exists. That is fine inside Blender and useless outside it — Maya, 3ds
Max and Unreal will not read a DDS referenced from an FBX. The export
warns when any bound texture is still DDS; the fix is to publish first
so the DDS→PNG pass runs:

```bash
wows-export-blender BA_Montana --accessories   # converts DDS -> PNG
wows-export-blender BA_Montana --accessories --fbx
```

## Axis convention

Defaults are `--axis-up Y --axis-forward=-Z`, which is what Maya, Unity
and Unreal expect. Pass a negative axis with an equals sign —
`--axis-forward=-X` — or argparse reads the value as another flag.

The values used are recorded in the manifest.

## Finding Blender

Discovery order: `--blender`, then `$WOWS_BLENDER_EXE`, then `blender`
on `PATH`, then the usual install locations — including Steam libraries
on every drive, since Steam is a common way to get Blender on Windows.
