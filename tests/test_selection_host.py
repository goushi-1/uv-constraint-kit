# SPDX-License-Identifier: GPL-3.0-or-later
"""Disconnected UV edge selections must not bridge a one-edge gap."""
import argparse
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import bpy
import bmesh

parser = argparse.ArgumentParser()
parser.add_argument('--addon-root', type=Path,
                    default=Path(__file__).resolve().parents[1] / 'uv_constraint_kit')
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
sys.path.insert(0, str(args.addon_root.resolve().parent))
import uv_constraint_kit as addon
from uv_constraint_kit import core, ops, overlay


def fixture():
    if bpy.context.object and bpy.context.object.mode == 'EDIT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for obj in list(bpy.context.scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    mesh = bpy.data.meshes.new('SelectionGap')
    mesh.from_pydata([(x, y, 0) for y in range(2) for x in range(4)], [],
                     [(i, i + 1, i + 5, i + 4) for i in range(3)])
    mesh.uv_layers.new()
    obj = bpy.data.objects.new('SelectionGap', mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(mesh)
    core.prepare_bm(bm)
    uvl = bm.loops.layers.uv.active
    for face in bm.faces:
        face.select = True
        face.uv_select = False
        for loop in face.loops:
            loop[uvl].uv = (loop.vert.co.x * .1, loop.vert.co.y * .1)
            loop.uv_select_edge = False
            loop.uv_select_vert = loop.vert.co.y == 0
    loops = core.loops_in_order(bm)
    selected = {p for p, loop in enumerate(loops)
                if {loop.vert.index, loop.link_loop_next.vert.index}
                in ({0, 1}, {2, 3})}
    gap = next(p for p, loop in enumerate(loops)
               if {loop.vert.index, loop.link_loop_next.vert.index} == {1, 2})
    for p in selected:
        loops[p].uv_select_edge = True
    bm.uv_select_sync_valid = True
    settings = bpy.context.scene.tool_settings
    settings.use_uv_select_sync = False
    settings.uv_select_mode = 'EDGE'
    return obj, selected, gap


class SelectionGap(unittest.TestCase):
    def test_edge_flags_preserve_gap_and_separate_runs(self):
        obj, selected, gap = fixture()
        bm = bmesh.from_edit_mesh(obj.data)
        self.assertEqual(core.selected_uv_edges(bpy.context, bm), selected)
        core.constraint_layer(bm, create=True)
        topology = core.UVTopology(bm, bm.loops.layers.uv.active)
        self.assertEqual(len(core.group_chains(topology, selected,
                                             dict.fromkeys(selected, core.C_VERTICAL))), 2)
        self.assertNotIn(gap, selected)

    def test_gap_overlay_uses_edge_selection(self):
        obj, selected, gap = fixture()
        bm = bmesh.from_edit_mesh(obj.data)
        loops = core.loops_in_order(bm)
        uvl = bm.loops.layers.uv.active
        self.assertTrue(all(overlay._uv_edge_selected(loops[p], False, uvl)
                            for p in selected))
        self.assertFalse(overlay._uv_edge_selected(loops[gap], False, uvl))

    def test_edge_mode_never_infers_edges_from_endpoints(self):
        obj, _, _ = fixture()
        bm = bmesh.from_edit_mesh(obj.data)
        for loop in core.loops_in_order(bm):
            loop.uv_select_edge = False
        self.assertEqual(core.selected_uv_edges(bpy.context, bm), set())

    def test_vertex_mode_keeps_endpoint_semantics(self):
        obj, selected, gap = fixture()
        bpy.context.scene.tool_settings.uv_select_mode = 'VERTEX'
        bm = bmesh.from_edit_mesh(obj.data)
        self.assertEqual(core.selected_uv_edges(bpy.context, bm), selected | {gap})

    def test_sync_uses_mesh_edge_mode_instead_of_hidden_uv_vertex_mode(self):
        obj, selected, gap = fixture()
        settings = bpy.context.scene.tool_settings
        settings.uv_select_mode = 'VERTEX'
        settings.use_uv_select_sync = True
        settings.mesh_select_mode = (False, True, False)
        bm = bmesh.from_edit_mesh(obj.data)
        loops = core.loops_in_order(bm)
        # Toggling sync flushes selection; restore the captured edge-side flags.
        for p, loop in enumerate(loops):
            loop.uv_select_edge = p in selected
            loop.uv_select_vert = loop.vert.co.y == 0
        bm.uv_select_sync_valid = True
        self.assertEqual(core.selected_uv_edges(bpy.context, bm), selected)
        self.assertFalse(overlay._uv_edge_selected(loops[gap], True,
                                                bm.loops.layers.uv.active))

    def test_invalid_sync_flags_use_mesh_edges(self):
        obj, selected, _ = fixture()
        settings = bpy.context.scene.tool_settings
        settings.use_uv_select_sync = True
        settings.mesh_select_mode = (False, True, False)
        bm = bmesh.from_edit_mesh(obj.data)
        loops = core.loops_in_order(bm)
        first, last = sorted(selected)
        for edge in bm.edges:
            edge.select = False
        loops[last].edge.select = True
        for p, loop in enumerate(loops):
            loop.uv_select_edge = p == first
        bm.uv_select_sync_valid = False
        self.assertEqual(core.selected_uv_edges(bpy.context, bm), {last})
        self.assertEqual(core.selected_uv_edges(bpy.context, bm, mesh_fallback=False), set())

    def test_face_fallback_does_not_select_neighbor_seam_sides(self):
        obj, _, _ = fixture()
        settings = bpy.context.scene.tool_settings
        settings.use_uv_select_sync = True
        settings.mesh_select_mode = (False, False, True)
        bm = bmesh.from_edit_mesh(obj.data)
        for face in bm.faces:
            face.select = face.index == 0
        bm.uv_select_sync_valid = False
        loops = core.loops_in_order(bm)
        expected = {p for p, loop in enumerate(loops) if loop.face.index == 0}
        self.assertEqual(core.selected_uv_edges(bpy.context, bm), expected)

    def test_mark_and_clear_only_selected_runs_with_and_without_solve(self):
        for auto in (False, True):
            for kind, operator in ((core.C_HORIZONTAL, bpy.ops.uv.uvck_constrain_horizontal),
                                   (core.C_VERTICAL, bpy.ops.uv.uvck_constrain_vertical),
                                   (core.C_ALIGNED, bpy.ops.uv.uvck_constrain_aligned)):
                with self.subTest(auto=auto, kind=kind):
                    obj, selected, gap = fixture()
                    with patch.object(ops, '_auto_optimize', return_value=auto):
                        self.assertEqual(operator(), {'FINISHED'})
                        bm = bmesh.from_edit_mesh(obj.data)
                        layer = core.constraint_layer(bm)
                        loops = core.loops_in_order(bm)
                        self.assertEqual({p for p, loop in enumerate(loops)
                                          if loop[layer] != core.C_NONE}, selected)
                        self.assertEqual(loops[gap][layer], core.C_NONE)
                        del loops, layer, bm
                        self.assertEqual(bpy.ops.uv.uvck_unconstrain(), {'FINISHED'})
                    bm = bmesh.from_edit_mesh(obj.data)
                    self.assertEqual(core.constrained_loops(bm), [])

    def test_empty_edge_selection_does_not_clear_other_marks(self):
        obj, selected, _ = fixture()
        self.assertTrue(ops._apply_constraint_edit(bpy.context, selected, core.C_VERTICAL))
        bm = bmesh.from_edit_mesh(obj.data)
        for loop in core.loops_in_order(bm):
            loop.uv_select_edge = False
        del bm
        self.assertEqual(bpy.ops.uv.uvck_unconstrain(), {'CANCELLED'})
        self.assertEqual(set(core.constrained_loops(bmesh.from_edit_mesh(obj.data))), selected)


addon.register()
try:
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(SelectionGap))
finally:
    addon.unregister()
if not result.wasSuccessful():
    raise AssertionError('UV selection gap regression failed')
