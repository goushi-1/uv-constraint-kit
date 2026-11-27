# SPDX-License-Identifier: GPL-3.0-or-later
"""Transactional UV quadrification with an independent integer-grid solver."""
import math
import time
import bmesh
import numpy as np
from . import circle, core, metric, safety, quad_grid
from .const import C_ALIGNED, C_HORIZONTAL, C_NONE, C_VERTICAL


def selected_faces_in_edit(context, bm, uvl):
    """Explicitly selected visible faces prior to flooding to whole islands."""
    sync = context.scene.tool_settings.use_uv_select_sync
    if sync:
        return {f.index for f in bm.faces if not f.hide and f.select}
    mode = context.scene.tool_settings.uv_select_mode
    if mode in {'FACE', 'ISLAND'}:
        return {f.index for f in bm.faces if not f.hide and f.select and f.uv_select}
    if mode == 'EDGE':
        return {f.index for f in bm.faces
                if not f.hide and f.select and all(lp.uv_select_edge for lp in f.loops)}
    return {f.index for f in bm.faces
            if not f.hide and f.select and all(lp.uv_select_vert for lp in f.loops)}


def _connected_quad_components(bm, uvl, face_ids):
    """Split a face subset into UV-edge-connected components."""
    wanted = set(face_ids)
    visited = set()
    components = []
    for start in sorted(wanted):
        if start in visited:
            continue
        comp = set()
        stack = [start]
        visited.add(start)
        while stack:
            fi = stack.pop()
            comp.add(fi)
            for loop in bm.faces[fi].loops:
                for linked in loop.edge.link_loops:
                    other = linked.face.index
                    if other in wanted and other not in visited:
                        if core.UVTopology._edge_uv_matches(loop, linked, uvl):
                            visited.add(other)
                            stack.append(other)
        components.append(comp)
    return components


def _output_pose(bm, topo, face_ids, output):
    """Area and centroid of a proposed {vgroup: (u, v)} mapping on `face_ids`."""
    area = 0.0
    cx = cy = 0.0
    for fi in sorted(face_ids):
        face = bm.faces[fi]
        points = [output[topo.loop_to_vgroup[topo.loop_pos[lp]]] for lp in face.loops]
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


def _build_partial_strip(topo, island, shape, average_shape, min_limit, strip_faces):
    """Solve pole-free quad strip(s) inside `island`, fitted to their own UV pose."""
    bm, uvl = topo.bm, topo.uvl
    components = _connected_quad_components(bm, uvl, strip_faces)
    held = {}
    for sub in components:
        sub_seed = _seed(bm, uvl, sub)
        if sub_seed is None:
            raise ValueError('Island has no visible quad face')
        report = quad_grid.solve(
            bm, uvl, sub, sub_seed, shape, average_shape, min_limit,
            require_complete=False, apply=False, topo=topo,
        )
        sub_output = report['output']
        orig_area, orig_center = _pose(bm, uvl, sub)
        curr_area, curr_center = _output_pose(bm, topo, sub, sub_output)
        scale = math.sqrt(orig_area / curr_area)
        for group, (x, y) in sub_output.items():
            target = (orig_center[0] + (x - curr_center[0]) * scale,
                      orig_center[1] + (y - curr_center[1]) * scale)
            if group in held and math.dist(held[group], target) > 1e-6:
                raise ValueError('Selected quad strips conflict at a shared UV vertex')
            held[group] = target
    return held, dict(progressive_metric=True, strip_faces=sorted(strip_faces),
                      strips=len(components))


