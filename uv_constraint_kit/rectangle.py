# SPDX-License-Identifier: GPL-3.0-or-later
"""Rectangular island boundaries, with an optimized interior.

Four boundary corners are matched in cyclic order to an oriented bounding box.
No mesh topology, seams, constraint marks or selection flags are changed.
"""
import math

import bmesh
import numpy as np

from . import circle, core


def _box(points):
    delta = np.roll(points, -1, axis=0)-points
    angles = sorted({round((math.atan2(y, x)+math.pi/4) % (math.pi/2)-math.pi/4, 12)
                     for x, y in delta if math.hypot(x, y) > 1e-12})
    best = None
    for angle in angles:
        c, s = math.cos(angle), math.sin(angle)
        rotation = np.array([[c, -s], [s, c]])
        local = points @ rotation
        lo, hi = local.min(axis=0), local.max(axis=0)
        area = float(np.prod(hi-lo))
        key = (round(area, 12), abs(angle), angle)
        if best is None or key < best[0]:
            best = key, local, lo, hi, rotation
    if best is None or best[0][0] <= 1e-14:
        raise ValueError('The island boundary has zero area')
    return best[1:]


def _corners(points, corners):
    """Ordered corner assignment; eight nearest starting vertices suffice.

    Dynamic programming keeps the other three corners distinct and ordered,
    including concave borders where independent nearest points may coincide.
    """
    n = len(points)
    if n < 4:
        raise ValueError('Rectification needs at least four boundary vertices')
    distance = ((points[:, None, :]-corners[None, :, :])**2).sum(axis=2)
    starts = sorted(range(n), key=lambda i: (distance[i, 0], i))[:8]
    best = None
    for start in starts:
        order = np.roll(np.arange(n), -start)
        costs = distance[order]
        dp = np.full(n, np.inf)
        dp[0] = costs[0, 0]
        parents = np.full((4, n), -1, dtype=int)
        for stage in range(1, 4):
            following = np.full(n, np.inf)
            smallest, previous = np.inf, -1
            for i in range(stage, n-(3-stage)):
                if dp[i-1] < smallest:
                    smallest, previous = dp[i-1], i-1
                following[i] = smallest+costs[i, stage]
                parents[stage, i] = previous
            dp = following
        last = int(np.argmin(dp))
        indices = [last]
        for stage in (3, 2, 1):
            last = int(parents[stage, last])
            indices.append(last)
        indices.reverse()
        original = [int(order[i]) for i in indices]
        key = (float(dp[indices[-1]]), tuple(original))
        if best is None or key < best[0]:
            best = key, original
    return best[1]


def _marked_corners(topo, faces, cycle, local, box_corners, positive,
                    positions=None):
    layer = core.constraint_layer(topo.bm)
    if layer is None:
        return None
    kinds = {}
    cycle_edges = {frozenset((cycle[i], cycle[(i+1) % len(cycle)])): cycle[i]
                   for i in range(len(cycle))}
    for position in (core.selection_border_loops(topo, faces)
                     if positions is None else positions):
        edge = frozenset(topo.loop_edge_vgroups(position))
        start = cycle_edges.get(edge)
        if start is not None:
            kinds[start] = int(topo.loops[position][layer])
    values = [kinds.get(g, 0) for g in cycle]
    if any(k not in (1, 2) for k in values):
        return None
    turns = [i for i, kind in enumerate(values) if kind != values[i-1]]
    if len(turns) != 4:
        return None
    expected = (1, 2, 1, 2) if positive else (2, 1, 2, 1)
    candidates = []
    for shift in range(4):
        ordered = turns[shift:]+turns[:shift]
        if tuple(values[i] for i in ordered) == expected:
            cost = float(np.sum((local[ordered]-box_corners)**2))
            candidates.append((cost, tuple(ordered)))
    return list(min(candidates)[1]) if candidates else None


