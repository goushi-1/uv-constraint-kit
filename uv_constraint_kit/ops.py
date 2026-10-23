# SPDX-License-Identifier: GPL-3.0-or-later
"""Operators: constrain / unconstrain / optimize."""

from time import perf_counter

import bmesh
import bpy
from bpy.props import BoolProperty, StringProperty, IntProperty, FloatProperty

from . import core, overlay
from .const import (
    C_ALIGNED,
    C_HORIZONTAL,
    C_NONE,
    C_VERTICAL,
    RIZOM_OPTIMIZE_DEFAULTS,
)
from .i18n import t


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------

last_optimize_result = {
    "obj": None, "status": "NOT_RUN", "reason": None,
    "solver": None, "max_deviation": None,
}
_timing_deferred = False


def _set_last_optimize_result(obj, status, reason=None, solver=None,
                              max_deviation=None):
    global last_optimize_result
    last_optimize_result = {
        "obj": getattr(obj, "name", None), "status": status,
        "reason": reason, "solver": solver,
        "max_deviation": max_deviation,
    }


def _rejection_reason(stats):
    reason = stats.get("reason") or (stats.get("health") or {}).get("rejection_reason")
    descriptions = {
        "fixed_boundary_conflict": "fixed boundary conflicts with edge constraints",
        "pinned_constraint_conflict": "pinned vertices conflict with edge constraints",
        "pinned_layout_rejected": "no safe constrained layout found while preserving fixed UV points",
        "constraint_triangle_conflict": "constraint run forces all three UV triangle corners onto one line",
        "non_finite": "non-finite UV result",
        "collapsed_constraint": "a constrained edge collapsed to zero length",
        "constraint_residual": "constraint residual exceeds tolerance",
        "triangle_orientation": "triangle flipped or became degenerate",
        "fixed_point_moved": "a pinned UV vertex moved",
        "outside_selection_moved": "UVs outside the target changed",
        "area_drift": "target island area changed",
        "new_self_overlap": "a new overlap appeared within an island",
        "new_island_overlap": "a new overlap appeared between islands",
        "no_feasible_result": "no safe candidate passed validation",
        "input_recovery_failed": "no safe initialization could repair the input UVs while preserving constraints",
    }
    if reason in descriptions:
        message = t(descriptions[reason])
    else:
        message = t("no safe candidate passed validation")
    deviation = (stats.get("health") or {}).get("max_deviation")
    if reason == "constraint_residual" and deviation is not None:
        message += " (%.4f deg)" % deviation
    points = stats.get('conflict_points')
    if points:
        coords = '; '.join('(%.5f, %.5f)' % tuple(p['uv']) for p in points[:4])
        message += '; ' + t('Conflicting fixed UV coordinates: %s') % coords
    elif reason == 'pinned_layout_rejected':
        message += '; ' + t('Fixed UV points in target: %d') % len(stats.get('fixed_points', ()))
    if ('manual_optimize_iterations' in stats and
            reason not in ('fixed_boundary_conflict', 'pinned_constraint_conflict')):
        message += '; ' + t('Manual optimization iterations: %d; no safe result found with these settings') % stats['manual_optimize_iterations']
    return message


def _elapsed_text(seconds):
    return t("Optimization time: %.3f s") % seconds


def _report_mark_status(operator, optimize_result):
    if optimize_result is None or _timing_deferred or 'elapsed_seconds' not in last_optimize_result:
        return
    elapsed = _elapsed_text(last_optimize_result['elapsed_seconds'])
    if ("CANCELLED" in optimize_result or
            last_optimize_result.get('status') in {'REJECTED', 'ERROR'}):
        operator.report({"WARNING"}, elapsed)
    else:
        operator.report({"INFO"}, elapsed)


def _edit_mesh_object(context):
    obj = context.edit_object
    if obj is None:
        obj = context.active_object
        if obj is not None and obj.mode != "EDIT":
            return None
    if obj is None or obj.type != "MESH":
        return None
    return obj


def poll_uv_constraint(context):
    if overlay._transform_running():
        return False
    obj = _edit_mesh_object(context)
    if obj is None:
        return False
    return len(obj.data.uv_layers) > 0


