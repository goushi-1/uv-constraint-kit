# SPDX-License-Identifier: GPL-3.0-or-later
"""Bundled native engines, using only array buffers over a versioned C ABI.

The engine never receives BMesh handles and never changes Blender state. Its
candidate is committed by the existing checked solver on Blender's main thread.
Unsupported platforms/load failures retain the Python implementation.
"""
import atexit
import ctypes as ct
import hashlib
import json
import math
from pathlib import Path
import platform
import threading

import numpy as np

_enabled = True
_required = False  # Differential tests may require every native call to succeed.
_library = None
_attempted = False
_error = None
_calls = dict(solve=0, deform=0, uv_groups=0, islands=0, overlaps=0, intersections=0, frames=0)
_fallbacks = {}
_last_solve = {}
_last_overlap = {}
_GPU_MIN_TRIANGLES = 32768
_gpu_lock = threading.RLock()
_gpu_library = None
_gpu_attempted = False
_gpu_disabled = False
_gpu_reason = None
_gpu_calls = 0
_gpu_fallbacks = 0
_last_backend = None


class _Input(ct.Structure):
    _fields_ = ([(k, ct.c_int64) for k in ('n', 'nt', 'nr', 'nm', 'ne', 'nb')]
                + [(k, ct.c_void_p) for k in (
                    'uv', 'inverse', 'weights', 'tri', 'kinds', 'offsets', 'members',
                    'fixed', 'held', 'penalty_axis', 'penalty_edges', 'penalty_weights',
                    'border_edges')]
                + [(k, ct.c_double) for k in (
                    'density', 'angle_length', 'strength', 'penalty_scale', 'overlap_space')]
                + [(k, ct.c_int64) for k in (
                    'iterations', 'automatic', 'prevent_flips', 'prevent_overlap')])


class _Output(ct.Structure):
    _fields_ = ([(k, ct.c_int64) for k in ('accepted', 'evaluations', 'reciprocal', 'finite')]
                + [('energy', ct.c_double), ('gradient_max', ct.c_double)])


def _load():
    global _library, _attempted, _error
    if not _enabled:
        return None
    if not _attempted:
        _attempted = True
        try:
            if platform.system() != 'Windows' or platform.machine().upper() not in ('AMD64', 'X86_64'):
                raise OSError('no bundled engine for this platform')
            folder = Path(__file__).resolve().parent / 'bin' / 'windows-x64'
            identity = json.loads((folder / 'engine.json').read_text(encoding='utf8'))
            filename = identity['library']
            if (Path(filename).name != filename or not filename.startswith('uvck_engine_')
                    or not filename.endswith('.dll') or identity['abi'] != 1):
                raise OSError('invalid CPU engine identity')
            path = folder / filename
            if hashlib.sha256(path.read_bytes()).hexdigest() != identity['sha256']:
                raise OSError('CPU engine checksum mismatch')
            lib = ct.CDLL(str(path), winmode=0x1100)
            lib.uvck_abi.argtypes = [ct.c_int64, ct.c_int64]
            lib.uvck_abi.restype = ct.c_int64
            if lib.uvck_abi(ct.sizeof(_Input), ct.sizeof(_Output)) != 1:
                raise OSError('CPU engine ABI mismatch')
            lib.uvck_solve.argtypes = [ct.POINTER(_Input), ct.c_void_p, ct.POINTER(_Output)]
            lib.uvck_solve.restype = ct.c_int
            lib.uvck_evaluate.argtypes = [ct.POINTER(_Input), ct.c_int64, ct.POINTER(ct.c_double), ct.c_void_p]
            lib.uvck_evaluate.restype = ct.c_int
            lib.uvck_deform.argtypes = [ct.c_int64, ct.c_int64] + [ct.c_void_p]*5
            lib.uvck_deform.restype = ct.c_int
            lib.uvck_uv_groups.argtypes = [ct.c_int64, ct.c_void_p, ct.c_void_p, ct.c_double, ct.c_void_p]
            lib.uvck_uv_groups.restype = ct.c_int
            lib.uvck_islands.argtypes = [ct.c_int64]*3 + [ct.c_void_p]*5 + [ct.c_double, ct.c_void_p]
            lib.uvck_islands.restype = ct.c_int
            lib.uvck_check_overlaps.argtypes = [ct.c_int64] + [ct.c_void_p]*5 + [ct.c_int64]*2 + [ct.c_void_p]
            lib.uvck_check_overlaps.restype = ct.c_int
            lib.uvck_intersections.argtypes = [ct.c_int64] + [ct.c_void_p]*3
            lib.uvck_intersections.restype = ct.c_int
            if hasattr(lib, 'uvck_triangle_frames'):
                lib.uvck_triangle_frames.argtypes = [ct.c_int64, ct.c_int64] + [ct.c_void_p]*4
                lib.uvck_triangle_frames.restype = ct.c_int
            _library = lib
        except (OSError, AttributeError, ValueError, KeyError, TypeError) as exc:
            _error = str(exc)
    return _library


