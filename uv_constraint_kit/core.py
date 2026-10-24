# SPDX-License-Identifier: GPL-3.0-or-later
"""Core geometry / solving logic for UV Constraint Kit.

Constraints are stored per **UV edge** (a loop plus its next loop), not per mesh
edge. That distinction matters on seams: a mesh edge that is a seam belongs to
two UV islands, so a per-mesh-edge constraint would silently constrain -- and
colour -- the copy of that edge in the other island too.

How a constraint is enforced
----------------------------
An edge constraint fixes a **direction, not a location**. "This edge is
horizontal" means its two ends share a V; it does not say *which* V. That
distinction is the whole reason two constraints on one island move each other:
add a vertical constraint and the horizontal one gets dragged to a new V while
staying exactly horizontal. RizomUV behaves that way, and pinning the run to a
computed position cannot reproduce it -- the position would be frozen.

    1. initialize a feasible boundary from the current UVs. Curved runs may
       require frame transport or gradual constraint loading to avoid folds.
    2. minimize metric distortion on the entire affected island. The independent
       CPU solver uses projected directions, user pins and a determinant
       barrier. Its C++ iteration engine has a Python fallback. Native Blender
       unwrap is only used for the optional hole fill and guarded recovery.
    3. restore each island's area, preserving fixed coordinates, then validate
       constraints, triangle winding, new overlaps and unchanged outside UVs.
       Any unsuccessful attempt restores the operation's original UV layout.

Each island has its own pins, pose and area. The parameter names and defaults
follow the requested RizomUV workflow; this is an independent implementation,
not a recovered proprietary solver. Operation parameters are exposed in F9.

The user's own UV pins are a different kind of thing and are honoured as pins:
they are held through the re-solve and restored afterwards.

Everything here works on a BMesh taken from the *edit* mesh, because while an
object is in edit mode `mesh.uv_layers[..].data` is empty -- only the BMesh
sees live UV data.
"""

import math
from itertools import combinations

import bmesh
import bpy
import numpy as np
from mathutils import Vector
from . import native_engine, mesh_reader

from .const import (
    C_ALIGNED,
    C_HORIZONTAL,
    C_NONE,
    C_VERTICAL,
    CONSTRAINT_ATTR,
    UV_EPS,
)

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _uv_close(a, b):
    return abs(a[0] - b[0]) <= UV_EPS and abs(a[1] - b[1]) <= UV_EPS


def prepare_bm(bm):
    """Make element indices usable for direct `bm.<elem>[i]` access.

    Note: `bm.loops` is a BMLoopSeq -- neither indexable nor iterable, with no
    lookup table -- so loops are always reached through `loops_in_order`.
    """
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.verts.index_update()
    bm.edges.index_update()
    bm.faces.index_update()


def loops_in_order(bm):
    """All loops in face-major order.

    `BMLoop.index` is not reliably maintained inside an edit-mesh BMesh, so
    nothing here trusts it; the returned list's position *is* the index used
    everywhere else.
    """
    out = []
    for face in bm.faces:
        out.extend(face.loops)
    return out


def loop_positions(bm):
    """{BMLoop: position} matching the face-major order above."""
    return {loop: i for i, loop in enumerate(loops_in_order(bm))}


# ---------------------------------------------------------------------------
# constraint bookkeeping
# ---------------------------------------------------------------------------


def constraint_layer(bm, create=False):
    """The CORNER-domain INT layer holding per-UV-edge constraint kinds."""
    layer = bm.loops.layers.int.get(CONSTRAINT_ATTR)
    if layer is None and create:
        layer = bm.loops.layers.int.new(CONSTRAINT_ATTR)
    return layer


# Counting in edit mode cannot go through the Mesh datablock (its attributes
# are stale while the object is in edit mode), so results are cached briefly.
_stats_cache = {"key": None, "value": (0, {}), "time": 0.0}


def constraint_stats(context, obj):
    """(total, {kind: count}) over the object's UV edges."""
    mesh = obj.data
    if obj.mode != "EDIT":
        attr = mesh.attributes.get(CONSTRAINT_ATTR)
        if attr is None:
            return 0, {}
        per_kind = {}
        for item in attr.data:
            if item.value != C_NONE:
                per_kind[item.value] = per_kind.get(item.value, 0) + 1
        return sum(per_kind.values()), per_kind

    import time
    now = time.time()
    key = (obj.name, mesh.name, len(mesh.loops))
    if _stats_cache["key"] == key and now - _stats_cache["time"] < 0.4:
        return _stats_cache["value"]

    bm = bmesh.from_edit_mesh(mesh)
    layer = constraint_layer(bm)
    per_kind = {}
    if layer is not None:
        for face in bm.faces:
            for loop in face.loops:
                value = loop[layer]
                if value != C_NONE:
                    per_kind[value] = per_kind.get(value, 0) + 1
    value = (sum(per_kind.values()), per_kind)
    _stats_cache.update(key=key, value=value, time=now)
    return value


def invalidate_stats_cache():
    """Drop the memoised constraint count.

    Called by every operator that changes the constraint layer. Without it the
    panel can keep showing the pre-edit count for up to the cache's lifetime:
    the cache key is (object, mesh, loop count), and marking or unmarking an
    edge changes none of those three.
    """
    _stats_cache.update(key=None, value=(0, {}), time=0.0)


# ---------------------------------------------------------------------------
# constraint persistence: a mode-independent mirror on the Object
# ---------------------------------------------------------------------------
#
# A BMesh CORNER/INT layer is *not* the Mesh datablock while the object is in
# edit mode.  Measured on 5.2.2 (`_tools/persist_d.py`), immediately after
# marking eight edges:
#
#     mesh.attributes["uvck_constraint"]   -> 0 items
#     BMesh layer                          -> 8 non-zero of 64
#
# The Mesh only learns about the layer when edit mode is left.  So a file saved
# while the user is still in edit mode does not carry the marks in its Mesh
# datablock at all -- they ride along only inside the stored edit session.
# Anything that opens that file without restoring the session (a scripted
# import, a crash-recovery path, a Blender that changes the restore rule, or a
# tool that reads the mesh directly) sees an empty constraint layer, which is
# reported as "the constraint edges are gone after a restart".
#
# The marks are therefore mirrored onto the Object as an IDProperty, which
# Blender writes into the .blend in *every* mode.  Division of labour: the
# BMesh layer stays the source of truth for behaviour, the mirror is the source
# of truth for recovery.  `restore_constraints` rebuilds the layer from it on
# load, and only when the layer is genuinely empty -- and because clearing the
# constraints deletes the mirror as well, an intentional clear is never undone.
#
# Record layout, as one flat int array:
#     [version, position, v_a, v_b, kind] * n
#   position -- face-major loop index, i.e. the same identity the overlay and
#               the solver use for a UV edge
#   v_a, v_b -- the mesh vertex indices of that edge, carried as a *check*.  If
#               topology changed enough to move the loop, the record is
#               re-found by vertex pair and dropped if that fails too; without
#               the check a stale position would silently mark a wrong edge.

CONSTRAINT_STORE = "uvck_constraints"
CONSTRAINT_STORE_VERSION = 1
_STORE_STRIDE = 4


def _store_records(bm):
    """The mirror's payload for the layer a BMesh is currently holding."""
    layer = constraint_layer(bm)
    if layer is None:
        return []
    prepare_bm(bm)
    out = []
    for position, loop in enumerate(loops_in_order(bm)):
        kind = loop[layer]
        if kind == C_NONE:
            continue
        edge = loop.edge
        out.extend((position, edge.verts[0].index, edge.verts[1].index, kind))
    return out


def store_constraints(obj, bm):
    """Mirror a BMesh's constraint layer onto the object.  Returns the count.

    Call after every mutation of the layer.  An empty layer deletes the mirror
    rather than storing an empty array, so "the property exists" always means
    "there is something to recover".
    """
    data = _store_records(bm)
    if data:
        obj[CONSTRAINT_STORE] = [CONSTRAINT_STORE_VERSION] + data
    elif CONSTRAINT_STORE in obj:
        del obj[CONSTRAINT_STORE]
    return len(data) // _STORE_STRIDE


def store_constraints_from_object(obj):
    """Mirror the object's current state, whatever mode it is in."""
    if obj.mode == "EDIT":
        return store_constraints(obj, bmesh.from_edit_mesh(obj.data))
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        return store_constraints(obj, bm)
    finally:
        bm.free()


def _records(obj):
    """The mirror's records, or [] if absent / from another version / malformed."""
    raw = obj.get(CONSTRAINT_STORE)
    if not raw:
        return []
    try:
        values = [int(v) for v in raw]
    except (TypeError, ValueError):
        return []
    if not values or values[0] != CONSTRAINT_STORE_VERSION:
        return []
    if (len(values) - 1) % _STORE_STRIDE:
        return []
    return [tuple(values[i:i + _STORE_STRIDE])
            for i in range(1, len(values), _STORE_STRIDE)]


def _layer_has_marks(bm):
    layer = constraint_layer(bm)
    if layer is None:
        return False
    return any(loop[layer] != C_NONE for face in bm.faces
               for loop in face.loops)


def _write_records(bm, records):
    """Map records onto the BMesh's loops and write them into the layer.

    Returns how many records found an edge.  A record's stored `position` is
    trusted only when the edge there still has the recorded vertices, so a
    topology change degrades to a vertex-pair search instead of marking an
    unrelated edge.
    """
    loops = loops_in_order(bm)
    prepare_bm(bm)
    by_edge = {}
    for position, loop in enumerate(loops):
        verts = loop.edge.verts
        by_edge.setdefault((verts[0].index, verts[1].index), []).append(position)

    resolved = []
    used = set()
    deferred = []
    for position, v_a, v_b, kind in records:
        key = (v_a, v_b)
        if 0 <= position < len(loops) and position not in used:
            verts = loops[position].edge.verts
            if (verts[0].index, verts[1].index) == key:
                resolved.append((position, kind))
                used.add(position)
                continue
        deferred.append((key, kind))

    for key, kind in deferred:
        candidates = by_edge.get(key) or by_edge.get((key[1], key[0])) or []
        pick = next((p for p in candidates if p not in used), None)
        if pick is None:
            continue
        resolved.append((pick, kind))
        used.add(pick)

    if not resolved:
        return 0
    # Create the layer first: adding a custom-data layer invalidates every
    # existing element reference, so `loops` has to be re-read afterwards.
    constraint_layer(bm, create=True)
    loops = loops_in_order(bm)
    layer = constraint_layer(bm)
    for position, kind in resolved:
        loops[position][layer] = kind
    return len(resolved)


def restore_constraints(obj):
    """Rebuild the layer from the object mirror.  Returns how many were restored.

    A no-op unless the layer is empty, so it is safe to call on every load: a
    file whose marks are already in place is never touched.
    """
    records = _records(obj)
    if not records:
        return 0

    mesh = obj.data
    if obj.mode == "EDIT":
        bm = bmesh.from_edit_mesh(mesh)
        if _layer_has_marks(bm):
            return 0
        restored = _write_records(bm, records)
        if restored:
            bmesh.update_edit_mesh(mesh)
        return restored

    attr = mesh.attributes.get(CONSTRAINT_ATTR)
    if attr is not None and any(item.value != C_NONE for item in attr.data):
        return 0
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        restored = _write_records(bm, records)
        if restored:
            bm.to_mesh(mesh)
            mesh.update()
    finally:
        bm.free()
    return restored


def restore_all_constraints():
    """`load_post` handler: recover any object that carries a mirror."""
    restored = 0
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        if CONSTRAINT_STORE not in obj:
            continue
        try:
            restored += restore_constraints(obj)
        except Exception:
            # A file load must never fail because one object's mirror is odd.
            pass
    return restored


# ---------------------------------------------------------------------------
# UV topology: UV-vertices and UV islands
# ---------------------------------------------------------------------------


class UVTopology:
    """Union-find over the BMesh giving UV-vertices and UV-islands.

    A *UV-vertex* is a group of loops that share both a mesh vertex and the
    same UV coordinate -- i.e. one draggable point in the UV editor. Working
    per UV-vertex (rather than per mesh vertex) is what makes seams behave:
    the two sides of a seam are two independent UV-vertices, exactly as
    RizomUV treats them.
    """

    def __init__(self, bm, uvl):
        self.bm = bm
        self.uvl = uvl
        prepare_bm(bm)
        captured = mesh_reader.capture(bm, uvl)
        if captured is None:
            self.loops = loops_in_order(bm)
            self.loop_pos = {loop: i for i, loop in enumerate(self.loops)}
            sizes = np.fromiter((len(face.loops) for face in bm.faces), dtype=np.int64)
            coordinates = [tuple(loop[uvl].uv) for loop in self.loops]
            self._loop_vertices = np.asarray([loop.vert.index for loop in self.loops], dtype=np.int64)
        else:
            self.loops, self.loop_pos, coordinates, face_sizes, vertices, offsets, radial = captured
            sizes = np.asarray(face_sizes, dtype=np.int64)
            self._loop_vertices = np.asarray(vertices, dtype=np.int64)
            self._radial_connectivity = (np.asarray(offsets, dtype=np.int64),
                                         np.asarray(radial, dtype=np.int64))
            del captured, face_sizes, vertices, offsets, radial
        self.loop_count = len(self.loops)
        # Face-major loop order supplies exact face/next indices without
        # repeatedly reading wrapped BMesh links during numerical solves.
        ends = np.cumsum(sizes)
        following = np.arange(1, self.loop_count+1, dtype=np.int64)
        if len(ends):
            following[ends-1] = np.concatenate(([0], ends[:-1]))
        self._loop_next_array = following
        self.loop_next = following.tolist()
        self.loop_faces = np.repeat(np.arange(len(bm.faces), dtype=np.int64), sizes)
        native_coordinates = np.asarray(coordinates, dtype=np.float64)
        native_groups = native_engine.uv_groups(
            self._loop_vertices, native_coordinates, UV_EPS)
        if native_groups is None:
            self._build_uv_vertices(coordinates)
        else:
            self.loop_to_vgroup = native_groups
            self.vgroup_loops = {}
            for position, group in enumerate(native_groups):
                self.vgroup_loops.setdefault(group, []).append(position)
        self._build_islands(coordinates, _native_coordinates=native_coordinates)
        # Numeric values only: the checked solver can reuse this exact input
        # snapshot instead of immediately reading all UV slots a second time.
        self.initial_uv = coordinates

    def refresh_uv(self):
        """Refresh UV partitions after coordinate writes within this operation.

        The mesh, loop ordering and BMesh wrapper must be unchanged. Native
        unwrap, mesh edits, undo and subsequent operations need a new topology.
        """
        coordinates = mesh_reader.read_uv(self.loops, self.uvl)
        native_coordinates = np.asarray(coordinates, dtype=np.float64)
        groups = native_engine.uv_groups(self._loop_vertices, native_coordinates, UV_EPS)
        if groups is None:
            self._build_uv_vertices(coordinates)
        else:
            self.loop_to_vgroup = groups
            self.vgroup_loops = {}
            for position, group in enumerate(groups):
                self.vgroup_loops.setdefault(group, []).append(position)
        self._build_islands(coordinates, _native_coordinates=native_coordinates)
        self.initial_uv = coordinates
        # This mapping contains UV groups; the mesh tessellation itself is
        # unchanged by coordinate writes and remains valid in this instance.
        self.__dict__.pop('_triangle_groups', None)

    def loop_triangles(self):
        """Reuse the immutable mesh tessellation within this topology only.

        Native unwrap rebuilds UVTopology. Never retain this BMesh cache across
        operations, mesh edits, undo, or a modal transform.
        """
        triangles = getattr(self, '_loop_triangles', None)
        if triangles is None:
            triangles = self._loop_triangles = self.bm.calc_loop_triangles()
        return triangles

    # -- UV vertices ------------------------------------------------------

    def _build_uv_vertices(self, coordinates=None):
        """Group loops into UV-vertices.

        A loop's UV slot holds the UV of *one* corner -- the one at `loop.vert`
        -- so a loop maps 1:1 onto a corner. Two corners are the same
        UV-vertex when they sit on the same mesh vertex and carry the same UV
        (that is exactly what makes a seam split them apart).

        The merge has to look at every loop around a vertex, not just the pairs
        sharing an edge: at a vertex where four quads meet, the corners are
        connected through the vertex, not through any single edge.
        """
        n = self.loop_count
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        uvl = self.uvl
        # Copy each corner once. Reading a wrapped BMesh UV for every pair at
        # a vertex dominates small-island solves on a larger edit mesh.
        if coordinates is None:
            coordinates = [tuple(loop[uvl].uv) for loop in self.loops]
        elif isinstance(coordinates, np.ndarray):
            # The Python union uses coordinate equality and dictionary keys.
            # Preserve its original tuple semantics when the engine falls back.
            coordinates = list(map(tuple, coordinates.tolist()))
        by_vert = {}
        for position, loop in enumerate(self.loops):
            by_vert.setdefault(loop.vert.index, []).append(position)

        for positions in by_vert.values():
            if len(positions) < 2:
                continue
            first = coordinates[positions[0]]
            if (math.isfinite(first[0]) and math.isfinite(first[1])
                    and all(coordinates[p] == first for p in positions[1:])):
                # The pairwise union of a complete clique ends at its final
                # corner. Keep that exact representative and member order.
                root = positions[-1]
                for p in positions:
                    parent[p] = root
                continue
            groups = {}
            for p in positions:
                groups.setdefault(coordinates[p], []).append(p)
            points = list(groups)
            if (all(math.isfinite(x) and math.isfinite(y) for x, y in points)
                    and not any(_uv_close(a, b) for a, b in combinations(points, 2))):
                # Independent exact-coordinate cliques also have the final
                # corner as representative. Near-equal coordinates must use
                # the original ordered union, including non-transitive chains.
                for members in groups.values():
                    for p in members:
                        parent[p] = members[-1]
                continue
            for a, b in combinations(positions, 2):
                if _uv_close(coordinates[a], coordinates[b]):
                    union(a, b)

        self.loop_to_vgroup = [find(i) for i in range(n)]
        self.vgroup_loops = {}
        for position, group in enumerate(self.loop_to_vgroup):
            self.vgroup_loops.setdefault(group, []).append(position)

    def next_position(self, position):
        """Position of the loop that follows `position` around its face."""
        return self.loop_next[position]

    def loop_edge_vgroups(self, position):
        """(vgroup, vgroup) of the UV edge starting at `position`."""
        return (self.loop_to_vgroup[position],
                self.loop_to_vgroup[self.next_position(position)])

    def vgroup_uv(self, group):
        uv = self.loops[self.vgroup_loops[group][0]][self.uvl].uv
        return Vector((uv[0], uv[1]))

    def set_vgroup_uv(self, group, u, v):
        for position in self.vgroup_loops[group]:
            self.loops[position][self.uvl].uv = (u, v)

    # -- UV islands -------------------------------------------------------

    @staticmethod
    def _edge_uv_matches(a, b, uvl):
        """Do two loops on the same mesh edge carry the same UV across it?"""
        a0, a1 = a[uvl].uv, a.link_loop_next[uvl].uv
        b0, b1 = b[uvl].uv, b.link_loop_next[uvl].uv
        return ((_uv_close(a0, b0) and _uv_close(a1, b1))
                or (_uv_close(a0, b1) and _uv_close(a1, b0)))

    def _build_islands(self, coordinates=None, *, _native_coordinates=None):
        bm = self.bm
        nf = len(bm.faces)
        if coordinates is None:
            coordinates = [tuple(loop[self.uvl].uv) for loop in self.loops]
        structure = getattr(self, '_radial_connectivity', None)
        if structure is None:
            offsets, radial = [0], []
            for edge in bm.edges:
                radial.extend([self.loop_pos[loop] for loop in edge.link_loops])
                offsets.append(len(radial))
            structure = self._radial_connectivity = (
                np.asarray(offsets, dtype=np.int64), np.asarray(radial, dtype=np.int64))
        offsets, radial = structure
        native = native_engine.islands(
            nf, offsets, radial, self.loop_faces, self._loop_next_array,
            coordinates if _native_coordinates is None else _native_coordinates, UV_EPS)
        if native is not None:
            self.face_island = native
            self.island_faces = {}
            for fi, root in enumerate(native):
                self.island_faces.setdefault(root, []).append(fi)
            return
        parent = list(range(nf))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        if coordinates is None:
            coordinates = [tuple(loop[self.uvl].uv) for loop in self.loops]
        for edge in bm.edges:
            for a, b in combinations(edge.link_loops, 2):
                a0, a1 = coordinates[self.loop_pos[a]], coordinates[self.loop_pos[a.link_loop_next]]
                b0, b1 = coordinates[self.loop_pos[b]], coordinates[self.loop_pos[b.link_loop_next]]
                if ((_uv_close(a0, b0) and _uv_close(a1, b1))
                        or (_uv_close(a0, b1) and _uv_close(a1, b0))):
                    union(a.face.index, b.face.index)

        self.face_island = [find(i) for i in range(nf)]
        self.island_faces = {}
        for fi, root in enumerate(self.face_island):
            self.island_faces.setdefault(root, []).append(fi)


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def visible_editable_faces(bm, context=None):
    """Face indices visible and in the active mesh-selection scope."""
    sync = (context.scene.tool_settings.use_uv_select_sync
            if context is not None else True)
    return {face.index for face in bm.faces
            if face.is_valid and not face.hide and (sync or face.select)}


