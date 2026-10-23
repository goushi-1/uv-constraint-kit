# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional native refinement of an already accepted UV layout.

Blender's SLIM supplies an alternative, while interpolation and independent
checks keep an alternative only when angle and length quality all improve.
No external application, library or mesh topology changes are required.
"""
import math

import bmesh
import bpy
import numpy as np

from . import safety


def _angle_errors(core, bm, uvl, faces):
    for triangle in bm.calc_loop_triangles():
        if triangle[0].face.index not in faces:
            continue
        uv = [tuple(l[uvl].uv) for l in triangle]
        xyz = [l.vert.co for l in triangle]
        for corner in range(3):
            a2, a3 = core._corner_angle2(uv, corner), core._corner_angle3(xyz, corner)
            if a2 is not None and a3 is not None and math.isfinite(a2) and math.isfinite(a3):
                yield abs(a2-a3)


def quality(core, topo, faces):
    """Native triangle angles and both UV sides of each mesh edge."""
    faces = set(faces)
    angles = list(_angle_errors(core, topo.bm, topo.uvl, faces))
    positions3 = core.vgroup_positions3d(topo)
    pairs = {tuple(sorted(topo.loop_edge_vgroups(p))) for p, l in enumerate(topo.loops)
             if l.face.index in faces}
    ratios = []
    for a, b in pairs:
        length3 = (positions3[a]-positions3[b]).length
        if length3 <= 1e-12:
            continue
        delta = topo.vgroup_uv(a)-topo.vgroup_uv(b)
        ratio = math.hypot(delta.x, delta.y)/length3
        if math.isfinite(ratio):
            ratios.append(ratio)
    if not angles or not ratios:
        return None
    median = float(np.median(ratios))
    if median <= 0:
        return None
    return np.asarray((max(angles), np.percentile(angles, 95),
                       max(abs(r-median) for r in ratios)/median), dtype=float)


def improve(core, context, obj, faces, settings):
    """Return diagnostics; retain the accepted input on any failed refinement.

