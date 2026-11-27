# SPDX-License-Identifier: GPL-3.0-or-later
"""Factory-Blender tests for 1.9.0: Solve Presets drawer, Distribute Edges,
whole-island Quad-Flow + partial quad-strip Quadrify, prevent-overlap reliability,
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
    core,
    distribute,
    ops,
    prefs,
    quadrify,
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
    }
    for op_id in expected_ops:
        cat, name = op_id.split(".")
        type_name = f"{cat.upper()}_OT_{name}"
        assert hasattr(bpy.types, type_name), f"Missing operator {op_id} ({type_name})"
    for dropped in ("UV_OT_uvck_auto_constrain_hv", "UV_OT_uvck_constrained_drag", "UV_OT_uvck_relax_brush"):
        assert not hasattr(bpy.types, dropped), f"Dropped {dropped} must not be registered"

    # 2. Verify every icon literal across UI/operator modules exists in Blender RNA.
    valid_icons = {
        item.identifier
        for item in bpy.types.UILayout.bl_rna.functions["operator"].parameters["icon"].enum_items
    }
    addon_dir = args.addon_root.resolve()
    for rel in ("ui.py", "prefs.py", "ops.py", "distribute.py", "shape_ops.py"):
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
            for dropped_op in ("uv.uvck_auto_constrain_hv", "uv.uvck_constrained_drag", "uv.uvck_relax_brush"):
                assert dropped_op not in drawn_ops
            assert "uv.uvck_distribute_edges" in drawn_ops
            assert "uv.uvck_quadrify" in drawn_ops
            assert "show_solve_presets" in drawn_props
            for key in ("preset_prevent_overlap", "preset_fix_boundary", "preset_use_density_map"):
                assert (key in drawn_props) == expanded

    menu_calls = []
    ui._draw_menu(type("M", (), {"layout": MockLayout(menu_calls)})(), bpy.context)
    menu_ops = [c[1] for c in menu_calls if c[0] == "operator"]
    for dropped_op in ("uv.uvck_auto_constrain_hv", "uv.uvck_constrained_drag", "uv.uvck_relax_brush"):
        assert dropped_op not in menu_ops
    assert "uv.uvck_distribute_edges" in menu_ops
    assert "uv.uvck_quadrify" in menu_ops

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


def test_whole_island_quad_flow_poled_island():
    # Pure-quad UV island containing an interior valence-3 pole
    # (rigid quad_grid.solve rejects poles, while whole-island QUAD_FLOW must succeed).
    pts = [
        (0.0, 0.0, 0.0),      # 0 (pole, val=3)
        (1.0, 0.0, 0.0),      # 1
        (0.8, 0.9, 0.0),      # 2
        (-0.5, 0.86, 0.0),    # 3
        (-1.1, 0.1, 0.0),     # 4
        (-0.5, -0.86, 0.0),   # 5
        (0.7, -0.8, 0.0),     # 6
        (1.9, 0.1, 0.0),      # 7
        (1.6, 1.6, 0.0),      # 8
        (-0.9, 1.7, 0.0),     # 9
        (-2.0, 0.2, 0.0),     # 10
        (-0.9, -1.7, 0.0),    # 11
        (1.4, -1.5, 0.0),     # 12
    ]
    qfaces = [
        (0, 1, 2, 3),
        (0, 3, 4, 5),
        (0, 5, 6, 1),
        (1, 7, 8, 2),
        (2, 8, 9, 3),
        (3, 9, 10, 4),
        (4, 10, 11, 5),
        (5, 11, 12, 6),
        (6, 12, 7, 1),
    ]
    obj = make_mesh(pts, qfaces, lambda _fi, _vi, co: (0.5 + 0.1 * co.x, 0.5 + 0.1 * co.y))
    res = quadrify.quadrify(bpy.context, obj, set(range(len(qfaces))),
                            shape="PER_FACE", average_shape=True,
                            respect_pins=True, prevent_overlap=True)
    assert res["changed"] == 1 and not res["skipped"], res
    assert res["islands"][0]["algorithm"] == "QUAD_FLOW", res
    assert res["islands"][0]["quality"]["flips"] == 0, res
    assert res["islands"][0]["area_error"] <= 1e-5, res


def test_prevent_overlap_self_only_ignores_neighbor_islands():
    # Prevent overlaps guards self-overlap within each island independently and
    # must never reject or shift an island just because it grazes a neighbor
    # island — both for Optimize and for Circularize.
    verts = [(float(x), float(y), 0.0) for y in range(3) for x in range(3)]
    verts += [(5.0, 0.0, 0.0), (6.0, 0.0, 0.0), (6.0, 2.0, 0.0), (5.0, 2.0, 0.0)]
    faces = [(y * 3 + x, y * 3 + x + 1, y * 3 + x + 4, y * 3 + x + 3) for y in range(2) for x in range(2)]
    faces.append((9, 10, 11, 12))

    def uv_fn(fi, vi, co):
        if fi == 4:
            return (0.205 + 0.1 * (co.x - 5.0), 0.0 + 0.1 * co.y)
        xi, yi = vi % 3, vi // 3
        u = 0.1 * xi - 0.03 * yi
        v = 0.1 * yi
        return (u, v)

    obj = make_mesh(verts, faces, uv_fn)
    t = topo_of(obj)
    core.constraint_layer(t.bm, create=True)
    del t
    t = topo_of(obj)
    layer = core.constraint_layer(t.bm)
    for lp in t.loops:
        if lp.face.index < 4:
            vs = {lp.vert.index, lp.link_loop_next.vert.index}
            if vs in ({2, 5}, {5, 8}):
                lp[layer] = core.C_VERTICAL
            if vs in ({0, 1}, {1, 2}, {6, 7}, {7, 8}):
                lp[layer] = core.C_HORIZONTAL
    for fi in range(4):
        t.bm.faces[fi].select = True
        t.bm.faces[fi].uv_select = True
    t.bm.faces[4].select = False
    t.bm.faces[4].uv_select = False
    t.bm.uv_select_sync_valid = True
    bpy.context.scene.tool_settings.use_uv_select_sync = True
    before_neighbor = uv_snapshot(obj)[-4:]
    before_center_x = sum(uv[0] for uv in uv_snapshot(obj)[:-4]) / (len(uv_snapshot(obj)) - 4)
    del t

    res = bpy.ops.uv.uvck_optimize("EXEC_DEFAULT", prevent_overlap=True, prevent_flips=True, use_density_map=True)
    assert "FINISHED" in res, res
    last = ops.last_optimize_result
    assert last["status"] == "COMPLETED" and last["max_deviation"] <= 0.05, last
    assert uv_snapshot(obj)[-4:] == before_neighbor
    after_center_x = sum(uv[0] for uv in uv_snapshot(obj)[:-4]) / (len(uv_snapshot(obj)) - 4)
    assert abs(after_center_x - before_center_x) < 0.02, (before_center_x, after_center_x)
    t = topo_of(obj)
    probe = safety.AcceptanceProbe(core, t, {0, 1, 2, 3}, check_other_islands=False)
    ov = probe.check_overlaps(t, include_existing=True)
    assert ov["self_overlaps"] == 0, ov
    del t

    # Circularize with prevent_overlap=True next to a neighbor island must also
    # succeed in place without rejecting on cross-island overlap.
    from uv_constraint_kit import circle
    obj2 = make_mesh(verts, faces, uv_fn)
    t = topo_of(obj2)
    for lp in t.loops:
        lp.uv_select_edge = lp.uv_select_vert = False
    for p in core.selection_border_loops(t, {0, 1, 2, 3}):
        lp = t.loops[p]
        lp.edge.select = True
        lp.uv_select_edge = lp.uv_select_vert = True
        lp.link_loop_next.uv_select_vert = True
    t.bm.uv_select_sync_valid = True
    bpy.context.scene.tool_settings.use_uv_select_sync = True
    bpy.context.scene.tool_settings.mesh_select_mode = (False, True, False)
    before_neighbor2 = uv_snapshot(obj2)[-4:]
    del t
    cres = circle.circularize(bpy.context, obj2, {0, 1, 2, 3}, spacing="EVEN",
                              iterations=150, respect_pins=True, prevent_overlap=True,
                              selected_only=True)
    assert cres["changed"] == 1 and not cres["skipped"], cres
    assert uv_snapshot(obj2)[-4:] == before_neighbor2
    t = topo_of(obj2)
    probe2 = safety.AcceptanceProbe(core, t, {0, 1, 2, 3}, check_other_islands=False)
    ov2 = probe2.check_overlaps(t, include_existing=True)
    assert ov2["self_overlaps"] == 0, ov2
    del t


def test_stepped_border_prevent_overlap_no_foldback():
    # Two vertical border runs separated by a narrow 1-edge horizontal step must
    # straighten cleanly under prevent_overlap=True without 180-deg edge
    # fold-back or corner stretch explosion (bull_right_18 regression).
    verts = []
    vid = {}
    for y in range(13):
        cols = 7 if y <= 6 else 6
        for x in range(cols):
            vid[(x, y)] = len(verts)
            px = float(x) * 0.025 if x <= 5 else 5 * 0.025 + 0.006
            py = float(y) * 0.025
            verts.append((px, py, 0.0))
    faces = []
    for y in range(12):
        max_x = 6 if y < 6 else 5
        for x in range(max_x):
            faces.append((vid[(x, y)], vid[(x + 1, y)], vid[(x + 1, y + 1)], vid[(x, y + 1)]))

    def uv_fn(_fi, _vi, co):
        u = co.x + 0.009 * math.sin((co.y - 0.15) * 9.0) + 0.06 * (co.y - 0.15)
        v = co.y
        return (u, v)

    for prevent_flips in (False, True):
        obj = make_mesh(verts, faces, uv_fn)
        t = topo_of(obj)
        core.constraint_layer(t.bm, create=True)
        del t
        t = topo_of(obj)
        layer = core.constraint_layer(t.bm)
        upper_v = {(vid[(5, y)], vid[(5, y + 1)]) for y in range(6, 12)}
        lower_v = {(vid[(6, y)], vid[(6, y + 1)]) for y in range(0, 6)}
        target_edges = {frozenset(pair) for pair in (upper_v | lower_v)}
        for lp in t.loops:
            if frozenset((lp.vert.index, lp.link_loop_next.vert.index)) in target_edges:
                lp[layer] = core.C_VERTICAL
        for f in t.bm.faces:
            f.select = True
            f.uv_select = True
        t.bm.uv_select_sync_valid = True
        bpy.context.scene.tool_settings.use_uv_select_sync = True
        del t

        res = bpy.ops.uv.uvck_optimize(
            "EXEC_DEFAULT",
            prevent_overlap=True,
            prevent_flips=prevent_flips,
            use_density_map=True,
        )
        assert "FINISHED" in res, res
        last = ops.last_optimize_result
        assert last["status"] == "COMPLETED" and last["max_deviation"] <= 0.05, last

        t = topo_of(obj)
        v_uv = {}
        for lp in t.loops:
            v_uv[lp.vert.index] = (float(lp[t.uvl].uv.x), float(lp[t.uvl].uv.y))
        for y in range(6, 12):
            assert v_uv[vid[(5, y + 1)]][1] > v_uv[vid[(5, y)]][1] + 1e-3, (
                prevent_flips, y, v_uv[vid[(5, y)]], v_uv[vid[(5, y + 1)]]
            )
        for y in range(0, 6):
            assert v_uv[vid[(6, y + 1)]][1] > v_uv[vid[(6, y)]][1] + 1e-3, (
                prevent_flips, y, v_uv[vid[(6, y)]], v_uv[vid[(6, y + 1)]]
            )
        p5 = v_uv[vid[(5, 6)]]
        p6 = v_uv[vid[(6, 6)]]
        step_len = math.hypot(p6[0] - p5[0], p6[1] - p5[1])
        assert p6[0] > p5[0] and step_len < 0.015, (prevent_flips, p5, p6, step_len)
        probe = safety.AcceptanceProbe(core, t, set(range(len(faces))), check_other_islands=False)
        ov = probe.check_overlaps(t, include_existing=True)
        assert ov["self_overlaps"] == 0, ov
        qm = core.quality_metrics(t, set(range(len(faces))))
        assert qm["flips"] == 0 and qm["edge_spread"] < 1.0, (prevent_flips, qm)
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
        test_whole_island_quad_flow_poled_island,
        test_prevent_overlap_self_only_ignores_neighbor_islands,
        test_stepped_border_prevent_overlap_no_foldback,
    ):
        test()
finally:
    addon.unregister()
    clear_scene()

print(json.dumps(dict(
    passed=True,
    cases=7,
    blender=bpy.app.version_string,
    native_required=args.require_native,
    scene_saved=False,
    preferences_saved=False,
)))
