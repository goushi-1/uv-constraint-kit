# SPDX-License-Identifier: GPL-3.0-or-later
"""Projected metric optimization of the current UV layout (no re-unwrapping).

The angle objective is scale invariant; the length objective penalizes principal
stretch relative to the input density. This is an independent implementation, not RizomUV source.
"""
import numpy as np
from .const import C_HORIZONTAL, C_VERTICAL
from . import native_engine

_UNPREPARED = object()


def _distortion_terms(j, angle_length, prevent_flips, reciprocal=True):
    """Measure both compression and expansion in a feasible parameterization.

    The forward-only rigid energy has a finite cost when a singular value
    approaches zero. Symmetric Dirichlet adds the inverse map's stretch, so
    a short constrained segment cannot cheaply absorb nearby distortion.
    Infeasible starting maps retain the former forward objective to untangle.
    """
    det = j[:, 0, 0]*j[:, 1, 1]-j[:, 0, 1]*j[:, 1, 0]
    if not np.all(np.isfinite(j)) or (prevent_flips and np.any(det <= 1e-6)):
        return None, None
    norm = (j*j).sum(axis=(1, 2))
    # The conformal term uses unsigned area. Regularization keeps the energy
    # finite while crossing zero area; a negative determinant is not an error.
    squared = det*det+1e-16
    denominator = det if prevent_flips else np.sqrt(squared)
    derivative = np.ones_like(det) if prevent_flips else det/denominator
    root = np.sqrt(np.maximum(norm+2*det, 1e-20))
    length = .5*(norm+norm/squared-4) if reciprocal else norm-2*root+2
    value = (1-angle_length)*norm/denominator+angle_length*length
    return value, (j, det, norm, root, denominator, derivative, squared, reciprocal)


def _distortion_gradient(terms, angle_length):
    j, det, norm, root, denominator, derivative, squared, reciprocal = terms
    cofactor = np.empty_like(j)
    cofactor[:, 0, 0] = j[:, 1, 1]; cofactor[:, 1, 1] = j[:, 0, 0]
    cofactor[:, 0, 1] = -j[:, 1, 0]; cofactor[:, 1, 0] = -j[:, 0, 1]
    if reciprocal:
        a = (1-angle_length)*2/denominator+angle_length*(1+1/squared)
        b = (-(1-angle_length)*norm*derivative/denominator**2
             -angle_length*norm*det/squared**2)
    else:
        rotation = np.where(norm+2*det > 1e-20, 2*angle_length/root, 0.)
        a = (1-angle_length)*2/denominator+2*angle_length-rotation
        b = -(1-angle_length)*norm*derivative/denominator**2-rotation
    return a[:, None, None]*j+b[:, None, None]*cofactor