def connected_uv_faces(bm, uvl, face_indices):
    """The full UV islands reached from faces, without building UV vertices.

    Island membership uses exactly the same edge-pair predicate as
    UVTopology. An operation resolving its targets needs only this adjacency;
    the solver still builds the complete mesh's UV vertex groups.
    """
    found = set(face_indices)
    pending = list(found)
    while pending:
        face = bm.faces[pending.pop()]
        for a in face.loops:
            for b in a.edge.link_loops:
                fi = b.face.index
                if fi not in found and UVTopology._edge_uv_matches(a, b, uvl):
                    found.add(fi)
                    pending.append(fi)
    return found


def uv_selection_mode(context):
    """Resolve the visible mode; sync hides and retains the old UV-only mode."""
    settings = context.scene.tool_settings
    if settings.use_uv_select_sync:
        vertex, edge, face = settings.mesh_select_mode
        return 'FACE' if face else 'EDGE' if edge else 'VERTEX'
    return settings.uv_select_mode


def uv_edge_selected(loop, mode):
    """Selection of one UV-side edge, shared by edits and overlay tinting."""
    if mode == 'VERTEX':
        return loop.uv_select_vert and loop.link_loop_next.uv_select_vert
    if mode in {'FACE', 'ISLAND'}:
        return loop.face.uv_select
    return loop.uv_select_edge


def selected_uv_edges(context, bm, *, mesh_fallback=True):
    """Loop positions whose UV edge is selected in the UV editor.

    Prefer UV-side flags, resolved using the mode actually shown in the editor.
    Edge mode never infers a missing edge from its selected endpoints.
    `mesh_fallback=False` also disables the pre-sync mesh selection fallback.
    """
    loops = loops_in_order(bm)
    sync = context.scene.tool_settings.use_uv_select_sync
    mode = uv_selection_mode(context)
    visible = visible_editable_faces(bm, context)
    valid = not sync or bm.uv_select_sync_valid
    selected = ({position for position, loop in enumerate(loops)
                 if loop.face.index in visible and uv_edge_selected(loop, mode)}
                if valid else set())
    if selected or not (sync and mesh_fallback):
        return selected

    # With UV sync on, Blender mirrors the mesh selection into the UV flags,
    # but only after a sync pass -- so map the mesh selection as a fallback.
    if mode == 'FACE':
        selected_mesh = lambda loop: loop.face.select
    elif mode == 'VERTEX':
        selected_mesh = lambda loop: loop.vert.select and loop.link_loop_next.vert.select
    else:
        selected_mesh = lambda loop: loop.edge.select
    return {p for p, loop in enumerate(loops)
            if loop.face.index in visible and not loop.edge.hide and selected_mesh(loop)}


def constrained_loops(bm, only_faces=None):
    """Loop positions whose UV edge carries a constraint."""
    layer = constraint_layer(bm)
    if layer is None:
        return []
    faces = None if only_faces is None else set(only_faces)
    out = []
    offset = 0
    for face in bm.faces:
        if faces is None or face.index in faces:
            for i, loop in enumerate(face.loops):
                if loop[layer] != C_NONE:
                    out.append(offset+i)
        offset += len(face.loops)
    return out


# ---------------------------------------------------------------------------
# constraint solving
# ---------------------------------------------------------------------------


def group_chains(topo, positions, kinds):
    """Split constrained UV edges into runs connected through shared vertices.

    `positions` are loop positions, `kinds` maps position -> constraint kind.
    Connecting through UV-vertices (not mesh vertices) keeps the two sides of a
    seam as separate runs, which is what RizomUV does too.
    """
    per_kind = {}
    for position in positions:
        kind = kinds.get(position, C_NONE)
        if kind != C_NONE:
            per_kind.setdefault(kind, []).append(position)

    chains = []
    for kind, items in per_kind.items():
        parent = {}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for position in items:
            a, b = topo.loop_edge_vgroups(position)
            parent.setdefault(a, a)
            parent.setdefault(b, b)
            union(a, b)

        grouped = {}
        for position in items:
            a, _b = topo.loop_edge_vgroups(position)
            grouped.setdefault(find(a), []).append(position)
        for positions_in_run in grouped.values():
            chains.append((kind, positions_in_run))
    return chains


def constraint_runs(bm, topo, positions):
    """The constrained UV edges, grouped into runs: [(kind, [position, ...])].

    One run per connected group of same-kind constrained edges. Runs of
    different kinds that share a vertex stay separate -- they constrain
    different axes, so they do not fight.
    """
    if not positions:
        return []
    layer = constraint_layer(bm)
    if layer is None:
        return []
    kinds = {p: topo.loops[p][layer] for p in positions}
    return group_chains(topo, positions, kinds)


def _project_run(x, index, kind, blend=1.0):
    """Re-lay one run onto its constraint manifold, in place.

    `x` is the whole (n, 2) coordinate array and `index` selects the run's
    vertices. NOTE: `x[index]` is a *copy*, so the result has to be written back
    through `x[index] = ...` -- editing the result of the fancy-index alone
    silently does nothing.

    The manifold is "every edge of the run points along X" for horizontal and
    "along Y" for vertical; for aligned it is "the run is a straight line".

    Horizontal and vertical runs retain their coordinate along the demanded
    axis and share the mean of the other coordinate. This leaves an already
    satisfied run unchanged, including its vertex order. Aligned runs use their
    principal direction. Large axis changes are handled by island rotation
    before solving; final acceptance rejects collapsed or intersecting layouts.

    Returns False when the run is too small to fit a direction to.
    """
    coords = x[index]
    if coords.shape[0] < 2:
        return False
    centroid = coords.mean(axis=0)
    rel = coords - centroid
    # principal direction of the run (total least squares, closed form)
    cxx = float((rel[:, 0] * rel[:, 0]).sum())
    cyy = float((rel[:, 1] * rel[:, 1]).sum())
    cxy = float((rel[:, 0] * rel[:, 1]).sum())
    if cxx + cyy < 1e-20:
        return False
    theta = 0.5 * math.atan2(2.0 * cxy, cxx - cyy)
    dx, dy = math.cos(theta), math.sin(theta)
    along = rel[:, 0] * dx + rel[:, 1] * dy

    if kind == C_HORIZONTAL:
        target = coords.copy()
        target[:, 1] = centroid[1]
    elif kind == C_VERTICAL:
        target = coords.copy()
        target[:, 0] = centroid[0]
    else:                                       # aligned: keep its own trend
        target = np.empty_like(coords)
        target[:, 0] = centroid[0] + along * dx
        target[:, 1] = centroid[1] + along * dy
    # `blend` is how much of the way to the manifold this application goes.
    # 1.0 is the exact re-lay described above; anything less eases towards it.
    # It exists because the exact re-lay is a *hard* move: a run whose own
    # direction is perpendicular to the demanded axis is turned a quarter turn
    # in one step, and the faces around it are dragged through each other before
    # the relaxation can react. See `relax_uv`'s ramp.
    if blend < 1.0:
        target = coords + (target - coords) * blend
    x[index] = target
    return True


def project_runs(x, runs, passes=32, tol=1e-13, blend=1.0):
    """Apply every run's manifold to `x` (an (n, 2) array), in place.

    Applied **repeatedly**, not once. A vertex shared by two runs of different
    kinds -- the corner where a horizontal and a vertical constrained edge meet,
    which is the ordinary case, not an exotic one -- belongs to both manifolds
    at once, and one pass cannot satisfy both: `_project_run` re-centres the
    run's own axis as well, so the run applied last drags that corner off the
    manifold the run before it just put it on. The visible symptom is a
    constrained corner sitting a couple of degrees off both axes.

    Alternating between the manifolds converges, but **not monotonically** --
    and the first pass can be worse than doing nothing at all. Measured with
    `_tools/project_runs_trace.py` (archived as
    `楠岃瘉璇佹嵁/浜ゆ浛鎶曞奖鏀舵暃娈嬪樊.json`), maximum side deviation in degrees:

        L      two runs sharing a corner, rotated 11 deg
               11.0 -> 37.6 (one pass) -> 1.5 -> 0.000002 -> 0.0

        square all four borders of a 5x5 grid, rotated 13 deg
               13.0 -> 15.3 -> 56.2 -> 71.3 -> 82.1 -> 63.1 -> 1.4 -> 63.9
                    -> 13.3 -> 0.001 -> 26.5 -> 0.4 -> 0.000

    The square reaches exact after twelve passes. The cause is that
    `_project_run` is not a plain projection -- re-laying a run from its current
    centroid also rewrites the run's own axis, so consecutive passes
    over-correct. That is why the pass count is generous and the loop only exits
    once nothing moves.

    `relax_uv` passes `passes=1` inside its iteration loop -- there the
    projection is only a nudge and the solve redistributes the error -- and uses
    the converged default to land the constraints for good.
    """
    active = [(kind, index) for kind, index in (runs or ())
              if index.size >= 2]
    if not active:
        return
    if passes <= 1:
        for kind, index in active:
            _project_run(x, index, kind, blend)
        return
    for _ in range(passes):
        before = x.copy()
        for kind, index in active:
            _project_run(x, index, kind, blend)
        if float(np.abs(x - before).max()) <= tol:
            break
# ---------------------------------------------------------------------------
# relaxation: ARAP-style, in place, never re-unwrapping
# ---------------------------------------------------------------------------


def vgroup_positions3d(topo, groups=None):
    """{vgroup: Vector} -- every loop of a UV-vertex shares one mesh vertex."""
    out = {}
    for group in topo.vgroup_loops if groups is None else groups:
        positions = topo.vgroup_loops[group]
        out[group] = topo.loops[positions[0]].vert.co.copy()
    return out


def triangles_for_faces(topo, faces):
    """Use the same triangles as Blender and final acceptance, cached per solve."""
    faces = sorted(faces)
    by_face = getattr(topo, '_triangle_groups', None)
    if by_face is None:
        by_face = topo._triangle_groups = {}
    missing = set(faces).difference(by_face)
    if missing:
        for fi in missing:
            by_face[fi] = []
        for triangle in topo.loop_triangles():
            fi = triangle[0].face.index
            if fi not in missing:
                continue
            groups = tuple(topo.loop_to_vgroup[topo.loop_pos[l]] for l in triangle)
            if len(set(groups)) == 3:
                by_face[fi].append(groups)
    return [triangle for fi in faces for triangle in by_face.get(fi, ())]


def _triangle_local2d(p3):
    """(3,2) local frame of one 3D triangle: p0 at origin, p0->p1 along +x."""
    a, b, c = p3
    e1 = b - a
    e2 = c - a
    length = e1.length
    if length < 1e-12:
        return None
    x2 = e2.dot(e1) / length
    y2sq = e2.length_squared - x2 * x2
    if y2sq <= 1e-24:
        return None
    return np.array([[0.0, 0.0], [length, 0.0], [x2, math.sqrt(y2sq)]])


def rectangular_boundary_layout(topo, faces, runs, held):
    """Positive harmonic embedding for a closed four-sided axis boundary.

    A displacement solve can invert the interior when the final side is
    flattened. Build a convex boundary and solve interior coordinates instead.
    Explicit user pins and holes are not changed by this fallback.
    """
    if held:
        return None
    boundary=selection_border_loops(topo,faces)
    kinds={p:k for k,positions in runs for p in positions}
    if any(kinds.get(p) not in (C_HORIZONTAL,C_VERTICAL) for p in boundary):
        return None
    if set(kinds)-set(boundary):
        return None
    outgoing={}
    for p in boundary:
        a,b=topo.loop_edge_vgroups(p)
        if a in outgoing:
            return None
        outgoing[a]=(b,p)
    if not outgoing:
        return None
    start=min(outgoing);g=start;cycle=[]
    while g in outgoing and len(cycle)<len(boundary):
        b,p=outgoing[g];cycle.append((g,b,p));g=b
        if g==start:break
    if g!=start or len(cycle)!=len(boundary):
        return None
    turns=[i for i,e in enumerate(cycle) if kinds[e[2]]!=kinds[cycle[i-1][2]]]
    if len(turns)!=4:
        return None
    cycle=cycle[turns[0]:]+cycle[:turns[0]]
    sides=[]
    for edge in cycle:
        k=kinds[edge[2]]
        if not sides or sides[-1][0]!=k:sides.append((k,[]))
        sides[-1][1].append(edge)
    horizontal=[i for i,(k,_) in enumerate(sides) if k==C_HORIZONTAL]
    vertical=[i for i,(k,_) in enumerate(sides) if k==C_VERTICAL]
    if len(horizontal)!=2 or len(vertical)!=2:
        return None
    triangles=triangles_for_faces(topo,faces)
    groups=sorted({g for tri in triangles for g in tri});index={g:i for i,g in enumerate(groups)}
    xyz=vgroup_positions3d(topo)
    lengths=[[max((xyz[b]-xyz[a]).length,1e-10) for a,b,p in edges] for k,edges in sides]
    aspect=sum(sum(lengths[i]) for i in horizontal)/max(sum(sum(lengths[i]) for i in vertical),1e-12)
    area=island_area(topo,faces)
    width=math.sqrt(max(area*aspect,1e-16));height=area/max(width,1e-12)
    original=np.array([tuple(topo.vgroup_uv(g)) for g in groups])
    center=original.mean(axis=0)
    constants={}
    for ids,axis,size in ((horizontal,1,height),(vertical,0,width)):
        ordered=sorted(ids,key=lambda i:sum(float(topo.vgroup_uv(a)[axis]) for a,b,p in sides[i][1])/len(sides[i][1]))
        constants[ordered[0]]=center[axis]-size*.5
        constants[ordered[1]]=center[axis]+size*.5
    corners=[]
    for i,(kind,edges) in enumerate(sides):
        value=np.empty(2);axis=1 if kind==C_HORIZONTAL else 0
        value[axis]=constants[i];value[1-axis]=constants[(i-1)%4]
        corners.append(value)
    target={}
    for i,(kind,edges) in enumerate(sides):
        total=sum(lengths[i]);distance=0.
        for (a,b,p),length in zip(edges,lengths[i]):
            target[a]=corners[i]+(corners[(i+1)%4]-corners[i])*(distance/total)
            distance+=length
        target[edges[-1][1]]=corners[(i+1)%4]
    from .circle import _harmonic
    try:
        result = _harmonic(topo, faces, {g: tuple(value) for g, value in target.items()})
    except ValueError:
        return None
    land_uv_groups(topo, result, faces)
    return {g:Vector(tuple(value)) for g,value in target.items()}


