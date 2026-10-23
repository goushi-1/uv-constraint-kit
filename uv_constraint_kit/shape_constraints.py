# SPDX-License-Identifier: GPL-3.0-or-later
"""Undoable closed-loop shape templates, solved through existing fixed groups.

Templates follow the loop's current translation, rotation and uniform scale.
Their fitted points are held for one solve; user pins are restored afterwards.
No timer changes UVs and no drawing callback creates or changes custom data.
"""
from contextlib import contextmanager
from types import SimpleNamespace
import math
import zlib

import bmesh
import bpy
import numpy as np
from bpy.app.handlers import persistent

from . import core, live_symmetry, overlay
from .const import C_CIRCULAR, C_RECTANGULAR

LAYER = 'uvck_shape_constraints'
BACKUP = '_uvck_shape_constraints_save'
_draw_cache = {}


def _plan(topo, strict=True):
    raw = live_symmetry._raw(topo.bm, topo.loops, LAYER)
    if not raw:
        return []
    try:
        plan = live_symmetry._unpack(raw)
        if not plan or plan.get('schema') != 'UVCK_SHAPES_1':
            raise ValueError('Invalid shape constraint data')
        groups = [r for r in plan['groups'] if r['uv_name'] == topo.uvl.name]
        stamp=live_symmetry.signature(topo.bm)
        for record in groups:
            if record.get('topology',plan['topology']) != stamp:
                raise ValueError('Shape constraints no longer match the mesh topology')
            if record['kind'] not in ('CIRCULAR', 'RECTANGULAR'):
                raise ValueError('Invalid shape constraint data')
            if (len(record['members']) < 3 or
                    len(record['members']) != len(record['points']) or
                    any(not fan or any(not isinstance(p, int) or p < 0 or p >= len(topo.loops)
                                       for p in fan) for fan in record['members']) or
                    any(len(point) != 2 or not all(math.isfinite(v) for v in point)
                        for point in record['points'])):
                raise ValueError('Invalid shape constraint data')
            if any(not isinstance(p,int) or p<0 or p>=len(topo.loops) for p in record['edges']):
                raise ValueError('Invalid shape constraint data')
        return groups
    except (ValueError, TypeError, KeyError, IndexError, zlib.error) as exc:
        if strict:
            raise ValueError('Invalid or outdated shape constraints; remove the affected constraints first') from exc
        return []


def _groups(topo, record):
    groups = []
    for fan in record['members']:
        resolved = {topo.loop_to_vgroup[p] for p in fan}
        if len(resolved) != 1:
            raise ValueError('A shape constraint was split by a UV seam; remove it before optimizing')
        groups.append(resolved.pop())
    return groups


def _fit(topo, record, groups):
    aspect = float(record.get('aspect', 1.0))
    if not math.isfinite(aspect) or aspect <= 0:
        raise ValueError('Invalid shape constraint data')
    scale = np.array([aspect, 1.])
    template = np.asarray(record['points'], dtype=float)*scale
    current = np.asarray([tuple(topo.vgroup_uv(g)) for g in groups])*scale
    source = template-template.mean(axis=0)
    target = current-current.mean(axis=0)
    denominator = float(np.sum(source*source))
    if denominator <= 1e-20:
        raise ValueError('Invalid shape constraint data')
    u, singular, vh = np.linalg.svd(source.T@target)
    rotation = u@vh
    size = float(singular.sum())/denominator
    fitted = source@rotation*size+current.mean(axis=0)
    if not np.isfinite(fitted).all() or size <= 1e-10:
        raise ValueError('A shape constraint collapsed; restore or remove it first')
    return {g: tuple(point/scale) for g, point in zip(groups, fitted)}


