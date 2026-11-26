# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent integer-grid layout for orientable, regular quad UV charts.

This module uses a topological grid embedding rather than face-follow copying.
Only charts that admit a unique integer lattice are supported. Poles, wrapped
cycles, mixed polygons, non-manifold edges and overlapping grid cells are
rejected before any UV coordinate is written. No third-party quadrify code is
used. The caller can restore the original chart area and centroid afterwards.
"""
from collections import defaultdict, deque
import heapq
import math
import struct

from . import core
from .const import C_HORIZONTAL, C_VERTICAL


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
          min_limit=0.002, *, require_complete=True, apply=True, topo=None):
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

    if topo is None:
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
                if (require_complete and linked.face.index not in wanted and
                        core.UVTopology._edge_uv_matches(source, linked, uvl)):
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
    if apply:
        try:
            for corners, ids, _points in rows.values():
                for loop, group in zip(corners, ids):
                    loop[uvl].uv = output[group]
        except Exception:
            for loop, point in original:
                loop[uvl].uv = point
            raise
    report = {'algorithm': 'INTEGER_GRID', 'faces': len(faces),
              'vertices': len(lattice),
              'grid_width': max(xs) - min(xs), 'grid_height': max(ys) - min(ys)}
    if not apply:
        report['output'] = output
    return report


def quad_flow_runs(bm, uvl, face_ids, seed, *, topo=None, exclude_poles=False, existing_runs=()):
    """Extract maximal acyclic conflict-free H/V quad-flow runs over an island.

    Works on pure-quad and mixed-polygon UV islands even when interior poles or
    concave notches prevent a rigid integer lattice embedding. Guarantees that
    the resulting row (H) and column (V) equivalence classes form strict DAGs
    with respect to quad orientation, preventing zero-area quad collapse.
    """
    if uvl is None:
        raise ValueError('Grid quadrification requires a UV map')
    core.prepare_bm(bm)
    wanted = {int(i) for i in face_ids}
    quads = [fi for fi in sorted(wanted) if 0 <= fi < len(bm.faces) and len(bm.faces[fi].loops) == 4]
    if not quads:
        raise ValueError('Island has no visible quad face')
    if topo is None:
        topo = core.UVTopology(bm, uvl)

    rows = {}
    edge_uses = defaultdict(list)
    v_faces = defaultdict(set)
    windings = []
    for fi in quads:
        face = bm.faces[fi]
        corners = tuple(face.loops)
        lpos = tuple(topo.loop_pos[lp] for lp in corners)
        gids = tuple(int(topo.loop_to_vgroup[p]) for p in lpos)
        if len(set(gids)) != 4:
            continue
        pts = [tuple(float(c) for c in lp[uvl].uv) for lp in corners]
        if not all(math.isfinite(c) for pt in pts for c in pt):
            continue
        signed = _area(pts)
        if not math.isfinite(signed) or abs(signed) <= 1e-14:
            continue
        sign = 1 if signed > 0.0 else -1
        windings.append(sign)
        rows[fi] = (corners, lpos, gids, pts, sign)
        for c, lp in enumerate(corners):
            if len(lp.edge.link_faces) > 2:
                continue
            a, b = gids[c], gids[(c + 1) % 4]
            v_faces[a].add(fi)
            key = (lp.edge.index, min(a, b), max(a, b))
            edge_uses[key].append((fi, c, a, b, lpos[c]))

    if not rows:
        raise ValueError('Island has no valid quad faces for quadrification')
    winding = 1 if sum(windings) >= 0 else -1
    valid_quads = {fi for fi, r in rows.items() if r[4] == winding}
    if not valid_quads:
        raise ValueError('Island has no consistently oriented quad faces')

    # Build internal dual adjacency and detect boundary vertices & interior poles.
    boundary_verts = set()
    neighbors = defaultdict(list)
    for uses in edge_uses.values():
        uses = [u for u in uses if u[0] in valid_quads]
        if len(uses) == 1:
            boundary_verts.add(uses[0][2])
            boundary_verts.add(uses[0][3])
        elif len(uses) == 2:
            u0, u1 = uses
            if u0[2] == u1[3] and u0[3] == u1[2]:
                neighbors[u0[0]].append((u1[0], u0[1], u1[1], u0[2], u0[3]))
                neighbors[u1[0]].append((u0[0], u1[1], u0[1], u1[2], u1[3]))
    # Vertices touching non-quads in mixed islands also behave as non-regular boundaries.
    for fi in wanted:
        if fi not in valid_quads:
            for lp in bm.faces[fi].loops:
                boundary_verts.add(int(topo.loop_to_vgroup[topo.loop_pos[lp]]))

    poles = {g for g, fs in v_faces.items() if g not in boundary_verts and len(fs) != 4}

    def edge_dir(c0, c):
        return ((c - c0) % 4) if winding > 0 else ((-(c - c0)) % 4)

    def c0_from_edge_dir(c, d):
        return ((c - d) % 4) if winding > 0 else ((c + d) % 4)

    def frame_uv_alignment(fi, c0):
        pts = rows[fi][3]
        ux = uy = vx = vy = 0.0
        for c in range(4):
            a, b = pts[c], pts[(c + 1) % 4]
            dx, dy = b[0] - a[0], b[1] - a[1]
            d = edge_dir(c0, c)
            if d == 0:
                ux += dx
                uy += dy
            elif d == 2:
                ux -= dx
                uy -= dy
            elif d == 1:
                vx += dx
                vy += dy
            else:
                vx -= dx
                vy -= dy
        un = math.hypot(ux, uy)
        vn = math.hypot(vx, vy)
        if un <= 1e-12 or vn <= 1e-12:
            return 0.0
        return 0.5 * (ux / un + vy / vn)

    # Priority-BFS propagate canonical (H, V) frame across each quad sheet.
    face_c0 = {}
    start_order = ([int(seed)] if int(seed) in valid_quads else []) + sorted(valid_quads)
    for start in start_order:
        if start in face_c0:
            continue
        best_c0 = max(range(4), key=lambda c0: frame_uv_alignment(start, c0))
        face_c0[start] = best_c0
        pq = []
        for nbr, c_cur, c_nbr, ga, gb in neighbors[start]:
            d_cur = edge_dir(best_c0, c_cur)
            prop_c0 = c0_from_edge_dir(c_nbr, (d_cur + 2) % 4)
            pole_pen = (1 if ga in poles else 0) + (1 if gb in poles else 0)
            heapq.heappush(pq, (pole_pen, -frame_uv_alignment(nbr, prop_c0), nbr, prop_c0))
        while pq:
            _pp, _neg_al, fi, prop_c0 = heapq.heappop(pq)
            if fi in face_c0:
                continue
            face_c0[fi] = prop_c0
            for nbr, c_cur, c_nbr, ga, gb in neighbors[fi]:
                if nbr in face_c0:
                    continue
                d_cur = edge_dir(prop_c0, c_cur)
                nc0 = c0_from_edge_dir(c_nbr, (d_cur + 2) % 4)
                pole_pen = (1 if ga in poles else 0) + (1 if gb in poles else 0)
                heapq.heappush(pq, (pole_pen, -frame_uv_alignment(nbr, nc0), nbr, nc0))

    # Map existing user H/V constraints for top-priority preservation.
    existing_h_pairs = set()
    existing_v_pairs = set()
    for kind, pos_list in existing_runs:
        if kind not in (C_HORIZONTAL, C_VERTICAL):
            continue
        target_set = existing_h_pairs if kind == C_HORIZONTAL else existing_v_pairs
        for p in pos_list:
            ga, gb = topo.loop_edge_vgroups(p)
            target_set.add((min(int(ga), int(gb)), max(int(ga), int(gb))))

    # Collect agreeing edges and detect holonomy-cut quads.
    cut_quads = set()
    cand_h = []
    cand_v = []
    for uses in edge_uses.values():
        uses = [u for u in uses if u[0] in face_c0]
        if not uses:
            continue
        if len(uses) == 2:
            u0, u1 = uses
            d0 = edge_dir(face_c0[u0[0]], u0[1])
            d1 = edge_dir(face_c0[u1[0]], u1[1])
            if (d0 + 2) % 4 != d1:
                cut_quads.add(u0[0])
                cut_quads.add(u1[0])
                continue
        fi, c, ga, gb, lpos = uses[0]
        epair = (min(ga, gb), max(ga, gb))
        user_h = epair in existing_h_pairs
        user_v = epair in existing_v_pairs
        if user_h and user_v:
            continue
        d = edge_dir(face_c0[fi], c)
        is_h = True if user_h else (False if user_v else (d in (0, 2)))
        touches_pole = (ga in poles) or (gb in poles)
        if exclude_poles and not (user_h or user_v):
            if touches_pole or any(u[0] in cut_quads for u in uses):
                continue
        pts = rows[fi][3]
        pa, pb = pts[c], pts[(c + 1) % 4]
        dx, dy = abs(pb[0] - pa[0]), abs(pb[1] - pa[1])
        length = math.hypot(dx, dy)
        align = ((dx / length) if is_h else (dy / length)) if length > 1e-12 else 0.0
        if exclude_poles and not (user_h or user_v) and align < 0.5:
            # On heavily poled organic charts, skip extreme >60 deg edges so poles stay smooth.
            continue
        prio = (-1 if (user_h or user_v) else (1 if touches_pole else 0), -align, lpos)
        if is_h:
            cand_h.append((prio, ga, gb, lpos))
        else:
            cand_v.append((prio, ga, gb, lpos))

    if exclude_poles and cut_quads:
        # Re-filter any edge added before its second pass flagged a cut quad.
        cut_lpos = {lpos for fi in cut_quads for lpos in rows[fi][1]}
        cand_h = [item for item in cand_h if item[0][0] == -1 or item[3] not in cut_lpos]
        cand_v = [item for item in cand_v if item[0][0] == -1 or item[3] not in cut_lpos]

    x_order_pairs = []
    y_order_pairs = []
    for fi, c0 in face_c0.items():
        if exclude_poles and fi in cut_quads:
            continue
        gids = rows[fi][2]
        for c in range(4):
            d = edge_dir(c0, c)
            ga, gb = gids[c], gids[(c + 1) % 4]
            if d == 0:
                x_order_pairs.append((ga, gb))
            elif d == 2:
                x_order_pairs.append((gb, ga))
            elif d == 1:
                y_order_pairs.append((ga, gb))
            else:
                y_order_pairs.append((gb, ga))

    def filter_acyclic(cands, order_pairs):
        if not cands:
            return []
        node_set = set()
        for _pr, a, b, _lp in cands:
            node_set.add(a)
            node_set.add(b)
        for a, b in order_pairs:
            node_set.add(a)
            node_set.add(b)
        node_list = sorted(node_set)
        nid = {g: i for i, g in enumerate(node_list)}
        n_nodes = len(node_list)
        parent = list(range(n_nodes))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        out_adj = [set() for _ in range(n_nodes)]
        in_adj = [set() for _ in range(n_nodes)]
        for a, b in order_pairs:
            ia, ib = nid[a], nid[b]
            if ia != ib:
                out_adj[ia].add(ib)
                in_adj[ib].add(ia)

        stamp = 0
        seen = [0] * n_nodes

        def reaches(src, dst):
            nonlocal stamp
            stamp += 1
            stack = [src]
            seen[src] = stamp
            while stack:
                u = stack.pop()
                for w in out_adj[u]:
                    if w == dst:
                        return True
                    if seen[w] != stamp:
                        seen[w] = stamp
                        stack.append(w)
            return False

        accepted_lpos = []
        for _pr, a, b, lp in sorted(cands, key=lambda item: item[0]):
            ra, rb = find(nid[a]), find(nid[b])
            if ra == rb:
                accepted_lpos.append(lp)
                continue
            if rb in out_adj[ra] or ra in out_adj[rb]:
                continue
            if reaches(ra, rb) or reaches(rb, ra):
                continue
            if len(out_adj[ra]) + len(in_adj[ra]) < len(out_adj[rb]) + len(in_adj[rb]):
                ra, rb = rb, ra
            parent[rb] = ra
            for w in out_adj[rb]:
                in_adj[w].discard(rb)
                if w != ra:
                    out_adj[ra].add(w)
                    in_adj[w].add(ra)
            for w in in_adj[rb]:
                out_adj[w].discard(rb)
                if w != ra:
                    in_adj[ra].add(w)
                    out_adj[w].add(ra)
            out_adj[rb].clear()
            in_adj[rb].clear()
            accepted_lpos.append(lp)
        return accepted_lpos

    h_lpos = filter_acyclic(cand_h, y_order_pairs)
    v_lpos = filter_acyclic(cand_v, x_order_pairs)
    all_lpos = list(h_lpos) + list(v_lpos)
    kinds = {p: C_HORIZONTAL for p in h_lpos}
    for p in v_lpos:
        kinds[p] = C_VERTICAL
    runs = core.group_chains(topo, all_lpos, kinds) if all_lpos else []
    if not runs:
        raise ValueError('No consistent quad-flow edges found in island')
    stats = {
        'quads': len(valid_quads),
        'poles': len(poles),
        'cut_quads': len(cut_quads),
        'h_edges': len(h_lpos),
        'v_edges': len(v_lpos),
        'exclude_poles': bool(exclude_poles),
    }
    return runs, stats
