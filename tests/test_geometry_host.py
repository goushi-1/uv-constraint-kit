# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic CPU geometry-reuse integration checks in a disposable factory GUI.

No add-on registration, user-scene load, scene save, or preference save. Each
group uses independent synthetic edit meshes. Mocked solvers are not used.
"""
import argparse
from contextlib import contextmanager
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tomllib
from types import ModuleType
import traceback

import bmesh
import bpy
import numpy as np


HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--addon-root', type=Path, default=HERE.parent / 'uv_constraint_kit')
parser.add_argument('--output', type=Path, default=HERE.parent / 'test-results/geometry.json')
ARGS = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
ROOTS = {'candidate': ARGS.addon_root.resolve()}
OUT = ARGS.output.resolve()
assert not OUT.is_relative_to(ARGS.addon_root.resolve()), 'Write reports outside the add-on tree'
REPORT = dict(status='running', scene_saved=False, preferences_saved=False,
              registered=False, user_install_changed=False, variants={}, comparisons=[])


def persist():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf8')


def identity(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}


def plain(value):
    if isinstance(value, np.ndarray):
        return dict(shape=list(value.shape), dtype=value.dtype.str,
                    sha256=hashlib.sha256(np.ascontiguousarray(value).tobytes()).hexdigest())
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def digest(value):
    return hashlib.sha256(json.dumps(plain(value), sort_keys=True, separators=(',', ':'),
                                   allow_nan=False).encode()).hexdigest()


def load_core(label):
    name = 'uvck_integration_' + label
    package = ModuleType(name)
    package.__path__ = [str(ROOTS[label])]
    package.__file__ = str(ROOTS[label] / '__init__.py')
    sys.modules[name] = package
    core = importlib.import_module(name + '.core')
    metric = importlib.import_module(name + '.metric')
    core.native_engine._enabled = core.native_engine._required = True
    core.native_engine._GPU_MIN_TRIANGLES = 2 ** 63 - 1
    assert core.native_engine._load() is not None
    return core, metric


def clean_scene():
    if bpy.context.object and bpy.context.object.mode == 'EDIT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for obj in list(bpy.context.scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


def fixture(core, kind):
    clean_scene()
    xyz, faces, xy = [], [], []
    if kind == 'ring':
        xy = [(0., 0.), (3., 0.), (3., 3.), (0., 3.), (1., 1.), (2., 1.), (2., 2.), (1., 2.)]
        xyz = [(x, y, 0.) for x, y in xy]
        faces = [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    elif kind == 'stack':
        xy = [(0., 0.), (1., .12), (1.1, 1.), (-.05, 1.)] * 2
        xyz = [(float(x + 3 * chart), float(y), 0.)
               for chart in range(2) for x, y in ((0, 0), (1, 0), (1, 1), (0, 1))]
        faces = [(0, 1, 2, 3), (4, 5, 6, 7)]
    else:
        for chart in range(2 if kind == 'multi' else 1):
            start = len(xyz)
            xyz.extend((float(x + 4 * chart), float(y), 0.) for y in range(3) for x in range(3))
            for y in range(3):
                for x in range(3):
                    u, v = (1.18, .87) if (x, y) == (1, 1) else (float(x), float(y))
                    xy.append((u + 4 * chart, v + (.11 * u if kind in {'multi', 'conflict'} else 0.)))
            faces.extend(tuple(start + i for i in face)
                         for face in ((0, 1, 4, 3), (1, 2, 5, 4), (3, 4, 7, 6), (4, 5, 8, 7)))
    target = set(range(len(faces)))
    outside = len(xyz)
    xyz.extend(((12., 0., 0.), (13., 0., 0.), (13., 1., 0.), (12., 1., 0.)))
    xy.extend(((12., 0.), (13., 0.), (13., 1.), (12., 1.)))
    faces.append(tuple(range(outside, outside + 4)))
    mesh = bpy.data.meshes.new('IntegrationMesh')
    mesh.from_pydata(xyz, [], faces)
    mesh.uv_layers.new(name='UVMap')
    obj = bpy.data.objects.new('IntegrationObject', mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(mesh)
    core.prepare_bm(bm)
    layer = core.constraint_layer(bm, create=True)
    uvl = core.uv_layer(bm)
    for face in bm.faces:
        face.select = True
        face.uv_select = False
        for loop in face.loops:
            loop[uvl].uv = xy[loop.vert.index]
            loop[uvl].pin_uv = face.index not in target
            loop.uv_select_vert = loop.vert.index % 2 == 0
            loop.uv_select_edge = False
            ids = {loop.vert.index, loop.link_loop_next.vert.index}
            mark = ((kind in {'multi', 'fixed', 'conflict'} and ids == {0, 1})
                    or (kind == 'multi' and ids == {9, 10})
                    or (kind == 'stack' and ids in ({0, 1}, {4, 5})))
            loop[layer] = core.C_HORIZONTAL if mark else core.C_NONE
    bm.uv_select_sync_valid = True
    settings = bpy.context.scene.tool_settings
    settings.use_uv_select_sync = False
    settings.uv_select_mode = 'EDGE'
    settings.mesh_select_mode = (False, True, False)
    settings.use_edge_path_live_unwrap = False
    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    return obj, target


def snapshot(core, obj):
    """Return numbers only; no BMesh objects survive this helper call."""
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    uvl, layer = core.uv_layer(bm), core.constraint_layer(bm)
    loops = core.loops_in_order(bm)
    positions = {loop: p for p, loop in enumerate(loops)}
    triangles = [(tri[0].face.index, tuple(positions[l] for l in tri)) for tri in bm.calc_loop_triangles()]
    return dict(uv=np.asarray([tuple(l[uvl].uv) for l in loops], dtype=np.float64),
        pins=np.asarray([l[uvl].pin_uv for l in loops], dtype=np.uint8),
        marks=np.asarray([l[layer] if layer is not None else 0 for l in loops], dtype=np.int64),
        loop_faces=np.asarray([l.face.index for l in loops], dtype=np.int64),
        xyz=np.asarray([tuple(v.co) for v in bm.verts], dtype=np.float64),
        topology=([tuple(l.vert.index for l in f.loops) for f in bm.faces],
                  [tuple(v.index for v in e.verts) for e in bm.edges]),
        flags=([[v.select, v.hide] for v in bm.verts], [[e.select, e.hide, e.seam] for e in bm.edges],
               [[f.select, f.hide, f.uv_select] for f in bm.faces],
               [[l.uv_select_vert, l.uv_select_edge] for l in loops], bool(bm.uv_select_sync_valid)),
        triangles=triangles,
        settings=(bpy.context.scene.tool_settings.use_uv_select_sync,
                  bpy.context.scene.tool_settings.uv_select_mode,
                  tuple(bpy.context.scene.tool_settings.mesh_select_mode),
                  bpy.context.scene.tool_settings.use_edge_path_live_unwrap))


def invariant(before, after, target, exact_uv=False):
    for key in ('pins', 'marks', 'loop_faces', 'xyz'):
        assert np.array_equal(before[key], after[key]), ('changed', key)
    for key in ('topology', 'flags', 'triangles', 'settings'):
        assert before[key] == after[key], ('changed', key)
    fixed = before['pins'].astype(bool) | ~np.isin(before['loop_faces'], list(target))
    assert np.array_equal(before['uv'][fixed], after['uv'][fixed]), 'Pinned/outside UV moved'
    assert np.isfinite(after['uv']).all(), 'Nonfinite UV'
    if exact_uv:
        assert np.array_equal(before['uv'], after['uv']), 'Rejected fit did not restore exact UV'


def no_new_flips(before, after, target):
    for fi, positions in before['triangles']:
        if fi not in target:
            continue
        old, new = before['uv'][list(positions)], after['uv'][list(positions)]
        a, b = old[1:] - old[:1]
        c, d = new[1:] - new[:1]
        old_cross = float(a[0] * b[1] - a[1] * b[0])
        new_cross = float(c[0] * d[1] - c[1] * d[0])
        if abs(old_cross) > 2e-14:
            assert old_cross * new_cross > 0 and abs(new_cross) > 2e-14, ('New flip/degeneration', fi)


def marks(core, obj, target):
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    return core.constrained_loops(bm, only_faces=target)


@contextmanager
def trace(core, metric):
    """Keep only numeric hashes; do not wrap a rebuild holding its topo args."""
    old_prepare, old_solve = metric._prepare_geometry, core.native_engine.solve
    rows = dict(preparations=[], native=[])

    def prepare(*args, **kwargs):
        result = old_prepare(*args, **kwargs)
        rows['preparations'].append(dict(faces=sorted(args[2]), none=result is None))
        return result

    def solve(*args, **kwargs):
        input_hash = digest([args, kwargs])
        result = old_solve(*args, **kwargs)
        diag = core.native_engine.diagnostics()
        assert result is not None and diag['gpu']['last_backend'] == 'CPU', diag
        rows['native'].append(dict(input_sha256=input_hash, status=result[0],
                                   backend='CPU', statistics=dict(diag['last_solve'])))
        return result

    metric._prepare_geometry, core.native_engine.solve = prepare, solve
    try:
        yield rows
    finally:
        metric._prepare_geometry, core.native_engine.solve = old_prepare, old_solve


def multi_case(label, core, metric):
    obj, target = fixture(core, 'multi')
    positions = marks(core, obj, target)
    bm = bmesh.from_edit_mesh(obj.data)
    topo = core.UVTopology(bm, core.uv_layer(bm))
    expected_keys = {frozenset(range(4)), frozenset(range(4, 8))}
    assert {frozenset(x) for x in core.face_islands(topo, target).values()} == expected_keys
    cache, stages = {}, []
    with trace(core, metric) as events:
        for strength in (1., 100., 1e4, 1e6):
            before = snapshot(core, obj)
            start = len(events['native'])
            result = core._rebuild_islands(bpy.context, obj, target, positions,
                _topo=topo, _constraint_strength=strength, _quality_before={}, _quality_after=False,
                auto_iterations=False, optimize_iterations=3, prevent_flips=True,
                respect_uv_selection=False, **({'_geometry_cache': cache} if label == 'candidate' else {}))
            after = snapshot(core, obj)
            invariant(before, after, target)
            no_new_flips(before, after, target)
            if label == 'candidate':
                assert set(cache) == expected_keys
                for key, entry in cache.items():
                    assert entry[0] is topo and entry[3]['faces'] == key
            stages.append(dict(strength=strength, result=plain(result), output=plain(after),
                               native=events['native'][start:]))
        expected_prepares = 2 if label == 'candidate' else 8
        assert len(events['preparations']) == expected_prepares
        preparations = list(events['preparations'])
    entry = None
    cache.clear()
    del topo, bm
    # The explicit strength sequence above exercises real internal solves.
    # Also run the public checked core transaction on both islands together.
    before = snapshot(core, obj)
    with trace(core, metric) as checked_events:
        checked = core.rebuild_islands(bpy.context, obj, target, positions,
            prevent_flips=True, respect_uv_selection=False, _report_quality=False)
    after = snapshot(core, obj)
    invariant(before, after, target)
    no_new_flips(before, after, target)
    assert not checked[2].get('rejected'), checked
    return dict(stages=stages, checked_result=plain(checked), checked_output=plain(after),
                checked_native=checked_events['native']), dict(preparations=preparations,
                internal_sequence='actual _rebuild_islands, then actual checked rebuild_islands', cache_faces_separate=True)


def fixed_case(core, metric):
    rows = []
    for kind in ('fixed', 'conflict'):
        obj, target = fixture(core, kind)
        positions = marks(core, obj, target)
        before = snapshot(core, obj)
        with trace(core, metric) as events:
            result = core.rebuild_islands(bpy.context, obj, target, positions, fix_boundary=True,
                prevent_flips=True, respect_uv_selection=False, _report_quality=False)
        after = snapshot(core, obj)
        rejected = bool(result[2].get('rejected'))
        invariant(before, after, target, exact_uv=rejected)
        no_new_flips(before, after, target)
        if kind == 'conflict':
            assert rejected and result[2].get('reason') == 'fixed_boundary_conflict', result
        else:
            assert not rejected, result
        rows.append(dict(kind=kind, result=plain(result), output=plain(after), native=events['native']))
    return dict(rows=rows), dict(actual_fixed_boundary_transactions=2)


def fill_holes_case(core, metric):
    obj, target = fixture(core, 'ring')
    before = snapshot(core, obj)
    positions = marks(core, obj, target)
    actual_unwrap = bpy.ops.uv.unwrap
    if not actual_unwrap.poll():
        raise NotImplementedError('uv.unwrap.poll() false in factory IMAGE_EDITOR context')
    calls = []

    class UVOps:
        def __getattr__(self, name):
            return getattr(bpy.ops.uv, name)

        def unwrap(self, *args, **kwargs):
            result = actual_unwrap(*args, **kwargs)
            calls.append(dict(parameters=plain(kwargs), returned=sorted(result)))
            return result

    class Ops:
        uv = UVOps()

        def __getattr__(self, name):
            return getattr(bpy.ops, name)

    class BpyProxy:
        ops = Ops()

        def __getattr__(self, name):
            return getattr(bpy, name)

    original = core.bpy
    core.bpy = BpyProxy()
    try:
        # Snapshots and positions contain only numbers. No test-side topo,
        # geometry cache or BMesh wrapper is retained across actual unwrap.
        with trace(core, metric) as events:
            result = core.rebuild_islands(bpy.context, obj, target, positions,
                fill_holes=True, prevent_flips=True, respect_uv_selection=False,
                auto_iterations=False, unfold_iterations=3, optimize_iterations=3, _report_quality=False)
    finally:
        core.bpy = original
    after = snapshot(core, obj)
    invariant(before, after, target, exact_uv=bool(result[2].get('rejected')))
    no_new_flips(before, after, target)
    assert calls and all(call['returned'] == ['FINISHED'] for call in calls), calls
    assert not result[2].get('rejected'), result
    return dict(result=plain(result), output=plain(after), native=events['native'], unwrap=calls), dict(
        actual_builtin_unwrap=True, ring_has_inner_boundary=True, prepare_calls=events['preparations'],
        test_side_bmesh_handles_released=True)


def shared_case(core, metric):
    obj, target = fixture(core, 'stack')
    bm = bmesh.from_edit_mesh(obj.data)
    topo = core.UVTopology(bm, core.uv_layer(bm))
    positions = core.constrained_loops(bm, only_faces=target)
    runs = core.constraint_runs(bm, topo, positions)
    shared = core.stacked_uv_groups(topo, target)
    assert shared
    pairs = [(topo.vgroup_loops[g][0], topo.vgroup_loops[other][0]) for g, other in sorted(shared.items())]
    before = snapshot(core, obj)
    safety = importlib.import_module(core.__package__ + '.safety')
    with trace(core, metric) as events:
        result = core.fit_constraint_boundary(topo, target, runs, {}, shared_uv_groups=shared,
            iterations=3, use_density_map=True, prevent_flips=True)
    after = snapshot(core, obj)
    invariant(before, after, target, exact_uv=not result)
    no_new_flips(before, after, target)
    for a, b in pairs:
        assert np.array_equal(after['uv'][a], after['uv'][b]), ('Shared variables separated', a, b)
    health = safety.AcceptanceProbe(core, topo, target).measure(topo)
    assert health['finite'] and not health['zero_edges']
    if result:
        assert health['max_deviation'] <= .02
    del topo, bm
    return dict(result=int(result), output=plain(after), native=events['native'], shared_pairs=pairs,
                health=plain(health)), dict(actual_solver=True, mocked=False, iterations_per_strength=3,
                native_calls=len(events['native']), shared_map_nonempty=True,
                rejected_exact_uv_restored=not bool(result), prepare_calls=events['preparations'])


try:
    assert (not bpy.app.background and not bpy.data.filepath and '--factory-startup' in sys.argv
            and '--disable-autoexec' in sys.argv), 'Require disposable --factory-startup --disable-autoexec GUI'
    bpy.context.preferences.use_preferences_save = False
    bpy.context.preferences.filepaths.use_auto_save_temporary_files = False
    REPORT['identities_before'] = {label: identity(root) for label, root in ROOTS.items()}
    REPORT['package_versions'] = {label: tomllib.loads((root / 'blender_manifest.toml').read_text(encoding='utf8'))['version']
                                  for label, root in ROOTS.items()}
    REPORT['cpu_artifacts'] = {}
    for label, root in ROOTS.items():
        data = json.loads((root / 'bin/windows-x64/engine.json').read_text(encoding='utf8'))
        library = root / 'bin/windows-x64' / data['library']
        actual = hashlib.sha256(library.read_bytes()).hexdigest()
        assert data['sha256'] == actual
        REPORT['cpu_artifacts'][label] = dict(identity=data, actual_sha256=actual)
    area = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
    area.type, area.ui_type = 'IMAGE_EDITOR', 'UV'
    override = dict(window=bpy.context.window, area=area,
                    region=next(r for r in area.regions if r.type == 'WINDOW'))
    functions = {'multi_island': multi_case, 'fixed_boundary': fixed_case,
                 'actual_fill_holes': fill_holes_case, 'actual_shared_stack': shared_case}
    with bpy.context.temp_override(**override):
        for label in ROOTS:
            core, metric = load_core(label)
            rows = {}
            REPORT['variants'][label] = rows
            for name, function in functions.items():
                REPORT['current'] = dict(variant=label, case=name)
                persist()
                try:
                    comparable, evidence = (function(label, core, metric) if name == 'multi_island'
                                            else function(core, metric))
                    rows[name] = dict(status='passed', comparable=comparable, evidence=evidence)
                except NotImplementedError as exc:
                    rows[name] = dict(status='unsupported', reason=str(exc))
                except BaseException:
                    rows[name] = dict(status='failed', error=traceback.format_exc())
                persist()
    REPORT['cases'] = len(functions)
    assert all(row['status'] == 'passed' for row in REPORT['variants']['candidate'].values()), 'Integration case failed or was unsupported'
    REPORT['identities_after'] = {label: identity(root) for label, root in ROOTS.items()}
    assert REPORT['identities_before'] == REPORT['identities_after'], 'Product files changed'
    REPORT.update(passed=True, status='passed', blender_version=bpy.app.version_string, installed_lifecycle_tested=False,
                  performance_claim=False, note='Actual core/host integration with package aliases; not installed add-on lifecycle acceptance')
except BaseException:
    REPORT.update(status='failed', error=traceback.format_exc())
finally:
    persist()
print(json.dumps(dict(status=REPORT['status'], report=OUT.name, comparisons=REPORT['comparisons'],
                     error=REPORT.get('error')), ensure_ascii=False), flush=True)
if REPORT['status'] != 'passed':
    os._exit(1)


def deferred_quit():
    try:
        REPORT['quit_requested'] = True
        persist()
        result = bpy.ops.wm.quit_blender('EXEC_DEFAULT')
        if 'FINISHED' not in result:
            raise RuntimeError('Factory quit was not finished: ' + repr(result))
    except BaseException:
        REPORT.update(status='failed', quit_error=traceback.format_exc())
        persist()
        os._exit(1)
    return None


# Match the already accepted factory GPU-host runner: no persistent preference
# save, suppress this disposable process's unsaved prompt, then quit after the
# startup script returns control to the GUI event loop.
bpy.context.preferences.view.use_save_prompt = False
bpy.app.timers.register(deferred_quit, first_interval=.1)