def _straight_chain_seed(topo, positions, kind, index, original, held, force=False):
    """Initialize a folded open run by arc length, preserving its vertex order.

    A coordinate projection can fold a curved run back over itself. This only
    repairs that starting point; the metric solver still optimizes its lengths.
    Branches, closed runs, pins, and already monotone runs keep the normal path.
    """
    adjacent = {}
    for p in positions:
        a, b = topo.loop_edge_vgroups(p)
        if a == b or a not in index or b not in index:
            return None
        adjacent.setdefault(a, set()).add(b)
        adjacent.setdefault(b, set()).add(a)
    ends = [g for g, links in adjacent.items() if len(links) == 1]
    if len(ends) != 2 or any(len(links) > 2 for links in adjacent.values()):
        return None
    if any(g in held for g in adjacent):
        return None
    ordered, previous, group = [], None, min(ends)
    while group is not None:
        ordered.append(group)
        following = adjacent[group] - {previous}
        previous, group = group, next(iter(following), None)
    if len(ordered) != len(adjacent):
        return None
    members = np.asarray([index[g] for g in ordered], dtype=np.int64)
    points = original[members]
    center = points.mean(axis=0)
    if kind == C_HORIZONTAL:
        direction = np.array((1.0, 0.0))
    elif kind == C_VERTICAL:
        direction = np.array((0.0, 1.0))
    else:
        relative = points - center
        _, basis = np.linalg.eigh(relative.T @ relative)
        direction = basis[:, -1]
    along = (points - center) @ direction
    sign = -1.0 if along[-1] < along[0] else 1.0
    if not force and np.all(sign * np.diff(along) > 1e-12):
        return None
    lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    if np.any(lengths <= 1e-12):
        return None
    distance = np.concatenate(([0.0], np.cumsum(lengths)))
    target = center + (sign * (distance - distance.mean()))[:, None] * direction
    return members, target


def deform_constraint_boundary(topo, faces, runs, held, transport=False):
    """Spread the boundary displacement through the existing island layout."""
    faces = set(faces)
    triangles = triangles_for_faces(topo, faces)
    groups = sorted({g for tri in triangles for g in tri})
    index = {g: i for i, g in enumerate(groups)}
    if not groups:
        return 0
    original = np.array([tuple(topo.vgroup_uv(g)) for g in groups], dtype=np.float64)
    target = original.copy()
    arrays = []
    seeds = []
    for kind, positions in runs:
        members = {g for p in positions for g in topo.loop_edge_vgroups(p) if g in index}
        arrays.append((kind, np.array([index[g] for g in sorted(members)], dtype=np.int64)))
        seed = _straight_chain_seed(topo, positions, kind, index, original, held,
                                    force=transport)
        if seed is not None:
            target[seed[0]] = seed[1]
            seeds.append(seed[0])
    project_runs(target, arrays)
    fixed = np.zeros(len(groups), dtype=bool)
    for _, members in arrays:
        fixed[members] = True
    for g, uv in held.items():
        if g in index:
            fixed[index[g]] = True
            target[index[g]] = tuple(uv)
    edges = set()
    for tri in triangles:
        for a, b in zip(tri, tri[1:]+tri[:1]):
            edges.add(tuple(sorted((index[a], index[b]))))
    edge = np.array(sorted(edges), dtype=np.int64)
    if not edge.size:
        return 0
    a, b = edge[:, 0], edge[:, 1]
    if transport and seeds:
        # Transport the local frame along a straightened run. A displacement
        # field alone leaves the free side of a curved strip pointing in its
        # old direction and can fold its end faces over the constrained side.
        import heapq
        rotation = np.zeros((len(groups), 2))
        rotation[:, 0] = 1.0
        for members in seeds:
            old_tangent = np.empty((len(members), 2))
            new_tangent = np.empty_like(old_tangent)
            for points, tangent in ((original[members], old_tangent),
                                    (target[members], new_tangent)):
                tangent[0], tangent[-1] = points[1]-points[0], points[-1]-points[-2]
                tangent[1:-1] = points[2:]-points[:-2]
            norm = np.linalg.norm(old_tangent, axis=1)*np.linalg.norm(new_tangent, axis=1)
            usable = norm > 1e-16
            ids = members[usable]
            u, v = old_tangent[usable], new_tangent[usable]
            rotation[ids, 0] = (u*v).sum(axis=1)/norm[usable]
            rotation[ids, 1] = (u[:, 0]*v[:, 1]-u[:, 1]*v[:, 0])/norm[usable]
        adjacent = [[] for _ in groups]
        for i, j in edge:
            length = float(np.linalg.norm(original[i]-original[j]))
            adjacent[i].append((int(j), length))
            adjacent[j].append((int(i), length))
        owners = np.full(len(groups), -1, dtype=np.int64)
        distances = np.full(len(groups), np.inf)
        queue = []
        for i in np.flatnonzero(fixed):
            owners[i], distances[i] = i, 0.0
            heapq.heappush(queue, (0.0, int(i)))
        while queue:
            distance, i = heapq.heappop(queue)
            if distance != distances[i]:
                continue
            for j, length in adjacent[i]:
                candidate = distance+length
                if candidate < distances[j]:
                    distances[j], owners[j] = candidate, owners[i]
                    heapq.heappush(queue, (candidate, j))
        result = original.copy()
        reachable = owners >= 0
        owner = owners[reachable]
        relative = original[reachable]-original[owner]
        c, s = rotation[owner].T
        result[reachable] = target[owner]+np.column_stack((
            c*relative[:, 0]-s*relative[:, 1], s*relative[:, 0]+c*relative[:, 1]))
        result[fixed] = target[fixed]
        land_uv_groups(topo, {g: result[i] for g, i in index.items()}, faces)
        return len(triangles)
    weight = 1.0 / np.maximum(np.linalg.norm(original[a]-original[b], axis=1), 1e-8)
    scatter = np.concatenate((a, b))
    degree = np.bincount(scatter, weights=np.tile(weight, 2), minlength=len(groups))
    displacement = np.zeros_like(original)
    displacement[fixed] = (target-original)[fixed]
    native = native_engine.deform(edge, weight, degree, fixed, displacement)
    if native is not None:
        displacement = native
    else:
        for _ in range(400):
            summed = np.column_stack([
                np.bincount(scatter,weights=np.concatenate((displacement[b,axis]*weight,
                                                           displacement[a,axis]*weight)),
                            minlength=len(groups)) for axis in (0,1)])
            new = summed / np.maximum(degree[:, None], 1e-12)
            new[fixed] = displacement[fixed]
            if float(np.abs(new-displacement).max()) < 1e-9:
                displacement = new
                break
            displacement = new
    result = original + displacement
    for g, i in index.items():
        for p in topo.vgroup_loops[g]:
            if topo.loops[p].face.index in faces:
                topo.loops[p][topo.uvl].uv = result[i]
    return len(triangles)


def feasible_constraint_boundary(topo, faces, runs, held, angle_length=1.0,
                                 _geometry=None):
    """Avoid introducing a fold when flattening a healthy curved boundary."""
    triangles=triangles_for_faces(topo,faces)
    groups=sorted({g for tri in triangles for g in tri})
    index={g:i for i,g in enumerate(groups)}
    tri=np.asarray([[index[g] for g in row] for row in triangles],dtype=np.int64)
    if not len(tri):return 0
    original=np.asarray([tuple(topo.vgroup_uv(g)) for g in groups])
    def cross(value):
        q=value[tri];a,b=q[:,1]-q[:,0],q[:,2]-q[:,0]
        return a[:,0]*b[:,1]-a[:,1]*b[:,0]
    before=cross(original)
    healthy=bool(np.all(before>2e-14) or np.all(before < -2e-14))
    # An exact axis run containing all three corners of a triangle has no
    # nondegenerate endpoint. Continuation cannot repair that geometry; keep
    # the existing conflict/protection path instead of spending its trial budget.
    for kind in (C_HORIZONTAL,C_VERTICAL):
        labels=np.full(len(groups),-1,dtype=np.int64)
        for run_id,(current,positions) in enumerate(runs):
            if current!=kind:continue
            for p in positions:
                for g in topo.loop_edge_vgroups(p):
                    if g in index:labels[index[g]]=run_id
        corners=labels[tri]
        if np.any((corners[:,0]>=0)&(corners[:,0]==corners[:,1])&
                  (corners[:,0]==corners[:,2])):
            healthy=False
            break
    result=deform_constraint_boundary(topo,faces,runs,held)
    if healthy:
        current=np.asarray([tuple(topo.vgroup_uv(g)) for g in groups])
        if np.any(before*cross(current)<=0):
            land_uv_groups(topo,{g:original[i] for g,i in index.items()},faces)
            continued=continue_constraint_boundary(topo,faces,runs,held,angle_length,
                                                   max_trials=24,_geometry=_geometry)
            if continued:return continued
            # Infeasible targets retain the existing recovery path and the
            # user's protection settings; an unsuccessful seed is not a pass.
            result=deform_constraint_boundary(topo,faces,runs,held)
    return result


def continue_constraint_boundary(topo, faces, runs, held, angle_length=1.0,
                                 max_trials=64,_geometry=None):
    """Reach a hard boundary gradually from a feasible parameterization."""
    from . import metric
    import sys
    module = sys.modules[__name__]
    triangles = triangles_for_faces(topo, faces)
    groups = sorted({g for tri in triangles for g in tri})
    if not groups or not runs:
        return 0
    index = {g: i for i, g in enumerate(groups)}
    tri = np.asarray([[index[g] for g in row] for row in triangles])
    original = np.asarray([tuple(topo.vgroup_uv(g)) for g in groups], dtype=float)
    target = original.copy()
    arrays = []
    for kind, positions in runs:
        members = {g for p in positions for g in topo.loop_edge_vgroups(p) if g in index}
        arrays.append((kind, np.asarray([index[g] for g in sorted(members)])))
        seed = _straight_chain_seed(topo, positions, kind, index, original, held, force=True)
        if seed is not None:
            target[seed[0]] = seed[1]
    project_runs(target, arrays)
    moving = {int(i) for _, members in arrays for i in members}

    def cross(value):
        q = value[tri]
        a, b = q[:, 1]-q[:, 0], q[:, 2]-q[:, 0]
        return a[:, 0]*b[:, 1]-a[:, 1]*b[:, 0]

    old_cross = cross(original)
    oriented = np.abs(old_cross) > 2e-14
    current = original.copy()
    geometry = (_geometry if _geometry is not None else
                metric._prepare_geometry(module, topo, set(faces)))
    # A UV group can touch an outside face at a single shared vertex. Its
    # representative then reads the outside corner even after an inside-only
    # write. Preserve that BMesh path; ordinary complete charts use numeric
    # trials and publish only the final accepted coordinates.
    numeric_trials = (geometry is not None and
                      all(topo.loop_faces[p] in faces for p in geometry['representatives']))
    numeric_written = False
    progress, step = 0.0, .25
    for _ in range(max_trials):
        next_progress = min(1.0, progress+step)
        boundary = {groups[i]: Vector(original[i]+next_progress*(target[i]-original[i]))
                    for i in moving}
        boundary.update(held)
        trial = metric.solve(module, topo, faces, boundary, (), 32,
                             angle_length=angle_length, auto=False, use_density_map=False,
                             _geometry=geometry,
                             _coordinates=current if numeric_trials else None)
        candidate = (trial[2] if numeric_trials else
                     np.asarray([tuple(topo.vgroup_uv(g)) for g in groups]))
        if numeric_trials:
            numeric_written |= trial[0] > 0
        new_cross = cross(candidate)
        reached = all(np.max(np.abs(candidate[index[g]]-np.asarray(tuple(uv)))) <= 2e-7
                      for g, uv in boundary.items() if g in index)
        feasible = (reached and np.all(np.isfinite(candidate)) and
                    np.all(old_cross[oriented]*new_cross[oriented] > 0))
        if not feasible:
            numeric_written |= numeric_trials
            if not numeric_trials:
                land_uv_groups(topo, {g: current[i] for g, i in index.items()}, faces)
            step *= .5
            if step < 1e-5:
                break
            continue
        current, progress = candidate, next_progress
        if progress == 1.0:
            if numeric_trials and numeric_written:
                land_uv_groups(topo, {g: current[i] for g, i in index.items()}, faces)
            return len(triangles)
        step = min(.25, step*1.5)
    land_uv_groups(topo, {g: original[i] for g, i in index.items()}, faces)
    return 0


def stacked_uv_groups(topo, faces, *, coordinates=None):
    """Share UV variables only for complete, coincident input face boundaries.

    This preserves intentional stacks while each triangle retains its own 3D
    geometry. It does not weld the mesh or modify constraints or selection.
    """
    if len(faces) < 2:
        return {}
    if coordinates is not None:
        # An immutable, fresh input snapshot lets recovery reject unique face
        # bounds in bulk. Exact polygon matching below remains authoritative.
        uv = np.asarray(coordinates, dtype=float)
        if uv.shape != (len(topo.loops), 2):
            raise ValueError('Invalid UV stack input dimensions')
        sizes = np.bincount(topo.loop_faces, minlength=len(topo.bm.faces))
        starts = np.r_[0, np.cumsum(sizes)[:-1]]
        selected = np.asarray(sorted(faces), dtype=np.int64)
        bounds = np.column_stack((sizes, np.minimum.reduceat(uv, starts),
                                  np.maximum.reduceat(uv, starts)))[selected]
        finite = np.isfinite(bounds).all(axis=1)
        selected, bounds = selected[finite], bounds[finite]
        if len(selected) < 2:
            return {}
        _, inverse, counts = np.unique(bounds, axis=0, return_inverse=True, return_counts=True)
        faces = selected[counts[inverse] > 1].tolist()
        if not faces:
            return {}
    parent, stacked = {}, {}

    def find(group):
        parent.setdefault(group, group)
        while parent[group] != group:
            parent[group] = parent[parent[group]]
            group = parent[group]
        return group

    for fi in sorted(faces):
        if coordinates is None:
            face = topo.bm.faces[fi]
            points = tuple(tuple(loop[topo.uvl].uv) for loop in face.loops)
        else:
            positions = range(int(starts[fi]), int(starts[fi]+sizes[fi]))
            points = tuple(tuple(float(v) for v in coordinates[p]) for p in positions)
        if (not all(math.isfinite(value) for point in points for value in point)
                or len(set(points)) != len(points)):
            continue
        if abs(sum(a[0]*b[1]-b[0]*a[1]
                   for a,b in zip(points,points[1:]+points[:1]))) <= 2e-14:
            continue
        smallest = min(points)
        key = min(sequence[i:]+sequence[:i]
                  for sequence in (points, tuple(reversed(points)))
                  for i, point in enumerate(sequence) if point == smallest)
        groups = ({point: topo.loop_to_vgroup[topo.loop_pos[loop]]
                   for point, loop in zip(points, face.loops)} if coordinates is None else
                  {point: topo.loop_to_vgroup[p] for point, p in zip(points, positions)})
        previous = stacked.setdefault(key, groups)
        for point, group in groups.items():
            a, b = find(previous[point]), find(group)
            if a != b:
                parent[max(a,b)] = min(a,b)
    return {group: find(group) for group in parent if find(group) != group}


