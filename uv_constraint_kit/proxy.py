# SPDX-License-Identifier: GPL-3.0-or-later
"""Local/global supplemental solve for constrained islands with fixed UVs.

Conformal and principal-stretch objectives follow the angle/length setting.
A small symmetric Dirichlet term regularizes near collapse. This is an
independent implementation; the caller validates the entire resulting layout.
"""
import numpy as np


def fit_boundary(core, topo, faces, runs, held, *, iterations=40,
                 angle_length=1., use_density_map=True, prevent_flips=False,
                 prevent_overlap=False, overlap_space=0.):
    from . import safety
    if not held or (prevent_overlap and overlap_space>0):
        return 0
    native = [row for row in topo.bm.calc_loop_triangles() if row[0].face.index in faces]
    triangles = [tuple(topo.loop_to_vgroup[topo.loop_pos[l]] for l in row) for row in native]
    groups = sorted({g for row in triangles for g in row})
    if not groups or not runs:
        return 0
    ix = {g: i for i, g in enumerate(groups)}
    xyz = core.vgroup_positions3d(topo, groups)
    x = np.asarray([tuple(topo.vgroup_uv(g)) for g in groups], dtype=float)
    original = x.copy()
    local, tri, areas, triangle_faces = [], [], [], []
    for row,native_row in zip(triangles,native):
        p = core._triangle_local2d(tuple(xyz[g] for g in row))
        if p is None:
            continue
        ids = [ix[g] for g in row]
        q = x[ids]
        signed = np.linalg.det(np.column_stack((q[1]-q[0], q[2]-q[0])))
        matrix = np.column_stack((p[1]-p[0], p[2]-p[0]))
        area = abs(np.linalg.det(matrix))*.5
        if area < 1e-16 or abs(signed) <= 2e-14:
            continue
        matrix[1] *= 1 if signed > 0 else -1
        local.append(matrix); tri.append(ids); areas.append(area)
        triangle_faces.append(native_row[0].face)
    if not tri:
        return 0
    tri = np.asarray(tri, dtype=np.int64)
    local, areas = np.asarray(local), np.asarray(areas)
    q = x[tri]
    uv_area = np.abs(np.linalg.det(np.stack((q[:,1]-q[:,0],q[:,2]-q[:,0]),axis=2))).sum()*.5
    local *= np.sqrt(uv_area/areas.sum())
    if use_density_map:
        layer=topo.bm.faces.layers.float.get('uvck_density')
        if layer:
            local *= np.asarray([max(float(face[layer]),1e-4) for face in triangle_faces])[:,None,None]
    inv = np.linalg.inv(local)
    weight = areas/areas.sum()
    scatter_ix = np.concatenate((tri[:,0],tri[:,1],tri[:,2]))
    corner = np.stack((-inv[:,0]-inv[:,1],inv[:,0],inv[:,1]),axis=1)
    corner_norm = (corner*corner).sum(axis=2)
    anchors = {ix[g]: np.asarray(tuple(uv), dtype=float) for g,uv in held.items() if g in ix}
    fixed = np.zeros(len(x),dtype=bool)
    for i,uv in anchors.items():
        x[i] = uv; fixed[i] = True
    arrays = []
    for kind,positions in runs:
        if kind not in (core.C_HORIZONTAL,core.C_VERTICAL):
            return 0
        ids = np.asarray(sorted({ix[g] for p in positions for g in topo.loop_edge_vgroups(p)
                                 if g in ix}),dtype=np.int64)
        if len(ids)>1:
            arrays.append((1 if kind==core.C_HORIZONTAL else 0,ids))
    if not arrays:
        return 0
    # These triangles would be collapsed by exact collinearity. The existing
    # penalty solver already attempts a tolerance-feasible layout; this proxy
    # does not specialize in that ill-conditioned limit.
    if any(np.any(np.isin(tri,ids).all(axis=1)) for _,ids in arrays):
        return 0
    scale = max(float(np.ptp(x,axis=0)@np.ptp(x,axis=0)),1e-20)
    probe = safety.AcceptanceProbe(core,topo,faces)

    def jacobian(value):
        q = value[tri]
        return np.stack((q[:,1]-q[:,0],q[:,2]-q[:,0]),axis=2)@inv

    def scatter(value):
        gu = value@inv.transpose(0,2,1)
        out = np.empty_like(x)
        for axis in (0,1):
            v = np.concatenate((-gu[:,axis,0]-gu[:,axis,1],gu[:,axis,0],gu[:,axis,1]))
            out[:,axis] = np.bincount(scatter_ix,weights=v,minlength=len(x))
        return out

    def penalty(value,strength):
        out = np.zeros_like(value)
        for axis,ids in arrays:
            out[ids,axis] += strength/(len(ids)*scale)*(value[ids,axis]-value[ids,axis].mean())
        return out

    def energy(value,strength):
        j = jacobian(value)
        det = np.linalg.det(j)
        if np.any(det<=(1e-6 if prevent_flips else 1e-10)) or not np.isfinite(j).all():
            return np.inf
        norm = (j*j).sum(axis=(1,2))
        root = np.sqrt(np.maximum(norm+2*det,1e-20))
        e = float((weight*((1-angle_length)*norm/det+
                   angle_length*(norm-2*root+2)+.001*norm*(1+1/det**2))).sum())
        return e + float((value*penalty(value,strength)).sum())

    if not np.isfinite(energy(x,0)):
        return 0
    for strength in (.1,1.,10.,100.,1e3,1e4,1e5,1e6,1e7,1e8):
        for _ in range(iterations):
            j = jacobian(x)
            u,s,vt = np.linalg.svd(j)
            rotation = u@vt
            ws = (s+1)*(s*s+1)/s**3
            sd = (u*ws[:,None,:])@u.transpose(0,2,1)
            r = np.sqrt(s.prod(axis=1))
            ratio = s/r[:,None]
            ws = (ratio+1)*(ratio*ratio+1)/(2*ratio**3*r[:,None]**2)
            conformal = (u*ws[:,None,:])@u.transpose(0,2,1)
            wtw = ((1-angle_length)*conformal+angle_length*np.eye(2)+.001*sd)
            proxy_rhs = ((1-angle_length)*(conformal@rotation)*r[:,None,None]+
                         angle_length*rotation+.001*(sd@rotation))
            weighted = weight[:,None,None]*wtw
            # A small proximal term removes translation nullspace without
            # making the current position part of the distortion objective.
            prox = 1e-8/scale
            rhs = scatter(weight[:,None,None]*proxy_rhs)+prox*x
            def multiply(value):
                return scatter(weighted@jacobian(value))+penalty(value,strength)+prox*value
            diagonal = np.broadcast_to(prox*np.eye(2),(len(x),2,2)).copy()
            for a in (0,1):
                for b in (0,1):
                    values = corner_norm*weighted[:,a,b,None]
                    diagonal[:,a,b] += np.bincount(scatter_ix,weights=values.T.reshape(-1),minlength=len(x))
            for axis,ids in arrays:
                diagonal[ids,axis,axis] += strength/(len(ids)*scale)*(1-1/len(ids))
            eigen,basis = np.linalg.eigh(diagonal)
            eigen = np.maximum(eigen,np.maximum(eigen[:,-1:]*1e-14,1e-20))
            inverse = (basis/eigen[:,None,:])@basis.transpose(0,2,1)
            def precondition(value):return (inverse@value[:,:,None])[:,:,0]
            target = x.copy()
            residual = rhs-multiply(target); residual[fixed] = 0
            initial = float((residual*residual).sum())
            z = precondition(residual)
            direction = z.copy(); rz = float((residual*z).sum())
            for cg_step in range(min(4*len(x),1200)):
                hd = multiply(direction); hd[fixed] = 0
                dhd = float((direction*hd).sum())
                if dhd<=1e-30 or rz<=1e-30:
                    break
                alpha = rz/dhd
                target += alpha*direction
                residual -= alpha*hd
                if float((residual*residual).sum())<=max(initial*1e-16,1e-24):
                    break
                z = precondition(residual)
                next_rz = float((residual*z).sum())
                direction = z+(next_rz/rz)*direction; rz=next_rz
            for i,uv in anchors.items():target[i] = uv
            e = energy(x,strength)
            direction = target-x
            descent = 2*float(((multiply(x)-rhs)*direction).sum())
            if descent>=0:
                break
            alpha = 1.
            for _ in range(50):
                candidate = x+alpha*direction
                ne = energy(candidate,strength)
                if ne<=e+1e-4*alpha*descent:
                    break
                alpha *= .5
            else:
                break
            x = candidate
            if np.max(np.abs(alpha*direction))<1e-11:
                break
        core.land_uv_groups(topo,{g:x[i] for g,i in ix.items()},faces)
        health = probe.measure(topo)
        if health['finite'] and health['zero_edges']==0 and health['max_deviation']<=.02:
            return len(tri)
    core.land_uv_groups(topo,{g:original[i] for g,i in ix.items()},faces)
    return 0
