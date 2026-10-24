# SPDX-License-Identifier: GPL-3.0-or-later
"""Final UV acceptance, including triangle intersections and degenerate edges."""
import math
import numpy as np
from mathutils import Vector

from .const import C_HORIZONTAL, C_VERTICAL
from . import native_engine


def _area(p):
    return sum(a[0] * b[1] - b[0] * a[1]
               for a, b in zip(p, p[1:] + p[:1])) * 0.5


def _boundary_crosses(points, include_touch=False):
    """Non-adjacent UV boundary edges intersect (translation invariant)."""
    def cross(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    span = max(max(p[j] for p in points)-min(p[j] for p in points)
               for j in (0, 1))
    epsilon = max(span*span*1e-12, 1e-24)
    for i, a in enumerate(points):
        b = points[(i+1) % len(points)]
        for k in range(i+2, len(points)):
            if i == 0 and k == len(points)-1:
                continue
            c, d = points[k], points[(k+1) % len(points)]
            s, t = cross(a, b, c), cross(a, b, d)
            u, v = cross(c, d, a), cross(c, d, b)
            if ((s > epsilon and t < -epsilon or s < -epsilon and t > epsilon)
                    and (u > epsilon and v < -epsilon or u < -epsilon and v > epsilon)):
                return True
            if include_touch:
                for value, p, q, r in ((s,c,a,b), (t,d,a,b), (u,a,c,d), (v,b,c,d)):
                    if abs(value) <= epsilon and all(
                            min(q[j],r[j]) <= p[j] <= max(q[j],r[j]) for j in (0,1)):
                        return True
    return False


def folded_face_candidates(topo, triangles, crosses, reference):
    """Cache only input faces with mixed winding AND a crossing UV boundary.

    A normal mirrored or concave face is not a repair candidate. A repair must
    make every native triangle agree with the input face's net orientation;
    the caller still checks pins, area, selection and inter-face intersections.
    """
    by_face = {}
    for i, positions in enumerate(triangles):
        by_face.setdefault(topo.loops[positions[0]].face.index, []).append(i)
    candidates = {}
    for face, indices in by_face.items():
        areas = [float(crosses[i]) for i in indices]
        if not (any(a > 2e-14 for a in areas) and any(a < -2e-14 for a in areas)):
            continue
        net = sum(areas)
        if abs(net) <= 2e-14:
            continue
        corners = tuple(topo.loop_pos[l] for l in topo.bm.faces[face].loops)
        if _boundary_crosses([reference[p] for p in corners]):
            candidates[face] = (indices, 1. if net > 0 else -1., corners)
    return candidates


def _intersection(a, b):
    # Adjacent triangles on opposite sides of their shared UV edge only touch.
    # Avoid polygon clipping for the common case, while retaining same-side
    # folds and complete coincident stacks for the exact test below.
    if len(a)==len(b)==3:
        shared=set(a).intersection(b)
        if len(shared)==2:
            p,q=shared
            u=next((v for v in a if v not in shared),None)
            v=next((v for v in b if v not in shared),None)
            if u is None or v is None:return 0.0
            cross=lambda r:(q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
            if cross(u)*cross(v)<=0:return 0.0
    poly = a if _area(a) >= 0 else list(reversed(a))
    b = b if _area(b) >= 0 else list(reversed(b))
    for v, w in zip(b, b[1:] + b[:1]):
        out = []
        for p, q in zip(poly, poly[1:] + poly[:1]):
            sp = (w[0]-v[0])*(p[1]-v[1])-(w[1]-v[1])*(p[0]-v[0])
            sq = (w[0]-v[0])*(q[1]-v[1])-(w[1]-v[1])*(q[0]-v[0])
            if sp >= -1e-12:
                out.append(p)
            if (sp > 1e-12 and sq < -1e-12) or (sp < -1e-12 and sq > 1e-12):
                t = sp / (sp - sq)
                out.append((p[0]+t*(q[0]-p[0]), p[1]+t*(q[1]-p[1])))
        poly = out
        if not poly:
            return 0.0
    return abs(_area(poly))


def measure(core, topo, faces=None):
    island_of = {face: key for key, members in
                 core.face_islands(topo, set(range(len(topo.bm.faces)))).items()
                 for face in members}
    triangles = []
    for face in topo.bm.faces:
        points = [tuple(loop[topo.uvl].uv) for loop in face.loops]
        for k in range(1, len(points)-1):
            p = [points[0], points[k], points[k+1]]
            triangles.append((face.index, p, (min(x for x,y in p), min(y for x,y in p),
                                               max(x for x,y in p), max(y for x,y in p))))
    flips = sum(_area(p) < -1e-12 for _, p, _ in triangles)
    degenerate = sum(abs(_area(p)) < 1e-14 for _, p, _ in triangles)
    overlaps = 0
    self_overlaps = 0
    # Sweep bounding boxes before exact clipping; no dependency beyond Blender.
    rows = sorted(triangles, key=lambda row: row[2][0])
    for i, (fi, a, ba) in enumerate(rows):
        for j in range(i + 1, len(rows)):
            fj, b, bb = rows[j]
            if bb[0] >= ba[2]:
                break
            if fi == fj or bb[1] >= ba[3] or bb[3] <= ba[1]:
                continue
            if _intersection(a, b) > 1e-10:
                overlaps += 1
                self_overlaps += island_of[fi] == island_of[fj]
    zero = 0
    deviation = 0.0
    positions = core.constrained_loops(topo.bm, only_faces=faces)
    layer = core.constraint_layer(topo.bm)
    for pos in positions:
        loop = topo.loops[pos]
        dx, dy = loop.link_loop_next[topo.uvl].uv - loop[topo.uvl].uv
        if math.hypot(dx, dy) < 1e-10:
            zero += 1
            continue
        kind = loop[layer]
        angle = math.degrees(math.atan2(abs(dy), abs(dx)))
        if kind == C_HORIZONTAL:
            deviation = max(deviation, angle)
        elif kind == C_VERTICAL:
            deviation = max(deviation, 90.0-angle)
    for kind, positions in core.constraint_runs(topo.bm, topo, positions):
        if kind in (C_HORIZONTAL, C_VERTICAL):
            continue
        groups = set()
        for pos in positions:
            groups.update(topo.loop_edge_vgroups(pos))
        points = [topo.vgroup_uv(g) for g in groups]
        if len(points) < 3:
            continue
        center = sum(points[1:], points[0].copy()) / len(points)
        rel = [p-center for p in points]
        xx = sum(p.x*p.x for p in rel)
        yy = sum(p.y*p.y for p in rel)
        xy = sum(p.x*p.y for p in rel)
        theta = 0.5*math.atan2(2*xy, xx-yy)
        scale = max((p.length for p in rel), default=0.0)
        if scale > 1e-10:
            deviation = max(deviation, math.degrees(math.asin(min(1.0,
                max(abs(-math.sin(theta)*p.x+math.cos(theta)*p.y) for p in rel)/scale))))
    return dict(flips=flips, overlaps=overlaps, self_overlaps=self_overlaps,
                island_overlaps=overlaps-self_overlaps, degenerate=degenerate,
                zero_edges=zero, max_deviation=deviation,
                finite=all(math.isfinite(v) for _,p,_ in triangles for xy in p for v in xy))


def _triangle_health(triangles):
    """Batch the exact three-term shoelace sum used by _area, in its order."""
    if not len(triangles):
        return 0, 0, True
    values = np.asarray(triangles)
    if values.dtype != np.float64:
        # Internal UV snapshots are doubles. Preserve the old arithmetic for
        # explicit lower precision or integer values passed by other callers.
        flips = degenerate = 0
        finite = True
        for points in triangles:
            finite &= all(math.isfinite(v) for xy in points for v in xy)
            area = _area(points)
            flips += area < -1e-12
            degenerate += abs(area) < 1e-14
        return flips, degenerate, finite
    p, q, r = values[:, 0], values[:, 1], values[:, 2]
    # Match Python float arithmetic without changing a caller's NumPy error
    # policy or producing new console warnings for damaged input UVs.
    with np.errstate(invalid='ignore', over='ignore', under='ignore'):
        area = p[:, 0]*q[:, 1] - q[:, 0]*p[:, 1]
        area += q[:, 0]*r[:, 1] - r[:, 0]*q[:, 1]
        area += r[:, 0]*p[:, 1] - p[:, 0]*r[:, 1]
        area *= 0.5
    return (int(np.count_nonzero(area < -1e-12)),
            int(np.count_nonzero(np.abs(area) < 1e-14)),
            bool(np.isfinite(values).all()))


class AcceptanceProbe:
    """Per-operation safety check for changed faces and their relevant peers."""
    def __init__(self, core, topo, faces, check_other_islands=False, *, triangles=None,
                 coordinates=None):
        self.core, self.faces = core, set(faces)
        self.check_other_islands = check_other_islands
        layer = core.constraint_layer(topo.bm)
        self.positions = ([topo.loop_pos[loop] for fi in sorted(self.faces)
                           for loop in topo.bm.faces[fi].loops if loop[layer] != core.C_NONE]
                          if layer is not None else [])
        self.roots = topo.face_island
        target_roots = {self.roots[f] for f in self.faces}
        self.target = []
        self.static = []
        self.reference = {}
        for tri in topo.loop_triangles() if triangles is None else triangles:
            fi = tri[0].face.index
            if fi not in self.faces and not (check_other_islands or self.roots[fi] in target_roots):
                continue
            row = (fi, tuple(topo.loop_pos[l] for l in tri), self.roots[fi])
            if fi in self.faces:
                self.target.append(row)
            elif check_other_islands or self.roots[fi] in target_roots:
                self.static.append(row)
        self.target_positions = sorted({p for _, pos, _ in self.target for p in pos})
        self.reference = self.capture(topo, coordinates=coordinates)

    def capture(self, topo, *, coordinates=None):
        """Fresh sparse UV values for one candidate; never retain across writes."""
        points = ({p: tuple(topo.loops[p][topo.uvl].uv) for p in self.target_positions}
                  if coordinates is None else
                  {p: tuple(coordinates[p]) for p in self.target_positions})
        return {pos: tuple(points[p] for p in pos) for _, pos, _ in self.target}

    def measure(self, topo, *, coordinates=None, loop_coordinates=None):
        coordinates = (self.capture(topo, coordinates=loop_coordinates)
                       if coordinates is None else coordinates)
        flips, degenerate, finite = _triangle_health(
            [coordinates[pos] for _, pos, _ in self.target])
        zero, deviation = 0, 0.0
        layer = self.core.constraint_layer(topo.bm)
        for pos in self.positions:
            loop = topo.loops[pos]
            # Numeric trials must measure their own state. Vector preserves
            # the float32 subtraction used by the ordinary BMesh path.
            dx, dy = (loop.link_loop_next[topo.uvl].uv - loop[topo.uvl].uv
                      if loop_coordinates is None else
                      Vector(loop_coordinates[topo.next_position(pos)]) - Vector(loop_coordinates[pos]))
            if math.hypot(dx, dy) < 1e-10:
                zero += 1
                continue
            kind = loop[layer]
            angle = math.degrees(math.atan2(abs(dy), abs(dx)))
            if kind == C_HORIZONTAL:
                deviation = max(deviation, angle)
            elif kind == C_VERTICAL:
                deviation = max(deviation, 90.0-angle)
        for kind, positions in self.core.constraint_runs(topo.bm, topo, self.positions):
            if kind in (C_HORIZONTAL, C_VERTICAL):
                continue
            groups = set()
            for pos in positions:
                groups.update(topo.loop_edge_vgroups(pos))
            points = ([topo.vgroup_uv(g) for g in groups] if loop_coordinates is None else
                      [Vector(loop_coordinates[topo.vgroup_loops[g][0]]) for g in groups])
            if len(points) < 3:
                continue
            center = sum(points[1:], points[0].copy()) / len(points)
            rel = [p-center for p in points]
            xx, yy, xy = (sum(p.x*p.x for p in rel), sum(p.y*p.y for p in rel),
                          sum(p.x*p.y for p in rel))
            theta = 0.5*math.atan2(2*xy, xx-yy)
            scale = max((p.length for p in rel), default=0.0)
            if scale > 1e-10:
                deviation = max(deviation, math.degrees(math.asin(min(1.0,
                    max(abs(-math.sin(theta)*p.x+math.cos(theta)*p.y) for p in rel)/scale))))
        return dict(flips=flips, overlaps=None, self_overlaps=None,
                    island_overlaps=None, degenerate=degenerate, zero_edges=zero,
                    max_deviation=deviation, finite=finite,
                    scope='affected_triangles')

    def check_overlaps(self, topo, *, include_existing=False, coordinates=None):
        target = self.target
        current = self.capture(topo) if coordinates is None else coordinates
        self_key = 'self_overlaps' if include_existing else 'novel_self_overlaps'
        island_key = ('island_overlaps' if include_existing
                      else 'novel_island_overlaps')

        def no_overlaps():
            if include_existing:
                return dict(self_overlaps=0, island_overlaps=0,
                            overlap_check_complete=True, scope='affected_triangles')
            return dict(novel_self_overlaps=0, novel_island_overlaps=0,
                        overlap_check_complete=True, scope='affected_triangles')

        if not include_existing and all(current[pos] == self.reference[pos] for pos in current):
            return dict(novel_self_overlaps=0, novel_island_overlaps=0,
                        overlap_check_complete=True, scope='affected_triangles')
        records = []
        for fi, pos, root in target:
            records.append((fi, pos, root, current[pos], True))
        for fi, pos, root in self.static:
            p = tuple(tuple(float(x) for x in topo.loops[i][topo.uvl].uv) for i in pos)
            records.append((fi, pos, root, p, False))
        native = native_engine.overlaps(records, self.reference, self.check_other_islands, include_existing)
        if native is not None:
            kind, _ = native
            if kind == 0:
                return no_overlaps()
            return {self_key: int(kind == 1), island_key: int(kind == 2),
                    'overlap_check_complete': False, 'scope': 'affected_triangles'}
        sizes = [max(max(x for x, y in r[3])-min(x for x, y in r[3]),
                     max(y for x, y in r[3])-min(y for x, y in r[3]))
                 for r in records if all(math.isfinite(v) for xy in r[3] for v in xy)]
        if not sizes:
            return no_overlaps()
        cell = max(sorted(s for s in sizes if s > 0)[len([s for s in sizes if s > 0])//2]*2
                   if any(s > 0 for s in sizes) else 1e-12, 1e-12)
        grid, large, buckets = {}, {}, {}
        boxes = {}
        for n, r in enumerate(records):
            p = r[3]
            if not all(math.isfinite(v) for xy in p for v in xy):
                continue
            box = (min(x for x,y in p), min(y for x,y in p),
                   max(x for x,y in p), max(y for x,y in p))
            boxes[n] = box
            bucket = None if self.check_other_islands else r[2]
            buckets.setdefault(bucket, []).append(n)
            ix0, iy0 = math.floor(box[0]/cell), math.floor(box[1]/cell)
            ix1, iy1 = math.floor(box[2]/cell), math.floor(box[3]/cell)
            if (ix1-ix0+1)*(iy1-iy0+1) > 64:
                large.setdefault(bucket, []).append(n)
            else:
                for ix in range(ix0, ix1+1):
                    for iy in range(iy0, iy1+1):
                        grid.setdefault((bucket,ix,iy), []).append(n)
        for i, a in enumerate(records):
            if not a[4] or i not in boxes:
                continue
            box = boxes[i]
            bucket = None if self.check_other_islands else a[2]
            ix0, iy0 = math.floor(box[0]/cell), math.floor(box[1]/cell)
            ix1, iy1 = math.floor(box[2]/cell), math.floor(box[3]/cell)
            candidates = set(large.get(bucket, ()))
            if (ix1-ix0+1)*(iy1-iy0+1) > 64:
                candidates.update(buckets.get(bucket, ()))
            else:
                for ix in range(ix0, ix1+1):
                    for iy in range(iy0, iy1+1):
                        candidates.update(grid.get((bucket,ix,iy), ()))
            for j in candidates:
                if j == i or j not in boxes:
                    continue
                b = records[j]
                if a[4] and b[4] and j < i:
                    continue
                if a[0] == b[0] or (not self.check_other_islands and a[2] != b[2]):
                    continue
                if (box[0] >= boxes[j][2] or boxes[j][0] >= box[2] or
                    box[1] >= boxes[j][3] or boxes[j][1] >= box[3]):
                    continue
                if _intersection(a[3], b[3]) <= 1e-10:
                    continue
                if include_existing:
                    same_island = a[2] == b[2]
                    return {self_key: int(same_island),
                            island_key: int(not same_island),
                            'overlap_check_complete': False,
                            'scope': 'affected_triangles'}
                olda = self.reference.get(a[1], a[3])
                oldb = self.reference.get(b[1], b[3])
                if _intersection(olda, oldb) <= 1e-10:
                    same_island = a[2] == b[2]
                    return {self_key: int(same_island),
                            island_key: int(not same_island),
                            'overlap_check_complete': False,
                            'scope': 'affected_triangles'}
        return no_overlaps()


def accepts(before, after, max_deviation=0.05, prevent_overlap=True):
    # An island changing shape can reach its neighbour. Only the explicit
    # Prevent overlaps option forbids that; self-intersections remain unsafe.
    overlap_key = 'overlaps' if prevent_overlap else 'self_overlaps'
    return (after['finite'] and after['zero_edges'] == 0
            and after['max_deviation'] <= max_deviation
            and all(after[k] <= before[k] for k in ('flips', overlap_key, 'degenerate')))