def fit_constraint_boundary(topo, faces, runs, held, angle_length=1.0,
                            shared_uv_groups=None, iterations=200,
                            use_density_map=True, prevent_flips=False,
                            prevent_overlap=False, overlap_space=0.):
    """Increase an axis penalty without projecting through inverted triangles."""
    from . import metric, safety
    import sys
    module = sys.modules[__name__]
    saved = _loop_uv_snapshot(topo, faces)
    shared = (stacked_uv_groups(topo, faces) if shared_uv_groups is None else shared_uv_groups)
    probe = safety.AcceptanceProbe(module, topo, faces)
    for strength in (1., 10., 100., 1e3, 1e4, 1e5, 1e6, 1e7, 1e8):
        n, _ = metric.solve(module, topo, faces, held, runs, max(1, int(iterations)),
                           angle_length=angle_length, auto=False,
                           constraint_strength=strength, shared_uv_groups=shared,
                           use_density_map=use_density_map, prevent_flips=prevent_flips,
                           prevent_overlap=prevent_overlap, overlap_space=overlap_space)
        health = probe.measure(topo)
        if health['finite'] and health['zero_edges'] == 0 and health['max_deviation'] <= .02:
            return n
    for pos, uv in saved.items():
        topo.loops[pos][topo.uvl].uv = uv
    return 0


def relax_uv(topo, faces, fixed=None, runs=None, iterations=150, step=0.7,
             tol=1e-7, angle_distance_mix=0.0):
    """ARAP-style relaxation of the UVs of `faces`, in place.

    Each triangle gets the similarity transform (rotation + uniform scale) that
    best maps its 3D shape onto its *current* UVs; every vertex then moves to
    the average of what its incident triangles want it to be. Because the
    transform is fitted to the current layout, an already-good layout barely
    moves -- the island keeps its position, its size and its texel density.

    `fixed` maps vgroup -> (u, v): those vertices are pinned by the user and
    are held exactly.

    `runs` is `[(kind, [loop_position, ...]), ...]` -- the constrained edges.
    They are **not** position-pinned. After every iteration each run is re-laid
    onto its manifold (see `_project_run`), from its *current* centroid, so a
    run is free to slide while it stays exactly horizontal / vertical /
    straight. Two constraints on one island therefore move each other, which is
    what RizomUV does and what position-pinning cannot reproduce.

    `angle_distance_mix` adds a **length** term next to the similarity target.
    RizomUV only says "0 = Optimize Angles, 1 = Optimize Lengths" and never
    says which step the slider acts on, so this is the plug-in's own reading
    and not a recovered fact:

      * the similarity target above is the *angle* half -- each triangle keeps
        its own angles, whatever size it ends up at,
      * the extra term is the *length* half -- every edge is pulled towards the
        length it has in 3D, measured at the island's current texel density, so
        the UV/3D length ratio stops drifting from edge to edge.

    They are complementary rather than exclusive (angles alone leave the length
    field free, lengths alone say nothing about shape), so the weight is an
    addition on top of the classic solve. 0.0 is therefore exactly the
    pre-existing behaviour.

    Nothing is re-unwrapped and nothing is rescaled, so this can never grow the
    island, shove it across the tile, or drop it on top of another island.

    Returns (triangles processed, triangles left flipped).
    """
    fixed = fixed or {}
    triangles = triangles_for_faces(topo, faces)
    if not triangles:
        return 0, 0

    faces = set(faces)
    groups = sorted(topo.vgroup_loops)
    index = {g: i for i, g in enumerate(groups)}
    n = len(groups)
    p3d = vgroup_positions3d(topo)

    tri_list, local = [], []
    for a, b, c in triangles:
        P = _triangle_local2d((p3d[a], p3d[b], p3d[c]))
        if P is None:
            continue
        tri_list.append((index[a], index[b], index[c]))
        local.append(P)
    if not tri_list:
        return 0, 0

    tri = np.asarray(tri_list, dtype=np.int64)
    P = np.asarray(local, dtype=np.float64)

    x = np.empty((n, 2), dtype=np.float64)
    for g, i in index.items():
        uv = topo.vgroup_uv(g)
        x[i, 0] = uv.x
        x[i, 1] = uv.y

    # -- the length term (see `angle_distance_mix`) -------------------------
    # One entry per undirected edge of this face set, with the length it has in
    # 3D. Built once: the 3D lengths cannot change during the solve.
    # Arrays even when the term is off, so the solve below can test `.size`
    # without caring whether the term is enabled.
    edge_a = np.zeros(0, dtype=np.int64)
    edge_b = np.zeros(0, dtype=np.int64)
    edge_l3 = np.zeros(0, dtype=np.float64)
    density = 0.0
    if angle_distance_mix > 0.0:
        edge_a, edge_b, edge_l3 = [], [], []
        seen = set()
        for i, j, k in tri_list:
            for u, v in ((i, j), (j, k), (k, i)):
                key = (u, v) if u < v else (v, u)
                if key in seen:
                    continue
                seen.add(key)
                l3 = (p3d[groups[u]] - p3d[groups[v]]).length
                if l3 > 1e-12:
                    edge_a.append(u)
                    edge_b.append(v)
                    edge_l3.append(l3)
        edge_a = np.asarray(edge_a, dtype=np.int64)
        edge_b = np.asarray(edge_b, dtype=np.int64)
        edge_l3 = np.asarray(edge_l3, dtype=np.float64)
        # How long one UV unit is compared with one 3D unit. Taken from the
        # areas rather than from a fitted scale, so that a layout which is
        # already distorted still yields one honest number.
        area_p = float(np.abs(
            0.5 * ((P[:, 1, 0] - P[:, 0, 0]) * (P[:, 2, 1] - P[:, 0, 1])
                   - (P[:, 2, 0] - P[:, 0, 0]) * (P[:, 1, 1] - P[:, 0, 1]))
        ).sum())
        q = x[tri]
        area_q = float(np.abs(
            0.5 * ((q[:, 1, 0] - q[:, 0, 0]) * (q[:, 2, 1] - q[:, 0, 1])
                   - (q[:, 2, 0] - q[:, 0, 0]) * (q[:, 1, 1] - q[:, 0, 1]))
        ).sum())
        if area_p > 1e-18 and area_q > 1e-18:
            density = math.sqrt(area_q / area_p)

    held = np.zeros(n, dtype=bool)
    for g in fixed:
        i = index.get(g)
        if i is not None:
            held[i] = True
    # NOTE: the held vertices are *not* moved yet -- they start where they are
    # and are ramped towards their targets inside the loop.

    run_arrays = []
    for kind, positions in (runs or ()):
        members = set()
        for position in positions:
            members.update(topo.loop_edge_vgroups(position))
        idx = sorted(index[g] for g in members if g in index)
        if len(idx) >= 2:
            run_arrays.append((kind, np.asarray(idx, dtype=np.int64)))

    raw_count = np.bincount(tri.reshape(-1), minlength=n)
    # Every vertex that belongs to no processed triangle (i.e. every vertex of
    # every other island) has no ARAP target at all, so it has to be held too.
    # Without this they would be pulled towards the origin.
    island = raw_count > 0
    held |= ~island
    # the user's pins, restricted to this island -- kept apart from the plain
    # "held" mask because only these deserve to anchor the density correction
    pinned = held & island
    count = np.maximum(raw_count, 1).astype(np.float64)

    def signed_areas(uv):
        a = uv[tri[:, 0]]
        b = uv[tri[:, 1]]
        c = uv[tri[:, 2]]
        return 0.5 * ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
                      - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))

    flips_before = int((signed_areas(x) < 0).sum())
    area_before = float(np.abs(signed_areas(x)).sum())
    lam = float(step)

    # Hold the pinned vertices at the *original* positions and slide them
    # towards their targets over the first two thirds of the run. Snapping them
    # straight to the target can fold the faces next to them -- especially when
    # they have to travel a long way -- whereas a ramp gives the relaxation time
    # to redistribute the distortion as it goes.
    start = x.copy()
    goal = x.copy()
    for g, want in fixed.items():
        i = index.get(g)
        if i is not None:
            goal[i, 0] = float(want[0])
            goal[i, 1] = float(want[1])
    ramp = max(1.0, iterations * 2.0 / 3.0)
    # How long the constraint re-lay takes to reach full strength. Full strength
    # by the end of the first third, so two thirds of the budget is spent on a
    # fully imposed constraint -- the ramp only buys the surrounding faces time
    # to follow, it must not leave the constraint partially imposed.
    constraint_ramp = max(1.0, iterations / 3.0)

    for iteration in range(iterations):
        if held.any():
            alpha = min(1.0, (iteration + 1) / ramp)
            x[held] = start[held] + alpha * (goal[held] - start[held])

        Q = x[tri]
        Pm = P.mean(axis=1, keepdims=True)
        Qm = Q.mean(axis=1, keepdims=True)
        Pc = P - Pm
        Qc = Q - Qm

        a = (Pc[..., 0] * Qc[..., 0] + Pc[..., 1] * Qc[..., 1]).sum(axis=1)
        b = (Pc[..., 0] * Qc[..., 1] - Pc[..., 1] * Qc[..., 0]).sum(axis=1)
        norm = (Pc ** 2).sum(axis=(1, 2))
        norm[norm < 1e-18] = 1e-18
        c_ = a / norm
        d_ = b / norm

        des = np.empty_like(Q)
        des[..., 0] = (c_[:, None] * Pc[..., 0] - d_[:, None] * Pc[..., 1]
                       + Qm[..., 0])
        des[..., 1] = (d_[:, None] * Pc[..., 0] + c_[:, None] * Pc[..., 1]
                       + Qm[..., 1])

        acc = np.zeros((n, 2), dtype=np.float64)
        np.add.at(acc, tri.reshape(-1), des.reshape(-1, 2))
        target = acc / count[:, None]

        if edge_a.size:
            # Length term: nudge both ends of every edge towards the length it
            # has in 3D at the island's current density. Added to the ARAP
            # target, so a weight of 0 leaves the classic solve untouched.
            delta = x[edge_b] - x[edge_a]
            length = np.hypot(delta[:, 0], delta[:, 1])
            safe = length > 1e-12
            factor = np.zeros_like(length)
            factor[safe] = ((density * edge_l3[safe] - length[safe])
                            / (2.0 * length[safe]))
            shift = delta * factor[:, None]
            acc2 = np.zeros((n, 2), dtype=np.float64)
            np.add.at(acc2, edge_a, -shift)
            np.add.at(acc2, edge_b, shift)
            degree = np.bincount(np.concatenate((edge_a, edge_b)),
                                 minlength=n).astype(np.float64)
            degree[degree < 1.0] = 1.0
            target += angle_distance_mix * (acc2 / degree[:, None])

        candidate = x + lam * (target - x)
        candidate[held] = x[held]
        # Re-impose the constraints on the candidate, so the flip guard below
        # judges the state we would actually keep. A constraint deliberately
        # wins over a user pin sitting on the same vertex. One pass is enough
        # here: the next iteration redistributes whatever the corners lose.
        #
        # ...and it is applied *gradually*, over the first third of the budget.
        # Snapping the run onto its manifold is a hard move: when the run's own
        # direction is perpendicular to the demanded axis the re-lay turns it a
        # quarter turn in a single step, and the ring of faces around it is
        # dragged through itself before the ARAP term gets a chance to react.
        # The guard below cannot save that -- by the time the candidate exists
        # the fold is already in it. Ramping keeps the demanded direction only
        # slightly ahead of where the run actually is, so the surrounding faces
        # follow instead of being folded.
        #
        # Measured on the case that made this necessary, a 6x2 grid whose UV
        # makes a 3D row into a *vertical* UV line asked to be horizontal
        # (`_tools/_probe_disjoint.py`, case `B_one_row_h` / `D_two_rows_h`):
        # 8 and 16 flipped triangles with 62 and 82 overlapping triangle pairs
        # before, 0 and 0 after. RizomUV on the same input: 0 and 0, but with
        # the largest displacement of any of its cases because it too turns the
        # *island* rather than the run (`瀹為獙 G`).
        project_runs(candidate, run_arrays, passes=1,
                     blend=min(1.0, (iteration + 1) / constraint_ramp))

        if int((signed_areas(candidate) < 0).sum()) > flips_before:
            # this step would fold triangles over: back off and retry
            lam *= 0.5
            if lam < 1e-3:
                break
            continue

        delta = float(np.abs(candidate - x).max())
        x = candidate
        if delta < tol and iteration >= ramp:
            break

    # Land the constraints exactly, then the pins that are not on a run.
    project_runs(x, run_arrays)
    on_run = np.zeros(n, dtype=bool)
    for _kind, idx in run_arrays:
        on_run[idx] = True
    land = held & ~on_run
    if held.any():
        x[land] = goal[land]
    _restore_density(x, tri, island, area_before, pinned & ~on_run)
    if held.any():
        x[land] = goal[land]      # a pin is a promise: land it to the last bit

    flips_after = int((signed_areas(x) < 0).sum())

    # Write back **only the corners of the faces that were solved**. Writing
    # each UV vertex group wholesale would also move corners belonging to other
    # islands: two islands can share one UV vertex where they pinch together at
    # a single mesh vertex, and those corners are in the same group. Measured
    # consequence of getting this wrong: solving island B shrank island A by
    # 17% (0.03260413 -> 0.02709064), and nothing in island A's own numbers
    # showed it.
    mine = set()
    for face in topo.bm.faces:
        if face.index in faces:
            for loop in face.loops:
                mine.add(topo.loop_pos[loop])
    for g, i in index.items():
        u, v = float(x[i, 0]), float(x[i, 1])
        for position in topo.vgroup_loops[g]:
            if position in mine:
                topo.loops[position][topo.uvl].uv = (u, v)
    return len(tri), flips_after


def _restore_density(out, tri, island, area_before, pins=None):
    """Scale the island about a fixed point so its UV area is unchanged.

    ARAP is scale preserving in principle, but hard constraints plus the
    step-limiting can let the area creep -- and hard constraints are exactly
    what makes the creep one-directional (the island shrinks a little on every
    pass). Growth is the "the island got much bigger" complaint and shrinkage is
    the same defect with the other sign, so the area is corrected explicitly.

    The whole island is scaled, constrained vertices included. A uniform scale
    is a similarity, so it maps horizontal lines to horizontal lines, vertical
    to vertical and keeps points collinear: every constraint survives. Scaling
    only the free vertices -- the obvious version -- cannot work, because the
    triangles that touch a fixed vertex do not scale with it and the factor
    never lands.

    `pins` marks the user's pinned vertices; when there are any, the scale is
    taken about their centre so that a single pin does not move at all.
    """
    if not island.any():
        return

    a = out[tri[:, 0]]
    b = out[tri[:, 1]]
    c = out[tri[:, 2]]
    have = float(np.abs(0.5 * ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
                               - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
                          ).sum())
    if area_before <= 0.0 or have <= 0.0:
        return
    factor = math.sqrt(area_before / have)
    if not (1e-3 < factor < 1e3) or 0.999 <= factor <= 1.001:
        return

    index = np.flatnonzero(island)
    if pins is not None and pins.any():
        centre = out[np.flatnonzero(pins)].mean(axis=0)
    else:
        centre = out[index].mean(axis=0)
    out[index] = centre + (out[index] - centre) * factor


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------


def islands_touching_loops(topo, positions):
    """Face indices of every UV island touched by the given UV edges."""
    faces = set()
    for position in positions:
        faces.add(topo.loops[position].face.index)
    roots = {topo.face_island[fi] for fi in faces}
    out = set()
    for root in roots:
        out.update(topo.island_faces[root])
    return out


def islands_touching_faces(topo, face_indices):
    roots = {topo.face_island[fi] for fi in face_indices}
    out = set()
    for root in roots:
        out.update(topo.island_faces[root])
    return out


# ---------------------------------------------------------------------------
# global rebuild: re-solve the island with the constraints solved inside it
# ---------------------------------------------------------------------------


def uv_layer(bm):
    """The active UV layer of an edit-mode BMesh (None when there is none)."""
    return bm.loops.layers.uv.active


def _uv_loops(topo, faces):
    """[(loop_position, BMLoop)] for every loop of the given faces."""
    out = []
    for fi in sorted(faces):
        for loop in topo.bm.faces[fi].loops:
            out.append((topo.loop_pos[loop], loop))
    return out


def _loop_uv_snapshot(topo, faces):
    """{loop_position: (u, v)} -- the island's pose reference.

    Keyed per *loop*, not per UV-vertex: unwrapping never changes the mesh
    topology, so a loop is a stable handle for one corner even when UVs move
    and corners that used to coincide drift apart.
    """
    uvl = topo.uvl
    out = {}
    for position, loop in _uv_loops(topo, faces):
        uv = loop[uvl].uv
        out[position] = (float(uv[0]), float(uv[1]))
    return out


def island_area(topo, faces, *, coordinates=None):
    """Summed |UV area| of the given faces."""
    uvl = topo.uvl
    total = 0.0
    for fi in sorted(faces):
        face = topo.bm.faces[fi]
        pts = ([loop[uvl].uv for loop in face.loops] if coordinates is None else
               [coordinates[topo.loop_pos[loop]] for loop in face.loops])
        acc = 0.0
        for k in range(len(pts)):
            p, q = pts[k], pts[(k + 1) % len(pts)]
            acc += p[0] * q[1] - q[0] * p[1]
        total += abs(acc) * 0.5
    return total


# ---------------------------------------------------------------------------
# island decomposition, border detection and readable quality numbers
# ---------------------------------------------------------------------------


