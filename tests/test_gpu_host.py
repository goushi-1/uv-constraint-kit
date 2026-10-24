# SPDX-License-Identifier: GPL-3.0-or-later
"""Factory GUI mark/clear/optimize CPU versus natural GPU dispatch checks.

Run against an extracted CPU+GPU extension ZIP, with no input scene. The GPU
threshold is unchanged in AUTO trials. This measures complete synchronous
operators, not GPU kernels or mouse/undo behavior. Never saves user preferences
or scenes. Blender may write factory recovery data to its test TEMP directory.
Requires Windows x64, NVIDIA SM 8.9+ and CUDA driver 12.4+.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import bpy
import bmesh
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--addon-root', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
assert (not bpy.app.background and not bpy.data.filepath and '--factory-startup' in sys.argv
        and '--disable-autoexec' in sys.argv), 'Use a disposable factory GUI process'
bpy.context.preferences.use_preferences_save = False
sys.path.insert(0, str(args.addon_root.resolve().parent))
import uv_constraint_kit as addon


def identity():
    folder = args.addon_root.resolve()
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.rglob('*') if p.is_file() and '__pycache__' not in p.parts}


def create(side):
    if bpy.context.object and bpy.context.object.mode == 'EDIT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for obj in list(bpy.context.scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    points = [(x / side, y / side, 0.) for y in range(side + 1) for x in range(side + 1)]
    faces = []
    for y in range(side):
        for x in range(side):
            a = y * (side + 1) + x
            faces.append((a, a + 1, a + side + 2, a + side + 1))
    outside_first = len(points)
    points.extend(((2., 0., 0.), (3., 0., 0.), (3., 1., 0.), (2., 1., 0.)))
    faces.append(tuple(range(outside_first, outside_first + 4)))
    mesh = bpy.data.meshes.new('GPUCheckMesh')
    mesh.from_pydata(points, [], faces)
    mesh.uv_layers.new()
    obj = bpy.data.objects.new('GPUCheck', mesh)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(mesh)
    addon.core.prepare_bm(bm)
    uvl = bm.loops.layers.uv.active
    scale = min(1., 16. / side)
    for face in bm.faces:
        face.select = True
        face.uv_select = False
        for loop in face.loops:
            x, y, _ = loop.vert.co
            if face.index < side * side:
                loop[uvl].uv = (x + scale * .025 * math.sin(y * math.tau) * (.3 + math.sin(x * math.pi)),
                                y + scale * .018 * math.sin(x * math.tau) * (.3 + math.sin(y * math.pi)))
            else:
                loop[uvl].uv = (x, y)
                loop[uvl].pin_uv = True
            selected = face.index < side * side and y == 0 and loop.link_loop_next.vert.co.y == 0
            loop.uv_select_edge = selected
            loop.uv_select_vert = y == 0 and face.index < side * side
    bm.uv_select_sync_valid = True
    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    bpy.context.scene.tool_settings.use_uv_select_sync = False
    bpy.context.scene.tool_settings.uv_select_mode = 'EDGE'
    return obj


def snapshot(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    addon.core.prepare_bm(bm)
    uvl = bm.loops.layers.uv.active
    loops = addon.core.loops_in_order(bm)
    layer = addon.core.constraint_layer(bm)
    return dict(uv=np.asarray([tuple(l[uvl].uv) for l in loops]),
        marks=[l[layer] if layer else 0 for l in loops],
        pins=[l[uvl].pin_uv for l in loops],
        selection=([v.select for v in bm.verts], [e.select for e in bm.edges],
                   [f.select for f in bm.faces], [f.uv_select for f in bm.faces],
                   [(l.uv_select_vert, l.uv_select_edge) for l in loops], bm.uv_select_sync_valid),
        xyz=[tuple(v.co) for v in bm.verts])


def run_variant(side, cpu_only):
    native = addon.native_engine
    native.shutdown_gpu()
    native.diagnostics(reset=True)
    threshold = native._GPU_MIN_TRIANGLES
    native._GPU_MIN_TRIANGLES = 2 ** 63 - 1 if cpu_only else threshold
    obj = create(side)
    initial = snapshot(obj)
    rows, states = [], []
    try:
        for action in ('mark', 'clear', 'optimize'):
            native.diagnostics(reset=True)
            start = time.perf_counter()
            if action == 'mark':
                result = bpy.ops.uv.uvck_constrain_horizontal('EXEC_DEFAULT')
            elif action == 'clear':
                result = bpy.ops.uv.uvck_unconstrain('EXEC_DEFAULT')
            else:
                result = bpy.ops.uv.uvck_optimize('EXEC_DEFAULT')
            elapsed = time.perf_counter() - start
            diagnostics = native.diagnostics()
            state = snapshot(obj)
            assert result == {'FINISHED'}, result
            assert addon.ops.last_optimize_result['status'] == 'COMPLETED', addon.ops.last_optimize_result
            assert state['selection'] == initial['selection'] and state['pins'] == initial['pins']
            assert state['xyz'] == initial['xyz'] and np.isfinite(state['uv']).all()
            assert np.array_equal(state['uv'][-4:], initial['uv'][-4:]), 'Outside island moved'
            assert np.array_equal(state['uv'][state['pins']], initial['uv'][state['pins']]), 'Pin moved'
            if action == 'mark':
                assert sum(v == addon.core.C_HORIZONTAL for v in state['marks']) == side
                selected = [p for p, pair in enumerate(state['selection'][4]) if pair[1]]
                edges = [(p, p - p % 4 + (p + 1) % 4) for p in selected]
                assert max(abs(state['uv'][a, 1] - state['uv'][b, 1]) for a, b in edges) < 1e-6
            else:
                assert not any(state['marks']), 'Clear left constraints behind'
            gpu = diagnostics['gpu']
            expected = 'CPU' if cpu_only or side < 128 else 'GPU'
            assert gpu['last_backend'] == expected and diagnostics['fallbacks'] == 0, diagnostics
            if expected == 'GPU':
                assert gpu['calls'] > 0 and gpu['fallbacks'] == 0 and not gpu['disabled'], gpu
                assert gpu['driver']['sessions'] > 0 and gpu['driver']['energy'] > 0
                assert gpu['driver']['gradient'] > 0 and gpu['driver']['errors'] == 0, gpu
            else:
                assert not gpu['attempted'], gpu
            rows.append(dict(action=action, side=side, triangles=side * side * 2,
                seconds=elapsed, backend=expected, diagnostics=diagnostics,
                optimize=addon.ops.last_optimize_result.copy(), passed=True))
            states.append(state)
            print(json.dumps(dict(action=action, side=side, backend=expected,
                                  seconds=elapsed, passed=True)), flush=True)
        return rows, states
    finally:
        native._GPU_MIN_TRIANGLES = threshold
        native.shutdown_gpu()
        assert not native.diagnostics()['gpu']['loaded']


def main():
    before = identity()
    addon.register()
    addon.native_engine._required = True
    area = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
    area.type = 'IMAGE_EDITOR'
    area.ui_type = 'UV'
    rows, pairs = [], []
    try:
        with bpy.context.temp_override(area=area, region=next(r for r in area.regions if r.type == 'WINDOW')):
            small, _ = run_variant(16, False)
            rows.extend(small)
            cpu, left = run_variant(128, True)
            auto, right = run_variant(128, False)
            rows.extend(cpu + auto)
            for a, b, l, r in zip(cpu, auto, left, right):
                delta = float(np.max(np.abs(l['uv'] - r['uv'])))
                assert delta <= 2e-6 and l['marks'] == r['marks'], delta
                for key in ('selection', 'pins', 'xyz'):
                    assert l[key] == r[key], key
                pairs.append(dict(action=a['action'], uv_max_delta=delta,
                    cpu_seconds=a['seconds'], auto_seconds=b['seconds'], passed=True))
    finally:
        addon.unregister()
    assert identity() == before, 'Packaged source or engine changed'
    report = dict(passed=True, checks=len(rows), pairs=pairs, rows=rows,
        blender=bpy.app.version_string, build=bpy.app.build_hash.decode(),
        source_sha256=before, scene_saved=False, preferences_saved=False,
        timing_note='One CPU block then AUTO block, includes first initialization; no stable speedup claim',
        scope='Synthetic complete operators and natural dispatch; not live installed gestures or undo')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf8')
    print(json.dumps(dict(passed=True, checks=len(rows), gpu_operations=3)), flush=True)


if __name__ == '__main__':
    main()
    # Only this factory process's in-memory setting changes; no preferences are
    # saved. Defer exit until the GUI event loop can process the quit request.
    bpy.context.preferences.view.use_save_prompt = False
    bpy.app.timers.register(lambda: bpy.ops.wm.quit_blender() and None, first_interval=.1)