def _concentric(topo, faces, fitted, circular_cycles):
    """A circular annulus shares its outer center, retaining each radius.

    Match actual UV boundary edges, so internal cycles, seam counterparts and
    separate holes never become an accidental concentric group. The relation
    follows the saved circles and current UV topology without extra mesh data.
    """
    if len(circular_cycles) < 2:
        return fitted
    from . import circle

    def edges(cycle):
        return frozenset(frozenset((a, b)) for a, b in
                         zip(cycle, cycle[1:]+cycle[:1]))

    circles = {edges(cycle): (cycle, aspect) for cycle, aspect in circular_cycles}

    def center(boundary):
        cycle, aspect = circles[edges(boundary)]
        scale = np.asarray([aspect, 1.])
        points = np.asarray([fitted[g] for g in cycle])*scale
        origin = points.mean(axis=0)
        local = points-origin
        system = np.column_stack((2*local, np.ones(len(local))))
        solution, _, rank, _ = np.linalg.lstsq(system, (local*local).sum(axis=1), rcond=None)
        if rank != 3 or not np.isfinite(solution).all():
            raise ValueError('Invalid shape constraint data')
        return (origin+solution[:2])/scale

    result = fitted
    for island in core.face_islands(topo, faces).values():
        try:
            boundaries = circle._boundary_cycles(topo, set(island))
        except ValueError:
            continue
        if (len(boundaries) != 2 or
                any(edges(cycle) not in circles for cycle in boundaries)):
            continue
        outer = max(boundaries, key=lambda cycle: abs(circle._area(
            np.asarray([tuple(topo.vgroup_uv(g)) for g in cycle]))))
        inner = next(cycle for cycle in boundaries if cycle is not outer)
        delta = center(outer)-center(inner)
        if result is fitted:
            result = dict(fitted)
        for group in inner:
            result[group] = tuple(np.asarray(fitted[group])+delta)
    return result


def fixed(topo, faces, exclude_cycles=(), circular_cycles=None):
    """Fit saved shapes in this island; a fully replaced loop may be reshaped."""
    excluded = [set(cycle) for cycle in exclude_cycles]
    result = {}
    circles = []
    for record in _plan(topo):
        groups = _groups(topo, record)
        if not any(topo.loops[p].face.index in faces for fan in record['members'] for p in fan):
            continue
        touched = set(groups)
        if any(touched <= cycle for cycle in excluded):
            continue
        if any(touched.intersection(cycle) for cycle in excluded):
            raise ValueError('Selected shapes share vertices with an existing shape constraint')
        if any(topo.loops[p].face.index not in faces for fan in record['members'] for p in fan):
            raise ValueError('Select the whole island containing the shape constraint')
        if record['kind'] == 'CIRCULAR':
            circles.append((groups, record.get('aspect', 1.)))
        for g, uv in _fit(topo, record, groups).items():
            if g in result and math.dist(result[g], uv) > 1e-6:
                raise ValueError('Shape constraints conflict at a shared vertex')
            result[g] = uv
    if circular_cycles is not None:
        circular_cycles.extend(circles)
    return _concentric(topo, faces, result, circles)


def capture(topo, faces, cycles, kind, aspect=1., options=None):
    """Return plain numeric data before any custom-layer allocation."""
    records = []
    for cycle in cycles:
        members = [[p for p in topo.vgroup_loops[g] if topo.loops[p].face.index in faces]
                   for g in cycle]
        edge_pairs = {tuple(sorted((a,b))) for a,b in zip(cycle,cycle[1:]+cycle[:1])}
        edges = [p for p,l in enumerate(topo.loops) if l.face.index in faces and
                 tuple(sorted(topo.loop_edge_vgroups(p))) in edge_pairs]
        records.append(dict(kind=kind, uv_name=topo.uvl.name, members=members, edges=edges,
                            points=[list(topo.vgroup_uv(g)) for g in cycle],
                            aspect=aspect, options=dict(options or {})))
    return records


def capture_targets(topo, faces, cycles, kind, targets, aspect=1., options=None):
    """Register a shape's target template without changing the current UVs."""
    records = capture(topo, faces, cycles, kind, aspect, options)
    for record, cycle in zip(records, cycles):
        record['points'] = [list(targets[g]) for g in cycle]
    return records


