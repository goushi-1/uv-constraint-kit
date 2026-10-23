# SPDX-License-Identifier: GPL-3.0-or-later
"""Circular island boundaries with a harmonic interior and metric refinement.

Selection identifies complete UV islands or explicit closed edge loops.
Successful shapes become persistent templates; topology, seams, user pins,
directional constraint marks and selection stay intact.
"""
import math

import bmesh
import numpy as np

from . import core, metric, safety


def selected_island_faces(context, bm, uvl):
    # In Blender 5.2, a UV selection may be valid before its mesh selection
    # mirrors it. UV corners also carry the seam side; mesh edges do not.
    positions = core.selected_uv_edges(context, bm, mesh_fallback=False)
    if positions:
        loops = core.loops_in_order(bm)
        return core.connected_uv_faces(bm, uvl, {loops[p].face.index for p in positions})
    sync = context.scene.tool_settings.use_uv_select_sync
    touched = set()
    if sync:
        mode = context.scene.tool_settings.mesh_select_mode
        for face in bm.faces:
            if face.hide:
                continue
            selected = (face.select if mode[2] else
                        any(edge.select for edge in face.edges) if mode[1] else
                        any(vert.select for vert in face.verts))
            if selected:
                touched.add(face.index)
    else:
        mode = context.scene.tool_settings.uv_select_mode
        for face in bm.faces:
            if face.hide or not face.select:
                continue
            flags = [loop.uv_select_vert for loop in face.loops]
            selected = (any(loop.uv_select_edge for loop in face.loops)
                        if mode == 'EDGE' else getattr(face, 'uv_select', all(flags))
                        if mode in {'FACE', 'ISLAND'} else any(flags))
            if selected:
                touched.add(face.index)
    return core.connected_uv_faces(bm, uvl, touched)


def _area(points):
    q = np.roll(points, -1, axis=0)
    return float(np.sum(points[:, 0]*q[:, 1]-q[:, 0]*points[:, 1])*.5)


def _boundary_cycles(topo, faces):
    outgoing, incoming = {}, {}
    for position in core.selection_border_loops(topo, faces):
        a, b = topo.loop_edge_vgroups(position)
        if a == b or a in outgoing or b in incoming:
            raise ValueError('The island boundary is branched or non-manifold')
        outgoing[a], incoming[b] = b, a
    if not outgoing or outgoing.keys() != incoming.keys():
        raise ValueError('The island has no closed UV boundary')
    remaining, cycles = set(outgoing), []
    while remaining:
        start = min(remaining)
        g, cycle = start, []
        while g in remaining:
            remaining.remove(g)
            cycle.append(g)
            g = outgoing[g]
        if g != start or len(cycle) < 3:
            raise ValueError('The island has no closed UV boundary')
        cycles.append(cycle)
    return cycles


def _aspect(context):
    space = getattr(context, 'space_data', None)
    image = getattr(space, 'image', None)
    if image and image.size[0] > 0 and image.size[1] > 0:
        return float(image.size[0])/float(image.size[1])
    return 1.


def _circle_boundary(topo, faces, spacing, aspect):
    cycles = _boundary_cycles(topo, faces)
    scale = np.array([aspect, 1.])
    points = [np.array([tuple(topo.vgroup_uv(g)) for g in cycle])*scale
              for cycle in cycles]
    areas = [_area(p) for p in points]
    outer_index = max(range(len(cycles)), key=lambda i: abs(areas[i]))
    outer, p = cycles[outer_index], points[outer_index]
    area = areas[outer_index]
    if abs(area) < 1e-14:
        raise ValueError('The island boundary has zero area')
    q = np.roll(p, -1, axis=0)
    cross = p[:, 0]*q[:, 1]-q[:, 0]*p[:, 1]
    center = np.sum((p+q)*cross[:, None], axis=0)/(6*area)
    if spacing == 'EVEN':
        lengths = np.ones(len(outer))
    elif spacing == 'GEOMETRY':
        xyz = core.vgroup_positions3d(topo)
        lengths = np.array([(xyz[a]-xyz[b]).length for a, b in
                            zip(outer, outer[1:]+outer[:1])])
    else:
        lengths = np.linalg.norm(q-p, axis=1)
    if not np.isfinite(lengths).all() or np.min(lengths) <= 1e-12:
        raise ValueError('The island boundary contains a zero-length edge')
    angles = np.r_[0., np.cumsum(lengths[:-1])]/lengths.sum()*math.tau
    angles *= 1 if area > 0 else -1
    unit = np.column_stack((np.cos(angles), np.sin(angles)))
    unit_area = abs(_area(unit))
    if unit_area < 1e-12:
        raise ValueError('The island boundary cannot form a safe circle')
    # Preserve the island's UV area, including unchanged inner hole boundaries.
    target_area = core.island_area(topo, faces)*aspect
    target_area += sum(abs(a) for i, a in enumerate(areas) if i != outer_index)
    radius = math.sqrt(target_area/unit_area)
    rel = p-center
    phase = math.atan2(float(np.sum(unit[:, 0]*rel[:, 1]-unit[:, 1]*rel[:, 0])),
                       float(np.sum(unit*rel)))
    c, s = math.cos(phase), math.sin(phase)
    unit = unit @ np.array([[c, s], [-s, c]])
    target = (center+radius*unit)/scale
    held = {g: tuple(uv) for g, uv in zip(outer, target)}
    for i, cycle in enumerate(cycles):
        if i != outer_index:
            held.update({g: tuple(topo.vgroup_uv(g)) for g in cycle})
    return held, outer, center/scale, radius