def _solve_whole_island_quad_flow(context, obj, island, seed, shape, average_shape,
                                  respect_pins, prevent_overlap, orig_area, orig_center,
                                  before_quality):
    """Whole-island Quad-Flow DAG Quadrify for concave or poled quad islands.

    Uses temporary acyclic H/V quad-flow runs through the native metric solver
    without altering the mesh's stored constraint marks, preserving flips/overlaps,
    pins, existing constraints, and exact island area.
    """
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    uvl = core.uv_layer(bm)
    topo = core.UVTopology(bm, uvl)
    base_uv = np.asarray(core.mesh_reader.read_uv(topo.loops, uvl), dtype=np.float64)
    existing_pos = core.constrained_loops(bm, only_faces=island)
    existing_runs = core.constraint_runs(bm, topo, existing_pos)
    held = core._island_pins(topo, island) if respect_pins else {}
    # Preserve existing shape constraints (circles/rectangles) on this island.
    from . import shape_constraints
    for g, uv in shape_constraints.fixed(topo, island).items():
        held.setdefault(g, uv)

    # Prepare reference geometry once for this island; for EVEN shape, equalize
    # reference quad edges while keeping island orientation.
    geometry = metric._prepare_geometry(core, topo, island)
    if geometry is None:
        del topo, uvl, bm
        raise ValueError('Island has no valid triangles for quad-flow quadrification')
    if shape == 'EVEN':
        # Normalize each triangle's 2D reference frame to unit area / isotropic shape
        # so metric.solve naturally produces evenly spaced quads.
        loc = geometry['local'].copy()
        det = np.abs(loc[:, 0, 0] * loc[:, 1, 1] - loc[:, 0, 1] * loc[:, 1, 0])
        scale = np.sqrt(np.maximum(det, 1e-20))
        loc /= scale[:, None, None]
        geometry = dict(geometry, local=loc, weights=np.full_like(geometry['weights'], 1.0 / len(geometry['weights'])))
        geometry['inv_unit'] = np.linalg.inv(loc)

    def restore_base():
        for lp, uv in zip(topo.loops, base_uv.tolist()):
            lp[uvl].uv = uv

    last_reason = 'Quad-flow quadrification did not converge safely'
    for exclude_poles in (False, True):
        restore_base()
        try:
            q_runs, q_stats = quad_grid.quad_flow_runs(
                bm, uvl, island, seed, topo=topo,
                exclude_poles=exclude_poles, existing_runs=existing_runs,
            )
        except ValueError as exc:
            last_reason = str(exc)
            continue

        # Combine temporary quad-flow H/V runs with any existing aligned runs.
        runs = list(q_runs) + [(k, ps) for k, ps in existing_runs if k == C_ALIGNED]
        probe = safety.AcceptanceProbe(core, topo, island, check_other_islands=False, coordinates=base_uv)
        from . import native_engine
        use_contact = bool(not held and native_engine.supports_contact(len(geometry['tri'])))

        # Fast exact-subspace attempt first, then soft penalty ramp + exact subspace snap.
        best_uv = None
        best_dev = float('inf')
        best_p50 = float('inf')
        best_p90 = float('inf')

        def eval_candidate(active_runs=q_runs, require_existing_exact=True):
            coords = np.asarray(core.mesh_reader.read_uv(topo.loops, uvl), dtype=np.float64)
            if not np.all(np.isfinite(coords)):
                return None, float('inf'), float('inf'), float('inf'), float('inf')
            tris = probe._capture_triangles(topo, coordinates=coords)
            edges = tris[:, 1:] - tris[:, :1]
            cross = edges[:, 0, 0] * edges[:, 1, 1] - edges[:, 0, 1] * edges[:, 1, 0]
            ref_edges = probe._ref_array[:, 1:] - probe._ref_array[:, :1]
            ref_cross = ref_edges[:, 0, 0] * ref_edges[:, 1, 1] - ref_edges[:, 0, 1] * ref_edges[:, 1, 0]
            oriented = np.abs(ref_cross) > 2e-14
            if np.any(np.sign(ref_cross[oriented]) * cross[oriented] <= 2e-14):
                return None, float('inf'), float('inf'), float('inf'), float('inf')
            # Measure angle deviations on the active quad-flow H/V runs.
            devs = []
            for kind, pos_list in active_runs:
                for p in pos_list:
                    q = topo.loop_next[p]
                    du = float(coords[q, 0] - coords[p, 0])
                    dv = float(coords[q, 1] - coords[p, 1])
                    if math.hypot(du, dv) < 1e-10:
                        return None, float('inf'), float('inf'), float('inf'), float('inf')
                    ang = abs(math.degrees(math.atan2(dv, du)))
                    devs.append(min(ang, abs(180.0 - ang)) if kind == C_HORIZONTAL else abs(90.0 - ang))
            if not devs:
                return None, float('inf'), float('inf'), float('inf'), float('inf')
            devs.sort()
            worst = devs[-1]
            p50 = devs[len(devs) // 2]
            p90 = devs[int((len(devs) - 1) * 0.90)]
            ex_dev = 0.0
            if existing_pos:
                m_ex = probe.measure(topo, coordinates=probe._triangle_view(tris),
                                     loop_coordinates=coords, _triangles=tris)
                if not m_ex['finite'] or m_ex['zero_edges']:
                    return None, float('inf'), float('inf'), float('inf'), float('inf')
                ex_dev = float(m_ex['max_deviation'])
                if require_existing_exact and ex_dev > 0.05:
                    return None, float('inf'), float('inf'), float('inf'), ex_dev
            ov = probe.check_overlaps(topo, include_existing=not prevent_overlap,
                                      coordinates=probe._triangle_view(tris))
            if ov.get('self_overlaps', 0) or ov.get('novel_self_overlaps', 0):
                return None, float('inf'), float('inf'), float('inf'), ex_dev
            return coords, worst, p50, p90, ex_dev

        def fit_after_solve():
            if not held:
                core.restore_pose(topo, island, base_uv, allow_rotation=False, target_area=orig_area)
            else:
                core.land_uv_groups(topo, held, island)
                core._restore_anchored_area(topo, island, held, runs, orig_area)

        def write_uv_array(arr):
            for lp, uv in zip(topo.loops, arr.tolist()):
                lp[uvl].uv = uv

        def is_better(dev, p50, p90):
            if best_uv is None:
                return True
            if best_dev <= 0.05:
                return dev < best_dev
            if dev <= 0.05:
                return True
            return (p90, p50, dev) < (best_p90, best_p50, best_dev)

        # Stage 0: direct exact subspace solve
        n0, _ = metric.solve(
            core, topo, island, held, runs, 1, angle_length=1.0,
            use_density_map=False, auto=True, prevent_flips=True,
            prevent_overlap=True, _geometry=geometry,
        )
        if n0:
            fit_after_solve()
            cand, dev, p50, p90, ex_dev = eval_candidate()
            if cand is not None and dev <= 0.05:
                del topo, uvl, bm
                return q_stats

        # Stage 1..4: soft penalty continuation + hard-barrier fallback + exact snap
        restore_base()
        warm = base_uv
        warm_p90 = float('inf')
        warm_p50 = float('inf')
        warm_dev = float('inf')
        for strength in (1.0, 100.0, 1e4, 1e6):
            cand = None
            dev = p50 = p90 = ex_dev = float('inf')
            barriers = (True, False) if use_contact else (False,)
            for cb in barriers:
                write_uv_array(warm)
                n, _ = metric.solve(
                    core, topo, island, held, runs, 1, angle_length=1.0,
                    use_density_map=False, auto=True, prevent_flips=True,
                    prevent_overlap=True, constraint_strength=strength,
                    _geometry=geometry, _contact_barrier=cb,
                )
                if not n:
                    continue
                fit_after_solve()
                cand, dev, p50, p90, ex_dev = eval_candidate(require_existing_exact=False)
                if cand is not None:
                    break
            if cand is not None:
                if (p90, p50, dev) < (warm_p90, warm_p50, warm_dev):
                    warm = cand
                    warm_dev, warm_p50, warm_p90 = dev, p50, p90
                if ex_dev <= 0.05 and is_better(dev, p50, p90):
                    best_uv, best_dev, best_p50, best_p90 = cand, dev, p50, p90
                # Try polishing/snapping cleanly onto exact H/V subspace without
                # artificial contact-barrier repulsion once nearly straight.
                if dev < 5.0 or (existing_pos and ex_dev > 0.05 and p90 < 4.0):
                    n_ex, _ = metric.solve(
                        core, topo, island, held, runs, 1, angle_length=1.0,
                        use_density_map=False, auto=True, prevent_flips=True,
                        prevent_overlap=True, _geometry=geometry,
                    )
                    if n_ex:
                        fit_after_solve()
                        cand_ex, dev_ex, p50_ex, p90_ex, ex_dev_ex = eval_candidate(require_existing_exact=True)
                        if cand_ex is not None and (is_better(dev_ex, p50_ex, p90_ex) or dev_ex <= 0.05):
                            best_uv, best_dev, best_p50, best_p90 = cand_ex, dev_ex, p50_ex, p90_ex
                            warm = cand_ex
                            warm_dev, warm_p50, warm_p90 = dev_ex, p50_ex, p90_ex
                            if dev_ex <= 0.05:
                                break
                    write_uv_array(warm)
                if ex_dev <= 0.05 and dev <= 0.05:
                    break
            else:
                write_uv_array(warm)
                if not exclude_poles and strength == 1.0 and warm is base_uv:
                    # On heavily poled meshes where even gentle st=1.0 cannot stay
                    # overlap-free with pole constraints, move directly to exclude_poles=True.
                    break

        # On heavily poled organic meshes (exclude_poles=True), if >=90% of edges
        # straightened cleanly while a few pole-adjacent runs hit geometric walls,
        # prune only the blocked runs (>5 deg) while always keeping user constraints,
        # then snap the clean 90%+ grid cleanly onto the exact subspace.
        seed_uv = best_uv if best_uv is not None else (warm if warm is not base_uv else None)
        seed_p50 = best_p50 if best_uv is not None else warm_p50
        seed_p90 = best_p90 if best_uv is not None else warm_p90
        if (exclude_poles and seed_uv is not None and (best_uv is None or best_dev > 0.05)
                and seed_p50 <= 1.0 and seed_p90 <= 4.0):
            existing_set = set(existing_pos)
            pruned_q = []
            for kind, pos_list in q_runs:
                if existing_set and any(p in existing_set for p in pos_list):
                    pruned_q.append((kind, pos_list))
                    continue
                ok_run = True
                for p in pos_list:
                    q = topo.loop_next[p]
                    du = float(seed_uv[q, 0] - seed_uv[p, 0])
                    dv = float(seed_uv[q, 1] - seed_uv[p, 1])
                    ang = abs(math.degrees(math.atan2(dv, du)))
                    d_edge = min(ang, abs(180.0 - ang)) if kind == C_HORIZONTAL else abs(90.0 - ang)
                    if d_edge > 5.0:
                        ok_run = False
                        break
                if ok_run:
                    pruned_q.append((kind, pos_list))
            if pruned_q and len(pruned_q) >= int(len(q_runs) * 0.70):
                pruned_runs = list(pruned_q) + [(k, ps) for k, ps in existing_runs if k == C_ALIGNED]
                for st_pr in (None, 1e4, 1e6):
                    write_uv_array(seed_uv)
                    n_pr, _ = metric.solve(
                        core, topo, island, held, pruned_runs, 1, angle_length=1.0,
                        use_density_map=False, auto=True, prevent_flips=True,
                        prevent_overlap=True, constraint_strength=st_pr,
                        _geometry=geometry, _contact_barrier=False,
                    )
                    if not n_pr:
                        continue
                    fit_after_solve()
                    cand_pr, dev_pr, p50_pr, p90_pr, ex_pr = eval_candidate(
                        active_runs=pruned_q, require_existing_exact=True,
                    )
                    if cand_pr is not None and (dev_pr <= 0.05 or (p50_pr <= 0.5 and p90_pr <= 3.0)):
                        best_uv, best_dev, best_p50, best_p90 = cand_pr, dev_pr, p50_pr, p90_pr
                        q_stats['pruned_runs'] = len(q_runs) - len(pruned_q)
                        if dev_pr <= 0.05:
                            break
                    elif st_pr is not None:
                        cand_w, _dw, p50_w, p90_w, _ew = eval_candidate(
                            active_runs=pruned_q, require_existing_exact=False,
                        )
                        if cand_w is not None and (p90_w, p50_w) < (seed_p90, seed_p50):
                            seed_uv, seed_p50, seed_p90 = cand_w, p50_w, p90_w
        # If user constraints kept the combined solve slightly above 0.05 deg,
        # snap ONLY the user constraints onto the exact subspace using the quadrified
        # 2D grid as the reference metric so the island stays cleanly quadrified.
        if best_uv is None and existing_runs and seed_uv is not None and seed_p50 <= 1.0 and seed_p90 <= 4.0:
            seed_pts = seed_uv[geometry['representatives']][geometry['tri']].copy()
            if float(np.sum(geometry['reference_signed'])) < 0.0:
                seed_pts[:, :, 0] *= -1.0
            seed_local = (seed_pts[:, 1:] - seed_pts[:, :1]).transpose(0, 2, 1).copy()
            seed_det = np.linalg.det(seed_local)
            snap_geom = geometry
            if np.all(seed_det > 1e-15):
                seed_areas = seed_det * 0.5
                snap_geom = dict(
                    geometry,
                    local=seed_local,
                    inv_unit=np.linalg.inv(seed_local),
                    areas=seed_areas,
                    weights=seed_areas / seed_areas.sum(),
                )
            ex_warm = seed_uv
            for ex_st in (None, 100.0, 1e4, 1e6, None):
                for ex_cb in ((True, False) if (ex_st is not None and use_contact) else (False,)):
                    write_uv_array(ex_warm)
                    n_ex_only, _ = metric.solve(
                        core, topo, island, held, list(existing_runs), 1, angle_length=1.0,
                        use_density_map=False, auto=True, prevent_flips=True,
                        prevent_overlap=True, constraint_strength=ex_st,
                        _geometry=snap_geom, _contact_barrier=ex_cb,
                    )
                    if not n_ex_only:
                        continue
                    fit_after_solve()
                    cand_eo, dev_eo, p50_eo, p90_eo, ex_eo = eval_candidate(
                        active_runs=q_runs, require_existing_exact=False,
                    )
                    if cand_eo is not None:
                        ex_warm = cand_eo
                        if ex_eo <= 0.05 and p50_eo <= 2.0 and p90_eo <= 8.0:
                            best_uv, best_dev, best_p50, best_p90 = cand_eo, dev_eo, p50_eo, p90_eo
                            q_stats['snapped_existing_only'] = True
                        break
                if best_uv is not None:
                    break

        # Accept exact (<= 0.05 deg) or clean whole-island alignment on heavily poled meshes.
        acceptable = (
            best_uv is not None and (
                best_dev <= 0.05
                or (exclude_poles and (best_dev <= 2.0 or (best_p50 <= 2.0 and best_p90 <= 8.0)))
            )
        )
        if acceptable:
            write_uv_array(best_uv)
            q_stats['quad_flow_max_dev'] = best_dev
            q_stats['quad_flow_p50_dev'] = best_p50
            q_stats['quad_flow_p90_dev'] = best_p90
            del topo, uvl, bm
            return q_stats
        last_reason = f'Quad-flow convergence limited (dev={best_dev:.2f} deg, p50={best_p50:.2f} deg)'

    restore_base()
    del topo, uvl, bm
    raise ValueError(last_reason)


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


def _fast_island_quality(topo, island, _check_other_islands=False, probe=None):
    """Fast island quality & self-overlap summary via AcceptanceProbe (native C++ when loaded)."""
    p = probe or safety.AcceptanceProbe(core, topo, island, check_other_islands=False)
    m = p.measure(topo)
    ov = p.check_overlaps(topo, include_existing=True)
    self_ov = int(ov.get('self_overlaps', 0))
    return dict(
        flips=int(m['flips']),
        overlaps=self_ov,
        self_overlaps=self_ov,
        island_overlaps=0,
        degenerate=int(m['degenerate']),
        zero_edges=int(m['zero_edges']),
        max_deviation=float(m['max_deviation']),
        finite=bool(m['finite']),
    )


def _island(context, obj, island, shape, average_shape, skip_non_quads,
            respect_pins, prevent_overlap, preserve_uv_connections,
            selected_faces=None):
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
    island_probe = safety.AcceptanceProbe(core, topo, island, check_other_islands=False)
    before_quality = _fast_island_quality(topo, island, False, probe=island_probe)
    connections = _internal_uv_connections(bm, uvl, island)
    pin_groups = [(positions, state['uv'][next(p for p in positions if state['pins'][p])])
                  for positions in topo.vgroup_loops.values()
                  if any(state['pins'][p] and topo.loops[p].face.index in island for p in positions)]
    started = time.perf_counter()
    algorithm = 'INTEGER_GRID'
    strip_faces = []
    try:
        image = getattr(getattr(context, 'space_data', None), 'image', None)
        min_limit = 1.0/image.size[0] if image and image.size[0] else 0.002
        try:
            quad_grid.solve(bm, uvl, island, seed, shape, average_shape, min_limit, topo=topo)
            _fit_pose(bm, uvl, island, area, center)
        except ValueError as grid_error:
            strip = {fi for fi in (selected_faces or ())
                     if fi in island and not bm.faces[fi].hide}
            prefer_strip = bool(non_quads and strip and strip < island and
                                all(len(bm.faces[fi].loops) == 4 for fi in strip))
            del topo, uvl, bm
            if prefer_strip:
                builder = (lambda t_topo, t_island, _spacing, _aspect:
                           _build_partial_strip(t_topo, t_island, shape, average_shape,
                                                min_limit, strip))
                reshape = circle._reshape(
                    context, obj, island, builder,
                    spacing='UV', iterations=150,
                    respect_pins=respect_pins,
                    prevent_overlap=prevent_overlap,
                    template_only=False,
                )
                bm = bmesh.from_edit_mesh(obj.data)
                core.prepare_bm(bm)
                uvl = core.uv_layer(bm)
                topo = core.UVTopology(bm, uvl)
                if not reshape.get('changed'):
                    reason = (reshape['skipped'][0] if reshape.get('skipped')
                              else 'Partial quad strip relaxation failed')
                    raise ValueError(reason) from None
                algorithm = 'PARTIAL_QUAD_STRIP'
                strip_faces = sorted(strip)
            else:
                try:
                    _solve_whole_island_quad_flow(
                        context, obj, island, seed, shape, average_shape,
                        respect_pins, prevent_overlap, area, center, before_quality,
                    )
                    bm = bmesh.from_edit_mesh(obj.data)
                    core.prepare_bm(bm)
                    uvl = core.uv_layer(bm)
                    topo = core.UVTopology(bm, uvl)
                    algorithm = 'QUAD_FLOW'
                except ValueError:
                    if strip and strip < island and all(len(bmesh.from_edit_mesh(obj.data).faces[fi].loops) == 4 for fi in strip):
                        builder = (lambda t_topo, t_island, _spacing, _aspect:
                                   _build_partial_strip(t_topo, t_island, shape, average_shape,
                                                        min_limit, strip))
                        reshape = circle._reshape(
                            context, obj, island, builder,
                            spacing='UV', iterations=150,
                            respect_pins=respect_pins,
                            prevent_overlap=prevent_overlap,
                            template_only=False,
                        )
                        bm = bmesh.from_edit_mesh(obj.data)
                        core.prepare_bm(bm)
                        uvl = core.uv_layer(bm)
                        topo = core.UVTopology(bm, uvl)
                        if not reshape.get('changed'):
                            raise grid_error from None
                        algorithm = 'PARTIAL_QUAD_STRIP'
                        strip_faces = sorted(strip)
                    else:
                        raise

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
        after_topo = core.UVTopology(bm, uvl)
        quality = _fast_island_quality(after_topo, island, False)
        novel_ov = (island_probe.check_overlaps(after_topo, include_existing=False)
                    if prevent_overlap else {})
        if (not quality['finite'] or quality['degenerate'] > before_quality['degenerate'] or
                quality['flips'] > before_quality['flips'] or quality['zero_edges'] or
                (prevent_overlap and (not novel_ov.get('overlap_check_complete', True)
                                      or novel_ov.get('novel_self_overlaps', 0)))):
            raise ValueError('Quadrification failed the UV safety check')
        if quality['max_deviation'] > 0.05:
            raise ValueError('Quadrification violates a marked constraint')
        after_area, after_center = _pose(bm, uvl, island)
        area_error = abs(after_area/area-1.0)
        center_error = math.dist(after_center, center)
        if area_error > 1e-5 or (algorithm == 'INTEGER_GRID' and center_error > 1e-7):
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
                   algorithm=algorithm, shape=shape, average_shape=average_shape,
                   before_quality=before_quality, quality=quality,
                   area_error=area_error, centroid_error=center_error,
                   new_uv_boundaries=len(cuts), new_uv_boundary_edges=_connection_rows(cuts),
                   uv_connections_before=len(connections), uv_connections_after=len(current_connections),
                   elapsed_seconds=time.perf_counter()-started)
        if strip_faces:
            row['strip_faces'] = strip_faces
    except Exception as error:
        reason = str(error) or type(error).__name__
        error.__traceback__ = error.__context__ = error.__cause__ = None
        bm = bmesh.from_edit_mesh(obj.data)
        core.prepare_bm(bm)
        uvl = core.uv_layer(bm)
        _restore_uv(bm, uvl, state['uv'])
        row = dict(faces=sorted(island), skipped=True, reason=reason,
                   chosen_seed=seed, elapsed_seconds=time.perf_counter()-started)
    _restore_marks(bm, uvl, state)
    topo = None
    del topo, uvl, bm
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    # The update may flush selection; restore exact marks without changing UVs.
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    _restore_marks(bm, core.uv_layer(bm), state)
    return row


