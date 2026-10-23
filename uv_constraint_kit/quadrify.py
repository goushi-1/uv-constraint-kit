# SPDX-License-Identifier: GPL-3.0-or-later
"""Transactional UV quadrification with an independent integer-grid solver."""
import math
import time
import bmesh
from . import core, safety, quad_grid


def _signed_area(points):
    return sum(a[0]*b[1] - b[0]*a[1]
               for a, b in zip(points, points[1:] + points[:1])) * 0.5


def _pose(bm, uvl, faces):
    area = 0.0
    cx = cy = 0.0
    for face in bm.faces:
        if face.index not in faces:
            continue
        points = [(float(loop[uvl].uv.x), float(loop[uvl].uv.y))
                  for loop in face.loops]
        twice = sum(a[0]*b[1] - b[0]*a[1]
                    for a, b in zip(points, points[1:] + points[:1]))
        signed = twice * 0.5
        if abs(signed) <= 1e-14:
            continue
        px = sum((a[0]+b[0])*(a[0]*b[1]-b[0]*a[1])
                 for a, b in zip(points, points[1:] + points[:1])) / (6*signed)
        py = sum((a[1]+b[1])*(a[0]*b[1]-b[0]*a[1])
                 for a, b in zip(points, points[1:] + points[:1])) / (6*signed)
        weight = abs(signed)
        area += weight
        cx += px * weight
        cy += py * weight
    if area <= 1e-14:
        raise ValueError('UV island has zero area')
    return area, (cx/area, cy/area)


def _internal_uv_connections(bm, uvl, faces):
    """Identity set of originally joined UV edges internal to these faces."""
    wanted = set(faces)
    connections = set()
    for edge in bm.edges:
        linked_faces = sorted(edge.link_faces, key=lambda face: face.index)
        if len(linked_faces) != 2 or any(face.index not in wanted
                                         for face in linked_faces):
            continue
        loops = [next(loop for loop in face.loops if loop.edge == edge)
                 for face in linked_faces]
        if core.UVTopology._edge_uv_matches(loops[0], loops[1], uvl):
            connections.add((edge.index, linked_faces[0].index,
                             linked_faces[1].index,
                             tuple(sorted(v.index for v in edge.verts))))
    return connections


def _connection_rows(connections):
    return [dict(mesh_edge=edge, faces=[face_a, face_b], vertices=list(vertices))
            for edge, face_a, face_b, vertices in sorted(connections)]


def _snapshot(bm, uvl):
    loops = core.loops_in_order(bm)
    layer = core.constraint_layer(bm)
    return {
        'uv': [(float(loop[uvl].uv.x), float(loop[uvl].uv.y)) for loop in loops],
        'pins': [bool(loop[uvl].pin_uv) for loop in loops],
        'constraints': ([int(loop[layer]) for loop in loops] if layer else None),
        'vert_select': [bool(v.select) for v in bm.verts],
        'edge_select': [bool(e.select) for e in bm.edges],
        'face_select': [bool(f.select) for f in bm.faces],
        'uv_vert_select': [bool(loop.uv_select_vert) for loop in loops],
        'uv_edge_select': [bool(loop.uv_select_edge) for loop in loops],
        'uv_face_select': [bool(f.uv_select) for f in bm.faces],
        'edge_seams': [bool(e.seam) for e in bm.edges],
        'active_face': (bm.faces.active.index if bm.faces.active is not None else None),
        'select_history': [('VERT' if isinstance(item, bmesh.types.BMVert) else
                            'EDGE' if isinstance(item, bmesh.types.BMEdge) else 'FACE', item.index)
                           for item in bm.select_history],
    }


def _restore_uv(bm, uvl, values):
    for loop, uv in zip(core.loops_in_order(bm), values):
        loop[uvl].uv = uv