def selected_uv_cycles(topo, faces, selected_positions):
    """Selected simple closed UV edge graphs, including loops inside an island."""
    edges={tuple(sorted(topo.loop_edge_vgroups(p))) for p in selected_positions
           if 0<=p<len(topo.loops) and topo.loops[p].face.index in faces}
    if not edges:
        raise ValueError('Select a closed UV edge loop first')
    adjacent={}
    for a,b in edges:
        if a==b:raise ValueError('Selected UV edges must form complete non-branching loops')
        adjacent.setdefault(a,set()).add(b);adjacent.setdefault(b,set()).add(a)
    if any(len(neighbours)!=2 for neighbours in adjacent.values()):
        raise ValueError('Selected UV edges must form complete non-branching loops')
    remaining=set(adjacent);cycles=[]
    while remaining:
        start=min(remaining);previous=None;group=start;cycle=[]
        while group in remaining:
            remaining.remove(group);cycle.append(group)
            following=min(adjacent[group]-({previous} if previous is not None else set()))
            previous,group=group,following
        if group!=start or len(cycle)<3:
            raise ValueError('Selected UV edges must form complete non-branching loops')
        cycles.append(cycle)
    return cycles


def _selected_boundary_cycles(topo, faces, selected_positions):
    return selected_uv_cycles(topo,faces,selected_positions)


def _circle_cycle(topo, cycle, spacing, aspect):
    scale = np.array([aspect, 1.])
    points = np.array([tuple(topo.vgroup_uv(g)) for g in cycle])*scale
    area = _area(points)
    if abs(area) < 1e-14:
        raise ValueError('The selected boundary has zero area')
    q = np.roll(points, -1, axis=0)
    cross = points[:, 0]*q[:, 1]-q[:, 0]*points[:, 1]
    center = np.sum((points+q)*cross[:, None], axis=0)/(6*area)
    if spacing == 'EVEN':
        lengths = np.ones(len(cycle))
    elif spacing == 'GEOMETRY':
        xyz = core.vgroup_positions3d(topo)
        lengths = np.array([(xyz[a]-xyz[b]).length for a, b in
                            zip(cycle, cycle[1:]+cycle[:1])])
    else:
        lengths = np.linalg.norm(q-points, axis=1)
    if not np.isfinite(lengths).all() or np.min(lengths) <= 1e-12:
        raise ValueError('The island boundary contains a zero-length edge')
    angles = np.r_[0., np.cumsum(lengths[:-1])]/lengths.sum()*math.tau
    angles *= 1 if area > 0 else -1
    unit = np.column_stack((np.cos(angles), np.sin(angles)))
    unit_area = abs(_area(unit))
    if unit_area < 1e-12:
        raise ValueError('The island boundary cannot form a safe circle')
    radius = math.sqrt(abs(area)/unit_area)
    relative = points-center
    phase = math.atan2(float(np.sum(unit[:, 0]*relative[:, 1]-unit[:, 1]*relative[:, 0])),
                       float(np.sum(unit*relative)))
    c, s = math.cos(phase), math.sin(phase)
    unit = unit @ np.array([[c, s], [-s, c]])
    return {g: tuple(uv) for g, uv in
            zip(cycle, (center+radius*unit)/scale)}, center/scale, radius