def store(obj, incoming):
    if not incoming:
        return
    bm=overlay._edit_bmesh(obj);core.prepare_bm(bm)
    topo=core.UVTopology(bm,bm.loops.layers.uv.active)
    raw=live_symmetry._raw(bm,topo.loops,LAYER)
    old=live_symmetry._unpack(raw) if raw else None
    if old and old.get('schema')!='UVCK_SHAPES_1':
        raise ValueError('Invalid shape constraint data')
    touched={p for r in incoming for fan in r['members'] for p in fan}
    uv_name=topo.uvl.name
    retained=[r for r in (old.get('groups',[]) if old else []) if r['uv_name'] != uv_name or
              not touched.intersection(p for fan in r['members'] for p in fan)]
    stamp=live_symmetry.signature(bm)
    for record in retained:record.setdefault('topology',old['topology'])
    for record in incoming:record['topology']=stamp
    plan=dict(version=2,schema='UVCK_SHAPES_1',topology=stamp,groups=retained+incoming)
    data=live_symmetry._pack(plan)
    del topo
    live_symmetry._write(bm,core.loops_in_order(bm),data,LAYER)
    obj.data[BACKUP]=data.decode('ascii')
    bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
    invalidate()


def touched_edges(topo, selected=()):
    chosen=set(selected)
    try:records=_plan(topo)
    except ValueError:
        if chosen:
            raise ValueError('Shape constraints are outdated; deselect UV edges before clearing the invalid shapes')
        # With no selection, Unconstrain already means all constraints in the
        # active UV map. Use current positions, never stale corner indices.
        return set(range(len(topo.loops)))
    return {p for r in records if not chosen or chosen.intersection(r['edges']) for p in r['edges']}


def clear(obj, selected):
    bm=overlay._edit_bmesh(obj);core.prepare_bm(bm)
    topo=core.UVTopology(bm,bm.loops.layers.uv.active)
    raw=live_symmetry._raw(bm,topo.loops,LAYER)
    if not raw:
        return
    chosen=set(selected)
    try:
        plan=live_symmetry._unpack(raw)
        all_current=chosen==set(range(len(topo.loops)))
        plan['groups']=[r for r in plan['groups'] if r['uv_name'] != topo.uvl.name or
                        (not all_current and not chosen.intersection(r['edges']))]
    except (ValueError,KeyError,TypeError,zlib.error):
        if chosen != set(range(len(topo.loops))):raise ValueError('Invalid shape constraint data')
        plan=dict(groups=[])
    data=live_symmetry._pack(plan) if plan['groups'] else b''
    del topo
    live_symmetry._write(bm,core.loops_in_order(bm),data,LAYER)
    if data:obj.data[BACKUP]=data.decode('ascii')
    else:obj.data.pop(BACKUP,None)
    bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
    invalidate()


def _prepare_pins(obj, faces):
    bm=overlay._edit_bmesh(obj)
    if bm.loops.layers.string.get(LAYER) is None:
        return None
    core.prepare_bm(bm);topo=core.UVTopology(bm,bm.loops.layers.uv.active)
    held=fixed(topo,faces)
    if not held:
        return None
    snapshot=core._loop_uv_snapshot(topo,faces)
    pins={p:bool(topo.loops[p][topo.uvl].pin_uv) for g in held for p in topo.vgroup_loops[g]}
    for g,uv in held.items():
        for p in topo.vgroup_loops[g]:
            loop=topo.loops[p]
            if loop[topo.uvl].pin_uv and math.dist(loop[topo.uvl].uv,uv)>1e-6:
                raise ValueError('Pinned UV points conflict with a shape constraint')
    core.land_uv_groups(topo,held,faces)
    for p in pins:topo.loops[p][topo.uvl].pin_uv=True
    return dict(snapshot=snapshot,pins=pins,accepted=False)


def _restore(obj, state):
    bm=overlay._edit_bmesh(obj);core.prepare_bm(bm)
    layer=bm.loops.layers.uv.active;loops=core.loops_in_order(bm)
    for p,pin in state['pins'].items():loops[p][layer].pin_uv=pin
    if not state['accepted']:
        for p,uv in state['snapshot'].items():loops[p][layer].uv=uv
    bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)


