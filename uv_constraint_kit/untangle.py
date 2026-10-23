# SPDX-License-Identifier: GPL-3.0-or-later
"""Small numeric initializers for folded UV charts; no Blender API dependency."""
import math

import numpy as np


def fold_seed(topo, chart, held):
    """Repair sparse inverted vertex stars without moving held/outside UVs."""
    groups = np.asarray(topo.loop_to_vgroup, dtype=np.int64)
    triangles = groups[np.asarray(chart['positions'], dtype=np.int64)]
    uv = np.asarray(topo.initial_uv, dtype=float).copy()
    sign = chart['sign']

    def crosses(indices, point=None, group=None):
        points = uv[triangles[indices]].copy()
        if point is not None:
            points[triangles[indices] == group] = point
        edges = points[:, 1:] - points[:, :1]
        return sign * (edges[:, 0, 0] * edges[:, 1, 1] - edges[:, 0, 1] * edges[:, 1, 0])

    bad = np.flatnonzero(crosses(np.arange(len(triangles))) <= 2e-14)
    # ponytail: bound local work; widespread folds use the established global initializers.
    if not len(bad) or len(bad) > 256:
        return None
    locked = set(held) | set(groups[~np.isin(topo.loop_faces, chart['faces'])])
    candidates = set(triangles[bad].ravel()) - locked
    incident = {g: [] for g in candidates}
    nearby = np.flatnonzero(np.any(np.isin(triangles, list(candidates)), axis=1))
    for index in nearby:
        for group in set(triangles[index]) & candidates:
            incident[group].append(index)
    changed = set()
    for _ in range(8):
        progress = False
        for index in bad:
            if crosses([index])[0] > 2e-14:
                continue
            choices = []
            for group in set(triangles[index]) & candidates:
                star = incident[group]
                if len(star) > 512:
                    continue
                edges = []
                for triangle in triangles[star]:
                    corner = np.flatnonzero(triangle == group)
                    if len(corner) != 1:
                        break
                    k = int(corner[0])
                    edges.append((uv[triangle[(k + 1) % 3]], uv[triangle[(k + 2) % 3]]))
                else:
                    neighbors = np.asarray(edges).reshape(-1, 2)
                    low, high = neighbors.min(axis=0), neighbors.max(axis=0)
                    polygon = [low, np.array([high[0], low[1]]), high,
                               np.array([low[0], high[1]])]
                    for a, b in edges:
                        edge = b - a

                        def distance(point):
                            delta = point - a
                            return sign * (edge[0] * delta[1] - edge[1] * delta[0]) - 2e-13

                        clipped = []
                        previous, dprevious = polygon[-1], distance(polygon[-1])
                        for current in polygon:
                            dcurrent = distance(current)
                            if (dprevious >= 0) != (dcurrent >= 0):
                                clipped.append(previous + (current - previous) * dprevious / (dprevious - dcurrent))
                            if dcurrent >= 0:
                                clipped.append(current)
                            previous, dprevious = current, dcurrent
                        polygon = clipped
                        if not polygon:
                            break
                    if len(polygon) < 3:
                        continue
                    point = np.asarray(polygon).mean(axis=0).astype(np.float32).astype(float)
                    if np.all(np.isfinite(point)) and np.all(crosses(star, point, group) > 2e-14):
                        choices.append((float(np.sum((point - uv[group]) ** 2)), int(group), point))
            if choices:
                _, group, point = min(choices, key=lambda item: item[:2])
                uv[group] = point
                changed.add(group)
                progress = True
        bad = np.flatnonzero(crosses(np.arange(len(triangles))) <= 2e-14)
        if not len(bad):
            result = np.asarray(topo.initial_uv, dtype=float).copy()
            for group in changed:
                result[topo.vgroup_loops[group]] = uv[group]
            return result
        if not progress:
            break
    return None


def _cross(a, b, c):
    ab, ac = b - a, c - a
    return float(ab[0] * ac[1] - ab[1] * ac[0])


def _inside_value(a, b, p, sign):
    edge, delta = b - a, p - a
    return float(sign * (edge[0] * delta[1] - edge[1] * delta[0]))


