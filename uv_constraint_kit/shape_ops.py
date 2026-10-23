# SPDX-License-Identifier: GPL-3.0-or-later
"""Rectify and quadrify selected UV islands."""

import bmesh
import bpy
from time import perf_counter
from bpy.props import BoolProperty, EnumProperty, IntProperty

from . import circle, circle_ops, core, ops, overlay
from .i18n import t


def _poll(context):
    return circle_ops._poll(context)


def _run(context, function, **kwargs):
    changed = face_count = new_uv_boundaries = 0
    skipped = []
    selected = False

    for obj in circle_ops._edit_meshes(context):
        if not obj.data.uv_layers:
            continue
        bm = bmesh.from_edit_mesh(obj.data)
        core.prepare_bm(bm)
        uvl = bm.loops.layers.uv.active
        if uvl is None:
            del bm
            continue
        faces = circle.selected_island_faces(context, bm, uvl)
        del bm, uvl
        if not faces:
            continue
        selected = True
        try:
            result = function(context, obj, faces, **kwargs)
        except Exception as exc:
            skipped.append(str(exc))
            continue
        changed += int(result.get("changed", 0))
        face_count += int(result.get("faces", 0))
        new_uv_boundaries += int(result.get("new_uv_boundaries", 0))
        skipped.extend(result.get("skipped", ()))

    if changed:
        from . import live_symmetry
        live_symmetry.sync(context)
        overlay.bump_revision()
        core.invalidate_stats_cache()
        overlay.request_redraw()
        return changed, face_count, skipped, new_uv_boundaries, selected
    return 0, 0, skipped, 0, selected


def _finish(operator, context, function, **kwargs):
    started = perf_counter()
    changed, faces, skipped, new_uv_boundaries, selected = _run(context, function, **kwargs)

    if not changed:
        if selected and not skipped:
            operator.report({"INFO"}, ops._elapsed_text(perf_counter()-started))
            return {"FINISHED"}
        message = "; ".join(t(str(reason)) for reason in skipped)
        if not message:
            message = t("Select a closed UV edge loop first" if function.__name__=='rectify'
                        else "Select a point, edge, or face inside a UV island first")
        operator.report({"WARNING"}, message)
        return {"FINISHED"} if selected else {"CANCELLED"}

    operator.report({"INFO"}, ops._elapsed_text(perf_counter()-started))
    if skipped:
        operator.report({"WARNING"}, "; ".join(t(str(reason)) for reason in skipped))
    return {"FINISHED"}


class UVCK_OT_rectify(bpy.types.Operator):
    bl_idname = "uv.uvck_rectify"
    bl_label = "Rectify Island"
    bl_description = "Rectify complete selected UV edge loops and optimize the rest of their islands"
    bl_options = {"REGISTER", "UNDO"}

    shape: EnumProperty(
        name="Target shape",
        items=(("RECTANGLE", "Target shape: Rectangle", "Fit a rectangle"),
               ("SQUARE", "Target shape: Square", "Fit a square")),
        default="RECTANGLE",
    )
    spacing: EnumProperty(
        name="Boundary spacing",
        items=(("GEOMETRY", "Boundary spacing: Geometry", "Use mesh edge lengths"),
               ("UV", "Boundary spacing: UV", "Keep original UV edge proportions"),
               ("EVEN", "Boundary spacing: Even", "Space boundary points evenly")),
        default="GEOMETRY",
    )
    orient_to_axis: BoolProperty(name="Orient to axis", default=True)
    iterations: IntProperty(name="Interior iterations", default=150, min=1, max=1000)
    respect_pins: BoolProperty(name="Respect pins", default=True)
    prevent_overlap: BoolProperty(name="Prevent overlap", default=False)

    @classmethod
    def poll(cls, context):
        return _poll(context)

    def execute(self, context):
        from . import rectangle
        return _finish(
            self, context, rectangle.rectify,
            shape=self.shape, spacing=self.spacing,
            orient_to_axis=self.orient_to_axis,
            selected_only=True, iterations=self.iterations,
            respect_pins=self.respect_pins,
            prevent_overlap=self.prevent_overlap,
            template_only=not ops._auto_optimize(context),
        )


class UVCK_OT_quadrify(bpy.types.Operator):
    bl_idname = "uv.uvck_quadrify"
    bl_label = "Quadrify Island Grid"
    bl_description = "Quadrify selected UV islands"
    bl_options = {"REGISTER", "UNDO"}

    shape: EnumProperty(
        name="Quad face shape",
        items=(("PER_FACE", "Quad face shape: Per Face", "Keep the proportions of each mesh face"),
               ("FACE", "Quad face shape: Active Face", "Copy the proportions of the initial face"),
               ("EVEN", "Quad face shape: Square", "Make square UV faces")),
        default="PER_FACE",
    )
    average_shape: BoolProperty(name="Average quad shape", default=True)
    skip_non_quads: BoolProperty(name="Skip islands with non-quads", default=False)
    respect_pins: BoolProperty(name="Respect pins", default=True)
    prevent_overlap: BoolProperty(name="Prevent overlap", default=False)

    @classmethod
    def poll(cls, context):
        return _poll(context)

    def execute(self, context):
        from . import quadrify
        return _finish(
            self, context, quadrify.quadrify,
            shape=self.shape, average_shape=self.average_shape,
            skip_non_quads=self.skip_non_quads,
            respect_pins=self.respect_pins,
            prevent_overlap=self.prevent_overlap,
        )


CLASSES = (UVCK_OT_rectify, UVCK_OT_quadrify)
