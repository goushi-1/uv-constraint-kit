# SPDX-License-Identifier: GPL-3.0-or-later
"""One-way UV links, with undoable state and modal-safe retained BMesh data."""
import base64
import hashlib
import json
import math
import zlib

import bmesh
import bpy
from bpy.app.handlers import persistent

from . import core, overlay

LAYER = 'uvck_live_symmetry'
BACKUP = '_uvck_live_symmetry_save'
PREFIX = b'UVCK1:'
_cache = {}
_enabled = False
_reset_pending = False
_suspended = False


def _objects(context=None):
    # An import inside Blender's add-on enable operator can expose a temporary
    # Context wrapper. Do not retain it in a Python default argument.
    if context is None:
        context = bpy.context
    return [o for o in getattr(context, 'objects_in_mode_unique_data', ()) or ()
            if o.type == 'MESH' and o.mode == 'EDIT']


def signature(bm):
    """Detect same-count rewires as well as inserted/deleted corners."""
    topology = [len(bm.verts), len(bm.edges),
                [[loop.vert.index for loop in face.loops] for face in bm.faces]]
    return hashlib.sha256(json.dumps(topology, separators=(',', ':')).encode()).hexdigest()


def _pack(plan):
    return PREFIX + base64.b85encode(zlib.compress(
        json.dumps(plan, separators=(',', ':'), allow_nan=False).encode()))


def _unpack(raw):
    if not raw.startswith(PREFIX):
        return None
    plan = json.loads(zlib.decompress(base64.b85decode(raw[len(PREFIX):])))
    if plan.get('version') == 1:
        group = {name: plan[name] for name in ('uv_name', 'axis', 'axis_3d', 'pairs', 'anchors')}
        group.update(id=0, center=[.5, .5], settings={'legacy': True})
        plan = dict(version=2, topology=plan['topology'], counts=plan['counts'], groups=[group])
    if plan.get('version') != 2:
        raise ValueError('Unsupported live symmetry data')
    return plan


def _raw(bm, loops, layer_name=LAYER):
    layer = bm.loops.layers.string.get(layer_name)
    return b''.join(loop[layer] for loop in loops) if layer else b''


def _write(bm, loops, raw, layer_name=LAYER):
    # Native custom strings have a 255-byte limit. ASCII chunks are undoable
    # mesh data; no external catalog or non-undoable object property drives links.
    if len(raw) > len(loops) * 240:
        raise ValueError('Live symmetry data exceeds mesh storage capacity')
    layer = bm.loops.layers.string.get(layer_name) or bm.loops.layers.string.new(layer_name)
    # Creating a custom-data layer invalidates previously wrapped corners.
    loops = core.loops_in_order(bm)
    for i, loop in enumerate(loops):
        loop[layer] = raw[i*240:(i+1)*240]


