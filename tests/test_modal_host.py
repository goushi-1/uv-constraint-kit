# SPDX-License-Identifier: GPL-3.0-or-later
"""Factory GUI acceptance using actual queued mouse-move modal G/R/S events.

Use --enable-event-simulate --factory-startup --disable-autoexec. Arguments are
the same --addon-root / --output / --reader options as test_gui_host.py.
No OS mouse is moved. Events go only to this process's factory window.
"""
from pathlib import Path
import hashlib
import json
import math
import sys
import time
import traceback

HERE = Path(__file__).resolve().parent
HELPER = HERE / 'reader_gui_checks.py'
if not HELPER.is_file():
    HELPER = HERE / 'test_gui_host.py'
helper_source = HELPER.read_text(encoding='utf8')
assert helper_source.count('\nSTAGES = build_stages()') == 1
# Import the existing fixture, snapshot, strict undo assertions and CLI without
# registering its EXEC_DEFAULT timer. No product source is patched.
exec(compile(helper_source.split('\nSTAGES = build_stages()',1)[0],str(HELPER),'exec'),globals())
assert '--factory-startup' in sys.argv and '--disable-autoexec' in sys.argv
assert '--enable-event-simulate' in sys.argv and not bpy.app.background and not bpy.data.filepath
START = time.monotonic()
RESULTS = []
DRAW = dict(total=0, modal=0, errors=[])
CURRENT = {}
OBS = None
INDEX = 0
DONE = False
SCENARIOS = [(sync,target,action,ending) for sync in (False,True)
             for target in ('whole','partial') for action in ('G','R','S')
             for ending in ('confirm','cancel')]


class UVCK_OT_modal_select(bpy.types.Operator):
    bl_idname = 'uv.uvck_modal_test_select'
    bl_label = 'UVCK Modal Fixture Selection'
    bl_options = {'UNDO','INTERNAL'}
    target: bpy.props.StringProperty()
    sync: bpy.props.BoolProperty()

    def execute(self,context):
        settings = context.scene.tool_settings
        settings.use_uv_select_sync = self.sync
        settings.uv_select_mode = 'VERTEX'
        settings.mesh_select_mode = (True,False,False)
        obj = bpy.data.objects[OBJ_NAME]
        bm = bmesh.from_edit_mesh(obj.data)
        addon.core.prepare_bm(bm)
        for face in bm.faces:face.select_set(False)
        for edge in bm.edges:edge.select_set(False)
        for vertex in bm.verts:
            vertex.select_set(vertex.co.x <= (1.001 if self.target=='whole' else .501))
        bm.select_flush_mode()
        for face in bm.faces:
            if not self.sync:face.select_set(True)
            for loop in face.loops:
                selected = (loop.vert.co.x <= (1.001 if self.target=='whole' else .501))
                loop.uv_select_vert = selected
                loop.uv_select_edge = False
        bm.uv_select_sync_valid = True
        bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
        del bm,obj
        return {'FINISHED'}


def schedule(function,delay=.12):
    def run():
        if DONE:return None
        try:
            if time.monotonic()-START>130:raise TimeoutError('Modal acceptance exceeded 130 seconds')
            function()
        except BaseException:
            fail(traceback.format_exc())
        return None
    bpy.app.timers.register(run,first_interval=delay)


