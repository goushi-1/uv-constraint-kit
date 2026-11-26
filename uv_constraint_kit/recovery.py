# SPDX-License-Identifier: GPL-3.0-or-later
"""Feasible initialization for folded input UVs, before metric optimization.

Uniformly mirrored charts and intentional complete-face stacks remain intact.
Numeric local repairs precede global parameterizers; no RizomUV is required.
"""
import bmesh
import bpy
from copy import deepcopy
import numpy as np

from . import safety


def seed_key(core, topo, faces, positions, fixed, saved, pins, settings):
    """Exact numeric inputs for one recursive guard escalation, never a cache."""
    layer = core.constraint_layer(topo.bm)
    options = (('mix', 1.), ('keep_metric', False), ('fill_holes', False),
               ('auto_iterations', True), ('optimize_iterations', 1),
               ('angle_distance_mix', 1.), ('use_density_map', True))
    saved_key = tuple(map(tuple, saved.tolist())) if isinstance(saved, np.ndarray) else tuple(saved)
    return dict(uv=saved_key, pins=tuple(pins), faces=frozenset(faces),
        marks=tuple((p, int(topo.loops[p][layer])) for p in sorted(positions)),
        fixed=frozenset(fixed), vertices=tuple(topo._loop_vertices.tolist()),
        following=tuple(topo.loop_next), groups=tuple(topo.loop_to_vgroup),
        islands=tuple(topo.face_island), xyz=tuple(tuple(v.co) for v in topo.bm.verts),
        seams=tuple(e.seam for e in topo.bm.edges), hidden=tuple(f.hide for f in topo.bm.faces),
        options=tuple((name, settings.get(name, default)) for name, default in options))


def remember(key, seed, report):
    """Transfer only a previously checked seed to this operation's strict retry."""
    if key is None or seed is None or not report or report['failed']:
        return None
    return dict(key=key, seed=tuple(tuple(xy) for xy in seed), report=deepcopy(report))


def recall(core, topo, faces, positions, fixed, saved, pins, settings, state):
    """Reject stale inputs and independently recheck the numeric embedding."""
    if state is None or state['key'] != seed_key(core, topo, faces, positions, fixed, saved, pins, settings):
        return None
    seed = np.asarray(state['seed'], dtype=np.float64)
    if seed.shape != (len(saved), 2) or not np.isfinite(seed).all() or state['report']['failed']:
        return None
    target = {fi for chart in state['report']['repaired'] for fi in chart['faces']}
    held = [p for p, fi in enumerate(topo.loop_faces) if fi not in target or p in fixed]
    if not np.array_equal(seed[held], np.asarray(saved)[held]):
        return None
    for chart in state['report']['repaired']:
        probe = safety.AcceptanceProbe(core, topo, set(chart['faces']), coordinates=seed)
        triangles = probe._ref_array
        edges = triangles[:, 1:]-triangles[:, :1]
        cross = edges[:, 0, 0]*edges[:, 1, 1]-edges[:, 0, 1]*edges[:, 1, 0]
        zero_edges = any(np.linalg.norm(seed[p]-seed[topo.next_position(p)]) < 1e-10
                         for p in core.constrained_loops(topo.bm, set(chart['faces'])))
        if (np.any(cross*chart['sign'] <= 2e-14) or zero_edges or
                probe.check_overlaps(topo, include_existing=True, coordinates=triangles)['self_overlaps']):
            return None
    return dict(seed=state['seed'], report=deepcopy(state['report']))