def install(obj, capture):
    """Normalize exact corner pairs and linked fans before storing plain data."""
    bm = overlay._edit_bmesh(obj)
    core.prepare_bm(bm)
    uv = bm.loops.layers.uv.active
    loops = core.loops_in_order(bm)
    primary = {target: (source, pivot) for source, target, _, pivot in capture['pairs']}
    if len(primary) != len(capture['pairs']):
        raise ValueError('Live symmetry has ambiguous target corners')
    sources = {source for source, _, _, _ in capture['pairs']}
    if sources.intersection(primary):
        raise ValueError('Live symmetry has cyclic corner links')
    anchors = {}
    extras = {}
    before = capture['before']
    for source, target, fan, pivot in capture['pairs']:
        if (not capture['unlink'] and loops[source].vert == loops[target].vert and
                core._uv_close(before[source], before[target]) and source in fan):
            anchors[source] = pivot
        for position in fan:
            if position in primary or position in sources:
                continue
            chosen = source
            previous = extras.get(position)
            if previous is not None:
                other, old_pivot = previous
                if (loops[source].vert != loops[other].vert or
                        not core._uv_close(before[source], before[other]) or
                        abs(pivot-old_pivot) > 1e-7):
                    raise ValueError('Live symmetry has ambiguous linked corners')
                chosen = min(source, other)
            extras[position] = chosen, pivot
    pairs = [[source, target, pivot] for target, (source, pivot) in sorted(primary.items())]
    pairs.extend([source, target, pivot] for target, (source, pivot) in sorted(extras.items()))
    stamp = signature(bm)
    previous = _unpack(capture.get('previous_live', b''))
    old_groups = previous['groups'] if previous and previous['topology'] == stamp else []
    touched = {p for s, target, _ in pairs for p in (s, target)}
    retained = [g for g in old_groups if g['uv_name'] != uv.name or not
                touched.intersection(p for s, target, _ in g['pairs'] for p in (s, target))]
    replaced = [g for g in old_groups if g not in retained]
    next_id = max((g['id'] for g in old_groups), default=-1) + 1
    roots = capture.get('island_roots', {})
    grouped = {}
    for source, target, pivot in pairs:
        key = (roots.get(source, source), roots.get(target, target))
        grouped.setdefault(key, []).append([source, target, pivot])
    # Legacy callers do not capture island roots; preserve their single group.
    if not roots:
        grouped = {0: pairs}
    incoming = []
    for index, group_pairs in enumerate(grouped.values()):
        group_sources = {s for s, _, _ in group_pairs}
        group_id = replaced[index]['id'] if index < len(replaced) else next_id
        if index >= len(replaced):
            next_id += 1
        incoming.append(dict(id=group_id, uv_name=uv.name,
            axis=capture['axis'], axis_3d=capture['axis_3d'],
            center=list(capture.get('center', (.5, .5))),
            settings=dict(capture.get('settings', {'legacy': True})), pairs=group_pairs,
            anchors=[[s, p] for s, p in sorted(anchors.items()) if s in group_sources]))
    plan = dict(version=2, topology=stamp,
                counts=[len(bm.verts), len(bm.edges), len(bm.faces), len(loops)],
                groups=retained + incoming)
    _validate(plan, len(loops))
    _cache.pop(obj.data.as_pointer(), None)
    _write(bm, loops, _pack(plan))
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    sync()


def clear(obj):
    bm = overlay._edit_bmesh(obj)
    loops = core.loops_in_order(bm)
    if bm.loops.layers.string.get(LAYER):
        _write(bm, loops, b'')
    _cache.pop(obj.data.as_pointer(), None)
    obj.data.pop(BACKUP, None)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)


def _validate(plan, count):
    assert plan['version'] == 2 and isinstance(plan['groups'], list)
    names = {g['uv_name'] for g in plan['groups']}
    for group in plan['groups']:
        assert group['axis'] in (0, 1)
        assert all(isinstance(s, int) and isinstance(t, int) and
                   0 <= s < count and 0 <= t < count and math.isfinite(p)
                   for s, t, p in group['pairs'])
        assert all(0 <= s < count and math.isfinite(p) for s, p in group['anchors'])
        assert all(math.isfinite(p) for p in group['center'])
    for name in names:
        groups = [g for g in plan['groups'] if g['uv_name'] == name]
        sources = {s for g in groups for s, _, _ in g['pairs']}
        targets = [t for g in groups for _, t, _ in g['pairs']]
        if len(targets) != len(set(targets)):
            raise ValueError('Live symmetry has ambiguous target corners')
        if sources.intersection(targets):
            raise ValueError('Live symmetry has cyclic corner links')


def clear_selected_targets(context, obj, positions=None, *, bake=False):
    """Remove touched active-layer groups; without a selection remove that layer's links."""
    entry = _prepare(obj)
    if not entry or not entry['active']:
        return False
    uv_name = entry['uv'].name
    if positions is None:
        sync = context.scene.tool_settings.use_uv_select_sync
        positions = {p for p, loop in enumerate(entry['loops']) if not loop.face.hide and
            (loop.face.select if sync and not entry['bm'].uv_select_sync_valid else
             (sync or loop.face.select) and (loop.uv_select_vert or loop.uv_select_edge or loop.face.uv_select))}
    kept = [g for g in entry['plan']['groups'] if g['uv_name'] != uv_name or
            (positions and not positions.intersection(p for s, target, _ in g['pairs'] for p in (s, target)))]
    if len(kept) == len(entry['plan']['groups']):
        return False
    removed = [g for g in entry['active_groups'] if g not in kept]
    if bake:
        _apply(entry)
        mirror_constraints(obj, removed)
    plan = dict(entry['plan'], groups=kept)
    # Constraint allocation may invalidate the cached corner wrappers.
    _write(entry['bm'], core.loops_in_order(entry['bm']), _pack(plan) if kept else b'')
    _cache.pop(obj.data.as_pointer(), None)
    obj.data.pop(BACKUP, None)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return True


