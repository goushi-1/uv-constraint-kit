# SPDX-License-Identifier: GPL-3.0-or-later
"""One-shot UV edge chain spacing distribution with safe island relaxation."""

import math
from time import perf_counter

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty

from . import circle, circle_ops, core, ops, overlay
from .i18n import t


def _sample_polyline(points, cum_uv, target_s):
    """Linearly interpolate a point at arc length `target_s` along a polyline."""
    if target_s <= 0.0:
        return points[0]
    if target_s >= cum_uv[-1]:
        return points[-1]
    lo, hi = 0, len(cum_uv) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if cum_uv[mid] <= target_s:
            lo = mid
        else:
            hi = mid
    seg = cum_uv[lo + 1] - cum_uv[lo]
    if seg <= 1e-15:
        return points[lo]
    alpha = (target_s - cum_uv[lo]) / seg
    a, b = points[lo], points[lo + 1]
    return (a[0] + (b[0] - a[0]) * alpha, a[1] + (b[1] - a[1]) * alpha)


def _extract_chains(topo, island, selected_positions, respect_pins):
    """Decompose selected UV edges in `island` into maximal non-branching chains."""
    uvl = topo.uvl
    edge_lengths = {}
    for pos in sorted(selected_positions):
        loop = topo.loops[pos]
        if loop.face.index not in island:
            continue
        a, b = topo.loop_edge_vgroups(pos)
        if a == b:
            continue
        key = (a, b) if a < b else (b, a)
        if key not in edge_lengths:
            edge_lengths[key] = float(loop.edge.calc_length())

    if not edge_lengths:
        return []

    adj = {}
    for (a, b), length3d in edge_lengths.items():
        adj.setdefault(a, []).append((b, length3d))
        adj.setdefault(b, []).append((a, length3d))
    for node in adj:
        adj[node].sort(key=lambda item: item[0])

    pinned = set()
    if respect_pins:
        for node in adj:
            if any(topo.loops[p][uvl].pin_uv for p in topo.vgroup_loops.get(node, ())):
                pinned.add(node)

    split_nodes = {node for node, nbrs in adj.items() if len(nbrs) != 2 or node in pinned}
    visited_edges = set()
    chains = []

    def edge_key(u, v):
        return (u, v) if u < v else (v, u)

    def trace(start, nxt, first_len, closed_hint=False):
        visited_edges.add(edge_key(start, nxt))
        verts = [start, nxt]
        lengths = [first_len]
        prev, curr = start, nxt
        while curr not in split_nodes and not (closed_hint and curr == start):
            step = None
            for cand, cand_len in adj[curr]:
                ek = edge_key(curr, cand)
                if ek not in visited_edges:
                    step = (cand, cand_len, ek)
                    break
            if step is None:
                break
            cand, cand_len, ek = step
            visited_edges.add(ek)
            verts.append(cand)
            lengths.append(cand_len)
            prev, curr = curr, cand
            if curr == start:
                break
        is_closed = (verts[0] == verts[-1]) and (start not in pinned)
        return verts, lengths, is_closed

    for node in sorted(split_nodes):
        for nbr, length3d in adj[node]:
            if edge_key(node, nbr) not in visited_edges:
                chains.append(trace(node, nbr, length3d, closed_hint=False))

    for node in sorted(adj):
        for nbr, length3d in adj[node]:
            if edge_key(node, nbr) not in visited_edges:
                chains.append(trace(node, nbr, length3d, closed_hint=True))

    return chains


def _build_distribution(topo, island, mode, _aspect, selected_positions, respect_pins):
    if mode not in {"EVEN", "GEOMETRY"}:
        raise ValueError("Invalid edge distribution mode")
    chains = _extract_chains(topo, island, selected_positions, respect_pins)
    distributable = [chain for chain in chains if len(chain[1]) >= 2]
    if not distributable:
        raise ValueError("Select a UV edge chain of at least two connected edges")

    held = {}
    for verts, lengths3d, is_closed in distributable:
        k = len(lengths3d)
        pts = [tuple(float(c) for c in topo.vgroup_uv(v)) for v in verts]
        cum_uv = [0.0]
        for j in range(k):
            cum_uv.append(cum_uv[-1] + math.hypot(pts[j + 1][0] - pts[j][0],
                                                  pts[j + 1][1] - pts[j][1]))
        total_uv = cum_uv[-1]
        if not math.isfinite(total_uv) or total_uv <= 1e-12:
            raise ValueError("Selected UV edge chain has zero length")

        if mode == "EVEN":
            fractions = [i / float(k) for i in range(k + 1)]
        else:
            if any(not math.isfinite(d) or d <= 0.0 for d in lengths3d):
                raise ValueError("Selected edge chain contains a zero-length 3D edge")
            total_3d = sum(lengths3d)
            if not math.isfinite(total_3d) or total_3d <= 0.0:
                raise ValueError("Selected edge chain contains a zero-length 3D edge")
            cum = 0.0
            fractions = [0.0]
            for d in lengths3d:
                cum += d
                fractions.append(cum / total_3d)
            fractions[-1] = 1.0

        # Keep endpoints (and junction / anchor vertices) fixed; redistribute interior vertices.
        end_count = k if is_closed else k + 1
        for i in range(end_count):
            v = verts[i]
            target = pts[0] if i == 0 else (pts[-1] if (not is_closed and i == k)
                                            else _sample_polyline(pts, cum_uv, fractions[i] * total_uv))
            if v in held and math.dist(held[v], target) > 1e-6:
                raise ValueError("Selected UV edge chains conflict at a shared vertex")
            held[v] = target

    # Keep short 1-edge branches attached to selected junctions pinned in place too.
    for verts, lengths3d, _is_closed in chains:
        if len(lengths3d) < 2:
            for v in verts:
                held.setdefault(v, tuple(float(c) for c in topo.vgroup_uv(v)))

    # If the selection covers the island's entire outer border (with no border pins),
    # match the boundary's enclosed area so curved full-border redistribution can
    # preserve island area cleanly even with zero free boundary vertices.
    border_loops = core.selection_border_loops(topo, island)
    border_groups = {g for p in border_loops for g in topo.loop_edge_vgroups(p)}
    if border_groups and border_groups <= held.keys():
        uvl = topo.uvl
        has_border_pin = respect_pins and any(
            topo.loops[p][uvl].pin_uv
            for g in border_groups
            for p in topo.vgroup_loops.get(g, ())
        )
        if not has_border_pin:
            orig_area = sum(
                (topo.vgroup_uv(a).x * topo.vgroup_uv(b).y - topo.vgroup_uv(b).x * topo.vgroup_uv(a).y)
                for p in border_loops
                for a, b in (topo.loop_edge_vgroups(p),)
            ) * 0.5
            new_area = sum(
                (held[a][0] * held[b][1] - held[b][0] * held[a][1])
                for p in border_loops
                for a, b in (topo.loop_edge_vgroups(p),)
            ) * 0.5
            if abs(orig_area) > 1e-14 and orig_area * new_area > 0.0 and abs(new_area / orig_area - 1.0) > 1e-6:
                scale = math.sqrt(orig_area / new_area)
                cx = sum(held[g][0] for g in border_groups) / len(border_groups)
                cy = sum(held[g][1] for g in border_groups) / len(border_groups)
                held = {
                    g: (cx + (uv[0] - cx) * scale, cy + (uv[1] - cy) * scale)
                    for g, uv in held.items()
                }

    return held, dict(progressive_metric=True, mode=mode, chains=len(distributable))


