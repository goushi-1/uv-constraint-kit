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
    return dict(topo=topo, faces=frozenset(faces), groups=groups, index=index,
                tri=tri, local=local, areas=areas, group_triangles=group_triangles,
                representatives=representatives, reference_signed=reference_signed,
                weights=areas/areas.sum(), write_targets=write_targets)


def solve(core, topo, faces, fixed, runs, iterations, angle_length=1.0,
          use_density_map=True, auto=True, prevent_flips=False,
          prevent_overlap=False, overlap_space=0., free_selection_borders=False,
          constraint_strength=None, shared_uv_groups=None, _geometry=_UNPREPARED,
          _reference_sign=None, _coordinates=None, _contact_barrier=False):
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
    local = _geometry['local'].copy()
    x=(np.array([tuple(topo.loops[p][topo.uvl].uv)
                 for p in _geometry['representatives']],dtype=float)
       if _coordinates is None else np.array(_coordinates,dtype=float,copy=True))
    assert x.shape == (len(groups), 2)
    uv=x[tri]
    signed=((uv[:,1,0]-uv[:,0,0])*(uv[:,2,1]-uv[:,0,1])-
            (uv[:,1,1]-uv[:,0,1])*(uv[:,2,0]-uv[:,0,0]))
    # Boundary straightening can temporarily invert an adjacent triangle.
    # Its 3D frame must retain the input UV orientation, rather than making
    # that intermediate fold an intentional mirror in the optimization target.
    reference_signed=_geometry['reference_signed']
    signs=np.where(np.abs(reference_signed)>1e-14,np.sign(reference_signed),
                   np.where(signed>=0,1.,-1.))
    if _reference_sign is not None:
        # Only the checked recovery path supplies a chart-wide orientation,
        # after verifying consistent mesh winding and excluding intentional stacks.
        signs.fill(_reference_sign)
    # Reflect the local coordinate system, preserving both edge lengths and
    # their dot product. Negating one edge column changes the triangle shape.
    local[:,1,:]*=signs[:,None]
    density=np.sqrt(np.abs(signed).sum()*.5/areas.sum())
    local*=max(density,1e-12)
    if use_density_map:
        layer=topo.bm.faces.layers.float.get('uvck_density')
        if layer:
            triangle_face={tuple(t):face for face in topo.bm.faces if face.index in faces
                           for t in core.triangles_for_faces(topo,{face.index})}
            for i,triangle in enumerate(tri):
                group_triangle=group_triangles[i]
                face=triangle_face[group_triangle]
                local[i]*=max(float(face[layer]),1e-4)
    inv=np.linalg.inv(local)
    weights=_geometry['weights']
    arrays=[]
    for kind, positions in runs:
        members={g for p in positions for g in topo.loop_edge_vgroups(p) if g in index}
        arrays.append((kind,np.array(sorted({index[g] for g in members}),dtype=np.int64)))
    held={index[g]:np.asarray(tuple(uv),dtype=float) for g,uv in fixed.items() if g in index}
    penalty_scale = max(float(np.ptp(x, axis=0) @ np.ptp(x, axis=0)), 1e-20)
    axis_penalties = []
    if constraint_strength is not None:
        # Penalize edge directions in their own scale. A short segment must
        # not disappear inside a long run's point-variance penalty.
        for kind, positions in runs:
            if kind not in (C_HORIZONTAL, C_VERTICAL):
                continue
            edges = sorted({tuple(sorted((index[a], index[b]))) for p in positions
                            for a, b in [topo.loop_edge_vgroups(p)]
                            if a in index and b in index and index[a] != index[b]})
            if not edges:
                continue
            edges = np.asarray(edges, dtype=np.int64)
            lengths = ((x[edges[:, 1]]-x[edges[:, 0]])**2).sum(axis=1)
            weight = constraint_strength / (len(edges)*np.maximum(lengths, penalty_scale*1e-12))
            axis_penalties.append((1 if kind == C_HORIZONTAL else 0, edges, weight))
    # The caller decides whether this is an intentional partial vertex solve.
    # Do not infer pins again from transient UV flags in the numeric solver.
    boundary=[]
    if prevent_overlap:
        for p in core.selection_border_loops(topo,faces):
            a,b=topo.loop_edge_vgroups(p)
            if a in index and b in index:boundary.append((index[a],index[b]))
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
        for pos, i in _geometry['write_targets']:
            topo.loops[pos][topo.uvl].uv = solved[i]
        return len(tri), int((signed < 0).sum())
    scatter_index=np.concatenate((tri[:,0],tri[:,1],tri[:,2]))
    axes_only = all(kind in (C_HORIZONTAL, C_VERTICAL) for kind, _ in arrays)
    axis_sets = _axis_sets(arrays, held) if axes_only else []
    edge=np.array(boundary,dtype=np.int64)
    pairs=[(i,j) for i in range(len(edge)) for j in range(i+1,len(edge))
           if not set(edge[i]) & set(edge[j])] if prevent_overlap else []
    pair_index=np.asarray(pairs,dtype=np.int64)
    def borders_valid(value):
        if not prevent_overlap or not pairs:return True
        p=pair_index
        a=value[edge[p[:,0],0]];b=value[edge[p[:,0],1]]
        c=value[edge[p[:,1],0]];d=value[edge[p[:,1],1]]
        def cross(u,v):return u[:,0]*v[:,1]-u[:,1]*v[:,0]
        ab=b-a;cd=d-c
        if np.any((cross(ab,c-a)*cross(ab,d-a)<0)&(cross(cd,a-c)*cross(cd,b-c)<0)):return False
        if overlap_space>0:
            def distance(q,a,b):
                v=b-a
                t=np.clip(((q-a)*v).sum(axis=1)/np.maximum((v*v).sum(axis=1),1e-20),0,1)
                return np.linalg.norm(q-a-t[:,None]*v,axis=1)
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

    def energy(value, gradient=True):
        q=value[tri]
        u=np.stack((q[:,1]-q[:,0],q[:,2]-q[:,0]),axis=2)
        j=u@inv
        value_e, terms = _distortion_terms(j, angle_length, prevent_flips or reciprocal,
                                          reciprocal=reciprocal)
        if value_e is None or not borders_valid(value):
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
            q*=max(density*density,1e-6)*.05
        for (s,y,rho),alpha in zip(history,reversed(coeff)):
            q+=s*(alpha-rho*float((y*q).sum()))
        direction=-q
        slope=float((g*direction).sum())
        if slope>=0:direction=-g;slope=-float((g*g).sum());history=[]
        step=1.
        for _ in range(32):
            candidate=project(x+step*direction)
            next_e,_,energy_state=energy(candidate,gradient=False)
            if next_e<=e+1e-4*step*slope:break
            step*=.5
        else:break
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
    for group,i in index.items():
        for pos in topo.vgroup_loops[group]:
            if topo.loops[pos].face.index in faces:
                topo.loops[pos][topo.uvl].uv=x[i]
    return len(tri),int((signed<0).sum())