def _fallback(reason):
    _fallbacks[reason] = _fallbacks.get(reason, 0) + 1
    if _required and _enabled:
        raise RuntimeError('UV Constraint Kit CPU engine: ' + reason)
    return None


def supports_contact(triangles=0):
    """Only the checked CPU engine advertises the internal contact objective."""
    # Keep large solves on the established GPU route until a GPU contact
    # objective has its own quality and complete-operation timing evidence.
    if triangles >= _GPU_MIN_TRIANGLES:
        return False
    lib = _load()
    if lib is None:
        return False
    try:
        capability = lib.uvck_contact_capability
    except AttributeError:
        return False
    capability.argtypes = []
    capability.restype = ct.c_int64
    return capability() == 1


def _gpu_asset(folder, identity, key, prefix, suffix):
    filename = identity[key]
    if (not isinstance(filename, str) or Path(filename).name != filename
            or not filename.startswith(prefix) or not filename.endswith(suffix)):
        raise OSError('invalid GPU asset identity')
    path = (folder / filename).resolve()
    if path.parent != folder.resolve():
        raise OSError('GPU asset outside engine folder')
    if hashlib.sha256(path.read_bytes()).hexdigest() != identity[key + '_sha256']:
        raise OSError('GPU asset checksum mismatch: ' + key)
    return path


def _load_gpu():
    """Called under _gpu_lock, only for eligible solves; CUDA stays optional."""
    global _gpu_library, _gpu_attempted, _gpu_disabled, _gpu_reason, _gpu_fallbacks
    if _gpu_disabled:
        return None
    if not _gpu_attempted:
        _gpu_attempted = True
        lib = None
        try:
            folder = Path(__file__).resolve().parent / 'bin' / 'windows-x64'
            identity = json.loads((folder / 'gpu.json').read_text(encoding='utf8'))
            if identity['abi'] != 1:
                raise OSError('invalid GPU engine ABI identity')
            path = _gpu_asset(folder, identity, 'library', 'uvck_gpu_', '.dll')
            ptx = _gpu_asset(folder, identity, 'ptx', 'uvck_gpu_', '.ptx')
            lib = ct.CDLL(str(path), winmode=0x1100)
            lib.uvck_abi.argtypes = [ct.c_int64, ct.c_int64]
            lib.uvck_abi.restype = ct.c_int64
            if lib.uvck_abi(ct.sizeof(_Input), ct.sizeof(_Output)) != 1:
                raise OSError('GPU engine ABI mismatch')
            lib.uvck_solve.argtypes = [ct.POINTER(_Input), ct.c_void_p, ct.POINTER(_Output)]
            lib.uvck_solve.restype = ct.c_int
            lib.uvck_gpu_configure.argtypes = [ct.c_char_p, ct.c_int64]
            lib.uvck_gpu_configure.restype = ct.c_int
            lib.uvck_gpu_stats.argtypes = [ct.POINTER(ct.c_int64), ct.c_int64, ct.c_int64]
            lib.uvck_gpu_stats.restype = ct.c_int
            lib.uvck_gpu_error.argtypes = [ct.c_void_p, ct.c_int64]
            lib.uvck_gpu_error.restype = ct.c_int
            lib.uvck_gpu_shutdown.argtypes = []
            lib.uvck_gpu_shutdown.restype = ct.c_int
            # Required GPU mode: errors escape this engine so the separate,
            # original CPU engine can restart from the original input.
            if lib.uvck_gpu_configure(str(ptx).encode('utf8'), 1):
                raise OSError('GPU engine configuration failed')
            _gpu_library = lib
        except Exception as exc:
            _gpu_reason = str(exc)
            _gpu_disabled = True
            _gpu_fallbacks += 1
            if lib is not None:
                try:
                    lib.uvck_gpu_shutdown()
                except Exception:
                    pass
    return _gpu_library


