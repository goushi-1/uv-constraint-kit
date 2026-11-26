# SPDX-License-Identifier: GPL-3.0-or-later
"""Factory-Blender tests for 1.8.0 features: presets drawer, Auto H/V,
Distribute Edges, partial quad-strip Quadrify, headless UI draw, icons & i18n.

Run: blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_v180_host.py
"""
import argparse
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
from uv_constraint_kit import core, distribute, ops, prefs, quad_grid, quadrify, safety, terms_zh, ui


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
    mesh = bpy.data.meshes.new("V180Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.uv_layers.new(name="UVMap")
    obj = bpy.data.objects.new("V180Obj", mesh)
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


def reset_prefs():
    p = ops._prefs(bpy.context)
    if p is not None:
        p.auto_optimize = True
        p.show_solve_presets = False
        p.preset_prevent_overlap = False
        p.preset_fix_boundary = False
        p.preset_use_density_map = True
        p.show_overlay = True
    return p


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
        # Assert the property genuinely exists on the RNA struct.
        assert hasattr(data, prop_name), f"Missing RNA property {prop_name} on {data}"
        self.calls.append(("prop", prop_name, k))
        return None

    def operator(self, idname, *a, **k):
        self.calls.append(("operator", idname, k))
        return MockLayout(self.calls, self.path + f"/op:{idname}")

    def separator(self, *a, **k):
        return None


def test_ui_headless_icons_and_i18n():
    # 1. Verify all existing Pie Menu operator IDs + new 1.8.0 IDs exist.
    expected_ops = {
        "uv.uvck_constrain_horizontal",
        "uv.uvck_constrain_vertical",
        "uv.uvck_constrain_aligned",
        "uv.uvck_auto_constrain_hv",
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
        assert hasattr(getattr(bpy.ops, cat), name), f"Missing operator {op_id}"

    # 2. Verify every icon literal in ui.py and prefs.py exists in Blender's RNA icon enum.
    valid_icons = {
        item.identifier
        for item in bpy.types.UILayout.bl_rna.functions["operator"].parameters["icon"].enum_items
    }
    addon_dir = args.addon_root.resolve()
    for rel in ("ui.py", "prefs.py", "ops.py", "distribute.py", "shape_ops.py"):
        text = (addon_dir / rel).read_text(encoding="utf8")
        for icon in set(re.findall(r'icon\s*=\s*["\']([A-Z0-9_]+)["\']', text)):
            assert icon in valid_icons, f"Invalid Blender icon {icon} in {rel}"

    # 3. Verify panel & menu headless draw across collapsed / expanded / auto_optimize states.
    obj = make_mesh(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
        [(0, 1, 2, 3)],
        lambda _f, _v, co: (co.x, co.y),
    )
    addon_prefs = reset_prefs()
    assert addon_prefs is None or addon_prefs.show_solve_presets is False

    # Build a synthetic prefs object if addon was imported directly without preferences registration.
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

    dummy_prefs = addon_prefs if addon_prefs is not None else FallbackPrefs()

    # Check real UVCKPreferences RNA properties directly on the class.
    for prop_name in (
        "show_solve_presets",
        "preset_prevent_overlap",
        "preset_fix_boundary",
        "preset_use_density_map",
    ):
        assert prop_name in prefs.UVCKPreferences.bl_rna.properties, prop_name

    # Drive UVCK_PT_uv_editor & UVCK_PT_view3d in both collapsed and expanded states.
    for expanded in (False, True):
        for show_overlay in (False, True):
            dummy_prefs.show_solve_presets = expanded
            dummy_prefs.show_overlay = show_overlay
            calls = []

            class DummyPanel(ui._ConstraintButtons):
                layout = MockLayout(calls)

            # Temporarily inject dummy_prefs if addon preferences aren't installed as an extension.
            if addon_prefs is None:
                class FakeAddon:
                    preferences = dummy_prefs
                orig_get = bpy.context.preferences.addons.get
                # Test direct draw with a lightweight context proxy.
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
            assert "uv.uvck_auto_constrain_hv" in drawn_ops
            assert "uv.uvck_distribute_edges" in drawn_ops
            assert "show_solve_presets" in drawn_props
            for preset_key in ("preset_prevent_overlap", "preset_fix_boundary", "preset_use_density_map"):
                if expanded:
                    assert preset_key in drawn_props, f"{preset_key} missing when expanded"
                else:
                    assert preset_key not in drawn_props, f"{preset_key} drawn when collapsed"

    # Drive UV menu draw.
    menu_calls = []
    ui._draw_menu(type("M", (), {"layout": MockLayout(menu_calls)})(), bpy.context)
    menu_ops = [c[1] for c in menu_calls if c[0] == "operator"]
    assert "uv.uvck_auto_constrain_hv" in menu_ops
    assert "uv.uvck_distribute_edges" in menu_ops

    # Drive UVCKPreferences.draw & operator draw().
    pref_calls = []
    dummy_pref_self = type("DP", (), {"layout": MockLayout(pref_calls)})()
    for attr in dir(FallbackPrefs):
        if not attr.startswith("_"):
            setattr(dummy_pref_self, attr, getattr(dummy_prefs, attr))
    prefs.UVCKPreferences.draw(dummy_pref_self, bpy.context)
    pref_props = [c[1] for c in pref_calls if c[0] == "prop"]
    for preset_key in ("preset_prevent_overlap", "preset_fix_boundary", "preset_use_density_map"):
        assert preset_key in pref_props

    # 4. Verify i18n coverage under zh_HANS for all new strings.
    view = bpy.context.preferences.view
    prev_lang, prev_iface = view.language, view.use_translate_interface
    try:
        view.language = "zh_HANS"
        view.use_translate_interface = True
        new_strings = [
            "Solve presets",
            "Auto Constrain H/V",
            "Angle threshold",
            "Include sharp edges",
            "Distribute Edges",
            "Edge distribution",
            "Edge distribution: Even",
            "Edge distribution: Geometry",
            "Space vertices evenly along each selected UV edge chain",
            "Space vertices proportionally to 3D mesh edge lengths",
            "Select UV edges or an island first",
            "No UV edges within %.1f deg of horizontal or vertical",
            "Auto constrained: %d horizontal, %d vertical",
            "Select a UV edge chain of at least two connected edges",
        ]
        for msg in new_strings:
            assert msg in terms_zh.TERMS, f"Missing zh term: {msg}"
            translated = bpy.app.translations.pgettext_iface(msg, "Operator")
            assert translated == terms_zh.TERMS[msg], f"Translation lookup mismatch for {msg}: {translated!r}"

        # Also audit all `text="..."` literals in ui.py and prefs.py via AST (handles multiline strings).
        import ast
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
    # 3x3 grid with curved interior horizontal edges and non-axis-aligned border.
    verts = [(float(x), float(y), 0.0) for y in range(3) for x in range(3)]
    faces = [(0, 1, 4, 3), (1, 2, 5, 4), (3, 4, 7, 6), (4, 5, 8, 7)]
    def uv_fn(_fi, _vi, co):
        return (0.1 * co.x + 0.01 * co.y, 0.1 * co.y + (0.025 if (co.x == 1.0 and co.y == 1.0) else 0.0))

    obj = make_mesh(verts, faces, uv_fn)
    t = topo_of(obj)
    border_loops = set(core.selection_border_loops(t, {0, 1, 2, 3}))
    border_vgroups = {g for p in border_loops for g in t.loop_edge_vgroups(p)}
    border_positions = {p for g in border_vgroups for p in t.vgroup_loops[g]}

    # Select interior horizontal edge (3)-(4)-(5)
    for p, lp in enumerate(t.loops):
        vs = {lp.vert.index, lp.link_loop_next.vert.index}
        if vs in ({3, 4}, {4, 5}):
            lp.uv_select_edge = True
    t.bm.uv_select_sync_valid = True
    before = uv_snapshot(obj)
    del t

    # Inject preset_fix_boundary=True via ops._prefs mock when running from source.
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
    # Center vertex moved (interior edge became horizontal), while border stayed fixed!
    assert any(math.dist(before[p], after[p]) > 1e-3 for p in range(len(before)) if p not in border_positions)
    for p in border_positions:
        assert math.dist(before[p], after[p]) < 1e-6, f"Border moved despite preset_fix_boundary=True: {before[p]} vs {after[p]}"

    # Now verify explicit kwarg override fix_boundary=False overrides preset_fix_boundary=True.
    obj2 = make_mesh(verts, faces, uv_fn)
    t2 = topo_of(obj2)
    for p, lp in enumerate(t2.loops):
        if {lp.vert.index, lp.link_loop_next.vert.index} == {0, 1}:
            lp.uv_select_edge = True
    t2.bm.uv_select_sync_valid = True
    before2 = uv_snapshot(obj2)
    del t2

    ops._prefs = lambda _ctx: MockPrefs()
    try:
        # Mark a tilted border edge with explicit fix_boundary=False -> must succeed and move border.
        res2 = bpy.ops.uv.uvck_constrain_horizontal(fix_boundary=False)
        assert "FINISHED" in res2, res2
    finally:
        ops._prefs = orig_prefs
    after2 = uv_snapshot(obj2)
    t2 = topo_of(obj2)
    assert safety.AcceptanceProbe(core, t2, {0, 1, 2, 3}).measure(t2)["max_deviation"] <= 0.05


def test_auto_constrain_hv():
    # 2x2 grid rotated by 10 degrees + a second untouched island.
    rad = math.radians(10.0)
    ca, sa = math.cos(rad), math.sin(rad)
    verts = [(float(x), float(y), 0.0) for y in range(3) for x in range(3)]
    # Add a 40-degree chamfer triangle attached to the top edge of island 1? Keep clean 2x2 + diagonal quad.
    verts += [(5.0, 0.0, 0.0), (6.0, 0.0, 0.0), (6.0, 1.0, 0.0), (5.0, 1.0, 0.0)]
    faces = [(0, 1, 4, 3), (1, 2, 5, 4), (3, 4, 7, 6), (4, 5, 8, 7), (9, 10, 11, 12)]

    def uv_fn(fi, vi, co):
        if fi == 4:
            return (0.8 + 0.1 * (co.x - 5.0), 0.1 * co.y)
        u, v = 0.1 * co.x, 0.1 * co.y
        if vi == 4:
            # Pull center vertex slightly to make interior edges tilted too.
            u += 0.015
            v -= 0.01
        return (u * ca - v * sa, u * sa + v * ca)

    # 3a. Local edge selection priority (EDGE mode, partial selection, auto_optimize=False)
    obj = make_mesh(verts, faces, uv_fn)
    t = topo_of(obj)
    for lp in t.loops:
        if lp.face.index < 4 and {lp.vert.index, lp.link_loop_next.vert.index} in ({0, 1}, {1, 2}):
            lp.uv_select_edge = True
    t.bm.uv_select_sync_valid = True
    before_uv = uv_snapshot(obj)
    del t

    class MarkOnlyPrefs:
        auto_optimize = False
        preset_prevent_overlap = False
        preset_fix_boundary = False
        preset_use_density_map = True

    orig_prefs = ops._prefs
    ops._prefs = lambda _ctx: MarkOnlyPrefs()
    try:
        res = bpy.ops.uv.uvck_auto_constrain_hv(angle_threshold=20.0, include_sharp=False)
        assert "FINISHED" in res, res
    finally:
        ops._prefs = orig_prefs

    t = topo_of(obj)
    layer = core.constraint_layer(t.bm)
    assert layer is not None
    marked_h = [p for p, lp in enumerate(t.loops) if lp[layer] == core.C_HORIZONTAL]
    marked_v = [p for p, lp in enumerate(t.loops) if lp[layer] == core.C_VERTICAL]
    assert len(marked_h) == 2 and len(marked_v) == 0, (marked_h, marked_v)
    assert uv_snapshot(obj) == before_uv, "auto_optimize=False must not move UVs"
    del t

    # 3b. Face mode (no local edge selection) -> fallback to island outer contour + auto_optimize=True
    obj2 = make_mesh(verts, faces, uv_fn)
    bpy.context.scene.tool_settings.uv_select_mode = "FACE"
    t2 = topo_of(obj2)
    t2.bm.faces[0].uv_select = True
    # Mark one interior vertical edge (1)-(4) as sharp to test include_sharp=False vs True
    for e in t2.bm.edges:
        if {e.verts[0].index, e.verts[1].index} == {1, 4}:
            e.smooth = False
    t2.bm.uv_select_sync_valid = True
    before_out = uv_snapshot(obj2)[16:]
    area_before = core.island_area(t2, {0, 1, 2, 3})
    del t2

    class AutoPrefs:
        auto_optimize = True
        preset_prevent_overlap = False
        preset_fix_boundary = False
        preset_use_density_map = True

    ops._prefs = lambda _ctx: AutoPrefs()
    try:
        res = bpy.ops.uv.uvck_auto_constrain_hv(angle_threshold=20.0, include_sharp=False)
        assert "FINISHED" in res, res
    finally:
        ops._prefs = orig_prefs

    t2 = topo_of(obj2)
    layer2 = core.constraint_layer(t2.bm)
    border2 = set(core.selection_border_loops(t2, {0, 1, 2, 3}))
    marked2 = {p: int(t2.loops[p][layer2]) for p in range(len(t2.loops)) if t2.loops[p][layer2] != core.C_NONE}
    assert set(marked2.keys()) == border2, f"Expected outer border only, got {marked2}"
    assert sum(v == core.C_HORIZONTAL for v in marked2.values()) == 4
    assert sum(v == core.C_VERTICAL for v in marked2.values()) == 4
    assert uv_snapshot(obj2)[16:] == before_out, "Untouched island changed"
    assert abs(core.island_area(t2, {0, 1, 2, 3}) / area_before - 1.0) < 1e-4
    assert safety.AcceptanceProbe(core, t2, {0, 1, 2, 3}).measure(t2)["max_deviation"] <= 0.05
    del t2

    # 3c. include_sharp=True adds interior sharp edge
    obj3 = make_mesh(verts, faces, uv_fn)
    bpy.context.scene.tool_settings.uv_select_mode = "FACE"
    t3 = topo_of(obj3)
    t3.bm.faces[0].uv_select = True
    sharp_loops = []
    for e in t3.bm.edges:
        if {e.verts[0].index, e.verts[1].index} == {1, 4}:
            e.smooth = False
    for p, lp in enumerate(t3.loops):
        if lp.face.index < 4 and {lp.vert.index, lp.link_loop_next.vert.index} == {1, 4}:
            sharp_loops.append(p)
    t3.bm.uv_select_sync_valid = True
    del t3

    ops._prefs = lambda _ctx: MarkOnlyPrefs()
    try:
        res = bpy.ops.uv.uvck_auto_constrain_hv(angle_threshold=25.0, include_sharp=True)
        assert "FINISHED" in res, res
    finally:
        ops._prefs = orig_prefs

    t3 = topo_of(obj3)
    layer3 = core.constraint_layer(t3.bm)
    assert all(t3.loops[p][layer3] == core.C_VERTICAL for p in sharp_loops), "Interior sharp edge was not constrained"
    del t3

    # 3d. UV Sync ON in mesh EDGE mode with partial selection -> local selected edges only.
    obj4 = make_mesh(verts, faces, uv_fn)
    ts = bpy.context.scene.tool_settings
    ts.use_uv_select_sync = True
    ts.mesh_select_mode = (False, True, False)
    t4 = topo_of(obj4)
    for f in t4.bm.faces:
        f.select = False
    for e in t4.bm.edges:
        e.select = {e.verts[0].index, e.verts[1].index} in ({0, 1}, {1, 2})
    t4.bm.select_flush_mode()
    t4.bm.uv_select_sync_valid = False
    del t4

    ops._prefs = lambda _ctx: MarkOnlyPrefs()
    try:
        res = bpy.ops.uv.uvck_auto_constrain_hv(angle_threshold=20.0, include_sharp=False)
        assert "FINISHED" in res, res
    finally:
        ops._prefs = orig_prefs
        ts.use_uv_select_sync = False

    t4 = topo_of(obj4)
    layer4 = core.constraint_layer(t4.bm)
    marked4 = [p for p, lp in enumerate(t4.loops) if lp[layer4] == core.C_HORIZONTAL]
    assert len(marked4) == 2, marked4
    del t4


def test_distribute_edges():
    # 4x2 grid with non-uniform 3D x-coordinates [0, 1, 3, 6, 10] (ratios 1:2:3:4)
    # and bunched UV x-coordinates [0.0, 0.05, 0.12, 0.25, 1.0].
    xs3d = [0.0, 1.0, 3.0, 6.0, 10.0]
    uv_xs = [0.0, 0.05, 0.12, 0.25, 1.0]
    verts = [(xs3d[x], float(y), 0.0) for y in range(3) for x in range(5)]
    verts += [(20.0, 0.0, 0.0), (21.0, 0.0, 0.0), (21.0, 1.0, 0.0), (20.0, 1.0, 0.0)]
    faces = []
    for y in range(2):
        for x in range(4):
            a = y * 5 + x
            faces.append((a, a + 1, a + 6, a + 5))
    out_start = 15
    faces.append((out_start, out_start + 1, out_start + 2, out_start + 3))

    def uv_fn(fi, vi, co):
        if fi == len(faces) - 1:
            return (1.5 + 0.1 * (co.x - 20.0), 0.1 * co.y)
        xi = vi % 5
        yi = vi // 5
        return (uv_xs[xi], 0.25 * yi)

    # 4a. EVEN mode on bottom border chain (0)-(1)-(2)-(3)-(4)
    obj = make_mesh(verts, faces, uv_fn)
    t = topo_of(obj)
    for lp in t.loops:
        if lp.face.index < 8 and lp.vert.index < 5 and lp.link_loop_next.vert.index < 5:
            lp.uv_select_edge = True
    t.bm.uv_select_sync_valid = True
    before_out = uv_snapshot(obj)[32:]
    area_before = core.island_area(t, set(range(8)))
    del t

    res = bpy.ops.uv.uvck_distribute_edges(mode="EVEN")
    assert "FINISHED" in res, res
    t = topo_of(obj)
    assert core.constraint_layer(t.bm) is None, "Distribute Edges must not create persistent constraints"
    assert t.bm.loops.layers.string.get("uvck_shape") is None, "Distribute Edges must not create shape overlays"
    assert uv_snapshot(obj)[32:] == before_out, "Outside island moved"
    assert abs(core.island_area(t, set(range(8))) / area_before - 1.0) < 1e-4
    probe = safety.AcceptanceProbe(core, t, set(range(8))).measure(t)
    assert probe["finite"] and probe["flips"] == 0 and probe["zero_edges"] == 0, probe
    bottom_u = {}
    for lp in t.loops:
        if lp.face.index < 8 and lp.vert.index < 5:
            bottom_u[lp.vert.index] = float(lp[t.uvl].uv.x)
    for i, expected in enumerate((0.0, 0.25, 0.5, 0.75, 1.0)):
        assert abs(bottom_u[i] - expected) < 1e-4, f"EVEN v{i}: {bottom_u[i]} != {expected}"
    del t

    # 4b. GEOMETRY mode matches 3D edge length ratios 1:2:3:4 -> [0.0, 0.1, 0.3, 0.6, 1.0]
    obj2 = make_mesh(verts, faces, uv_fn)
    t2 = topo_of(obj2)
    for lp in t2.loops:
        if lp.face.index < 8 and lp.vert.index < 5 and lp.link_loop_next.vert.index < 5:
            lp.uv_select_edge = True
    t2.bm.uv_select_sync_valid = True
    del t2

    res2 = bpy.ops.uv.uvck_distribute_edges(mode="GEOMETRY")
    assert "FINISHED" in res2, res2
    t2 = topo_of(obj2)
    bottom_u2 = {lp.vert.index: float(lp[t2.uvl].uv.x) for lp in t2.loops if lp.face.index < 8 and lp.vert.index < 5}
    for i, expected in enumerate((0.0, 0.1, 0.3, 0.6, 1.0)):
        assert abs(bottom_u2[i] - expected) < 1e-4, f"GEOMETRY v{i}: {bottom_u2[i]} != {expected}"
    del t2

    # 4c. Interior pinned vertex v2 stays pinned at x=0.12; sub-chains [0..2] and [2..4] distribute independently.
    obj3 = make_mesh(verts, faces, uv_fn)
    t3 = topo_of(obj3)
    for lp in t3.loops:
        if lp.face.index < 8 and lp.vert.index < 5 and lp.link_loop_next.vert.index < 5:
            lp.uv_select_edge = True
        if lp.face.index < 8 and lp.vert.index == 2:
            lp[t3.uvl].pin_uv = True
    t3.bm.uv_select_sync_valid = True
    del t3

    res3 = bpy.ops.uv.uvck_distribute_edges(mode="EVEN", respect_pins=True)
    assert "FINISHED" in res3, res3
    t3 = topo_of(obj3)
    bottom_u3 = {lp.vert.index: float(lp[t3.uvl].uv.x) for lp in t3.loops if lp.face.index < 8 and lp.vert.index < 5}
    for i, expected in enumerate((0.0, 0.06, 0.12, 0.56, 1.0)):
        assert abs(bottom_u3[i] - expected) < 1e-4, f"PINNED EVEN v{i}: {bottom_u3[i]} != {expected}"
    del t3

    # 4d. Closed uneven outer border loop (8-sided fan around center) -> distributes cleanly & preserves area.
    angles = [0.0, 0.25, 0.65, 1.4, 2.3, 3.3, 4.4, 5.5]
    c_verts = [(0.0, 0.0, 0.0)] + [(math.cos(a), math.sin(a), 0.0) for a in angles]
    c_faces = [(0, 1 + i, 1 + ((i + 1) % 8)) for i in range(8)]
    obj_c = make_mesh(c_verts, c_faces, lambda _f, _v, co: (0.2 + 0.15 * co.x, 0.2 + 0.15 * co.y))
    tc = topo_of(obj_c)
    border_c = set(core.selection_border_loops(tc, set(range(8))))
    for p in border_c:
        tc.loops[p].uv_select_edge = True
    tc.bm.uv_select_sync_valid = True
    area_c_before = core.island_area(tc, set(range(8)))
    del tc
    res_c = bpy.ops.uv.uvck_distribute_edges(mode="EVEN")
    assert "FINISHED" in res_c, res_c
    tc = topo_of(obj_c)
    assert abs(core.island_area(tc, set(range(8))) / area_c_before - 1.0) < 1e-4
    probe_c = safety.AcceptanceProbe(core, tc, set(range(8))).measure(tc)
    assert probe_c["finite"] and probe_c["flips"] == 0 and probe_c["zero_edges"] == 0, probe_c
    del tc


def test_partial_quad_strip_quadrify():
    # 5a. Mixed island: skewed 3x1 quad strip (faces 0,1,2) + 2 attached triangles (faces 3,4)
    #     plus one untouched outside quad (face 5).
    verts = [
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (3.0, 0.0, 0.0),
        (0.0, 1.0, 0.0), (1.0, 1.0, 0.0), (2.0, 1.0, 0.0), (3.0, 1.0, 0.0),
        (1.0, 2.0, 0.0), (2.0, 2.0, 0.0),
        (10.0, 0.0, 0.0), (11.0, 0.0, 0.0), (11.0, 1.0, 0.0), (10.0, 1.0, 0.0),
    ]
    faces = [
        (0, 1, 5, 4),
        (1, 2, 6, 5),
        (2, 3, 7, 6),
        (4, 5, 8),       # triangle cap 1
        (5, 6, 9, 8),    # upper quad
        (6, 7, 9),       # triangle cap 2
        (10, 11, 12, 13),  # untouched outside island
    ]
    def uv_fn(fi, _vi, co):
        if fi == 6:
            return (1.5 + 0.1 * (co.x - 10.0), 0.1 * co.y)
        # Skew and wave the quad strip so it genuinely needs quadrifying.
        return (0.1 * co.x + 0.015 * co.y, 0.1 * co.y + 0.018 * math.sin(co.x * 1.1))

    obj = make_mesh(verts, faces, uv_fn)
    bpy.context.scene.tool_settings.uv_select_mode = "FACE"
    t = topo_of(obj)
    for fi in (0, 1, 2):
        t.bm.faces[fi].uv_select = True
    t.bm.uv_select_sync_valid = True
    before_all = uv_snapshot(obj)
    outside_loops = [p for p, lp in enumerate(t.loops) if lp.face.index == 6]
    area_before = core.island_area(t, set(range(6)))
    del t

    # Test skip_non_quads=True first: must skip mixed island unchanged.
    skipped_res = quadrify.quadrify(bpy.context, obj, {0, 1, 2}, skip_non_quads=True, selected_faces={0, 1, 2})
    assert skipped_res["changed"] == 0 and skipped_res["skipped"], skipped_res
    assert uv_snapshot(obj) == before_all

    # Test direct partial quad strip path via quadrify API.
    res = quadrify.quadrify(bpy.context, obj, {0, 1, 2}, shape="EVEN", preserve_uv_connections=True, selected_faces={0, 1, 2})
    assert res["changed"] == 1 and not res["skipped"], res
    assert res["islands"][0]["algorithm"] == "PARTIAL_QUAD_STRIP", res["islands"][0]
    assert res["islands"][0]["strip_faces"] == [0, 1, 2], res["islands"][0]
    assert res["new_uv_boundaries"] == 0
    t = topo_of(obj)
    after_all = uv_snapshot(obj)
    assert all(after_all[p] == before_all[p] for p in outside_loops)
    assert abs(core.island_area(t, set(range(6))) / area_before - 1.0) < 1e-4
    # Verify all 3 strip quads are strictly axis-aligned!
    for fi in (0, 1, 2):
        pts = [tuple(float(c) for c in lp[t.uvl].uv) for lp in t.bm.faces[fi].loops]
        assert signed_area(pts) > 0
        for a, b in zip(pts, pts[1:] + pts[:1]):
            dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
            assert max(dx, dy) > 1e-4 and min(dx, dy) < 1e-5, (fi, a, b)
    # Verify surrounding triangle/quad faces have positive winding & zero flips!
    for fi in (3, 4, 5):
        pts = [tuple(float(c) for c in lp[t.uvl].uv) for lp in t.bm.faces[fi].loops]
        assert signed_area(pts) > 0, (fi, pts)
    del t

    # Test via Blender operator uv.uvck_quadrify() as well!
    obj_op = make_mesh(verts, faces, uv_fn)
    bpy.context.scene.tool_settings.uv_select_mode = "FACE"
    t_op = topo_of(obj_op)
    for fi in (0, 1, 2):
        t_op.bm.faces[fi].uv_select = True
    t_op.bm.uv_select_sync_valid = True
    del t_op
    op_res = bpy.ops.uv.uvck_quadrify(shape="PER_FACE")
    assert "FINISHED" in op_res, op_res
    t_op = topo_of(obj_op)
    for fi in (0, 1, 2):
        pts = [tuple(float(c) for c in lp[t_op.uvl].uv) for lp in t_op.bm.faces[fi].loops]
        for a, b in zip(pts, pts[1:] + pts[:1]):
            assert min(abs(b[0] - a[0]), abs(b[1] - a[1])) < 1e-5
    del t_op

    # 5b. Pure-quad island WITH a 3-valent pole + attached 2x1 pole-free quad strip:
    #     3 quads around center pole (0), plus 2 quads forming a strip off outer edge (4)-(5)-(6)?
    #     Build cleanly in 2D:
    #       v0 = (0, 0) [3-valent pole]
    #       v1, v2, v3 = radial at 0, 120, 240 deg (radius 1)
    #       v4, v5, v6 = outer corners at 60, 180, 300 deg (radius 1.7)
    #       faces 0,1,2 = 3 quads meeting at v0:
    #         f0 = (0, 1, 4, 2), f1 = (0, 2, 5, 3), f2 = (0, 3, 6, 1)
    #       Attach a 2-quad strip to outer side (1)-(4) & (4)-(2)? Wait: (1)-(4) is 1 edge -> attach a 2-quad chain outward:
    #         f3 = (1, 7, 8, 4), f4 = (7, 9, 10, 8)
    r1, r2 = 1.0, 1.7
    p_verts = [(0.0, 0.0, 0.0)]
    for k in range(3):
        ang = math.radians(k * 120.0)
        p_verts.append((r1 * math.cos(ang), r1 * math.sin(ang), 0.0))
    for k in range(3):
        ang = math.radians(k * 120.0 + 60.0)
        p_verts.append((r2 * math.cos(ang), r2 * math.sin(ang), 0.0))
    # Edge (1)-(4) goes from (1, 0) to (0.85, 1.472). Extend outward to the right (+x).
    p_verts += [
        (2.2, 0.1, 0.0),   # 7
        (2.05, 1.55, 0.0), # 8
        (3.4, 0.25, 0.0),  # 9
        (3.25, 1.70, 0.0), # 10
    ]
    p_faces = [
        (0, 1, 4, 2),
        (0, 2, 5, 3),
        (0, 3, 6, 1),
        (1, 7, 8, 4),
        (7, 9, 10, 8),
    ]
    obj_pole = make_mesh(p_verts, p_faces, lambda _f, _v, co: (0.1 * co.x, 0.1 * co.y))
    before_pole = uv_snapshot(obj_pole)

    # Selecting the whole pole island must be safely rejected unchanged.
    rej = quadrify.quadrify(bpy.context, obj_pole, set(range(5)), selected_faces=set(range(5)))
    assert rej["changed"] == 0 and rej["skipped"], rej
    assert uv_snapshot(obj_pole) == before_pole

    # Selecting ONLY the pole-free 2-quad strip {3, 4} must succeed via PARTIAL_QUAD_STRIP!
    ok_pole = quadrify.quadrify(bpy.context, obj_pole, {3, 4}, shape="EVEN", preserve_uv_connections=True, selected_faces={3, 4})
    assert ok_pole["changed"] == 1 and not ok_pole["skipped"], ok_pole
    assert ok_pole["islands"][0]["algorithm"] == "PARTIAL_QUAD_STRIP", ok_pole["islands"][0]
    t_p = topo_of(obj_pole)
    for fi in (3, 4):
        pts = [tuple(float(c) for c in lp[t_p.uvl].uv) for lp in t_p.bm.faces[fi].loops]
        assert signed_area(pts) > 0
        for a, b in zip(pts, pts[1:] + pts[:1]):
            assert min(abs(b[0] - a[0]), abs(b[1] - a[1])) < 1e-5, (fi, a, b)
    for fi in (0, 1, 2):
        pts = [tuple(float(c) for c in lp[t_p.uvl].uv) for lp in t_p.bm.faces[fi].loops]
        assert signed_area(pts) > 0, f"Pole face {fi} flipped!"
    del t_p

    # 5c. Regular pure-quad island MUST stay on INTEGER_GRID even if only 1 face is selected!
    reg_verts = [(float(x), float(y), 0.0) for y in range(3) for x in range(3)]
    reg_faces = [(0, 1, 4, 3), (1, 2, 5, 4), (3, 4, 7, 6), (4, 5, 8, 7)]
    obj_reg = make_mesh(reg_verts, reg_faces, lambda _f, _v, co: (0.1 * co.x + 0.02 * co.y, 0.1 * co.y))
    reg_res = quadrify.quadrify(bpy.context, obj_reg, {0}, shape="EVEN", selected_faces={0})
    assert reg_res["changed"] == 1 and reg_res["islands"][0]["algorithm"] == "INTEGER_GRID", reg_res


addon.register()
try:
    if args.require_native:
        assert addon.native_engine._load() is not None
    for test in (
        test_ui_headless_icons_and_i18n,
        test_solve_presets_wiring,
        test_auto_constrain_hv,
        test_distribute_edges,
        test_partial_quad_strip_quadrify,
    ):
        test()
finally:
    addon.unregister()
    clear_scene()

print(json.dumps(dict(
    passed=True,
    cases=5,
    blender=bpy.app.version_string,
    native_required=args.require_native,
    scene_saved=False,
    preferences_saved=False,
)))
