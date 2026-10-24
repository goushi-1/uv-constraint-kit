# SPDX-License-Identifier: GPL-3.0-or-later
"""Private GUI undo/redo and G/R/S checks for the isolated UVCK reader build.

Run in a disposable factory GUI process, for example:
  blender --factory-startup --disable-autoexec --python reader_gui_checks.py -- \
      --addon-root <extracted-addon> --output <report.json>

The G/R/S checks execute confirmed Blender operators with EXEC_DEFAULT in the
UV editor context. They do not simulate or accept real mouse-drag gestures.
No user scene or preferences are saved.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback

import bpy
import bmesh
import numpy as np


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--addon-root", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--reader", choices=("native", "python"), default="native")
ARGS = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
if "--factory-startup" not in sys.argv or "--disable-autoexec" not in sys.argv or bpy.data.filepath:
    raise RuntimeError("Use an unsaved --factory-startup --disable-autoexec test process")
ADDON_ROOT = ARGS.addon_root.resolve()
OUTPUT = ARGS.output.resolve()
SIDE = 8
OBJ_NAME = "UVCK_Reader_GUI_Check"
MAX_SECONDS = 90.0
STARTED = time.monotonic()

assert ADDON_ROOT.is_dir() and (ADDON_ROOT / "__init__.py").is_file(), "Invalid --addon-root"
assert not OUTPUT.is_relative_to(ADDON_ROOT), "Report path must be outside the add-on package"
bpy.context.preferences.use_preferences_save = False
bpy.context.preferences.view.use_save_prompt = False
sys.path.insert(0, str(ADDON_ROOT.parent))
import uv_constraint_kit as addon  # noqa: E402


def fingerprint():
    files = {}
    for path in sorted(ADDON_ROOT.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            relative = path.relative_to(ADDON_ROOT).as_posix()
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    root_hash = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"sha256": root_hash, "files": files}


def create_synthetic_mesh():
    """Same side-N UV fixture and selected bottom edge setup as test_gpu_host.create."""
    if bpy.context.object and bpy.context.object.mode == "EDIT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in list(bpy.context.scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    points = [(x / SIDE, y / SIDE, 0.) for y in range(SIDE + 1) for x in range(SIDE + 1)]
    faces = []
    for y in range(SIDE):
        for x in range(SIDE):
            a = y * (SIDE + 1) + x
            faces.append((a, a + 1, a + SIDE + 2, a + SIDE + 1))
    outside_first = len(points)
    points.extend(((2., 0., 0.), (3., 0., 0.), (3., 1., 0.), (2., 1., 0.)))
    faces.append(tuple(range(outside_first, outside_first + 4)))

    mesh = bpy.data.meshes.new("UVCKReaderGuiMesh")
    mesh.from_pydata(points, [], faces)
    mesh.uv_layers.new()
    obj = bpy.data.objects.new(OBJ_NAME, mesh)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")

    bm = bmesh.from_edit_mesh(mesh)
    addon.core.prepare_bm(bm)
    uvl = bm.loops.layers.uv.active
    scale = min(1., 16. / SIDE)
    for face in bm.faces:
        face.select = True
        face.uv_select = False
        for loop in face.loops:
            x, y, _ = loop.vert.co
            if face.index < SIDE * SIDE:
                loop[uvl].uv = (
                    x + scale * .025 * math.sin(y * math.tau) * (.3 + math.sin(x * math.pi)),
                    y + scale * .018 * math.sin(x * math.tau) * (.3 + math.sin(y * math.pi)),
                )
            else:
                loop[uvl].uv = (x, y)
                loop[uvl].pin_uv = True
            selected = (face.index < SIDE * SIDE and y == 0
                        and loop.link_loop_next.vert.co.y == 0)
            loop.uv_select_edge = selected
            loop.uv_select_vert = y == 0 and face.index < SIDE * SIDE
    bm.uv_select_sync_valid = True
    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    settings = bpy.context.scene.tool_settings
    settings.use_uv_select_sync = False
    settings.uv_select_mode = "EDGE"
    settings.use_edge_path_live_unwrap = False
    settings.transform_pivot_point = "MEDIAN_POINT"
    del bm, uvl, mesh, obj


def acquire_uv_area():
    window = bpy.context.window
    if window is None:
        raise RuntimeError("Factory GUI has no active window")
    area = next((a for a in window.screen.areas if a.type == "VIEW_3D"), None)
    if area is None:
        raise RuntimeError("Factory GUI has no VIEW_3D area to convert to a UV editor")
    area.type = "IMAGE_EDITOR"
    area.ui_type = "UV"
    region = next((r for r in area.regions if r.type == "WINDOW"), None)
    if region is None:
        raise RuntimeError("UV editor has no WINDOW region")
    return window, area, region


def uv_override():
    return bpy.context.temp_override(window=STATE["window"], area=STATE["area"], region=STATE["region"])


def snapshot():
    """Copy all acceptance data to numeric values; keep no BMesh wrappers on return."""
    obj = bpy.data.objects.get(OBJ_NAME)
    if obj is None or obj.type != "MESH" or obj.mode != "EDIT":
        raise RuntimeError("Synthetic edit mesh disappeared or left Edit Mode")
    bm = bmesh.from_edit_mesh(obj.data)
    addon.core.prepare_bm(bm)
    captures_before = addon.core.mesh_reader.diagnostics()["captures"]
    topo = addon.core.UVTopology(bm, addon.core.uv_layer(bm))
    expected = captures_before + (ARGS.reader == "native")
    if addon.core.mesh_reader.diagnostics()["captures"] != expected:
        raise AssertionError("UVTopology snapshot used an unexpected reader route")
    layer = addon.core.constraint_layer(bm)
    loops = topo.loops
    uv = np.array(topo.initial_uv, dtype=np.float64, copy=True)
    values = {
        "uv": uv,
        "marks": np.asarray([loop[layer] if layer is not None else 0 for loop in loops], dtype=np.int64),
        "pins": np.asarray([bool(loop[topo.uvl].pin_uv) for loop in loops], dtype=np.bool_),
        "selected": np.asarray([bool(loop.uv_select_vert) for loop in loops], dtype=np.bool_),
        "selected_edge": np.asarray([bool(loop.uv_select_edge) for loop in loops], dtype=np.bool_),
        "selected_face_ids": np.asarray(
            [face.index for face in bm.faces if any(loop.uv_select_vert for loop in face.loops)],
            dtype=np.int64),
        "xyz": np.asarray([tuple(v.co) for v in bm.verts], dtype=np.float64),
    }
    del topo, loops, layer, bm, obj
    return values


def assert_same_uv_marks(actual, expected, label):
    if not np.array_equal(actual["uv"], expected["uv"]):
        raise AssertionError(f"{label}: UV values differ")
    if not np.array_equal(actual["marks"], expected["marks"]):
        raise AssertionError(f"{label}: constraint marks differ")
    if not np.array_equal(actual["pins"], expected["pins"]):
        raise AssertionError(f"{label}: pin flags differ")
    if not np.array_equal(actual["xyz"], expected["xyz"]):
        raise AssertionError(f"{label}: mesh vertex coordinates differ")
    for key in ("selected", "selected_edge"):
        if not np.array_equal(actual[key], expected[key]):
            raise AssertionError(f"{label}: {key} differs")


def warm_read_and_redraw():
    # Exercise fresh BMesh readers and redraws between undo/redo stages. Every
    # wrapper is local to snapshot() and is discarded before this returns.
    first = snapshot()
    second = snapshot()
    if not np.array_equal(first["uv"], second["uv"]):
        raise AssertionError("Repeated UVTopology reads changed UV data")
    for area in STATE["window"].screen.areas:
        area.tag_redraw()


def set_uv_selection(target):
    bpy.context.scene.tool_settings.uv_select_mode = "VERTEX"
    obj = bpy.data.objects.get(OBJ_NAME)
    bm = bmesh.from_edit_mesh(obj.data)
    addon.core.prepare_bm(bm)
    for face in bm.faces:
        if face.index >= SIDE * SIDE:
            selected_face = False
        elif target == "whole":
            selected_face = True
        else:
            x = face.index % SIDE
            selected_face = x < SIDE // 2
        for loop in face.loops:
            loop.uv_select_vert = selected_face
            loop.uv_select_edge = False
    bm.uv_select_sync_valid = True
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    del bm, obj


class UVCK_OT_test_selection(bpy.types.Operator):
    """Make fixture selection a real undoable action before transforming."""
    bl_idname = "uv.uvck_test_selection"
    bl_label = "UVCK Test Selection"
    bl_options = {"UNDO", "INTERNAL"}
    target: bpy.props.StringProperty()

    def execute(self, context):
        set_uv_selection(self.target)
        return {"FINISHED"}


def affine_fit(before, after, selected):
    source = before[selected]
    destination = after[selected]
    design = np.column_stack((source, np.ones(len(source), dtype=np.float64)))
    if np.linalg.matrix_rank(design) < 3:
        raise AssertionError("Selected UV points are collinear or insufficient for an affine fit")
    coefficients, _, _, _ = np.linalg.lstsq(design, destination, rcond=None)
    matrix = coefficients[:2, :].T
    translation = coefficients[2, :]
    fitted = source @ matrix.T + translation
    residual = float(np.max(np.abs(fitted - destination)))
    return matrix, translation, np.linalg.svd(matrix, compute_uv=False), residual


def validate_transform(before, after, action, selected, parameters, label):
    if selected.shape != (len(before["uv"]),):
        raise AssertionError(f"{label}: selected mask shape mismatch")
    if not np.any(selected):
        raise AssertionError(f"{label}: no UV points selected")
    if not np.array_equal(after["uv"][~selected], before["uv"][~selected]):
        raise AssertionError(f"{label}: nonselected UV coordinates moved")
    if not np.array_equal(after["marks"], before["marks"]):
        raise AssertionError(f"{label}: transform changed constraint marks")
    if not np.array_equal(after["pins"], before["pins"]):
        raise AssertionError(f"{label}: transform changed pin flags")
    if not np.array_equal(after["selected"], before["selected"]):
        raise AssertionError(f"{label}: transform changed UV selection")
    if not np.array_equal(after["selected_edge"], before["selected_edge"]):
        raise AssertionError(f"{label}: transform changed UV edge selection")
    matrix, translation, singular, residual = affine_fit(before["uv"], after["uv"], selected)
    tolerance = 4e-5
    if residual > tolerance:
        raise AssertionError(f"{label}: selected UVs are not affine (residual={residual})")
    if action == "G":
        expected_translation = np.asarray(parameters[:2], dtype=np.float64)
        if not np.allclose(matrix, np.eye(2), atol=tolerance, rtol=0):
            raise AssertionError(f"{label}: translate linear part differs from identity: {matrix}")
        if not np.allclose(translation, expected_translation, atol=tolerance, rtol=0):
            raise AssertionError(f"{label}: translate delta differs: {translation}")
    elif action == "R":
        angle = math.atan2(matrix[1, 0], matrix[0, 0])
        if not np.allclose(singular, np.ones(2), atol=tolerance, rtol=0):
            raise AssertionError(f"{label}: rotation scale changed: {singular}")
        if abs(angle - parameters) > tolerance:
            raise AssertionError(f"{label}: rotation angle {angle} differs from {parameters}")
    else:
        expected = np.sort(np.abs(np.asarray(parameters[:2], dtype=np.float64)))[::-1]
        if not np.allclose(singular, expected, atol=tolerance, rtol=0):
            raise AssertionError(f"{label}: scale singular values {singular} differ from {expected}")
    return {"matrix": matrix.tolist(), "translation": translation.tolist(),
            "singular_values": singular.tolist(), "residual": residual}


STATE = {
    "registered": False,
    "window": None,
    "area": None,
    "region": None,
    "results": [],
    "reader_diagnostics": {},
    "source_before": None,
    "error": None,
    "stage": "preflight",
    "pending": None,
}


def setup():
    STATE["source_before"] = fingerprint()
    if (bpy.app.background or bpy.data.filepath
            or "--factory-startup" not in sys.argv or "--disable-autoexec" not in sys.argv):
        raise RuntimeError("Use a disposable --factory-startup --disable-autoexec GUI process with no loaded file")
    addon.register()
    bpy.utils.register_class(UVCK_OT_test_selection)
    STATE["registered"] = True
    reader = addon.core.mesh_reader
    reader._enabled = ARGS.reader == "native"
    reader._required = ARGS.reader == "native"
    if ARGS.reader == "native" and (reader._load() is None or not reader.diagnostics()["loaded"]):
        raise RuntimeError(f"Native BMesh reader is required: {reader.diagnostics()}")

    prefs_entry = bpy.context.preferences.addons.get(addon.__package__)
    if prefs_entry is None:
        # register() creates RNA classes, while addon_utils normally creates
        # this in-memory entry. The factory test bypasses addon_utils only.
        prefs_entry = bpy.context.preferences.addons.new()
        prefs_entry.module = addon.__package__
    if prefs_entry.preferences is None:
        raise RuntimeError("Add-on preferences RNA could not be instantiated")
    prefs_entry.preferences.auto_optimize = True
    prefs_entry.preferences.show_overlay = True
    if addon.overlay._uv_handle is None or addon.overlay._mesh_updated not in bpy.app.handlers.depsgraph_update_post:
        raise RuntimeError("UV constraint overlay handlers did not register")

    create_synthetic_mesh()
    window, area, region = acquire_uv_area()
    STATE.update(window=window, area=area, region=region)
    settings = bpy.context.scene.tool_settings
    if settings.use_uv_select_sync or settings.uv_select_mode != "EDGE" or settings.use_edge_path_live_unwrap:
        raise AssertionError("UV fixture tool settings are not sync-off, EDGE mode, and live-unwrap-off")

    before = snapshot()
    if np.count_nonzero(before["selected_edge"]) != SIDE:
        raise AssertionError("Fixture create() did not select exactly its bottom UV edges")
    with uv_override():
        if not bpy.ops.ed.undo_push.poll():
            raise AssertionError("Initial baseline undo_push is unavailable")
        result = bpy.ops.ed.undo_push(message="UVCK GUI baseline before mark test")
        if result != {"FINISHED"}:
            raise AssertionError(f"Initial baseline undo_push returned {result}")
    STATE["mark_before"] = before
    STATE["reader_diagnostics"]["setup"] = reader.diagnostics()
    warm_read_and_redraw()


def mark_and_auto():
    before_count = addon.core.mesh_reader.diagnostics()["captures"]
    with uv_override():
        operator = bpy.ops.uv.uvck_constrain_horizontal
        if not operator.poll():
            raise AssertionError("UV horizontal constraint poll failed in UV editor context")
        result = operator("EXEC_DEFAULT", True)
    if result != {"FINISHED"}:
        raise AssertionError(f"Horizontal mark+auto operator returned {result}")
    settings = bpy.context.scene.tool_settings
    if settings.use_uv_select_sync or settings.use_edge_path_live_unwrap:
        raise AssertionError("Mark+auto did not restore sync-off and live-unwrap-off settings")
    after = snapshot()
    if ARGS.reader == "native" and addon.core.mesh_reader.diagnostics()["captures"] <= before_count:
        raise AssertionError("Mark/auto operation did not use the required reader")
    horizontal = int(addon.core.C_HORIZONTAL)
    if int(np.count_nonzero(after["marks"] == horizontal)) != SIDE:
        raise AssertionError("Horizontal operator did not mark exactly the selected bottom edges")
    optimize = dict(addon.ops.last_optimize_result)
    if optimize.get("status") not in {"COMPLETED", "COMPLETED_LIMITED"}:
        raise AssertionError(f"Automatic optimize did not complete: {optimize}")
    STATE["mark_after"] = after
    STATE["mark_optimize"] = optimize
    STATE["reader_diagnostics"]["mark_after"] = addon.core.mesh_reader.diagnostics()
    warm_read_and_redraw()


def undo_mark():
    with uv_override():
        if not bpy.ops.ed.undo.poll():
            raise AssertionError("Undo poll failed after the single mark+auto action")
        result = bpy.ops.ed.undo()
    if result != {"FINISHED"}:
        raise AssertionError(f"Undo mark+auto returned {result}")
    restored = snapshot()
    assert_same_uv_marks(restored, STATE["mark_before"], "single undo of mark+auto")
    STATE["reader_diagnostics"]["mark_undo"] = addon.core.mesh_reader.diagnostics()
    warm_read_and_redraw()


def redo_mark():
    with uv_override():
        if not bpy.ops.ed.redo.poll():
            raise AssertionError("Redo poll failed after undo of mark+auto")
        result = bpy.ops.ed.redo()
    if result != {"FINISHED"}:
        raise AssertionError(f"Redo mark+auto returned {result}")
    restored = snapshot()
    assert_same_uv_marks(restored, STATE["mark_after"], "redo of mark+auto")
    STATE["reader_diagnostics"]["mark_redo"] = addon.core.mesh_reader.diagnostics()
    STATE["results"].append({"stage": "mark_auto_undo_redo", "passed": True,
                              "optimize": STATE["mark_optimize"]})
    warm_read_and_redraw()


TRANSFORMS = {
    "G": ((0.137, -0.083, 0.0), (0.137, -0.083, 0.0)),
    # Positive UV-editor rotation uses the view-facing axis. Verify this
    # signed expectation again in the independent Python-reader control run.
    "R": (math.radians(17.0), -math.radians(17.0)),
    "S": ((1.23, 0.81, 1.0), (1.23, 0.81, 1.0)),
}


def begin_transform(target, action):
    with uv_override():
        result = bpy.ops.uv.uvck_test_selection("EXEC_DEFAULT", True, target=target)
    if result != {"FINISHED"}:
        raise AssertionError(f"Fixture selection operator failed: {result}")
    before = snapshot()
    selected = before["selected"].copy()
    if np.count_nonzero(selected) < 4:
        raise AssertionError(f"{target} target has too few selected UV corners")
    selected_faces = before["selected_face_ids"]
    if target == "whole":
        if not np.array_equal(selected_faces, np.arange(SIDE * SIDE, dtype=np.int64)):
            raise AssertionError("Whole-target transform did not select exactly the connected target island")
    elif not (SIDE * SIDE // 4 <= len(selected_faces) <= 3 * SIDE * SIDE // 4):
        raise AssertionError(f"Partial target is not a half-island selection: {len(selected_faces)} faces")
    parameters, expected = TRANSFORMS[action]
    with uv_override():
        operator = {"G": bpy.ops.transform.translate,
                    "R": bpy.ops.transform.rotate,
                    "S": bpy.ops.transform.resize}[action]
        if not operator.poll():
            raise AssertionError(f"{action} poll failed in the UV editor context")
        result = operator("EXEC_DEFAULT", True, value=parameters)
    if result != {"FINISHED"}:
        raise AssertionError(f"{action} EXEC_DEFAULT returned {result}")
    after = snapshot()
    label = f"{target}_{action}"
    affine = validate_transform(before, after, action, selected, expected, label)
    STATE["pending"] = {"target": target, "action": action, "before": before, "after": after,
                        "affine": affine}
    STATE["reader_diagnostics"][label + "_after"] = addon.core.mesh_reader.diagnostics()
    warm_read_and_redraw()


def undo_transform():
    pending = STATE["pending"]
    label = f"{pending['target']}_{pending['action']}"
    with uv_override():
        if not bpy.ops.ed.undo.poll():
            raise AssertionError(f"Undo poll failed after {label}")
        result = bpy.ops.ed.undo()
    if result != {"FINISHED"}:
        raise AssertionError(f"Undo {label} returned {result}")
    restored = snapshot()
    assert_same_uv_marks(restored, pending["before"], f"undo {label}")
    STATE["reader_diagnostics"][label + "_undo"] = addon.core.mesh_reader.diagnostics()
    warm_read_and_redraw()


def redo_transform():
    pending = STATE["pending"]
    label = f"{pending['target']}_{pending['action']}"
    with uv_override():
        if not bpy.ops.ed.redo.poll():
            raise AssertionError(f"Redo poll failed after undo {label}")
        result = bpy.ops.ed.redo()
    if result != {"FINISHED"}:
        raise AssertionError(f"Redo {label} returned {result}")
    restored = snapshot()
    assert_same_uv_marks(restored, pending["after"], f"redo {label}")
    STATE["results"].append({"stage": label, "passed": True, "affine": pending["affine"]})
    STATE["results"][-1]["uv_sha256"] = hashlib.sha256(pending["after"]["uv"].tobytes()).hexdigest()
    STATE["reader_diagnostics"][label + "_redo"] = addon.core.mesh_reader.diagnostics()
    STATE["pending"] = None
    warm_read_and_redraw()


def finish():
    source_after = None
    try:
        source_after = fingerprint()
        if source_after != STATE["source_before"]:
            raise AssertionError("Candidate source files changed during the GUI checks")
    except BaseException:
        if STATE["error"] is None:
            STATE["error"] = traceback.format_exc()
    reader_diag = None
    try:
        reader_diag = addon.core.mesh_reader.diagnostics()
    except BaseException:
        if STATE["error"] is None:
            STATE["error"] = traceback.format_exc()
    if STATE["registered"]:
        try:
            addon.unregister()
            bpy.utils.unregister_class(UVCK_OT_test_selection)
            STATE["registered"] = False
        except BaseException:
            if STATE["error"] is None:
                STATE["error"] = traceback.format_exc()
    report = {
        "passed": STATE["error"] is None,
        "stage": STATE["stage"],
        "error_traceback": STATE["error"],
        "elapsed_seconds": round(time.monotonic() - STARTED, 3),
        "blender_version": bpy.app.version_string,
        "blender_build_hash": bpy.app.build_hash.decode(errors="replace"),
        "addon_root": str(ADDON_ROOT),
        "source_sha256_before": STATE["source_before"],
        "source_sha256_after": source_after,
        "source_unchanged": source_after == STATE["source_before"],
        "reader_required": ARGS.reader == "native",
        "reader_route": ARGS.reader,
        "reader_diagnostics_by_stage": STATE["reader_diagnostics"],
        "reader_diagnostics_final": reader_diag,
        "results": STATE["results"],
        "scene_saved": False,
        "preferences_saved": False,
        "real_mouse_gestures_tested": False,
        "transform_scope": "Confirmed G/R/S EXEC_DEFAULT operators in UV editor context; no mouse-modal gesture acceptance",
        "undo_scope": "One baseline push before mark; each fixture selection uses its own undoable selection operator; no synthetic pushes after edits",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8")
    print(json.dumps({"passed": report["passed"], "stage": report["stage"],
                      "elapsed_seconds": report["elapsed_seconds"],
                      "results": len(report["results"]), "output": str(OUTPUT)},
                     ensure_ascii=False), flush=True)
    bpy.app.timers.register(quit_timer, first_interval=0.2)


def quit_timer():
    if STATE["error"] is not None:
        os._exit(1)
    bpy.ops.wm.quit_blender()
    return None


def build_stages():
    stages = [("setup", setup), ("mark_auto", mark_and_auto),
              ("undo_mark_auto", undo_mark), ("redo_mark_auto", redo_mark)]
    for target in ("whole", "partial"):
        for action in ("G", "R", "S"):
            stages.extend(((f"{target}_{action}_execute", lambda t=target, a=action: begin_transform(t, a)),
                           (f"{target}_{action}_undo", undo_transform),
                           (f"{target}_{action}_redo", redo_transform)))
    return stages


STAGES = build_stages()
STAGE_INDEX = 0


def stage_timer():
    global STAGE_INDEX
    if time.monotonic() - STARTED > MAX_SECONDS:
        STATE["error"] = f"TimeoutError: GUI acceptance exceeded {MAX_SECONDS:.0f} seconds at {STATE['stage']}"
        finish()
        return None
    if STAGE_INDEX >= len(STAGES):
        finish()
        return None
    name, function = STAGES[STAGE_INDEX]
    STATE["stage"] = name
    try:
        function()
        STATE["reader_diagnostics"][name] = addon.core.mesh_reader.diagnostics()
        STAGE_INDEX += 1
    except BaseException:
        STATE["error"] = traceback.format_exc()
        finish()
        return None
    return 0.08


bpy.app.timers.register(stage_timer, first_interval=0.25)