def source_faces(obj, faces):
    """Solve independent/source faces; linked target faces only receive UVs."""
    faces = set(faces)
    entry = _prepare(obj)
    if not entry or not entry['active']:
        return faces
    loops = entry['loops']
    pairs = [pair for g in entry['active_groups'] for pair in g['pairs']]
    sources = {s for s, _, _ in pairs}
    targets = {t for _, t, _ in pairs}
    driven = set()
    positions = {loop:p for p,loop in enumerate(loops)}
    for face in entry['bm'].faces:
        if all(positions[l] in targets and positions[l] not in sources for l in face.loops):
            driven.add(face.index)
    requested = faces & driven
    redirected = {loops[s].face.index for s,t,_ in pairs if loops[t].face.index in requested}
    return (faces-driven) | (redirected-driven)


def _mirrored_edges(loops, pairs):
    """Map outgoing UV edges using both endpoints, including reversed winding."""
    positions = {loop:p for p,loop in enumerate(loops)}
    mapped = {(s, loops[t].face.index):t for s,t,_ in pairs}
    result = {}
    for source,target,_ in pairs:
        other = mapped.get((positions[loops[source].link_loop_next], loops[target].face.index))
        if other is None:
            continue
        if positions[loops[target].link_loop_next] == other:
            result[target] = source
        elif positions[loops[other].link_loop_next] == target:
            result[other] = source
    return result


def mirror_constraints(obj, groups):
    """Bake source direction and shape constraints onto the mirrored UV side.

    Called by direct symmetry or detach, never by the modal/live timer. A
    driven side is not an independent set of solver constraints while linked.
    """
    from . import shape_constraints as shapes
    groups = list(groups)
    if not groups:
        return False
    bm = overlay._edit_bmesh(obj)
    core.prepare_bm(bm)
    loops = core.loops_in_order(bm)
    uv = bm.loops.layers.uv.active
    groups = [g for g in groups if g['uv_name'] == uv.name]
    if not groups:
        return False
    layer = core.constraint_layer(bm)
    before_marks = [loop[layer] for loop in loops] if layer else [0]*len(loops)
    marks = {}
    mappings = []
    for group in groups:
        edges = _mirrored_edges(loops, group['pairs'])
        marks.update({target:before_marks[source] for target,source in edges.items()})
        mappings.append((group,edges))
    before_raw = _raw(bm, loops, shapes.LAYER)
    after_raw = before_raw
    if before_raw:
        # Validate the active map before changing any data.
        topo = core.UVTopology(bm, uv)
        records = shapes._plan(topo)
        del topo
        plan = _unpack(before_raw)
        targets = {t for group,_ in mappings for _,t,_ in group['pairs']}
        target_edges = set(marks)
        retained = [r for r in plan['groups'] if r['uv_name'] != uv.name or not (
            {p for fan in r['members'] for p in fan} <= targets and set(r['edges']) <= target_edges)]
        incoming = []
        for group,edges in mappings:
            mapped = {}
            for source,target,pivot in group['pairs']:
                mapped.setdefault(source, []).append((target,pivot))
            for record in records:
                members = [sorted({t for p in fan for t,_ in mapped.get(p,())}) for fan in record['members']]
                if not all(members) or not set(record['edges']) <= set(edges.values()):
                    continue
                pivots = {pivot for fan in record['members'] for p in fan for _,pivot in mapped.get(p,())}
                if len(pivots) != 1:
                    raise ValueError('A shape constraint spans different symmetry centers')
                pivot = pivots.pop()
                points = [list(point) for point in record['points']]
                for point in points:
                    point[group['axis']] = 2*pivot-point[group['axis']]
                incoming.append(dict(record, members=members, points=points,
                    edges=sorted(t for t,s in edges.items() if s in record['edges'])))
        plan = dict(plan, groups=retained+incoming)
        after_raw = _pack(plan) if plan['groups'] else b''
        if len(after_raw) > len(loops)*240:
            raise ValueError('Shape constraint data exceeds mesh storage capacity')
    marks_changed = any(before_marks[p] != kind for p,kind in marks.items())
    shapes_changed = before_raw != after_raw
    if not marks_changed and not shapes_changed:
        return False
    backup = obj.data.get(shapes.BACKUP)
    try:
        if marks_changed:
            core.constraint_layer(bm, create=True)
            loops = core.loops_in_order(bm)
            layer = core.constraint_layer(bm)
            for p,kind in marks.items():
                loops[p][layer] = kind
        if shapes_changed:
            _write(bm, core.loops_in_order(bm), after_raw, shapes.LAYER)
            if after_raw:obj.data[shapes.BACKUP] = after_raw.decode('ascii')
            else:obj.data.pop(shapes.BACKUP,None)
    except Exception:
        loops = core.loops_in_order(bm)
        layer = core.constraint_layer(bm)
        if layer:
            for p,kind in enumerate(before_marks):loops[p][layer] = kind
        if shapes_changed:
            _write(bm, loops, before_raw, shapes.LAYER)
        if backup is None:obj.data.pop(shapes.BACKUP,None)
        else:obj.data[shapes.BACKUP] = backup
        raise
    core.store_constraints(obj,bm)
    core.invalidate_stats_cache()
    shapes.invalidate()
    bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
    return True