def _prefs(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def _auto_optimize(context):
    prefs = _prefs(context)
    return True if prefs is None else prefs.auto_optimize


def _relax_settings(context):
    # Internal automatic iteration budget; user controls belong to Optimize/F9.
    return 150, 0.7


def _redraw(context):
    """Repaint the UV editors. See `overlay.request_redraw` for why the plain
    `area.tag_redraw()` is not enough."""
    from . import live_symmetry
    live_symmetry.sync(context)
    overlay.request_redraw()


def _unwrap_settings(context):
    return "ANGLE_BASED", 0.001


def _optimize_settings():
    """The Optimize knobs, as a kwargs dict for `core.rebuild_islands`.

    These are RizomUV's own defaults, read off the running application's
    Optimize panel (see `const.RIZOM_OPTIMIZE_DEFAULTS` for the snapshot and the
    one caveat that matters: `keep_metric=True` rescales islands). They are not
    exposed in the preferences -- the user asked for RizomUV's values to be used
    as-is rather than to have six sliders to maintain.
    """
    return dict(RIZOM_OPTIMIZE_DEFAULTS)


def _describe(stats, faces, solved):
    """One line saying what the solve actually did, in measured numbers.

    Note the shape of every `t(...)` here: the **template** is translated and
    only then is `%` applied. Translating the finished sentence (the `%` inside
    the call) would look up a string no catalog can hold -- the numbers are
    already in it -- and quietly hand back English.
    """
    if (stats.get('health') or {}).get('constraints_limited'):
        return t("Protection settings limited alignment; remaining deviation %.3f deg") % stats['health']['max_deviation']
    if stats.get("skipped"):
        return (t("Optimize strength is 0, nothing was changed (%d faces)")
                % len(faces))
    if not stats:
        return t("Re-solved %d faces (%d triangles)") % (len(faces), solved)
    before = stats.get("before") or {}
    after = stats.get("after") or {}
    return (
        t("Re-solved %d islands / %d faces: worst angle %.2f deg -> %.2f deg, "
          "edge spread %.3f -> %.3f, density %.3f -> %.3f")
        % (stats.get("islands", 0), len(faces),
           before.get("worst_angle_deg", 0.0), after.get("worst_angle_deg", 0.0),
           before.get("edge_spread", 0.0), after.get("edge_spread", 0.0),
           before.get("density", 0.0), after.get("density", 0.0)))


def _optimize_islands(context, obj, face_indices, fix_boundary=False, parameters=None):
    """Re-solve the given islands: constrain, re-unwrap, restore the pose.

    This is RizomUV's Optimize. Every island is re-solved with the constrained
    vertices held as a hard boundary condition, then its own position, size and
    texel density are fitted back so the layout around it is left alone.

    `fix_boundary` pins the island's own border as well -- a fixed boundary
    condition in the literal sense: the outline stops moving and only the
    interior is relaxed.

    Returns (triangles solved, triangles left flipped, stats).
    """
    from . import live_symmetry
    face_indices = live_symmetry.source_faces(obj, face_indices)
    if not face_indices:
        return 0, 0, {}
    settings = _optimize_settings()
    if parameters:
        settings.update(parameters)
    if settings["mix"] <= 0.0:
        return 0, 0, {"skipped": True}
    iterations, step = _relax_settings(context)
    method, margin = _unwrap_settings(context)

    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    uvl = bm.loops.layers.uv.active
    if uvl is None:
        return 0, 0, {}
    positions = core.constrained_loops(bm, only_faces=face_indices)
    # `bpy.ops.uv.unwrap` rebuilds the edit mesh, so no BMesh handle may be
    # alive across the call below.
    del bm, uvl

    from . import shape_constraints
    with shape_constraints.preserve(obj,face_indices) as shapes:
        result = core.rebuild_islands(context, obj, face_indices, positions,
                                 method=method, margin=margin,
                                 iterations=iterations, step=step,
                                 fix_boundary=fix_boundary,
                                 _report_quality=False,
                                 **settings)
        if shapes is not None:shapes['accepted']=not result[2].get('rejected')
    result[2]['engine'] = 'REPLICA'
    if result[2].get('rejected') and not settings.get('auto_iterations', True):
        result[2]['manual_optimize_iterations'] = max(1, int(settings.get('optimize_iterations', 1)))
    return result


def _solve_and_record(context, obj, faces, fix_boundary=False, parameters=None):
    """Shared solve body; the calling operator owns its single undo step."""
    started = perf_counter()
    stats = None
    _set_last_optimize_result(obj, "RUNNING")
    try:
        solved, flips, stats = _optimize_islands(
            context, obj, faces, fix_boundary=fix_boundary, parameters=parameters)
        health = stats.get("health") or {}
        if stats.get('rejected'):
            _set_last_optimize_result(obj, "REJECTED", _rejection_reason(stats),
                                      stats.get("solver"), health.get("max_deviation"))
        else:
            # Native refinement may rebuild the edit mesh; mirror from fresh handles.
            core.store_constraints_from_object(obj)
            core.invalidate_stats_cache()
            flips = health.get("new_flips", flips)
            status = "SKIPPED" if stats.get("skipped") else "COMPLETED_LIMITED" if health.get('constraints_limited') else (
                "COMPLETED_WITH_FLIPS" if flips else "COMPLETED")
            reason = ("%d triangles remain flipped" % flips) if flips else None
            _set_last_optimize_result(obj, status, reason, stats.get("solver"),
                                      health.get("max_deviation"))
        return solved, flips, stats
    except Exception as exc:
        _set_last_optimize_result(obj, "ERROR", str(exc))
        raise
    finally:
        elapsed = perf_counter()-started
        last_optimize_result['elapsed_seconds'] = elapsed
        last_optimize_result['engine'] = 'REPLICA'
        if stats is not None:
            stats['elapsed_seconds'] = elapsed


def _auto_optimize_edges(context, fix_boundary, positions, parameters=None):
    """Finish marking and its automatic solve inside the same undo step.

    Synchronous execution cannot race a later transform/undo, so it needs no
    queued request or duplicate full-mesh snapshots. The Optimize operator owns
    this action's redo parameters. No BMesh handle crosses native refinement.
    """
    obj = _edit_mesh_object(context)
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    uvl = bm.loops.layers.uv.active
    loops = core.loops_in_order(bm)
    source_faces = {loops[p].face.index for p in positions}
    visible_faces = core.visible_editable_faces(bm, context)
    faces = core.connected_uv_faces(bm, uvl, source_faces) & visible_faces
    del bm, uvl, loops
    try:
        _, _, stats = _solve_and_record(
            context, obj, faces, fix_boundary,
            parameters=dict(parameters or {}, respect_uv_selection=False))
    except Exception:
        return {'CANCELLED'}
    return {'CANCELLED'} if stats.get('rejected') else {'FINISHED'}


def _apply_constraint_edit(context, positions, kind, shape_positions=()):
    """Apply the stored redo targets before solving, in the same undo step."""
    obj = _edit_mesh_object(context)
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    if not positions and not shape_positions:
        return False
    if positions:
        if min(positions)<0 or max(positions)>=sum(len(f.loops) for f in bm.faces):return False
        # Adding custom data invalidates element references; acquire them afterwards.
        core.constraint_layer(bm, create=True)
        core.prepare_bm(bm)
        layer = core.constraint_layer(bm)
        loops = core.loops_in_order(bm)
        for position in positions:loops[position][layer] = kind
        bmesh.update_edit_mesh(obj.data)
        core.store_constraints(obj, bm)
        del layer,loops
    overlay.bump_revision()
    core.invalidate_stats_cache()
    del bm
    if shape_positions:
        from . import shape_constraints
        shape_constraints.clear(obj,shape_positions)
    return True


def _dispatch_constraint_edit(operator, context, positions, kind, fix_boundary=False, shape_positions=()):
    global _timing_deferred
    # Dispatchers do not edit data or own undo. The registered Optimize operator
    # owns both the mark and solve, so F9 replays both from the pre-mark state.
    auto = _auto_optimize(context)
    operation = (bpy.ops.uv.uvck_optimize if auto
                 else bpy.ops.uv.uvck_edit_constraints)
    parameters = dict(constraint_positions=','.join(str(p) for p in sorted(positions)),
                      constraint_kind=kind,
                      shape_clear_positions=','.join(str(p) for p in sorted(shape_positions)))
    if auto:
        parameters['fix_boundary'] = fix_boundary
    previous = _timing_deferred
    _timing_deferred = previous or auto
    try:
        result = operation('EXEC_DEFAULT', True, **parameters)
    finally:
        _timing_deferred = previous
    if auto:
        # EXEC_DEFAULT consumes reports from the nested Optimize operator.
        # Report its final timing once through the visible marking operator.
        _report_mark_status(operator, result)
    return result


class _ConstrainBase:
    bl_options = set()
    kind = C_NONE

    @classmethod
    def poll(cls, context):
        return poll_uv_constraint(context)

    def execute(self, context):
        obj = _edit_mesh_object(context)
        bm = bmesh.from_edit_mesh(obj.data)
        core.prepare_bm(bm)
        positions = sorted(core.selected_uv_edges(context, bm))
        del bm
        if not positions:
            self.report({'WARNING'}, t('Select at least one edge in the UV editor first'))
            return {'CANCELLED'}
        return _dispatch_constraint_edit(self, context, positions, self.kind, self.fix_boundary)


class UVCK_OT_constrain_horizontal(_ConstrainBase, bpy.types.Operator):
    bl_idname = 'uv.uvck_constrain_horizontal'
    bl_label = 'Constrain Edge(s) Horizontally'
    bl_description = 'Constrain the selected edges so they stay horizontal during unwrapping and optimization (RizomUV: Constrain Edge(s) Horizontally)'
    kind = C_HORIZONTAL
    fix_boundary: BoolProperty(name='Fix boundary', default=False, options={'HIDDEN', 'SKIP_SAVE'})


class UVCK_OT_constrain_vertical(_ConstrainBase, bpy.types.Operator):
    bl_idname = 'uv.uvck_constrain_vertical'
    bl_label = 'Constrain Edge(s) Vertically'
    bl_description = 'Constrain the selected edges so they stay vertical during unwrapping and optimization (RizomUV: Constrain Edge(s) Vertically)'
    kind = C_VERTICAL
    fix_boundary: BoolProperty(name='Fix boundary', default=False, options={'HIDDEN', 'SKIP_SAVE'})


class UVCK_OT_constrain_aligned(_ConstrainBase, bpy.types.Operator):
    bl_idname = 'uv.uvck_constrain_aligned'
    bl_label = 'Constrain Edge(s) Aligned'
    bl_description = 'Constrain the selected edges to stay a straight line along their current trend instead of being forced horizontal or vertical (RizomUV: Constrain Edge(s) Aligned)'
    kind = C_ALIGNED
    fix_boundary: BoolProperty(name='Fix boundary', default=False, options={'HIDDEN', 'SKIP_SAVE'})


class UVCK_OT_unconstrain(bpy.types.Operator):
    bl_idname = 'uv.uvck_unconstrain'
    bl_label = 'Unconstrain Edge(s)'
    bl_description = 'Remove the constraints from the selected UV edges'
    bl_options = set()

    all_edges: BoolProperty(name='All edges', default=False, options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return poll_uv_constraint(context)

    def execute(self, context):
        obj = _edit_mesh_object(context)
        bm = bmesh.from_edit_mesh(obj.data)
        core.prepare_bm(bm)
        layer = core.constraint_layer(bm)
        loops = core.loops_in_order(bm)
        selected = [] if self.all_edges else core.selected_uv_edges(context, bm)
        if not selected and not self.all_edges:
            return {'CANCELLED'}
        targets = {p for p in (range(len(loops)) if self.all_edges else selected)
                   if layer and loops[p][layer] != C_NONE}
        from . import shape_constraints
        shape_targets=set()
        if bm.loops.layers.string.get(shape_constraints.LAYER) is not None:
            topology=core.UVTopology(bm,bm.loops.layers.uv.active)
            try:shape_targets=shape_constraints.touched_edges(topology,selected)
            except ValueError as exc:
                self.report({'WARNING'},t(str(exc)))
                return {'CANCELLED'}
            del topology
        del bm, layer, loops
        if not targets and not shape_targets:
            return {'CANCELLED'}
        return _dispatch_constraint_edit(self, context, targets, C_NONE, shape_positions=shape_targets)


class UVCK_OT_edit_constraints(bpy.types.Operator):
    """Undoable mark-only path when automatic optimization is disabled."""
    bl_idname = 'uv.uvck_edit_constraints'
    bl_label = 'UV Constraint'
    bl_options = {'UNDO', 'INTERNAL'}

    constraint_positions: StringProperty(default='', options={'HIDDEN', 'SKIP_SAVE'})
    shape_clear_positions: StringProperty(default='', options={'HIDDEN', 'SKIP_SAVE'})
    constraint_kind: IntProperty(default=C_NONE, min=C_NONE, max=C_ALIGNED,
                                 options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return poll_uv_constraint(context)

    def execute(self, context):
        positions = [int(p) for p in self.constraint_positions.split(',') if p]
        shape_positions=[int(p) for p in self.shape_clear_positions.split(',') if p]
        if not _apply_constraint_edit(context, positions, self.constraint_kind,shape_positions):
            return {'CANCELLED'}
        _redraw(context)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# optimize
# ---------------------------------------------------------------------------


class UVCK_OT_optimize(bpy.types.Operator):
    bl_idname = "uv.uvck_optimize"
    bl_label = "Optimize"
    bl_description = (
        "Re-solve the UV island they belong to, with the marked constraint "
        "edges held as hard constraints, to reduce distortion. The island's "
        "position, size and texel density are fitted back, so it neither grows "
        "nor lands on top of another island (RizomUV: Optimize)"
    )
    bl_options = {"REGISTER", "UNDO"}

    auto_iterations: BoolProperty(name="Auto iterations", default=True)
    unfold_iterations: IntProperty(name="Unfold iterations", default=1, min=1, max=10000)
    optimize_iterations: IntProperty(name="Optimize iterations", default=1, min=1, max=10000)
    strength: FloatProperty(name="Strength", default=1.0, min=0, max=1)
    angle_length: FloatProperty(name="Angles / Lengths", default=1.0, min=0, max=1)
    use_density_map: BoolProperty(name="Use density map", default=True)
    prevent_flips: BoolProperty(name="Prevent flips", default=False)
    prevent_overlap: BoolProperty(name="Prevent overlaps", default=False)
    overlap_space: FloatProperty(name="Overlap space", default=0.0, min=0, precision=4)
    align_borders: BoolProperty(name="Align borders", default=False)
    align_threshold: FloatProperty(name="Threshold", default=20.0, min=0, max=45)
    auto_orient: BoolProperty(name="Auto orient", default=True)
    fill_holes: BoolProperty(name="Fill holes", default=False)
    free_selection_borders: BoolProperty(name="Free selection borders", default=False)

    def solver_parameters(self):
        return dict(mix=self.strength, angle_distance_mix=self.angle_length,
            auto_iterations=self.auto_iterations, unfold_iterations=self.unfold_iterations,
            optimize_iterations=self.optimize_iterations, use_density_map=self.use_density_map,
            prevent_flips=self.prevent_flips, prevent_overlap=self.prevent_overlap,
            overlap_space=self.overlap_space, align_borders=self.align_borders,
            align_borders_angle=self.align_threshold, align_borders_pre_orient=self.auto_orient,
            fill_holes=self.fill_holes, free_selection_borders=self.free_selection_borders)

    def draw(self, context):
        layout=self.layout
        layout.use_property_split=True
        layout.use_property_decorate=False
        box=layout.box();box.label(text=t("Iterations"))
        box.prop(self,"auto_iterations",text=t("Auto iterations"))
        col=box.column();col.active=not self.auto_iterations
        col.prop(self,"unfold_iterations",text=t("Unfold iterations"));col.prop(self,"optimize_iterations",text=t("Optimize iterations"))
        box=layout.box();box.label(text=t("Stretch"))
        for name,label in (("strength","Strength"),("angle_length","Angles / Lengths"),("use_density_map","Use density map")):
            box.prop(self,name,text=t(label))
        box=layout.box();box.label(text=t("Integrity checks"))
        box.prop(self,"prevent_flips");box.prop(self,"prevent_overlap")
        col=box.column();col.active=self.prevent_overlap;col.prop(self,"overlap_space")
        box=layout.box();box.label(text=t("Boundary"))
        box.prop(self,"fix_boundary")
        box.prop(self,"align_borders")
        col=box.column();col.active=self.align_borders and not self.fix_boundary
        col.prop(self,"align_threshold");col.prop(self,"auto_orient")
        box.prop(self,"fill_holes");box.prop(self,"free_selection_borders")

    target_faces: StringProperty(default="", options={'HIDDEN', 'SKIP_SAVE'})

    # Frozen targets make F9 edits replay the original constraint action rather
    # than deriving a new action from a changed selection or optimized UVs.
    constraint_positions: StringProperty(default='', options={'HIDDEN', 'SKIP_SAVE'})
    shape_clear_positions: StringProperty(default='', options={'HIDDEN', 'SKIP_SAVE'})
    constraint_kind: IntProperty(default=C_NONE, min=C_NONE, max=C_ALIGNED,
                                 options={'HIDDEN', 'SKIP_SAVE'})

    fix_boundary: BoolProperty(
        name="Fix boundary",
        description="Hold the island's border UVs in place while optimizing, so "
                    "the outline stays exactly put and only the interior is "
                    "relaxed. Off by default. Turning it on equals RizomUV's "
                    "'Free selection border' left off, i.e. its border vertices "
                    "are pinned and do not move",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        return poll_uv_constraint(context)

    def execute(self, context):
        obj = _edit_mesh_object(context)
        _set_last_optimize_result(obj, "RUNNING")
        if self.constraint_positions or self.shape_clear_positions:
            positions = [int(p) for p in self.constraint_positions.split(',') if p]
            shape_positions=[int(p) for p in self.shape_clear_positions.split(',') if p]
            if not _apply_constraint_edit(context, positions, self.constraint_kind,shape_positions):
                return {'CANCELLED'}
            result = _auto_optimize_edges(context, self.fix_boundary, sorted(set(positions)|set(shape_positions)),
                                          self.solver_parameters())
            _report_mark_status(self, result)
            _redraw(context)
            # A rejected solve preserves the mark. FINISHED keeps it undoable.
            return {'FINISHED'}
        # Resolving target islands needs edge adjacency, not UV vertex groups.
        # The checked solve builds the latter once for the complete mesh.
        bm = bmesh.from_edit_mesh(obj.data)
        core.prepare_bm(bm)
        uvl = bm.loops.layers.uv.active
        if uvl is None:
            _set_last_optimize_result(obj, "CANCELLED", "no UV layer")
            self.report({"WARNING"}, t("This mesh has no UV map"))
            return {"CANCELLED"}

        # Resolve UV selection inside the visible scope; with no UV selection,
        # optimize the visible mesh-selection scope (or all visible faces in sync mode).
        if self.target_faces:
            requested = {int(i) for i in self.target_faces.split(",")}
            if any(i < 0 or i >= len(bm.faces) for i in requested):
                _set_last_optimize_result(obj, "CANCELLED", "invalid target face index")
                return {'CANCELLED'}
            faces = requested
        else:
            sync = context.scene.tool_settings.use_uv_select_sync
            visible_faces = core.visible_editable_faces(bm, context)
            selected_faces = set()
            for face in bm.faces:
                if face.index not in visible_faces or (not sync and not face.select):
                    continue
                if any(loop.uv_select_edge or loop.uv_select_vert
                       for loop in face.loops):
                    selected_faces.add(face.index)
            if not selected_faces and sync:
                selected_faces = {f.index for f in bm.faces
                                  if f.index in visible_faces and f.select}
            if selected_faces:
                # The flood may cross into hidden neighbors; keep this action
                # within the visible, editable part of the current edit mesh.
                faces = core.connected_uv_faces(bm, uvl, selected_faces) & visible_faces
            else:
                faces = set(visible_faces)

        if not faces:
            del bm, uvl
            _set_last_optimize_result(obj, "CANCELLED", "no visible face selection")
            self.report({"WARNING"}, t("No visible selected UV or mesh faces to optimize"))
            return {"CANCELLED"}

        del bm, uvl

        try:
            snapped, flips, stats = _solve_and_record(
                context, obj, faces, fix_boundary=self.fix_boundary,
                parameters=dict(self.solver_parameters(),
                                respect_uv_selection=not bool(self.target_faces)))
        except Exception as exc:
            self.report({'WARNING'},
                        _elapsed_text(last_optimize_result['elapsed_seconds']))
            _redraw(context)
            return {'CANCELLED'}
        if stats.get('rejected'):
            self.report({'WARNING'}, _elapsed_text(stats['elapsed_seconds']))
            _redraw(context)
            return {'CANCELLED'}
        self.report({"INFO"}, _elapsed_text(stats['elapsed_seconds']))
        _redraw(context)
        return {"FINISHED"}


CLASSES = (
    UVCK_OT_constrain_horizontal,
    UVCK_OT_constrain_vertical,
    UVCK_OT_constrain_aligned,
    UVCK_OT_unconstrain,
    UVCK_OT_edit_constraints,
    UVCK_OT_optimize,
)
