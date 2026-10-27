# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic checked-solve snapshot and rollback regression, factory Blender only."""
import argparse
import hashlib
import importlib
import json
import math
from pathlib import Path
import sys
from types import ModuleType

import bmesh
import bpy
import numpy as np

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--addon-root', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--require-native', action='store_true')
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
root, output = args.addon_root.resolve(), args.output.resolve()
assert '--background' in sys.argv and '--factory-startup' in sys.argv and '--disable-autoexec' in sys.argv
assert not bpy.data.filepath and not output.is_relative_to(root)
bpy.context.preferences.use_preferences_save = False
bpy.context.preferences.filepaths.use_auto_save_temporary_files = False
package = ModuleType('uvck_transaction_public')
package.__path__, package.__file__ = [str(root)], str(root / '__init__.py')
sys.modules[package.__name__] = package
core = importlib.import_module(package.__name__ + '.core')
metric = importlib.import_module(package.__name__ + '.metric')
safety = importlib.import_module(package.__name__ + '.safety')
core.native_engine._required = args.require_native
if args.require_native:
    assert core.native_engine._load() is not None
rows = []


def identity():
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}


def fixture():
    if bpy.context.object and bpy.context.object.mode == 'EDIT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for obj in list(bpy.data.objects):
        if obj.name.startswith('TransactionSnapshot_'):
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    nx, ny = 4, 3
    vertices = [(float(x), float(y), 0.) for y in range(ny + 1) for x in range(nx + 1)]
    index = lambda x, y: y * (nx + 1) + x
    faces = [(index(x, y), index(x + 1, y), index(x + 1, y + 1), index(x, y + 1))
             for y in range(ny) for x in range(nx)]
    target = set(range(len(faces)))
    first = len(vertices)
    vertices += [(10., 0., 0.), (11., 0., 0.), (11., 1., 0.), (10., 1., 0.)]
    faces.append(tuple(range(first, first + 4)))
    mesh = bpy.data.meshes.new('TransactionSnapshot_mesh')
    mesh.from_pydata(vertices, [], faces)
    mesh.uv_layers.new(name='UVMap')
    obj = bpy.data.objects.new('TransactionSnapshot_object', mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(mesh)
    core.prepare_bm(bm)
    mark = core.constraint_layer(bm, create=True)
    topo = core.UVTopology(bm, core.uv_layer(bm))
    positions = []
    for face in bm.faces:
        face.select, face.uv_select = True, False
    for p, loop in enumerate(topo.loops):
        x, y, _ = loop.vert.co
        loop[topo.uvl].uv = (.1 * x, .1 * y + (.03 * math.sin(math.pi * x / nx) * (1 - y / ny) if loop.face.index in target else 0.))
        loop[topo.uvl].pin_uv = False
        loop.uv_select_vert = loop.uv_select_edge = False
        loop[mark] = core.C_NONE
        if loop.face.index in target and y == 0 and loop.link_loop_next.vert.co.y == 0:
            positions.append(p)
            loop[mark] = core.C_HORIZONTAL
    ts = bpy.context.scene.tool_settings
    ts.use_uv_select_sync, ts.uv_select_mode = False, 'EDGE'
    ts.mesh_select_mode, ts.use_edge_path_live_unwrap = (False, True, False), False
    bm.uv_select_sync_valid = True
    outside = [p for p, loop in enumerate(topo.loops) if loop.face.index not in target]
    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    return obj, target, positions, outside


def state(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    layer, mark = core.uv_layer(bm), core.constraint_layer(bm)
    loops = core.loops_in_order(bm)
    ts = bpy.context.scene.tool_settings
    return dict(uv=[tuple(loop[layer].uv) for loop in loops], pins=[bool(loop[layer].pin_uv) for loop in loops],
                marks=[int(loop[mark]) for loop in loops], uv_selection=[(bool(loop.uv_select_vert), bool(loop.uv_select_edge)) for loop in loops],
                faces=[(bool(f.select), bool(f.uv_select), bool(f.hide)) for f in bm.faces],
                xyz=[tuple(v.co) for v in bm.verts], topology=[tuple(v.index for v in f.verts) for f in bm.faces],
                settings=(bool(ts.use_uv_select_sync), ts.uv_select_mode, tuple(ts.mesh_select_mode),
                          bool(ts.use_edge_path_live_unwrap), bool(bm.uv_select_sync_valid)))


def solve(obj, faces, marks, **settings):
    return core.rebuild_islands(bpy.context, obj, faces, marks, _report_quality=False,
                               respect_uv_selection=False, **settings)


class InjectedFailure(RuntimeError):
    pass


before_source = identity()
for native_reader in (True, False):
    core.mesh_reader._enabled = native_reader
    core.mesh_reader._required = native_reader and args.require_native
    if native_reader and core.mesh_reader._load() is None:
        assert not args.require_native
        rows.append(dict(case='native_reader', skipped=True, reason='optional native reader unavailable'))
        continue
    original_measure = safety.AcceptanceProbe.measure
    calls = []

    def checked_measure(self, topo, **kwargs):
        if kwargs.get('_triangles') is None:
            return original_measure(self, topo, **kwargs)
        shared = kwargs['_triangles']
        reference = np.asarray([kwargs['coordinates'][pos] for _, pos, _ in self.target])
        assert shared.dtype == reference.dtype and shared.tobytes() == reference.tobytes()
        old = original_measure(self, topo, **{k: v for k, v in kwargs.items() if k != '_triangles'})
        new = original_measure(self, topo, **kwargs)
        assert old == new
        calls.append(True)
        return new

    safety.AcceptanceProbe.measure = checked_measure
    try:
        for settings in ({}, {'prevent_flips': True}, {'mix': .5}):
            obj, faces, marks, outside = fixture()
            before = state(obj)
            result = solve(obj, faces, marks, **settings)
            after = state(obj)
            rejected = bool(result[2].get('rejected'))
            if rejected:
                # A partial blend can miss the angular guard even when its full
                # step is feasible. The existing safe rejection must restore all
                # state; the test does not require future solvers to reject it.
                assert settings.get('mix') == .5 and result[2]['reason'] == 'constraint_residual'
                assert after == before
            assert all(before['uv'][p] == after['uv'][p] for p in outside)
            for key in before.keys() - {'uv'}:
                assert before[key] == after[key], key
            json.dumps(result, allow_nan=False)  # No retained arrays/wrappers in reports.
            rows.append(dict(case='shared_health_actual_solve', native_reader=native_reader, parameters=settings,
                             solver_rejected=rejected, reason=result[2].get('reason'), passed=True))
    finally:
        safety.AcceptanceProbe.measure = original_measure
    assert calls, 'The shared numeric health path was never reached'

    obj, faces, marks, outside = fixture()
    bm = bmesh.from_edit_mesh(obj.data)
    topo = core.UVTopology(bm, core.uv_layer(bm))
    saved = np.asarray(topo.initial_uv, dtype=float)
    saved_bytes = saved.tobytes()
    topo.loops[0][topo.uvl].uv.x += .125
    topo.refresh_uv()
    assert saved.tobytes() == saved_bytes and not np.array_equal(saved, np.asarray(topo.initial_uv))
    if native_reader:
        assert not saved.flags.writeable
    del bm, topo, saved
    rows.append(dict(case='snapshot_independent_after_live_write', native_reader=native_reader, passed=True))

    for fault in ('outside_write', 'shared_health_exception'):
        obj, faces, marks, outside = fixture()
        before = state(obj)
        original_solve, original_measure = metric.solve, safety.AcceptanceProbe.measure
        reached = []

        def corrupted(module, topo, faces, *positional, **keywords):
            result = original_solve(module, topo, faces, *positional, **keywords)
            reached.append(True)
            if fault == 'outside_write':
                topo.loops[outside[0]][topo.uvl].uv.x += .125
            return result

        def failed_health(self, topo, **kwargs):
            if fault == 'shared_health_exception' and kwargs.get('_triangles') is not None and state(obj)['uv'] != before['uv']:
                assert reached
                raise InjectedFailure('shared health after UV write')
            return original_measure(self, topo, **kwargs)

        metric.solve, safety.AcceptanceProbe.measure = corrupted, failed_health
        try:
            try:
                result = solve(obj, faces, marks)
            except InjectedFailure:
                assert fault == 'shared_health_exception'
            else:
                assert fault == 'outside_write' and result[2].get('rejected') and result[2]['reason'] == 'outside_selection_moved', result
        finally:
            metric.solve, safety.AcceptanceProbe.measure = original_solve, original_measure
        assert reached and state(obj) == before
        rows.append(dict(case=fault, native_reader=native_reader, full_state_restored=True, passed=True))
assert identity() == before_source
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(dict(passed=True, cases=rows, source_sha256=before_source,
                                  scene_saved=False, preferences_saved=False), indent=2) + '\n', encoding='utf8')
print(json.dumps(dict(passed=True, cases=len(rows))), flush=True)
