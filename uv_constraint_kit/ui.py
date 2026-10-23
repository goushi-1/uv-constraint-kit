# SPDX-License-Identifier: GPL-3.0-or-later
"""Panels and menu entries."""

import bpy
from . import live_symmetry

class _ConstraintButtons:
    """Shared button block used by both the UV editor and 3D viewport panels."""

    def draw_constraint_buttons(self, context, layout):
        prefs = context.preferences.addons.get(__package__)
        prefs = prefs.preferences if prefs else None

        col = layout.column(align=True)
        col.scale_y = 1.35
        row = col.row(align=True)
        row.operator("uv.uvck_constrain_horizontal", icon="EDGESEL")
        row = col.row(align=True)
        row.operator("uv.uvck_constrain_vertical", icon="EDGESEL")
        row = col.row(align=True)
        row.operator("uv.uvck_constrain_aligned", icon="EDGESEL")

        col = layout.column(align=True)
        col.operator("uv.uvck_unconstrain", icon="X")

        layout.separator()
        row = layout.row(align=True)
        row.scale_y = 1.35
        row.operator("uv.uvck_optimize", icon="UV_SYNC_SELECT")
        if prefs is not None:
            row.prop(prefs, "auto_optimize", text="", icon="AUTO")

        row=layout.row(align=True)
        row.operator("uv.uvck_symmetrize", icon="MOD_MIRROR")
        if live_symmetry.has_links(context):
            row.operator("uv.uvck_stop_symmetry", icon="UNLINKED")
        layout.operator("uv.uvck_circularize", icon="MESH_CIRCLE")
        layout.operator("uv.uvck_rectify", icon="MESH_PLANE")
        layout.operator("uv.uvck_quadrify", icon="MESH_GRID")

        if prefs is not None:
            row = layout.row(align=True)
            row.prop(prefs, "show_overlay", text="Show constraint colours",
                     icon="COLORSET_03_VEC")
            sub = row.row()
            sub.active = prefs.show_overlay
            sub.prop(prefs, "uv_edge_width", text="Line width")



class UVCK_PT_uv_editor(_ConstraintButtons, bpy.types.Panel):
    bl_idname = "UVCK_PT_uv_editor"
    bl_label = "UV Constraint"
    bl_space_type = "IMAGE_EDITOR"
    bl_region_type = "UI"
    bl_category = "UV Constraint"

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return bool(space and space.type == "IMAGE_EDITOR" and space.show_uvedit)

    def draw(self, context):
        layout = self.layout
        if context.edit_object is None:
            layout.label(text="Enter Edit Mode First", icon="INFO")
            return
        self.draw_constraint_buttons(context, layout)


class UVCK_PT_view3d(_ConstraintButtons, bpy.types.Panel):
    bl_idname = "UVCK_PT_view3d"
    bl_label = "UV Constraint"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "UV Constraint"

    @classmethod
    def poll(cls, context):
        obj = context.edit_object
        return bool(obj and obj.type == "MESH" and obj.data.uv_layers)

    def draw(self, context):
        self.draw_constraint_buttons(context, self.layout)


# ---------------------------------------------------------------------------
# menu entries
# ---------------------------------------------------------------------------


def _draw_menu(self, context):
    layout = self.layout
    layout.separator()
    layout.operator("uv.uvck_constrain_horizontal", icon="EDGESEL")
    layout.operator("uv.uvck_constrain_vertical", icon="EDGESEL")
    layout.operator("uv.uvck_constrain_aligned", icon="EDGESEL")
    layout.separator()
    layout.operator("uv.uvck_unconstrain", icon="X")
    layout.separator()
    layout.operator("uv.uvck_optimize", icon="UV_SYNC_SELECT")
    layout.operator("uv.uvck_circularize", icon="MESH_CIRCLE")
    layout.operator("uv.uvck_symmetrize", icon="MOD_MIRROR")
    layout.operator("uv.uvck_rectify", icon="MESH_PLANE")
    layout.operator("uv.uvck_quadrify", icon="MESH_GRID")


CLASSES = (UVCK_PT_uv_editor, UVCK_PT_view3d)


def register_menus():
    bpy.types.IMAGE_MT_uvs.append(_draw_menu)
    bpy.types.VIEW3D_MT_uv_map.append(_draw_menu)


def unregister_menus():
    bpy.types.IMAGE_MT_uvs.remove(_draw_menu)
    bpy.types.VIEW3D_MT_uv_map.remove(_draw_menu)
