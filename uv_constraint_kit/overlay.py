# SPDX-License-Identifier: GPL-3.0-or-later
"""Colour overlay for constrained UV edges, drawn in the UV editor.

Where the colours come from
---------------------------
RizomUV's own preference file defines exactly three colours for edge
constraints, and we reuse those numbers verbatim (see const.py):

    EdgeConstrainedHorizontaly  0.5 0.5 1     (淡蓝)
    EdgeConstrainedVerticaly    0.4 0.8 0.4   (绿)
    EdgeConstrainedAligned      1   1   0     (黄)

Those are passed to the shader *unchanged*. RizomUV stores display (sRGB)
values and Blender's UI writes shader output straight into the window
framebuffer, so handing over the same numbers reproduces the same on-screen
colour -- verified by sampling the rendered pixels and comparing against
RizomUV's stored values:

    horizontal  rendered (127, 127, 255)   RizomUV (128, 128, 255)
    vertical    rendered (102, 203, 102)   RizomUV (102, 204, 102)
    aligned     rendered (255, 255,   0)   RizomUV (255, 255,   0)

(An sRGB->linear conversion was tried first; it renders the light blue and the
green far too dark, because that transform is already accounted for.)

What gets coloured
------------------
One segment per constrained *UV edge* (a loop and the one that follows it),
so a seam edge is only coloured in the island it was actually constrained in.

A constrained edge that is **also selected** is drawn in a blend towards
RizomUV's selected-edge colour (`Selected = 0 1 1`). Without that there is no
way, while several constrained edges are on screen, to tell which of them the
next click is going to act on -- the constraint colour alone says what an edge
is constrained to, but not whether it is picked. The selection is read fresh on
every draw rather than cached (see `_constrained_loops`), because a selection
change does not go through any of the operators that invalidate the cache and
would otherwise leave the tint one selection behind.

How the drawing works
---------------------
A POST_VIEW draw handler on the UV editor is handed a projection matrix that
maps *UV space* straight to the screen, zoom and pan included. So the overlay
is drawn by feeding plain UV coordinates to the shader and letting the space's
own matrix place them: no manual zoom/offset maths, and panning stays correct.
(Verified by drawing the known UV corners and checking they land exactly on the
0..1 UV square.)

While an object is in edit mode the Mesh datablock's UV layer is empty, so all
reads go through `bmesh.from_edit_mesh`.
"""

import bmesh
import bpy
import gpu
from bpy.app.handlers import persistent
from gpu_extras.batch import batch_for_shader

from . import core
from .const import (
    C_ALIGNED,
    C_HORIZONTAL,
    C_NONE,
    C_VERTICAL,
    C_CIRCULAR,
    C_RECTANGULAR,
    SHAPE_SRGB,
    RIZOM_SELECTED_BLEND,
    RIZOM_SRGB,
    RIZOM_SRGB_SELECTED,
    UV_EPS,
)

_uv_handle = None
_bm_handles = {}


def _transform_running():
    return any(op.bl_idname.startswith("TRANSFORM_OT_")
               for window in bpy.context.window_manager.windows
               for op in window.modal_operators)


def _edit_bmesh(obj):
    """Keep Python's edit-mesh wrapper alive across draw calls.

    Its destructor removes CD_BM_ELEM_PYPTR layers, reallocating loop custom
    data and invalidating the UV pointers held by a native modal transform.
    Never create a fresh set of element wrappers midway through a transform.
    Undo/load invalidate the old wrapper; reacquire it on the next idle draw.
    """
    key = obj.data.as_pointer()
    if _transform_running():
        bm = _bm_handles.get(key)
        return bm if bm is not None and bm.is_valid else None
    for stale in [k for k, bm in _bm_handles.items() if not bm.is_valid]:
        del _bm_handles[stale]
    bm = bmesh.from_edit_mesh(obj.data)
    _bm_handles[key] = bm
    return bm


def _release_bmeshes_when_idle():
    if _transform_running():
        return 0.1
    _loop_cache.clear()
    _bm_handles.clear()
    return None

