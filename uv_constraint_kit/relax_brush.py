# SPDX-License-Identifier: GPL-3.0-or-later
"""Whole-island smooth-falloff constraint-preserving UV relax brush."""

import math

import bmesh
import bpy
import gpu
from gpu_extras.batch import batch_for_shader
import numpy as np
from bpy.props import BoolProperty, FloatProperty, IntProperty

from . import circle_ops, core, metric, ops, overlay, safety, shape_constraints
from .const import C_ALIGNED, C_HORIZONTAL, C_VERTICAL
from .i18n import t


def _project_line(points):
    """Orthogonally project a (K, 2) point array onto its least-squares line."""
    mean = points.mean(axis=0)
    centered = points - mean
    cov = centered.T @ centered
    eigvals, eigvecs = np.linalg.eigh(cov)
    direction = eigvecs[:, int(np.argmax(eigvals))]
    norm = float(np.linalg.norm(direction))
    if norm <= 1e-15:
        return points
    direction = direction / norm
    proj = centered @ direction
    return mean + proj[:, None] * direction[None, :]


class _IslandBrushState:
    """Cached per-island geometry, constraints, pins, and safety guards for brush painting."""

    def __init__(self, obj, island, fix_boundary, prevent_overlap, respect_pins):
        bm = bmesh.from_edit_mesh(obj.data)
        core.prepare_bm(bm)
        uvl = bm.loops.layers.uv.active
        topo = core.UVTopology(bm, uvl)
        island = set(island)
        topo.vgroup_loops = {
            g: [p for p in positions if topo.loops[p].face.index in island]
            for g, positions in topo.vgroup_loops.items()
        }
        topo.vgroup_loops = {g: ps for g, ps in topo.vgroup_loops.items() if ps}
        geometry = metric._prepare_geometry(core, topo, island)
        if geometry is None:
            raise ValueError("Island has no valid triangles")

        self.obj = obj
        self.island = island
        self.topo = topo
        self.geometry = geometry
        self.groups = geometry["groups"]
        self.index = geometry["index"]
        self.tri = geometry["tri"]
        self.write_loops = geometry["write_loops"]
        self.write_idx = geometry["write_idx"]
        self.coords = np.asarray(
            [tuple(float(c) for c in topo.vgroup_uv(g)) for g in self.groups],
            dtype=np.float64,
        )

        # Gather H/V/Aligned constraint runs mapped into local vertex indices.
        positions = core.constrained_loops(bm, only_faces=island)
        self.runs = core.constraint_runs(bm, topo, positions)
        self.run_indices = []
        for kind, pos_list in self.runs:
            members = sorted({
                self.index[g]
                for p in pos_list
                for g in topo.loop_edge_vgroups(p)
                if g in self.index
            })
            if len(members) >= 2:
                self.run_indices.append((kind, np.asarray(members, dtype=np.int64)))

        # Gather fixed vertices: user pins, existing shape constraints, optional border.
        fixed_base = {}
        for g, uv in shape_constraints.fixed(topo, island).items():
            if g in self.index:
                fixed_base[g] = (float(uv[0]), float(uv[1]))
        if respect_pins:
            for g, ps in topo.vgroup_loops.items():
                if g in self.index and any(topo.loops[p][uvl].pin_uv for p in ps):
                    uv0 = topo.vgroup_uv(g)
                    fixed_base.setdefault(g, (float(uv0.x), float(uv0.y)))
        if fix_boundary:
            for p in core.selection_border_loops(topo, island):
                for g in topo.loop_edge_vgroups(p):
                    if g in self.index:
                        uv0 = topo.vgroup_uv(g)
                        fixed_base.setdefault(g, (float(uv0.x), float(uv0.y)))

        self.fixed_base = fixed_base
        self.fixed_mask = np.zeros(len(self.groups), dtype=bool)
        for g in fixed_base:
            self.fixed_mask[self.index[g]] = True

        self.probe = safety.AcceptanceProbe(core, topo, island, check_other_islands=prevent_overlap)
        p = self.coords[self.tri]
        self.orig_signs = (p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1]) - (p[:, 1, 1] - p[:, 0, 1]) * (p[:, 2, 0] - p[:, 0, 0])
        self.oriented = np.abs(self.orig_signs) > 2e-14
        self.area_before = core.island_area(topo, island)
        self.dirty = False

    def touches_brush(self, center_uv, radius_uv, reach_scale=2.5):
        cu, cv = float(center_uv[0]), float(center_uv[1])
        ru = max(float(radius_uv[0]), 1e-9) * reach_scale
        rv = max(float(radius_uv[1]), 1e-9) * reach_scale
        u_min, v_min = float(np.min(self.coords[:, 0])), float(np.min(self.coords[:, 1]))
        u_max, v_max = float(np.max(self.coords[:, 0])), float(np.max(self.coords[:, 1]))
        return (u_min - ru <= cu <= u_max + ru) and (v_min - rv <= cv <= v_max + rv)

    def _write_coords(self, coords):
        uvl = self.topo.uvl
        for loop, uv in zip(self.write_loops, coords[self.write_idx].tolist()):
            loop[uvl].uv = uv

    def dab(self, center_uv, radius_uv, strength=0.5, step_iters=8,
            prevent_overlap=False, use_density_map=True):
        """Apply one whole-island smooth Gaussian falloff relaxation dab."""
        if not self.touches_brush(center_uv, radius_uv):
            return False
        cu, cv = float(center_uv[0]), float(center_uv[1])
        ru = max(float(radius_uv[0]), 1e-9)
        rv = max(float(radius_uv[1]), 1e-9)
        dx = (self.coords[:, 0] - cu) / ru
        dy = (self.coords[:, 1] - cv) / rv
        d2 = dx * dx + dy * dy
        s = max(0.0, min(1.0, float(strength)))
        w = s * np.exp(-1.5 * d2)
        w[w < 1e-4] = 0.0
        w[self.fixed_mask] = 0.0
        if float(np.max(w)) <= 1e-4:
            return False

        solved_count, _flips, target = metric.solve(
            core, self.topo, self.island, self.fixed_base, self.runs,
            max(1, int(step_iters)), angle_length=0.5, auto=False,
            prevent_flips=True, prevent_overlap=prevent_overlap,
            use_density_map=use_density_map,
            _geometry=self.geometry, _coordinates=self.coords,
        )
        if not solved_count or not np.all(np.isfinite(target)):
            return False

        delta = target - self.coords
        if float(np.max(np.abs(delta))) <= 1e-10:
            return False

        # Preserve exact H/V/Aligned collinearity by equalizing weights along constrained axes.
        wx = w.copy()
        wy = w.copy()
        aligned_active = []
        for kind, members in self.run_indices:
            if kind == C_HORIZONTAL:
                wy[members] = 0.0 if np.any(self.fixed_mask[members]) else float(np.mean(w[members]))
            elif kind == C_VERTICAL:
                wx[members] = 0.0 if np.any(self.fixed_mask[members]) else float(np.mean(w[members]))
            elif kind == C_ALIGNED:
                if np.any(self.fixed_mask[members]):
                    wx[members] = 0.0
                    wy[members] = 0.0
                else:
                    wr = float(np.mean(w[members]))
                    wx[members] = wr
                    wy[members] = wr
                    if wr > 1e-4 and len(members) >= 3:
                        aligned_active.append(members)

        for alpha in (1.0, 0.5, 0.25, 0.125, 0.0625):
            cand = self.coords.copy()
            cand[:, 0] += alpha * wx * delta[:, 0]
            cand[:, 1] += alpha * wy * delta[:, 1]
            for members in aligned_active:
                cand[members] = _project_line(cand[members])
            cand[self.fixed_mask] = self.coords[self.fixed_mask]

            if not np.all(np.isfinite(cand)):
                continue
            p = cand[self.tri]
            cross = (p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1]) - (p[:, 1, 1] - p[:, 0, 1]) * (p[:, 2, 0] - p[:, 0, 0])
            if np.any(self.orig_signs[self.oriented] * cross[self.oriented] <= 1e-14):
                continue

            self._write_coords(cand)
            if prevent_overlap:
                ov = self.probe.check_overlaps(self.topo)
                if ov.get("novel_self_overlaps") or ov.get("novel_island_overlaps"):
                    self._write_coords(self.coords)
                    continue

            self.coords = cand
            self.dirty = True
            return True
        return False

    def finish_stroke(self, preserve_area=False):
        if not self.dirty:
            return
        if preserve_area and self.area_before > 1e-14:
            if core._restore_anchored_area(self.topo, self.island, self.fixed_base, self.runs, self.area_before):
                self.coords = np.asarray(
                    [tuple(float(c) for c in self.topo.vgroup_uv(g)) for g in self.groups],
                    dtype=np.float64,
                )
        self.dirty = False