def distribute_edges(context, obj, faces, mode="EVEN", iterations=150,
                     respect_pins=True, prevent_overlap=False,
                     selected_positions=None):
    """Redistribute vertices along selected UV edge chains and safely relax their islands."""
    if selected_positions is None:
        bm = bmesh.from_edit_mesh(obj.data)
        core.prepare_bm(bm)
        selected_positions = core.selected_uv_edges(context, bm)
        del bm
    selected_positions = set(selected_positions)
    if not selected_positions:
        return dict(changed=0, faces=0, skipped=["Select a UV edge chain of at least two connected edges"], islands=[])

    builder = (lambda topo, island, spacing, aspect:
               _build_distribution(topo, island, spacing, aspect, selected_positions, respect_pins))
    return circle._reshape(
        context, obj, faces, builder,
        spacing=mode,
        iterations=iterations,
        respect_pins=respect_pins,
        prevent_overlap=prevent_overlap,
        template_only=False,
    )


class UVCK_OT_distribute_edges(bpy.types.Operator):
    bl_idname = "uv.uvck_distribute_edges"
    bl_label = "Distribute Edges"
    bl_description = (
        "Redistribute vertices along selected UV edge chains evenly or by 3D "
        "edge lengths, then safely relax the rest of the island"
    )
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(
        name="Edge distribution",
        items=(
            ("EVEN", "Edge distribution: Even", "Space vertices evenly along each selected UV edge chain"),
            ("GEOMETRY", "Edge distribution: Geometry", "Space vertices proportionally to 3D mesh edge lengths"),
        ),
        default="EVEN",
    )
    iterations: IntProperty(name="Interior iterations", default=150, min=1, max=1000)
    respect_pins: BoolProperty(name="Respect pins", default=True)
    prevent_overlap: BoolProperty(name="Prevent overlap", default=False)

    @classmethod
    def poll(cls, context):
        return circle_ops._poll(context)

    def invoke(self, context, event):
        ops._apply_preset_defaults(self, context, ("prevent_overlap",))
        return self.execute(context)

    def execute(self, context):
        started = perf_counter()
        ops._apply_preset_defaults(self, context, ("prevent_overlap",))
        changed = 0
        skipped = []
        has_targets = False

        for obj in circle_ops._edit_meshes(context):
            if not obj.data.uv_layers:
                continue
            bm = bmesh.from_edit_mesh(obj.data)
            core.prepare_bm(bm)
            uvl = bm.loops.layers.uv.active
            if uvl is None:
                del bm
                continue
            sel_edges = core.selected_uv_edges(context, bm)
            if not sel_edges:
                del bm, uvl
                continue
            loops = core.loops_in_order(bm)
            visible = core.visible_editable_faces(bm, context)
            faces = core.connected_uv_faces(
                bm, uvl, {loops[p].face.index for p in sel_edges}) & visible
            del bm, uvl, loops
            if not faces:
                continue
            has_targets = True
            try:
                result = distribute_edges(
                    context, obj, faces,
                    mode=self.mode,
                    iterations=self.iterations,
                    respect_pins=self.respect_pins,
                    prevent_overlap=self.prevent_overlap,
                    selected_positions=sel_edges,
                )
            except Exception as exc:
                skipped.append(str(exc))
                continue
            changed += int(result.get("changed", 0))
            skipped.extend(result.get("skipped", ()))

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
            message = t("Select a UV edge chain of at least two connected edges")
        self.report({"WARNING"}, message)
        return {"FINISHED"} if has_targets else {"CANCELLED"}


CLASSES = (UVCK_OT_distribute_edges,)