def _selected_circle_boundary(topo, faces, selected_positions, spacing, aspect):
    selected_cycles = _selected_boundary_cycles(topo, faces, selected_positions)
    held = {}
    circles = []
    for cycle in selected_cycles:
        target, center, radius = _circle_cycle(topo, cycle, spacing, aspect)
        held.update(target)
        circles.append(dict(groups=cycle, center=center.tolist(), radius=radius))
    return held, dict(selected_boundaries=circles,shape_kind='CIRCULAR',
                      shape_cycles=selected_cycles,shape_options=dict(spacing=spacing))


def _harmonic(topo, faces, held, displacement=False):
    """Positive graph Laplacian, matrix-free preconditioned conjugate gradient."""
    triangles = core.triangles_for_faces(topo, faces)
    groups = sorted({g for tri in triangles for g in tri})
    adjacent = {g: set() for g in groups}
    for a, b, c in triangles:
        for u, v in ((a, b), (b, c), (c, a)):
            if u != v:
                adjacent[u].add(v)
                adjacent[v].add(u)
    free = [g for g in groups if g not in held]
    if not free:
        return dict(held)
    index = {g: i for i, g in enumerate(free)}
    diagonal = np.array([len(adjacent[g]) for g in free], dtype=float)
    if np.any(diagonal == 0):
        raise ValueError('The island contains disconnected UV vertices')
    rhs = np.zeros((len(free), 2))
    rows, cols = [], []
    for g, i in index.items():
        for neighbour in sorted(adjacent[g]):
            if neighbour in held:
                rhs[i] += held[neighbour]
            else:
                rows.append(i)
                cols.append(index[neighbour])
    rows, cols = np.asarray(rows, dtype=np.int64), np.asarray(cols, dtype=np.int64)
    def multiply(value):
        out = diagonal[:, None]*value
        np.add.at(out, rows, -value[cols])
        return out
    x = (np.zeros((len(free), 2)) if displacement else
         np.array([tuple(topo.vgroup_uv(g)) for g in free]))
    residual = rhs-multiply(x)
    z = residual/diagonal[:, None]
    direction = z.copy()
    product = np.sum(residual*z, axis=0)
    tolerance = 1e-10*max(1., float(np.max(np.abs(rhs))))
    for _ in range(min(2000, max(64, len(free)*2))):
        if np.max(np.abs(residual)) <= tolerance:
            break
        ad = multiply(direction)
        denominator = np.sum(direction*ad, axis=0)
        alpha = np.divide(product, denominator, out=np.zeros(2),
                          where=np.abs(denominator) > 1e-30)
        x += direction*alpha
        residual -= ad*alpha
        z = residual/diagonal[:, None]
        following = np.sum(residual*z, axis=0)
        beta = np.divide(following, product, out=np.zeros(2),
                         where=np.abs(product) > 1e-30)
        direction = z+direction*beta
        product = following
    if not np.isfinite(x).all() or np.max(np.abs(rhs-multiply(x))) > tolerance*10:
        raise ValueError('The interior solve did not converge')
    result = dict(held)
    result.update({g: tuple(uv) for g, uv in zip(free, x)})
    return result


def _triangle_signs(topo, faces):
    values = []
    for a, b, c in core.triangles_for_faces(topo, faces):
        p, q, r = (topo.vgroup_uv(g) for g in (a, b, c))
        values.append((q.x-p.x)*(r.y-p.y)-(q.y-p.y)*(r.x-p.x))
    return np.asarray(values)