def _axis_sets(arrays, held):
    """Merge transitive H/V equalities once; the two axes then commute."""
    result = []
    for kind, axis in ((C_HORIZONTAL, 1), (C_VERTICAL, 0)):
        parent = {}
        def root(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for current, members in arrays:
            if current != kind or not len(members):
                continue
            for i in members:
                parent.setdefault(int(i), int(i))
            first = root(int(members[0]))
            for i in members[1:]:
                parent[root(int(i))] = first
        groups = {}
        for i in parent:
            groups.setdefault(root(i), []).append(i)
        for members in groups.values():
            anchor = next((held[i][axis] for i in members if i in held), None)
            result.append((axis, np.asarray(members, dtype=np.int64), anchor))
    return result


def _prepare_geometry(core, topo, faces, shared_uv_groups=None):
    """Prepare immutable 3D frames for one solve or boundary continuation."""
    if len(faces) >= 4096 and not shared_uv_groups and hasattr(topo, '_triangle_arrays'):
        tri_faces, tri_loops = topo._triangle_arrays()
        if len(faces) == len(topo.bm.faces):
            sel_loops = tri_loops
        else:
            sel_loops = tri_loops[np.isin(tri_faces, sorted(faces))]
        if not len(sel_loops):
            return None
        g = np.asarray(topo.loop_to_vgroup, dtype=np.int64)
        tri_g = g[sel_loops]
        distinct = (tri_g[:, 0] != tri_g[:, 1]) & (tri_g[:, 0] != tri_g[:, 2]) & (tri_g[:, 1] != tri_g[:, 2])
        tri_g = tri_g[distinct]
        if not len(tri_g):
            return None
        groups_arr, inv_flat = np.unique(tri_g, return_inverse=True)
        groups = groups_arr.tolist()
        tri_idx = inv_flat.reshape(-1, 3)
        index = {gr: i for i, gr in enumerate(groups)}
        representatives = (topo._vgroup_first_positions(groups_arr).tolist()
                           if hasattr(topo, '_vgroup_first_positions')
                           else [topo.vgroup_loops[gr][0] for gr in groups])
        rep_loops = [topo.loops[p] for p in representatives]
        prepared = native_engine.triangle_frames([loop.vert.co for loop in rep_loops], tri_idx)
        if prepared is not None:
            frames, eligible = prepared
            del prepared
            if not np.all(eligible):
                frames = frames[eligible]
            tri_arr = tri_idx[eligible]
            group_triangles_arr = tri_g[eligible]
            if not len(tri_arr):
                return None
            frames = np.asarray(frames)
            local = (frames[:, 1:] - frames[:, :1]).transpose(0, 2, 1).copy()
            areas = np.abs(np.linalg.det(local)) * .5
            valid = ~(areas < 1e-16)
            if not np.any(valid):
                return None
            tri = np.asarray(tri_arr, dtype=np.int64)[valid]
            local, areas = local[valid], areas[valid]
            group_triangles = group_triangles_arr[valid]
            reference = np.asarray([topo.initial_uv[p] for p in representatives], dtype=float)[tri]
            a, b = reference[:, 1] - reference[:, 0], reference[:, 2] - reference[:, 0]
            reference_signed = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
            all_faces = len(faces) == len(topo.bm.faces)
            pos = np.arange(topo.loop_count, dtype=np.int64) if all_faces else np.flatnonzero(np.isin(topo.loop_faces, list(faces)))
            pg = g[pos]
            idx = np.searchsorted(groups_arr, pg)
            in_set = (idx < len(groups_arr)) & (groups_arr[np.minimum(idx, len(groups_arr) - 1)] == pg)
            if all_faces and np.all(in_set):
                write_loops = topo.loops
                write_idx = idx.astype(np.intp, copy=False)
            else:
                valid_pos = pos[in_set]
                write_loops = [topo.loops[p] for p in valid_pos.tolist()]
                write_idx = idx[in_set].astype(np.intp, copy=False)
            return dict(topo=topo, faces=frozenset(faces), groups=groups, index=index,
                        tri=tri, local=local, inv_unit=np.linalg.inv(local), areas=areas,
                        group_triangles=group_triangles, representatives=representatives,
                        rep_loops=rep_loops, reference_signed=reference_signed,
                        weights=areas/areas.sum(), write_targets=None,
                        write_loops=write_loops, write_idx=write_idx)

    triangles = core.triangles_for_faces(topo, faces)
    groups = sorted({g for tri in triangles for g in tri})
    if not triangles:
        return None
    index = {g:i for i,g in enumerate(groups)}
    if shared_uv_groups:
        original_index = index.copy()
        index = {g: original_index.get(shared_uv_groups.get(g, g), original_index[g])
                 for g in groups}
    xyz = core.vgroup_positions3d(topo, groups)
    spatial_index = original_index if shared_uv_groups else index
    prepared = native_engine.triangle_frames(
        [xyz[g] for g in groups],
        [(spatial_index[a], spatial_index[b], spatial_index[c]) for a,b,c in triangles])
    if prepared is None:
        frames, tri, group_triangles = [], [], []
        for a,b,c in triangles:
            p = core._triangle_local2d((xyz[a],xyz[b],xyz[c]))
            if p is None:
                continue
            frames.append(p)
            tri.append((index[a],index[b],index[c]))
            group_triangles.append((a,b,c))
    else:
        frames, eligible = prepared
        del prepared
        if not np.all(eligible):
            frames = frames[eligible]
        group_triangles = [row for row, keep in zip(triangles, eligible) if keep]
        # Geometry uses each triangle's own vertices even when UVs are shared.
        tri = [(index[a],index[b],index[c]) for a,b,c in group_triangles]
    if not tri:
        return None
    frames = np.asarray(frames)
    local = (frames[:, 1:]-frames[:, :1]).transpose(0, 2, 1).copy()
    areas = np.abs(np.linalg.det(local))*.5
    valid = ~(areas < 1e-16)
    if not np.any(valid):
        return None
    tri = np.asarray(tri, dtype=np.int64)[valid]
    local, areas = local[valid], areas[valid]
    group_triangles = [row for row, keep in zip(group_triangles, valid) if keep]
    representatives = [topo.vgroup_loops[g][0] for g in groups]
    reference = np.asarray([topo.initial_uv[p] for p in representatives], dtype=float)[tri]
    a, b = reference[:, 1]-reference[:, 0], reference[:, 2]-reference[:, 0]
    reference_signed = a[:, 0]*b[:, 1]-a[:, 1]*b[:, 0]
    write_targets = [(pos, i) for g, i in index.items() for pos in topo.vgroup_loops[g]
                     if topo.loop_faces[pos] in faces]
    inv_unit = np.linalg.inv(local)
    rep_loops = [topo.loops[p] for p in representatives]
    write_loops = [topo.loops[pos] for pos, _ in write_targets]
    write_idx = np.asarray([i for _, i in write_targets], dtype=np.intp)
    return dict(topo=topo, faces=frozenset(faces), groups=groups, index=index,
                tri=tri, local=local, inv_unit=inv_unit, areas=areas,
                group_triangles=group_triangles, representatives=representatives,
                rep_loops=rep_loops, reference_signed=reference_signed,
                weights=areas/areas.sum(), write_targets=write_targets,
                write_loops=write_loops, write_idx=write_idx)


def solve(core, topo, faces, fixed, runs, iterations, angle_length=1.0,
          use_density_map=True, auto=True, prevent_flips=False,
          prevent_overlap=False, overlap_space=0., free_selection_borders=False,
          constraint_strength=None, shared_uv_groups=None, _geometry=_UNPREPARED,
          _reference_sign=None, _coordinates=None, _contact_barrier=False,
          _skip_same_uv=False):
    # Internal continuation trials can keep coordinates outside the BMesh.
    # They return (count, flips, float32-rounded coordinates), matching a UV
    # write/read round trip. Ordinary calls retain their two-value API.
    if _coordinates is not None:
        assert not shared_uv_groups
    faces = set(faces)
    if _geometry is _UNPREPARED:
        _geometry = _prepare_geometry(core, topo, faces, shared_uv_groups)
    elif _geometry is not None:
        # This cache lives only within a single continuation, before any native
        # unwrap or topology change. UV-dependent data is rebuilt on every trial.
        assert _geometry['topo'] is topo and _geometry['faces'] == faces
        assert not shared_uv_groups
    if _geometry is None:
        assert _coordinates is None
        return 0, 0
    groups, index = _geometry['groups'], _geometry['index']
    tri, areas = _geometry['tri'], _geometry['areas']
    group_triangles = _geometry['group_triangles']
    local = None
    x = (np.asarray(core.mesh_reader.read_uv(_geometry['rep_loops'], topo.uvl), dtype=float)
         if _coordinates is None else np.array(_coordinates, dtype=float, copy=True))
    assert x.shape == (len(groups), 2)
    uv = x[tri]
    signed = ((uv[:, 1, 0]-uv[:, 0, 0])*(uv[:, 2, 1]-uv[:, 0, 1]) -
              (uv[:, 1, 1]-uv[:, 0, 1])*(uv[:, 2, 0]-uv[:, 0, 0]))
    # Boundary straightening can temporarily invert an adjacent triangle.
    # Its 3D frame must retain the input UV orientation, rather than making
    # that intermediate fold an intentional mirror in the optimization target.
    reference_signed = _geometry['reference_signed']
    signs = np.where(np.abs(reference_signed) > 1e-14, np.sign(reference_signed),
                     np.where(signed >= 0, 1., -1.))
    if _reference_sign is not None:
        # Only the checked recovery path supplies a chart-wide orientation,
        # after verifying consistent mesh winding and excluding intentional stacks.
        signs.fill(_reference_sign)
    density = np.sqrt(np.abs(signed).sum()*.5/areas.sum())
    scale = max(density, 1e-12)
    density_layer = topo.bm.faces.layers.float.get('uvck_density') if use_density_map else None
    if not density_layer and len(tri) >= 32768:
        inv = _geometry['inv_unit'] / scale
        inv[:, :, 1] *= signs[:, None]
    else:
        local = _geometry['local'].copy()
        local[:, 1, :] *= signs[:, None]
        local *= scale
        if density_layer:
            triangle_face = {tuple(t): face for face in topo.bm.faces if face.index in faces
                             for t in core.triangles_for_faces(topo, {face.index})}
            for i, triangle in enumerate(tri):
                group_triangle = tuple(int(v) for v in group_triangles[i])
                face = triangle_face[group_triangle]
                local[i] *= max(float(face[density_layer]), 1e-4)
        inv = np.linalg.inv(local)
    weights=_geometry['weights']
    run_cache = _geometry.setdefault('_run_cache', {})
    run_key = (id(runs), len(runs))
    cached_run = run_cache.get(run_key)
    if cached_run is None or cached_run[0] is not runs:
        arrays = []
        raw_penalties = []
        for kind, positions in runs:
            members = {g for p in positions for g in topo.loop_edge_vgroups(p) if g in index}
            arrays.append((kind, np.array(sorted({index[g] for g in members}), dtype=np.int64)))
            if kind in (C_HORIZONTAL, C_VERTICAL):
                edges = sorted({tuple(sorted((index[a], index[b]))) for p in positions
                                for a, b in [topo.loop_edge_vgroups(p)]
                                if a in index and b in index and index[a] != index[b]})
                if edges:
                    raw_penalties.append((1 if kind == C_HORIZONTAL else 0, np.asarray(edges, dtype=np.int64)))
        cached_run = (runs, arrays, raw_penalties)
        run_cache[run_key] = cached_run
    else:
        _, arrays, raw_penalties = cached_run
    held={index[g]:np.asarray(tuple(uv),dtype=float) for g,uv in fixed.items() if g in index}
    penalty_scale = max(float(np.ptp(x, axis=0) @ np.ptp(x, axis=0)), 1e-20)
    axis_penalties = []
    if constraint_strength is not None:
        # Penalize edge directions in their own scale. A short segment must
        # not disappear inside a long run's point-variance penalty.
        floor = penalty_scale * 1e-12
        for axis, edges in raw_penalties:
            lengths = ((x[edges[:, 1]]-x[edges[:, 0]])**2).sum(axis=1)
            weight = constraint_strength / (len(edges)*np.maximum(lengths, floor))
            axis_penalties.append((axis, edges, weight))
    # The caller decides whether this is an intentional partial vertex solve.
    # Do not infer pins again from transient UV flags in the numeric solver.
    boundary=[]
    if prevent_overlap:
        cached_boundary = _geometry.get('_boundary')
        if cached_boundary is None:
            cached_boundary = []
            for p in core.selection_border_loops(topo,faces):
                a,b=topo.loop_edge_vgroups(p)
                if a in index and b in index:cached_boundary.append((index[a],index[b]))
            _geometry['_boundary'] = cached_boundary
        boundary = cached_boundary
    native = native_engine.solve(
        x, tri, inv, weights, arrays, held, axis_penalties, boundary,
        density, angle_length, constraint_strength, penalty_scale, overlap_space,
        int(iterations), auto, prevent_flips, 2 if _contact_barrier else prevent_overlap)
    if native is None and _contact_barrier:
        # A CPU failure must not silently label a Python hard-overlap solve
        # as contact optimization. The checked caller restores the normal UVs.
        return (0, int((signed < 0).sum()), x) if _coordinates is not None else (0, int((signed < 0).sum()))
    if native is not None:
        status, solved = native
        if status == 1:
            if _coordinates is not None:
                return 0, int((signed < 0).sum()), x
            return 0, int((signed < 0).sum())
        if _coordinates is not None:
            with np.errstate(over='ignore', invalid='ignore'):
                rounded = solved.astype(np.float32).astype(float)
            return len(tri), int((signed < 0).sum()), rounded
        write_loops = _geometry['write_loops']
        write_idx = _geometry['write_idx']
        if _skip_same_uv and write_loops and core._same_uv_bits(solved, x):
            # A group representative can be unchanged while its other loops
            # differ. The original would still write to these loops.
            current = core.mesh_reader.read_uv(write_loops, topo.uvl)
            if core._same_uv_bits(current, solved[write_idx]):
                return len(tri), int((signed < 0).sum())
        core.mesh_reader.write_uv(write_loops, topo.uvl, solved[write_idx])
        return len(tri), int((signed < 0).sum())
    if local is None:
        local = _geometry['local'].copy()
        local[:, 1, :] *= signs[:, None]
        local *= scale
    scatter_index=np.concatenate((tri[:,0],tri[:,1],tri[:,2]))
    axes_only = all(kind in (C_HORIZONTAL, C_VERTICAL) for kind, _ in arrays)
    axis_sets = _axis_sets(arrays, held) if axes_only else []
    edge=np.array(boundary,dtype=np.int64)
    pairs=[(i,j) for i in range(len(edge)) for j in range(i+1,len(edge))
           if not set(edge[i]) & set(edge[j])] if prevent_overlap else []
    pair_index=np.asarray(pairs,dtype=np.int64)
    def borders_valid(value, baseline=None):
        if not prevent_overlap or not pairs:return True
        p=pair_index
        a=value[edge[p[:,0],0]];b=value[edge[p[:,0],1]]
        c=value[edge[p[:,1],0]];d=value[edge[p[:,1],1]]
        def cross(u,v):return u[:,0]*v[:,1]-u[:,1]*v[:,0]
        def distance(q,u,v):
            w=v-u
            t=np.clip(((q-u)*w).sum(axis=1)/np.maximum((w*w).sum(axis=1),1e-20),0,1)
            return np.linalg.norm(q-u-t[:,None]*w,axis=1)
        ab=b-a;cd=d-c
        if np.any((cross(ab,c-a)*cross(ab,d-a)<0)&(cross(cd,a-c)*cross(cd,b-c)<0)):return False
        if not _contact_barrier:
            with np.errstate(over='ignore', invalid='ignore'):
                af=a.astype(np.float32).astype(np.float64)
                bf=b.astype(np.float32).astype(np.float64)
                cf=c.astype(np.float32).astype(np.float64)
                df=d.astype(np.float32).astype(np.float64)
            abf=bf-af;cdf=df-cf
            if np.any((cross(abf,cf-af)*cross(abf,df-af)<0)&(cross(cdf,af-cf)*cross(cdf,bf-cf)<0)):return False
            if baseline is not None and overlap_space<=0.:
                dist_cand=np.minimum.reduce([distance(a,c,d),distance(b,c,d),distance(c,a,b),distance(d,a,b)])
                base_scale=max(1.0,float(np.sqrt(max(penalty_scale,1e-20))))
                pair_abs=np.maximum.reduce([np.abs(a[:,0]),np.abs(a[:,1]),np.abs(b[:,0]),np.abs(b[:,1]),
                                            np.abs(c[:,0]),np.abs(c[:,1]),np.abs(d[:,0]),np.abs(d[:,1])])
                local_margin=6e-7*np.maximum(base_scale,pair_abs)
                close=dist_cand<local_margin
                if np.any(close):
                    pc=p[close]
                    ba=baseline[edge[pc[:,0],0]];bb=baseline[edge[pc[:,0],1]]
                    bc=baseline[edge[pc[:,1],0]];bd=baseline[edge[pc[:,1],1]]
                    dist_base=np.minimum.reduce([distance(ba,bc,bd),distance(bb,bc,bd),distance(bc,ba,bb),distance(bd,ba,bb)])
                    if np.any(dist_cand[close]<np.minimum(local_margin[close],0.5*dist_base)):return False
        if overlap_space>0:
            dist=np.minimum.reduce([distance(a,c,d),distance(b,c,d),distance(c,a,b),distance(d,a,b)])
            if np.any(dist<overlap_space):return False
        return True
    def project(value):
        if constraint_strength is None:
            if axes_only:
                for axis, members, anchor in axis_sets:
                    value[members, axis] = value[members, axis].mean() if anchor is None else anchor
            else:
                core.project_runs(value,arrays)
                for kind, members in arrays:
                    anchors = [i for i in members if i in held]
                    if anchors and kind in (C_HORIZONTAL, C_VERTICAL):
                        axis = 1 if kind == C_HORIZONTAL else 0
                        value[members, axis] = held[anchors[0]][axis]
        for i,uv in held.items():value[i]=uv
        return value
    def gradient_from(terms, penalty_gradient, penalty_terms, value):
        gj=_distortion_gradient(terms, angle_length)*weights[:,None,None]
        gu=gj@inv.transpose(0,2,1)
        # Keep the same corner-batch addition order as three add.at calls.
        # Bincount avoids repeated indexed 2D scatter on large islands.
        g=np.empty_like(x)
        for axis in (0,1):
            contribution=np.concatenate((-gu[:,axis,0]-gu[:,axis,1],
                                          gu[:,axis,0],gu[:,axis,1]))
            g[:,axis]=np.bincount(scatter_index,weights=contribution,minlength=len(x))
        # Project gradients onto the linear H/V constraint tangent space.
        if constraint_strength is None:
            if axes_only:
                for axis, members, anchor in axis_sets:
                    g[members, axis] = g[members, axis].mean() if anchor is None else 0.
            else:
                for kind,members in arrays:
                    if kind in (C_HORIZONTAL,C_VERTICAL) and members.size:
                        axis=1 if kind==C_HORIZONTAL else 0
                        g[members,axis]=(0. if any(i in held for i in members)
                                         else g[members,axis].mean())
        else:
            if penalty_gradient is None:
                penalty_gradient=np.zeros_like(x)
                for members,direction,perpendicular,weight in penalty_terms:
                    penalty_gradient[members] += (2*weight*perpendicular)[:, None]*direction
            g += penalty_gradient
            for axis, edges, weight in axis_penalties:
                delta = value[edges[:, 1], axis]-value[edges[:, 0], axis]
                scatter = np.concatenate((edges[:, 0], edges[:, 1]))
                contribution = 2*weight*delta
                g[:, axis] += np.bincount(scatter, weights=np.concatenate((-contribution, contribution)), minlength=len(x))
        for i in held:g[i]=0
        return g

    def energy(value, gradient=True, baseline=None):
        q=value[tri]
        u=np.stack((q[:,1]-q[:,0],q[:,2]-q[:,0]),axis=2)
        j=u@inv
        value_e, terms = _distortion_terms(j, angle_length, prevent_flips or reciprocal,
                                          reciprocal=reciprocal)
        if value_e is None or not borders_valid(value, baseline=baseline):
            return (float('inf'),None) if gradient else (float('inf'),None,None)
        e=float((value_e*weights).sum())
        penalty_gradient = None
        penalty_terms = []
        if constraint_strength is not None:
            penalty_gradient = np.zeros_like(x) if gradient else None
            for kind, members in arrays:
                if kind in (C_HORIZONTAL, C_VERTICAL):
                    continue
                if len(members) < 2:
                    continue
                relative = value[members]-value[members].mean(axis=0)
                _, basis = np.linalg.eigh(relative.T @ relative)
                direction = basis[:, 0]
                perpendicular = relative @ direction
                weight = constraint_strength/(len(members)*penalty_scale)
                e += weight*float(perpendicular @ perpendicular)
                if gradient:
                    penalty_gradient[members] += (2*weight*perpendicular)[:, None]*direction
                else:
                    penalty_terms.append((members,direction,perpendicular,weight))
            for axis, edges, weight in axis_penalties:
                delta = value[edges[:, 1], axis]-value[edges[:, 0], axis]
                e += float((weight*delta*delta).sum())
        if not gradient:
            return e,None,(terms,None,penalty_terms,value)
        return e,gradient_from(terms,penalty_gradient,penalty_terms,value)
    project(x)
    initial=x[tri]
    initial_j=np.stack((initial[:,1]-initial[:,0],initial[:,2]-initial[:,0]),axis=2)@inv
    initial_det=initial_j[:,0,0]*initial_j[:,1,1]-initial_j[:,0,1]*initial_j[:,1,0]
    # A feasible solve stays in its orientation component. Already-folded input
    # needs the forward objective until the recovery/continuation makes it valid.
    reciprocal=bool(np.all(np.isfinite(initial_det)) and np.all(initial_det>1e-6))
    e,g=energy(x)
    if not np.isfinite(e):
        if _coordinates is not None:
            return 0, int((signed < 0).sum()), np.array(_coordinates,dtype=float,copy=True)
        return 0,int((signed<0).sum())
    initial_uv=x.copy() if (prevent_overlap and not _contact_barrier and overlap_space<=0.) else None
    history=[]
    limit=1000 if auto else max(1,int(iterations))
    for _ in range(limit):
        if auto and float(np.abs(g).max())<1e-6:break
        q=g.copy();coeff=[]
        for s,y,rho in reversed(history):
            alpha=rho*float((s*q).sum());coeff.append(alpha);q-=alpha*y
        if history:
            s,y,_=history[-1]
            q*=float((s*y).sum())/max(float((y*y).sum()),1e-20)
        else:
            scale = max(density*density,1e-6)*.05
            g_inf = float(np.abs(g).max())
            max_disp = .05 * float(np.sqrt(max(penalty_scale, 1e-20)))
            if g_inf > 0. and max_disp > 0. and scale * g_inf > max_disp:
                scale = max_disp / g_inf
            q*=scale
        for (s,y,rho),alpha in zip(history,reversed(coeff)):
            q+=s*(alpha-rho*float((y*q).sum()))
        direction=-q
        slope=float((g*direction).sum())
        if slope>=0:
            g_inf = float(np.abs(g).max())
            max_disp = .05 * float(np.sqrt(max(penalty_scale, 1e-20)))
            reset_scale = (max_disp / g_inf) if (g_inf > max_disp and max_disp > 0.) else 1.
            direction = -reset_scale * g
            slope = float((g * direction).sum())
            history = []
        step=1.
        accepted=False
        for _ in range(32):
            candidate=project(x+step*direction)
            next_e,_,energy_state=energy(candidate,gradient=False,baseline=initial_uv)
            if next_e<=e+1e-4*step*slope:
                accepted=True
                break
            step*=.5
        if not accepted:
            if not history:break
            history=[]
            continue
        # Reuse the accepted trial's energy intermediates; rejected trials
        # never build or scatter a gradient.
        next_g=gradient_from(*energy_state)
        change=candidate-x;y=next_g-g
        dot=float((change*y).sum())
        if dot>1e-14:
            history.append((change,y,1/dot));history=history[-8:]
        improvement=e-next_e
        x,e,g=candidate,next_e,next_g
        if auto and improvement<=1e-10*max(1,abs(e)):break
    if _coordinates is not None:
        with np.errstate(over='ignore', invalid='ignore'):
            rounded = x.astype(np.float32).astype(float)
        return len(tri), int((signed < 0).sum()), rounded
    core.mesh_reader.write_uv(_geometry['write_loops'], topo.uvl, x[_geometry['write_idx']])
    return len(tri),int((signed<0).sum())
