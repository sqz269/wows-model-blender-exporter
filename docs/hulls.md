# Hull modules and hull-linked skins

A ship with several hull modules (a research-tree ship's A/B hulls, an
event ship's branches) has one hull mesh in the export but a different
set of mounts per module, and on some event ships a different skin per
module. The importer shows one module at a time.

Producer schema: `wows-model-export` `reference/schemas/METADATA_SPEC.md`
§11.1 (`variants.rendered_hull`) and §11.2 (`hulls` block, "Hull-linked
skins"). Reference consumer: the webview's Hulls tab
(`webview/src/lib/ship/viewer.ts`: `defaultHullName`, `applyHullSnapshot`,
the hull-linked skin step of `loadShip`).

## What the sidecar carries

| Field | Meaning |
|---|---|
| `variants.rendered_hull` | The hull whose mounts the export placed. The render doc `models/<Ship>_accessories.json` holds exactly those mounts. |
| `hulls.<H>.turrets` … `accessories` | The mount snapshot of hull `<H>`, with its own GameParams fields (`misc_filter`, arcs, …). |
| `hulls.<H>.linked_exterior_id` / `linked_skin` | The Exterior the hull's GameParams component links (`linkedCamouflage`) and the `skins[].skin_id` it produced. The game paints that skin whenever this hull is the selected one. |
| `hulls.<H>.is_active` | The gameplay-active (fully upgraded) hull. Not the rendered one in general. |

## Which hull a build shows

`--hull` (CLI and headless driver), **Hull** on the import operator, or
`build_ship(hull=...)`. It takes a `hulls` key (`C_Hull`), any case of
it, or the hull's upgrade module id (`PXUH803_HULL_C`). A name the
sidecar does not list is an error that names the available hulls.

Left out, the build shows the **rendered hull**. A sidecar written
before 2026-11-10 has no `variants.rendered_hull`; the importer then
applies the producer's own rule (`scaffold_ship.rendered_hull_name`):
the hull whose weapon mounts (turrets, secondaries, AA, torpedoes)
equal the top-level ones as `(section, hp_name, asset_id)` sets (spec
§11.1), else the hull sharing the most of them, else the active hull.
Over the build 13533378 workspace this fallback names the stored
`rendered_hull` on all 56 sidecars that carry one.

Another hull is built like this (webview `applyHullSnapshot`):

- The typed mount sections (turrets, secondaries, AA, torpedoes) are
  replaced by the hull's snapshot.
- Accessories split: an instance any hull's snapshot lists is a mount and
  is replaced; everything else (the hull's skel_ext decoratives) stays.
- A render-doc entry for the same instance, hardpoint and asset keeps the
  fields the snapshot lacks (`dead_asset_id`, `source`), with the
  snapshot's values on top.
- Each mount's attachments follow the shown hull's own `misc_filter`;
  the top-level values belong to the rendered hull.
- The hull mesh does not change: the producer exports the model of the
  lowest upgrade id only (spec §11.2 "Rendered hull").

With `--exterior` on a hull other than the rendered one, a mount swap
applies only where the hull places the swap's `base_asset_id`; the
exterior's records are diffed against the rendered hull, and a hardpoint
that holds another gun on this hull keeps it (with a warning).

## Which skin a build paints

`--skin` defaults to `auto`:

1. An exterior built with `--exterior` paints its own `camo_scheme_key`
   (unchanged: the scheme is part of the exterior's look).
2. Else the shown hull's `linked_skin`, when `skins[]` has it.
3. Else the bare ship.

Any explicit skin id wins over the hull-linked skin; `--skin default` is
the bare ship. A hull that links a skin missing from `skins[]` builds
bare, with a warning. The result reports why a skin was painted
(`skin_source`: `requested`, `exterior`, `hull` or `none`).

The ship root carries `wows_hull`, `wows_rendered_hull`, `wows_skin_id`
and `wows_skin_source`; the headless result JSON carries `hull`,
`rendered_hull`, `skin` and `skin_source`.

## Listing

```bash
wows-export-blender Siegfried_1510 --list-hulls
```

```
Siegfried_1510 — 15 hull(s):
  A_Hull           PXUH801_HULL_A           rendered,stock         GGM058_280mm54_5_C34  skin mat_Bermuda_Black
  B_Hull           PXUH802_HULL_B           stock                  GGM059_380mm_SKC_34_RF  skin mat_Bermuda_Blue
  C_Hull           PXUH803_HULL_C           stock                  AGM2005_8in55_Mk71  skin mat_Bermuda_Yellow
  ...
  E_Hull_TIER3     PXUH825_HULL_E_TIER3     active                 BGM163_12in_X_35_MK_VIII  skin mat_Bermuda_Red
```

The five branch roots are all stock hulls; the gameplay-active hull is
the last tier of the E branch, and the export rendered `A_Hull`.

This reads the sidecar with the stdlib reader (`wows_blender.hulls`), so
it needs no Blender. Inside Blender, **List Hulls** in the N-panel prints
the same list to the System Console.

## Examples

```bash
# The Expeditioner as the game shows its default branch: A_Hull guns,
# Bermuda Black paint.
wows-export-blender Siegfried_1510 --accessories --fbx --bake

# The C branch: phaser-laser turrets (AGM2005), Bermuda Yellow paint.
wows-export-blender Siegfried_1510 --accessories --fbx --bake --hull C_Hull

# The C branch, bare.
wows-export-blender Siegfried_1510 --accessories --fbx --hull C_Hull --skin default
```

Camo survives FBX only with `--bake` (see [`fbx.md`](fbx.md)).
