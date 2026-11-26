# SPDX-License-Identifier: GPL-3.0-or-later
"""Interactive preview drag with confirm-time high-precision constrained solve."""

import math
from time import perf_counter

import bmesh
import bpy
import numpy as np
from bpy.props import BoolProperty, FloatVectorProperty, IntProperty

from . import circle, circle_ops, core, metric, ops, overlay, safety, shape_constraints
from .i18n import t


def _selected_uv_loop_positions(context, bm, uvl):
    """Loop positions whose UV corner is explicitly selected in the active mode."""
    visible = core.visible_editable_faces(bm, context)
    sync = context.scene.tool_settings.use_uv_select_sync
    mode = core.uv_selection_mode(context)
    loops = core.loops_in_order(bm)
    loop_pos = {lp: i for i, lp in enumerate(loops)}
    valid = (not sync) or bm.uv_select_sync_valid
    selected = set()
    if valid:
        for i, lp in enumerate(loops):
            fi = lp.face.index
            if fi not in visible or (not sync and not lp.face.select):
                continue
            if mode == "VERTEX":
                if lp.uv_select_vert:
                    selected.add(i)
            elif mode == "EDGE":
                if lp.uv_select_edge:
                    selected.add(i)
                    selected.add(loop_pos[lp.link_loop_next])
            else:
                if lp.face.uv_select:
                    selected.add(i)
    if selected or not sync:
        return selected

    for i, lp in enumerate(loops):
        fi = lp.face.index
        if fi not in visible:
            continue
        if mode == "FACE":
            if lp.face.select:
                selected.add(i)
        elif mode == "EDGE":
            if not lp.edge.hide and lp.edge.select:
                selected.add(i)
                selected.add(loop_pos[lp.link_loop_next])
        else:
            if lp.vert.select:
                selected.add(i)
    return selected


def _island_drag_targets(context, obj):
    """Collect visible source-island UV vertex groups and preview loop records."""
    from . import live_symmetry
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    uvl = bm.loops.layers.uv.active
    if uvl is None:
        del bm
        return None
    sel_loops = _selected_uv_loop_positions(context, bm, uvl)
    if not sel_loops:
        del bm, uvl
        return None
    topo = core.UVTopology(bm, uvl)
    visible = core.visible_editable_faces(bm, context)
    seed_faces = {topo.loops[p].face.index for p in sel_loops}
    touched = core.connected_uv_faces(bm, uvl, seed_faces) & visible
    touched = live_symmetry.source_faces(obj, touched)
    if not touched:
        del topo, bm, uvl
        return None

    selected_groups = {
        topo.loop_to_vgroup[p]
        for p in sel_loops
        if topo.loops[p].face.index in touched
    }
    preview_loops = []
    for g in sorted(selected_groups):
        for p in topo.vgroup_loops[g]:
            if topo.loops[p].face.index in touched:
                uv = topo.loops[p][uvl].uv
                preview_loops.append((p, float(uv.x), float(uv.y)))
    snapshot = core._loop_uv_snapshot(topo, touched)
    del topo, bm, uvl
    if not preview_loops:
        return None
    return dict(
        obj=obj,
        touched=touched,
        selected_groups=selected_groups,
        preview_loops=preview_loops,
        snapshot=snapshot,
    )