def _clip_halfplane(poly, a, b, sign, margin):
    if not poly:
        return []
    result = []
    previous = poly[-1]
    previous_value = _inside_value(a, b, previous, sign) - margin
    previous_inside = previous_value >= 0.0
    for current in poly:
        current_value = _inside_value(a, b, current, sign) - margin
        current_inside = current_value >= 0.0
        if current_inside != previous_inside:
            denominator = previous_value - current_value
            if denominator == 0.0 or not math.isfinite(denominator):
                return []
            result.append(previous + previous_value / denominator * (current - previous))
        if current_inside:
            result.append(current)
        previous, previous_value, previous_inside = current, current_value, current_inside
    return result


def _star_kernel(uv, triangles, incident, group, sign):
    points = [uv[group]]
    for ti in incident:
        points.extend(uv[triangles[ti]])
    points = np.asarray(points, dtype=np.float64)
    if not np.all(np.isfinite(points)):
        return []
    span = float(max(np.ptp(points[:, 0]), np.ptp(points[:, 1])))
    if not math.isfinite(span) or span <= 0.0:
        return []
    pad = span * 0.5
    lo, hi = points.min(axis=0) - pad, points.max(axis=0) + pad
    polygon = [np.array((lo[0], lo[1])), np.array((hi[0], lo[1])),
               np.array((hi[0], hi[1])), np.array((lo[0], hi[1]))]
    for ti in incident:
        tri = triangles[ti]
        k = int(np.flatnonzero(tri == group)[0])
        a, b = uv[tri[(k + 1) % 3]], uv[tri[(k + 2) % 3]]
        polygon = _clip_halfplane(polygon, a, b, sign, 2e-13)
        if len(polygon) < 3:
            return []
    clean = []
    for point in polygon:
        if not clean or not np.array_equal(point, clean[-1]):
            clean.append(point)
    if len(clean) > 1 and np.array_equal(clean[0], clean[-1]):
        clean.pop()
    return clean if len(clean) >= 3 else []


def _pair_area(engine, uv, triangles, first, second):
    a = uv[triangles[first]][None, :, :]
    b = uv[triangles[second]][None, :, :]
    result = engine.intersections(a, b)
    if result is None:
        return None
    value = float(np.asarray(result).reshape(-1)[0])
    return value if math.isfinite(value) and value >= 0.0 else None


