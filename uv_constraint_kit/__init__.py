# SPDX-License-Identifier: GPL-3.0-or-later
"""UV Constraint Kit -- RizomUV-style edge constraints for Blender's UV editor.

Adds RizomUV's edge constraints to the UV editor:

    Constrain Edge(s) Horizontally   (uv.uvck_constrain_horizontal)
    Constrain Edge(s) Vertically     (uv.uvck_constrain_vertical)
    Constrain Edge(s) Aligned        (uv.uvck_constrain_aligned)
    Unconstrain Edge(s)              (uv.uvck_unconstrain)
    Optimize                         (uv.uvck_optimize)

Constrained edges use configurable overlay colors. Marking a constraint (or
pressing Optimize) re-solves the island it belongs to with the constrained edges
held as hard boundary conditions, then fits the island's position, size and texel
density back -- the layout around it is never disturbed.

Language
--------
The interface uses Blender translations; English and Simplified Chinese are
supported without requiring an external translation extension.

Persistence
-----------
The constraint marks live in a CORNER/INT layer on the mesh, and are mirrored
onto each object as an IDProperty because a Mesh datablock saved while its
object is in edit mode does not contain that layer. `load_post` rebuilds the
layer from the mirror on load; see `core.CONSTRAINT_STORE`.
"""

import bpy

from . import core, i18n, ops, overlay, prefs, ui, symmetry, circle_ops, shape_ops, live_symmetry
from . import shape_constraints, native_engine

__all__ = ("register", "unregister")


@bpy.app.handlers.persistent
def _on_data_changed(_dummy=None):
    overlay.bump_revision()
    core.invalidate_stats_cache()
    overlay.request_redraw()


@bpy.app.handlers.persistent
def _on_history_changed(_dummy=None):
    # Edit-mesh undo restores corner attributes, but not Object ID properties.
    # Rebuild the persistence mirror from the restored layer, including clearing
    # it when the first mark has just been undone. UVs remain untouched.
    for obj in getattr(bpy.context, "objects_in_mode_unique_data", ()):
        if obj.type != 'MESH' or obj.mode != 'EDIT':
            continue
        bm = overlay._edit_bmesh(obj)
        if bm is not None and (core.CONSTRAINT_STORE in obj or
                               core.constraint_layer(bm) is not None):
            core.store_constraints(obj, bm)
    _on_data_changed()


@bpy.app.handlers.persistent
def _on_load(_dummy=None):
    """Recover mirrored constraint layers before anything reads them.

    Registered on `load_post` *ahead* of `_on_data_changed`. A file saved while
    the user was still in edit mode does not carry the marks in its Mesh
    datablock at all -- only in the stored edit session -- so a file opened
    without that session has an empty layer. The mirror on the object is what
    brings them back; see the persistence section in `core`.
    """
    core.restore_all_constraints()


def register():
    # Translations first: the classes registered below hold `bl_label` /
    # `bl_description` source strings that Blender resolves through this catalog
    # every time it draws them, so the catalog has to exist before anything can
    # be drawn -- and, more to the point, before the user can press F10.
    i18n.register()

    bpy.utils.register_class(prefs.UVCKPreferences)
    for cls in ops.CLASSES:
        bpy.utils.register_class(cls)
    bpy.utils.register_class(symmetry.UVCK_OT_symmetrize)
    bpy.utils.register_class(symmetry.UVCK_OT_stop_symmetry)
    bpy.utils.register_class(circle_ops.UVCK_OT_circularize)
    for cls in shape_ops.CLASSES:
        bpy.utils.register_class(cls)
    for cls in ui.CLASSES:
        bpy.utils.register_class(cls)

    ui.register_menus()
    overlay.register_handlers()
    live_symmetry.register()
    shape_constraints.register()

    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.insert(0, _on_load)
    for collection in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _on_history_changed not in collection:
            collection.append(_on_history_changed)
    if _on_data_changed not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_data_changed)


def unregister():
    shape_constraints.unregister()
    live_symmetry.unregister()
    overlay.unregister_handlers()
    ui.unregister_menus()

    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    for collection in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _on_history_changed in collection:
            collection.remove(_on_history_changed)
    if _on_data_changed in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_data_changed)

    for cls in reversed(ui.CLASSES):
        bpy.utils.unregister_class(cls)
    for cls in reversed(shape_ops.CLASSES):
        bpy.utils.unregister_class(cls)
    bpy.utils.unregister_class(circle_ops.UVCK_OT_circularize)
    bpy.utils.unregister_class(symmetry.UVCK_OT_symmetrize)
    bpy.utils.unregister_class(symmetry.UVCK_OT_stop_symmetry)
    for cls in reversed(ops.CLASSES):
        bpy.utils.unregister_class(cls)
    bpy.utils.unregister_class(prefs.UVCKPreferences)

    # Last: pull the catalog only once nothing that uses it is registered.
    i18n.unregister()
    native_engine.shutdown_gpu()