Manual iteration budgets, non-unit strength, metric sizing and density painting
continue through the existing solver. The extra native trial is bounded to
moderate islands and is never part of overlay drawing or live transform timers.
"""
    if (not settings.get('auto_iterations', True) or settings.get('mix', 1.) != 1.
            or settings.get('keep_metric', False) or settings.get('overlap_space', 0.) > 0
            or settings.get('fill_holes', False)):
        return []
    # Unwrap enumerates the view layer's edit objects; a context object override
    # alone cannot restrict a multi-object edit to this mesh.
    if context.edit_object != obj or len(context.objects_in_mode_unique_data) != 1:
        return []
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    if sum(len(face.loops) for face in bm.faces) > 250000:
        return []
    # _improve_one cannot refine mirrored/folded input. Exclude those islands
    # before building whole-mesh UV groups or scanning intentional stacks.
    faces = set(faces)
    uvl = core.uv_layer(bm)
    def signed(triangle):
        a, b, c = (l[uvl].uv for l in triangle)
        return (b.x-a.x)*(c.y-a.y)-(b.y-a.y)*(c.x-a.x)
    bad = {t[0].face.index for t in bm.calc_loop_triangles()
           if t[0].face.index in faces and signed(t) <= 2e-14}
    if bad:
        faces -= core.connected_uv_faces(bm, uvl, bad)
    del signed, uvl
    if not faces:
        return [dict(skipped='input_orientation')]
    # The existing trial rejects quality < 40 before any unwrap. Its maximum
    # native angle needs no UV connectivity, stack scan, or edge-length metric.
    # If the whole target is below that threshold every island will skip.
    errors = list(_angle_errors(core, bm, core.uv_layer(bm), set(faces)))
    if not errors or max(errors) < 40.:
        return [dict(skipped='quality')]
    topo = core.UVTopology(bm, core.uv_layer(bm))
    islands = [set(f) for f in core.face_islands(topo, set(faces)).values()]
    shared = core.stacked_uv_groups(topo, set(range(len(bm.faces))))
    stacked = set(shared) | set(shared.values())
    reports = []
    for island in islands:
        if not 2 <= len(island) <= 5000:
            continue
        bm = bmesh.from_edit_mesh(obj.data)
        core.prepare_bm(bm)
        topo = core.UVTopology(bm, core.uv_layer(bm))
        density = bm.faces.layers.float.get('uvck_density')
        if (settings.get('use_density_map', True) and density
                and any(abs(float(bm.faces[fi][density])-1.) > 1e-6 for fi in island)):
            continue
        # Native reparameterization must not separate intentional face stacks.
        island_groups = {topo.loop_to_vgroup[topo.loop_pos[l]]
                         for fi in island for l in bm.faces[fi].loops}
        if stacked.intersection(island_groups):
            continue
        # No wrapped edit BMesh survives in the caller over native unwrap.
        del density, bm, topo
        reports.append(_improve_one(core, context, obj, island, settings))
    return reports


def _improve_one(core, context, obj, faces, settings):
    def topology():
        bm = bmesh.from_edit_mesh(obj.data)
        core.prepare_bm(bm)
        return core.UVTopology(bm, core.uv_layer(bm))

    def snapshot(topo):
        return np.asarray([tuple(l[topo.uvl].uv) for l in topo.loops])

    def land(value):
        topo = topology()
        for l, uv in zip(topo.loops, value):
            l[topo.uvl].uv = uv
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)

    topo = topology()
    positions = core.constrained_loops(topo.bm, only_faces=faces)
    runs = core.constraint_runs(topo.bm, topo, positions)
    triangles = [tuple(topo.loop_pos[l] for l in row) for row in topo.bm.calc_loop_triangles()
                 if row[0].face.index in faces]
    if not triangles:
        return dict(skipped='no_triangles')
    tri = np.asarray(triangles, dtype=int)
    base = snapshot(topo)

    def crosses(value):
        q = value[tri]
        a, b = q[:, 1]-q[:, 0], q[:, 2]-q[:, 0]
        return a[:, 0]*b[:, 1]-a[:, 1]*b[:, 0]

    old_cross = crosses(base)
    # Preserve mirrored or degenerate input through the existing solver.
    if np.any(old_cross <= 2e-14):
        return dict(skipped='input_orientation')
    baseline = quality(core, topo, faces)
    if baseline is None or not np.all(np.isfinite(baseline)) or baseline[0] < 40.:
        return dict(skipped='quality')
    held = core._island_pins(topo, faces)
    held.update(core.selection_region_pins(topo, faces,
        settings.get('free_selection_borders', False), context,
        settings.get('respect_uv_selection', True)))
    if settings.get('fix_boundary'):
        for p in core.selection_border_loops(topo, faces):
            for g in topo.loop_edge_vgroups(p):
                held[g] = topo.vgroup_uv(g).copy()
    native_groups = set(held)
    native_groups.update(g for p in positions for g in topo.loop_edge_vgroups(p))
    target = [p for p, l in enumerate(topo.loops) if l.face.index in faces]
    target_set = set(target)
    outside = [p for p, l in enumerate(topo.loops) if l.face.index not in faces]
    fixed = [p for g in held for p in topo.vgroup_loops[g] if p in target_set]
    native_fixed = [p for p in target if topo.loop_to_vgroup[p] in native_groups]
    connectivity = topo.loop_to_vgroup.copy()
    pins = [bool(l[topo.uvl].pin_uv) for l in topo.loops]
    selected = core._capture_selection(topo.bm)
    selected_uv_faces = [f.uv_select for f in topo.bm.faces]
    selection_valid = topo.bm.uv_select_sync_valid
    ts = context.scene.tool_settings
    mesh_mode = tuple(ts.mesh_select_mode)
    saved_tools = {}
    for prop in ts.bl_rna.properties:
        if (prop.is_readonly or prop.type not in {'BOOLEAN', 'INT', 'FLOAT', 'ENUM', 'STRING'}
                or not ('uv' in prop.identifier.lower() or 'unwrap' in prop.identifier.lower())):
            continue
        value = getattr(ts, prop.identifier)
        saved_tools[prop.identifier] = tuple(value) if getattr(prop, 'is_array', False) else value
    probe = safety.AcceptanceProbe(core, topo, faces, check_other_islands=True)
    area = core.island_area(topo, faces)
    best, best_score, chosen = base.copy(), 1., 0.
    report = dict(faces=len(faces), before=baseline.tolist(), alpha=0.)
    try:
        ts.use_edge_path_live_unwrap = False
        ts.use_uv_select_sync = False
        ts.uv_select_mode = 'VERTEX'
        ts.mesh_select_mode = (False, False, True)
        core.select_faces_only(topo.bm, faces)
        for face in topo.bm.faces:
            face.uv_select = face.index in faces
        for p, l in enumerate(topo.loops):
            member = l.face.index in faces
            movable = member and topo.loop_to_vgroup[p] not in native_groups
            l.uv_select_vert = movable
            l.uv_select_edge = movable
            l[topo.uvl].pin_uv = pins[p] or (member and topo.loop_to_vgroup[p] in native_groups)
        topo.bm.uv_select_sync_valid = True
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        del l, face, topo
        status = bpy.ops.uv.unwrap(method='MINIMUM_STRETCH', fill_holes=False,
            use_original_bounds=False, correct_aspect=False, no_flip=True, iterations=50,
            use_subsurf_data=False, use_weights=False)
        topo = topology()
        raw = snapshot(topo)
        for p, l in enumerate(topo.loops):
            l[topo.uvl].pin_uv = pins[p]
        tolerance = max(float(np.abs(base).max()), 1.)*2e-7
        if ('FINISHED' not in status or not np.all(np.isfinite(raw))
                or not np.array_equal(raw[outside], base[outside])
                or topo.loop_to_vgroup != connectivity
                or (native_fixed and np.abs(raw[native_fixed]-base[native_fixed]).max() > tolerance)):
            report['discarded'] = 'native_integrity'
        else:
            raw[native_fixed] = base[native_fixed]
            reference = {p: tuple(uv) for p, uv in enumerate(base)}
            for alpha in (.05, .1, .25, .5, .75, 1.):
                land(base+alpha*(raw-base))
                topo = topology()
                if topo.loop_to_vgroup != connectivity:
                    continue
                if held:
                    core.land_uv_groups(topo, held, faces)
                    if not core._restore_anchored_area(topo, faces, held, runs, area):
                        continue
                else:
                    core.restore_pose(topo, faces, reference, allow_rotation=False, target_area=area)
                value = snapshot(topo)
                if (not np.all(np.isfinite(value)) or np.any(crosses(value) <= 2e-14)
                        or not np.array_equal(value[fixed], base[fixed])
                        or not np.array_equal(value[outside], base[outside])
                        or abs(core.island_area(topo, faces)-area) > max(area, 1e-12)*1e-4):
                    continue
                health = probe.measure(topo)
                if health['zero_edges'] or health['max_deviation'] > .05:
                    continue
                measured = quality(core, topo, faces)
                if measured is None or not np.all(np.isfinite(measured)):
                    continue
                scores = measured/np.maximum(baseline, 1e-8)
                if scores.max() > 1.00001 or scores.min() > .99:
                    continue
                score = float(scores.mean())
                if score >= best_score:
                    continue
                overlap = probe.check_overlaps(topo)
                if (not overlap['overlap_check_complete'] or overlap['novel_self_overlaps']
                        or overlap['novel_island_overlaps']):
                    continue
                best, best_score, chosen = value.copy(), score, alpha
                report['after'] = measured.tolist()
    except Exception as exc:
        best, chosen = base.copy(), 0.
        report['error'] = '%s: %s' % (type(exc).__name__, exc)
    finally:
        try:
            land(best)
        finally:
            topo = topology()
            for p, l in enumerate(topo.loops):
                l[topo.uvl].pin_uv = pins[p]
            for name, value in saved_tools.items():
                if name != 'use_edge_path_live_unwrap':
                    setattr(ts, name, value)
            ts.mesh_select_mode = mesh_mode
            core._apply_selection(topo.bm, selected)
            for face, was_selected in zip(topo.bm.faces, selected_uv_faces):
                face.uv_select = was_selected
            topo.bm.uv_select_sync_valid = selection_valid
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
            ts.use_edge_path_live_unwrap = saved_tools['use_edge_path_live_unwrap']
    report['alpha'] = chosen
    return report