@contextmanager
def preserve(obj, faces):
    state=_prepare_pins(obj,faces)
    try:
        yield state
    finally:
        if state is not None:_restore(obj,state)


def invalidate():
    if not overlay._transform_running():_draw_cache.clear()


def visible_edges(obj,bm,uvl,sync):
    """Retain wrappers only while idle, just like the live symmetry overlay."""
    if bm.loops.layers.string.get(LAYER) is None:return []
    key=obj.data.as_pointer()
    entry=_draw_cache.get(key)
    if not overlay._transform_running():
        stamp=overlay.cache_stamp(bm,obj)
        if (not entry or entry.get('dirty') or entry['revision']!=overlay._revision or
                entry['bm']!=bm or entry['uv_name']!=uvl.name or entry.get('stamp')!=stamp):
            core.prepare_bm(bm)
            topo=SimpleNamespace(bm=bm,uvl=uvl,loops=core.loops_in_order(bm))
            records=_plan(topo,strict=False)
            items=[(p,C_CIRCULAR if r['kind']=='CIRCULAR' else C_RECTANGULAR)
                   for r in records for p in r['edges'] if 0<=p<len(topo.loops)]
            entry=dict(revision=overlay._revision,bm=bm,uv_name=uvl.name,loops=topo.loops,items=items,dirty=False,stamp=stamp)
            _draw_cache[key]=entry
    if not entry or entry['bm']!=bm or entry['uv_name']!=uvl.name:return []
    return [(entry['loops'][p],kind) for p,kind in entry['items']
            if not entry['loops'][p].face.hide and (sync or entry['loops'][p].face.select)]


@persistent
def _mesh_updated(_scene, depsgraph):
    changed={getattr(u.id,'original',u.id).as_pointer() for u in depsgraph.updates
             if isinstance(u.id,bpy.types.Mesh)}
    for key in changed:
        if key in _draw_cache:_draw_cache[key]['dirty']=True


@persistent
def _history_changed(_dummy=None):
    invalidate()
    # Mesh ID properties are not edit-mesh undo data. Keep the save fallback
    # consistent with the authoritative corner layer, including an undone first
    # shape, so leaving edit mode and saving cannot resurrect a removed shape.
    _before_save()


@persistent
def _before_save(_dummy=None):
    if overlay._transform_running():return
    for obj in getattr(bpy.context,'objects_in_mode_unique_data',()):
        if obj.type!='MESH':continue
        bm=overlay._edit_bmesh(obj)
        raw=live_symmetry._raw(bm,core.loops_in_order(bm),LAYER)
        if raw:obj.data[BACKUP]=raw.decode('ascii')
        else:obj.data.pop(BACKUP,None)


@persistent
def _after_load(_dummy=None):
    invalidate()
    for mesh in bpy.data.meshes:
        raw=mesh.get(BACKUP)
        if not raw:continue
        bm=bmesh.new()
        try:
            bm.from_mesh(mesh);core.prepare_bm(bm)
            if bm.loops.layers.string.get(LAYER) is None:
                plan=live_symmetry._unpack(raw.encode('ascii'))
                if plan and plan['topology']==live_symmetry.signature(bm):
                    live_symmetry._write(bm,core.loops_in_order(bm),raw.encode('ascii'),LAYER);bm.to_mesh(mesh)
        except (ValueError,TypeError,KeyError,RuntimeError,zlib.error):
            continue
        finally:bm.free()


def register():
    for name,fn in (('save_pre',_before_save),('load_post',_after_load),
                    ('undo_post',_history_changed),('redo_post',_history_changed),
                    ('depsgraph_update_post',_mesh_updated)):
        handlers=getattr(bpy.app.handlers,name)
        if fn not in handlers:handlers.append(fn)


def unregister():
    for name,fn in (('save_pre',_before_save),('load_post',_after_load),
                    ('undo_post',_history_changed),('redo_post',_history_changed),
                    ('depsgraph_update_post',_mesh_updated)):
        handlers=getattr(bpy.app.handlers,name)
        if fn in handlers:handlers.remove(fn)
    invalidate()