def has_links(context):
    for obj in _objects(context):
        entry = (_cache.get(obj.data.as_pointer()) if overlay._transform_running() else _prepare(obj,cached=True))
        if entry and entry['active']:
            return True
    return False


def _prepare(obj,*,cached=False):
    bm = overlay._edit_bmesh(obj)
    if bm is None:
        return None
    key = obj.data.as_pointer()
    layer = bm.loops.layers.string.get(LAYER)
    first = next(iter(bm.faces), None)
    if not layer or first is None or not first.loops[0][layer].startswith(PREFIX):
        _cache.pop(key, None)
        return None
    entry=_cache.get(key)
    token=overlay.cache_stamp(bm,obj)
    if (cached and entry and entry['bm']==bm and entry.get('cache_stamp')==token
            and entry['loops'] and entry['loops'][0].is_valid):
        return entry
    core.prepare_bm(bm)
    loops = core.loops_in_order(bm)
    raw = _raw(bm, loops)
    entry = _cache.get(key)
    # Revalidate structure when idle, including same-count edge rewires.
    stamp = signature(bm) if raw else None
    uv = bm.loops.layers.uv.active
    if (entry is None or entry['bm'] != bm or entry['raw'] != raw or
            entry['stamp'] != stamp or entry.get('uv_name') != (uv.name if uv else None)):
        try:
            plan = _unpack(raw)
            if plan:
                n = len(loops)
                _validate(plan, n)
        except (ValueError, KeyError, TypeError, AssertionError, zlib.error):
            plan = None
        groups = [g for g in plan['groups'] if uv and g['uv_name'] == uv.name] if plan else []
        active = bool(groups and plan['topology'] == stamp)
        entry = dict(obj=obj, bm=bm, loops=loops, uv=uv, raw=raw, stamp=stamp,
                     uv_name=uv.name if uv else None, plan=plan, active=active,
                     active_groups=groups, dirty=False, target_triangles=[])
        if active:
            targets = {t for g in groups for _, t, _ in g['pairs']}
            sources = {s for g in groups for s, _, _ in g['pairs']}
            positions = {loop: p for p, loop in enumerate(loops)}
            faces = {face for face in bm.faces if all(positions[l] in targets and
                     positions[l] not in sources for l in face.loops)}
            entry['target_triangles'] = [tuple(positions[l] for l in tri)
                for tri in bm.calc_loop_triangles() if tri[0].face in faces]
        _cache[key] = entry
    # Other edit operators can add custom-data layers without changing topology
    # or the active UV name. Such allocation invalidates corner wrappers too.
    # Refresh them while idle, never during a native modal transform.
    entry['loops'] = loops
    entry['uv'] = uv
    entry['cache_stamp']=token
    return entry


def _apply(entry):
    loops, uv = entry['loops'], entry['uv']
    groups = entry['active_groups']
    values = {s: loops[s][uv].uv.copy() for g in groups for s, _, _ in g['pairs']}
    changed = False
    for group in groups:
        axis = group['axis']
        for source, pivot in group['anchors']:
            value = values[source]
            value[axis] = pivot
            if loops[source][uv].uv != value:
                loops[source][uv].uv = value
                changed = True
        # Snapshot all sources before any writes; targets never drive a source.
        for source, target, pivot in group['pairs']:
            value = values[source].copy()
            value[axis] = 2*pivot-value[axis]
            if loops[target][uv].uv != value:
                loops[target][uv].uv = value
                changed = True
    entry['dirty'] |= changed
    return changed


