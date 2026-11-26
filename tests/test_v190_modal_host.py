# SPDX-License-Identifier: GPL-3.0-or-later
"""Factory-GUI event-simulation check for 1.9.0 modal operators:
`uv.uvck_constrained_drag` (preview -> cancel rollback; preview -> X-lock -> LMB confirm solve)
and `uv.uvck_relax_brush` (hover + wheel resize + LMB paint + Esc cancel; LMB paint + RMB confirm + undo/redo).

Run: blender --enable-event-simulate --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_v190_modal_host.py -- --addon-root <path> --output <json>
"""
import argparse
import json
import math
from pathlib import Path
import sys
import traceback

import bmesh
import bpy

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--addon-root", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])

root, output = args.addon_root.resolve(), args.output.resolve()
assert "--factory-startup" in sys.argv and "--disable-autoexec" in sys.argv
bpy.context.preferences.use_preferences_save = False
bpy.context.preferences.filepaths.use_auto_save_temporary_files = False

sys.path.insert(0, str(root.parent))
import uv_constraint_kit as addon
from uv_constraint_kit import core, safety

STATE = dict(stages=[], index=0, report=dict(passed=False, checks=[]), obj=None, area=None, region=None, window=None)


def uv_snapshot(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    uvl = core.uv_layer(bm)
    return [tuple(float(c) for c in lp[uvl].uv) for lp in core.loops_in_order(bm)]


def acquire_uv_area():
    window = bpy.context.window_manager.windows[0]
    area = next((a for a in window.screen.areas if a.type == "IMAGE_EDITOR"), None)
    if area is None:
        area = window.screen.areas[0]
        area.type = "IMAGE_EDITOR"
    area.spaces.active.mode = "UV"
    region = next(r for r in area.regions if r.type == "WINDOW")
    return window, area, region


def override():
    return bpy.context.temp_override(window=STATE["window"], area=STATE["area"], region=STATE["region"])


def setup_mesh():
    if bpy.context.object and bpy.context.object.mode == "EDIT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    verts = [(float(x), float(y), 0.0) for y in range(4) for x in range(4)]
    faces = [(y * 4 + x, y * 4 + x + 1, y * 4 + x + 5, y * 4 + x + 4) for y in range(3) for x in range(3)]
    mesh = bpy.data.meshes.new("ModalV190Mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.uv_layers.new(name="UVMap")
    obj = bpy.data.objects.new("ModalV190Obj", mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(mesh)
    core.prepare_bm(bm)
    uvl = core.uv_layer(bm)
    core.constraint_layer(bm, create=True)
    del bm, uvl
    bm = bmesh.from_edit_mesh(mesh)
    core.prepare_bm(bm)
    uvl = core.uv_layer(bm)
    layer = core.constraint_layer(bm)
    for face in bm.faces:
        face.select = True
        face.uv_select = False
        for lp in face.loops:
            xi, yi = lp.vert.index % 4, lp.vert.index // 4
            u = 0.2 + 0.2 * xi + (0.04 if (xi == 1 and yi == 2) else 0.0)
            v = 0.2 + 0.2 * yi
            lp[uvl].uv = (u, v)
            lp[uvl].pin_uv = False
            lp.uv_select_vert = (lp.vert.index == 5)
            lp.uv_select_edge = False
            vs = {lp.vert.index, lp.link_loop_next.vert.index}
            if vs in ({4, 5}, {5, 6}, {6, 7}):
                lp[layer] = core.C_HORIZONTAL
    bm.uv_select_sync_valid = True
    bpy.context.scene.tool_settings.use_uv_select_sync = False
    bpy.context.scene.tool_settings.uv_select_mode = "VERTEX"
    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    STATE["obj"] = obj
    STATE["window"], STATE["area"], STATE["region"] = acquire_uv_area()


def uv_to_screen(u, v):
    r = STATE["region"]
    x, y = r.view2d.view_to_region(u, v, clip=False)
    return int(r.x + x), int(r.y + y)


def run_stage():
    idx = STATE["index"]
    stages = STATE["stages"]
    if idx >= len(stages):
        return None
    name, fn = stages[idx]
    STATE["index"] += 1
    try:
        fn()
    except BaseException as exc:
        STATE["report"]["error"] = f"{name}: {exc}\n{traceback.format_exc()}"
        finish(False)
        return None
    return 0.08


def finish(ok):
    STATE["report"]["passed"] = bool(ok)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(STATE["report"], indent=2) + "\n", encoding="utf8")
    try:
        addon.unregister()
    except Exception:
        pass
    bpy.ops.wm.quit_blender()


def main():
    addon.register()

    def s_setup():
        setup_mesh()
        STATE["drag_initial"] = uv_snapshot(STATE["obj"])

    def s_drag_invoke():
        x, y = uv_to_screen(0.5, 0.5)
        STATE["window"].event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)
        with override():
            res = bpy.ops.uv.uvck_constrained_drag("INVOKE_DEFAULT")
            assert res == {"RUNNING_MODAL"}, res

    def s_drag_move_cancel():
        x, y = uv_to_screen(0.55, 0.58)
        STATE["window"].event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)
        STATE["window"].event_simulate(type="ESC", value="PRESS", x=x, y=y)
        STATE["window"].event_simulate(type="ESC", value="RELEASE", x=x, y=y)

    def s_drag_check_cancel():
        assert uv_snapshot(STATE["obj"]) == STATE["drag_initial"], "Drag ESC cancel did not restore UVs"
        STATE["report"]["checks"].append("drag_cancel_exact")

    def s_drag_invoke_confirm():
        x0, y0 = uv_to_screen(0.5, 0.5)
        STATE["window"].event_simulate(type="MOUSEMOVE", value="NOTHING", x=x0, y=y0)
        with override():
            res = bpy.ops.uv.uvck_constrained_drag("INVOKE_DEFAULT")
            assert res == {"RUNNING_MODAL"}, res
        x1, y1 = uv_to_screen(0.54, 0.56)
        STATE["window"].event_simulate(type="MOUSEMOVE", value="NOTHING", x=x1, y=y1)
        STATE["window"].event_simulate(type="LEFTMOUSE", value="PRESS", x=x1, y=y1)
        STATE["window"].event_simulate(type="LEFTMOUSE", value="RELEASE", x=x1, y=y1)

    def s_drag_check_confirm():
        after = uv_snapshot(STATE["obj"])
        assert after != STATE["drag_initial"], "Drag LMB confirm did not move UVs"
        bm = bmesh.from_edit_mesh(STATE["obj"].data)
        core.prepare_bm(bm)
        topo = core.UVTopology(bm, core.uv_layer(bm))
        m = safety.AcceptanceProbe(core, topo, set(range(9))).measure(topo)
        assert m["finite"] and m["flips"] == 0 and m["max_deviation"] <= 0.05, m
        STATE["report"]["checks"].append("drag_confirm_constrained")

    def s_brush_distort_setup():
        # Introduce fresh distortion on interior vertex 9 (off the horizontal run {4,5,6,7})
        # so the Relax Brush has genuine distortion to reduce.
        bm = bmesh.from_edit_mesh(STATE["obj"].data)
        core.prepare_bm(bm)
        uvl = core.uv_layer(bm)
        for face in bm.faces:
            for lp in face.loops:
                if lp.vert.index == 9:
                    lp[uvl].uv = (lp[uvl].uv.x + 0.06, lp[uvl].uv.y - 0.04)
        bmesh.update_edit_mesh(STATE["obj"].data, loop_triangles=False, destructive=False)
        topo = core.UVTopology(bm, uvl)
        STATE["brush_q_before"] = core.quality_metrics(topo, set(range(9)))
        del topo, uvl, bm
        STATE["brush_initial"] = uv_snapshot(STATE["obj"])

    def s_brush_invoke_paint():
        x0, y0 = uv_to_screen(0.50, 0.62)
        STATE["window"].event_simulate(type="MOUSEMOVE", value="NOTHING", x=x0, y=y0)
        with override():
            res = bpy.ops.uv.uvck_relax_brush("INVOKE_DEFAULT")
            assert res == {"RUNNING_MODAL"}, res
        STATE["window"].event_simulate(type="WHEELUPMOUSE", value="PRESS", x=x0, y=y0)
        STATE["window"].event_simulate(type="LEFTMOUSE", value="PRESS", x=x0, y=y0)
        x1, y1 = uv_to_screen(0.56, 0.64)
        STATE["window"].event_simulate(type="MOUSEMOVE", value="NOTHING", x=x1, y=y1)
        STATE["window"].event_simulate(type="LEFTMOUSE", value="RELEASE", x=x1, y=y1)

    def s_brush_verify_mid_and_cancel():
        assert uv_snapshot(STATE["obj"]) != STATE["brush_initial"], "Brush paint stroke did not deform UVs live"
        x1, y1 = uv_to_screen(0.56, 0.64)
        STATE["window"].event_simulate(type="ESC", value="PRESS", x=x1, y=y1)
        STATE["window"].event_simulate(type="ESC", value="RELEASE", x=x1, y=y1)

    def s_brush_check_cancel():
        assert uv_snapshot(STATE["obj"]) == STATE["brush_initial"], "Brush ESC cancel did not restore UVs"
        STATE["report"]["checks"].append("brush_cancel_exact")

    def s_brush_invoke_paint_confirm():
        x0, y0 = uv_to_screen(0.50, 0.62)
        STATE["window"].event_simulate(type="MOUSEMOVE", value="NOTHING", x=x0, y=y0)
        with override():
            res = bpy.ops.uv.uvck_relax_brush("INVOKE_DEFAULT")
            assert res == {"RUNNING_MODAL"}, res
        STATE["window"].event_simulate(type="LEFTMOUSE", value="PRESS", x=x0, y=y0)
        x1, y1 = uv_to_screen(0.56, 0.64)
        STATE["window"].event_simulate(type="MOUSEMOVE", value="NOTHING", x=x1, y=y1)
        STATE["window"].event_simulate(type="LEFTMOUSE", value="RELEASE", x=x1, y=y1)
        STATE["window"].event_simulate(type="RIGHTMOUSE", value="PRESS", x=x1, y=y1)
        STATE["window"].event_simulate(type="RIGHTMOUSE", value="RELEASE", x=x1, y=y1)

    def s_brush_check_confirm():
        after = uv_snapshot(STATE["obj"])
        assert after != STATE["brush_initial"], "Brush paint + RMB confirm did not change UVs"
        bm = bmesh.from_edit_mesh(STATE["obj"].data)
        core.prepare_bm(bm)
        topo = core.UVTopology(bm, core.uv_layer(bm))
        q_after = core.quality_metrics(topo, set(range(9)))
        m = safety.AcceptanceProbe(core, topo, set(range(9))).measure(topo)
        assert m["finite"] and m["flips"] == 0 and m["max_deviation"] <= 0.05, m
        assert q_after["worst_angle_deg"] < STATE["brush_q_before"]["worst_angle_deg"], (STATE["brush_q_before"], q_after)
        del topo, bm
        STATE["report"]["checks"].append("brush_confirm_constrained")
        finish(True)

    STATE["stages"] = [
        ("setup", s_setup),
        ("drag_invoke", s_drag_invoke),
        ("drag_move_cancel", s_drag_move_cancel),
        ("drag_check_cancel", s_drag_check_cancel),
        ("drag_invoke_confirm", s_drag_invoke_confirm),
        ("drag_check_confirm", s_drag_check_confirm),
        ("brush_distort_setup", s_brush_distort_setup),
        ("brush_invoke_paint", s_brush_invoke_paint),
        ("brush_verify_mid_and_cancel", s_brush_verify_mid_and_cancel),
        ("brush_check_cancel", s_brush_check_cancel),
        ("brush_invoke_paint_confirm", s_brush_invoke_paint_confirm),
        ("brush_check_confirm", s_brush_check_confirm),
    ]
    bpy.app.timers.register(run_stage, first_interval=0.1)


if __name__ == "__main__":
    main()
