# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic live-BMesh versus supplied-input recovery checks.

Run only in a disposable factory background process. This checks data
routes and fresh default reads; it is not a solver or performance benchmark.
"""
from pathlib import Path
from types import ModuleType
import argparse
import hashlib
import importlib
import json
import sys
import traceback
import warnings

import bpy
import bmesh
import numpy as np

HERE=Path(__file__).resolve().parent
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--addon-root',type=Path,default=HERE.parent/'uv_constraint_kit')
parser.add_argument('--output',type=Path,default=HERE.parent/'test-results/recovery-input.json')
parser.add_argument('--require-native',action='store_true')
args=parser.parse_args(sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else [])
ROOTS={'candidate':args.addon_root.resolve()}
assert not args.output.resolve().is_relative_to(args.addon_root.resolve()),'Write reports outside the add-on tree'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def identity(root):return {p.relative_to(root).as_posix():sha(p) for p in root.rglob('*')
    if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
def load(label):
    name='uvck_precheck_'+label
    package=ModuleType(name)
    package.__path__=[str(ROOTS[label])]
    sys.modules[name]=package
    return importlib.import_module(name+'.core'),importlib.import_module(name+'.recovery')
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

def reference_crosses(topo, faces, triangles=None):
    positions = [tuple(topo.loop_pos[l] for l in tri)
                 for tri in (topo.loop_triangles() if triangles is None else triangles)
                 if tri[0].face.index in faces]
    uv = np.asarray([[tuple(topo.loops[p][topo.uvl].uv) for p in tri] for tri in positions])
    edges = uv[:, 1:]-uv[:, :1]
    return positions, edges[:, 0, 0]*edges[:, 1, 1]-edges[:, 0, 1]*edges[:, 1, 0]

report=dict(passed=False,rows=[],source_before={k:identity(p) for k,p in ROOTS.items()},
    script_sha256=sha(Path(__file__)),installed_changed=False,scene_saved=False,preferences_saved=False)
original_err=np.geterr()
def capture(topo):return [tuple(loop[topo.uvl].uv) for loop in topo.loops]
def outcome(function):
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter('always',RuntimeWarning)
        try:result=('value',function())
        except (ValueError,FloatingPointError,IndexError,ZeroDivisionError) as exc:
            result=('error',type(exc).__name__,str(exc))
    return result,[str(w.message) for w in captured]
try:
    assert bpy.app.background and not bpy.data.filepath and '--factory-startup' in sys.argv
    core,new=load('candidate')
    base,old=core,new
    for native in (False,True):
        for module in (core,):
            module.mesh_reader._enabled=module.native_engine._enabled=native
            module.mesh_reader._required=bool(native and args.require_native)
            module.native_engine._required=False
        for kind in ('multi','ring','stack','conflict'):
            for state in ('normal','mirrored','udim','folded','collapsed','nonfinite'):
                obj,faces=fixture(core,kind)
                bm=bmesh.from_edit_mesh(obj.data)
                uvl=core.uv_layer(bm)
                for face in bm.faces:
                    for loop in face.loops:
                        x,y=loop[uvl].uv
                        if state=='mirrored':loop[uvl].uv=(-x,y)
                        elif state=='udim':loop[uvl].uv=(x+3,y-2)
                        elif state=='folded' and loop.vert.index==4:loop[uvl].uv=(2.2,-.4)
                        elif state=='collapsed' and face.index in faces:loop[uvl].uv=(0,0)
                        elif state=='nonfinite' and loop.vert.index==0:loop[uvl].uv=(float('nan'),float('inf'))
                with np.errstate(all='ignore'):
                    topo=core.UVTopology(bm,uvl)
                values=capture(topo)
                before=np.asarray(values).tobytes()
                for mode in ('warn','raise'):
                    np.seterr(all=mode)
                    for mix in (1.,.5):
                        settings=dict(mix=mix)
                        expected=outcome(lambda:old.plan(base,topo,faces,settings))
                        for representation in ('list','array64'):
                            supplied={} if representation=='default' else dict(_coordinates=(
                                values if representation=='list' else np.asarray(values,dtype=float)))
                            actual=outcome(lambda:new.plan(core,topo,faces,settings,**supplied))
                            assert expected==actual,(native,kind,state,mode,mix,representation,expected,actual)
                            assert np.asarray(capture(topo)).tobytes()==before
                            assert np.geterr()==dict.fromkeys(('divide','over','under','invalid'),mode)
                            report['rows'].append(dict(native=native,kind=kind,state=state,mode=mode,mix=mix,
                                representation=representation,outcome=actual))
                # UVs change after the original input capture. Default callers
                # must still read current BMesh values; no implicit stale cache.
                for p,loop in enumerate(topo.loops):loop[uvl].uv=(p*.037+(p%3)*.019,p*.011)
                np.seterr(all='warn')
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore',RuntimeWarning)
                    a,b=reference_crosses(topo,faces)
                    c,d=new._crosses(topo,faces)
                assert a==c and b.dtype==d.dtype and b.tobytes()==d.tobytes()
                assert outcome(lambda:new.plan(core,topo,faces,{},_coordinates=capture(topo)))==outcome(lambda:new.plan(core,topo,faces,{}))
                loop=face=None
                del topo,bm,uvl
    if args.require_native:
        reader=core.mesh_reader.diagnostics()
        assert reader['loaded'] and reader['error'] is None and reader['captures']>0
        report['native_reader']=reader
    report['source_after']={k:identity(p) for k,p in ROOTS.items()}
    assert report['source_before']==report['source_after']
    report.update(passed=True,checks=len(report['rows']),fresh_after_writes=True,blender=bpy.app.version_string)
except BaseException:
    report['error']=traceback.format_exc()
finally:
    np.seterr(**original_err)
    report['numpy_err_restored']=np.geterr()==original_err
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
print(json.dumps({k:v for k,v in report.items() if k not in ('rows','source_before','source_after')}),flush=True)
if not report['passed']:raise RuntimeError(report.get('error'))
