"""CLI entry points for the Blender exporter.

Two commands:

* ``wows-export-blender``       — publish pipeline artifacts to a
                                  Blender-friendly directory with PNG
                                  texture sidecars.
* ``wows-pack-blender-addon``   — bundle ``src/wows_blender/`` into a
                                  ZIP that can be installed via
                                  Blender's ``Edit > Preferences > Add-ons``
                                  install dialog.
"""
from __future__ import annotations

__all__: list[str] = []
