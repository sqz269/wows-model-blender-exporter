"""N-panel UI under the 3D Viewport "WoWS" tab.

Import, skin discovery, and FBX export. Future iterations would add:

* damage state toggle (per-seam patch / crack)
* per-section visibility toggles

Skin selection is a free-text ``skin_id`` on the import operator rather
than a drop-down: populating an enum would mean re-parsing a ~1.4 MB
sidecar on every UI redraw, so List Skins prints the catalogue to the
console instead and the id is pasted in.
"""
from __future__ import annotations

import bpy
from bpy.types import Panel


class WOWS_PT_import_panel(Panel):
    bl_label = "WoWS Import"
    bl_idname = "WOWS_PT_import_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "WoWS"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="Import")
        col.operator("wows.import_ship", icon="MESH_DATA")
        col.operator("wows.import_accessory", icon="MESH_CUBE")
        col.operator("wows.list_skins", text="List Skins", icon="COLOR")

        layout.separator()
        col = layout.column(align=True)
        col.label(text="Export")
        col.operator("wows.export_fbx", text="Export FBX", icon="EXPORT")

        layout.separator()
        col = layout.column(align=True)
        col.label(text="Quick links")
        col.operator(
            "wm.url_open", text="Publisher: wows-export-blender", icon="URL",
        ).url = "https://github.com/sqz269/wows-model-blender-exporter"
        col.operator(
            "wm.url_open", text="Producer: wows-model-export", icon="URL",
        ).url = "https://github.com/sqz269/wows-model-export"


classes = (
    WOWS_PT_import_panel,
)


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


__all__ = ["WOWS_PT_import_panel", "register", "unregister"]
