# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate README UV diagrams from actual add-on results in factory Blender.

Run: blender --background --factory-startup --python scripts/generate_examples.py
No scene or preferences are saved. SVGs contain only our synthetic UV geometry,
not screenshots or third-party assets. These small examples are not benchmarks.
"""
import argparse
from html import escape
import json
import math
from pathlib import Path
import sys

import bpy
import bmesh
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import uv_constraint_kit as addon
from uv_constraint_kit import core, ops, circle, rectangle, quadrify, distribute
from uv_constraint_kit import symmetry, live_symmetry, shape_constraints

if not bpy.app.background or '--factory-startup' not in sys.argv or bpy.data.filepath:
    raise RuntimeError('Run this generator in background Blender with --factory-startup and no input scene')

COLORS = {0: '#8794a6', 1: '#aaa1ff', 2: '#90d794', 3: '#f4e56b',
          4: '#ff9b55', 5: '#ff7ba9'}
SELECTED = '#27d9ed'


def topology(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    core.prepare_bm(bm)
    return core.UVTopology(bm, core.uv_layer(bm))


def create(vertices, faces, uv):
    if bpy.context.object and bpy.context.object.mode == 'EDIT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for obj in list(bpy.context.scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    mesh = bpy.data.meshes.new('ExampleMesh')
    mesh.from_pydata(vertices, [], faces)
    mesh.uv_layers.new(name='UVMap')
    obj = bpy.data.objects.new('SyntheticExample', mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    topo = topology(obj)
    for face in topo.bm.faces:
        face.select = True
        face.uv_select = False
        for loop in face.loops:
            loop[topo.uvl].uv = uv[loop.vert.index]
            loop.uv_select_vert = loop.uv_select_edge = False
    topo.bm.uv_select_sync_valid = True
    bpy.context.scene.tool_settings.use_uv_select_sync = False
    bpy.context.scene.tool_settings.uv_select_mode = 'EDGE'
    return obj


def grid(warp=True, rotated=False):
    nx, ny = 10, 7
    vertices, uv = [], []
    for j in range(ny + 1):
        for i in range(nx + 1):
            x, y = i / nx, j / ny
            vertices.append((x, y, 0))
            u = .16 + .68*x + (.045*math.sin(math.pi*y) if warp else 0)
            v = .20 + .52*y + (.09*math.sin(math.pi*x) + .035*x if warp else 0)
            if rotated:
                a = .38
                u, v = .5 + math.cos(a)*(u-.5)-math.sin(a)*(v-.5), .5 + math.sin(a)*(u-.5)+math.cos(a)*(v-.5)
            uv.append((u, v))
    faces = [(j*(nx+1)+i, j*(nx+1)+i+1, (j+1)*(nx+1)+i+1, (j+1)*(nx+1)+i)
             for j in range(ny) for i in range(nx)]
    return create(vertices, faces, uv), nx, ny


def edge_positions(obj, predicate):
    topo = topology(obj)
    return [p for p, loop in enumerate(topo.loops)
            if predicate(loop.vert.index, loop.link_loop_next.vert.index)]


def select(obj, positions):
    topo = topology(obj)
    positions = set(positions)
    loop_indices = core.loop_positions(topo.bm)
    for p, loop in enumerate(topo.loops):
        loop.uv_select_edge = p in positions
        loop.uv_select_vert = p in positions or loop_indices[loop.link_loop_prev] in positions
    topo.bm.uv_select_sync_valid = True


def mark(obj, positions, kind):
    assert ops._apply_constraint_edit(bpy.context, positions, kind)


def solve(obj, **kwargs):
    count = len(topology(obj).bm.faces)
    result = ops._optimize_islands(bpy.context, obj, set(range(count)),
        parameters={'respect_uv_selection': False}, **kwargs)
    assert not result[2].get('rejected'), result[2]
    assert addon.safety.AcceptanceProbe(core, topology(obj), set(range(count))).measure(topology(obj))['max_deviation'] <= .05
    return result


def capture(obj, selected=(), targets=()):
    topo = topology(obj)
    positions = {loop: p for p, loop in enumerate(topo.loops)}
    points = [tuple(loop[topo.uvl].uv) for loop in topo.loops]
    assert np.isfinite(points).all()
    layer = core.constraint_layer(topo.bm)
    kinds = [int(loop[layer]) if layer else 0 for loop in topo.loops]
    for record in shape_constraints._plan(topo):
        groups = shape_constraints._groups(topo, record)
        edges = {frozenset((a, b)) for a, b in zip(groups, groups[1:] + groups[:1])}
        for p in range(len(topo.loops)):
            if frozenset(topo.loop_edge_vgroups(p)) in edges:
                kinds[p] = 4 if record['kind'] == 'CIRCULAR' else 5
    faces = [[positions[loop] for loop in f.loops] for f in topo.bm.faces]
    return dict(points=points, faces=faces, kinds=kinds, selected=list(selected), targets=list(targets))


def diagram(name, title, subtitle, panels, footer):
    width, height = 1120, 520
    gutter, margin = 28, 28
    pw = (width - margin*2 - gutter*(len(panels)-1))/len(panels)
    top, ph = 118, 330
    all_points = np.asarray([p for _, data in panels for p in data['points']])
    low, high = all_points.min(axis=0), all_points.max(axis=0)
    center = (low + high)/2
    scale = min((pw-54)/max(high[0]-low[0], .01), (ph-40)/max(high[1]-low[1], .01))
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{escape(title)}</title><desc id="desc">{escape(subtitle)}. Actual add-on output on synthetic UV geometry. {escape(footer)}</desc>',
        '<rect width="1120" height="520" rx="16" fill="#111923"/>',
        '<g font-family="sans-serif" fill="#eff5fa">',
        f'<text x="28" y="39" font-size="26" font-weight="700">{escape(title)}</text>',
        f'<text x="28" y="70" font-size="16" fill="#b5c4d5">{escape(subtitle)}</text>']
    for index, (label, data) in enumerate(panels):
        left = margin + index*(pw+gutter)
        svg += [f'<text x="{left+4:.2f}" y="105" font-size="17" font-weight="700">{escape(label)}</text>',
                f'<rect x="{left:.2f}" y="{top}" width="{pw:.2f}" height="{ph}" rx="10" fill="#1b2633" stroke="#344254"/>']
        def xy(point):
            return left+pw/2+(point[0]-center[0])*scale, top+ph/2-(point[1]-center[1])*scale
        def coords(ids):
            return ' '.join('%.2f,%.2f' % xy(data['points'][p]) for p in ids)
        for k in range(1, 10):
            x, y = left + pw*k/10, top + ph*k/10
            svg += [f'<path d="M{x:.2f} {top}v{ph} M{left:.2f} {y:.2f}h{pw:.2f}" stroke="#283646" stroke-width=".7"/>']
        for fi, ids in enumerate(data['faces']):
            fill = '#33bfdc' if fi in data['targets'] else '#8196ad'
            alpha = '.15' if fi in data['targets'] else '.045'
            svg.append(f'<polygon points="{coords(ids)}" fill="{fill}" fill-opacity="{alpha}"/>')
        edges = {}
        for ids in data['faces']:
            for p, q in zip(ids, ids[1:]+ids[:1]):
                key = tuple(sorted((tuple(round(v, 7) for v in data['points'][p]), tuple(round(v, 7) for v in data['points'][q]))))
                selected = p in data['selected']
                kind = data['kinds'][p]
                style = (3 if selected else 2 if kind else 1, SELECTED if selected else COLORS[kind])
                if key not in edges or style[0] > edges[key][0]:
                    edges[key] = (*style, p, q)
        for weight, color, p, q in sorted(edges.values()):
            x1, y1 = xy(data['points'][p]); x2, y2 = xy(data['points'][q])
            svg.append(f'<path d="M{x1:.2f} {y1:.2f}L{x2:.2f} {y2:.2f}" fill="none" stroke="{color}" stroke-width="{2.8 if weight>1 else 1.15}" stroke-linecap="round"/>')
    svg += [f'<text x="28" y="480" font-size="15" fill="#c5d2df">{escape(footer)}</text>',
            '<text x="28" y="506" font-size="12" fill="#8fa2b5">Synthetic mesh · genuine add-on results · shared scale in all panels · not a performance comparison</text>',
            '</g></svg>']
    path = OUTPUT / name
    path.write_text('\n'.join(svg)+'\n', encoding='utf8', newline='\n')
    return dict(file=name, panels=len(panels), uv_corners=len(panels[0][1]['points']))


def constraints():
    results = []
    for name, title, kind in [('horizontal', 'Horizontal constraint', 1), ('vertical', 'Vertical constraint', 2), ('aligned', 'Aligned constraint', 3)]:
        obj, nx, ny = grid(rotated=name == 'aligned')
        if name == 'vertical':
            chosen = edge_positions(obj, lambda a,b: a % (nx+1) == nx and b % (nx+1) == nx)
        else:
            chosen = edge_positions(obj, lambda a,b: a//(nx+1) == ny and b//(nx+1) == ny)
        before = capture(obj, chosen)
        mark(obj, chosen, kind)
        solve(obj)
        after = capture(obj)
        assert all(after['kinds'][p] == kind for p in chosen)
        results.append(diagram(f'constrain-{name}.svg', title,
            'Select UV edges; the solver adjusts the surrounding island as well.',
            [('Before · selected edges', before), ('After · persistent constraint', after)],
            {1: 'Purple = horizontal. These edges remain horizontal in later optimization.',
             2: 'Green = vertical. These edges remain vertical in later optimization.',
             3: 'Yellow = aligned. Straighten the chain along its trend, without forcing an axis.'}[kind]))
    return results


def optimize():
    obj, nx, ny = grid(warp=False)
    topo = topology(obj)
    for loop in topo.loops:
        i, j = loop.vert.index % (nx+1), loop.vert.index//(nx+1)
        if i not in (0,nx) and j not in (0,ny):
            loop[topo.uvl].uv.x += .055*math.sin(i*1.2)*math.sin(j*.9)
            loop[topo.uvl].uv.y += .035*math.cos(i*.9)*math.sin(j*.7)
    before = capture(obj)
    del topo
    solve(obj, fix_boundary=True)
    after = capture(obj)
    assert np.max(np.abs(np.asarray(after['points'])-before['points'])) > .01
    return diagram('optimize.svg', 'Optimize the island interior',
        'Reduce interior UV distortion while respecting constraints and pins.',
        [('Before · distorted interior', before), ('After · Fix boundary enabled', after)],
        'This example fixes the outer boundary; the interior is re-solved by the add-on.')


def unconstrain():
    obj, nx, ny = grid()
    top = edge_positions(obj, lambda a,b: a//(nx+1) == ny and b//(nx+1) == ny)
    right = edge_positions(obj, lambda a,b: a%(nx+1) == nx and b%(nx+1) == nx)
    mark(obj, top, 1); mark(obj, right, 2); solve(obj)
    before = capture(obj, top)
    mark(obj, top, 0); solve(obj)
    after = capture(obj)
    assert all(after['kinds'][p] == 0 for p in top)
    assert all(after['kinds'][p] == 2 for p in right)
    return diagram('unconstrain.svg', 'Remove selected constraints',
        'Release one edge chain without clearing constraints on other edges.',
        [('Before · select the top chain', before), ('After · top chain released', after)],
        'Top constraint removed; right-side vertical constraint retained. Auto optimize is used here.')


def annulus():
    n, rings = 32, 5
    vertices, uv = [], []
    for j in range(rings+1):
        r = .19 + .27*j/rings
        for i in range(n):
            a = math.tau*i/n
            vertices.append((r*math.cos(a), r*math.sin(a), 0))
            amount = 1-j/rings
            u = .5 + r*(1+.23*amount)*math.cos(a)
            v = .5 + r*(1-.23*amount)*math.sin(a)
            uv.append((u,v))
    faces = [(j*n+i, j*n+(i+1)%n, (j+1)*n+(i+1)%n, (j+1)*n+i)
             for j in range(rings) for i in range(n)]
    obj = create(vertices, faces, uv)
    chosen = edge_positions(obj, lambda a,b: a<n and b<n)
    select(obj, chosen)
    return obj, chosen


def shape(kind):
    obj, chosen = annulus()
    before = capture(obj, chosen)
    faces = set(range(len(topology(obj).bm.faces)))
    if kind == 'circularize':
        result = circle.circularize(bpy.context, obj, faces, spacing='EVEN')
        title, label = 'Circularize a selected loop', 'After · persistent circular constraint'
    else:
        result = rectangle.rectify(bpy.context, obj, faces, shape='SQUARE', spacing='EVEN')
        title, label = 'Rectangularize a selected loop', 'After · persistent rectangular constraint'
    assert result['changed'] == 1 and not result['skipped'], result
    after = capture(obj)
    assert any(k == (4 if kind == 'circularize' else 5) for k in after['kinds'])
    solve(obj)
    subsequent = capture(obj)
    assert np.max(np.abs(np.asarray(subsequent['points'])[chosen]-np.asarray(after['points'])[chosen])) < 1e-5
    return diagram(f'{kind}.svg', title,
        'Only the selected closed loop gets a shape; the rest of the island is optimized.',
        [('Before · select the inner loop', before), (label, after)],
        ('Orange = circular constraint; ' if kind == 'circularize' else 'Pink = rectangular constraint; ') +
        'the hole remains open and the shape persists during later optimization.')


def quad():
    # Build a warped rotated 8x6 pure-quad chart with an interior pole insert
    # (one quad cell subdivided into 5 quads -> four valence-3 and four valence-5 poles)
    # so the diagram demonstrates 1.9.0 whole-island Quad-Flow DAG quadrification.
    nx, ny = 8, 6
    vid = {}
    vertices, uv = [], []

    def add_v(x, y):
        u = .16 + .68*x + .045*math.sin(math.pi*y)
        v = .20 + .52*y + .085*math.sin(math.pi*x) + .035*x
        a = .34
        ur = .5 + math.cos(a)*(u-.5) - math.sin(a)*(v-.5)
        vr = .5 + math.sin(a)*(u-.5) + math.cos(a)*(v-.5)
        idx = len(vertices)
        vertices.append((x, y, 0.0))
        uv.append((ur, vr))
        return idx

    for j in range(ny + 1):
        for i in range(nx + 1):
            vid[(i, j)] = add_v(i / nx, j / ny)
    x0, x1 = 3.0 / nx, 4.0 / nx
    y0, y1 = 2.0 / ny, 3.0 / ny
    q0 = add_v(x0 + 0.28*(x1-x0), y0 + 0.28*(y1-y0))
    q1 = add_v(x0 + 0.72*(x1-x0), y0 + 0.28*(y1-y0))
    q2 = add_v(x0 + 0.72*(x1-x0), y0 + 0.72*(y1-y0))
    q3 = add_v(x0 + 0.28*(x1-x0), y0 + 0.72*(y1-y0))
    faces = []
    for j in range(ny):
        for i in range(nx):
            if (i, j) == (3, 2):
                continue
            faces.append((vid[(i, j)], vid[(i+1, j)], vid[(i+1, j+1)], vid[(i, j+1)]))
    faces.extend([
        (q0, q1, q2, q3),
        (vid[(3, 2)], vid[(4, 2)], q1, q0),
        (vid[(4, 2)], vid[(4, 3)], q2, q1),
        (vid[(4, 3)], vid[(3, 3)], q3, q2),
        (vid[(3, 3)], vid[(3, 2)], q0, q3),
    ])
    obj = create(vertices, faces, uv)
    before = capture(obj)
    all_faces = set(range(len(topology(obj).bm.faces)))
    result = quadrify.quadrify(bpy.context, obj, all_faces, shape='PER_FACE',
                               average_shape=True, prevent_overlap=True)
    assert result['changed'] == 1 and not result['skipped'], result
    assert result['islands'][0]['algorithm'] == 'QUAD_FLOW', result
    after = capture(obj)
    return diagram('quadrify.svg', 'Quadrify regular and poled quad islands',
        'Regular quad charts use exact integer grids; poled or concave quad islands use whole-island Quad-Flow DAG.',
        [('Before · warped quad island with poles', before), ('After · whole-island Quad-Flow DAG', after)],
        'Preserves pins, existing constraints, island area, and zero flips/overlaps without adding UV seams.')


def distribute_edges_example():
    nx, ny = 10, 6
    vertices, uv = [], []
    # Bunch the top row's UV vertices tightly toward the left/right ends so redistribution is unmistakable.
    bunched = [0.0, 0.04, 0.09, 0.16, 0.28, 0.50, 0.72, 0.84, 0.91, 0.96, 1.0]
    for j in range(ny + 1):
        for i in range(nx + 1):
            x, y = i / nx, j / ny
            vertices.append((x, y, 0.0))
            bx = bunched[i]
            blend = y ** 1.4
            u = 0.12 + 0.76 * (x * (1.0 - blend) + bx * blend)
            v = 0.18 + 0.62 * y + 0.05 * math.sin(math.pi * x)
            uv.append((u, v))
    faces = [(j*(nx+1)+i, j*(nx+1)+i+1, (j+1)*(nx+1)+i+1, (j+1)*(nx+1)+i)
             for j in range(ny) for i in range(nx)]
    obj = create(vertices, faces, uv)
    chosen = edge_positions(obj, lambda a, b: a // (nx + 1) == ny and b // (nx + 1) == ny)
    select(obj, chosen)
    before = capture(obj, chosen)
    all_faces = set(range(len(topology(obj).bm.faces)))
    result = distribute.distribute_edges(bpy.context, obj, all_faces, mode='EVEN',
                                         respect_pins=True, prevent_overlap=True)
    assert result['changed'] == 1 and not result['skipped'], result
    after = capture(obj, chosen)
    return diagram('distribute-edges.svg', 'Distribute edges along a UV chain',
        'Evenly (or geometrically) redistribute vertices along selected UV edge chains and relax the island.',
        [('Before · uneven spacing along top chain', before), ('After · Even distribution + relaxed interior', after)],
        'One-shot spacing tool (Even or Geometry ratio). No persistent constraint overlay is added; preserves area.')


def symmetric():
    n = 4
    vertices, uv, faces = [], [], []
    for side in (-1,1):
        offset = len(vertices)
        for j in range(n+1):
            for i in range(n+1):
                vertices.append((side, i/n, j/n))
                uv.append((.12+.25*i/n if side < 0 else .62+.25*i/n+.025*math.sin(j),
                           .28+.42*j/n if side < 0 else .28+.42*j/n+.035*math.sin(i)))
        for j in range(n):
            for i in range(n):
                f = (offset+j*(n+1)+i, offset+j*(n+1)+i+1,
                     offset+(j+1)*(n+1)+i+1, offset+(j+1)*(n+1)+i)
                faces.append(f if side < 0 else tuple(reversed(f)))
    obj = create(vertices, faces, uv)
    topo = topology(obj)
    for f in topo.bm.faces:
        f.uv_select = f.index < n*n
    topo.bm.uv_select_sync_valid = True
    target = list(range(n*n, 2*n*n))
    before = capture(obj, targets=target)
    del topo
    saved = {}
    matched, missing = symmetry.symmetrize_mesh(bpy.context, obj, '-X to +X', False,
        'X', False, False, .005, saved, center=(.5,.5))
    assert matched == n*n and missing == 0, (matched, missing)
    live_symmetry.install(obj, saved)
    direct = capture(obj, targets=target)
    topo = topology(obj)
    for p in sorted({s for s,_,_,_ in saved['pairs']}):
        u, v = topo.loops[p][topo.uvl].uv
        topo.loops[p][topo.uvl].uv = (u-.035+.025*math.sin(v*8), v+.065)
    del topo
    live_symmetry.sync(bpy.context)
    topo = topology(obj)
    for source, target_corner, _, pivot in saved['pairs']:
        s, t = topo.loops[source][topo.uvl].uv, topo.loops[target_corner][topo.uvl].uv
        assert abs(t.x-(2*pivot-s.x)) < 1e-6 and abs(t.y-s.y) < 1e-6
    del topo
    after = capture(obj, targets=target)
    return diagram('symmetry.svg', 'Symmetry and live symmetry',
        'Match mirrored mesh faces, copy UVs, then optionally keep the target linked.',
        [('Before', before), ('After symmetry', direct), ('Edit source · target follows', after)],
        'Source = no blue fill. Linked target = translucent blue. This example mirrors around U = 0.5.')


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, default=ROOT/'docs'/'images')
args = parser.parse_args(sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else [])
OUTPUT = args.output.resolve()
OUTPUT.mkdir(parents=True, exist_ok=True)
addon.register()
try:
    results = constraints() + [optimize(), unconstrain(), shape('circularize'),
        shape('rectangularize'), quad(), distribute_edges_example(), symmetric()]
finally:
    addon.unregister()
print(json.dumps(dict(passed=True, figures=results, blender=bpy.app.version_string,
                     scene_saved=False, preferences_saved=False)))