def polygon_area3d(face):
    """3D area of one face. Newell's formula, so non-planar quads still work."""
    acc = Vector((0.0, 0.0, 0.0))
    verts = [loop.vert.co for loop in face.loops]
    count = len(verts)
    for i in range(count):
        acc += verts[i].cross(verts[(i + 1) % count])
    return 0.5 * acc.length


def mesh_area3d(topo, faces):
    """Summed 3D area of the given faces."""
    return sum(polygon_area3d(topo.bm.faces[fi]) for fi in sorted(faces))


def face_islands(topo, faces):
    """{island root: [face index, ...]}, restricted to `faces`.

    Optimize works on one island at a time. Batching several islands into a
    single pose fit is wrong and visibly so: the unwrapper re-normalises every
    island into the same area, so one global similarity drags the islands
    around relative to each other instead of putting each one back.
    """
    out = {}
    # Sorted, so the order in which islands are solved -- and therefore the
    # order of anything the caller reports about them -- does not wobble
    # between runs. `faces` is usually a set.
    for index in sorted(faces):
        out.setdefault(topo.face_island[index], []).append(index)
    return out


def selection_border_loops(topo, faces):
    """UV boundaries, including cuts inside one otherwise connected island."""
    wanted=set(faces)
    copies={}
    for fi in sorted(wanted):
        face = topo.bm.faces[fi]
        for loop in face.loops:
            p=topo.loop_pos[loop]
            a,b=topo.loop_edge_vgroups(p)
            key=(loop.edge.index,min(a,b),max(a,b))
            copies.setdefault(key,[]).append(p)
    return [positions[0] for positions in copies.values() if len(positions)==1]


def loop_direction_deg(topo, position):
    """Direction of one UV edge in degrees, folded into [0, 180). None if degenerate."""
    a, b = topo.loop_edge_vgroups(position)
    pa = topo.vgroup_uv(a)
    pb = topo.vgroup_uv(b)
    du = pb.x - pa.x
    dv = pb.y - pa.y
    if abs(du) < 1e-14 and abs(dv) < 1e-14:
        return None
    return math.degrees(math.atan2(dv, du)) % 180.0


def boundary_align_runs(topo, faces, limit_deg):
    """Border edges within `limit_deg` of an axis, grouped into runs.

    This is RizomUV's "Align borders". Its manual says those edges "come out
    exactly horizontal or vertical, **as if it carried an edge constraint
    during this optimization only**" -- so the implementation is exactly that:
    temporary runs, merged into the same optimization as the user's own
    constraints, and never written to the constraint attribute.
    """
    kinds = {}
    for position in selection_border_loops(topo, faces):
        angle = loop_direction_deg(topo, position)
        if angle is None:
            continue
        dev_h = min(angle, 180.0 - angle)
        dev_v = abs(angle - 90.0)
        if dev_h <= limit_deg:
            kinds[position] = C_HORIZONTAL
        elif dev_v <= limit_deg:
            kinds[position] = C_VERTICAL
    if not kinds:
        return []
    return group_chains(topo, list(kinds), kinds)


def _run_direction_deg(topo, positions):
    """Least-squares direction of a run, plus its extent (used as the weight)."""
    points = []
    for position in positions:
        for group in topo.loop_edge_vgroups(position):
            uv = topo.vgroup_uv(group)
            points.append((uv.x, uv.y))
    if len(points) < 2:
        return None, 0.0
    count = float(len(points))
    mx = sum(p[0] for p in points) / count
    my = sum(p[1] for p in points) / count
    cxx = sum((p[0] - mx) ** 2 for p in points)
    cyy = sum((p[1] - my) ** 2 for p in points)
    cxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    if cxx + cyy < 1e-20:
        return None, 0.0
    theta = 0.5 * math.atan2(2.0 * cxy, cxx - cyy)
    return math.degrees(theta) % 180.0, math.sqrt(cxx + cyy)


def estimate_axis_rotation(topo, runs):
    """Rotation (degrees) that would land every run on its target axis.

    Undoing it right after the unwrap is what keeps the solver off a bad local
    minimum. Without it a constrained run starts at whatever angle the
    unwrapper left it at, gets dragged onto its axis, folds the faces around
    it, and the relaxation then has to repair damage it never needed to cause.

    Directions only mean something modulo 180 degrees, so the estimate is taken
    with the doubled-angle trick.
    """
    sine = 0.0
    cosine = 0.0
    for kind, positions in runs:
        angle, weight = _run_direction_deg(topo, positions)
        if angle is None or weight <= 0.0:
            continue
        want = 0.0 if kind == C_HORIZONTAL else 90.0
        delta = (want - angle + 90.0) % 180.0 - 90.0
        sine += weight * math.sin(math.radians(2.0 * delta))
        cosine += weight * math.cos(math.radians(2.0 * delta))
    if abs(sine) < 1e-12 and abs(cosine) < 1e-12:
        return 0.0
    return math.degrees(0.5 * math.atan2(sine, cosine))


def dominant_border_angle(topo, faces):
    """The border's dominant direction in degrees mod 90, or None.

    Length-weighted orientation tensor of the island's own border edges --
    the direction Align borders turns each island onto before classifying its
    edges ("each island rectifies along its own natural axes, whatever its
    current orientation", in RizomUV's words).
    """
    sine = 0.0
    cosine = 0.0
    for position in selection_border_loops(topo, faces):
        angle = loop_direction_deg(topo, position)
        if angle is None:
            continue
        a, b = topo.loop_edge_vgroups(position)
        pa = topo.vgroup_uv(a)
        pb = topo.vgroup_uv(b)
        weight = math.hypot(pb.x - pa.x, pb.y - pa.y)
        if weight <= 1e-14:
            continue
        sine += weight * math.sin(math.radians(4.0 * angle))
        cosine += weight * math.cos(math.radians(4.0 * angle))
    if abs(sine) < 1e-12 and abs(cosine) < 1e-12:
        return None
    return math.degrees(0.25 * math.atan2(sine, cosine)) % 90.0


def rotate_faces_uv(topo, faces, degrees, center=None):
    """Rotate about the island centre or its single fixed UV anchor."""
    if abs(degrees) < 1e-9:
        return 0.0
    rows = _uv_loops(topo, faces)
    if not rows:
        return 0.0
    points = [(float(l[topo.uvl].uv[0]), float(l[topo.uvl].uv[1]))
              for _p, l in rows]
    count = float(len(points))
    cx = sum(p[0] for p in points) / count
    cy = sum(p[1] for p in points) / count
    if center is not None:
        cx,cy=map(float,center)
    theta = math.radians(degrees)
    ct, st = math.cos(theta), math.sin(theta)
    for _position, loop in rows:
        uv = loop[topo.uvl].uv
        dx = float(uv[0]) - cx
        dy = float(uv[1]) - cy
        loop[topo.uvl].uv = (cx + ct * dx - st * dy, cy + st * dx + ct * dy)
    return degrees


def blend_towards(topo, snapshot, faces, amount):
    """Pull a face set's UVs back towards `snapshot` by `amount` (0 = untouched).

    `snapshot` is a `_loop_uv_snapshot`. Keyed per loop, so the blend is exact
    wherever it is applied and simply skipped where the snapshot has no entry.
    """
    amount = max(0.0, min(1.0, float(amount)))
    uvl = topo.uvl
    for position, loop in _uv_loops(topo, faces):
        was = snapshot.get(position)
        if was is None:
            continue
        uv = loop[uvl].uv
        loop[uvl].uv = (was[0] + amount * (float(uv[0]) - was[0]),
                        was[1] + amount * (float(uv[1]) - was[1]))


def scale_faces_to_area(topo, faces, target, *, coordinates=None):
    """Uniformly scale a face set about its own centre to reach `target` area.

    Used after `blend_towards`: blending two equal-area layouts is not a
    similarity, so the blend shrinks the island a little, and "Optimize never
    grows or shrinks the island" has to stay true at every strength.
    """
    if target <= 0.0:
        return 1.0
    have = island_area(topo, faces, coordinates=coordinates)
    if have <= 0.0:
        return 1.0
    factor = math.sqrt(target / have)
    if not (1e-3 < factor < 1e3) or abs(factor - 1.0) < 1e-6:
        return 1.0
    rows = _uv_loops(topo, faces)
    if not rows:
        return 1.0
    uvl = topo.uvl
    count = float(len(rows))
    points = ([tuple(l[uvl].uv) for _p, l in rows] if coordinates is None else
              [coordinates[p] for p, _l in rows])
    cu = sum(float(uv[0]) for uv in points) / count
    cv = sum(float(uv[1]) for uv in points) / count
    for (position, loop), uv in zip(rows, points):
        value = (cu + (float(uv[0]) - cu) * factor,
                 cv + (float(uv[1]) - cv) * factor)
        if coordinates is None:
            loop[uvl].uv = value
        else:
            coordinates[position] = np.asarray(value, dtype=np.float32)
    return factor


