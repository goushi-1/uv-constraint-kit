# SPDX-License-Identifier: GPL-3.0-or-later
# Integration modifications for UV Constraint Kit through 2026-10-23.
# SPDX-FileCopyrightText: 2025 Oxicid
# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone adaptation of UniV 4.0's UV Symmetrize by Oxicid.

Preserves its local 3D face matching, selected-island ordering, UV tile/cursor
pivot and linked-corner behavior. Uses our own selection and island plumbing;
does not overwrite BMesh face indices or depend on UniV being installed.
"""

import math
import bmesh
import bpy
from mathutils import Vector, kdtree
from bpy.props import EnumProperty, BoolProperty, FloatProperty
from . import overlay, core, ops, live_symmetry
from .i18n import t


def _pair(a, b, uv):
    return (a[uv].uv == b.link_loop_next[uv].uv and
            a.link_loop_next[uv].uv == b[uv].uv)


def _components(selected, uv):
    remaining=set(selected);out=[]
    for face in selected:
        if face not in remaining:continue
        remaining.remove(face);island=[];pending=[face]
        while pending:
            f=pending.pop();island.append(f)
            for loop in f.loops:
                other=loop.link_loop_radial_prev
                if (other.face in remaining and not loop.edge.seam and _pair(loop,other,uv)):
                    remaining.remove(other.face);pending.append(other.face)
        out.append(island)
    return out


def _linked_corners(corner, uv, visible):
    """UV-contiguous corner fan, including the destination corner."""
    found={corner};pending=[corner]
    while pending:
        loop=pending.pop()
        edges=(loop,loop.link_loop_prev)
        for edge_loop in edges:
            pair=edge_loop.link_loop_radial_prev
            candidate=pair.link_loop_next if edge_loop==loop else pair
            if (candidate not in found and candidate.vert==corner.vert and
                    candidate.face in visible and _pair(edge_loop,pair,uv)):
                found.add(candidate);pending.append(candidate)
    return found


def symmetrize_mesh(context,obj,axis_uv,axis_uv_flip,axis_3d,by_cursor,unlink,threshold,capture=None,center=None):
    bm=overlay._edit_bmesh(obj) if capture is not None else bmesh.from_edit_mesh(obj.data)
    uv=bm.loops.layers.uv.active
    if uv is None:return 0,0
    sync=context.scene.tool_settings.use_uv_select_sync
    visible=[f for f in bm.faces if not f.hide and (sync or f.select)]
    valid=bm.uv_select_sync_valid
    selected=[f for f in visible if (f.select if sync and not valid else f.uv_select)]
    if not selected:return 0,0
    uv_axis=0 if axis_uv=='-X to +X' else 1
    if capture is not None:
        core.prepare_bm(bm)
        positions=core.loop_positions(bm)
        capture.update(axis=uv_axis, axis_3d=axis_3d, unlink=unlink, pairs=[],
                       before=[tuple(loop[uv].uv) for loop in core.loops_in_order(bm)],
                       previous_live=live_symmetry._raw(bm,core.loops_in_order(bm)))
        capture['island_roots'] = {positions[loop]: i
            for i, island in enumerate(_components(visible, uv))
            for face in island for loop in face.loops}
    xyz_axis={'X':0,'Y':1,'Z':2}[axis_3d]
    xyz_scale=Vector((1,1,1));xyz_scale[xyz_axis]=-1
    uv_scale=Vector((1,1));uv_scale[uv_axis]=-1
    tree=kdtree.KDTree(len(visible))
    for i,f in enumerate(visible):tree.insert(f.calc_center_median(),i)
    tree.balance()
    islands=_components(selected,uv)
    centers={}
    for i,island in enumerate(islands):
        values=[l[uv].uv[uv_axis] for f in island for l in f.loops]
        centers[i]=(min(values)+max(values))*.5
    order=sorted(range(len(islands)),key=lambda i:centers[i]%1,reverse=axis_uv_flip)
    island_of={f:i for i,island in enumerate(islands) for f in island}
    pivots={i:math.floor(centers[i])+.5 for i in range(len(islands))}
    chosen_center = tuple(center) if center is not None else (.5, .5)
    if center is not None:
        if len(center) != 2 or not all(math.isfinite(p) for p in center):
            raise ValueError('Invalid symmetry center')
        pivots = {i:float(center[uv_axis]) for i in pivots}
    if by_cursor:
        if context.space_data is None or context.space_data.type!='IMAGE_EDITOR':
            raise ValueError('UV cursor requires the UV editor')
        pivot=float(context.space_data.cursor_location[uv_axis])
        pivots={i:pivot for i in pivots}
        chosen_center = tuple(context.space_data.cursor_location)
    if not all(math.isfinite(p) for p in chosen_center):
        raise ValueError('Invalid symmetry center')
    if capture is not None:
        capture['center'] = chosen_center
        capture['settings'] = dict(axis_uv=axis_uv, axis_uv_flip=bool(axis_uv_flip),
            axis_3d=axis_3d, by_cursor=bool(by_cursor), unlink=bool(unlink),
            threshold=float(threshold), center_x=float(center[0]) if center is not None else .5,
            center_y=float(center[1]) if center is not None else .5, live=True)
    available=set(visible);visible_set=set(visible)
    matched=missing=0
    for i in order:
        for source in sorted(islands[i],key=lambda f:f.calc_center_median()[xyz_axis]):
            if source not in available:continue
            available.remove(source)
            for _,idx,_ in tree.find_range(source.calc_center_median()*xyz_scale,threshold):
                target=visible[idx]
                # UniV's original guard compares destination size with itself.
                # Reject unlike polygons instead of assigning corners arbitrarily.
                if target not in available or len(source.loops)!=len(target.loops):continue
                available.remove(target)
                src,dst=source,target
                if axis_uv_flip and island_of.get(dst)==i:src,dst=dst,src
                vertices=list(dst.verts)
                vt=kdtree.KDTree(len(vertices))
                for j,v in enumerate(vertices):vt.insert(v.co,j)
                vt.balance()
                for loop in src.loops:
                    _,j,distance=vt.find(loop.vert.co*xyz_scale)
                    if capture is not None and distance > threshold:
                        raise ValueError('Live symmetry corner matching exceeded the threshold')
                    dest=dst.loops[j]
                    value=loop[uv].uv*uv_scale
                    value[uv_axis]+=2*pivots[i]
                    corners={dest} if unlink else _linked_corners(dest,uv,visible_set)
                    if capture is not None:
                        capture['pairs'].append((positions[loop], positions[dest],
                                                 sorted(positions[c] for c in corners), pivots[i]))
                    for corner in corners:corner[uv].uv=value
                matched+=1
                break
            else:missing+=1
    if matched:bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
    return matched,missing


class UVCK_OT_symmetrize(bpy.types.Operator):
    bl_idname='uv.uvck_symmetrize'
    bl_label='Symmetrize'
    bl_description='Mirror UVs between faces paired across the local mesh symmetry axis'
    bl_options={'REGISTER','UNDO'}

    axis_uv:EnumProperty(name='UV Axis',default='-X to +X',items=(('-X to +X','-X to +X',''),('-Y to +Y','-Y to +Y','')))
    axis_uv_flip:BoolProperty(name='Flip UV Axis',default=False)
    axis_3d:EnumProperty(name='3D Axis',default='X',items=(('X','X',''),('Y','Y',''),('Z','Z','')))
    by_cursor:BoolProperty(name='By Cursor',default=True)
    center_x:FloatProperty(name='Symmetry center X',default=.5,soft_min=0,soft_max=1,precision=3)
    center_y:FloatProperty(name='Symmetry center Y',default=.5,soft_min=0,soft_max=1,precision=3)
    unlink:BoolProperty(name='Unlink',default=False)
    threshold:FloatProperty(name='Symmetry Threshold',default=.005,soft_min=.005,min=.00001,soft_max=1,max=10,precision=5)
    live:BoolProperty(name='Live symmetry link',default=False,
                      description='Keep target UV corners mirrored when source corners change')

    @classmethod
    def poll(cls,context):
        return bool(not overlay._transform_running() and context.mode=='EDIT_MESH' and
                    context.edit_object and context.edit_object.data.uv_layers)

    def draw(self,context):
        layout=self.layout
        row=layout.row(align=True,heading=t('UV Axis'));row.prop(self,'axis_uv',expand=True)
        row.prop(self,'axis_uv_flip',icon_only=True,icon='ARROW_LEFTRIGHT')
        row=layout.row(align=True,heading=t('3D Axis'));row.prop(self,'axis_3d',expand=True)
        layout.prop(self,'unlink',text=t('Unlink UV Corners'))
        layout.prop(self,'by_cursor',text=t('Use UV Cursor'))
        col = layout.column(align=True)
        col.active = not self.by_cursor
        col.prop(self, 'center_x', text=t('Symmetry center X'), slider=True)
        col.prop(self, 'center_y', text=t('Symmetry center Y'), slider=True)
        layout.prop(self,'threshold')
        layout.prop(self,'live')

    def execute(self,context):
        if overlay._transform_running():
            self.report({'WARNING'},t('Finish the current transform before symmetrizing'))
            return {'CANCELLED'}
        if self.by_cursor and (context.space_data is None or context.space_data.type!='IMAGE_EDITOR'):
            self.report({'WARNING'},t('UV cursor requires the UV editor'))
            return {'CANCELLED'}
        total=missing=0
        skipped=[]
        objects=getattr(context,'objects_in_mode_unique_data',None) or [context.edit_object]
        for obj in objects:
            if obj.type!='MESH' or not obj.data.uv_layers:continue
            capture={}
            saved=capture
            try:
                matched,unmatched=symmetrize_mesh(context,obj,self.axis_uv,self.axis_uv_flip,self.axis_3d,self.by_cursor,self.unlink,self.threshold,capture,
                                                center=(self.center_x,self.center_y))
                if matched and self.live:
                    live_symmetry.install(obj,capture)
                elif matched:
                    touched = {p for source,target,fan,_ in capture['pairs']
                               for p in (source,target,*fan)}
                    live_symmetry.clear_selected_targets(context,obj,touched)
                    bm=overlay._edit_bmesh(obj)
                    live_symmetry.mirror_constraints(obj,[dict(
                        uv_name=bm.loops.layers.uv.active.name,axis=capture['axis'],
                        pairs=[[s,target,pivot] for s,target,_,pivot in capture['pairs']])])
            except Exception as error:
                if saved and 'before' in saved:
                    bm=overlay._edit_bmesh(obj)
                    uv=bm.loops.layers.uv.active
                    for loop,value in zip(core.loops_in_order(bm),saved['before']):
                        loop[uv].uv=value
                    if saved['previous_live'] or bm.loops.layers.string.get(live_symmetry.LAYER):
                        live_symmetry._write(bm,core.loops_in_order(bm),saved['previous_live'])
                    live_symmetry._cache.pop(obj.data.as_pointer(),None)
                    bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
                skipped.append(t(str(error)))
                continue
            total+=matched;missing+=unmatched
        overlay.bump_revision();overlay.request_redraw()
        if not total:
            self.report({'WARNING'},'; '.join(skipped) or t('No selected symmetric face pairs found'))
            return {'CANCELLED'}
        message=t('Symmetrized %d face pairs; %d source faces unmatched')%(total,missing)
        if self.live:message+='; '+t('Live symmetry linked; edit the source side to update the target')
        if skipped:message+='; '+'; '.join(skipped)
        self.report({'INFO'},message)
        return {'FINISHED'}


class UVCK_OT_stop_symmetry(bpy.types.Operator):
    bl_idname='uv.uvck_stop_symmetry'
    bl_label='Stop symmetry link'
    bl_description='Keep the current UVs and stop source-to-target updates'
    bl_options={'REGISTER','UNDO'}

    @classmethod
    def poll(cls,context):
        return not overlay._transform_running() and live_symmetry.has_links(context)

    def execute(self,context):
        if overlay._transform_running():
            self.report({'WARNING'},t('Finish the current transform before symmetrizing'))
            return {'CANCELLED'}
        for obj in live_symmetry._objects(context):
            live_symmetry.clear_selected_targets(context,obj,bake=True)
        overlay.bump_revision();overlay.request_redraw()
        return {'FINISHED'}