def event(kind,value='PRESS',xy=None):
    xy = xy or CURRENT.get('mouse',CURRENT.get('start_mouse'))
    if xy is None:
        region=STATE['region'];xy=(region.x+region.width//2,region.y+region.height//2)
    STATE['window'].event_simulate(type=kind,value=value,x=int(xy[0]),y=int(xy[1]))


def modal():
    return addon.overlay._transform_running()


def finish(error=None):
    global DONE,OBS
    if DONE:return
    if modal():
        raise RuntimeError('Do not release mesh readers while a transform is active')
    DONE=True
    OBS=None
    source_after=fingerprint()
    if source_after!=STATE['source_before']:error=error or 'Candidate source changed during test'
    if DRAW['errors']:error=error or 'Overlay draw raised: '+repr(DRAW['errors'])
    if len(RESULTS)!=24:error=error or 'Expected exactly 24 completed gestures'
    STATE['error']=error
    diagnostics=addon.core.mesh_reader.diagnostics()
    try:
        addon.unregister()
        bpy.utils.unregister_class(UVCK_OT_test_selection)
        bpy.utils.unregister_class(UVCK_OT_modal_select)
    except BaseException:
        error=error or traceback.format_exc()
    report=dict(passed=error is None and len(RESULTS)==24,error=error,results=RESULTS,
        gestures=len(RESULTS),queued_mouse_moves=sum(len(r['samples']) for r in RESULTS),
        draw=DRAW,reader_route=ARGS.reader,reader=diagnostics,
        source_sha256_before=STATE['source_before'],source_sha256_after=source_after,
        helper_sha256=hashlib.sha256(HELPER.read_bytes()).hexdigest(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        blender=bpy.app.version_string,build=bpy.app.build_hash.decode(),
        simulated_mouse_modal_tested=True,os_mouse_moved=False,
        scope='Factory-window queued mouse events; G/R/S sampled while active; both sync modes, whole/partial selection, confirm/cancel, undo/redo; not a human or installed-environment test',
        scene_saved=False,preferences_saved=False,elapsed_seconds=time.monotonic()-START)
    try:
        OUTPUT.parent.mkdir(parents=True,exist_ok=True)
        OUTPUT.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
    except BaseException:
        traceback.print_exc()
        os._exit(1)
    print(json.dumps(dict(passed=report['passed'],gestures=len(RESULTS),error=error)),flush=True)
    bpy.app.timers.register(quit_timer,first_interval=.15)


def fail(error):
    # Cancel before releasing retained observers or overlay handlers.
    if modal():
        event('ESC');event('ESC','RELEASE')
        schedule(lambda: finish(error),.3)
    else:finish(error)


def setup_modal():
    setup()
    bpy.utils.register_class(UVCK_OT_modal_select)
    mark_and_auto()
    space=STATE['area'].spaces.active
    space.pivot_point='CURSOR';space.cursor_location=(.5,.5)
    original=addon.overlay.draw_uv_editor
    def draw():
        DRAW['total']+=1
        if modal():DRAW['modal']+=1
        try:return original()
        except BaseException:
            DRAW['errors'].append(traceback.format_exc());raise
    addon.overlay.unregister_handlers()
    addon.overlay.draw_uv_editor=draw
    addon.overlay.register_handlers()
    with uv_override():bpy.ops.image.view_all()
    event('ESC');event('ESC','RELEASE')
    schedule(begin_case,.8)


def begin_case():
    global CURRENT
    if INDEX==len(SCENARIOS):finish();return
    sync,target,action,ending=SCENARIOS[INDEX]
    CURRENT=dict(sync=sync,target=target,action=action,ending=ending,samples=[],draw_start=DRAW['modal'])
    with uv_override():
        result=bpy.ops.uv.uvck_modal_test_select('EXEC_DEFAULT',True,target=target,sync=sync)
    assert result=={'FINISHED'}
    STATE['area'].tag_redraw()
    schedule(prepare_gesture,.25)


def prepare_gesture():
    global OBS
    before=snapshot()
    assert np.any(before['selected']) and np.any(~before['selected'])
    CURRENT['before']=before
    # Prepare wrappers BEFORE starting. During the gesture read only the same
    # retained corners that the real overlay uses; do not construct UVTopology,
    # refresh BMesh indices or release element wrappers inside a modal transform.
    obj=bpy.data.objects[OBJ_NAME]
    bm=addon.overlay._edit_bmesh(obj)
    uvl=addon.core.uv_layer(bm)
    loops=addon.core.loops_in_order(bm)
    OBS=dict(bm=bm,uvl=uvl,loops=loops)
    region=STATE['region']
    start=region.view2d.view_to_region(.7,.6,clip=False)
    xy=(region.x+start[0],region.y+start[1])
    assert region.x+4<xy[0]<region.x+region.width-4 and region.y+4<xy[1]<region.y+region.height-4,xy
    CURRENT['start_mouse']=CURRENT['mouse']=xy
    event('MOUSEMOVE','NOTHING',xy)
    schedule(start_gesture)


def start_gesture():
    event(CURRENT['action']);event(CURRENT['action'],'RELEASE')
    schedule(lambda: wait_modal(0))


def wait_modal(ticks):
    if modal():
        send_motion(1);return
    if ticks>=12:raise AssertionError('Keyboard did not start a native modal transform')
    schedule(lambda:wait_modal(ticks+1))


def send_motion(step):
    t=step/6
    if CURRENT['action']=='G':u,v=.7+.10*t,.6+.06*t
    elif CURRENT['action']=='R':
        angle=.45*t;u=.5+.2*math.cos(angle)-.1*math.sin(angle);v=.5+.2*math.sin(angle)+.1*math.cos(angle)
    else:u,v=.5+.2*(1+.5*t),.5+.1*(1+.5*t)
    region=STATE['region'];p=region.view2d.view_to_region(u,v,clip=False)
    xy=(region.x+p[0],region.y+p[1]);CURRENT['mouse']=xy
    event('MOUSEMOVE','NOTHING',xy)
    STATE['area'].tag_redraw()
    schedule(lambda:sample_motion(step))


def sample_motion(step):
    assert modal(),'Transform ended before the mouse sample'
    points=np.asarray([tuple(loop[OBS['uvl']].uv) for loop in OBS['loops']],dtype=np.float64)
    before=CURRENT['before'];selected=before['selected']
    assert np.array_equal(points[~selected],before['uv'][~selected]),'Nonselected UVs moved during gesture'
    matrix,translation,singular,residual=affine_fit(before['uv'],points,selected)
    assert np.isfinite(points).all() and residual<4e-5,(CURRENT['action'],residual)
    if CURRENT['action']=='G':assert np.allclose(matrix,np.eye(2),atol=4e-5,rtol=0),matrix
    elif CURRENT['action']=='R':
        assert np.allclose(singular,[1.,1.],atol=4e-5,rtol=0) and np.linalg.det(matrix)>0,matrix
    else:
        assert abs(matrix[0,1])<4e-5 and abs(matrix[1,0])<4e-5 and abs(matrix[0,0]-matrix[1,1])<4e-5,matrix
        assert abs(matrix[0,0])>.1
    delta=float(np.max(np.abs(points[selected]-before['uv'][selected])))
    CURRENT['samples'].append(dict(step=step,max_delta=delta,affine_residual=residual,
        matrix=matrix.tolist(),translation=translation.tolist(),modal_draw_calls=DRAW['modal']-CURRENT['draw_start']))
    if step<6:send_motion(step+1);return
    assert delta>1e-4,'Mouse moves did not change selected UVs'
    assert DRAW['modal']>CURRENT['draw_start'],'Overlay was not drawn during gesture'
    CURRENT['last_uv']=points.copy()
    key='LEFTMOUSE' if CURRENT['ending']=='confirm' else 'ESC'
    event(key);event(key,'RELEASE')
    schedule(check_finished,.2)


def check_finished():
    global OBS
    assert not modal(),'Confirm/cancel did not end the gesture'
    # Release all observers before undo may replace the edit mesh.
    OBS=None
    after=snapshot();before=CURRENT['before']
    if CURRENT['ending']=='cancel':assert_same_uv_marks(after,before,'modal cancel')
    else:
        assert np.array_equal(after['uv'],CURRENT['last_uv']),'Confirmation changed the final modal coordinates'
        for key in ('marks','pins','selected','selected_edge','xyz'):
            assert np.array_equal(after[key],before[key]),key
    CURRENT['after']=after
    if CURRENT['ending']=='confirm':schedule(undo_gesture)
    else:complete_case()


def undo_gesture():
    with uv_override():
        assert bpy.ops.ed.undo.poll()
        assert bpy.ops.ed.undo()=={'FINISHED'}
    assert_same_uv_marks(snapshot(),CURRENT['before'],'modal undo')
    schedule(redo_gesture)


def redo_gesture():
    with uv_override():
        assert bpy.ops.ed.redo.poll()
        assert bpy.ops.ed.redo()=={'FINISHED'}
    assert_same_uv_marks(snapshot(),CURRENT['after'],'modal redo')
    complete_case()


def complete_case():
    global INDEX
    result={k:CURRENT[k] for k in ('sync','target','action','ending','samples')}
    result['passed']=True
    RESULTS.append(result)
    print(json.dumps({k:result[k] for k in ('sync','target','action','ending','passed')}),flush=True)
    INDEX+=1
    schedule(begin_case)


schedule(setup_modal,1.0)