def shutdown_gpu():
    """Explicit normal teardown; never rely on CUDA work in a DLL destructor."""
    global _gpu_library, _gpu_attempted, _gpu_disabled, _gpu_reason
    with _gpu_lock:
        if _gpu_library is not None:
            try:
                if _gpu_library.uvck_gpu_shutdown():
                    raise OSError('GPU shutdown failed')
            except Exception as exc:
                _gpu_disabled = True
                _gpu_reason = str(exc)
            _gpu_library = None
        # Clean disable/re-enable can load again. A failed device stays disabled
        # for this module's lifetime, including add-on unregister/register.
        if not _gpu_disabled:
            _gpu_attempted = False


atexit.register(shutdown_gpu)


def _try_gpu(args, buffers):
    global _gpu_disabled, _gpu_reason, _gpu_calls, _gpu_fallbacks, _last_backend
    # The bundled CUDA objective does not implement the CPU contact barrier.
    if args.prevent_overlap == 2 or args.nt < _GPU_MIN_TRIANGLES or (not args.automatic and args.iterations < 50):
        return None
    with _gpu_lock:
        lib = _load_gpu()
        if lib is None:
            return None
        # A failing native implementation cannot poison CPU's restart UV input.
        gpu_uv = buffers[0].copy()
        gpu_args = _Input.from_buffer_copy(args)
        gpu_args.uv = gpu_uv.ctypes.data
        output = np.empty_like(gpu_uv)
        stats = _Output()
        _gpu_calls += 1
        try:
            status = lib.uvck_solve(ct.byref(gpu_args), output.ctypes.data, ct.byref(stats))
            if status not in (0, 1):
                detail = ct.create_string_buffer(1024)
                lib.uvck_gpu_error(detail, len(detail))
                raise OSError('GPU solve status %s: %s' % (status, detail.value.decode('utf8', 'replace')))
            if status == 0 and not np.isfinite(output).all():
                raise OSError('GPU returned nonfinite UVs')
            _last_backend = 'GPU'
            return status, output, stats
        except Exception as exc:
            _gpu_disabled = True
            _gpu_reason = str(exc)
            _gpu_fallbacks += 1
            shutdown_gpu()
            return None


def _gpu_diagnostics(reset=False):
    global _gpu_calls, _gpu_fallbacks
    with _gpu_lock:
        driver = None
        if _gpu_library is not None:
            values = (ct.c_int64 * 7)()
            if _gpu_library.uvck_gpu_stats(values, 7, reset) == 0:
                driver = dict(zip(('sessions', 'energy', 'gradient', 'errors',
                                   'upload_bytes', 'download_bytes', 'initializations'), values))
        result = dict(attempted=_gpu_attempted, loaded=_gpu_library is not None,
                      disabled=_gpu_disabled, reason=_gpu_reason, calls=_gpu_calls,
                      fallbacks=_gpu_fallbacks, last_backend=_last_backend,
                      minimum_triangles=_GPU_MIN_TRIANGLES, driver=driver)
        if reset:
            _gpu_calls = _gpu_fallbacks = 0
        return result