def apply_dab(context, obj, center_uv, radius_uv, strength=0.5, iterations=8,
              prevent_overlap=False, fix_boundary=False, use_density_map=True,
              respect_pins=True, preserve_area=False):
    """Apply one deterministic whole-island falloff relax dab (script/test friendly)."""
    from . import live_symmetry
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    uvl = bm.loops.layers.uv.active
    if uvl is None:
        del bm
        return dict(changed=0, islands=0)
    visible = core.visible_editable_faces(bm, context)
    visible = live_symmetry.source_faces(obj, visible)
    topo_all = core.UVTopology(bm, uvl)
    islands = [set(g) for g in core.face_islands(topo_all, visible).values()]
    del topo_all, bm, uvl

    changed = 0
    for island in islands:
        try:
            state = _IslandBrushState(obj, island, fix_boundary, prevent_overlap, respect_pins)
        except ValueError:
            continue
        if state.dab(center_uv, radius_uv, strength=strength, step_iters=iterations,
                     prevent_overlap=prevent_overlap, use_density_map=use_density_map):
            state.finish_stroke(preserve_area=preserve_area)
            changed += 1
        del state

    if changed > 0:
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        live_symmetry.sync(context)
        overlay.bump_revision()
        core.invalidate_stats_cache()
        overlay.request_redraw()
    return dict(changed=changed, islands=len(islands))


