# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic factory-Blender integration checks; never saves scene/preferences."""
import argparse
import json
import math
from pathlib import Path
import sys
import bpy
import bmesh
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--addon-root',type=Path,default=ROOT/'uv_constraint_kit')
parser.add_argument('--require-native',action='store_true')
args=parser.parse_args(sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else [])
sys.path.insert(0,str(args.addon_root.resolve().parent))
import uv_constraint_kit as addon
from uv_constraint_kit import core,quadrify,circle,shape_constraints,symmetry,live_symmetry


def create(vertices,faces):
    if bpy.context.object and bpy.context.object.mode=='EDIT':bpy.ops.object.mode_set(mode='OBJECT')
    for obj in list(bpy.context.scene.objects):bpy.data.objects.remove(obj,do_unlink=True)
    mesh=bpy.data.meshes.new('SyntheticMesh');mesh.from_pydata(vertices,[],faces);mesh.uv_layers.new()
    obj=bpy.data.objects.new('Synthetic',mesh);bpy.context.scene.collection.objects.link(obj)
    obj.select_set(True);bpy.context.view_layer.objects.active=obj;bpy.ops.object.mode_set(mode='EDIT')
    topo=topology(obj)
    for face in topo.bm.faces:
        face.select=True;face.uv_select=False
        for loop in face.loops:
            x,y,_=loop.vert.co
            loop[topo.uvl].uv=(.1*x+.012*math.sin(y),.1*y+.013*x)
            loop.uv_select_vert=loop.uv_select_edge=False
    topo.bm.uv_select_sync_valid=True
    bpy.context.scene.tool_settings.use_uv_select_sync=False
    bpy.context.scene.tool_settings.uv_select_mode='EDGE'
    return obj


def topology(obj):
    bm=bmesh.from_edit_mesh(obj.data);core.prepare_bm(bm)
    return core.UVTopology(bm,core.uv_layer(bm))


def grid():
    points=[(x,y,0) for y in range(3) for x in range(3)]
    points += [(4,0,0),(5,0,0),(5,1,0),(4,1,0)]
    return create(points,[(0,1,4,3),(1,2,5,4),(3,4,7,6),(4,5,8,7),(9,10,11,12)])


def values(obj):
    t=topology(obj)
    return [tuple(l[t.uvl].uv) for l in t.loops]


def constraint_case():
    obj=grid();t=topology(obj);faces={0,1,2,3}
    core.constraint_layer(t.bm,create=True)
    del t
    t=topology(obj);layer=core.constraint_layer(t.bm)
    marked=[p for p,l in enumerate(t.loops) if {l.vert.index,l.link_loop_next.vert.index}=={0,1}]
    for p in marked:t.loops[p][layer]=core.C_HORIZONTAL
    t.loops[0][t.uvl].pin_uv=True
    before=values(obj);outside=[p for p,l in enumerate(t.loops) if l.face.index not in faces]
    area=core.island_area(t,faces)
    del t
    result=core.rebuild_islands(bpy.context,obj,faces,marked,respect_uv_selection=False)
    assert not result[2].get('rejected'),result[2]
    t=topology(obj);after=values(obj)
    assert after[0]==before[0] and all(after[p]==before[p] for p in outside)
    assert abs(core.island_area(t,faces)/area-1)<1e-4
    assert addon.safety.AcceptanceProbe(core,t,faces).measure(t)['max_deviation']<=.05


def grid_case():
    obj=grid();before=values(obj)
    result=quadrify.quadrify(bpy.context,obj,{0,1,2,3},shape='EVEN',preserve_uv_connections=True)
    assert result['changed']==1 and not result['skipped'],result
    t=topology(obj)
    assert values(obj)[16:]==before[16:]
    for face in list(t.bm.faces)[:4]:
        for l in face.loops:
            d=l[t.uvl].uv-l.link_loop_next[t.uvl].uv
            assert min(abs(d.x),abs(d.y))<1e-6 and max(abs(d.x),abs(d.y))>1e-4


def protected_case():
    obj=grid();t=topology(obj);faces={0,1,2,3}
    core.constraint_layer(t.bm,create=True)
    del t
    t=topology(obj);layer=core.constraint_layer(t.bm)
    marks=[p for p,l in enumerate(t.loops) if {l.vert.index,l.link_loop_next.vert.index}=={0,1}]
    for p in marks:t.loops[p][layer]=core.C_HORIZONTAL
    original=values(obj)
    del t
    result=core.rebuild_islands(bpy.context,obj,faces,marks,prevent_flips=True,
        respect_uv_selection=False,_report_quality=False)
    assert not result[2].get('rejected'),result[2]
    assert result[2]['solver']=='protected_metric' and result[2]['health']['max_deviation']<=.05
    json.dumps(result,allow_nan=False)
    assert values(obj)[16:]==original[16:]


def shape_case():
    obj=create([(0,0,0),(1,0,0),(1,1,0),(0,1,0)],[(0,1,2,3)])
    t=topology(obj)
    for l in t.loops:l.uv_select_vert=l.uv_select_edge=True
    t.bm.uv_select_sync_valid=True
    del t
    result=circle.circularize(bpy.context,obj,{0},selected_only=True)
    assert result['changed']==1 and not result['skipped'],result
    before=values(obj)
    addon.ops._optimize_islands(bpy.context,obj,{0},parameters={'respect_uv_selection':False})
    assert np.max(np.abs(np.asarray(values(obj))-before))<2e-6
    assert len(shape_constraints._plan(topology(obj)))==1


def symmetry_case():
    vertices=[(-1,-1,0),(-1,1,0),(-1,1,1),(-1,-1,1),
              (1,-1,0),(1,1,0),(1,1,1),(1,-1,1)]
    obj=create(vertices,[(0,1,2,3),(4,7,6,5)])
    t=topology(obj)
    for i,l in enumerate(t.loops):l[t.uvl].uv=((.1,.2,.2,.1)[i%4],(.1,.1,.2,.2)[i%4])
    t.bm.faces[0].uv_select=True;t.bm.faces[1].uv_select=False;t.bm.uv_select_sync_valid=True
    del t
    capture={}
    matched,missing=symmetry.symmetrize_mesh(bpy.context,obj,'-X to +X',False,'X',False,False,.005,capture,center=(.5,.5))
    assert matched==1 and missing==0
    live_symmetry.install(obj,capture)
    t=topology(obj)
    for source,target,fan,pivot in capture['pairs']:
        assert abs(t.loops[target][t.uvl].uv.x-(2*pivot-t.loops[source][t.uvl].uv.x))<1e-6
    source=capture['pairs'][0][0];t.loops[source][t.uvl].uv.y+=.025
    del t
    live_symmetry.sync(bpy.context)
    t=topology(obj)
    for source,target,fan,pivot in capture['pairs']:
        assert abs(t.loops[source][t.uvl].uv.y-t.loops[target][t.uvl].uv.y)<1e-6
    del t
    live_symmetry.clear_selected_targets(bpy.context,obj,set(range(8)),bake=True)


addon.register()
try:
    if args.require_native:assert addon.native_engine._load() is not None
    for test in (constraint_case,grid_case,protected_case,shape_case,symmetry_case):test()
finally:
    addon.unregister()
print(json.dumps(dict(passed=True,cases=5,blender=bpy.app.version_string,native_required=args.require_native,
                     scene_saved=False,preferences_saved=False)))