def diagnostics(reset=False):
    result = dict(loaded=_library is not None, enabled=_enabled, error=_error, abi=1,
                  library=getattr(_library, '_name', None),
                  calls=sum(_calls.values()), solve_calls=_calls['solve'],
                  deform_calls=_calls['deform'], topology_calls=_calls['uv_groups'],
                  island_calls=_calls['islands'], overlap_calls=_calls['overlaps'],
                  intersection_calls=_calls['intersections'],
                  frame_calls=_calls['frames'],
                  fallbacks=sum(_fallbacks.values()), reasons=dict(_fallbacks),
                  last_solve=dict(_last_solve), last_overlap=dict(_last_overlap),
                  gpu=_gpu_diagnostics(reset))
    if reset:
        for key in _calls:
            _calls[key] = 0
        _fallbacks.clear()
        _last_solve.clear()
        _last_overlap.clear()
    return result


def _array(value, dtype=np.float64):
    return np.ascontiguousarray(value, dtype=dtype)


def _input(x, tri, inverse, weights, arrays, held, penalties, boundary,
           density, angle_length, strength, penalty_scale, overlap_space,
           iterations, automatic, prevent_flips, prevent_overlap):
    x = _array(x)
    n = len(x)
    kinds, offsets, members = [], [0], []
    for kind, ids in arrays:
        kinds.append(kind)
        members.extend(map(int, ids))
        offsets.append(len(members))
    fixed = np.zeros(n, dtype=np.uint8)
    anchors = np.zeros((n, 2), dtype=np.float64)
    for i, uv in held.items():
        fixed[i] = 1
        anchors[i] = uv
    axes, edges, edge_weights = [], [], []
    for axis, pairs, values in penalties:
        axes.extend([axis]*len(pairs))
        edges.extend(pairs)
        edge_weights.extend(values)
    buffers = [x, _array(inverse), _array(weights), _array(tri, np.int64),
               _array(kinds, np.int64), _array(offsets, np.int64),
               _array(members, np.int64), fixed, anchors,
               _array(axes, np.int64), _array(edges, np.int64),
               _array(edge_weights), _array(boundary, np.int64)]
    if (x.shape != (n, 2) or buffers[1].shape != (len(tri), 2, 2)
            or buffers[2].shape != (len(tri),) or buffers[3].shape != (len(tri), 3)):
        raise ValueError('Invalid CPU engine geometry buffer dimensions')
    if (buffers[10].size != 2*len(axes) or buffers[11].size != len(axes)
            or buffers[12].size != 2*len(boundary)):
        raise ValueError('Invalid CPU engine edge buffer dimensions')
    args = _Input(n, len(tri), len(kinds), len(members), len(axes), len(boundary),
                  *[a.ctypes.data for a in buffers], density, angle_length,
                  -1. if strength is None else strength, penalty_scale, overlap_space,
                  iterations, automatic, prevent_flips, prevent_overlap)
    return args, buffers  # buffers own every address until the native call returns.


def solve(x, tri, inverse, weights, arrays, held, penalties, boundary,
          density, angle_length, strength, penalty_scale, overlap_space,
          iterations, automatic, prevent_flips, prevent_overlap):
    global _last_backend
    lib = _load()
    if lib is None:
        return _fallback('disabled' if not _enabled else _error or 'unavailable')
    args, buffers = _input(x, tri, inverse, weights, arrays, held, penalties, boundary,
                           density, angle_length, strength, penalty_scale, overlap_space,
                           iterations, automatic, prevent_flips, prevent_overlap)
    result = _try_gpu(args, buffers)
    if result is None:
        output = np.empty_like(buffers[0])
        stats = _Output()
        status = lib.uvck_solve(ct.byref(args), output.ctypes.data, ct.byref(stats))
        _last_backend = 'CPU'
    else:
        status, output, stats = result
    if status not in (0, 1):
        return _fallback('solve_status_' + str(status))
    _calls['solve'] += 1
    _last_solve.update({key: (None if isinstance(getattr(stats, key), float)
                            and not math.isfinite(getattr(stats, key)) else getattr(stats, key))
                       for key, _ in stats._fields_})
    return status, output if status == 0 else None


