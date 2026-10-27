# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent UV area regression; run in factory Blender background mode.

blender --background --factory-startup --disable-autoexec --python test_uv_area_host.py -- --addon-root PATH --output PATH [--require-native]
"""
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import struct
import sys
import threading
from types import SimpleNamespace

import bmesh
import bpy
import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument('--addon-root', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--require-native', action='store_true')
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
root = args.addon_root.resolve()
output = args.output.resolve()
assert root not in output.parents and output != root, 'report must be outside add-on'
assert bpy.app.background and '--factory-startup' in sys.argv and '--disable-autoexec' in sys.argv and not bpy.data.filepath
bpy.context.preferences.use_preferences_save = False
bpy.context.preferences.filepaths.use_auto_save_temporary_files = False


def identity():
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()
            and '__pycache__' not in p.parts and p.suffix != '.pyc'}


def exact(actual, expected):
    assert (math.isnan(actual) and math.isnan(expected)) or struct.pack('d', actual) == struct.pack('d', expected), (actual, expected)


def oracle(topo, faces, coordinates=None):
    total = 0.0
    for index in sorted(faces):
        points = [tuple(loop[topo.uvl].uv) if coordinates is None
                  else tuple(coordinates[topo.loop_pos[loop]]) for loop in topo.bm.faces[index].loops]
        area = 0.0
        for i, (x, y) in enumerate(points):
            nx, ny = points[(i + 1) % len(points)]
            area += float(x) * float(ny) - float(nx) * float(y)
        total += abs(area) * 0.5
    return total


before = identity()
assert before and 'core.py' in before and 'mesh_reader.py' in before
spec = importlib.util.spec_from_file_location('_uv_area_test_addon', root / '__init__.py', submodule_search_locations=[str(root)])
addon = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = addon
spec.loader.exec_module(addon)
core, reader = addon.core, addon.core.mesh_reader
reader._required = args.require_native
native = reader._load()
if args.require_native:
    assert native is not None and hasattr(native, 'island_area'), 'native area method required'
has_native = native is not None and hasattr(native, 'island_area')
bm = bmesh.new()
cases = 0
try:
    for number, count in enumerate((3, 4, 5, 9)):
        verts = [bm.verts.new((number * 4 + math.cos(2 * math.pi * i / count),
                               math.sin(2 * math.pi * i / count), 0)) for i in range(count)]
        bm.faces.new(verts)
    core.prepare_bm(bm)
    layer = bm.loops.layers.uv.new('UVMap')
    loops = core.loops_in_order(bm)
    topo = SimpleNamespace(bm=bm, uvl=layer, loop_pos={loop: i for i, loop in enumerate(loops)})
    queries = [[], [0], [1], [2], [3], [3, 1, 0, 2], [3, 0, 3, 1], {0, 2, 3}]
    rng = np.random.default_rng(175802)
    for case in range(40):
        exponent = (-20, -5, 0, 5, 20)[case % 5]
        values = rng.normal(size=(len(loops), 2)) * 2.0 ** exponent
        if case % 10 == 0:
            values[:, 1] = 0
        if case % 10 == 1:
            values += 2.0 ** (exponent + 20)
        for loop, xy in zip(loops, values):
            loop[layer].uv = xy
        coordinates = np.asarray([tuple(loop[layer].uv) for loop in loops], dtype=np.float64)
        for faces in queries:
            exact(core.island_area(topo, faces), oracle(topo, faces))
            exact(core.island_area(topo, faces, coordinates=coordinates), oracle(topo, faces, coordinates))
            cases += 2
        loops[0][layer].uv.x += 1.0
        exact(core.island_area(topo, [0]), oracle(topo, [0]))
        cases += 1

    saved_module, saved_enabled = reader._module, reader._enabled
    try:
        for mode in ('old_api', 'disabled'):
            reader._module = SimpleNamespace() if mode == 'old_api' else saved_module
            reader._enabled = mode != 'disabled'
            for faces in queries:
                exact(core.island_area(topo, faces), oracle(topo, faces))
    finally:
        reader._module, reader._enabled = saved_module, saved_enabled

    class InjectedReadError(RuntimeError):
        pass

    def fail(*unused):
        raise InjectedReadError('injected read error')

    try:
        reader._module = SimpleNamespace(island_area=fail)
        try:
            core.island_area(topo, [0])
        except InjectedReadError:
            pass
        else:
            raise AssertionError('native runtime error swallowed')
    finally:
        reader._module = saved_module
    try:
        core.island_area(topo, [len(bm.faces)])
    except IndexError:
        pass
    else:
        raise AssertionError('invalid face index accepted')

    if has_native:
        errors = []
        def worker():
            try:
                reader.island_area(iter(()), layer)
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        assert not thread.is_alive() and errors == ['BMesh capture requires the main thread']

    class Loop:
        def __init__(self, xy):
            self.uv = xy
        def __getitem__(self, unused):
            return self

    for points in ([], [(0., 0.)], [(0., 0.), (1., 1.)],
                   [(0., 0.), (1., 0.), (0., 1.)], [(0., 0.), (-1., 0.), (0., 1.)],
                   [(math.nan, 0.), (1., 0.), (0., 1.)],
                   [(math.inf, 0.), (1., 0.), (0., 1.)],
                   [(1e308, -1e308), (-1e308, 1e308), (0., 0.)],
                   [(-0., -0.), (1., 0.), (0., -0.)]):
        fake_loops = [Loop(p) for p in points]
        fake = SimpleNamespace(bm=SimpleNamespace(faces=[SimpleNamespace(loops=fake_loops)]),
                               uvl=None, loop_pos={loop: i for i, loop in enumerate(fake_loops)})
        coords = [loop.uv for loop in fake_loops]
        exact(core.island_area(fake, [0]), oracle(fake, [0]))
        exact(core.island_area(fake, [0], coordinates=coords), oracle(fake, [0], coords))
        cases += 2
    diagnostics = reader.diagnostics()
    if has_native:
        assert diagnostics['area_reads'] > 0
finally:
    bm.free()
assert identity() == before, 'add-on files changed during test'
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({'passed': True, 'cases': cases, 'native_area': has_native,
                              'reader': diagnostics, 'addon_files_sha256': before,
                              'optional_fallback_cases': 2 * len(queries),
                              'runtime_error_propagated': True, 'invalid_face_rejected': True,
                              'scene_saved': False, 'preferences_saved': False, 'addon_registered': False,
                              'main_thread_guard_checked': has_native}, indent=2) + '\n', encoding='utf8')
print(f'UV area checks passed: {cases}; native area: {has_native}')
