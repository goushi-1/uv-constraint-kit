# SPDX-License-Identifier: GPL-3.0-or-later
"""Public checks for the optional native triangle-frame preparation API.

Run under Blender with an extracted add-on root. The script uses only synthetic
geometry, does not register the add-on, and does not save a scene or preferences.
"""
import argparse
import importlib
import json
import math
from pathlib import Path
import random
import sys
from types import ModuleType

import bmesh
import bpy
import numpy as np
from mathutils import Vector


PARSER = argparse.ArgumentParser(description=__doc__)
PARSER.add_argument('--addon-root', type=Path, required=True)
PARSER.add_argument('--output', type=Path, required=True)
PARSER.add_argument('--require-native', action='store_true',
                     help='Require the optional native triangle-frame symbol.')
ARGS = PARSER.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
ADDON = ARGS.addon_root.resolve()
OUT = ARGS.output.resolve()
assert (ADDON / 'core.py').is_file() and (ADDON / 'metric.py').is_file()
assert not OUT.is_relative_to(ADDON), 'Write reports outside the add-on tree'
REPORT = {'status': 'running', 'passed': False, 'scene_saved': False,
          'preferences_saved': False, 'add_on_registered': False, 'cases': {}}


def persist():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(REPORT, indent=2, allow_nan=False) + '\n', encoding='utf8')


def load_addon():
    package = ModuleType('uvck_native_frames_public')
    package.__path__ = [str(ADDON)]
    package.__file__ = str(ADDON / '__init__.py')
    sys.modules[package.__name__] = package
    core = importlib.import_module(package.__name__ + '.core')
    metric = importlib.import_module(package.__name__ + '.metric')
    if ARGS.require_native:
        lib = core.native_engine._load()
        assert lib is not None and hasattr(lib, 'uvck_triangle_frames'), (
            'Required native triangle-frame capability did not load')
    return core, metric


def oracle_frames(points, triangles):
    """Independent public-mathutils oracle; inputs have already been cast to float32."""
    frames = np.zeros((len(triangles), 3, 2), dtype=np.float64)
    eligible = np.zeros(len(triangles), dtype=bool)
    for row, triangle in enumerate(triangles):
        a, b, c = (Vector(tuple(float(v) for v in points[index])) for index in triangle)
        edge, other = b - a, c - a
        length = edge.length
        if length < 1e-12:
            continue
        x = other.dot(edge) / length
        height_squared = other.length_squared - x * x
        if height_squared <= 1e-24:
            continue
        frames[row, 1, 0] = length
        frames[row, 2, 0] = x
        frames[row, 2, 1] = math.sqrt(height_squared)
        eligible[row] = True
    return frames, eligible


def numeric_inputs():
    rng = random.Random(0x5A17)
    triples = []
    for _ in range(96):
        a = tuple(rng.uniform(-20.0, 20.0) for _axis in range(3))
        b = tuple(a[i] + rng.uniform(-5.0, 5.0) for i in range(3))
        c = tuple(a[i] + rng.uniform(-5.0, 5.0) for i in range(3))
        triples.append((a, b, c))
    triples.extend((
        ((0., 0., 0.), (0., 0., 0.), (0., 1., 0.)),
        ((0., 0., 0.), (.5e-12, 0., 0.), (0., 1., 0.)),
        ((0., 0., 0.), (1e-12, 0., 0.), (0., 1., 0.)),
        ((0., 0., 0.), (2e-12, 0., 0.), (0., 1., 0.)),
        ((0., 0., 0.), (1., 0., 0.), (0., .5e-12, 0.)),
        ((0., 0., 0.), (1., 0., 0.), (0., 1e-12, 0.)),
        ((0., 0., 0.), (1., 0., 0.), (0., 2e-12, 0.)),
        ((2., 3., -1.), (3., 3., -1.), (4., 3., -1.)),
    ))
    points = np.asarray([point for tri in triples for point in tri], dtype=np.float32)
    triangles = np.arange(len(points), dtype=np.int64).reshape(-1, 3)
    return np.ascontiguousarray(points), np.ascontiguousarray(triangles)