def deform(edges, weights, degree, fixed, displacement):
    lib = _load()
    if lib is None:
        return _fallback('disabled' if not _enabled else _error or 'unavailable')
    buffers = [_array(edges, np.int64), _array(weights), _array(degree),
               _array(fixed, np.uint8), _array(displacement).copy()]
    n = len(displacement)
    if (buffers[0].shape != (len(edges), 2) or buffers[1].shape != (len(edges),)
            or buffers[2].shape != (n,) or buffers[3].shape != (n,)
            or buffers[4].shape != (n, 2)):
        raise ValueError('Invalid CPU engine deformation buffer dimensions')
    status = lib.uvck_deform(len(displacement), len(edges), *[a.ctypes.data for a in buffers])
    if status:
        return _fallback('deform_status_' + str(status))
    _calls['deform'] += 1
    return buffers[-1]


def uv_groups(vertices, coordinates, epsilon):
    lib = _load()
    if lib is None:
        return _fallback('disabled' if not _enabled else _error or 'unavailable')
    vertices, coordinates = _array(vertices, np.int64), _array(coordinates)
    if vertices.ndim != 1 or coordinates.size != 2*len(vertices):
        raise ValueError('Invalid CPU engine topology buffer dimensions')
    output = np.empty(len(vertices), dtype=np.int64)
    status = lib.uvck_uv_groups(len(vertices), vertices.ctypes.data, coordinates.ctypes.data,
                                epsilon, output.ctypes.data)
    if status:
        return _fallback('uv_groups_status_' + str(status))
    _calls['uv_groups'] += 1
    return output.tolist()


def islands(nfaces, offsets, members, faces, following, coordinates, epsilon):
    lib = _load()
    if lib is None:
        return _fallback('disabled' if not _enabled else _error or 'unavailable')
    buffers = [_array(offsets, np.int64), _array(members, np.int64),
               _array(faces, np.int64), _array(following, np.int64), _array(coordinates)]
    nl = len(faces)
    if (buffers[2].shape != (nl,) or buffers[3].shape != (nl,)
            or buffers[4].size != 2*nl or len(members) != nl or not len(offsets)):
        raise ValueError('Invalid CPU engine island buffer dimensions')
    output = np.empty(nfaces, dtype=np.int64)
    status = lib.uvck_islands(nfaces, nl, len(offsets)-1,
                              *[a.ctypes.data for a in buffers], epsilon, output.ctypes.data)
    if status:
        return _fallback('islands_status_' + str(status))
    _calls['islands'] += 1
    return output.tolist()


def overlaps(records, reference, check_other_islands=False, include_existing=False):
    lib = _load()
    if lib is None:
        return _fallback('disabled' if not _enabled else _error or 'unavailable')
    n = len(records)
    buffers = [_array([r[0] for r in records], np.int64),
               _array([r[2] for r in records], np.int64),
               _array([r[4] for r in records], np.uint8),
               _array([r[3] for r in records]).reshape(n, 3, 2),
               _array([reference.get(r[1], r[3]) for r in records]).reshape(n, 3, 2)]
    output = np.zeros(2, dtype=np.int64)
    status = lib.uvck_check_overlaps(n, *[a.ctypes.data for a in buffers],
                                   check_other_islands, include_existing, output.ctypes.data)
    if status:
        return _fallback('overlap_status_' + str(status))
    _calls['overlaps'] += 1
    _last_overlap.update(triangles=n, tested_pairs=int(output[1]), kind=int(output[0]))
    if output[0] == 3:
        return _fallback('ambiguous_overlap_order')
    return int(output[0]), int(output[1])