class UVCK_OT_relax_brush(bpy.types.Operator):
    bl_idname = "uv.uvck_relax_brush"
    bl_label = "Relax Brush"
    bl_description = (
        "Paint in the UV editor to smoothly relax UV distortion with whole-island "
        "distance falloff while preserving constraints, pins, and flip-free safety"
    )
    bl_options = {"REGISTER", "UNDO"}

    radius_px: FloatProperty(
        name="Brush radius (px)",
        description="Brush radius in screen pixels (adjust with Wheel or [ / ])",
        default=60.0,
        min=10.0,
        max=400.0,
    )
    strength: FloatProperty(
        name="Brush strength",
        description="Relaxation strength per brush dab (adjust with Shift+Wheel)",
        default=0.5,
        min=0.05,
        max=1.0,
        precision=2,
    )
    step_iterations: IntProperty(
        name="Step iterations",
        description="Solver iterations per brush dab",
        default=8,
        min=1,
        max=64,
    )
    prevent_overlap: BoolProperty(name="Prevent overlaps", default=False)
    fix_boundary: BoolProperty(name="Fix boundary", default=False)
    use_density_map: BoolProperty(name="Use density map", default=True)
    respect_pins: BoolProperty(name="Respect pins", default=True)
    preserve_area: BoolProperty(
        name="Preserve island area",
        description="Restore original island UV area at the end of each stroke",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        return bool(
            circle_ops._poll(context)
            and space is not None
            and space.type == "IMAGE_EDITOR"
            and space.show_uvedit
        )

    def _uv_window_region(self, context):
        area = getattr(context, "area", None)
        if area is not None:
            for reg in area.regions:
                if reg.type == "WINDOW":
                    return reg
        return getattr(context, "region", None)

    def _uv_cursor_and_radius(self, context, event):
        region = self._uv_window_region(context)
        view2d = getattr(region, "view2d", None)
        if view2d is None:
            return None, None
        rx = event.mouse_x - region.x
        ry = event.mouse_y - region.y
        cu, cv = view2d.region_to_view(rx, ry)
        u1, v1 = view2d.region_to_view(rx + self.radius_px, ry + self.radius_px)
        ru = max(abs(u1 - cu), 1e-6)
        rv = max(abs(v1 - cv), 1e-6)
        return (cu, cv), (ru, rv)

    def _draw_cursor(self):
        center = getattr(self, "_cursor_uv", None)
        radius = getattr(self, "_radius_uv", None)
        if center is None or radius is None:
            return
        cu, cv = center
        ru, rv = radius
        segs = 48
        ring = [
            (cu + ru * math.cos(2.0 * math.pi * i / segs),
             cv + rv * math.sin(2.0 * math.pi * i / segs))
            for i in range(segs)
        ]
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        old_blend = gpu.state.blend_get()
        old_width = gpu.state.line_width_get()
        try:
            gpu.state.blend_set("ALPHA")
            gpu.state.line_width_set(2.0 if self._painting else 1.5)
            batch = batch_for_shader(shader, "LINE_LOOP", {"pos": ring})
            color = (0.2, 0.9, 1.0, 0.9) if self._painting else (1.0, 1.0, 1.0, 0.7)
            shader.uniform_float("color", color)
            batch.draw(shader)
        finally:
            gpu.state.line_width_set(old_width)
            gpu.state.blend_set(old_blend)

    def _set_header(self, context):
        area = getattr(context, "area", None)
        if area is not None:
            area.header_text_set(
                t("Relax Brush | Radius: %.0f px ([/] or Wheel) | Strength: %.2f (Shift+Wheel) | LMB: Paint | RMB/Enter: Finish | Esc: Cancel")
                % (self.radius_px, self.strength)
            )

    def _cleanup(self, context):
        handle = getattr(self, "_draw_handle", None)
        if handle is not None:
            try:
                bpy.types.SpaceImageEditor.draw_handler_remove(handle, "WINDOW")
            except Exception:
                pass
            self._draw_handle = None
        area = getattr(context, "area", None)
        if area is not None:
            area.header_text_set(None)
        self._island_states = {}
        overlay.request_redraw()

    def _restore_all(self):
        for obj, saved in self._session_snapshots:
            bm = bmesh.from_edit_mesh(obj.data)
            core.prepare_bm(bm)
            uvl = bm.loops.layers.uv.active
            if uvl is None:
                del bm
                continue
            loops = core.loops_in_order(bm)
            for p, uv in enumerate(saved):
                loops[p][uvl].uv = uv
            del loops, uvl, bm
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)

    def _prepare_session(self, context):
        from . import live_symmetry
        self._session_snapshots = []
        self._island_specs = []
        self._island_states = {}
        for obj in circle_ops._edit_meshes(context):
            if not obj.data.uv_layers:
                continue
            bm = bmesh.from_edit_mesh(obj.data)
            core.prepare_bm(bm)
            uvl = bm.loops.layers.uv.active
            if uvl is None:
                del bm
                continue
            loops = core.loops_in_order(bm)
            self._session_snapshots.append(
                (obj, [(float(lp[uvl].uv.x), float(lp[uvl].uv.y)) for lp in loops])
            )
            visible = core.visible_editable_faces(bm, context)
            visible = live_symmetry.source_faces(obj, visible)
            topo = core.UVTopology(bm, uvl)
            for idx, group in enumerate(core.face_islands(topo, visible).values()):
                island = set(group)
                pts = [
                    (float(topo.loops[p][uvl].uv.x), float(topo.loops[p][uvl].uv.y))
                    for p in range(len(topo.loops))
                    if topo.loops[p].face.index in island
                ]
                if not pts:
                    continue
                bbox = (
                    min(p[0] for p in pts), min(p[1] for p in pts),
                    max(p[0] for p in pts), max(p[1] for p in pts),
                )
                self._island_specs.append(((obj.name, idx), obj, island, bbox))
            del topo, loops, uvl, bm

    def _paint_at(self, context, center_uv, radius_uv):
        from . import live_symmetry
        cu, cv = center_uv
        ru = max(radius_uv[0], 1e-9) * 2.5
        rv = max(radius_uv[1], 1e-9) * 2.5
        touched_objs = set()
        for key, obj, island, bbox in self._island_specs:
            state = self._island_states.get(key)
            if state is None:
                if not (bbox[0] - ru <= cu <= bbox[2] + ru and bbox[1] - rv <= cv <= bbox[3] + rv):
                    continue
                try:
                    state = _IslandBrushState(
                        obj, island,
                        fix_boundary=self.fix_boundary,
                        prevent_overlap=self.prevent_overlap,
                        respect_pins=self.respect_pins,
                    )
                except ValueError:
                    continue
                self._island_states[key] = state
            if state.dab(
                center_uv, radius_uv,
                strength=self.strength,
                step_iters=self.step_iterations,
                prevent_overlap=self.prevent_overlap,
                use_density_map=self.use_density_map,
            ):
                touched_objs.add(obj)
                self._any_changed = True

        for obj in touched_objs:
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        if touched_objs:
            live_symmetry.sync(context)
            overlay.request_redraw_areas_only()

    def invoke(self, context, event):
        ops._apply_preset_defaults(self, context, ("prevent_overlap", "fix_boundary", "use_density_map"))
        self._prepare_session(context)
        if not self._island_specs:
            self.report({"WARNING"}, t("No visible editable UV islands for Relax Brush"))
            return {"CANCELLED"}
        self._painting = False
        self._any_changed = False
        self._last_paint_px = None
        self._cursor_uv, self._radius_uv = self._uv_cursor_and_radius(context, event)
        self._draw_handle = bpy.types.SpaceImageEditor.draw_handler_add(
            self._draw_cursor, (), "WINDOW", "POST_VIEW"
        )
        self._set_header(context)
        context.window_manager.modal_handler_add(self)
        overlay.request_redraw_areas_only()
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "MIDDLEMOUSE":
            return {"PASS_THROUGH"}

        if event.type in {"WHEELUPMOUSE", "RIGHT_BRACKET"} and (event.type == "WHEELUPMOUSE" or event.value == "PRESS"):
            if event.shift:
                self.strength = min(1.0, round(self.strength + 0.05, 2))
            else:
                self.radius_px = min(400.0, self.radius_px * 1.15)
            self._cursor_uv, self._radius_uv = self._uv_cursor_and_radius(context, event)
            self._set_header(context)
            overlay.request_redraw_areas_only()
            return {"RUNNING_MODAL"}

        if event.type in {"WHEELDOWNMOUSE", "LEFT_BRACKET"} and (event.type == "WHEELDOWNMOUSE" or event.value == "PRESS"):
            if event.shift:
                self.strength = max(0.05, round(self.strength - 0.05, 2))
            else:
                self.radius_px = max(10.0, self.radius_px / 1.15)
            self._cursor_uv, self._radius_uv = self._uv_cursor_and_radius(context, event)
            self._set_header(context)
            overlay.request_redraw_areas_only()
            return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE":
            if event.value == "PRESS":
                self._painting = True
                self._cursor_uv, self._radius_uv = self._uv_cursor_and_radius(context, event)
                if self._cursor_uv is not None:
                    self._last_paint_px = (event.mouse_x, event.mouse_y)
                    self._paint_at(context, self._cursor_uv, self._radius_uv)
                overlay.request_redraw_areas_only()
            elif event.value == "RELEASE" and self._painting:
                self._painting = False
                self._last_paint_px = None
                dirty_objs = set()
                for state in self._island_states.values():
                    if state.dirty:
                        state.finish_stroke(preserve_area=self.preserve_area)
                        dirty_objs.add(state.obj)
                for obj in dirty_objs:
                    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
                if dirty_objs:
                    from . import live_symmetry
                    live_symmetry.sync(context)
                overlay.request_redraw_areas_only()
            return {"RUNNING_MODAL"}

        if event.type == "MOUSEMOVE":
            self._cursor_uv, self._radius_uv = self._uv_cursor_and_radius(context, event)
            if self._painting and self._cursor_uv is not None:
                px = (event.mouse_x, event.mouse_y)
                spacing = max(3.0, 0.15 * self.radius_px)
                if (self._last_paint_px is None or
                        math.hypot(px[0] - self._last_paint_px[0], px[1] - self._last_paint_px[1]) >= spacing):
                    self._last_paint_px = px
                    self._paint_at(context, self._cursor_uv, self._radius_uv)
            overlay.request_redraw_areas_only()
            return {"RUNNING_MODAL"}

        if event.type == "ESC" and event.value == "PRESS":
            self._restore_all()
            self._cleanup(context)
            return {"CANCELLED"}

        if event.type in {"RIGHTMOUSE", "RET", "NUMPAD_ENTER", "SPACE"} and event.value == "PRESS":
            if self._painting:
                for state in self._island_states.values():
                    if state.dirty:
                        state.finish_stroke(preserve_area=self.preserve_area)
                        bmesh.update_edit_mesh(state.obj.data, loop_triangles=False, destructive=False)
            self._cleanup(context)
            if self._any_changed:
                from . import live_symmetry
                live_symmetry.sync(context)
                overlay.bump_revision()
                core.invalidate_stats_cache()
                overlay.request_redraw()
            return {"FINISHED"}

        return {"RUNNING_MODAL"}


CLASSES = (UVCK_OT_relax_brush,)
