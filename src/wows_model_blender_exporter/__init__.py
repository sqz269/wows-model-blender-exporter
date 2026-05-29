"""wows-model-blender-exporter: Blender consumer for the wows-model-export pipeline.

Two pieces packaged together:

1. **Python publisher** (this package, ``wows_model_blender_exporter``)
   — wraps :func:`wows_model_export.compose.publish.publish` with
   Blender-friendly defaults, then runs a DDS→PNG conversion pass over
   the published textures so Blender's built-in image loader can pick
   them up. Blender does not ship a DDS importer in its bundled
   Python, and shipping one inside the add-on would force users to
   install Pillow into Blender's venv. Convert once at publish time
   instead.

2. **Blender add-on** (``src/wows_blender/``) — pure-stdlib add-on
   that reads the sidecar + library index and instantiates a fully-
   placed ship inside Blender. Operators land under the "WoWS" tab in
   the 3D Viewport N-panel. The add-on is shipped as a separate ZIP
   built via ``wows-pack-blender-addon`` — NOT bundled into the pip
   wheel, because Blender add-ons cannot pip-install dependencies and
   importing the publisher (which depends on Pillow + numpy) would
   error inside Blender.

The producer→exporter→consumer split:

    PRODUCER             EXPORTER               CONSUMER
    wows-model-export    wows-model-blender-    Blender + wows_blender add-on
                         exporter
        │                    │                          │
        ▼                    ▼                          ▼
    workspace/         <blender_dir>/             scene tree:
      ships/<S>/         <S>/...                    <S>_root
      libraries/         libraries/                   ├── hull
                         (PNGs alongside DDS)         └── turrets/secondaries/...
"""
from __future__ import annotations

__version__ = "0.1.0a1"

__all__ = ["__version__"]