def self_overlap_witness(coordinates, face_ids):
    """Find one same-chart overlap using reusable ABI-1 numeric buffers.

    Return (0, -1, -1) for a complete clean check, (1, i, j) for an actual
    intersecting pair, or None when the native checker is unavailable. This
    uses the same predicate and polygon exclusion as overlaps().
    """
    uv = _array(coordinates)
    faces = _array(face_ids, np.int64)
    if uv.ndim != 3 or uv.shape[1:] != (3, 2) or faces.shape != (len(uv),):
        raise ValueError('expected triangles[n,3,2] and faces[n]')
    if not np.all(np.isfinite(uv)):
        return _fallback('overlap_witness_nonfinite')
    n = len(faces)
    if not n:
        return 0, -1, -1
    lib = _load()
    if lib is None:
        return _fallback('disabled' if not _enabled else _error or 'unavailable')
    roots = np.zeros(n, dtype=np.int64)
    flags = np.ones(n, dtype=np.uint8)
    output = np.zeros(2, dtype=np.int64)

    def query(active, subset=None):
        flags.fill(0)
        flags[active] = 1
        if subset is None:
            f, r, changed, current = faces, roots, flags, uv
        else:
            f, r, changed, current = [np.ascontiguousarray(a[subset])
                                      for a in (faces, roots, flags, uv)]
        status = lib.uvck_check_overlaps(len(f), f.ctypes.data, r.ctypes.data,
            changed.ctypes.data, current.ctypes.data, current.ctypes.data,
            0, 1, output.ctypes.data)
        if status or int(output[0]) not in (0, 1):
            return _fallback('overlap_witness_status_' + str(status or int(output[0])))
        _calls['overlaps'] += 1
        _last_overlap.update(triangles=len(f), tested_pairs=int(output[1]), kind=int(output[0]))
        return int(output[0])

    active = np.arange(n, dtype=np.int64)
    kind = query(active)
    if kind is None:
        return None
    if kind == 0:
        return 0, -1, -1
    while len(active) > 1:
        middle = len(active)//2
        left = active[:middle]
        kind = query(left)
        if kind is None:
            return None
        active = left if kind else active[middle:]
    first = int(active[0])
    others = np.delete(np.arange(n, dtype=np.int64), first)
    while len(others) > 1:
        middle = len(others)//2
        left = others[:middle]
        kind = query([first], np.concatenate(([first], left)))
        if kind is None:
            return None
        others = left if kind else others[middle:]
    if not len(others):
        return _fallback('overlap_witness_missing_partner')
    second = int(others[0])
    if query([first], np.asarray([first, second], dtype=np.int64)) != 1:
        return _fallback('overlap_witness_confirmation')
    return 1, first, second


def intersections(a, b):
    """Batch entry point also used by independent numeric acceptance checks."""
    lib = _load()
    if lib is None:
        return _fallback('disabled' if not _enabled else _error or 'unavailable')
    a, b = _array(a), _array(b)
    if a.ndim != 3 or a.shape[1:] != (3, 2) or a.shape != b.shape:
        raise ValueError('Invalid CPU engine triangle buffer dimensions')
    output = np.empty(len(a), dtype=np.float64)
    status = lib.uvck_intersections(len(a), a.ctypes.data, b.ctypes.data, output.ctypes.data)
    if status:
        return _fallback('intersections_status_' + str(status))
    _calls['intersections'] += 1
    return output


def triangle_frames(points, triangles):
    """Optional read-only geometry batch, with the old Python path as fallback."""
    lib = _load()
    if lib is None or not hasattr(lib, 'uvck_triangle_frames'):
        return None
    xyz = _array(points, np.float32)
    ids = _array(triangles, np.int64)
    if xyz.ndim != 2 or xyz.shape[1] != 3 or ids.ndim != 2 or ids.shape[1] != 3:
        raise ValueError('Invalid native triangle-frame buffer dimensions')
    # Damaged nonfinite inputs retain the original Python warning/error path.
    if not np.isfinite(xyz).all():
        return None
    frames = np.empty((len(ids), 3, 2), dtype=np.float64)
    eligible = np.empty(len(ids), dtype=np.uint8)
    status = lib.uvck_triangle_frames(len(xyz), len(ids), xyz.ctypes.data,
                                    ids.ctypes.data, frames.ctypes.data, eligible.ctypes.data)
    if status:
        return _fallback('triangle_frames_status_' + str(status))
    _calls['frames'] += 1
    if not np.isfinite(frames).all():
        return None
    return frames, eligible.astype(bool)