def _crosses(topo, faces, triangles=None, *, coordinates=None, _positions=None):
    if _positions is None:
        positions = [tuple(topo.loop_pos[l] for l in tri)
                     for tri in (topo.loop_triangles() if triangles is None else triangles)
                     if tri[0].face.index in faces]
    else:
        positions = _positions
    if not positions:
        return positions, np.empty(0, dtype=np.float64)
    if coordinates is None:
        from . import mesh_reader
        all_uv = np.asarray(mesh_reader.read_uv(topo.loops, topo.uvl), dtype=np.float64)
        uv = all_uv[np.asarray(positions, dtype=np.intp)]
    elif isinstance(coordinates, np.ndarray):
        uv = coordinates[np.asarray(positions, dtype=np.intp)]
    else:
        uv = np.asarray(coordinates, dtype=np.float64)[np.asarray(positions, dtype=np.intp)]
    edges = uv[:, 1:]-uv[:, :1]
    return positions, edges[:, 0, 0]*edges[:, 1, 1]-edges[:, 0, 1]*edges[:, 1, 0]


def plan(core, topo, faces, settings, *, _coordinates=None, _initial_probe=None):
    """Find damaged charts; private probe/source pair belongs to unchanged input.

    Only the synchronous checked caller supplies this pair, before any UV or
    topology write. Identity guards do not authorize reuse after mutation.
    """
    result = dict(charts=[], skipped=[], clean_faces=[])
    if settings.get('mix', 1.) != 1.:
        return result
    islands = core.face_islands(topo, faces)
    roots = {fi: root for root, island in islands.items() for fi in island}
    use_rows = hasattr(topo, 'triangle_rows')
    triangles = {root: [] for root in islands}
    if use_rows:
        for fi, pos in topo.triangle_rows():
            root = roots.get(fi)
            if root is not None:
                triangles[root].append(pos)
    else:
        for tri in topo.loop_triangles():
            root = roots.get(tri[0].face.index)
            if root is not None:
                triangles[root].append(tri)
    for root, island in islands.items():
        island = set(island)
        if use_rows:
            positions, cross = _crosses(topo, island, None, coordinates=_coordinates,
                                        _positions=triangles[root])
        else:
            positions, cross = _crosses(topo, island, triangles[root], coordinates=_coordinates)
        if not len(cross) or not np.all(np.isfinite(cross)):
            continue
        mixed = np.any(cross > 2e-14) and np.any(cross < -2e-14)
        degenerate = np.any(np.abs(cross) <= 2e-14)
        if not mixed and not degenerate:
            if len(island) == 1:
                continue
            probe = None
            if (_coordinates is not None and isinstance(_initial_probe, tuple)
                    and len(_initial_probe) == 2 and _initial_probe[1] is _coordinates
                    and len(islands) == 1 and len(island) == len(topo.island_faces[root])):
                shared = _initial_probe[0]
                if (shared is not None and shared.core is core
                        and shared.roots is topo.face_island and shared.faces == island
                        and not shared.check_other_islands and not shared.static):
                    probe = shared
            if probe is None:
                probe = safety.AcceptanceProbe(
                    core, topo, island,
                    triangles=None if use_rows else triangles[root],
                    coordinates=_coordinates)
            # No UV writes occur between this probe's construction and the
            # existing-overlap check; reuse its exact current input values.
            overlap = probe.check_overlaps(topo, include_existing=True,
                                           coordinates=probe._ref_array)
            if not overlap['self_overlaps']:
                if overlap['overlap_check_complete']:
                    result['clean_faces'].extend(sorted(island))
                continue
        # Mixed winding on an inconsistently oriented 3D mesh is ambiguous.
        consistent = True
        for fi in island:
            for loop in topo.bm.faces[fi].loops:
                if len(loop.edge.link_loops) > 2:
                    consistent = False
                    break
                for other in loop.edge.link_loops:
                    if other.face.index not in island or other == loop:
                        continue
                    if (loop.vert != other.link_loop_next.vert or
                            loop.link_loop_next.vert != other.vert):
                        consistent = False
        area = abs(float(cross.sum()))*.5
        if not consistent or area <= 1e-14:
            result['skipped'].append(dict(faces=sorted(island), reason='ambiguous_winding'))
            continue
        result['charts'].append(dict(faces=sorted(island), sign=1 if cross.sum() > 0 else -1,
                                     area=core.island_area(topo, island), boundary_area=area,
                                     positions=positions, has_folds=bool(mixed or degenerate)))
    if result['charts']:
        shared = core.stacked_uv_groups(topo, set(range(len(topo.bm.faces))),
                                       coordinates=topo.initial_uv)
        protected = set(shared) | set(shared.values())
        charts = []
        for chart in result['charts']:
            members = {topo.loop_to_vgroup[p] for tri in chart['positions'] for p in tri}
            if members & protected:
                result['skipped'].append(dict(faces=chart['faces'], reason='intentional_stack'))
            else:
                charts.append(chart)
        result['charts'] = charts
    return result