def _restore_marks(bm, uvl, state):
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    # Restore coarse elements first: selecting/deselecting a face or edge can
    # implicitly flush selection to its vertices in BMesh.
    for element, value in zip(bm.faces, state['face_select']):
        element.select = value
    for element, value in zip(bm.edges, state['edge_select']):
        element.select = value
    for element, value in zip(bm.verts, state['vert_select']):
        element.select = value
    for face, value in zip(bm.faces, state['uv_face_select']):
        face.uv_select = value
    loops = core.loops_in_order(bm)
    for loop, vert, edge in zip(loops, state['uv_vert_select'], state['uv_edge_select']):
        loop.uv_select_vert = vert
        loop.uv_select_edge = edge
    for edge, value in zip(bm.edges, state['edge_seams']):
        edge.seam = value
    active = state['active_face']
    bm.faces.active = bm.faces[active] if active is not None and active < len(bm.faces) else None
    bm.select_history.clear()
    for kind, index in state['select_history']:
        bm.select_history.add({'VERT': bm.verts, 'EDGE': bm.edges, 'FACE': bm.faces}[kind][index])


def _fit_pose(bm, uvl, island, original_area, original_center):
    current_area, current_center = _pose(bm, uvl, island)
    scale = math.sqrt(original_area/current_area)
    for face in bm.faces:
        if face.index not in island:
            continue
        for loop in face.loops:
            x, y = loop[uvl].uv
            loop[uvl].uv = (original_center[0] + (x-current_center[0])*scale,
                            original_center[1] + (y-current_center[1])*scale)


def _seed(bm, uvl, island):
    quads = [f for f in bm.faces if f.index in island and len(f.verts) == 4 and not f.hide]
    if not quads:
        return None
    active = bm.faces.active
    if active in quads:
        return active.index
    points = [lp[uvl].uv.copy() for f in quads for lp in f.loops]
    center = ((min(p.x for p in points)+max(p.x for p in points))*0.5,
              (min(p.y for p in points)+max(p.y for p in points))*0.5)
    return min(quads, key=lambda f: ((sum(lp[uvl].uv.x for lp in f.loops)/4-center[0])**2 +
                                  (sum(lp[uvl].uv.y for lp in f.loops)/4-center[1])**2)).index


def _island(context, obj, island, shape, average_shape, skip_non_quads,
            respect_pins, prevent_overlap, preserve_uv_connections):
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    uvl = core.uv_layer(bm)
    state = _snapshot(bm, uvl)
    topo = core.UVTopology(bm, uvl)
    seed = _seed(bm, uvl, island)
    if seed is None:
        return dict(faces=sorted(island), skipped=True, reason='Island has no visible quad face')
    non_quads = sum(len(f.verts) != 4 for f in bm.faces if f.index in island)
    if skip_non_quads and non_quads:
        return dict(faces=sorted(island), skipped=True, reason='Island contains non-quad faces')
    area, center = _pose(bm, uvl, island)
    before_quality = safety.measure(core, topo, faces=island)
    connections = _internal_uv_connections(bm, uvl, island)
    pin_groups = [(positions, state['uv'][next(p for p in positions if state['pins'][p])])
                  for positions in topo.vgroup_loops.values()
                  if any(state['pins'][p] and topo.loops[p].face.index in island for p in positions)]
    started = time.perf_counter()
    try:
        image = getattr(getattr(context, 'space_data', None), 'image', None)
        min_limit = 1.0/image.size[0] if image and image.size[0] else 0.002
        quad_grid.solve(bm, uvl, island, seed, shape, average_shape, min_limit)
        _fit_pose(bm, uvl, island, area, center)
        loops = core.loops_in_order(bm)
        if respect_pins:
            for positions, anchor in pin_groups:
                if any(math.dist(tuple(loops[p][uvl].uv), anchor) > 1e-7 for p in positions):
                    raise ValueError('Pinned UV conflicts with quadrification')
                for p in positions:
                    loops[p][uvl].uv = anchor
        current_connections = _internal_uv_connections(bm, uvl, island)
        cuts = connections-current_connections
        if preserve_uv_connections and cuts:
            raise ValueError('Quadrification would split existing UV connections')
        quality = safety.measure(core, core.UVTopology(bm, uvl), faces=island)
        if (not quality['finite'] or quality['degenerate'] > before_quality['degenerate'] or
                quality['flips'] > before_quality['flips'] or quality['zero_edges'] or
                (prevent_overlap and quality['overlaps'] > before_quality['overlaps'])):
            raise ValueError('Quadrification failed the UV safety check')
        if quality['max_deviation'] > 0.05:
            raise ValueError('Quadrification violates a marked constraint')
        after_area, after_center = _pose(bm, uvl, island)
        area_error = abs(after_area/area-1.0)
        center_error = math.dist(after_center, center)
        if area_error > 1e-5 or center_error > 1e-7:
            raise ValueError('Area or centroid restoration failed')
        changed = any(math.dist(tuple(lp[uvl].uv), state['uv'][p]) > 1e-7
                      for p, lp in enumerate(loops) if lp.face.index in island)
        if not changed:
            _restore_uv(bm, uvl, state['uv'])
            quality = before_quality
            cuts = set()
            current_connections = connections
            area_error = center_error = 0.0
        row = dict(passed=True, changed=changed, faces=sorted(island), seed=seed,
                   chosen_seed=seed, quads=len(island)-non_quads, non_quads=non_quads,
                   algorithm='INTEGER_GRID', shape=shape, average_shape=average_shape,
                   before_quality=before_quality, quality=quality,
                   area_error=area_error, centroid_error=center_error,
                   new_uv_boundaries=len(cuts), new_uv_boundary_edges=_connection_rows(cuts),
                   uv_connections_before=len(connections), uv_connections_after=len(current_connections),
                   elapsed_seconds=time.perf_counter()-started)
    except Exception as error:
        reason = str(error) or type(error).__name__
        error.__traceback__ = error.__context__ = error.__cause__ = None
        _restore_uv(bm, uvl, state['uv'])
        row = dict(faces=sorted(island), skipped=True, reason=reason,
                   chosen_seed=seed, elapsed_seconds=time.perf_counter()-started)
    _restore_marks(bm, uvl, state)
    del topo, uvl, bm
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    # The update may flush selection; restore exact marks without changing UVs.
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    _restore_marks(bm, core.uv_layer(bm), state)
    return row