def constrained_drag(context, obj, offset, iterations=150,
                     fix_boundary=False, prevent_overlap=False,
                     use_density_map=True, respect_pins=True,
                     preserve_area=False):
    """Apply a confirmed UV drag offset with high-precision constraint solving."""
    du, dv = float(offset[0]), float(offset[1])
    result = dict(changed=0, faces=0, skipped=[], islands=[])
    if not (math.isfinite(du) and math.isfinite(dv)):
        result["skipped"].append("Drag offset must be finite")
        return result
    if abs(du) <= 1e-12 and abs(dv) <= 1e-12:
        return result

    from . import live_symmetry
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    uvl = bm.loops.layers.uv.active
    if uvl is None:
        del bm
        result["skipped"].append("Mesh has no UV map")
        return result

    sel_loops = _selected_uv_loop_positions(context, bm, uvl)
    if not sel_loops:
        del bm, uvl
        return result

    topo_all = core.UVTopology(bm, uvl)
    visible = core.visible_editable_faces(bm, context)
    seed_faces = {topo_all.loops[p].face.index for p in sel_loops}
    touched = core.connected_uv_faces(bm, uvl, seed_faces) & visible
    touched = live_symmetry.source_faces(obj, touched)
    if not touched:
        del topo_all, bm, uvl
        return result

    islands = list(core.face_islands(topo_all, touched).values())
    sel_by_island = []
    for island in islands:
        island_set = set(island)
        dragged = {
            topo_all.loop_to_vgroup[p]
            for p in sel_loops
            if topo_all.loops[p].face.index in island_set
        }
        if dragged:
            sel_by_island.append((island_set, dragged))
    del topo_all

    for island, _dragged_hint in sel_by_island:
        topo = core.UVTopology(bm, uvl)
        topo.vgroup_loops = {
            g: [p for p in positions if topo.loops[p].face.index in island]
            for g, positions in topo.vgroup_loops.items()
        }
        topo.vgroup_loops = {g: positions for g, positions in topo.vgroup_loops.items() if positions}
        dragged_groups = {
            topo.loop_to_vgroup[p]
            for p in sel_loops
            if topo.loops[p].face.index in island
        }
        snapshot = core._loop_uv_snapshot(topo, island)
        try:
            if any(bm.faces[fi].hide for fi in island):
                raise ValueError("The island contains hidden faces")
            probe = safety.AcceptanceProbe(core, topo, island, check_other_islands=prevent_overlap)
            before = probe.measure(topo)
            signs = circle._triangle_signs(topo, island)
            oriented = np.abs(signs) > 2e-14
            area_before = core.island_area(topo, island)
            if area_before <= 1e-14 or not before["finite"]:
                raise ValueError("UV island has zero area")

            all_groups = set(topo.vgroup_loops)
            existing_shapes = shape_constraints.fixed(topo, island)
            held = {}
            for g in dragged_groups:
                uv0 = topo.vgroup_uv(g)
                held[g] = (float(uv0.x) + du, float(uv0.y) + dv)

            # Fast rigid-translation path when the entire island is dragged without fixed shapes/borders.
            if dragged_groups == all_groups and not fix_boundary and not existing_shapes:
                core.land_uv_groups(topo, held, island)
                after = probe.measure(topo)
                after.update(probe.check_overlaps(topo))
                if (not after["finite"] or after["zero_edges"] or
                        after["novel_self_overlaps"] or
                        (prevent_overlap and after["novel_island_overlaps"])):
                    raise ValueError("Dragged UV layout failed safety or overlap checks")
                result["changed"] += 1
                result["faces"] += len(island)
                result["islands"].append(dict(faces=sorted(island), mode="RIGID", before=before, after=after))
                continue

            for g, uv in existing_shapes.items():
                if g in held and math.dist(held[g], uv) > 1e-6:
                    raise ValueError("Dragged vertices conflict with an existing shape constraint")
                held[g] = uv

            if fix_boundary:
                border_groups = {
                    g for p in core.selection_border_loops(topo, island)
                    for g in topo.loop_edge_vgroups(p)
                }
                for g in border_groups:
                    if g not in dragged_groups:
                        uv0 = topo.vgroup_uv(g)
                        held.setdefault(g, (float(uv0.x), float(uv0.y)))

            if respect_pins:
                for g, positions in topo.vgroup_loops.items():
                    if g in dragged_groups:
                        continue
                    if any(topo.loops[p][uvl].pin_uv for p in positions):
                        uv0 = topo.vgroup_uv(g)
                        held.setdefault(g, (float(uv0.x), float(uv0.y)))

            positions = core.constrained_loops(bm, only_faces=island)
            runs = core.constraint_runs(bm, topo, positions)

            for triangle, sign in zip(core.triangles_for_faces(topo, island), signs):
                if abs(sign) > 2e-14 and all(g in held for g in triangle):
                    a, b, c = (held[g] for g in triangle)
                    target_sign = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
                    if sign * target_sign <= 0.0:
                        raise ValueError("Dragged vertices would flip or collapse a locked triangle")

            initial = {g: tuple(float(c) for c in topo.vgroup_uv(g)) for g in held}
            delta = {g: (uv[0] - initial[g][0], uv[1] - initial[g][1]) for g, uv in held.items()}
            field = circle._harmonic(topo, island, delta, displacement=True)
            geometry = metric._prepare_geometry(core, topo, island)
            groups = sorted(field)
            original = {g: tuple(float(c) for c in topo.vgroup_uv(g)) for g in groups}

            budget_total = max(1, int(iterations))
            for stages in (1, 16, 64):
                per_stage = max(4, int(math.ceil(budget_total / stages)))
                core.land_uv_groups(topo, original, island)
                complete = True
                for stage in range(1, stages + 1):
                    factor = stage / float(stages)
                    target = {
                        g: (initial[g][0] + delta[g][0] * factor,
                            initial[g][1] + delta[g][1] * factor)
                        for g in held
                    }
                    current = {g: tuple(float(c) for c in topo.vgroup_uv(g)) for g in groups}
                    moved = {
                        g: (uv[0] + field[g][0] / stages, uv[1] + field[g][1] / stages)
                        for g, uv in current.items()
                    }
                    moved.update(target)
                    core.land_uv_groups(topo, moved, island)
                    budget = budget_total if stages == 1 else per_stage
                    metric.solve(
                        core, topo, island, target, runs, budget,
                        angle_length=0.5, auto=False, prevent_flips=True,
                        prevent_overlap=prevent_overlap,
                        use_density_map=use_density_map,
                        _geometry=geometry,
                    )
                    current_signs = circle._triangle_signs(topo, island)
                    if (not np.isfinite(current_signs).all() or
                            np.any(signs[oriented] * current_signs[oriented] <= 0.0) or
                            any(math.dist(tuple(topo.vgroup_uv(g)), uv) > 1e-5
                                for g, uv in target.items())):
                        complete = False
                        break
                if complete:
                    break
            else:
                raise ValueError("Dragged UV layout could not resolve without flipping triangles")

            core.land_uv_groups(topo, held, island)
            if preserve_area:
                if not core._restore_anchored_area(topo, island, held, runs, area_before):
                    border = {
                        g for p in core.selection_border_loops(topo, island)
                        for g in topo.loop_edge_vgroups(p)
                    }
                    stationary = {
                        g: tuple(float(c) for c in topo.vgroup_uv(g))
                        for g in topo.vgroup_loops if g not in border
                    }
                    stationary.update(held)
                    if not core._restore_anchored_area(topo, island, stationary, runs, area_before):
                        raise ValueError("Dragged UV layout could not preserve the island area")

            after = probe.measure(topo)
            after.update(probe.check_overlaps(topo))
            final_signs = circle._triangle_signs(topo, island)
            if (np.any(signs[oriented] * final_signs[oriented] <= 0.0) or
                    not after["finite"] or after["zero_edges"] or
                    after["max_deviation"] > 0.05 or
                    after["degenerate"] > before["degenerate"] or
                    after["novel_self_overlaps"] or
                    (prevent_overlap and after["novel_island_overlaps"])):
                raise ValueError("Dragged UV layout conflicts with constraints or would overlap")
            if preserve_area and abs(core.island_area(topo, island) / area_before - 1.0) > 1e-5:
                raise ValueError("Dragged UV layout could not preserve the island area")

            result["changed"] += 1
            result["faces"] += len(island)
            result["islands"].append(dict(faces=sorted(island), mode="SOLVED", before=before, after=after))
        except Exception as error:
            for pos, uv in snapshot.items():
                topo.loops[pos][uvl].uv = uv
            if not isinstance(error, (ValueError, np.linalg.LinAlgError)):
                bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
                raise
            result["skipped"].append(str(error))
        finally:
            del topo

    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    del bm, uvl
    return result