# Cache of "which loops carry a constraint", so each redraw only reads the UVs
# of the constrained loops instead of walking the whole mesh.
_loop_cache = {}
_mesh_updates = {}
_mesh_generation = 0
_revision = 0
_redraw_pending = False


def bump_revision():
    """Invalidate the loop cache (call after anything that can change it)."""
    global _revision
    _revision += 1
    if not _transform_running():_loop_cache.clear()


def cache_stamp(bm, obj=None):
    """Cheap invalidation for retained read-only corner data, outside gestures."""
    layers=bm.loops.layers
    return (_revision,_mesh_updates.get(obj.data.as_pointer(),0) if obj else _mesh_generation,
            len(bm.verts),len(bm.edges),len(bm.faces),
            layers.uv.active.name if layers.uv.active else None,
            tuple((name,tuple(collection.keys()))
                  for name in ('uv','int','string','float','float_vector','color','float_color','bool')
                  for collection in (getattr(layers,name,None),) if collection is not None))


@persistent
def _mesh_updated(_scene,depsgraph):
    global _mesh_generation
    changed={getattr(u.id,'original',u.id).as_pointer() for u in depsgraph.updates
             if isinstance(u.id,bpy.types.Mesh)}
    if changed:
        _mesh_generation+=1
        for key in changed:_mesh_updates[key]=_mesh_updates.get(key,0)+1


def request_redraw():
    """Tag every UV editor / viewport for an immediate repaint.

    `area.tag_redraw()` on its own is not enough to be *prompt*. The draw pass
    that is already running has captured the state it is about to hand to the
    GPU, so a change made from an operator can be one whole frame late -- and a
    constraint the user has just marked then looks like it did not take. Two
    things fix that: tagging each *region* as well as the area (a
    POST_VIEW handler lives in a region, and the area tag can be cleared by the
    pass in flight), and repeating the tag once from a timer, which always lands
    after the current frame is done.
    """
    global _redraw_pending
    try:
        windows = bpy.context.window_manager.windows
    except Exception:
        return
    for window in windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type not in {"IMAGE_EDITOR", "VIEW_3D"}:
                continue
            area.tag_redraw()
            for region in area.regions:
                region.tag_redraw()

    if _redraw_pending:
        return
    _redraw_pending = True
    try:
        bpy.app.timers.register(_tag_again, first_interval=0.0)
    except Exception:
        # No timer facility (background session, or timers unavailable): the
        # tags above are all there is, and that is still better than nothing.
        _redraw_pending = False


def _tag_again():
    """One-shot timer: re-tag, then let the flag go so the next call can queue."""
    global _redraw_pending
    _redraw_pending = False
    try:
        request_redraw_areas_only()
    except Exception:
        pass
    return None


def request_redraw_areas_only():
    try:
        windows = bpy.context.window_manager.windows
    except Exception:
        return
    for window in windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type in {"IMAGE_EDITOR", "VIEW_3D"}:
                area.tag_redraw()


