"""WoWS model import add-on for Blender.

Reads :ref:`wows-model-export` pipeline artifacts published via
``wows-export-blender`` and instantiates a fully-placed ship inside
Blender, with materials wired to PNG sibling textures and the
producer's coord conventions converted to Blender's +Z-up basis.

Two operators in the 3D Viewport "WoWS" N-panel:

* **Import WoWS Ship** — pick a ``<Ship>.meta.json``, get the hull
  + every accessory placement.
* **Import WoWS Accessory** — pick one library GLB standalone.

Designed to ship as a single ZIP and install via
``Edit > Preferences > Add-ons > Install...``. Pack with
``wows-pack-blender-addon`` from a checkout, or grab the
prebuilt ZIP from the wows-model-blender-exporter GitHub release.
"""
from __future__ import annotations

bl_info = {
    "name": "WoWS Model Importer",
    "author": "Zong",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > WoWS",
    "description": "Import World of Warships ships exported by the "
                   "wows-model-export pipeline.",
    "category": "Import-Export",
    "doc_url": "https://github.com/sqz269/wows-model-blender-exporter",
    "tracker_url": "https://github.com/sqz269/wows-model-blender-exporter/issues",
}

# bpy-dependent sub-modules are imported lazily inside register() so
# the stdlib-only pieces (sidecar, library_index, placement) stay
# importable from a normal Python interpreter for testing the
# publisher pipeline.


def register() -> None:
    from . import importer, panel
    importer.register()
    panel.register()


def unregister() -> None:
    from . import importer, panel
    panel.unregister()
    importer.unregister()


__all__ = ["register", "unregister", "bl_info"]