class UVCK_OT_constrained_drag(bpy.types.Operator):
    bl_idname = "uv.uvck_constrained_drag"
    bl_label = "Constrained Drag"
    bl_description = (
        "Preview dragging selected UVs interactively, then run a high-precision "
        "constrained solve on confirm (LMB confirm, RMB/Esc cancel, X/Y lock axis)"
    )
    bl_options = {"REGISTER", "UNDO"}

    offset: FloatVectorProperty(
        name="Drag offset",
        description="UV translation applied to the selected handle vertices",
        size=2,
        default=(0.0, 0.0),
        precision=4,
    )
    iterations: IntProperty(name="Interior iterations", default=150, min=1, max=1000)
    fix_boundary: BoolProperty(name="Fix boundary", default=False)
    prevent_overlap: BoolProperty(name="Prevent overlaps", default=False)
    use_density_map: BoolProperty(name="Use density map", default=True)
    respect_pins: BoolProperty(name="Respect pins", default=True)
    preserve_area: BoolProperty(
        name="Preserve island area",
        description="Keep the island's original UV area after dragging",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        return circle_ops._poll(context)

    def _apply_preview(self, du, dv):
        for entry in self._targets:
            obj = entry["obj"]
            bm = bmesh.from_edit_mesh(obj.data)
            core.prepare_bm(bm)
            uvl = bm.loops.layers.uv.active
            if uvl is None:
                del bm
                continue
            loops = core.loops_in_order(bm)
            for p, u0, v0 in entry["preview_loops"]:
                loops[p][uvl].uv = (u0 + du, v0 + dv)
            del loops, uvl, bm
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        overlay.request_redraw_areas_only()

    def _restore_preview(self):
        for entry in self._targets:
            obj = entry["obj"]
            bm = bmesh.from_edit_mesh(obj.data)
            core.prepare_bm(bm)
            uvl = bm.loops.layers.uv.active
            if uvl is None:
                del bm
                continue
            loops = core.loops_in_order(bm)
            for p, uv in entry["snapshot"].items():
                loops[p][uvl].uv = uv
            del loops, uvl, bm
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)

    def _set_header(self, context, du, dv):
        area = getattr(context, "area", None)
        if area is None:
            return
        axis_tag = f" [{self._axis}]" if self._axis else ""
        area.header_text_set(
            t("Constrained Drag: dU %.4f, dV %.4f%s | LMB: Confirm & Solve | RMB/Esc: Cancel | X/Y: Lock axis")
            % (du, dv, axis_tag)
        )

    def _clear_header(self, context):
        area = getattr(context, "area", None)
        if area is not None:
            area.header_text_set(None)

    def _uv_window_region(self, context):
        area = getattr(context, "area", None)
        if area is not None:
            for reg in area.regions:
                if reg.type == "WINDOW":
                    return reg
        return getattr(context, "region", None)

    def _event_uv(self, context, event):
        region = self._uv_window_region(context)
        view2d = getattr(region, "view2d", None)
        if view2d is None:
            return None
        return view2d.region_to_view(event.mouse_x - region.x, event.mouse_y - region.y)

    def invoke(self, context, event):
        ops._apply_preset_defaults(self, context, ("prevent_overlap", "fix_boundary", "use_density_map"))
        space = getattr(context, "space_data", None)
        start_uv = self._event_uv(context, event)
        if start_uv is None or space is None or space.type != "IMAGE_EDITOR" or not space.show_uvedit:
            return self.execute(context)

        targets = []
        for obj in circle_ops._edit_meshes(context):
            if not obj.data.uv_layers:
                continue
            entry = _island_drag_targets(context, obj)
            if entry is not None:
                targets.append(entry)
        if not targets:
            self.report({"WARNING"}, t("Select UV vertices, edges, or faces to drag first"))
            return {"CANCELLED"}

        self._targets = targets
        self._start_uv = start_uv
        self._axis = None
        self._delta = (0.0, 0.0)
        self._set_header(context, 0.0, 0.0)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type in {"MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE"}:
            return {"PASS_THROUGH"}

        if event.type in {"X", "Y"} and event.value == "PRESS":
            self._axis = None if self._axis == event.type else event.type
            cur = self._event_uv(context, event)
            if cur is not None:
                du = 0.0 if self._axis == "Y" else (cur[0] - self._start_uv[0])
                dv = 0.0 if self._axis == "X" else (cur[1] - self._start_uv[1])
                self._delta = (du, dv)
                self._apply_preview(du, dv)
                self._set_header(context, du, dv)
            return {"RUNNING_MODAL"}

        if event.type == "MOUSEMOVE":
            cur = self._event_uv(context, event)
            if cur is not None:
                du = 0.0 if self._axis == "Y" else (cur[0] - self._start_uv[0])
                dv = 0.0 if self._axis == "X" else (cur[1] - self._start_uv[1])
                self._delta = (du, dv)
                self._apply_preview(du, dv)
                self._set_header(context, du, dv)
            return {"RUNNING_MODAL"}

        if event.type in {"RIGHTMOUSE", "ESC"} and event.value == "PRESS":
            self._restore_preview()
            self._clear_header(context)
            overlay.request_redraw()
            return {"CANCELLED"}

        if event.type in {"LEFTMOUSE", "RET", "NUMPAD_ENTER", "SPACE"} and event.value == "PRESS":
            self._restore_preview()
            self._clear_header(context)
            self.offset = self._delta
            return self.execute(context)

        return {"RUNNING_MODAL"}

    def execute(self, context):
        started = perf_counter()
        ops._apply_preset_defaults(self, context, ("prevent_overlap", "fix_boundary", "use_density_map"))
        du, dv = float(self.offset[0]), float(self.offset[1])
        if abs(du) <= 1e-12 and abs(dv) <= 1e-12:
            overlay.request_redraw()
            return {"FINISHED"}

        changed = 0
        skipped = []
        has_targets = False
        for obj in circle_ops._edit_meshes(context):
            if not obj.data.uv_layers:
                continue
            bm = bmesh.from_edit_mesh(obj.data)
            core.prepare_bm(bm)
            uvl = bm.loops.layers.uv.active
            has_sel = bool(uvl is not None and _selected_uv_loop_positions(context, bm, uvl))
            del bm, uvl
            if not has_sel:
                continue
            has_targets = True
            try:
                res = constrained_drag(
                    context, obj, (du, dv),
                    iterations=self.iterations,
                    fix_boundary=self.fix_boundary,
                    prevent_overlap=self.prevent_overlap,
                    use_density_map=self.use_density_map,
                    respect_pins=self.respect_pins,
                    preserve_area=self.preserve_area,
                )
            except Exception as exc:
                skipped.append(str(exc))
                continue
            changed += int(res.get("changed", 0))
            skipped.extend(res.get("skipped", ()))

        if changed > 0:
            from . import live_symmetry
            live_symmetry.sync(context)
            overlay.bump_revision()
            core.invalidate_stats_cache()
            overlay.request_redraw()
            self.report({"INFO"}, ops._elapsed_text(perf_counter() - started))
            if skipped:
                self.report({"WARNING"}, "; ".join(t(str(reason)) for reason in skipped))
            return {"FINISHED"}

        message = "; ".join(t(str(reason)) for reason in skipped)
        if not message:
            message = t("Select UV vertices, edges, or faces to drag first")
        self.report({"WARNING"}, message)
        overlay.request_redraw()
        return {"FINISHED"} if has_targets else {"CANCELLED"}


CLASSES = (UVCK_OT_constrained_drag,)
