# SPDX-License-Identifier: GPL-3.0-or-later
"""Final UV acceptance, including triangle intersections and degenerate edges."""
import math
import weakref
from operator import is_ as _same_object
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


def folded_face_candidates(topo, triangles, crosses, reference, *, _tri_faces=None):
    """Cache only input faces with mixed winding AND a crossing UV boundary.

    A normal mirrored or concave face is not a repair candidate. A repair must
    make every native triangle agree with the input face's net orientation;
    the caller still checks pins, area, selection and inter-face intersections.
    """
    if not len(triangles):
        return {}
    tri_f = _tri_faces
    if tri_f is None and hasattr(topo, 'loop_faces'):
        tri_f = topo.loop_faces[np.asarray([t[0] for t in triangles], dtype=np.intp)]
    if tri_f is not None:
        cross_arr = np.asarray(crosses, dtype=np.float64)
        pos_f = set(np.unique(tri_f[cross_arr > 2e-14]).tolist())
        neg_f = set(np.unique(tri_f[cross_arr < -2e-14]).tolist())
        mixed = pos_f & neg_f
        if not mixed:
            return {}
        by_face = {fi: [] for fi in mixed}
        cand_idx = np.flatnonzero(np.isin(tri_f, sorted(mixed)))
        for i in cand_idx.tolist():
            by_face[int(tri_f[i])].append(i)
    else:
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