def _median(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _corner_angle2(points, k, lengths=None):
    count = len(points)
    p = points[k]
    a = points[(k - 1) % count]
    b = points[(k + 1) % count]
    v1 = (a[0] - p[0], a[1] - p[1])
    v2 = (b[0] - p[0], b[1] - p[1])
    n1 = math.hypot(v1[0], v1[1]) if lengths is None else lengths[(k-1) % count]
    n2 = math.hypot(v2[0], v2[1]) if lengths is None else lengths[k]
    if n1 < 1e-14 or n2 < 1e-14:
        return None
    c = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def _corner_angle3(points, k):
    count = len(points)
    p = points[k]
    v1 = points[(k - 1) % count] - p
    v2 = points[(k + 1) % count] - p
    if v1.length < 1e-14 or v2.length < 1e-14:
        return None
    c = v1.dot(v2) / (v1.length * v2.length)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def quality_metrics(topo, faces, *, coordinates=None):
    """Readable quality numbers for a face set.

    Deliberately *not* RizomUV's own metrics. `AQ`'s exact definition could not
    be recovered from the outside (see the research report), so reporting an
    approximation of it under its own name would be worse than useless. What is
    reported instead is transparent:

      density        sqrt(UV area / 3D area) -- the square of RizomUV's S.
                     1.0 means the island is at the model's own scale.
      worst_angle    worst per-corner angle deviation, in degrees. This is the
                     transparent stand-in for AQ; 0 means locally conformal.
      edge_spread    worst per-edge UV/3D length ratio, relative to the median.
                     Angle-based metrics are blind to an anisotropic stretch
                     that leaves angles alone, so this is reported alongside.
      flips          faces whose UV winding is inverted.
    """
    uvl = topo.uvl
    area_uv = 0.0
    area_3d = 0.0
    flips = 0
    worst_angle = 0.0
    ratios = []
    geometry = getattr(topo, '_quality_geometry', None)
    if geometry is None:
        # Numeric 3D terms only; this cache has the same operation lifetime
        # as the topology and is discarded after native unwrap/recovery.
        geometry = topo._quality_geometry = {}
    for fi in sorted(faces):
        face = topo.bm.faces[fi]
        # A caller may supply a known unchanged input snapshot. Every ordinary
        # post-write call reads live UV slots; no current-UV cache survives it.
        pts2 = ([tuple(l[uvl].uv) for l in face.loops] if coordinates is None else
                [coordinates[topo.loop_pos[l]] for l in face.loops])
        count = len(pts2)
        terms = geometry.get(fi)
        if terms is None:
            pts3 = [l.vert.co for l in face.loops]
            terms = (polygon_area3d(face),
                     tuple((pts3[(k+1) % count]-pts3[k]).length for k in range(count)),
                     tuple(_corner_angle3(pts3, k) for k in range(count)))
            geometry[fi] = terms
        face_area, lengths3, angles3 = terms
        signed = 0.0
        for k in range(count):
            p, q = pts2[k], pts2[(k + 1) % count]
            signed += p[0] * q[1] - q[0] * p[1]
        signed *= 0.5
        area_uv += abs(signed)
        if signed < 0.0:
            flips += 1
        area_3d += face_area
        lengths2 = [math.hypot(pts2[(k+1) % count][0]-pts2[k][0],
                              pts2[(k+1) % count][1]-pts2[k][1]) for k in range(count)]
        for k in range(count):
            l2 = lengths2[k]
            l3 = lengths3[k]
            if l3 > 1e-12:
                ratios.append(l2 / l3)
        for k in range(count):
            a2 = _corner_angle2(pts2, k, lengths2)
            a3 = angles3[k]
            if a2 is None or a3 is None:
                continue
            worst_angle = max(worst_angle, abs(a2 - a3))
    median = _median(ratios)
    spread = 0.0
    if median > 0.0 and ratios:
        spread = max(abs(r - median) / median for r in ratios)
    return {
        "area_uv": area_uv,
        "area_3d": area_3d,
        "density": math.sqrt(area_uv / area_3d) if area_3d > 0.0 else 0.0,
        "worst_angle_deg": worst_angle,
        "edge_spread": spread,
        "flips": flips,
    }


def restore_pose(topo, faces, reference, allow_rotation=True, target_area=0.0):
    """Move the island back onto its old pose, keeping the new shape.

    `reference` is a `_loop_uv_snapshot`. The similarity that best maps the
    reference onto the current layout is applied in reverse (a Procrustes fit),
    and the island is then scaled so its area matches `target_area` exactly --
    the fit alone cannot guarantee that, because the shape changed.

    `allow_rotation` is the one knob, and the default is to undo the rotation.
    The reason to ever say no was that a rotated island would tip its
    constrained edges off horizontal/vertical -- but the relaxation re-lays
    every constrained run onto its manifold *after* this call, so the projection
    takes that back. Leaving the rotation in place instead hands the relaxation
    a run whose direction the unwrapper chose, which on a narrow island can be
    perpendicular to the constraint; see the call site in `rebuild_islands` for
    the measurement.

    Returns {theta_deg, scale, applied}.
    """
    uvl = topo.uvl
    rows = _uv_loops(topo, faces)
    pairs = [(reference[p], loop[uvl].uv, loop) for p, loop in rows
             if p in reference]
    if len(pairs) < 2:
        return {"applied": False}

    P = np.asarray([(r[0], r[1]) for r, _c, _l in pairs], dtype=np.float64)
    Q = np.asarray([(float(c[0]), float(c[1])) for _r, c, _l in pairs],
                   dtype=np.float64)
    pc = P.mean(axis=0)
    qc = Q.mean(axis=0)
    dx = Q[:, 0] - qc[0]
    dy = Q[:, 1] - qc[1]

    Pc = P - pc
    a = float((Pc[:, 0] * dx + Pc[:, 1] * dy).sum())
    b = float((Pc[:, 0] * dy - Pc[:, 1] * dx).sum())
    norm = float((Pc ** 2).sum())
    if norm < 1e-18:
        return {"applied": False}

    theta = math.atan2(b, a) if allow_rotation else 0.0
    ct, st = math.cos(theta), math.sin(theta)
    # R(theta)^T applied to (dx, dy)
    rx = ct * dx + st * dy
    ry = -st * dx + ct * dy

    scale = 1.0
    if target_area > 0.0:
        have = island_area(topo, faces)
        if have > 0.0:
            scale = math.sqrt(target_area / have)
    if not (1e-3 < scale < 1e3):
        scale = 1.0

    for i, (_r, _c, loop) in enumerate(pairs):
        loop[uvl].uv = (pc[0] + scale * rx[i], pc[1] + scale * ry[i])
    return {"applied": True, "theta_deg": math.degrees(theta),
            "scale": scale}


def _pinned_loops(topo, faces):
    uvl = topo.uvl
    out = set()
    for position, loop in _uv_loops(topo, faces):
        if loop[uvl].pin_uv:
            out.add(position)
    return out


def set_pinned(topo, faces, positions):
    """Pin exactly `positions` (loop positions) inside `faces`, clear the rest.

    Only loops of the given faces are touched, so pins belonging to other
    islands are never disturbed.
    """
    uvl = topo.uvl
    want = set(positions)
    for position, loop in _uv_loops(topo, faces):
        loop[uvl].pin_uv = position in want


def _reference_uv_of_group(topo, reference, group):
    """The pre-Optimize UV of one UV vertex, or None if it is not in `reference`.

    Every loop of a group carries the same UV, so the first one that the
    snapshot happens to cover answers for all of them; the fallback matters only
    for a group that straddles the snapshot's face set, which is exactly the
    pinch case `land_uv_groups` guards against.
    """
    for position in topo.vgroup_loops.get(group, ()):
        if position in reference:
            return reference[position]
    return None


def land_uv_groups(topo, groups_uv, faces):
    """Write exact UVs back for the given UV vertex groups, inside `faces` only.

    Used to re-impose a held boundary *after* a correction that scaled the whole
    island. Same restriction as the write-back at the end of `relax_uv`: two
    islands can share one UV vertex where they pinch together at a single mesh
    vertex, so only the corners of the given faces may be touched -- writing the
    group wholesale would drag the other island's corner along with it.
    """
    wanted = set(faces)
    for group, uv in groups_uv.items():
        positions = topo.vgroup_loops.get(group)
        if not positions:
            continue
        u = float(uv[0])
        v = float(uv[1])
        for position in positions:
            loop = topo.loops[position]
            if loop.face.index in wanted:
                loop[topo.uvl].uv = (u, v)


def _capture_selection(bm, loops=None):
    """Capture the mesh *and* the UV selection, so neither is a casualty.

    Saving the mesh flags alone is not enough, and that is not obvious. The
    re-solve forces `use_uv_select_sync = True` around the `uv.unwrap` call, and
    with sync on Blender derives the UV selection *from* the mesh one -- so the
    user's UV selection (the constrained edges) is replaced by whatever the mesh
    selection says. The mesh selection a caller typically has is not that: the
    marker operators leave every *edge* deselected and only the faces selected,
    which syncs to "no UV edges selected". Put the mesh flags back and the UV
    selection stays empty, so the next command -- Unconstrain, typically -- has
    nothing to act on. Measured: `func_test` section 1's `unconstrain()` left
    every constraint standing.

    Loops are keyed by position in `loops_in_order`, the same identity the
    constraint layer uses. That order survives `uv.unwrap`: the unwrap rewrites
    UVs, not topology -- which is the same assumption `constraint_positions`
    already relies on, since those are read before the unwrap and used after.
    """
    uv_sel = [(loop.uv_select_edge, loop.uv_select_vert)
              for loop in (loops_in_order(bm) if loops is None else loops)]
    return ([f.select for f in bm.faces],
            [e.select for e in bm.edges],
            [v.select for v in bm.verts],
            uv_sel)


def _apply_selection(bm, saved, *, loops=None):
    faces, edges, verts, uv_sel = saved
    for f in bm.faces:
        if f.index < len(faces):
            f.select = faces[f.index]
    for e in bm.edges:
        if e.index < len(edges):
            e.select = edges[e.index]
    for v in bm.verts:
        if v.index < len(verts):
            v.select = verts[v.index]
    for position, loop in enumerate(loops_in_order(bm) if loops is None else loops):
        if position < len(uv_sel):
            loop.uv_select_edge, loop.uv_select_vert = uv_sel[position]


def select_faces_only(bm, faces):
    """Mesh-select exactly the given faces (and their edges/verts)."""
    if len(faces)*4 < len(bm.faces):
        # For a small chart, unrelated elements cannot have all their linked
        # faces selected. Visit adjacency only for candidates of selected faces.
        edges, verts = set(), set()
        for face in bm.faces:
            selected = face.index in faces
            face.select = selected
            if selected:
                edges.update(face.edges)
                verts.update(face.verts)
        for edge in bm.edges:
            edge.select = False
        for vert in bm.verts:
            vert.select = False
        for edge in edges:
            linked = edge.link_faces
            edge.select = bool(linked) and all(face.index in faces for face in linked)
        for vert in verts:
            linked = vert.link_faces
            vert.select = bool(linked) and all(face.index in faces for face in linked)
        return
    for f in bm.faces:
        f.select = f.index in faces
    for e in bm.edges:
        linked = e.link_faces
        e.select = bool(linked) and all(f.index in faces for f in linked)
    for v in bm.verts:
        linked = v.link_faces
        v.select = bool(linked) and all(f.index in faces for f in linked)


def selection_region_pins(topo, faces, free_borders=False, context=None,
                          respect_selection=True):
    """Only UV vertex regions have a pinned selection interface."""
    if not respect_selection:
        return {}
    if context is not None:
        settings=context.scene.tool_settings
        if settings.use_uv_select_sync or settings.uv_select_mode != 'VERTEX':
            return {}
    groups={g for p,l in enumerate(topo.loops) if l.face.index in faces
            for g in topo.loop_edge_vgroups(p)}
    loops=[topo.loops[p] for g in groups for p in topo.vgroup_loops[g]]
    if any(l.uv_select_edge for l in loops):return {}
    selected={g for g in groups if any(topo.loops[p].uv_select_vert for p in topo.vgroup_loops[g])}
    if not selected or selected==groups:return {}
    held=groups-selected
    if not free_borders:
        for tri in triangles_for_faces(topo,faces):
            if any(g not in selected for g in tri):held.update(tri)
    return {g:topo.vgroup_uv(g).copy() for g in held}


def _restore_anchored_area(topo, faces, held, runs, target_area):
    """Restore area along a direction that leaves pins and axis runs feasible."""
    have = island_area(topo, faces)
    if abs(have-target_area) <= max(target_area, 1e-12)*1e-6:
        return True
    triangles = triangles_for_faces(topo, faces)
    groups = sorted({g for tri in triangles for g in tri})
    index = {g: i for i, g in enumerate(groups)}
    x = np.asarray([tuple(topo.vgroup_uv(g)) for g in groups])
    if not len(groups):
        return False
    pinned = {index[g] for g in held if g in index}
    center = np.mean(x[list(pinned)], axis=0) if pinned else x.mean(axis=0)
    direction = x-center
    direction[list(pinned)] = 0
    for kind, positions in runs:
        members = sorted({index[g] for p in positions
                          for g in topo.loop_edge_vgroups(p) if g in index})
        if not members:
            continue
        if kind in (C_HORIZONTAL, C_VERTICAL):
            normal = np.array((0., 1.)) if kind == C_HORIZONTAL else np.array((1., 0.))
        else:
            points = x[members]-x[members].mean(axis=0)
            _, basis = np.linalg.eigh(points.T @ points)
            normal = basis[:, 0]
        perpendicular = direction[members] @ normal
        goal = 0. if any(i in pinned for i in members) else float(perpendicular.mean())
        direction[members] += (goal-perpendicular)[:, None]*normal
    direction[list(pinned)] = 0
    polygons = [np.asarray([index[topo.loop_to_vgroup[topo.loop_pos[l]]]
                            for l in topo.bm.faces[fi].loops]) for fi in sorted(faces)]

    def polygon_areas(value):
        return np.asarray([float(np.sum(value[row, 0]*value[np.roll(row, -1), 1]
                                        -value[np.roll(row, -1), 0]*value[row, 1]))*.5
                           for row in polygons])

    signs = np.sign(polygon_areas(x))
    zero = float(signs @ polygon_areas(x))
    plus, minus = float(signs @ polygon_areas(x+direction)), float(signs @ polygon_areas(x-direction))
    a, b, c = (plus+minus)*.5-zero, (plus-minus)*.5, zero-target_area
    scale = max(abs(a), abs(b), abs(c), 1e-20)
    if abs(a) <= scale*1e-12:
        roots = [-c/b] if abs(b) > scale*1e-12 else []
    else:
        discriminant = b*b-4*a*c
        roots = [(-b+s*math.sqrt(discriminant))/(2*a) for s in (-1., 1.)] if discriminant >= 0 else []
    tri = np.asarray([[index[g] for g in t] for t in triangles])

    def crosses(value):
        p = value[tri]
        u, v = p[:, 1]-p[:, 0], p[:, 2]-p[:, 0]
        return u[:, 0]*v[:, 1]-u[:, 1]*v[:, 0]

    old = crosses(x)
    oriented = np.abs(old) > 2e-14
    for amount in sorted(roots, key=abs):
        candidate = x+amount*direction
        if np.all(np.isfinite(candidate)) and np.all(old[oriented]*crosses(candidate)[oriented] > 0):
            land_uv_groups(topo, {g: candidate[i] for g, i in index.items()}, faces)
            land_uv_groups(topo, held, faces)
            if abs(island_area(topo, faces)-target_area) <= max(target_area, 1e-12)*1e-4:
                return True
    land_uv_groups(topo, {g: x[i] for g, i in index.items()}, faces)
    return False


def _island_pins(topo, faces, reference=None):
    groups = {topo.loop_to_vgroup[p] for fi in faces
              for p in (topo.loop_pos[l] for l in topo.bm.faces[fi].loops)}
    return {g: Vector(reference[topo.vgroup_loops[g][0]]) if reference is not None
            else topo.vgroup_uv(g).copy() for g in groups
            if any(topo.loops[p][topo.uvl].pin_uv for p in topo.vgroup_loops[g])}


def _rebuild_islands(context, obj, faces, constraint_positions=(),
                    method="MINIMUM_STRETCH", margin=.001, iterations=150,
                    step=.7, mix=1., angle_distance_mix=1., keep_metric=False,
                    align_borders=False, align_borders_angle=20.,
                    align_borders_pre_orient=True, fix_boundary=False,
                    auto_iterations=True, unfold_iterations=1, optimize_iterations=1,
                    use_density_map=True, prevent_flips=False, prevent_overlap=False,
                    overlap_space=0., fill_holes=False, free_selection_borders=False,
                    respect_uv_selection=True, _topo=None, _constraint_strength=None,
                    _recovery_orientation=None, _recovery_guarded_faces=(),
                    _quality_before=None, _quality_after=True, _contact_faces=()):
    """Optimize the current parameterization, without resetting its solution."""
    from . import metric
    topo = _topo
    if topo is None:
        bm=bmesh.from_edit_mesh(obj.data);prepare_bm(bm)
        topo=UVTopology(bm,uv_layer(bm))
    else:
        bm=topo.bm
    faces=set(faces)
    reference=_loop_uv_snapshot(topo,faces)
    before=quality_metrics(topo,faces) if _quality_before is None else _quality_before
    islands=face_islands(topo,faces)
    count=0
    for island in islands.values():
        island=set(island)
        area=island_area(topo,island)
        held=_island_pins(topo,island)
        held.update(selection_region_pins(topo,island,free_selection_borders,
                    context,respect_uv_selection))
        if fix_boundary:
            for p in selection_border_loops(topo,island):
                for g in topo.loop_edge_vgroups(p):held[g]=topo.vgroup_uv(g).copy()
        runs=[(k,p) for k,p in constraint_runs(bm,topo,constraint_positions)
              if any(topo.loops[i].face.index in island for i in p)]
        if align_borders and not fix_boundary:
            if align_borders_pre_orient and not held and not runs:
                angle=dominant_border_angle(topo,island)
                if angle is not None:rotate_faces_uv(topo,island,-angle)
            runs+=boundary_align_runs(topo,island,align_borders_angle)
        elif runs and len(held)<=1 and _constraint_strength is None:
            axis=[(k,p) for k,p in runs if k in (C_HORIZONTAL,C_VERTICAL)]
            if axis:
                rotate_faces_uv(topo,island,estimate_axis_rotation(topo,axis),
                                center=next(iter(held.values())) if held else None)
                land_uv_groups(topo,held,island)
        geometry=metric._prepare_geometry(__import__(__name__,fromlist=['']),topo,island)
        if runs and _constraint_strength is None:
            for _ in range(1 if auto_iterations else unfold_iterations):
                feasible_constraint_boundary(topo,island,runs,held,angle_distance_mix,
                                               _geometry=geometry)
        # Hole filling uses Blender's temporary caps; no mesh topology is saved.
        if fill_holes:
            sync=context.scene.tool_settings.use_uv_select_sync
            context.scene.tool_settings.use_uv_select_sync=True
            select_faces_only(bm,island)
            pinned=set()
            for g in held:pinned.update(topo.vgroup_loops[g])
            for k,positions in runs:
                for p in positions:
                    for g in topo.loop_edge_vgroups(p):pinned.update(topo.vgroup_loops[g])
            set_pinned(topo,island,pinned)
            bmesh.update_edit_mesh(obj.data)
            bpy.ops.uv.unwrap(method="MINIMUM_STRETCH",fill_holes=True,
                use_original_bounds=True,no_flip=prevent_flips,
                iterations=100 if auto_iterations else unfold_iterations)
            bm=bmesh.from_edit_mesh(obj.data);prepare_bm(bm)
            topo=UVTopology(bm,uv_layer(bm))
            context.scene.tool_settings.use_uv_select_sync=sync
            geometry=metric._prepare_geometry(__import__(__name__,fromlist=['']),topo,island)
        n,_=metric.solve(__import__(__name__,fromlist=['']),topo,island,held,runs,
            optimize_iterations,angle_length=angle_distance_mix,
            use_density_map=use_density_map,auto=auto_iterations,
            prevent_flips=prevent_flips,
            prevent_overlap=(prevent_overlap or not island.isdisjoint(_recovery_guarded_faces)
                             or not island.isdisjoint(_contact_faces)),
            overlap_space=overlap_space if prevent_overlap else 0.,free_selection_borders=free_selection_borders,
            constraint_strength=_constraint_strength,_geometry=geometry,
            _reference_sign=(next((_recovery_orientation[fi] for fi in island
                                   if fi in _recovery_orientation),None)
                             if _recovery_orientation else None),
            _contact_barrier=not island.isdisjoint(_contact_faces))
        count+=n
        if mix<1:blend_towards(topo,reference,island,mix)
        # Anchored layouts must never be similarity-fitted or rescaled.
        if not held:
            target=mesh_area3d(topo,island) if keep_metric else area
            restore_pose(topo,island,reference,allow_rotation=fill_holes and not runs,target_area=target)
        else:
            land_uv_groups(topo,held,island)
            if not keep_metric:
                _restore_anchored_area(topo,island,held,runs,area)
    bmesh.update_edit_mesh(obj.data)
    return count,0,dict(islands=len(islands),before=before,
                        after=quality_metrics(topo,faces) if _quality_after else None)


def rebuild_islands(context, obj, faces, constraint_positions=(), **settings):
    """One checked replica solve.

    The former angle-triggered post-refinement could rebuild the entire mesh
    topology repeatedly and turn a small constraint edit into a long operation.
    """
    return _checked_rebuild_islands(context, obj, set(faces), constraint_positions, **settings)


def _checked_rebuild_islands(context, obj, faces, constraint_positions=(),
                             _report_quality=True, _recovered=None, **settings):
    # The complete transaction owns the outside-UV invariant. Internal trials
    # can fail, restore and retry; scan the unchanged mesh region only once at
    # this boundary. Keep numeric snapshots, never BMesh wrappers, over unwrap.
    state = {}
    result = _checked_rebuild_body(context, obj, faces, constraint_positions,
        _report_quality=_report_quality, _recovered=_recovered,
        _outside_state=state, **settings)
    if state:
        bm = bmesh.from_edit_mesh(obj.data)
        prepare_bm(bm)
        layer, loops = uv_layer(bm), loops_in_order(bm)
        try:
            if not state['outside']:
                outside_moved = False
            elif isinstance(state['saved'], np.ndarray):
                current = mesh_reader.read_uv(map(loops.__getitem__, state['outside']), layer)
                outside_moved = not np.array_equal(np.asarray(current), state['saved'][state['outside']])
            else:
                outside_moved = any(tuple(loops[p][layer].uv) != state['saved'][p] for p in state['outside'])
        except BaseException:
            for loop, xy in zip(loops, state['saved']):
                loop[layer].uv = xy
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
            raise
        if outside_moved:
            for loop, xy in zip(loops, state['saved']):
                loop[layer].uv = xy
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
            return 0, state['flips'], dict(rejected=True, reason='outside_selection_moved',
                before=result[2].get('before', {}), after=result[2].get('before', {}))
    return result


def _checked_rebuild_body(context, obj, faces, constraint_positions=(),
                          _report_quality=True, _recovered=None,
                          _outside_state=None, **settings):
    """Enforce requested guards and invariants, restoring rejected UV/UI state."""
    faces = set(faces)
    if not faces or settings.get('mix', 1.0) <= 0.0:
        return 0, 0, {'skipped': True}
    import sys
    from . import safety
    module = sys.modules[__name__]
    bm = bmesh.from_edit_mesh(obj.data)
    uvl = uv_layer(bm)
    topo = UVTopology(bm, uvl)
    # Legacy numerical/safety consumers visit coordinates one at a time.
    # Materialize Python tuples once here rather than allocating ndarray row
    # views at every triangle/probe. Keep the packed snapshot for the outer
    # fresh outside-UV guard and native topology arrays.
    saved = topo.initial_uv
    if isinstance(saved, np.ndarray):
        saved = list(map(tuple, saved.tolist()))
    triangle_positions = [tuple(topo.loop_pos[l] for l in tri)
                          for tri in topo.loop_triangles()
                          if tri[0].face.index in faces]
    old_triangles = np.asarray([[saved[p] for p in tri] for tri in triangle_positions])
    old_edges = old_triangles[:, 1:] - old_triangles[:, :1]
    old_cross = old_edges[:, 0, 0]*old_edges[:, 1, 1] - old_edges[:, 0, 1]*old_edges[:, 1, 0]
    oriented = np.abs(old_cross) > 2e-14
    expected_orientation = np.sign(old_cross)
    fold_candidates = ({} if settings.get('prevent_flips', False) or
                       not (np.any(old_cross > 2e-14) and np.any(old_cross < -2e-14)) else
                       safety.folded_face_candidates(topo, triangle_positions, old_cross, saved))
    pins = [bool(loop[uvl].pin_uv) for loop in topo.loops]
    outside_positions = [p for p, loop in enumerate(topo.loops) if loop.face.index not in faces]
    _outside_state.setdefault('saved', topo.initial_uv)
    _outside_state.setdefault('outside', outside_positions)
    # Only hole-fill unwrap changes UI flags here. Input recovery owns its
    # separate native selection/pin transaction. Pure numerical solves never
    # touch these flags, so avoid rewriting the complete edit mesh afterwards.
    restore_ui=bool(settings.get('fill_holes',False))
    selected = _capture_selection(bm, topo.loops) if restore_ui else None
    uv_face_selection = [face.uv_select for face in bm.faces] if restore_ui else None
    selection_valid = bm.uv_select_sync_valid
    sync = context.scene.tool_settings.use_uv_select_sync
    live_unwrap = context.scene.tool_settings.use_edge_path_live_unwrap
    # No UV write has occurred since UVTopology captured saved. Reuse that
    # immutable input only here; candidate checks always capture fresh values.
    probe = safety.AcceptanceProbe(module, topo, faces,
                                  settings.get('prevent_overlap', False), coordinates=saved)
    before = probe.measure(topo, coordinates=probe.reference)
    _outside_state.setdefault('flips', before['flips'])
    # The GUI consumes elapsed time and probe health, not descriptive reports.
    # Direct callers retain full reports by default. The final interpolation
    # fallback still computes its exact angular acceptance gate when needed.
    quality_before = (quality_metrics(topo, set(faces), coordinates=saved)
                      if _report_quality else {})
    input_islands = face_islands(topo, faces)
    original_areas = {root: island_area(topo, island, coordinates=saved)
                      for root, island in input_islands.items()}
    fixed_positions = set(i for i, pin in enumerate(pins) if pin)
    for island in input_islands.values():
        for g in selection_region_pins(topo,island,settings.get('free_selection_borders',False),
                                      context,settings.get('respect_uv_selection',True)):
            fixed_positions.update(topo.vgroup_loops[g])
    if settings.get('fix_boundary'):
        for island in input_islands.values():
            for p in selection_border_loops(topo, island):
                for g in topo.loop_edge_vgroups(p):
                    fixed_positions.update(topo.vgroup_loops[g])

    fixed_groups = sorted({topo.loop_to_vgroup[p] for p in fixed_positions
                           if topo.loops[p].face.index in faces})
    fixed_details = [dict(uv=list(saved[topo.vgroup_loops[g][0]]),
                         loop_positions=list(topo.vgroup_loops[g]),
                         mesh_vertex=topo.loops[topo.vgroup_loops[g][0]].vert.index)
                     for g in fixed_groups]

    if settings.get('fix_boundary'):
        layer=constraint_layer(bm)
        conflicts=[]
        if layer:
            for p in constraint_positions:
                loop=topo.loops[p];other=topo.loop_pos[loop.link_loop_next]
                if p not in fixed_positions or other not in fixed_positions:continue
                d=loop.link_loop_next[uvl].uv-loop[uvl].uv
                if ((loop[layer]==C_HORIZONTAL and abs(d.y)>1e-7) or
                    (loop[layer]==C_VERTICAL and abs(d.x)>1e-7)):
                    conflicts.append(p)
        if conflicts:
            return 0,before['flips'],dict(rejected=True,reason='fixed_boundary_conflict',
                conflicts=len(conflicts),before=quality_before,after=quality_before)

    # Separate pinned vertices in one axis run must share that axis coordinate.
    # An exact constraint cannot move these pins to make the request feasible.
    conflicts = 0
    conflict_points = []
    for kind, positions in constraint_runs(bm, topo, constraint_positions):
        if kind not in (C_HORIZONTAL, C_VERTICAL):
            continue
        groups = {g for p in positions for g in topo.loop_edge_vgroups(p)}
        axis = 1 if kind == C_HORIZONTAL else 0
        pinned_groups = [g for g in groups
                         if any(p in fixed_positions for p in topo.vgroup_loops[g])]
        values = [saved[topo.vgroup_loops[g][0]][axis] for g in pinned_groups]
        if values and max(values)-min(values) > 2e-7:
            conflicts += 1
            for g in (min(pinned_groups,key=lambda g:saved[topo.vgroup_loops[g][0]][axis]),
                      max(pinned_groups,key=lambda g:saved[topo.vgroup_loops[g][0]][axis])):
                conflict_points.append(dict(uv=list(saved[topo.vgroup_loops[g][0]]),
                    loop_positions=list(topo.vgroup_loops[g]),
                    mesh_vertex=topo.loops[topo.vgroup_loops[g][0]].vert.index))
    if conflicts:
        return 0, before['flips'], dict(rejected=True,
            reason='pinned_constraint_conflict', conflicts=conflicts,
            conflict_points=conflict_points,
            health=before, before=quality_before, after=quality_before)

    # An exact run containing all three corners has no nondegenerate endpoint.
    # Diagnose it before numerical trials instead of publishing a folded map.
    components = {}
    for run_id, (kind, positions) in enumerate(constraint_runs(bm, topo, constraint_positions)):
        mapping = components.setdefault(kind, {})
        for p in positions:
            for g in topo.loop_edge_vgroups(p):
                mapping[g] = run_id
    collapsed = set()
    for positions, area in zip(triangle_positions, old_cross):
        if abs(area) <= 2e-14:
            continue
        groups = [topo.loop_to_vgroup[p] for p in positions]
        if any(groups[0] in mapping and
               all(mapping.get(g) == mapping[groups[0]] for g in groups)
               for mapping in components.values()):
            collapsed.add(topo.loops[positions[0]].face.index)
    if collapsed and settings.get('mix',1.)==1.:
        return 0,before['flips'],dict(rejected=True,reason='constraint_triangle_conflict',
            conflicts=len(collapsed),conflict_faces=sorted(collapsed),health=before,
            before=quality_before,after=quality_before)
    chart_signs={}
    chart_roots=[topo.face_island[topo.loops[tri[0]].face.index] for tri in triangle_positions]
    for root,area in zip(chart_roots,old_cross):
        chart_signs.setdefault(root,set()).add(1 if area>2e-14 else -1 if area < -2e-14 else 0)
    healthy_roots={root for root,signs in chart_signs.items() if signs-{0} in ({1},{-1})}
    healthy_mask=np.asarray([root in healthy_roots for root in chart_roots]) & oriented
    healthy_input=bool(np.any(healthy_mask))
    def valid(topo, health, coordinates, loop_coordinates=None):
        def uv_at(p):
            return (topo.loops[p][topo.uvl].uv if loop_coordinates is None else
                    loop_coordinates[p])
        # Negative winding may be an intentional mirrored island. Comparing
        # only total negative areas would incorrectly accept its reflection.
        current = np.asarray([coordinates[tri] for tri in triangle_positions])
        edges = current[:, 1:] - current[:, :1]
        cross = edges[:, 0, 0]*edges[:, 1, 1] - edges[:, 0, 1]*edges[:, 1, 0]
        orientation_valid = bool(np.all(expected_orientation[oriented]*cross[oriented] > 0)
                                 and np.all(np.abs(cross[oriented]) > 2e-14))
        repaired = []
        if not orientation_valid and fold_candidates:
            bad = np.flatnonzero(oriented & ((expected_orientation*cross <= 0) | (np.abs(cross) <= 2e-14)))
            changed_faces = {topo.loops[triangle_positions[i][0]].face.index for i in bad}
            for fi in sorted(changed_faces):
                if fi not in fold_candidates:
                    break
                indices, sign, corners = fold_candidates[fi]
                polygon = [tuple(uv_at(p)) for p in corners]
                if (not np.all(cross[indices]*sign > 2e-14) or
                        safety._boundary_crosses(polygon, include_touch=True)):
                    break
                repaired.append(fi)
            else:
                orientation_valid = True
        health['repaired_fold_faces'] = repaired if orientation_valid else []
        # Negative winding can be an intentional input mirror. Report new
        # inversions separately from the absolute negative-triangle count.
        health['new_flips'] = (0 if orientation_valid else
                              int(np.count_nonzero(oriented & (expected_orientation*cross <= 0))))
        tolerance = before['max_deviation']*(1-settings.get('mix',1.0))+.05
        limited = (health['max_deviation'] > tolerance
                   and (contact_faces or settings.get('prevent_flips', False) or settings.get('prevent_overlap', False))
                   and health['max_deviation'] < before['max_deviation']-1e-5)
        health['constraints_limited'] = limited
        reason = None
        if not health['finite']:
            reason = 'non_finite'
        elif health['zero_edges']:
            reason = 'collapsed_constraint'
        elif health['max_deviation'] > tolerance and not limited:
            reason = 'constraint_residual'
        elif ((settings.get('prevent_flips', False) and (
                not orientation_valid or health['degenerate'] > before['degenerate'])) or
              (healthy_input and (np.any(expected_orientation[healthy_mask]*cross[healthy_mask]<=0)
                                  or np.any(np.abs(cross[healthy_mask])<=2e-14)))):
            reason = 'triangle_orientation'
        elif any(max(abs(float(uv_at(p)[j])-saved[p][j])
                     for j in (0,1)) > 1e-7 for p in fixed_positions):
            reason = 'fixed_point_moved'
        elif (loop_coordinates is not None and
              not np.array_equal(loop_coordinates[outside_positions], saved_array[outside_positions])):
            reason = 'outside_selection_moved'
        elif not settings.get('keep_metric'):
            for root, island in face_islands(topo, faces).items():
                area = original_areas[root]
                if abs(island_area(topo, island, coordinates=loop_coordinates)-area) > max(area, 1e-12)*1e-4:
                    reason = 'area_drift'
                    break
        if reason is None and settings.get('prevent_overlap', False):
            health.update(probe.check_overlaps(topo, coordinates=coordinates))
            if health['novel_self_overlaps']:
                reason = 'new_self_overlap'
            elif health['novel_island_overlaps']:
                reason = 'new_island_overlap'
        if reason is None and recovery_report is not None:
            for chart in recovery_report['repaired']:
                repaired_probe = safety.AcceptanceProbe(module, topo, set(chart['faces']),
                                                        coordinates=loop_coordinates)
                remaining = repaired_probe.check_overlaps(
                    topo, include_existing=True, coordinates=repaired_probe.reference)
                if remaining['self_overlaps']:
                    reason = 'input_recovery_failed'
                    break
        if reason:
            health['rejection_reason'] = reason
        return reason is None

    def restore(value=None):
        mesh = bmesh.from_edit_mesh(obj.data)
        prepare_bm(mesh)
        layer = uv_layer(mesh)
        loops = loops_in_order(mesh)
        for loop, uv in zip(loops, saved if value is None else value):
            loop[layer].uv = uv
        bmesh.update_edit_mesh(obj.data)

    saved_array = np.asarray(saved, dtype=float)
    def interpolation(candidate, alpha, positions, *, scale_pinned=False):
        # Match both BMesh round trips: interpolation first, area correction
        # second. The summation order and loop-weighted centre stay unchanged.
        value = np.array(candidate, dtype=float, copy=True)
        p = np.asarray(positions, dtype=np.int64)
        value[p] = (saved_array[p] + alpha*(value[p]-saved_array[p])).astype(np.float32)
        for root, island in input_islands.items():
            if scale_pinned or not any(topo.loops[p].face.index in island for p in fixed_positions):
                scale_faces_to_area(topo, island, original_areas[root], coordinates=value)
        if fixed_positions:
            p = np.asarray(sorted(fixed_positions), dtype=np.int64)
            value[p] = saved_array[p]
        coordinates = probe.capture(topo, coordinates=value)
        health = probe.measure(topo, coordinates=coordinates, loop_coordinates=value)
        return value, coordinates, health

    def commit_interpolation(value):
        # Only successful trials touch the edit mesh. Recheck live coordinates
        # once after commit, including the exact outside-selection guard.
        for p in probe.target_positions:
            topo.loops[p][topo.uvl].uv = value[p]
        coordinates = probe.capture(topo)
        health = probe.measure(topo, coordinates=coordinates)
        if not valid(topo, health, coordinates):
            return None
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        return coordinates, health

    def prefer_clean_relaxation(coordinates, health):
        # A feasible relaxation can cross a distant part of its own outline
        # while keeping every triangle's winding. Prefer a shorter step only
        # when the complete input charts and all remaining constraints were
        # already feasible. This is not a second solve or a rejection policy.
        if not clean_relaxation or not probe.check_overlaps(
                topo, include_existing=True, coordinates=coordinates)['self_overlaps']:
            return coordinates, health, None
        candidate = np.asarray([tuple(loop[topo.uvl].uv) for loop in topo.loops])
        for attempt in range(1, 13):
            alpha = .5 ** attempt
            value, current, checked = interpolation(candidate, alpha, probe.target_positions, scale_pinned=True)
            if valid(topo, checked, current, value):
                overlap = probe.check_overlaps(topo, include_existing=True, coordinates=current)
                if overlap['overlap_check_complete'] and not overlap['self_overlaps']:
                    committed = commit_interpolation(value)
                    if committed is not None:
                        current, checked = committed
                        checked.update(overlap)
                        return current, checked, alpha
        # Constraints can require an overlap. Retain the ordinary accepted
        # result if no tested clean step meets every existing acceptance gate.
        for p in probe.target_positions:
            topo.loops[p][topo.uvl].uv = candidate[p]
        return coordinates, health, None

    def protected_trials(initial_deviation):
        best_values, best_result, best_deviation = None, None, initial_deviation
        strengths = ((1., 100., 1e4, 1e6) if settings.get('auto_iterations', True)
                     else (1e6,))
        for strength in strengths:
            guarded = _rebuild_islands(context, obj, faces, constraint_positions,
                _topo=topo, _constraint_strength=strength,_quality_before=quality_before,
                _quality_after=False, **dict(settings, _contact_faces=contact_faces))
            coordinates = probe.capture(topo)
            health = probe.measure(topo, coordinates=coordinates)
            accepted = valid(topo, health, coordinates) and health['max_deviation'] < best_deviation
            if accepted and contact_faces and not health['constraints_limited']:
                # The distance barrier is best-effort, not continuous
                # collision detection. Validate the actual rounded UVs.
                overlap = probe.check_overlaps(topo, include_existing=True, coordinates=coordinates)
                health.update(overlap)
                accepted = overlap['overlap_check_complete'] and not overlap['self_overlaps']
            if accepted:
                best_values = [tuple(l[topo.uvl].uv) for l in topo.loops]
                best_deviation = health['max_deviation']
                guarded[2].update(before=quality_before, health=health,
                    solver='protected_metric', constraint_penalty=strength)
                if recovery_report is not None:
                    guarded[2]['input_recovery'] = recovery_report
                if contact_faces:
                    guarded[2]['contact_barrier'] = True
                best_result = guarded[0], health['flips'], guarded[2]
                if not health['constraints_limited']:
                    break
            else:
                # A weak penalty can first improve distortion while its
                # worst constrained edge gets worse. Retry the stronger
                # penalty from the last accepted state, not this trial.
                restore((seed_values if seed_values is not None else saved)
                        if best_values is None else best_values)
        return best_values, best_result

    clean_relaxation = False
    contact_faces = set()
    contact_eligible = set()
    try:
        context.scene.tool_settings.use_edge_path_live_unwrap = False
        from . import recovery
        recovery_report = None
        seed_values = None
        if context.edit_object == obj and len(context.objects_in_mode_unique_data) == 1:
            recalled = recovery.recall(module, topo, faces, constraint_positions,
                                      fixed_positions, saved, pins, settings, _recovered)
            recovery_plan = None if recalled else recovery.plan(module, topo, faces, settings)
            clean_relaxation = bool(
                recovery_plan is not None and faces.issubset(recovery_plan['clean_faces'])
                and before['max_deviation'] <= .05 and not before['zero_edges']
                and not fixed_positions and not settings.get('keep_metric')
                and not settings.get('prevent_overlap') and not settings.get('fill_holes')
                and settings.get('mix', 1.) == 1.
                and all(len(island) == len(topo.island_faces[root])
                        for root, island in input_islands.items()))
            # Prevent contacts during an initially clean H/V continuation.
            # Reuse the recovery plan's full chart check and this topology;
            # no ordinary solve or extra whole-mesh transaction runs first.
            if (recovery_plan is not None and faces.issubset(recovery_plan['clean_faces'])
                    and before['max_deviation'] > .05 and not before['zero_edges']
                    and not fixed_positions and not settings.get('keep_metric')
                    and not settings.get('prevent_overlap') and not settings.get('fill_holes')
                    and settings.get('mix', 1.) == 1. and settings.get('auto_iterations', True)
                    and all(len(island) == len(topo.island_faces[root])
                            for root, island in input_islands.items())
                    and constraint_positions and set(constraint_positions) == set(probe.positions)
                    and all(kind in (C_HORIZONTAL, C_VERTICAL)
                            for kind, _ in constraint_runs(bm, topo, constraint_positions))):
                from . import native_engine
                if native_engine.supports_contact(len(triangle_positions)):
                    contact_eligible = faces.copy()
            if recalled or recovery_plan['charts']:
                if recalled:
                    # This is a fresh topology after the caller restored the
                    # exact original input. No native operator runs here.
                    for loop, xy in zip(topo.loops, recalled['seed']):
                        loop[uvl].uv = xy
                    loop = None
                    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
                    topo.refresh_uv()
                    recovery_report = recalled['report']
                else:
                    prepared = recovery.prepare(module, topo, recovery_plan, fixed_positions)
                    # Native parameterization must not inherit live caller handles.
                    loop = layer = None
                    del bm, topo, uvl
                    recovery_report = recovery.seed(module, context, obj, recovery_plan,
                                                    saved, pins, fixed_positions, settings,
                                                    _prepared=prepared)
                    bm = bmesh.from_edit_mesh(obj.data)
                    uvl = uv_layer(bm)
                    topo = UVTopology(bm, uvl)
                if recovery_report['failed']:
                    restore()
                    if settings.get('prevent_flips', False) or settings.get('prevent_overlap', False):
                        return 0, before['flips'], dict(rejected=True, reason='input_recovery_failed',
                            input_recovery=recovery_report, before=quality_before, after=quality_before)
                    bm = bmesh.from_edit_mesh(obj.data)
                    uvl = uv_layer(bm)
                    topo = UVTopology(bm, uvl)
                    recovery_report['repaired'] = []
                    # A failed native seed does not make a known input fold an
                    # intentional per-triangle mirror. Let the existing metric
                    # solve use the verified chart orientation to untangle it.
                    orientation = {fi: chart['sign'] for chart in recovery_plan['charts']
                                   for fi in chart['faces']}
                    settings = dict(settings, _recovery_orientation=orientation)
                    for i, tri in enumerate(triangle_positions):
                        sign = orientation.get(topo.loops[tri[0]].face.index)
                        if sign is not None:
                            expected_orientation[i] = sign
                            oriented[i] = True
                for chart in recovery_report['repaired']:
                    repaired_faces = set(chart['faces'])
                    for i, tri in enumerate(triangle_positions):
                        if topo.loops[tri[0]].face.index in repaired_faces:
                            expected_orientation[i] = chart['sign']
                            oriented[i] = True
                            healthy_mask[i] = True
                    for root, island in face_islands(topo, faces).items():
                        if set(island) == repaired_faces:
                            # A fully pinned outline fixes its signed area.
                            # Otherwise preserve the original UV surface area,
                            # matching the normal optimize policy.
                            original_areas[root] = chart['area']
                if recovery_report['repaired']:
                    healthy_input=bool(np.any(healthy_mask))
                    seed_values = [tuple(l[uvl].uv) for l in topo.loops]
                    # Preserve the recovered chart's embedding through the
                    # final solve. This guards only its own boundary; the
                    # user's cross-island overlap option stays independent.
                    guarded_faces = {fi for chart in recovery_report['repaired'] for fi in chart['faces']}
                    settings = dict(settings, _recovery_guarded_faces=guarded_faces)
        # Numeric solves change coordinates, not UV connectivity. Reuse this
        # operation's topology; native hole-filling unwrap rebuilds the BMesh.
        reusable = not settings.get('fill_holes', False)
        initial_deviation = before['max_deviation']
        recovery_guard = bool(settings.get('_recovery_guarded_faces'))
        if recovery_guard:
            initial_deviation = max(initial_deviation, probe.measure(topo)['max_deviation'])
        if (reusable and initial_deviation > .05 and
                (recovery_guard or settings.get('prevent_flips', False) or settings.get('prevent_overlap', False))):
            # Increase the constraint penalty inside the protected optimization.
            # Keep each feasible improvement; an over-aggressive later stage
            # must not discard the preceding useful result.
            best_values, best_result = protected_trials(initial_deviation)
            if best_result is not None:
                restore(best_values)
                best_result[2]['after'] = quality_metrics(topo, faces) if _report_quality else {}
                return best_result
            if seed_values is None:
                restore()
                health = before.copy()
                health.update(new_flips=0, constraints_limited=True)
                return 0, before['flips'], dict(skipped=True, protected_stop=True,
                    solver='protected_metric', health=health, before=quality_before, after=quality_before)
            # Keep the verified recovery seed for the checked hard trial and
            # its existing recovered-initialization fallback below.
            restore(seed_values)
        result = _rebuild_islands(context, obj, faces, constraint_positions,
                                  _topo=topo if reusable else None,_quality_before=quality_before,
                                  _quality_after=False, **settings)
        if recovery_report is not None:
            result[2]['input_recovery'] = recovery_report
            result[2]['before'] = quality_before
        if not reusable:
            bm = bmesh.from_edit_mesh(obj.data)
            prepare_bm(bm)
            topo = UVTopology(bm, uv_layer(bm))
        coordinates = probe.capture(topo)
        after = probe.measure(topo, coordinates=coordinates)
        if valid(topo, after, coordinates):
            if contact_eligible:
                overlap = probe.check_overlaps(topo, include_existing=True, coordinates=coordinates)
                if overlap['self_overlaps']:
                    # Pay for contact only after the ordinary result actually
                    # crosses its own outline. Reuse the original input, this
                    # topology and safety probe; no second recovery transaction.
                    ordinary = [tuple(l[topo.uvl].uv) for l in topo.loops]
                    restore()
                    contact_faces.update(contact_eligible)
                    try:
                        best_values, best_result = protected_trials(before['max_deviation'])
                    finally:
                        contact_faces.clear()
                    if best_result is not None and not best_result[2]['health']['constraints_limited']:
                        restore(best_values)
                        best_result[2]['after'] = quality_metrics(topo, faces) if _report_quality else {}
                        return best_result
                    # No complete clean contact result: retain the originally
                    # accepted UVs under the user's unchanged protection policy.
                    restore(ordinary)
            coordinates, after, clean_step = prefer_clean_relaxation(coordinates, after)
            if clean_step is not None:
                result[2]['accepted_step'] = clean_step
            result[2]['after'] = quality_metrics(topo, faces) if _report_quality else {}
            result[2]['health'] = after
            result[2]['solver'] = ('recovered_projected_metric' if recovery_report and recovery_report['repaired']
                                   else 'projected_metric')
            return result[0], after['flips'], result[2]
        if (healthy_input and after.get('rejection_reason')=='triangle_orientation'
                and not settings.get('prevent_flips',False)):
            # A healthy chart whose hard projection folded can usually reach
            # the same directions through a positive-area penalty continuation.
            # Reuse the checked protected route; never publish the folded trial.
            # Construct the numeric handoff only when escalation is needed;
            # ordinary successful solves pay no snapshot construction cost.
            recovered = recovery.remember(
                recovery.seed_key(module, topo, faces, constraint_positions,
                                  fixed_positions, saved, pins, settings) if seed_values is not None else None,
                seed_values, recovery_report)
            restore()
            loop=layer=None
            del bm,topo,uvl
            return _checked_rebuild_body(context,obj,faces,constraint_positions,
                                            _report_quality=_report_quality,
                                            _recovered=recovered,
                                            _outside_state=_outside_state,
                                            **dict(settings,prevent_flips=True))
        if seed_values is not None:
            restore(seed_values)
            bm = bmesh.from_edit_mesh(obj.data)
            topo = UVTopology(bm, uv_layer(bm))
            coordinates = probe.capture(topo)
            after = probe.measure(topo, coordinates=coordinates)
            if valid(topo, after, coordinates):
                return len(faces), after['flips'], dict(islands=len(face_islands(topo, faces)),
                    solver='recovered_initialization', input_recovery=recovery_report,
                    before=quality_before,
                    after=quality_metrics(topo, faces) if _report_quality else {}, health=after)
            # The original input is folded, so continuation from it cannot
            # cross the repaired orientation barrier. Keep the real reason
            # (for example a new overlap with a neighbouring island).
            restore()
            return 0, before['flips'], dict(rejected=True,
                reason=after.get('rejection_reason', 'input_recovery_failed'),
                input_recovery=recovery_report, health=after,
                before=quality_before, after=quality_before)
        # Removing constraints starts from a feasible layout. Preserve a safe
        # part of the metric step before trying a different relaxation method.
        # Otherwise the last fallback replaces the useful candidate and can
        # turn a partially blocked optimize into an unchanged rollback.
        if not settings.get('keep_metric') and before['max_deviation'] <= 0.05 and before['zero_edges'] == 0:
            candidate = np.asarray([tuple(loop[topo.uvl].uv) for loop in topo.loops])
            for attempt in range(1, 13):
                alpha = 0.5 ** attempt
                value, coordinates, health = interpolation(candidate, alpha, range(len(saved)))
                if valid(topo, health, coordinates, value):
                    committed = commit_interpolation(value)
                    if committed is None:
                        continue
                    coordinates, health = committed
                    result[2].update(health=health, solver='projected_metric',
                        accepted_step=alpha,
                        after=quality_metrics(topo,faces) if _report_quality else {})
                    return result[0], health['flips'], result[2]
        # Try a minimal projection before lengthy relaxation can damage a
        # newly constrained boundary. Each attempt starts from the saved UVs.
        budgets = ([-2, -3, -4, -5, -6, -1, 0, 8, 32, settings.get('iterations', 150)]
                   if before['max_deviation'] > 0.05 else [settings.get('iterations', 150)])
        for budget in budgets:
            restore()
            # The existing layout is often a better starting point than unwrap.
            if not reusable:
                bm = bmesh.from_edit_mesh(obj.data)
                prepare_bm(bm)
                topo = UVTopology(bm, uv_layer(bm))
            count = 0
            for island in face_islands(topo, set(faces)).values():
                island=set(island)
                area = island_area(topo, island)
                runs = constraint_runs(bm, topo, constraint_positions)
                runs = [(kind, pos) for kind, pos in runs
                        if any(topo.loops[p].face.index in island for p in pos)]
                held = _island_pins(topo, island, saved)
                held.update(selection_region_pins(topo,island,settings.get('free_selection_borders',False),
                            context,settings.get('respect_uv_selection',True)))
                if settings.get('fix_boundary'):
                    for p in selection_border_loops(topo, island):
                        for g in topo.loop_edge_vgroups(p):
                            held[g] = topo.vgroup_uv(g).copy()
                if runs and not held:
                    axis = [(k,p) for k,p in runs if k in (C_HORIZONTAL,C_VERTICAL)]
                    if axis:
                        rotate_faces_uv(topo, island, estimate_axis_rotation(topo, axis))
                if budget == -2:
                    border=rectangular_boundary_layout(topo,island,runs,held)
                    if border is None:
                        continue
                    from . import metric
                    n,_=metric.solve(module,topo,island,border,runs,
                        settings.get('optimize_iterations',1),
                        angle_length=settings.get('angle_distance_mix',1.),
                        auto=settings.get('auto_iterations',True),
                        use_density_map=settings.get('use_density_map',True),
                        prevent_flips=settings.get('prevent_flips',False),
                        prevent_overlap=settings.get('prevent_overlap',False),
                        overlap_space=settings.get('overlap_space',0.))
                elif budget == -3:
                    n = deform_constraint_boundary(topo, island, runs, held, transport=True)
                    from . import metric
                    metric.solve(module, topo, island, held, runs,
                        settings.get('optimize_iterations', 1),
                        angle_length=settings.get('angle_distance_mix', 1.),
                        auto=settings.get('auto_iterations', True),
                        use_density_map=settings.get('use_density_map', True),
                        prevent_flips=settings.get('prevent_flips', False),
                        prevent_overlap=settings.get('prevent_overlap', False),
                        overlap_space=settings.get('overlap_space', 0.))
                elif budget == -4:
                    n = continue_constraint_boundary(topo, island, runs, held,
                        angle_length=settings.get('angle_distance_mix', 1.))
                    if not n:
                        continue
                    from . import metric
                    metric.solve(module, topo, island, held, runs,
                        settings.get('optimize_iterations', 1),
                        angle_length=settings.get('angle_distance_mix', 1.),
                        auto=settings.get('auto_iterations', True),
                        use_density_map=settings.get('use_density_map', True),
                        prevent_flips=settings.get('prevent_flips', False),
                        prevent_overlap=settings.get('prevent_overlap', False),
                        overlap_space=settings.get('overlap_space', 0.))
                elif budget == -6:
                    from . import proxy
                    n = proxy.fit_boundary(module, topo, island, runs, held,
                        iterations=(40 if settings.get('auto_iterations', True) else
                                    max(1, int(settings.get('optimize_iterations', 1)))),
                        angle_length=settings.get('angle_distance_mix', 1.),
                        use_density_map=settings.get('use_density_map', True),
                        prevent_flips=settings.get('prevent_flips', False),
                        prevent_overlap=settings.get('prevent_overlap', False),
                        overlap_space=settings.get('overlap_space', 0.))
                    if not n:
                        continue
                    # The supplemental fit already meets the angular tolerance.
                    # An exact projection here could erase its valid thickness.
                elif budget == -5:
                    shared = stacked_uv_groups(topo, island)
                    n = fit_constraint_boundary(topo, island, runs, held,
                        angle_length=settings.get('angle_distance_mix', 1.),
                        shared_uv_groups=shared,
                        iterations=(200 if settings.get('auto_iterations', True) else
                                    max(1, int(settings.get('optimize_iterations', 1)))),
                        use_density_map=settings.get('use_density_map', True),
                        prevent_flips=settings.get('prevent_flips', False),
                        prevent_overlap=settings.get('prevent_overlap', False),
                        overlap_space=settings.get('overlap_space', 0.))
                    if not n:
                        continue
                    if not shared:
                        from . import metric
                        metric.solve(module, topo, island, held, runs,
                            settings.get('optimize_iterations', 1),
                            angle_length=settings.get('angle_distance_mix', 1.),
                            auto=settings.get('auto_iterations', True),
                            use_density_map=settings.get('use_density_map', True),
                            prevent_flips=settings.get('prevent_flips', False),
                            prevent_overlap=settings.get('prevent_overlap', False),
                            overlap_space=settings.get('overlap_space', 0.))
                    # Only an input stack keeps the tolerance-feasible fit:
                    # hard projection would separate its shared UV variables.
                elif budget == -1:
                    n = deform_constraint_boundary(topo, island, runs, held)
                else:
                    n, _ = relax_uv(topo, island, held, runs=runs,
                                    iterations=budget,
                                    step=settings.get('step',0.7),
                                    angle_distance_mix=settings.get('angle_distance_mix',0.0))
                count += n
                if settings.get('mix', 1.0) < 1.0:
                    blend_towards(topo, {i:uv for i,uv in enumerate(saved)}, island,
                                  settings['mix'])
                target_area = mesh_area3d(topo, island) if settings.get('keep_metric') else area
                if not held:
                    scale_faces_to_area(topo, island, target_area)
                if held:
                    land_uv_groups(topo, held, island)
                    if not settings.get('keep_metric'):
                        _restore_anchored_area(topo,island,held,runs,target_area)
            bmesh.update_edit_mesh(obj.data)
            coordinates = probe.capture(topo)
            after = probe.measure(topo, coordinates=coordinates)
            if valid(topo, after, coordinates):
                return count, after['flips'], dict(islands=len(face_islands(topo,set(faces))),
                    before=quality_before,
                    after=quality_metrics(topo,set(faces)) if _report_quality else {},
                    health=after, solver='safe_fallback', fallback_budget=budget)
        if not settings.get('keep_metric') and before['max_deviation'] <= 0.05 and before['zero_edges'] == 0:
            if not quality_before:
                quality_before = quality_metrics(topo, set(faces), coordinates=saved)
            candidate = np.asarray([tuple(loop[topo.uvl].uv) for loop in topo.loops])
            # Feasible axis constraints remain feasible under interpolation.
            # Test the *whole* final layout, including other islands.
            for attempt in range(1, 11):
                alpha = 0.5 ** attempt
                value, coordinates, after = interpolation(candidate, alpha, range(len(saved)), scale_pinned=True)
                q = quality_metrics(topo, set(faces), coordinates=value)
                if valid(topo, after, coordinates, value) and q['worst_angle_deg'] <= quality_before['worst_angle_deg'] + 1e-4:
                    committed = commit_interpolation(value)
                    if committed is None:
                        continue
                    coordinates, after = committed
                    return count, after['flips'], dict(islands=len(face_islands(topo,set(faces))),
                        before=quality_before, after=q, health=after, accepted_step=alpha)
        restore()
        reason = after.get('rejection_reason', 'no_feasible_result')
        conflict_info = {}
        if collapsed and reason in ('triangle_orientation', 'collapsed_constraint',
                                     'constraint_residual'):
            reason = 'constraint_triangle_conflict'
            conflict_info = dict(conflicts=len(collapsed),
                                 conflict_faces=sorted(collapsed))
        underlying_reason = reason
        if fixed_details and reason in ('triangle_orientation', 'constraint_residual',
                                       'no_feasible_result', 'constraint_triangle_conflict'):
            reason = 'pinned_layout_rejected'
            conflict_info.update(fixed_points=fixed_details,
                                 underlying_reason=underlying_reason)
        return 0, before['flips'], dict(rejected=True, health=after,
                                      reason=reason, **conflict_info,
                                      before=quality_before, after=quality_before)
    except Exception:
        restore()
        bmesh.from_edit_mesh(obj.data).uv_select_sync_valid = selection_valid
        raise
    finally:
        if context.scene.tool_settings.use_uv_select_sync != sync:
            context.scene.tool_settings.use_uv_select_sync = sync
        if restore_ui:
            bm = bmesh.from_edit_mesh(obj.data)
            prepare_bm(bm)
            layer = uv_layer(bm)
            for loop, pin in zip(loops_in_order(bm), pins):
                loop[layer].pin_uv = pin
            _apply_selection(bm, selected)
            for face, was_selected in zip(bm.faces, uv_face_selection):
                face.uv_select = was_selected
            bm.uv_select_sync_valid = selection_valid
            bmesh.update_edit_mesh(obj.data)
        context.scene.tool_settings.use_edge_path_live_unwrap = live_unwrap