def quadrify(context, obj, faces, mode=None, respect_pins=True,
             prevent_overlap=False, preserve_uv_connections=False,
             shape='PER_FACE', average_shape=True, skip_non_quads=False,
             selected_faces=None):
    """Integer-grid quadrification for regular quad islands, with partial
    pole-free quad strip fallback + safe island relaxation on mixed/pole islands.

    Preserve the original island area and all marks/selection flags.
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
    explicit = {int(i) for i in (faces if selected_faces is None else selected_faces)}
    requested = core.islands_touching_faces(topo, {int(i) for i in faces})
    from . import live_symmetry
    requested = live_symmetry.source_faces(obj, requested)
    groups = [set(group) for group in core.face_islands(topo, requested).values()]
    del topo, uvl, bm
    for island in groups:
        row = _island(context, obj, island, shape, average_shape, skip_non_quads,
                      respect_pins, prevent_overlap, preserve_uv_connections,
                      selected_faces=explicit & island)
        result['islands'].append(row)
        if row.get('skipped'):
            result['skipped'].append(row['reason'])
        else:
            result['changed'] += bool(row['changed'])
            result['faces'] += len(island) if row['changed'] else 0
            result['new_uv_boundaries'] += row['new_uv_boundaries']
            result['new_uv_boundary_edges'].extend(row['new_uv_boundary_edges'])
    return result
