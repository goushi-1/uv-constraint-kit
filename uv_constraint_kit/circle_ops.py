# SPDX-License-Identifier: GPL-3.0-or-later
"""Operator integration for circularizing selected UV islands."""

import bmesh
import bpy
from time import perf_counter
from bpy.props import BoolProperty, EnumProperty, IntProperty

from . import circle, core, ops, overlay
from .i18n import t


def _edit_meshes(context):
    objects = list(getattr(context, "objects_in_mode_unique_data", ()) or ())
    if not objects:
        obj = context.active_object
        if obj is not None:
            objects = [obj]
    return [obj for obj in objects
            if obj and obj.type == "MESH" and obj.mode == "EDIT"]


def _poll(context):
    return (not overlay._transform_running() and
            any(len(obj.data.uv_layers) > 0 for obj in _edit_meshes(context)))


class UVCK_OT_circularize(bpy.types.Operator):
    bl_idname = "uv.uvck_circularize"
    bl_label = "Circularize Island"
    bl_description = "Shape selected closed UV edge loops and optimize the rest of their islands"
    bl_options = {"REGISTER", "UNDO"}

    spacing: EnumProperty(
        name="Spacing",
        items=(
            ("UV", "UV", "Keep the original UV edge length proportions"),
            ("GEOMETRY", "Geometry", "Use mesh edge lengths"),
            ("EVEN", "Even", "Space boundary points evenly"),
        ),
        default="EVEN",
    )
    iterations: IntProperty(name="Iterations", default=150, min=1, max=1000)
    respect_pins: BoolProperty(name="Respect Pins", default=True)
    prevent_overlap: BoolProperty(name="Prevent Overlap", default=False)

    @classmethod
    def poll(cls, context):
        return _poll(context)

    def execute(self, context):
        started = perf_counter()
        changed = 0
        face_count = 0
        skipped = []
        has_targets = False

        for obj in _edit_meshes(context):
            if not obj.data.uv_layers:
                continue
            bm = bmesh.from_edit_mesh(obj.data)
            core.prepare_bm(bm)
            uvl = bm.loops.layers.uv.active
            if uvl is None:
                del bm
                continue
            faces = circle.selected_island_faces(context, bm, uvl)
            # The circularizer solves synchronously in Python and does not call
            # bpy.ops. Release the temporary BMesh wrappers before entering it.
            del bm, uvl
            if not faces:
                continue
            has_targets = True
            try:
                result = circle.circularize(
                    context, obj, faces, spacing=self.spacing,
                    iterations=self.iterations, respect_pins=self.respect_pins,
                    prevent_overlap=self.prevent_overlap,
                    selected_only=True,
                    template_only=not ops._auto_optimize(context),
                )
            except Exception as exc:
                # The kernel has restored this object's UVs. Keep successful
                # objects in this operation's undo step and report this one.
                skipped.append(str(exc))
                continue
            changed += int(result.get("changed", 0))
            face_count += int(result.get("faces", 0))
            skipped.extend(result.get("skipped", ()))

        if changed > 0:
            from . import live_symmetry
            live_symmetry.sync(context)
            overlay.bump_revision()
            core.invalidate_stats_cache()
            overlay.request_redraw()
            self.report({"INFO"}, ops._elapsed_text(perf_counter()-started))
            if skipped:
                self.report({"WARNING"}, "; ".join(t(str(reason)) for reason in skipped))
            return {"FINISHED"}

        if skipped:
            message = "; ".join(t(str(reason)) for reason in skipped)
        else:
            message = t("Select a closed UV edge loop first")
        self.report({"WARNING"}, message)
        # Keep F9 available to adjust spacing or pins after a geometric
        # rejection. The kernel restored the UVs.
        return {"FINISHED"} if has_targets else {"CANCELLED"}