def _rectangle_cycle(topo, faces, cycle, spacing, aspect, shape,
                     orient_to_axis, target_area, mark_positions=None):
    scale = np.array([aspect, 1.])
    points_outer = np.array([tuple(topo.vgroup_uv(g)) for g in cycle])*scale
    signed_area = circle._area(points_outer)
    if abs(signed_area) < 1e-14:
        raise ValueError('The island boundary has zero area')
    local, lo, hi, rotation = _box(points_outer)
    box_corners = np.array([[lo[0], lo[1]], [hi[0], lo[1]],
                            [hi[0], hi[1]], [lo[0], hi[1]]])
    positive = signed_area > 0
    if not positive:
        box_corners = box_corners[[0, 3, 2, 1]]
    corners = (_marked_corners(topo, faces, cycle, local, box_corners, positive,
                               mark_positions)
               or _corners(local, box_corners))
    n = len(cycle)
    sides = []
    for i, start in enumerate(corners):
        stop = corners[(i+1) % 4]
        sides.append([(start+j) % n for j in range((stop-start) % n)])
    xyz = core.vgroup_positions3d(topo)
    geometric = np.array([(xyz[b]-xyz[a]).length
                          for a, b in zip(cycle, cycle[1:]+cycle[:1])])
    uv_lengths = np.linalg.norm(np.roll(points_outer, -1, axis=0)-points_outer, axis=1)
    lengths = (geometric if spacing == 'GEOMETRY' else
               np.ones(n) if spacing == 'EVEN' else uv_lengths)
    if (not np.isfinite(lengths).all() or np.min(lengths) <= 1e-12 or
            any(not side for side in sides)):
        raise ValueError('The island boundary contains a zero-length edge')
    horizontal = (0, 2) if positive else (1, 3)
    vertical = (1, 3) if positive else (0, 2)
    proportions = geometric if spacing == 'GEOMETRY' else uv_lengths
    ratio = (sum(proportions[sides[i]].sum() for i in horizontal) /
             max(sum(proportions[sides[i]].sum() for i in vertical), 1e-12))
    if shape == 'SQUARE':
        ratio = 1.
    area = float(target_area)
    if not math.isfinite(area) or area <= 1e-14:
        raise ValueError('The island boundary has zero area')
    width, height = math.sqrt(area*ratio), math.sqrt(area/ratio)
    target_corners = np.array([[-width/2, -height/2], [width/2, -height/2],
                               [width/2, height/2], [-width/2, height/2]])
    if not positive:
        target_corners = target_corners[[0, 3, 2, 1]]
    if not orient_to_axis:
        target_corners = target_corners @ rotation.T
    q = np.roll(points_outer, -1, axis=0)
    cross = points_outer[:, 0]*q[:, 1]-q[:, 0]*points_outer[:, 1]
    center = np.sum((points_outer+q)*cross[:, None], axis=0)/(6*signed_area)
    held = {}
    for i, side in enumerate(sides):
        distance = np.r_[0., np.cumsum(lengths[side][:-1])]
        for index, t in zip(side, distance/lengths[side].sum()):
            value = center+(1-t)*target_corners[i]+t*target_corners[(i+1) % 4]
            held[cycle[index]] = tuple(value/scale)
    details = dict(outer=cycle, center=(center/scale).tolist(),
                   width=width/aspect, height=height, aspect=aspect,
                   corners=[cycle[i] for i in corners],
                   radius=math.hypot(width/aspect, height)/2)
    return held, details


def _boundary(topo, faces, spacing, aspect, shape, orient_to_axis):
    cycles = circle._boundary_cycles(topo, faces)
    points = [np.array([tuple(topo.vgroup_uv(g)) for g in cycle]) *
              np.array([aspect, 1.]) for cycle in cycles]
    areas = [circle._area(p) for p in points]
    outer_index = max(range(len(cycles)), key=lambda i: abs(areas[i]))
    outer = cycles[outer_index]
    target_area = core.island_area(topo, faces) * aspect
    target_area += sum(abs(a) for i, a in enumerate(areas) if i != outer_index)
    held, details = _rectangle_cycle(
        topo, faces, outer, spacing, aspect, shape, orient_to_axis,
        target_area, core.selection_border_loops(topo, faces))
    for i, cycle in enumerate(cycles):
        if i != outer_index:
            held.update({g: tuple(topo.vgroup_uv(g)) for g in cycle})
    details.update(shape_kind='RECTANGULAR', shape_cycles=[outer],
                    shape_options=dict(shape=shape, spacing=spacing,
                                       orient_to_axis=orient_to_axis),
                    progressive_metric=len(cycles) > 1)
    return held, details


def rectify(context, obj, faces, shape='RECTANGLE', spacing='GEOMETRY',
            orient_to_axis=True, iterations=150, respect_pins=True,
            prevent_overlap=False, selected_only=True, template_only=False):
    selected_positions = None
    if selected_only:
        bm = bmesh.from_edit_mesh(obj.data)
        selected_positions = core.selected_uv_edges(context, bm, mesh_fallback=False)
        del bm

    def boundary(topo, island, method, aspect):
        if selected_positions is None:
            return _boundary(topo, island, method, aspect, shape, orient_to_axis)

        selected_cycles = circle.selected_uv_cycles(
            topo, island, selected_positions)
        held = {}
        details = []
        scale = np.array([aspect, 1.])
        for cycle in selected_cycles:
            points = np.array([tuple(topo.vgroup_uv(g)) for g in cycle]) * scale
            target_area = abs(circle._area(points))
            target, info = _rectangle_cycle(
                topo, island, cycle, method, aspect, shape, orient_to_axis,
                target_area, selected_positions)
            held.update(target)
            details.append(info)
        return held, dict(
            shape_kind='RECTANGULAR', shape_cycles=selected_cycles,
            shape_options=dict(shape=shape, spacing=method,
                               orient_to_axis=orient_to_axis),
            selected_rectangles=details, progressive_metric=True)

    return circle._reshape(context, obj, faces, boundary, spacing, iterations,
                           respect_pins, prevent_overlap, template_only)