def _chart_state(core, topo, chart, fixed_positions):
    """Plain numeric recovery inputs; never retain a BMesh wrapper here."""
    island = set(chart['faces'])
    mask = np.isin(topo.loop_faces, chart['faces'])
    target = np.flatnonzero(mask).tolist()
    outside = np.flatnonzero(~mask).tolist()
    fixed_groups = {topo.loop_to_vgroup[p] for p in fixed_positions}
    island_groups = {topo.loop_to_vgroup[p] for p in target}
    held = {g: tuple(topo.vgroup_uv(g)) for g in topo.vgroup_loops
            if g in fixed_groups and g in island_groups}
    positions = core.constrained_loops(topo.bm, only_faces=island)
    runs = core.constraint_runs(topo.bm, topo, positions)
    boundary_groups = {g for p in core.selection_border_loops(topo, island)
                       for g in topo.loop_edge_vgroups(p)}
    locked = bool(boundary_groups) and boundary_groups <= set(held)
    return dict(connectivity=topo.loop_to_vgroup.copy(), islands=topo.face_island.copy(), runs=runs,
                target=target, outside=outside, held=held,
                native_fixed=[p for p in target if topo.loop_to_vgroup[p] in held],
                target_area=chart['boundary_area'] if locked else chart['area'],
                boundary_locked=locked)


def prepare(core, topo, plan, fixed_positions):
    """Reuse the caller's topology before it releases all native handles.

    The first positive self-overlapping chart starts with native unwrap, which
    needs only these inputs. Later charts capture their current state afresh.
    """
    charts = plan['charts']
    if not charts or charts[0].get('has_folds', True):
        return {}
    chart = charts[0]
    return {tuple(chart['faces']): _chart_state(core, topo, chart, fixed_positions)}


