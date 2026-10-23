# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent integer-grid layout for orientable, regular quad UV charts.

This module uses a topological grid embedding rather than face-follow copying.
Only charts that admit a unique integer lattice are supported. Poles, wrapped
cycles, mixed polygons, non-manifold edges and overlapping grid cells are
rejected before any UV coordinate is written. No third-party quadrify code is
used. The caller can restore the original chart area and centroid afterwards.
"""
from collections import defaultdict, deque
import math
import struct

from . import core


def _area(points):
    return 0.5 * sum(a[0] * b[1] - a[1] * b[0]
                     for a, b in zip(points, points[1:] + points[:1]))


def _mean_length(values):
    """Geometric mean keeps multiplicative geometric proportions symmetric."""
    if not values or any(not math.isfinite(v) or v <= 0.0 for v in values):
        raise ValueError('Grid quadrification requires nonzero finite mesh edges')
    return math.exp(sum(math.log(v) for v in values) / len(values))


def _float32(value):
    try:
        result = struct.unpack('f', struct.pack('f', value))[0]
    except (OverflowError, struct.error):
        raise ValueError('Grid coordinates exceed UV precision') from None
    if not math.isfinite(result):
        raise ValueError('Grid coordinates exceed UV precision')
    return result


def _coordinates(intervals, minimum, maximum, shape, average, seed_length, limit):
    if shape == 'EVEN':
        lengths = {i: 1.0 for i in range(minimum, maximum)}
    elif shape == 'FACE':
        lengths = {i: seed_length for i in range(minimum, maximum)}
    else:
        if any(i not in intervals for i in range(minimum, maximum)):
            raise ValueError('Quad chart contains a disconnected grid interval')
        lengths = {i: _mean_length(intervals[i])
                   for i in range(minimum, maximum)}
        if average:
            uniform = _mean_length(list(lengths.values()))
            lengths = dict.fromkeys(lengths, uniform)
    values = {minimum: 0.0}
    for i in range(minimum, maximum):
        values[i + 1] = values[i] + max(limit, lengths[i])
    return values


def solve(bm, uvl, face_ids, seed, shape='PER_FACE', average_shape=True,
          min_limit=0.002):
    """Lay out one complete UV chart; unsupported input raises without writes.

    EVEN uses unit grid spacing. FACE uses the seed's two geometric edge-pair
    lengths. PER_FACE averages geometric edge lengths per lattice interval;
    ``average_shape`` makes each axis uniformly spaced. Coordinates remain
    axis-aligned and retain the input winding and nearest seed-edge direction.
    Pins and constraint flags are left to the caller's transactional checks.
    """
    if shape not in {'PER_FACE', 'FACE', 'EVEN'}:
        raise ValueError('Invalid grid face shape')
    if uvl is None:
        raise ValueError('Grid quadrification requires a UV map')
    limit = float(min_limit)
    if not math.isfinite(limit) or limit <= 0.0:
        raise ValueError('Grid minimum spacing must be finite and positive')
    core.prepare_bm(bm)
    wanted = {int(i) for i in face_ids}
    if not wanted or any(i < 0 or i >= len(bm.faces) for i in wanted):
        raise ValueError('Grid quadrification requires valid chart faces')
    seed = int(seed)
    if seed not in wanted:
        raise ValueError('Grid seed is outside the chart')
    faces = [bm.faces[i] for i in sorted(wanted)]
    if any(len(face.loops) != 4 for face in faces):
        raise ValueError('Grid quadrification supports pure quad charts only')

    topo = core.UVTopology(bm, uvl)
    rows = {}
    edge_rows = defaultdict(list)
    original = []
    winding = None
    for face in faces:
        corners = tuple(face.loops)
        group_ids = tuple(int(topo.loop_to_vgroup[topo.loop_pos[loop]])
                          for loop in corners)
        if len(set(group_ids)) != 4:
            raise ValueError('Quad has repeated UV vertices')
        points = [tuple(float(v) for v in loop[uvl].uv) for loop in corners]
        if not all(math.isfinite(v) for point in points for v in point):
            raise ValueError('Grid quadrification requires finite input UVs')
        signed = _area(points)
        if not math.isfinite(signed) or signed == 0.0:
            raise ValueError('Grid quadrification requires nonzero UV faces')
        sign = 1 if signed > 0.0 else -1
        if winding is None:
            winding = sign
        elif sign != winding:
            raise ValueError('Quad chart has inconsistent UV winding')
        rows[face.index] = (corners, group_ids, points)
        for corner, loop in enumerate(corners):
            if len(loop.edge.link_faces) > 2:
                raise ValueError('Grid quadrification rejects non-manifold edges')
            a, b = group_ids[corner], group_ids[(corner + 1) % 4]
            key = (loop.edge.index, min(a, b), max(a, b))
            edge_rows[key].append((face.index, corner, a, b, loop.edge))
            original.append((loop, points[corner]))

    neighbors = defaultdict(list)
    for entries in edge_rows.values():
        if len(entries) > 2:
            raise ValueError('Quad chart has a non-manifold UV edge')
        if len(entries) == 2:
            a, b = entries
            if a[2] != b[3] or a[3] != b[2]:
                raise ValueError('Quad chart has inconsistent face orientation')
            neighbors[a[0]].append((b[0], b[1]))
            neighbors[b[0]].append((a[0], a[1]))
        else:
            entry = entries[0]
            edge = entry[4]
            source = rows[entry[0]][0][entry[1]]
            for linked in edge.link_loops:
                if linked.face.index not in wanted and core.UVTopology._edge_uv_matches(source, linked, uvl):
                    raise ValueError('Select the complete connected UV chart')

    lattice = {}
    occupied = {}
    cells = set()

    def assign(face_index, points):
        group_ids = rows[face_index][1]
        cell = tuple(sorted(points))
        if cell in cells:
            raise ValueError('Quad chart overlaps itself in the integer grid')
        for group, point in zip(group_ids, points):
            if group in lattice and lattice[group] != point:
                raise ValueError('Quad chart cannot form a consistent integer grid')
            if point in occupied and occupied[point] != group:
                raise ValueError('Different UV vertices occupy the same grid point')
        for group, point in zip(group_ids, points):
            lattice[group] = point
            occupied[point] = group
        cells.add(cell)

    assign(seed, ((0, 0), (1, 0), (1, 1), (0, 1)))
    visited = {seed}
    queue = deque([seed])
    while queue:
        current = queue.popleft()
        for adjacent, corner in neighbors[current]:
            ids = rows[adjacent][1]
            a, b = lattice[ids[corner]], lattice[ids[(corner + 1) % 4]]
            dx, dy = b[0] - a[0], b[1] - a[1]
            if abs(dx) + abs(dy) != 1:
                raise ValueError('Quad edge is not a unit lattice edge')
            left = (-dy, dx)
            local = (a, b, (b[0] + left[0], b[1] + left[1]),
                     (a[0] + left[0], a[1] + left[1]))
            points = tuple(local[(i - corner) % 4] for i in range(4))
            if adjacent in visited:
                if any(lattice[group] != point for group, point in zip(ids, points)):
                    raise ValueError('Quad chart has a conflicting closed grid cycle')
                continue
            assign(adjacent, points)
            visited.add(adjacent)
            queue.append(adjacent)
    if visited != wanted:
        raise ValueError('Grid quadrification requires one connected UV chart')

    lengths = (defaultdict(list), defaultdict(list))
    seen_edges = set()
    for entries in edge_rows.values():
        _fi, _corner, a, b, edge = entries[0]
        key = (edge.index, min(a, b), max(a, b))
        if key in seen_edges:
            continue
        seen_edges.add(key)
        pa, pb = lattice[a], lattice[b]
        axis = 0 if pa[0] != pb[0] else 1
        length = float(edge.calc_length())
        if not math.isfinite(length) or length <= 0.0:
            raise ValueError('Grid quadrification requires nonzero finite mesh edges')
        lengths[axis][min(pa[axis], pb[axis])].append(length)
    seed_loops, _ids, seed_uv = rows[seed]
    seed_x = _mean_length([float(seed_loops[i].edge.calc_length()) for i in (0, 2)])
    seed_y = _mean_length([float(seed_loops[i].edge.calc_length()) for i in (1, 3)])
    xs = [point[0] for point in lattice.values()]
    ys = [point[1] for point in lattice.values()]
    x = _coordinates(lengths[0], min(xs), max(xs), shape, average_shape, seed_x, limit)
    y = _coordinates(lengths[1], min(ys), max(ys), shape, average_shape, seed_y, limit)
    direction = (seed_uv[1][0] - seed_uv[0][0] + seed_uv[2][0] - seed_uv[3][0],
                 seed_uv[1][1] - seed_uv[0][1] + seed_uv[2][1] - seed_uv[3][1])
    if direction == (0.0, 0.0):
        raise ValueError('Quad seed has no stable horizontal direction')
    quarter = int(math.floor(math.atan2(direction[1], direction[0]) / (math.pi / 2.0) + 0.5)) % 4
    axis_x = ((1, 0), (0, 1), (-1, 0), (0, -1))[quarter]
    axis_y = (-axis_x[1] * winding, axis_x[0] * winding)
    center = (sum(p[0] for p in seed_uv) / 4.0,
              sum(p[1] for p in seed_uv) / 4.0)
    mid_x, mid_y = (x[0] + x[1]) / 2.0, (y[0] + y[1]) / 2.0
    output = {}
    for group, (i, j) in lattice.items():
        u, v = x[i] - mid_x, y[j] - mid_y
        output[group] = (_float32(center[0] + axis_x[0] * u + axis_y[0] * v),
                         _float32(center[1] + axis_x[1] * u + axis_y[1] * v))
    for _corners, ids, _points in rows.values():
        if _area([output[group] for group in ids]) * winding <= 0.0:
            raise ValueError('Grid layout would collapse at UV precision')
    # Commit only after the entire chart, including numeric output, is valid.
    # Retaining the exact initial float32 values also makes write errors atomic.
    try:
        for corners, ids, _points in rows.values():
            for loop, group in zip(corners, ids):
                loop[uvl].uv = output[group]
    except Exception:
        for loop, point in original:
            loop[uvl].uv = point
        raise
    return {'algorithm': 'INTEGER_GRID', 'faces': len(faces),
            'vertices': len(lattice),
            'grid_width': max(xs) - min(xs), 'grid_height': max(ys) - min(ys)}