def check_numeric(engine):
    lib = engine._load()
    if lib is None or not hasattr(lib, 'uvck_triangle_frames'):
        assert not ARGS.require_native
        points, triangles = numeric_inputs()
        assert engine.triangle_frames(points, triangles) is None
        return {'status': 'skipped', 'reason': 'optional native triangle-frame symbol unavailable'}
    points, triangles = numeric_inputs()
    points_before, triangles_before = points.tobytes(), triangles.tobytes()
    expected, expected_mask = oracle_frames(points, triangles)
    actual, actual_mask = engine.triangle_frames(points, triangles)
    assert actual.dtype == np.dtype(np.float64) and actual.shape == (len(triangles), 3, 2)
    assert actual_mask.dtype == np.dtype(bool) and actual_mask.shape == (len(triangles),)
    assert actual.tobytes() == expected.tobytes(), 'frame values differ from mathutils oracle'
    assert actual_mask.tobytes() == expected_mask.tobytes(), 'degenerate mask differs'
    assert points.tobytes() == points_before and triangles.tobytes() == triangles_before

    for bad_points in (
            np.asarray(((0., 0., 0.), (1., 0., 0.), (np.nan, 1., 0.)), dtype=np.float32),
            np.asarray(((0., 0., 0.), (1., 0., 0.), (np.inf, 1., 0.)), dtype=np.float32),
            np.asarray(((0., 0., 0.), (-3e38, 0., 0.), (3e38, 1., 0.)), dtype=np.float32)):
        assert engine.triangle_frames(bad_points, np.asarray(((0, 1, 2),), dtype=np.int64)) is None
    return {'finite_cases': len(triangles), 'degenerate_cases': int((~expected_mask).sum()),
            'oracle_bytes_equal': True, 'nonfinite_and_overflow_fallback': True,
            'inputs_unchanged': True}


def make_fixture(core):
    vertices, faces = [], []

    def add_face(points):
        start = len(vertices)
        vertices.extend(points)
        faces.append(tuple(range(start, len(vertices))))

    add_face(((0., 0., 0.), (2., 0., 0.), (0., 1., .25)))
    add_face(((4., 0., 0.), (6., 0., 0.), (6., 2., .5), (4., 2., 0.)))
    add_face(((9., 0., 0.), (11., 0., .2), (12., 1., .4),
              (11., 2., .8), (9.5, 2., .3), (8.5, 1., .1)))
    add_face(((15., 0., 0.), (16., 0., 0.), (17., 0., 0.)))  # Degenerate in 3D.

    mesh = bpy.data.meshes.new('NativeFramesPublicFixture')
    mesh.from_pydata(vertices, [], faces)
    mesh.uv_layers.new(name='UVMap')
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        core.prepare_bm(bm)
        uvl = core.uv_layer(bm)
        for face in bm.faces:
            for loop in face.loops:
                x, y = float(loop.vert.co.x), float(loop.vert.co.y)
                if face.index == 2:
                    loop[uvl].uv = (30. - 1.75 * x, 4. + .3 * y)
                else:
                    loop[uvl].uv = (x, y)
        return mesh, bm
    except BaseException:
        bm.free()
        bpy.data.meshes.remove(mesh)
        raise


def snapshots(core, bm):
    uvl = core.uv_layer(bm)
    loops = core.loops_in_order(bm)
    return (np.asarray([tuple(loop[uvl].uv) for loop in loops], dtype=np.float64),
            np.asarray([tuple(vertex.co) for vertex in bm.verts], dtype=np.float64))


def same_value(left, right, label):
    if isinstance(left, np.ndarray):
        assert isinstance(right, np.ndarray) and left.shape == right.shape, label
        assert left.dtype == right.dtype and left.tobytes() == right.tobytes(), label
    elif isinstance(left, dict):
        assert isinstance(right, dict) and left.keys() == right.keys(), label
        for key in left:
            if key != 'topo':
                same_value(left[key], right[key], f'{label}.{key}')
    elif isinstance(left, (list, tuple)):
        assert type(left) is type(right) and len(left) == len(right), label
        for index, (a, b) in enumerate(zip(left, right)):
            same_value(a, b, f'{label}[{index}]')
    elif isinstance(left, (set, frozenset)):
        assert left == right, label
    else:
        assert left == right, label