def sync(context=None,*,cached=False):
    """Apply links synchronously inside our operators' native undo step."""
    if _suspended:
        return 0
    if context is None:
        context = bpy.context
    modal = overlay._transform_running()
    if modal:
        entries = list(_cache.values())
    else:
        entries = [_prepare(obj,cached=cached) for obj in _objects(context)]
    changed = 0
    for entry in entries:
        if not entry or not entry['active'] or not entry['bm'].is_valid:
            continue
        obj = entry['obj']
        if obj.mode != 'EDIT':
            continue
        bm = entry['bm']
        if [len(bm.verts), len(bm.edges), len(bm.faces), len(entry['loops'])] != entry['plan']['counts']:
            continue
        if _apply(entry):
            changed += 1
        if not modal and entry['dirty']:
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
            entry['dirty'] = False
    if changed:
        overlay.request_redraw()
    return changed


def _tick():
    global _reset_pending
    if not _enabled:
        return None
    if _suspended:
        return .1
    modal = overlay._transform_running()
    if _reset_pending and not modal:
        _cache.clear()
        _reset_pending = False
    try:
        if not modal:
            current = {o.data.as_pointer() for o in _objects()}
            for key in list(_cache):
                if key not in current:
                    del _cache[key]
        sync(cached=True)
    except (ReferenceError, RuntimeError):
        # Undo/load can replace the mesh. Never drop wrappers during a gesture.
        _reset_pending = True
    # Fast tracking during gestures; avoid scanning a static linked mesh at
    # the same rate when no native transform is running.
    return .03 if modal else .1


@persistent
def _before_history(_dummy=None):
    global _suspended
    _suspended = True


@persistent
def _after_history(_dummy=None):
    global _suspended, _reset_pending
    _suspended = False
    _reset_pending = True


@persistent
def _before_save(_dummy=None):
    if overlay._transform_running():
        return
    sync()
    # Edit-mode saves can omit live custom-data layers from the Mesh datablock.
    for obj in _objects():
        entry = _prepare(obj)
        if entry and entry['raw']:
            obj.data[BACKUP] = entry['raw'].decode('ascii')
        else:
            obj.data.pop(BACKUP, None)


@persistent
def _after_load(_dummy=None):
    _after_history()
    for mesh in bpy.data.meshes:
        raw = mesh.get(BACKUP)
        if not raw:
            continue
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            core.prepare_bm(bm)
            loops = core.loops_in_order(bm)
            # Existing mesh state is authoritative, including an undone clear.
            if bm.loops.layers.string.get(LAYER) is None:
                plan = _unpack(raw.encode('ascii'))
                if plan and plan['topology'] == signature(bm):
                    _write(bm, loops, raw.encode('ascii'))
                    bm.to_mesh(mesh)
        except (ValueError, RuntimeError, zlib.error):
            pass
        finally:
            bm.free()


def register():
    global _enabled
    _enabled = True
    for name, function in (('undo_pre', _before_history), ('redo_pre', _before_history),
                           ('load_pre', _before_history), ('undo_post', _after_history),
                           ('redo_post', _after_history), ('load_post', _after_load),
                           ('save_pre', _before_save)):
        collection = getattr(bpy.app.handlers, name)
        if function not in collection:
            collection.append(function)
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=.03, persistent=True)


def unregister():
    global _enabled, _reset_pending, _suspended
    _enabled = False
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    for name, function in (('undo_pre', _before_history), ('redo_pre', _before_history),
                           ('load_pre', _before_history), ('undo_post', _after_history),
                           ('redo_post', _after_history), ('load_post', _after_load),
                           ('save_pre', _before_save)):
        collection = getattr(bpy.app.handlers, name)
        if function in collection:
            collection.remove(function)
    _suspended = False
    if overlay._transform_running():
        _reset_pending = True
        bpy.app.timers.register(_release_when_idle, first_interval=.1)
    else:
        _cache.clear()


def _release_when_idle():
    if overlay._transform_running():
        return .1
    _cache.clear()
    return None