def seed(core, context, obj, plan, saved, pins, fixed_positions, settings, *, _prepared=None, _topo_box=None):
    """Try bounded local and global initializers; leave only fully valid seeds."""
    if context.edit_object != obj or len(context.objects_in_mode_unique_data) != 1:
        raise RuntimeError('Recovery requires a single edit mesh')
    def topology():
        bm = bmesh.from_edit_mesh(obj.data)
        return core.UVTopology(bm, core.uv_layer(bm))

    def land(value, positions=None):
        bm = bmesh.from_edit_mesh(obj.data)
        core.prepare_bm(bm)
        uvl = core.uv_layer(bm)
        loops = core.loops_in_order(bm)
        vals = value.tolist() if isinstance(value, np.ndarray) else value
        if positions is None:
            for lp, uv in zip(loops, vals):
                lp[uvl].uv = uv
        else:
            for p in positions:
                loops[p][uvl].uv = vals[p]
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)

    report = dict(repaired=[], failed=[], skipped=plan['skipped'])
    ts = context.scene.tool_settings
    ui_saved = None
    best = np.asarray(saved, dtype=float).copy()
    numeric_topology = _topo_box.pop() if _topo_box else None
    last_topo = numeric_topology
    dirty = False
    try:
        for chart in plan['charts']:
            island = set(chart['faces'])
            state = (_prepared or {}).get(tuple(chart['faces']))
            if state is None:
                if numeric_topology is None:
                    numeric_topology = topology()
                    last_topo = numeric_topology
                state = _chart_state(core, numeric_topology, chart, fixed_positions)
            connectivity, islands = state['connectivity'], state['islands']
            runs, target, outside = state['runs'], state['target'], state['outside']
            held = state['held']
            boundary_locked, target_area = state['boundary_locked'], state['target_area']
            # Direction constraints leave the along-axis coordinate free. Only
            # actual pins and locked borders fix both coordinates of a point.
            native_fixed = state['native_fixed']
            target_set, native_fixed_set = set(target), set(native_fixed)
            chart_tri_idx = (
                np.asarray([pos for fi, pos in numeric_topology.triangle_rows() if fi in island], dtype=np.intp).reshape(-1, 3)
                if numeric_topology is not None else None
            )
            # Numeric attempts preserve mesh topology. Keep this wrapper only
            # until native unwrap; failed array-only repairs need no mesh reset.
            failure = 'no_feasible_initialization'
            accepted = False
            dirty = False
            sparse_dirty = True
            # An already positive map with self-contact needs a globally
            # feasible embedding. On dense charts (>= 4096 faces), try the fast
            # direct CONFORMAL + local contact repair ahead of slow iterative
            # MINIMUM_STRETCH; smaller charts keep MINIMUM_STRETCH first.
            if chart.get('has_folds', True):
                methods = ('LOCAL', 'METRIC', 'CONFORMAL', 'MINIMUM_STRETCH', 'ANGLE_BASED')
            elif len(island) >= 4096:
                methods = ('CONFORMAL', 'MINIMUM_STRETCH', 'LOCAL', 'METRIC', 'ANGLE_BASED')
            else:
                methods = ('MINIMUM_STRETCH', 'LOCAL', 'METRIC', 'CONFORMAL', 'ANGLE_BASED')
            local_folds_ok = False
            for method in methods:
                if method == 'METRIC' and local_folds_ok and len(island) >= 4096:
                    # On dense charts where LOCAL already confirmed positive
                    # orientation and failed only on >8 self-overlaps, an
                    # unguarded METRIC pass cannot untangle self-overlaps.
                    continue
                if dirty:
                    land(best, target if sparse_dirty else None)
                    dirty = False
                    if numeric_topology is not None:
                        numeric_topology.refresh_uv()
                try:
                    if method in ('LOCAL', 'METRIC') and numeric_topology is None:
                        numeric_topology = topology()
                        last_topo = numeric_topology
                    if method in ('LOCAL', 'METRIC') and chart_tri_idx is None:
                        chart_tri_idx = np.asarray(
                            [pos for fi, pos in numeric_topology.triangle_rows() if fi in island],
                            dtype=np.intp,
                        ).reshape(-1, 3)
                    if method == 'LOCAL':
                        from . import untangle, native_engine
                        topo = numeric_topology
                        diag = {}
                        raw = untangle.feasible_seed(topo, chart, held, native_engine, _diag=diag)
                        local_folds_ok = bool(diag.get('folds_ok', False))
                        topo = None
                        status = {'FINISHED'} if raw is not None else {'CANCELLED'}
                    elif method == 'METRIC':
                        # Local folds can often be repaired without rebuilding
                        # the chart. Axis runs keep their free coordinates and
                        # optimize jointly with interior points. This trial is
                        # published only after the strict seed checks below.
                        from . import metric
                        topo = numeric_topology
                        budget = 50 if settings.get('auto_iterations', True) else max(1, int(settings.get('optimize_iterations', 1)))
                        dirty = True
                        sparse_dirty = True
                        count, _ = metric.solve(core, topo, island, held, runs, budget,
                            angle_length=settings.get('angle_distance_mix', 1.),
                            use_density_map=settings.get('use_density_map', True),
                            auto=False, _reference_sign=chart['sign'])
                        raw = np.array(core.mesh_reader.read_uv(topo.loops, topo.uvl), dtype=float, copy=True)
                        topo = None
                        status = {'FINISHED'} if count else {'CANCELLED'}
                    else:
                        # Native unwrap can replace the edit mesh. No cached
                        # topology or BMesh wrapper may survive this boundary.
                        numeric_topology = topo = last_topo = None
                        bm = bmesh.from_edit_mesh(obj.data)
                        core.prepare_bm(bm)
                        if ui_saved is None:
                            selected = core._capture_selection(bm)
                            uv_faces = [f.uv_select for f in bm.faces]
                            selection_valid = bm.uv_select_sync_valid
                            tools = {p: getattr(ts, p) for p in
                                     ('use_uv_select_sync', 'uv_select_mode', 'use_edge_path_live_unwrap')}
                            tools['mesh_select_mode'] = tuple(ts.mesh_select_mode)
                            ui_saved = (selected, uv_faces, selection_valid, tools)
                        dirty = True
                        sparse_dirty = False
                        uvl = core.uv_layer(bm)
                        loops = core.loops_in_order(bm)
                        ts.use_edge_path_live_unwrap = False
                        ts.use_uv_select_sync = False
                        ts.uv_select_mode = 'VERTEX'
                        ts.mesh_select_mode = (False, False, True)
                        core.select_faces_only(bm, island)
                        all_target = len(target) == len(loops)
                        if all_target:
                            for face in bm.faces:
                                face.uv_select = True
                        else:
                            for face in bm.faces:
                                face.uv_select = face.index in island
                        flip_x = chart['sign'] < 0
                        if all_target and not native_fixed_set and not flip_x and not any(pins):
                            for loop in loops:
                                loop.uv_select_vert = loop.uv_select_edge = True
                                loop[uvl].pin_uv = False
                        else:
                            for p, loop in enumerate(loops):
                                member = all_target or (p in target_set)
                                fixed = p in native_fixed_set
                                loop.uv_select_vert = loop.uv_select_edge = member and not fixed
                                loop[uvl].pin_uv = pins[p] or fixed
                                if member and flip_x:
                                    loop[uvl].uv.x *= -1
                        bm.uv_select_sync_valid = True
                        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
                        del bm, uvl, loops, loop, face
                        dirty = True
                        status = bpy.ops.uv.unwrap(method=method, fill_holes=settings.get('fill_holes', False),
                            use_original_bounds=False, correct_aspect=False, no_flip=True, iterations=50,
                            use_subsurf_data=False, use_weights=False)
                        bm = bmesh.from_edit_mesh(obj.data)
                        uvl = core.uv_layer(bm)
                        raw = np.array(core.mesh_reader.read_uv(core.loops_in_order(bm), uvl), dtype=float, copy=True)
                        del bm, uvl
                        if chart['sign'] < 0:
                            raw[target, 0] *= -1
                    if ('FINISHED' not in status or not np.all(np.isfinite(raw)) or
                            not np.array_equal(raw[outside], best[outside])):
                        failure = 'native_integrity'
                        continue
                    tolerance = max(float(np.abs(best).max()), 1.)*2e-7
                    if method != 'METRIC' and native_fixed and np.abs(raw[native_fixed]-best[native_fixed]).max() > tolerance:
                        failure = 'native_fixed_point_moved'
                        continue
                    if method != 'METRIC':
                        raw[native_fixed] = best[native_fixed]
                    if method != 'CONFORMAL' and chart_tri_idx is not None and len(chart_tri_idx):
                        raw_tris = raw[chart_tri_idx]
                        raw_edges = raw_tris[:, 1:] - raw_tris[:, :1]
                        raw_cross = raw_edges[:, 0, 0]*raw_edges[:, 1, 1] - raw_edges[:, 0, 1]*raw_edges[:, 1, 0]
                        if np.any(raw_cross*chart['sign'] <= 0):
                            failure = 'seed_validation'
                            continue
                    dirty = True
                    sparse_dirty = True
                    if method == 'METRIC':
                        topo = numeric_topology
                        topo.refresh_uv()
                        last_topo = topo
                    else:
                        land(raw, target)
                        if method == 'LOCAL':
                            topo = numeric_topology
                            topo.refresh_uv()
                        else:
                            topo = topology()
                        last_topo = topo
                    if topo.loop_to_vgroup != connectivity or topo.face_island != islands:
                        failure = 'uv_connectivity_changed'
                        continue
                    if method == 'CONFORMAL':
                        # A fast global seed can leave a few local folds or
                        # contacts. Repair its numeric embedding before paying
                        # for the more expensive global stretch initializer.
                        from . import untangle, native_engine
                        repaired = untangle.feasible_seed(topo, chart, held, native_engine)
                        if repaired is not None:
                            if not np.array_equal(repaired[outside], best[outside]):
                                failure = 'native_integrity'
                                continue
                            land(repaired, target)
                            topo.refresh_uv()
                            if topo.loop_to_vgroup != connectivity or topo.face_island != islands:
                                failure = 'uv_connectivity_changed'
                                continue
                    if held:
                        core.land_uv_groups(topo, held, island)
                        if (not settings.get('keep_metric', False) and
                                not core._restore_anchored_area(topo, island, held, runs, target_area)):
                            failure = 'fixed_area_conflict'
                            continue
                    else:
                        pose_area = (core.mesh_area3d(topo, island) if settings.get('keep_metric', False)
                                     else target_area)
                        core.restore_pose(topo, island, best,
                                          allow_rotation=(method not in ('LOCAL', 'METRIC')), target_area=pose_area)
                    # All checks below read the same post-pose coordinates.
                    value = np.array(core.mesh_reader.read_uv(topo.loops, topo.uvl), dtype=float, copy=True)
                    probe = safety.AcceptanceProbe(core, topo, island, coordinates=value)
                    triangles = probe._ref_array
                    edges = triangles[:, 1:] - triangles[:, :1]
                    cross = edges[:, 0, 0]*edges[:, 1, 1] - edges[:, 0, 1]*edges[:, 1, 0]
                    if (np.any(cross*chart['sign'] <= 2e-14) or
                            not np.array_equal(value[outside], best[outside]) or
                            not np.array_equal(value[list(fixed_positions)], np.asarray(saved)[list(fixed_positions)])):
                        failure = 'seed_validation'
                        continue
                    health = probe.measure(topo, coordinates=probe._triangle_view(triangles), _triangles=triangles)
                    if not health['finite'] or health['zero_edges']:
                        failure = 'seed_validation'
                        continue
                    overlap = probe.check_overlaps(topo, include_existing=True,
                                                   coordinates=triangles)
                    if overlap['self_overlaps']:
                        failure = 'seed_validation'
                        continue
                    best = value.copy()
                    dirty = False
                    report['repaired'].append(dict(faces=chart['faces'], sign=chart['sign'],
                        area=target_area, area_policy='fixed_outline' if boundary_locked else 'preserve_area',
                        method=method))
                    accepted = True
                    break
                except Exception as exc:
                    failure = '%s: %s' % (type(exc).__name__, exc)
                finally:
                    # No native BMesh wrapper is retained between attempts.
                    topo = None
            if not accepted:
                report['failed'].append(dict(faces=chart['faces'], reason=failure))
                numeric_topology = topo = last_topo = None
            if dirty:
                land(best, target if sparse_dirty else None)
                dirty = False
    finally:
        numeric_topology = topo = None
        if dirty:
            land(best)
            last_topo = None
        if ui_saved is not None:
            selected, uv_faces, selection_valid, tools = ui_saved
            bm = bmesh.from_edit_mesh(obj.data)
            core.prepare_bm(bm)
            uvl = core.uv_layer(bm)
            loops = core.loops_in_order(bm)
            for loop, pin in zip(loops, pins):
                loop[uvl].pin_uv = pin
            for prop, value in tools.items():
                setattr(ts, prop, value)
            core._apply_selection(bm, selected, loops=loops)
            for face, value in zip(bm.faces, uv_faces):
                face.uv_select = value
            bm.uv_select_sync_valid = selection_valid
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        if (_topo_box is not None and not report['failed'] and not dirty
                and last_topo is not None and getattr(last_topo.bm, 'is_valid', False)):
            _topo_box.append(last_topo)
    return report