def check_geometry(core, metric, require_native):
    mesh, bm = make_fixture(core)
    engine = core.native_engine
    old_enabled, old_required = engine._enabled, engine._required
    old_library = engine._library
    old_attempted = engine._attempted
    old_calls = engine.diagnostics().get('frame_calls', 0)
    try:
        lib = engine._load()
        has_symbol = lib is not None and hasattr(lib, 'uvck_triangle_frames')
        if require_native:
            assert has_symbol, 'Required native triangle-frame symbol did not load'
        topo = core.UVTopology(bm, core.uv_layer(bm))
        faces = set(range(len(bm.faces)))
        groups = list(topo.vgroup_loops)
        shared = {group: group for group in groups}
        if len(groups) > 1:
            shared[groups[-1]] = groups[0]

        native_results = {}
        for label, mapping in (('unshared', None), ('shared_uv', shared)):
            before_uv, before_xyz = snapshots(core, bm)
            engine._enabled, engine._required = True, bool(require_native)
            native_result = metric._prepare_geometry(core, topo, faces, mapping)
            assert native_result is not None
            after_uv, after_xyz = snapshots(core, bm)
            assert before_uv.tobytes() == after_uv.tobytes(), label + ': UV changed'
            assert before_xyz.tobytes() == after_xyz.tobytes(), label + ': XYZ changed'
            native_results[label] = native_result

            before_uv, before_xyz = snapshots(core, bm)
            engine._enabled, engine._required = False, False
            fallback_result = metric._prepare_geometry(core, topo, faces, mapping)
            assert fallback_result is not None
            after_uv, after_xyz = snapshots(core, bm)
            assert before_uv.tobytes() == after_uv.tobytes(), label + ': fallback changed UV'
            assert before_xyz.tobytes() == after_xyz.tobytes(), label + ': fallback changed XYZ'
            same_value(native_result, fallback_result, label + '.geometry')
        if has_symbol:
            assert engine.diagnostics().get('frame_calls', 0) > old_calls

        # Simulate an older library: optional capability absence must use Python.
        engine._library = object()
        engine._attempted = True
        engine._enabled, engine._required = True, False
        missing_symbol_result = metric._prepare_geometry(core, topo, faces, None)
        same_value(native_results['unshared'], missing_symbol_result, 'missing_symbol.geometry')
        return {'faces': len(faces), 'geometry_fields': sorted(k for k in native_results['unshared']
                                                                if k != 'topo'),
                'triangle_count': len(native_results['unshared']['tri']),
                'shared_uv_mapping_equal': True, 'python_fallback_equal': True,
                'missing_symbol_fallback_equal': True, 'uv_xyz_unchanged': True,
                'native_symbol_present': has_symbol}
    finally:
        engine._enabled, engine._required = old_enabled, old_required
        engine._library, engine._attempted = old_library, old_attempted
        bm.free()
        bpy.data.meshes.remove(mesh)


def check_abi_sentinels(engine):
    lib = engine._load()
    if lib is None or not hasattr(lib, 'uvck_triangle_frames'):
        return {'status': 'skipped', 'reason': 'optional ABI symbol unavailable'}
    points, triangles = numeric_inputs()
    xyz = np.ascontiguousarray(points, dtype=np.float32)
    ids = np.ascontiguousarray(triangles, dtype=np.int64)
    rows = (0, len(ids) - 1)
    for row in rows:
        bad = ids.copy()
        bad[row, 0] = -1 if row == rows[0] else len(xyz)
        output = np.full((len(ids), 3, 2), 73.25, dtype=np.float64)
        eligible = np.full(len(ids), 91, dtype=np.uint8)
        output_before, eligible_before = output.tobytes(), eligible.tobytes()
        xyz_before, ids_before = xyz.tobytes(), bad.tobytes()
        status = lib.uvck_triangle_frames(len(xyz), len(ids), xyz.ctypes.data, bad.ctypes.data,
                                          output.ctypes.data, eligible.ctypes.data)
        assert status == -1
        assert output.tobytes() == output_before and eligible.tobytes() == eligible_before
        assert xyz.tobytes() == xyz_before and bad.tobytes() == ids_before
    return {'status': 'passed', 'cases': len(rows), 'sentinels_unchanged': True}


try:
    assert bpy.app.background and not bpy.data.filepath
    assert '--factory-startup' in sys.argv and '--disable-autoexec' in sys.argv
    core, metric = load_addon()
    REPORT['cases']['numeric'] = check_numeric(core.native_engine)
    REPORT['cases']['geometry'] = check_geometry(core, metric, ARGS.require_native)
    REPORT['cases']['abi_sentinels'] = check_abi_sentinels(core.native_engine)
    REPORT['native_diagnostics'] = core.native_engine.diagnostics()
    REPORT.update(status='passed', passed=True,
                  note='Synthetic mathutils oracle, geometry parity, optional fallback, and ABI boundaries; no solver claim.')
except BaseException as exc:
    import traceback
    REPORT.update(status='failed', error=type(exc).__name__, traceback=traceback.format_exc())
finally:
    persist()
print(json.dumps({'status': REPORT['status'], 'output': str(OUT),
                  'error': REPORT.get('error')}), flush=True)
if not REPORT['passed']:
    raise RuntimeError(REPORT.get('traceback', 'native frame public test failed'))