def quadrify(context, obj, faces, mode=None, respect_pins=True,
             prevent_overlap=False, preserve_uv_connections=False,
             shape='PER_FACE', average_shape=True, skip_non_quads=False):
    """Integer-grid quadrification of whole islands touched by the selection.

    Preserve the original island area/centroid and all marks/selection flags.
    Inconsistent lattice topology is rejected without introducing UV cuts.
    The old ``mode`` argument remains accepted for existing scripts.
    """
    if mode is not None:
        if mode not in {'LENGTH_AVERAGE', 'LENGTH', 'EVEN'}:
            raise ValueError('Invalid quad edge spacing')
        shape = 'EVEN' if mode == 'EVEN' else 'PER_FACE'
        average_shape = mode != 'LENGTH'
    if shape not in {'PER_FACE', 'FACE', 'EVEN'}:
        raise ValueError('Invalid quad face shape')
    result = dict(changed=0, faces=0, skipped=[], islands=[],
                  new_uv_boundaries=0, new_uv_boundary_edges=[])
    if obj is None or obj.type != 'MESH' or obj.mode != 'EDIT':
        result['skipped'].append('Object is not an edit-mode mesh')
        return result
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    uvl = core.uv_layer(bm)
    if uvl is None:
        result['skipped'].append('Mesh has no UV map')
        return result
    topo = core.UVTopology(bm, uvl)
    requested = core.islands_touching_faces(topo, {int(i) for i in faces})
    from . import live_symmetry
    requested = live_symmetry.source_faces(obj, requested)
    groups = [set(group) for group in core.face_islands(topo, requested).values()]
    del topo, uvl, bm
    for island in groups:
        row = _island(context, obj, island, shape, average_shape, skip_non_quads,
                      respect_pins, prevent_overlap, preserve_uv_connections)
        result['islands'].append(row)
        if row.get('skipped'):
            result['skipped'].append(row['reason'])
        else:
            result['changed'] += bool(row['changed'])
            result['faces'] += len(island) if row['changed'] else 0
            result['new_uv_boundaries'] += row['new_uv_boundaries']
            result['new_uv_boundary_edges'].extend(row['new_uv_boundary_edges'])
    return result