def _orientation_sign(a, b, c):
    """Exact sign of the determinant of finite stored binary coordinates."""
    ratios = [float(p[axis]).as_integer_ratio() for axis in (0, 1) for p in (a, b, c)]
    values = []
    for axis in (0, 1):
        rows = ratios[3*axis:3*axis+3]
        denominator = max(q for _, q in rows)
        values.append([p*(denominator//q) for p, q in rows])
    x, y = values
    determinant = (x[1]-x[0])*(y[2]-y[0])-(y[1]-y[0])*(x[2]-x[0])
    return (determinant > 0)-(determinant < 0)


def _proper_triangle_crossing(a, b):
    """Only strict edge crossings of nondegenerate triangles; touches stay legal."""
    if not all(math.isfinite(float(v)) for point in (*a,*b) for v in point):
        return False
    for i in range(3):
        p, q = a[i], a[(i+1) % 3]
        for j in range(3):
            r, s = b[j], b[(j+1) % 3]
            if (max(p[0],q[0]) < min(r[0],s[0]) or max(r[0],s[0]) < min(p[0],q[0])
                    or max(p[1],q[1]) < min(r[1],s[1]) or max(r[1],s[1]) < min(p[1],q[1])):
                continue
            first, second = _orientation_sign(p,q,r), _orientation_sign(p,q,s)
            if first == 0 or second == 0 or first == second:
                continue
            third, fourth = _orientation_sign(r,s,p), _orientation_sign(r,s,q)
            if third != 0 and fourth != 0 and third != fourth:
                return _orientation_sign(*a) != 0 and _orientation_sign(*b) != 0
    return False


def _triangles_overlap(a, b):
    # Preserve the existing area policy for containment/stacks; strict crossings
    # must not disappear under its absolute area threshold.
    return _intersection(a,b) > 1e-10 or _proper_triangle_crossing(a,b)


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
            if _triangles_overlap(a, b):
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


def _same_sequence(actual, expected):
    return (type(actual) in (list, tuple) and len(actual) == len(expected)
            and all(map(_same_object, actual, expected)))


class _TrialLayout:
    """Fully verified immutable integer metadata; no coordinates or mesh."""
    def __init__(self, owner, rows, positions, order, loop_count):
        self._owner = weakref.ref(owner)
        self.rows, self.positions, self.order = rows, positions, order
        self.loop_count = loop_count
        self.indices = np.asarray(order, dtype=np.intp)
        self.indices.flags.writeable = False
        self._overlap_witness = np.asarray((-1, -1, 0), dtype=np.int64)

    def bound(self, owner):
        return (self._owner() is owner and _same_sequence(owner.target, self.rows)
                and _same_sequence(owner.target_positions, self.positions))

    def matches(self, owner, order, loop_count):
        return (type(loop_count) is int and loop_count == self.loop_count
                and self.bound(owner) and _same_sequence(order, self.order))


class _TrialSnapshot:
    """Private frozen numeric trial; never a live mesh or general Mapping."""
    def __init__(self, owner, coordinates, layout):
        self.layout = layout
        self._owner = layout._owner
        self.rows, self.positions = layout.rows, layout.positions
        self.triangles = coordinates[layout.indices]
        self.triangles.flags.writeable = False
        self._point_rows, self._points, self._row_values = None, {}, {}
        self._mapping = None

    def _index_points(self):
        if self._point_rows is None:
            self._point_rows = {}
            for i, (_fi, pos, _root) in enumerate(self.rows):
                for j, p in enumerate(pos):
                    self._point_rows.setdefault(p, (i, j))

    def bound(self, owner):
        return self.triangles is not None and self.layout.bound(owner)

    def row(self, pos):
        if self.triangles is None:
            raise RuntimeError('expired private trial snapshot')
        if pos not in self._row_values:
            self._index_points()
            for p in pos:
                if p not in self._points:
                    i, j = self._point_rows[p]
                    self._points[p] = tuple(self.triangles[i, j])
            self._row_values[pos] = tuple(self._points[p] for p in pos)
        return self._row_values[pos]

    def materialize(self):
        if self.triangles is None:
            raise RuntimeError('expired private trial snapshot')
        if self._mapping is None:
            self._index_points()
            for p in self.positions:
                if p not in self._points:
                    i, j = self._point_rows[p]
                    self._points[p] = tuple(self.triangles[i, j])
            self._mapping = {pos: self.row(pos) for _fi, pos, _root in self.rows}
        return self._mapping

    def invalidate(self):
        self.triangles = self._mapping = self.layout = None
        self._point_rows = None
        self._points.clear()
        self._row_values.clear()


class _TriangleMapping:
    """Lazy pos->triangle view over a contiguous (N, 3, 2) float64 array."""
    def __init__(self, target, array):
        self._target = target
        self._array = array
        self._index = None

    def __getitem__(self, pos):
        if self._index is None:
            self._index = {p: i for i, (_fi, p, _root) in enumerate(self._target)}
        return tuple(map(tuple, self._array[self._index[pos]].tolist()))


class AcceptanceProbe:
    """Per-operation safety check for changed faces and their relevant peers."""
    def __init__(self, core, topo, faces, check_other_islands=False, *, triangles=None,
                 coordinates=None):
        self.core, self.faces = core, set(faces)
        self.check_other_islands = check_other_islands
        layer = core.constraint_layer(topo.bm)
        if layer is None:
            self.positions = []
        elif hasattr(core, 'constrained_loops'):
            self.positions = core.constrained_loops(topo.bm, only_faces=self.faces)
        else:
            self.positions = [topo.loop_pos[loop] for fi in sorted(self.faces)
                              for loop in topo.bm.faces[fi].loops if loop[layer] != core.C_NONE]
        self.roots = topo.face_island
        target_roots = {self.roots[f] for f in self.faces}
        self.target = []
        self.static = []
        self.reference = {}
        tri_rows = (topo.triangle_rows()
                    if triangles is None and hasattr(topo, 'triangle_rows') else None)
        all_faces = (tri_rows is not None and hasattr(topo, 'bm')
                     and len(self.faces) == len(topo.bm.faces))
        if all_faces:
            roots = self.roots
            self.target = [(fi, pos, roots[fi]) for fi, pos in tri_rows]
            self._target_idx = (topo._triangle_arrays()[1]
                                if hasattr(topo, '_triangle_arrays')
                                else np.asarray([pos for _, pos, _ in self.target], dtype=np.intp).reshape(-1, 3))
        elif tri_rows is not None:
            for fi, pos in tri_rows:
                if fi not in self.faces and not (check_other_islands or self.roots[fi] in target_roots):
                    continue
                row = (fi, pos, self.roots[fi])
                if fi in self.faces:
                    self.target.append(row)
                elif check_other_islands or self.roots[fi] in target_roots:
                    self.static.append(row)
            self._target_idx = np.asarray([pos for _, pos, _ in self.target], dtype=np.intp).reshape(-1, 3)
        else:
            for tri in topo.loop_triangles() if triangles is None else triangles:
                fi = tri[0].face.index
                if fi not in self.faces and not (check_other_islands or self.roots[fi] in target_roots):
                    continue
                row = (fi, tuple(topo.loop_pos[l] for l in tri), self.roots[fi])
                if fi in self.faces:
                    self.target.append(row)
                elif check_other_islands or self.roots[fi] in target_roots:
                    self.static.append(row)
            self._target_idx = np.asarray([pos for _, pos, _ in self.target], dtype=np.intp).reshape(-1, 3)
        self._reference_dict = None
        if self.target:
            slots, slot_order = np.unique(self._target_idx, return_inverse=True)
            self._target_slots = slots.tolist()
            self._target_slot_order = slot_order.reshape(-1, 3)
            self.target_positions = list(self._target_slots)
        else:
            self._target_slots = []
            self._target_slot_order = np.empty((0, 3), dtype=np.intp)
            self.target_positions = []
        self._ref_array = self._capture_triangles(topo, coordinates=coordinates)
        self._init_target = list(self.target)
        self._init_static = list(self.static)
        self._init_loop_count = len(topo.loops)

    @property
    def reference(self):
        if self._reference_dict is None:
            if self._ref_array is not None and self.target:
                self._reference_dict = {
                    pos: tuple(map(tuple, row))
                    for (_, pos, _), row in zip(self.target, self._ref_array.tolist())
                }
            else:
                self._reference_dict = {}
            self._ref_array = None
        return self._reference_dict

    @reference.setter
    def reference(self, value):
        self._reference_dict = value
        self._ref_array = None

    def _triangle_view(self, array):
        return _TriangleMapping(self.target, array)

    def _capture_triangles(self, topo, *, coordinates=None):
        """Fresh contiguous (N, 3, 2) float64 triangle UV array."""
        if not self.target:
            return np.empty((0, 3, 2), dtype=np.float64)
        if coordinates is not None:
            if isinstance(coordinates, dict):
                return np.ascontiguousarray(
                    [[coordinates[p] for p in pos] for _, pos, _ in self.target],
                    dtype=np.float64,
                )
            coords = np.asarray(coordinates, dtype=np.float64)
            return np.ascontiguousarray(coords[self._target_idx], dtype=np.float64)
        slot_uvs = np.asarray(
            self.core.mesh_reader.read_uv([topo.loops[p] for p in self._target_slots], topo.uvl),
            dtype=np.float64,
        )
        return np.ascontiguousarray(slot_uvs[self._target_slot_order], dtype=np.float64)

    def _trial_snapshot(self, coordinates, triangle_positions, loop_count, *, _layout=None):
        """Only an exact interpolation input and its ordered probe may opt in."""
        if (type(self) is not AcceptanceProbe or type(coordinates) is not np.ndarray
                or coordinates.dtype != np.float64 or coordinates.shape != (loop_count, 2)
                or not coordinates.flags.owndata or not coordinates.flags.c_contiguous
                or not coordinates.size or type(self.target) not in (list, tuple)
                or type(self.target_positions) not in (list, tuple)
                or type(triangle_positions) not in (list, tuple)):
            return None
        if type(_layout) is _TrialLayout and _layout.matches(self, triangle_positions, loop_count):
            return _TrialSnapshot(self, coordinates, _layout)
        rows, positions = tuple(self.target), tuple(self.target_positions)
        if not rows or not all(type(row) is tuple and len(row) == 3
                and type(row[0]) is int and type(row[2]) is int
                and type(row[1]) is tuple and len(row[1]) == 3
                and all(type(p) is int and 0 <= p < loop_count for p in row[1]) for row in rows):
            return None
        if (not all(type(pos) is tuple and len(pos) == 3
                    and all(type(p) is int for p in pos) for pos in triangle_positions)
                or tuple(triangle_positions) != tuple(pos for _fi, pos, _root in rows)
                or not all(type(p) is int for p in positions)
                or positions != tuple(sorted({p for _fi, pos, _root in rows for p in pos}))):
            return None
        layout = _TrialLayout(self, rows, positions, tuple(triangle_positions), loop_count)
        return _TrialSnapshot(self, coordinates, layout)

    def capture(self, topo, *, coordinates=None):
        """Fresh sparse UV values for one candidate; never retain across writes."""
        if coordinates is None:
            if not self.target_positions:
                points = {}
            else:
                coords = self.core.mesh_reader.read_uv(
                    [topo.loops[p] for p in self.target_positions], topo.uvl)
                values = coords.tolist() if isinstance(coords, np.ndarray) else coords
                points = dict(zip(self.target_positions, map(tuple, values)))
        else:
            points = {p: tuple(coordinates[p]) for p in self.target_positions}
        return {pos: tuple(points[p] for p in pos) for _, pos, _ in self.target}

    def measure(self, topo, *, coordinates=None, loop_coordinates=None, _triangles=None):
        if _triangles is None:
            if coordinates is None:
                _triangles = self._capture_triangles(topo, coordinates=loop_coordinates)
            elif isinstance(coordinates, np.ndarray):
                _triangles = coordinates
            elif type(coordinates) is _TriangleMapping:
                _triangles = coordinates._array
            else:
                _triangles = [coordinates[pos] for _, pos, _ in self.target]
        # A checked transaction can share this exact ordered snapshot
        # with its orientation guard. Other callers retain fresh packing.
        flips, degenerate, finite = _triangle_health(_triangles)
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

    def _overlap_buffers(self, topo, current, *, include_existing=False):
        # Cache integer row metadata only. Coordinates and reference values
        # are packed afresh, including live static faces on every check.
        is_array = (type(current) is np.ndarray and current.dtype == np.float64
                    and current.shape == (len(self.target), 3, 2))
        has_ref_array = self._reference_dict is None and self._ref_array is not None
        if (type(self) is not AcceptanceProbe or (type(current) is not dict and not is_array)
                or (not has_ref_array and type(self._reference_dict) is not dict)
                or type(self.target) not in (list, tuple)
                or type(self.static) not in (list, tuple)):
            return None
        layout = getattr(self, '_overlap_layout', None)
        if layout is None or not (_same_sequence(self.target, layout['target'])
                                 and _same_sequence(self.static, layout['static'])):
            target, static = tuple(self.target), tuple(self.static)
            rows = target + static
            trusted = (len(topo.loops) == self._init_loop_count
                       and self.target == self._init_target
                       and self.static == self._init_static)
            if not rows or (not trusted and not all(type(r) is tuple and len(r) == 3
                    and type(r[0]) is int and type(r[2]) is int
                    and type(r[1]) is tuple and len(r[1]) == 3
                    and all(type(p) is int and 0 <= p < len(topo.loops) for p in r[1])
                    for r in rows)):
                return None
            static_indices = np.asarray([r[1] for r in static], dtype=np.intp).reshape(-1, 3)
            slots, order = np.unique(static_indices, return_inverse=True)
            layout = dict(target=target, static=static, rows=rows, loop_count=len(topo.loops),
                slots=slots.tolist(), order=order.reshape(-1, 3),
                faces=np.asarray([r[0] for r in rows], dtype=np.int64),
                roots=np.asarray([r[2] for r in rows], dtype=np.int64),
                changed=np.asarray([1]*len(target) + [0]*len(static), dtype=np.uint8))
            self._overlap_layout = layout
        if layout['loop_count'] != len(topo.loops):
            return None
        count = len(layout['target'])
        value = np.empty((len(layout['rows']), 3, 2), dtype=np.float64)
        if count:
            if is_array:
                value[:count] = current
            else:
                value[:count] = [current[pos] for _fi, pos, _root in layout['target']]
        if layout['static']:
            live = np.asarray(self.core.mesh_reader.read_uv(
                [topo.loops[p] for p in layout['slots']], topo.uvl), dtype=np.float64)
            value[count:] = live[layout['order']]
        if include_existing:
            previous = value
        else:
            previous = value.copy()
            if has_ref_array:
                if count:
                    previous[:count] = self._ref_array
            else:
                missing = object()
                ref_dict = self._reference_dict
                for i, (_fi, pos, _root) in enumerate(layout['rows']):
                    old = ref_dict.get(pos, missing)
                    if old is not missing:
                        previous[i] = old
        return [layout['faces'], layout['roots'], layout['changed'], value, previous]

    def check_overlaps(self, topo, *, include_existing=False, coordinates=None):
        target = self.target
        native_done, trial_native = False, None
        if type(coordinates) is _TrialSnapshot:
            if (coordinates._mapping is None and coordinates.bound(self) and include_existing is True
                    and type(self.static) in (list, tuple) and not self.static
                    and (self._reference_dict is None or type(self._reference_dict) is dict)):
                trial_native = native_engine._overlaps_trial(coordinates, self._reference_dict or {}, self.check_other_islands)
                native_done = True
                if trial_native is not None:
                    kind, _ = trial_native
                    return dict(self_overlaps=int(kind == 1), island_overlaps=int(kind == 2),
                                overlap_check_complete=kind == 0, scope='affected_triangles')
            coordinates = coordinates.materialize()
        if type(coordinates) is _TriangleMapping:
            coordinates = coordinates._array
        current = self._capture_triangles(topo) if coordinates is None else coordinates
        self_key = 'self_overlaps' if include_existing else 'novel_self_overlaps'
        island_key = ('island_overlaps' if include_existing
                      else 'novel_island_overlaps')

        def no_overlaps():
            if include_existing:
                return dict(self_overlaps=0, island_overlaps=0,
                            overlap_check_complete=True, scope='affected_triangles')
            return dict(novel_self_overlaps=0, novel_island_overlaps=0,
                        overlap_check_complete=True, scope='affected_triangles')

        if not include_existing:
            if type(current) is np.ndarray and self._reference_dict is None and self._ref_array is not None:
                if np.array_equal(current, self._ref_array):
                    return no_overlaps()
            elif type(current) is dict and all(current[pos] == self.reference[pos] for pos in current):
                return no_overlaps()
        if not native_done and native_engine._load() is not None:
            buffers = self._overlap_buffers(topo, current, include_existing=include_existing)
            if buffers is not None:
                trial_native = native_engine._overlaps_arrays(
                    buffers, self.check_other_islands, include_existing)
                native_done = True
                if trial_native is not None:
                    kind, _ = trial_native
                    if kind == 0:
                        return no_overlaps()
                    return {self_key: int(kind == 1), island_key: int(kind == 2),
                            'overlap_check_complete': False, 'scope': 'affected_triangles'}

        if type(current) is np.ndarray:
            current = {pos: tuple(map(tuple, row)) for (_, pos, _), row in zip(target, current.tolist())}
        records = []
        for fi, pos, root in target:
            records.append((fi, pos, root, current[pos], True))
        for fi, pos, root in self.static:
            p = tuple(tuple(float(x) for x in topo.loops[i][topo.uvl].uv) for i in pos)
            records.append((fi, pos, root, p, False))
        native = trial_native if native_done else native_engine.overlaps(records, self.reference, self.check_other_islands, include_existing)
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
                if not _triangles_overlap(a[3], b[3]):
                    continue
                if include_existing:
                    same_island = a[2] == b[2]
                    return {self_key: int(same_island),
                            island_key: int(not same_island),
                            'overlap_check_complete': False,
                            'scope': 'affected_triangles'}
                olda = self.reference.get(a[1], a[3])
                oldb = self.reference.get(b[1], b[3])
                if not _triangles_overlap(olda, oldb):
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