def _reshape(context, obj, faces, boundary_builder, spacing='UV', iterations=150,
             respect_pins=True, prevent_overlap=False, template_only=False):
    from . import live_symmetry
    faces = live_symmetry.source_faces(obj, faces)
    bm = bmesh.from_edit_mesh(obj.data)
    uvl = bm.loops.layers.uv.active
    topo = core.UVTopology(bm, uvl)
    operation_snapshot = core._loop_uv_snapshot(topo, faces)
    islands = list(core.face_islands(topo, faces).values())
    result = dict(changed=0, faces=0, skipped=[], islands=[])
    shape_records = []
    aspect = _aspect(context)
    for island in islands:
        island = set(island)
        topo = core.UVTopology(bm, uvl)
        # A corner shared only at a mesh vertex may belong to another UV island.
        # Restrict numerical reads/writes to this island without joining seams.
        topo.vgroup_loops = {g: [p for p in positions
                                if topo.loops[p].face.index in island]
                             for g, positions in topo.vgroup_loops.items()}
        topo.vgroup_loops = {g: positions for g, positions in
                             topo.vgroup_loops.items() if positions}
        snapshot = core._loop_uv_snapshot(topo, island)
        try:
            if any(bm.faces[fi].hide for fi in island):
                raise ValueError('The island contains hidden faces')
            probe = safety.AcceptanceProbe(core, topo, island,
                                           check_other_islands=prevent_overlap)
            before = probe.measure(topo)
            signs = _triangle_signs(topo, island)
            area_before = core.island_area(topo, island)
            if area_before <= 1e-14 or not before['finite']:
                raise ValueError('The island boundary has zero area')
            held, details = boundary_builder(topo, island, spacing, aspect)
            progressive_metric = details.pop('progressive_metric', False)
            shape_cycles=details.pop('shape_cycles',[])
            shape_kind=details.pop('shape_kind',None)
            shape_options=details.pop('shape_options',{})
            from . import shape_constraints
            circular_cycles = []
            existing=shape_constraints.fixed(topo,island,exclude_cycles=shape_cycles,
                                             circular_cycles=circular_cycles)
            for g,uv in existing.items():
                if g in held and math.dist(held[g],uv)>1e-6:
                    raise ValueError('The new shape conflicts with an existing shape constraint')
                held[g]=uv
            if shape_kind == 'CIRCULAR':
                circular_cycles.extend((cycle, aspect) for cycle in shape_cycles)
            held = shape_constraints._concentric(topo, island, held, circular_cycles)
            if template_only:
                if not shape_kind or not shape_cycles:
                    raise ValueError('Select a complete closed loop to register a shape constraint')
                shape_records.extend(shape_constraints.capture_targets(
                    topo, island, shape_cycles, shape_kind, held, aspect,
                    shape_options))
                result['changed'] += 1
                result['faces'] += len(island)
                result['islands'].append(dict(faces=sorted(island), **details))
                continue
            if respect_pins:
                for g, positions in topo.vgroup_loops.items():
                    if any(topo.loops[p][uvl].pin_uv for p in positions):
                        original = tuple(topo.vgroup_uv(g))
                        if g in held and math.dist(held[g], original) > max(details.get('radius', 1.), 1.)*1e-6:
                            raise ValueError('Pinned boundary vertices conflict with the target shape')
                        held[g] = original
            positions = core.constrained_loops(bm, only_faces=island)
            runs = core.constraint_runs(bm, topo, positions)
            if progressive_metric:
                for triangle, sign in zip(core.triangles_for_faces(topo, island), signs):
                    if all(g in held for g in triangle):
                        a, b, c = (held[g] for g in triangle)
                        target_sign = ((b[0]-a[0])*(c[1]-a[1])-
                                       (b[1]-a[1])*(c[0]-a[0]))
                        if sign*target_sign <= 0:
                            raise ValueError('The target boundary would flip or collapse a triangle made only of fixed vertices')
            if progressive_metric:
                initial = {g: tuple(topo.vgroup_uv(g)) for g in held}
                # The graph and 3D frames stay unchanged throughout this solve.
                # Harmonic displacement is linear, so prepare the full field
                # once and take smaller steps only where geometry requires it.
                delta = {g: (uv[0]-initial[g][0], uv[1]-initial[g][1])
                         for g, uv in held.items()}
                field = _harmonic(topo, island, delta, displacement=True)
                geometry = metric._prepare_geometry(core, topo, island)
                groups = sorted(field)
                original = {g: tuple(topo.vgroup_uv(g)) for g in groups}
                # Most edits can reach the target in one solve. If that trial
                # cannot keep the input winding, restore it and use continuation.
                for method, stages in (('displacement', 1), ('harmonic', 1),
                                       ('displacement', 16), ('displacement', 64)):
                    per_stage = max(4, int(math.ceil(max(1, int(iterations))/stages)))
                    core.land_uv_groups(topo, original, island)
                    complete = True
                    for stage in range(1, stages+1):
                        factor = stage/stages
                        target = {g: (initial[g][0]+(uv[0]-initial[g][0])*factor,
                                      initial[g][1]+(uv[1]-initial[g][1])*factor)
                                  for g, uv in held.items()}
                        current = {g: tuple(topo.vgroup_uv(g)) for g in groups}
                        moved = (_harmonic(topo, island, target) if method=='harmonic' else
                                 {g: (uv[0]+field[g][0]/stages,
                                      uv[1]+field[g][1]/stages)
                                  for g, uv in current.items()})
                        moved.update(target)
                        core.land_uv_groups(topo, moved, island)
                        budget = max(1, int(iterations)) if stages==1 else per_stage
                        metric.solve(core, topo, island, target, runs, budget,
                                     angle_length=.5, auto=False, prevent_flips=True,
                                     prevent_overlap=prevent_overlap, _geometry=geometry)
                        current_signs = _triangle_signs(topo, island)
                        if (not np.isfinite(current_signs).all() or
                                np.any(signs*current_signs <= 0) or
                                any(math.dist(tuple(topo.vgroup_uv(g)), uv) > 1e-5
                                    for g, uv in target.items())):
                            complete = False
                            break
                    if complete:
                        break
                else:
                    raise ValueError('The target layout conflicts with pins or hole boundaries')
            else:
                layout = _harmonic(topo, island, held)
                core.land_uv_groups(topo, layout, island)
                initial_signs = _triangle_signs(topo, island)
                if np.any(signs*initial_signs <= 0):
                    raise ValueError('The target layout conflicts with pins or hole boundaries')
                metric.solve(core, topo, island, held, runs, max(1, int(iterations)),
                             auto=False, prevent_flips=True,
                             prevent_overlap=prevent_overlap)
            core.land_uv_groups(topo, held, island)
            if not core._restore_anchored_area(topo, island, held, runs, area_before):
                # A tightly anchored interior may forbid scaling every free
                # point. Try the free perimeter while retaining that interior.
                border = {g for p in core.selection_border_loops(topo, island)
                          for g in topo.loop_edge_vgroups(p)}
                stationary = {g: tuple(topo.vgroup_uv(g)) for g in topo.vgroup_loops
                              if g not in border}
                stationary.update(held)
                if not core._restore_anchored_area(topo, island, stationary, runs, area_before):
                    raise ValueError('The target shape could not preserve the island area')
            after = probe.measure(topo)
            after.update(probe.check_overlaps(topo))
            final_signs = _triangle_signs(topo, island)
            if (np.any(signs*final_signs <= 0) or
                    not after['finite'] or after['zero_edges'] or
                    after['max_deviation'] > .05 or
                    after['degenerate'] > before['degenerate'] or
                    after['novel_self_overlaps'] or
                    (prevent_overlap and after['novel_island_overlaps'])):
                raise ValueError('The target shape conflicts with constraints or would overlap')
            if abs(core.island_area(topo, island)/area_before-1.) > 1e-5:
                raise ValueError('The target shape could not preserve the island area')
            result['changed'] += 1
            result['faces'] += len(island)
            result['islands'].append(dict(faces=sorted(island), before=before,
                                         after=after, **details))
            if shape_kind:
                shape_records.extend(shape_constraints.capture(topo,island,shape_cycles,
                    shape_kind,aspect,shape_options))
        except Exception as error:
            for position, uv in snapshot.items():
                topo.loops[position][uvl].uv = uv
            if not isinstance(error, (ValueError, np.linalg.LinAlgError)):
                for position, uv in operation_snapshot.items():
                    topo.loops[position][uvl].uv = uv
                bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
                raise
            result['skipped'].append(str(error))
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    # Creating the persistent custom layer invalidates every wrapped corner.
    # Store only after all island solves and numeric captures have finished.
    geometry = None
    del topo, bm, uvl
    if shape_records:
        try:
            shape_constraints.store(obj,shape_records)
        except Exception:
            bm=bmesh.from_edit_mesh(obj.data);uvl=bm.loops.layers.uv.active
            loops=core.loops_in_order(bm)
            for p,uv in operation_snapshot.items():loops[p][uvl].uv=uv
            bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
            raise
    return result


