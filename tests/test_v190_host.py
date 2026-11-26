# SPDX-License-Identifier: GPL-3.0-or-later
"""Factory-Blender tests for 1.9.0: Solve Presets drawer, Distribute Edges,
partial quad-strip Quadrify, Constrained Drag, whole-island falloff Relax Brush,
headless UI draw, Blender RNA icons & zh_HANS i18n coverage.

Run: blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_v190_host.py
"""
import argparse
import ast
import json
import math
from pathlib import Path
import re
import sys

import bmesh
import bpy

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--addon-root", type=Path, default=ROOT / "uv_constraint_kit")
parser.add_argument("--require-native", action="store_true")
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])

sys.path.insert(0, str(args.addon_root.resolve().parent))
import uv_constraint_kit as addon
from uv_constraint_kit import (
    constrained_drag,
    core,
    distribute,
    ops,
    prefs,
    quadrify,
    relax_brush,
    safety,
    terms_zh,
    ui,
)


def clear_scene():
    if bpy.context.object and bpy.context.object.mode == "EDIT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in list(bpy.data.objects):
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if getattr(mesh, "users", 0) == 0 and isinstance(mesh, bpy.types.Mesh):
            bpy.data.meshes.remove(mesh)


def make_mesh(vertices, faces, uv_fn):
    clear_scene()
    mesh = bpy.data.meshes.new("V190Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.uv_layers.new(name="UVMap")
    obj = bpy.data.objects.new("V190Obj", mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(mesh)
    core.prepare_bm(bm)
    uvl = core.uv_layer(bm)
    for face in bm.faces:
        face.select = True
        face.uv_select = False
        for loop in face.loops:
            loop[uvl].uv = uv_fn(face.index, loop.vert.index, loop.vert.co)
            loop[uvl].pin_uv = False
            loop.uv_select_vert = False
            loop.uv_select_edge = False
    bm.uv_select_sync_valid = True
    ts = bpy.context.scene.tool_settings
    ts.use_uv_select_sync = False
    ts.uv_select_mode = "EDGE"
    ts.mesh_select_mode = (False, True, False)
    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    return obj


def topo_of(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    return core.UVTopology(bm, core.uv_layer(bm))


def uv_snapshot(obj):
    t = topo_of(obj)
    return [tuple(float(c) for c in l[t.uvl].uv) for l in t.loops]


def signed_area(points):
    return 0.5 * sum(a[0] * b[1] - a[1] * b[0] for a, b in zip(points, points[1:] + points[:1]))


class MockLayout:
    """Headless UILayout mock where leaf calls return None (matching real Blender)."""

    def __init__(self, calls, path="root"):
        self.calls = calls
        self.path = path
        self.enabled = True
        self.active = True
        self.scale_y = 1.0
        self.alert = False
        self.alignment = "EXPAND"
        self.use_property_split = False
        self.use_property_decorate = True

    def box(self):
        return MockLayout(self.calls, self.path + "/box")

    def row(self, *a, **k):
        return MockLayout(self.calls, self.path + "/row")

    def column(self, *a, **k):
        self.calls.append(("column", k))
        return MockLayout(self.calls, self.path + "/column")

    def split(self, *a, **k):
        return MockLayout(self.calls, self.path + "/split")

    def label(self, *a, **k):
        self.calls.append(("label", k))
        return None

    def prop(self, data, prop_name, *a, **k):
        assert hasattr(data, prop_name), f"Missing RNA property {prop_name} on {data}"
        self.calls.append(("prop", prop_name, k))
        return None

    def operator(self, idname, *a, **k):
        self.calls.append(("operator", idname, k))
        return MockLayout(self.calls, self.path + f"/op:{idname}")

    def separator(self, *a, **k):
        return None


def test_ui_headless_icons_and_i18n():
    # 1. Verify Pie Menu compatibility (all legacy IDs intact + 1.8/1.9 IDs present, Auto H/V removed).
    expected_ops = {
        "uv.uvck_constrain_horizontal",
        "uv.uvck_constrain_vertical",
        "uv.uvck_constrain_aligned",
        "uv.uvck_unconstrain",
        "uv.uvck_edit_constraints",
        "uv.uvck_optimize",
        "uv.uvck_symmetrize",
        "uv.uvck_stop_symmetry",
        "uv.uvck_circularize",
        "uv.uvck_rectify",
        "uv.uvck_quadrify",
        "uv.uvck_distribute_edges",
        "uv.uvck_constrained_drag",
        "uv.uvck_relax_brush",
    }
    for op_id in expected_ops:
        cat, name = op_id.split(".")
        type_name = f"{cat.upper()}_OT_{name}"
        assert hasattr(bpy.types, type_name), f"Missing operator {op_id} ({type_name})"
    assert not hasattr(bpy.types, "UV_OT_uvck_auto_constrain_hv"), "Dropped uv.uvck_auto_constrain_hv must not be registered"

    # 2. Verify every icon literal across UI/operator modules exists in Blender RNA.
    valid_icons = {
        item.identifier
        for item in bpy.types.UILayout.bl_rna.functions["operator"].parameters["icon"].enum_items
    }
    addon_dir = args.addon_root.resolve()
    for rel in ("ui.py", "prefs.py", "ops.py", "distribute.py", "shape_ops.py", "constrained_drag.py", "relax_brush.py"):
        text = (addon_dir / rel).read_text(encoding="utf8")
        for icon in set(re.findall(r'icon\s*=\s*["\']([A-Z0-9_]+)["\']', text)):
            assert icon in valid_icons, f"Invalid Blender icon {icon} in {rel}"

    # 3. Verify panel, menu, and preferences headless draw (collapsed vs expanded presets drawer).
    make_mesh(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
        [(0, 1, 2, 3)],
        lambda _f, _v, co: (co.x, co.y),
    )

    class FallbackPrefs:
        auto_optimize = True
        show_solve_presets = False
        preset_prevent_overlap = False
        preset_fix_boundary = False
        preset_use_density_map = True
        show_overlay = True
        uv_edge_width = 2.0
        color_horizontal = (0.5, 0.5, 1.0)
        color_vertical = (0.4, 0.8, 0.4)
        color_aligned = (1.0, 1.0, 0.0)
        color_circular = (1.0, 0.56, 0.22)
        color_rectangular = (1.0, 0.3, 0.58)
        tint_selected_edges = True
        color_selected = (0.0, 1.0, 1.0)
        selected_blend = 0.85
        symmetry_fill_color = (0.0, 1.0, 1.0)
        symmetry_fill_opacity = 0.12

    addon_prefs = ops._prefs(bpy.context)
    dummy_prefs = addon_prefs if addon_prefs is not None else FallbackPrefs()
    for prop_name in ("show_solve_presets", "preset_prevent_overlap", "preset_fix_boundary", "preset_use_density_map"):
        assert prop_name in prefs.UVCKPreferences.bl_rna.properties, prop_name

    for expanded in (False, True):
        for show_overlay in (False, True):
            dummy_prefs.show_solve_presets = expanded
            dummy_prefs.show_overlay = show_overlay
            calls = []

            class DummyPanel(ui._ConstraintButtons):
                layout = MockLayout(calls)

            if addon_prefs is None:
                class FakeAddon:
                    preferences = dummy_prefs
                class CtxProxy:
                    def __getattr__(self, item):
                        return getattr(bpy.context, item)
                ctx_proxy = CtxProxy()
                ctx_proxy.preferences = type("P", (), {"addons": {addon.__name__: FakeAddon()}})()
                DummyPanel().draw_constraint_buttons(ctx_proxy, DummyPanel.layout)
            else:
                DummyPanel().draw_constraint_buttons(bpy.context, DummyPanel.layout)

            drawn_props = [c[1] for c in calls if c[0] == "prop"]
            drawn_ops = [c[1] for c in calls if c[0] == "operator"]
            assert "uv.uvck_auto_constrain_hv" not in drawn_ops
            assert "uv.uvck_distribute_edges" in drawn_ops
            assert "uv.uvck_constrained_drag" in drawn_ops
            assert "uv.uvck_relax_brush" in drawn_ops
            assert "show_solve_presets" in drawn_props
            for key in ("preset_prevent_overlap", "preset_fix_boundary", "preset_use_density_map"):
                assert (key in drawn_props) == expanded

    menu_calls = []
    ui._draw_menu(type("M", (), {"layout": MockLayout(menu_calls)})(), bpy.context)
    menu_ops = [c[1] for c in menu_calls if c[0] == "operator"]
    assert "uv.uvck_auto_constrain_hv" not in menu_ops
    assert "uv.uvck_constrained_drag" in menu_ops
    assert "uv.uvck_relax_brush" in menu_ops
    assert "uv.uvck_distribute_edges" in menu_ops

    # 4. Verify zh_HANS translation coverage without Blender catalog clashes.
    view = bpy.context.preferences.view
    prev_lang, prev_iface = view.language, view.use_translate_interface
    try:
        view.language = "zh_HANS"
        view.use_translate_interface = True
        check_strings = [
            "Solve presets",
            "Distribute Edges",
            "Edge distribution",
            "Edge distribution: Even",
            "Edge distribution: Geometry",
            "Constrained Drag",
            "Drag offset",
            "Preserve island area",
            "Relax Brush",
            "Brush radius (px)",
            "Brush strength",
            "Step iterations",
        ]
        for msg in check_strings:
            assert msg in terms_zh.TERMS, f"Missing zh term: {msg}"
            translated = bpy.app.translations.pgettext_iface(msg, "Operator")
            assert translated == terms_zh.TERMS[msg], f"Translation mismatch for {msg}: {translated!r}"

        for rel in ("ui.py", "prefs.py"):
            tree = ast.parse((addon_dir / rel).read_text(encoding="utf8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "text" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str) and kw.value.value:
                            assert kw.value.value in terms_zh.TERMS, f"Untranslated UI text literal {kw.value.value!r} in {rel}"
    finally:
        view.language = prev_lang
        view.use_translate_interface = prev_iface


def test_solve_presets_wiring():
    verts = [(float(x), float(y), 0.0) for y in range(3) for x in range(3)]
    faces = [(0, 1, 4, 3), (1, 2, 5, 4), (3, 4, 7, 6), (4, 5, 8, 7)]
    uv_fn = lambda _fi, _vi, co: (0.1 * co.x + 0.01 * co.y, 0.1 * co.y + (0.025 if (co.x == 1.0 and co.y == 1.0) else 0.0))

    obj = make_mesh(verts, faces, uv_fn)
    t = topo_of(obj)
    border_loops = set(core.selection_border_loops(t, {0, 1, 2, 3}))
    border_vgroups = {g for p in border_loops for g in t.loop_edge_vgroups(p)}
    border_positions = {p for g in border_vgroups for p in t.vgroup_loops[g]}
    for lp in t.loops:
        if {lp.vert.index, lp.link_loop_next.vert.index} in ({3, 4}, {4, 5}):
            lp.uv_select_edge = True
    t.bm.uv_select_sync_valid = True
    before = uv_snapshot(obj)
    del t

    class MockPrefs:
        auto_optimize = True
        preset_prevent_overlap = False
        preset_fix_boundary = True
        preset_use_density_map = True

    orig_prefs = ops._prefs
    ops._prefs = lambda _ctx: MockPrefs()
    try:
        res = bpy.ops.uv.uvck_constrain_horizontal()
        assert "FINISHED" in res, res
    finally:
        ops._prefs = orig_prefs

    after = uv_snapshot(obj)
    assert any(math.dist(before[p], after[p]) > 1e-3 for p in range(len(before)) if p not in border_positions)
    for p in border_positions:
        assert math.dist(before[p], after[p]) < 1e-6


def test_distribute_edges():
    xs3d = [0.0, 1.0, 3.0, 6.0, 10.0]
    uv_xs = [0.0, 0.05, 0.12, 0.25, 1.0]
    verts = [(xs3d[x], float(y), 0.0) for y in range(3) for x in range(5)]
    verts += [(20.0, 0.0, 0.0), (21.0, 0.0, 0.0), (21.0, 1.0, 0.0), (20.0, 1.0, 0.0)]
    faces = [(y * 5 + x, y * 5 + x + 1, y * 5 + x + 6, y * 5 + x + 5) for y in range(2) for x in range(4)]
    faces.append((15, 16, 17, 18))

    def uv_fn(fi, vi, co):
        if fi == 8:
            return (1.5 + 0.1 * (co.x - 20.0), 0.1 * co.y)
        return (uv_xs[vi % 5], 0.25 * (vi // 5))

    for mode, expected_u in (("EVEN", (0.0, 0.25, 0.5, 0.75, 1.0)), ("GEOMETRY", (0.0, 0.1, 0.3, 0.6, 1.0))):
        obj = make_mesh(verts, faces, uv_fn)
        t = topo_of(obj)
        for lp in t.loops:
            if lp.face.index < 8 and lp.vert.index < 5 and lp.link_loop_next.vert.index < 5:
                lp.uv_select_edge = True
        t.bm.uv_select_sync_valid = True
        before_out = uv_snapshot(obj)[32:]
        area_before = core.island_area(t, set(range(8)))
        del t
        res = bpy.ops.uv.uvck_distribute_edges(mode=mode)
        assert "FINISHED" in res, res
        t = topo_of(obj)
        assert core.constraint_layer(t.bm) is None
        assert uv_snapshot(obj)[32:] == before_out
        assert abs(core.island_area(t, set(range(8))) / area_before - 1.0) < 1e-4
        bottom_u = {lp.vert.index: float(lp[t.uvl].uv.x) for lp in t.loops if lp.face.index < 8 and lp.vert.index < 5}
        for i, exp in enumerate(expected_u):
            assert abs(bottom_u[i] - exp) < 1e-4, (mode, i, bottom_u[i], exp)
        del t


def test_partial_quad_strip_quadrify():
    verts = [
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (3.0, 0.0, 0.0),
        (0.0, 1.0, 0.0), (1.0, 1.0, 0.0), (2.0, 1.0, 0.0), (3.0, 1.0, 0.0),
        (1.0, 2.0, 0.0), (2.0, 2.0, 0.0),
        (10.0, 0.0, 0.0), (11.0, 0.0, 0.0), (11.0, 1.0, 0.0), (10.0, 1.0, 0.0),
    ]
    faces = [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (4, 5, 8), (5, 6, 9, 8), (6, 7, 9), (10, 11, 12, 13)]
    uv_fn = lambda fi, _vi, co: (1.5 + 0.1 * (co.x - 10.0), 0.1 * co.y) if fi == 6 else (0.1 * co.x + 0.015 * co.y, 0.1 * co.y + 0.018 * math.sin(co.x * 1.1))

    obj = make_mesh(verts, faces, uv_fn)
    bpy.context.scene.tool_settings.uv_select_mode = "FACE"
    t = topo_of(obj)
    for fi in (0, 1, 2):
        t.bm.faces[fi].uv_select = True
    t.bm.uv_select_sync_valid = True
    before_out = uv_snapshot(obj)[-4:]
    del t

    res = quadrify.quadrify(bpy.context, obj, {0, 1, 2}, shape="EVEN", preserve_uv_connections=True, selected_faces={0, 1, 2})
    assert res["changed"] == 1 and res["islands"][0]["algorithm"] == "PARTIAL_QUAD_STRIP", res
    t = topo_of(obj)
    assert uv_snapshot(obj)[-4:] == before_out
    for fi in (0, 1, 2):
        pts = [tuple(float(c) for c in lp[t.uvl].uv) for lp in t.bm.faces[fi].loops]
        for a, b in zip(pts, pts[1:] + pts[:1]):
            assert min(abs(b[0] - a[0]), abs(b[1] - a[1])) < 1e-5
    for fi in (3, 4, 5):
        pts = [tuple(float(c) for c in lp[t.uvl].uv) for lp in t.bm.faces[fi].loops]
        assert signed_area(pts) > 0
    del t


def test_constrained_drag():
    # 3x3 quad grid + second untouched island.
    # Mark middle horizontal row (4)-(5)-(6)-(7) as C_HORIZONTAL and left column (0)-(4)-(8)-(12) as C_VERTICAL.
    verts = [(float(x), float(y), 0.0) for y in range(4) for x in range(4)]
    verts += [(10.0, 0.0, 0.0), (11.0, 0.0, 0.0), (11.0, 1.0, 0.0), (10.0, 1.0, 0.0)]
    faces = [(y * 4 + x, y * 4 + x + 1, y * 4 + x + 5, y * 4 + x + 4) for y in range(3) for x in range(3)]
    faces.append((16, 17, 18, 19))
    uv_fn = lambda fi, _vi, co: (1.5 + 0.1 * (co.x - 10.0), 0.1 * co.y) if fi == 9 else (0.1 * co.x, 0.1 * co.y)

    # Case A: Drag a single vertex (v5) on a horizontal constrained row up by +0.04.
    # The horizontal row must remain horizontal (at y=0.14), the vertical left column must remain vertical,
    # zero flips/overlaps, and the outside island stays untouched!
    obj = make_mesh(verts, faces, uv_fn)
    bpy.context.scene.tool_settings.uv_select_mode = "VERTEX"
    t = topo_of(obj)
    core.constraint_layer(t.bm, create=True)
    del t
    t = topo_of(obj)
    layer = core.constraint_layer(t.bm)
    for lp in t.loops:
        if lp.face.index < 9:
            vs = {lp.vert.index, lp.link_loop_next.vert.index}
            if vs in ({4, 5}, {5, 6}, {6, 7}):
                lp[layer] = core.C_HORIZONTAL
            if vs in ({0, 4}, {4, 8}, {8, 12}):
                lp[layer] = core.C_VERTICAL
            if lp.vert.index == 5:
                lp.uv_select_vert = True
    t.bm.uv_select_sync_valid = True
    before_out = uv_snapshot(obj)[-4:]
    del t

    res = bpy.ops.uv.uvck_constrained_drag(offset=(0.02, 0.04), fix_boundary=False, preserve_area=False)
    assert "FINISHED" in res, res
    t = topo_of(obj)
    assert uv_snapshot(obj)[-4:] == before_out
    v5_uv = next(tuple(float(c) for c in lp[t.uvl].uv) for lp in t.loops if lp.face.index < 9 and lp.vert.index == 5)
    assert abs(v5_uv[0] - 0.12) < 1e-4 and abs(v5_uv[1] - 0.14) < 1e-4, v5_uv
    row_ys = [float(lp[t.uvl].uv.y) for lp in t.loops if lp.face.index < 9 and lp.vert.index in (4, 5, 6, 7)]
    assert max(row_ys) - min(row_ys) < 1e-4, row_ys
    col_xs = [float(lp[t.uvl].uv.x) for lp in t.loops if lp.face.index < 9 and lp.vert.index in (0, 4, 8, 12)]
    assert max(col_xs) - min(col_xs) < 1e-4, col_xs
    probe = safety.AcceptanceProbe(core, t, set(range(9))).measure(t)
    assert probe["finite"] and probe["flips"] == 0 and probe["max_deviation"] <= 0.05, probe
    del t

    # Case B: Infeasible drag (dragging bottom border far past top border with fix_boundary=True on rest)
    # must reject safely and leave UVs byte-for-byte unchanged!
    obj2 = make_mesh(verts, faces, uv_fn)
    bpy.context.scene.tool_settings.uv_select_mode = "VERTEX"
    t2 = topo_of(obj2)
    for lp in t2.loops:
        if lp.face.index < 9 and lp.vert.index == 5:
            lp.uv_select_vert = True
    t2.bm.uv_select_sync_valid = True
    before2 = uv_snapshot(obj2)
    del t2
    out_rej = constrained_drag.constrained_drag(bpy.context, obj2, (0.0, 5.0), fix_boundary=True)
    assert out_rej["changed"] == 0 and out_rej["skipped"], out_rej
    assert uv_snapshot(obj2) == before2

    # Case C: Rigid whole-island drag translates cleanly without distortion.
    obj3 = make_mesh(verts, faces, uv_fn)
    bpy.context.scene.tool_settings.uv_select_mode = "FACE"
    t3 = topo_of(obj3)
    for fi in range(9):
        t3.bm.faces[fi].uv_select = True
    t3.bm.uv_select_sync_valid = True
    before3 = uv_snapshot(obj3)
    del t3
    out_rigid = constrained_drag.constrained_drag(bpy.context, obj3, (0.25, -0.15))
    assert out_rigid["changed"] == 1 and out_rigid["islands"][0]["mode"] == "RIGID", out_rigid
    after3 = uv_snapshot(obj3)
    assert after3[-4:] == before3[-4:]
    for p in range(len(before3) - 4):
        assert abs(after3[p][0] - (before3[p][0] + 0.25)) < 1e-5
        assert abs(after3[p][1] - (before3[p][1] - 0.15)) < 1e-5


def test_relax_brush_whole_island_falloff():
    # 4x4 distorted grid with a horizontal constraint on row y=2, a vertical constraint on col x=0,
    # a pinned corner (0,0), and a second untouched island.
    verts = [(float(x), float(y), 0.0) for y in range(5) for x in range(5)]
    verts += [(10.0, 0.0, 0.0), (11.0, 0.0, 0.0), (11.0, 1.0, 0.0), (10.0, 1.0, 0.0)]
    faces = [(y * 5 + x, y * 5 + x + 1, y * 5 + x + 6, y * 5 + x + 5) for y in range(4) for x in range(4)]
    faces.append((25, 26, 27, 28))

    def uv_fn(fi, vi, co):
        if fi == 16:
            return (2.0 + 0.1 * (co.x - 10.0), 0.1 * co.y)
        xi, yi = vi % 5, vi // 5
        u, v = 0.1 * xi, 0.1 * yi
        # Strongly distort interior vertices (keeping row y=2 strictly horizontal & col x=0 vertical).
        if 1 <= xi <= 3 and 1 <= yi <= 3:
            u += 0.032 if (xi + yi) % 2 == 0 else -0.025
            if yi != 2:
                v += 0.028 if xi == 2 else -0.020
        return (u, v)

    obj = make_mesh(verts, faces, uv_fn)
    t = topo_of(obj)
    core.constraint_layer(t.bm, create=True)
    del t
    t = topo_of(obj)
    layer = core.constraint_layer(t.bm)
    for lp in t.loops:
        if lp.face.index < 16:
            a, b = lp.vert.index, lp.link_loop_next.vert.index
            if a // 5 == 2 and b // 5 == 2:
                lp[layer] = core.C_HORIZONTAL
            if a % 5 == 0 and b % 5 == 0:
                lp[layer] = core.C_VERTICAL
            if lp.vert.index == 0:
                lp[t.uvl].pin_uv = True
    t.bm.uv_select_sync_valid = True
    before = uv_snapshot(obj)
    q_before = core.quality_metrics(t, set(range(16)))
    del t

    # Paint 4 smooth whole-island falloff dabs centered near (0.2, 0.2) with radius 0.18.
    for _ in range(4):
        res = relax_brush.apply_dab(
            bpy.context, obj,
            center_uv=(0.2, 0.2), radius_uv=(0.18, 0.18),
            strength=0.6, iterations=8,
            prevent_overlap=True, fix_boundary=False,
            use_density_map=True, respect_pins=True, preserve_area=True,
        )
        assert res["changed"] == 1, res

    t = topo_of(obj)
    after = uv_snapshot(obj)
    q_after = core.quality_metrics(t, set(range(16)))
    # Distortion genuinely reduced!
    assert q_after["worst_angle_deg"] < q_before["worst_angle_deg"], (q_before, q_after)
    # Outside island completely untouched!
    assert after[-4:] == before[-4:]
    # Pinned vertex (0,0) did not move!
    v0_uv = next(tuple(float(c) for c in lp[t.uvl].uv) for lp in t.loops if lp.face.index < 16 and lp.vert.index == 0)
    assert math.dist(v0_uv, (0.0, 0.0)) < 1e-6, v0_uv
    # Horizontal & vertical constraints remained exact!
    probe = safety.AcceptanceProbe(core, t, set(range(16)), check_other_islands=True)
    m = probe.measure(t)
    ov = probe.check_overlaps(t)
    assert m["finite"] and m["flips"] == 0 and m["zero_edges"] == 0 and m["max_deviation"] <= 0.05, m
    assert ov["novel_self_overlaps"] == 0 and ov["novel_island_overlaps"] == 0, ov
    # Verify smooth falloff: center vertex (v12) moved more than far corner vertex (v24).
    pos_by_v = {lp.vert.index: tuple(float(c) for c in lp[t.uvl].uv) for lp in t.loops if lp.face.index < 16}
    before_by_v = {}
    for p, lp in enumerate(t.loops):
        if lp.face.index < 16:
            before_by_v[lp.vert.index] = before[p]
    move_center = math.dist(pos_by_v[12], before_by_v[12])
    move_far = math.dist(pos_by_v[24], before_by_v[24])
    assert move_center > move_far, f"Expected smooth distance decay: center={move_center}, far={move_far}"
    del t


addon.register()
try:
    if args.require_native:
        assert addon.native_engine._load() is not None
    for test in (
        test_ui_headless_icons_and_i18n,
        test_solve_presets_wiring,
        test_distribute_edges,
        test_partial_quad_strip_quadrify,
        test_constrained_drag,
        test_relax_brush_whole_island_falloff,
    ):
        test()
finally:
    addon.unregister()
    clear_scene()

print(json.dumps(dict(
    passed=True,
    cases=6,
    blender=bpy.app.version_string,
    native_required=args.require_native,
    scene_saved=False,
    preferences_saved=False,
)))
