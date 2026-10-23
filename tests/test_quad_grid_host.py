# SPDX-License-Identifier: GPL-3.0-or-later
"""Factory Blender tests; synthetic meshes only, no files or preferences saved.

Run: blender --background --factory-startup --python tests/test_quad_grid_host.py
"""
import math
from pathlib import Path
import sys
import unittest

import bmesh

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from uv_constraint_kit import quad_grid


def snapshot(bm, uvl):
    return tuple(tuple(loop[uvl].uv) for face in bm.faces for loop in face.loops)


def signed(face, uvl):
    points = [tuple(loop[uvl].uv) for loop in face.loops]
    return sum(a[0] * b[1] - a[1] * b[0]
               for a, b in zip(points, points[1:] + points[:1])) / 2.0


def chart(nx=3, ny=2, mirror=False, omit=(), outside=False):
    bm = bmesh.new()
    uvl = bm.loops.layers.uv.new('UVMap')
    vertices = {}
    for y in range(ny + 1):
        for x in range(nx + 1):
            vertices[x, y] = bm.verts.new((x + 0.12 * x * x,
                                         y + 0.15 * y * y, 0.04 * x * y))
    wanted = []
    for y in range(ny):
        for x in range(nx):
            if (x, y) in omit:
                continue
            face = bm.faces.new([vertices[x, y], vertices[x + 1, y],
                                 vertices[x + 1, y + 1], vertices[x, y + 1]])
            for loop, (i, j) in zip(face.loops, ((x, y), (x + 1, y),
                                                (x + 1, y + 1), (x, y + 1))):
                u = i + 0.18 * j + 0.06 * i * j
                loop[uvl].uv = (-u if mirror else u, j + 0.08 * i * i)
            wanted.append(face)
    if outside:
        face = bm.faces.new([bm.verts.new(co) for co in
                             ((20, 0, 0), (21, 0, 0), (21, 1, 0), (20, 1, 0))])
        for loop in face.loops:
            loop[uvl].uv = (loop.vert.co.x - 10, loop.vert.co.y + 20)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    return bm, uvl, {face.index for face in wanted}


class GridLayout(unittest.TestCase):
    def aligned(self, bm, uvl, faces):
        for index in faces:
            face = bm.faces[index]
            points = [tuple(loop[uvl].uv) for loop in face.loops]
            for a, b in zip(points, points[1:] + points[:1]):
                dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
                self.assertGreater(max(dx, dy), 0.0)
                self.assertLess(min(dx, dy), 1e-6)
            a, b, c = points[:3]
            self.assertAlmostEqual((b[0] - a[0]) * (c[0] - b[0]) +
                                   (b[1] - a[1]) * (c[1] - b[1]), 0.0, places=6)

    def test_real_rectangularization_and_outside_unchanged(self):
        bm, uvl, faces = chart(outside=True)
        try:
            before = snapshot(bm, uvl)
            outside = tuple(tuple(loop[uvl].uv) for loop in bm.faces[-1].loops)
            report = quad_grid.solve(bm, uvl, faces, 0, 'EVEN', True, 0.002)
            self.assertEqual(report['algorithm'], 'INTEGER_GRID')
            self.assertNotEqual(before, snapshot(bm, uvl))
            self.assertEqual(outside, tuple(tuple(loop[uvl].uv) for loop in bm.faces[-1].loops))
            self.aligned(bm, uvl, faces)
            for index in faces:
                points = [tuple(loop[uvl].uv) for loop in bm.faces[index].loops]
                self.assertAlmostEqual(math.dist(points[0], points[1]),
                                       math.dist(points[1], points[2]), places=6)
        finally:
            bm.free()

    def test_mirror_winding_preserved(self):
        bm, uvl, faces = chart(mirror=True)
        try:
            self.assertTrue(all(signed(bm.faces[i], uvl) < 0 for i in faces))
            quad_grid.solve(bm, uvl, faces, 0, 'FACE', True, 0.002)
            self.aligned(bm, uvl, faces)
            self.assertTrue(all(signed(bm.faces[i], uvl) < 0 for i in faces))
        finally:
            bm.free()

    def test_interval_lengths_and_hole(self):
        bm, uvl, faces = chart(3, 3, omit={(1, 1)})
        try:
            quad_grid.solve(bm, uvl, faces, 0, 'PER_FACE', False, 0.002)
            self.aligned(bm, uvl, faces)
            widths = [abs(bm.faces[i].loops[1][uvl].uv.x -
                          bm.faces[i].loops[0][uvl].uv.x) for i in faces]
            self.assertGreater(max(widths) - min(widths), 0.1)
        finally:
            bm.free()

    def rejected_unchanged(self, bm, uvl, faces, seed=0):
        before = snapshot(bm, uvl)
        with self.assertRaises(ValueError):
            quad_grid.solve(bm, uvl, faces, seed, 'PER_FACE', True, 0.002)
        self.assertEqual(before, snapshot(bm, uvl))

    def test_mixed_polygon_rejected_unchanged(self):
        bm, uvl, faces = chart(1, 1)
        try:
            face = bm.faces.new([bm.verts.new(co) for co in
                                 ((3, 0, 0), (4, 0, 0), (3, 1, 0))])
            for loop in face.loops:
                loop[uvl].uv = loop.vert.co.xy
            bm.faces.index_update()
            self.rejected_unchanged(bm, uvl, faces | {face.index})
        finally:
            bm.free()

    def test_disconnected_rejected_unchanged(self):
        bm, uvl, faces = chart(1, 1, outside=True)
        try:
            self.rejected_unchanged(bm, uvl, set(range(len(bm.faces))))
        finally:
            bm.free()

    def test_partial_chart_rejected_unchanged(self):
        bm, uvl, faces = chart(2, 1)
        try:
            self.rejected_unchanged(bm, uvl, {0})
        finally:
            bm.free()

    def test_nonmanifold_edge_rejected_unchanged(self):
        bm = bmesh.new()
        uvl = bm.loops.layers.uv.new('UVMap')
        try:
            a, b = bm.verts.new((0, 0, 0)), bm.verts.new((1, 0, 0))
            for top, height in ((True, 1), (False, -1), (True, 2)):
                c = bm.verts.new((1, height, 0))
                d = bm.verts.new((0, height, 0))
                face = bm.faces.new((a, b, c, d) if top else (b, a, d, c))
                for loop in face.loops:
                    loop[uvl].uv = loop.vert.co.xy
            bm.faces.index_update()
            self.rejected_unchanged(bm, uvl, set(range(len(bm.faces))))
        finally:
            bm.free()

    def test_pole_cycle_rejected_unchanged(self):
        bm = bmesh.new()
        uvl = bm.loops.layers.uv.new('UVMap')
        try:
            center = bm.verts.new((0, 0, 0))
            radial = [bm.verts.new((math.cos(i * 2 * math.pi / 3),
                                    math.sin(i * 2 * math.pi / 3), 0)) for i in range(3)]
            for i in range(3):
                angle = (i + 0.5) * 2 * math.pi / 3
                outer = bm.verts.new((2 * math.cos(angle), 2 * math.sin(angle), 0))
                face = bm.faces.new((center, radial[i], outer, radial[(i + 1) % 3]))
                for loop in face.loops:
                    loop[uvl].uv = loop.vert.co.xy
            bm.faces.index_update()
            self.rejected_unchanged(bm, uvl, set(range(len(bm.faces))))
        finally:
            bm.free()


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(GridLayout))
    if not result.wasSuccessful():
        raise SystemExit(1)