def _circle_spec(topo, faces, spacing, aspect, selected_positions=None):
    if selected_positions is not None:
        held, details = _selected_circle_boundary(
            topo, faces, selected_positions, spacing, aspect)
        details.update(progressive_metric=True)
        return held, details
    held, outer, center, radius = _circle_boundary(topo, faces, spacing, aspect)
    return held, dict(outer=outer, center=center.tolist(), radius=radius,
                      progressive_metric=len(_boundary_cycles(topo, faces)) > 1,
                      shape_kind='CIRCULAR',shape_cycles=[outer],
                      shape_options=dict(spacing=spacing))


def circularize(context, obj, faces, spacing='EVEN', iterations=150,
                respect_pins=True, prevent_overlap=False,
                selected_boundary=True, selected_only=None,
                template_only=False):
    if selected_only is not None:selected_boundary=selected_only
    selected_positions = None
    if selected_boundary:
        bm = bmesh.from_edit_mesh(obj.data)
        selected_positions = core.selected_uv_edges(context, bm, mesh_fallback=False)
        del bm
    builder = (lambda topo, island, edge_spacing, aspect:
               _circle_spec(topo, island, edge_spacing, aspect, selected_positions))
    return _reshape(context, obj, faces, builder, spacing, iterations,
                    respect_pins, prevent_overlap, template_only)