def _prefs(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def _settings(context):
    """(show, width, palette, tint_on, selected_rgb, blend)."""
    prefs = _prefs(context)
    if prefs is None:
        return (True, 3.5, RIZOM_SRGB | SHAPE_SRGB, True, RIZOM_SRGB_SELECTED,
                RIZOM_SELECTED_BLEND)
    return (prefs.show_overlay, prefs.uv_edge_width, prefs.colors(),
            prefs.tint_selected_edges, prefs.selected_color(),
            prefs.selected_blend)


def _mix(base, other, amount):
    amount = max(0.0, min(1.0, float(amount)))
    return tuple(base[i] + (other[i] - base[i]) * amount for i in range(3))


def _color_for(kind, selected, palette, selected_rgb, blend):
    """The colour one segment is drawn in.

    The constraint colour is used as-is (see the module docstring); a selected
    edge is pushed towards RizomUV's selected colour by `blend` first.
    """
    rgb = palette.get(kind) or SHAPE_SRGB.get(kind) or RIZOM_SRGB[kind]
    if selected:
        rgb = _mix(rgb, selected_rgb, blend)
    return (rgb[0], rgb[1], rgb[2], 1.0)


def _same_uv_edge(a, b, uvl, eps):
    """Are these two loops the *same* UV edge (as opposed to a seam's far side)?"""
    a0, a1 = a[uvl].uv, a.link_loop_next[uvl].uv
    b0, b1 = b[uvl].uv, b.link_loop_next[uvl].uv
    close = lambda p, q: abs(p[0] - q[0]) <= eps and abs(p[1] - q[1]) <= eps  # noqa: E731
    return ((close(a0, b0) and close(a1, b1))
            or (close(a0, b1) and close(a1, b0)))


def _uv_edge_selected(loop, sync, uvl=None, mode=None):
    """Read UV selection only, including with mesh/UV selection sync enabled.

    A mesh edge can represent two distinct UV edges at a seam. Its selection
    must never decide the tint of either copy. Radial flags count only when
    both loops describe the same UV coordinates.
    """
    if mode is None:
        mode = core.uv_selection_mode(bpy.context)
    candidates = [loop]
    radial = loop.link_loop_next.link_loop_radial_next
    if uvl is not None:
        try:
            if _same_uv_edge(loop, radial, uvl, UV_EPS):
                candidates.append(radial)
        except (AttributeError, ReferenceError):
            pass

    for item in candidates:
        try:
            if core.uv_edge_selected(item, mode):
                return True
        except AttributeError:
            continue
    return False


def _constrained_loops(bm, obj):
    """[(loop_position, kind), ...] for every constrained UV edge."""
    # Read authoritative BMesh values on every draw. Undo can replace edit
    # data without changing object name, face count, or this module's revision.
    layer = core.constraint_layer(bm)
    items = []
    if layer is not None:
        pos = core.loop_positions(bm)
        for face in bm.faces:
            for loop in face.loops:
                kind = loop[layer]
                if kind != C_NONE:
                    items.append((pos[loop], kind))
    return items


def _visible_constrained_loops(bm, sync, obj=None):
    """[(BMLoop, kind), ...] for constrained edges on visible faces."""
    layer = core.constraint_layer(bm)
    if layer is None:
        return []
    key=id(bm);entry=_loop_cache.get(key)
    if not _transform_running():
        stamp=cache_stamp(bm,obj)
        if not entry or entry['bm']!=bm or entry['stamp']!=stamp:
            items=[(loop,loop[layer]) for face in bm.faces for loop in face.loops
                   if loop[layer]!=C_NONE]
            entry=dict(bm=bm,stamp=stamp,items=items)
            _loop_cache[key]=entry
    if not entry or entry['bm']!=bm:return []
    return [(loop,kind) for loop,kind in entry['items']
            if loop.is_valid and not loop.face.hide and (sync or loop.face.select)]


def _live_target_triangles(context, obj, bm, uvl):
    """Read retained triangle corners; never rebuild wrappers during a gesture."""
    from . import live_symmetry
    if live_symmetry._suspended:
        return []
    entry = (live_symmetry._cache.get(obj.data.as_pointer()) if _transform_running()
             else live_symmetry._prepare(obj,cached=True))
    if not entry or not entry['active'] or entry['bm'] != bm or entry['uv_name'] != uvl.name:
        return []
    sync = context.scene.tool_settings.use_uv_select_sync
    coords = []
    for positions in entry['target_triangles']:
        face = entry['loops'][positions[0]].face
        if face.hide or (not sync and not face.select):
            continue
        coords.extend(tuple(entry['loops'][p][uvl].uv) for p in positions)
    return coords


def draw_uv_editor():
    context = bpy.context
    space = getattr(context, "space_data", None)
    if space is None or space.type != "IMAGE_EDITOR" or not space.show_uvedit:
        return

    show, width, palette, tint_on, selected_rgb, blend = _settings(context)
    try:
        sync = bool(context.scene.tool_settings.use_uv_select_sync)
    except Exception:
        sync = False
    selection_mode = core.uv_selection_mode(context)

    # Two buckets per kind: plain, and selected. Draw plain first so a selected
    # segment is never overwritten by a neighbour that happens to come later.
    buckets = {}
    for kind in (C_HORIZONTAL, C_VERTICAL, C_ALIGNED, C_CIRCULAR, C_RECTANGULAR):
        buckets[(kind, False)] = []
        buckets[(kind, True)] = []
    prefs = _prefs(context)
    fill_color = tuple(prefs.symmetry_fill_color) if prefs else RIZOM_SRGB_SELECTED
    fill_opacity = prefs.symmetry_fill_opacity if prefs else .12
    triangles = []
    objects = getattr(context, 'objects_in_mode_unique_data', ()) or [context.edit_object]
    for obj in objects:
        if obj is None or obj.type != 'MESH' or obj.mode != 'EDIT':
            continue
        bm = _edit_bmesh(obj)
        if bm is None:
            continue
        uvl = bm.loops.layers.uv.active
        if uvl is None:
            continue
        if fill_opacity > 0:
            triangles.extend(_live_target_triangles(context, obj, bm, uvl))
        if show:
            from . import shape_constraints
            tint_selection = bool(tint_on) and (not sync or bm.uv_select_sync_valid)
            for loop, kind in _visible_constrained_loops(bm, sync,obj):
                selected = tint_selection and _uv_edge_selected(loop, sync, uvl, selection_mode)
                a, b = loop[uvl].uv, loop.link_loop_next[uvl].uv
                buckets[(kind, selected)].extend((a[0], a[1], b[0], b[1]))
            for loop,kind in shape_constraints.visible_edges(obj,bm,uvl,sync):
                selected=tint_selection and _uv_edge_selected(loop,sync,uvl,selection_mode)
                a,b=loop[uvl].uv,loop.link_loop_next[uvl].uv
                buckets[(kind,selected)].extend((a[0],a[1],b[0],b[1]))

    if not triangles and not any(buckets.values()):
        return

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    old_blend = gpu.state.blend_get()
    old_width = gpu.state.line_width_get()
    try:
        gpu.state.blend_set('ALPHA')
        if triangles:
            batch = batch_for_shader(shader, 'TRIS', {'pos': triangles})
            shader.uniform_float('color', (*fill_color, fill_opacity))
            batch.draw(shader)
        gpu.state.line_width_set(width)
        for (kind, selected), coords in buckets.items():
            if not coords:
                continue
            batch = batch_for_shader(
                shader, "LINES",
                {"pos": [(coords[i], coords[i + 1])
                         for i in range(0, len(coords), 2)]},
            )
            shader.uniform_float(
                "color", _color_for(kind, selected, palette, selected_rgb, blend))
            batch.draw(shader)
    finally:
        gpu.state.line_width_set(old_width)
        gpu.state.blend_set(old_blend)


def register_handlers():
    global _uv_handle
    if _mesh_updated not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_mesh_updated)
    if _uv_handle is None:
        _uv_handle = bpy.types.SpaceImageEditor.draw_handler_add(
            draw_uv_editor, (), "WINDOW", "POST_VIEW"
        )


def unregister_handlers():
    global _uv_handle
    if _uv_handle is not None:
        bpy.types.SpaceImageEditor.draw_handler_remove(_uv_handle, "WINDOW")
        _uv_handle = None
    if _mesh_updated in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_mesh_updated)
    # Disabling from another script must also leave modal transform pointers
    # intact until that gesture finishes. These wrappers never own the mesh.
    if _transform_running():
        if not bpy.app.timers.is_registered(_release_bmeshes_when_idle):
            bpy.app.timers.register(_release_bmeshes_when_idle, first_interval=0.1)
    else:
        _loop_cache.clear()
        _bm_handles.clear()
    _mesh_updates.clear()