def repair_contacts(uv, triangles, face_ids, locked, engine, sign=1, max_pairs=8):
    """Repair self-overlaps with bounded, orientation-preserving vertex-star moves."""
    uv = np.asarray(uv, dtype=np.float64)
    triangles = np.asarray(triangles, dtype=np.int64)
    face_ids = np.asarray(face_ids, dtype=np.int64)
    locked = {int(group) for group in locked}
    if (uv.ndim != 2 or uv.shape[1:] != (2,) or triangles.ndim != 2 or
            triangles.shape[1:] != (3,) or face_ids.shape != (len(triangles),)):
        return None
    if (not np.all(np.isfinite(uv)) or np.any(triangles < 0) or
            (triangles.size and int(triangles.max()) >= len(uv)) or
            sign not in (-1, 1) or int(max_pairs) < 0):
        return None
    work = uv.copy()
    signed = np.asarray([sign * _cross(*work[tri]) for tri in triangles])
    if np.any(~np.isfinite(signed)) or np.any(signed <= 2e-14):
        return None

    for round_index in range(int(max_pairs) + 1):
        witness = engine.self_overlap_witness(work[triangles], face_ids)
        if witness is None:
            return None
        if witness[0] == 0:
            return work
        if witness[0] != 1 or round_index == int(max_pairs):
            return None
        first, second = int(witness[1]), int(witness[2])
        if not (0 <= first < len(triangles) and 0 <= second < len(triangles) and first != second):
            return None
        old_area = _pair_area(engine, work, triangles, first, second)
        if old_area is None:
            return None

        witness_groups = sorted(set(map(int, triangles[first])) | set(map(int, triangles[second])))
        incident = {group: np.flatnonzero(np.any(triangles == group, axis=1)).tolist()
                    for group in witness_groups}
        best = None
        for group in witness_groups:
            if group in locked:
                continue
            star = incident[group]
            kernel = _star_kernel(work, triangles, star, group, sign)
            if not kernel:
                continue
            targets = list(kernel) + [np.mean(np.asarray(kernel), axis=0)]
            proposals, seen = [], set()
            origin = work[group]
            for target in targets:
                for fraction in (.25, .5, .75, 1.):
                    candidate = (origin + fraction * (target - origin)).astype(np.float32).astype(np.float64)
                    if not np.all(np.isfinite(candidate)):
                        continue
                    key = tuple(map(float, candidate))
                    if key in seen or np.array_equal(candidate, origin):
                        continue
                    seen.add(key)
                    trial = work.copy()
                    trial[group] = candidate
                    if not all(sign * _cross(*trial[triangles[ti]]) > 2e-14 for ti in star):
                        continue
                    proposals.append(candidate)
            if not proposals:
                continue
            a = np.repeat(work[triangles[first]][None, :, :], len(proposals), axis=0)
            b = np.repeat(work[triangles[second]][None, :, :], len(proposals), axis=0)
            local_a = {int(g): i for i, g in enumerate(triangles[first])}
            local_b = {int(g): i for i, g in enumerate(triangles[second])}
            for i, candidate in enumerate(proposals):
                if group in local_a:
                    a[i, local_a[group]] = candidate
                if group in local_b:
                    b[i, local_b[group]] = candidate
            areas = engine.intersections(a, b)
            if areas is None:
                return None
            for candidate, area_value in zip(proposals, np.asarray(areas).reshape(-1)):
                area = float(area_value)
                if not math.isfinite(area) or not area < old_area:
                    continue
                delta = float(np.dot(candidate - origin, candidate - origin))
                order = (max(area - 1e-10, 0.), delta, group, tuple(map(float, candidate)))
                if best is None or order < best[0]:
                    best = (order, group, candidate.copy())
        if best is None:
            return None
        _, group, candidate = best
        if group in locked:
            return None
        work[group] = candidate
    return None


def feasible_seed(topo, chart, held, engine):
    """Build a compact contact repair from a local fold seed; return corner UVs."""
    raw = fold_seed(topo, chart, held)
    if raw is None:
        raw = np.asarray(topo.initial_uv, dtype=float).copy()
    raw = np.asarray(raw, dtype=float).copy()
    chart_faces = set(chart['faces'])
    positions = np.asarray(chart['positions'], dtype=np.int64)
    if positions.ndim != 2 or positions.shape[1:] != (3,) or not len(positions):
        return None

    loop_groups = np.asarray(topo.loop_to_vgroup, dtype=np.int64)
    triangle_groups = loop_groups[positions]
    unique_groups = np.asarray(sorted(set(map(int, triangle_groups.ravel()))), dtype=np.int64)
    if not len(unique_groups):
        return None
    group_index = {int(group): i for i, group in enumerate(unique_groups)}
    triangles = np.asarray([[group_index[int(group)] for group in tri]
                            for tri in triangle_groups], dtype=np.int64)
    face_ids = np.asarray(topo.loop_faces, dtype=np.int64)[positions[:, 0]]
    compact_uv = np.asarray([raw[topo.vgroup_loops[int(group)][0]] for group in unique_groups], dtype=np.float64)
    outside_groups = {int(group) for p, group in enumerate(loop_groups)
                      if int(topo.loop_faces[p]) not in chart_faces}
    locked_groups = {int(group) for group in held} | outside_groups
    locked = {group_index[group] for group in locked_groups if group in group_index}

    repaired = repair_contacts(compact_uv, triangles, face_ids, locked, engine,
                               sign=chart['sign'], max_pairs=8)
    if repaired is None:
        return None
    for compact_index, group in enumerate(unique_groups):
        group = int(group)
        if compact_index in locked or np.array_equal(repaired[compact_index], compact_uv[compact_index]):
            continue
        raw[topo.vgroup_loops[group]] = repaired[compact_index]
    return raw
