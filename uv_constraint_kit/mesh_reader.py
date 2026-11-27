# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional read-only stable-ABI BMesh reader; no cached mesh wrappers."""
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import sys
import sysconfig
import threading
import numpy as np

_enabled = True
_required = False
_module = None
_attempted = False
_error = None
_captures = 0
_reads = 0
_writes = 0
_loop_collects = 0
_record_reads = 0
_area_reads = 0


def _load():
    global _module, _attempted, _error
    if not _enabled:
        return None
    if not _attempted:
        _attempted = True
        try:
            if (platform.system() != 'Windows' or platform.machine().upper() not in ('AMD64', 'X86_64')
                    or sys.implementation.name != 'cpython' or sys.version_info < (3, 11)
                    or sysconfig.get_config_var('Py_GIL_DISABLED')):
                raise ImportError('no bundled reader for this interpreter')
            folder = Path(__file__).resolve().parent / 'bin' / 'windows-x64'
            identity = json.loads((folder / 'reader.json').read_text(encoding='utf8'))
            filename = identity['library']
            if (identity['api'] != 2 or Path(filename).name != filename
                    or not filename.startswith('uvck_reader_') or not filename.endswith('.pyd')):
                raise ImportError('invalid reader identity')
            path = folder / filename
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != identity['sha256']:
                raise ImportError('reader checksum mismatch')
            fullname = __package__ + '._uvck_reader'
            spec = importlib.util.spec_from_file_location(fullname, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if module.version() != 2:
                raise ImportError('reader API mismatch')
            sys.modules[fullname] = module
            _module = module
        except MemoryError:
            _attempted = False
            raise
        except Exception as exc:
            # Optional loading/self-check failure only. Errors from capture()
            # and read_uv() are deliberately outside this fallback boundary.
            _error = str(exc)
    if _module is None and _required:
        raise RuntimeError('native mesh reader required: ' + str(_error))
    return _module


def _main_thread():
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('BMesh capture requires the main thread')


def capture(bm, layer):
    global _captures
    module = _load()
    if module is None:
        return None
    _main_thread()
    value = module.capture(bm, layer)
    loops, positions, uv, sizes, vertices, offsets, members = value
    value = (loops, positions, np.frombuffer(uv, dtype=np.float64).reshape(-1, 2),
             np.frombuffer(sizes, dtype=np.int64), np.frombuffer(vertices, dtype=np.int64),
             np.frombuffer(offsets, dtype=np.int64), np.frombuffer(members, dtype=np.int64))
    _captures += 1
    return value


def read_uv(loops, layer):
    global _reads
    module = _load()
    if module is None:
        return [tuple(loop[layer].uv) for loop in loops]
    _main_thread()
    value = np.frombuffer(module.read_uv(loops, layer), dtype=np.float64).reshape(-1, 2)
    _reads += 1
    return value


def write_uv(loops, layer, uv):
    """Batch-write UV pairs onto a live loop sequence."""
    global _writes
    module = _load()
    function = getattr(module, 'write_uv', None)
    if function is None:
        rows = uv.tolist() if isinstance(uv, np.ndarray) else uv
        for loop, xy in zip(loops, rows):
            loop[layer].uv = xy
        return
    _main_thread()
    arr = np.ascontiguousarray(uv, dtype=np.float64).reshape(-1, 2)
    function(loops, layer, arr.tobytes())
    _writes += 1


def write_uv_indexed(loops, layer, positions, uv):
    """Batch-write UV pairs onto indexed slots of a live loop sequence."""
    global _writes
    pos_arr = np.ascontiguousarray(positions, dtype=np.int64).reshape(-1)
    if not len(pos_arr):
        return
    uv_arr = np.ascontiguousarray(uv, dtype=np.float64).reshape(-1, 2)
    module = _load()
    function = getattr(module, 'write_uv_indexed', None)
    if function is None:
        for p, xy in zip(pos_arr.tolist(), uv_arr.tolist()):
            loops[p][layer].uv = xy
        return
    _main_thread()
    function(loops, layer, pos_arr.tobytes(), uv_arr.tobytes())
    _writes += 1


def read_pins(loops, layer):
    """Read pin_uv flags into a compact bytes mask (0/1 per loop)."""
    module = _load()
    function = getattr(module, 'read_pins', None)
    if function is None:
        return bytes(1 if loop[layer].pin_uv else 0 for loop in loops)
    _main_thread()
    return function(loops, layer)


def write_pins(loops, layer, pins):
    """Write pin_uv flags from a bytes mask, ndarray, or iterable of bools."""
    if isinstance(pins, bytes):
        mask = pins
    elif isinstance(pins, bytearray):
        mask = bytes(pins)
    elif isinstance(pins, np.ndarray):
        mask = np.ascontiguousarray(pins, dtype=np.bool_).tobytes()
    else:
        mask = bytes(1 if p else 0 for p in pins)
    module = _load()
    function = getattr(module, 'write_pins', None)
    if function is None:
        for loop, flag in zip(loops, mask):
            loop[layer].pin_uv = bool(flag)
        return
    _main_thread()
    function(loops, layer, mask)


def read_uv_select(loops):
    """Read uv_select_edge/uv_select_vert flags into a compact bytes mask."""
    module = _load()
    function = getattr(module, 'read_uv_select', None)
    if function is None:
        return bytes((1 if loop.uv_select_edge else 0) | (2 if loop.uv_select_vert else 0) for loop in loops)
    _main_thread()
    return function(loops)


def write_uv_select(loops, mask):
    """Write uv_select_edge/uv_select_vert flags from a compact bytes mask."""
    raw = bytes(mask) if not isinstance(mask, bytes) else mask
    module = _load()
    function = getattr(module, 'write_uv_select', None)
    if function is None:
        for pos, loop in enumerate(loops):
            if pos < len(raw):
                b = raw[pos]
                loop.uv_select_edge = bool(b & 1)
                loop.uv_select_vert = bool(b & 2)
        return
    _main_thread()
    function(loops, raw)


def set_uv_select_all(loops, layer, vert_flag, edge_flag, pin_mode=-1):
    """Bulk-set uv_select_vert, uv_select_edge, and optionally pin_uv (-1=leave)."""
    module = _load()
    function = getattr(module, 'set_uv_select_all', None)
    if function is None:
        vf, ef = bool(vert_flag), bool(edge_flag)
        if pin_mode >= 0:
            pf = bool(pin_mode)
            for loop in loops:
                loop.uv_select_vert = vf
                loop.uv_select_edge = ef
                loop[layer].pin_uv = pf
        else:
            for loop in loops:
                loop.uv_select_vert = vf
                loop.uv_select_edge = ef
        return
    _main_thread()
    function(loops, layer, int(bool(vert_flag)), int(bool(edge_flag)), int(pin_mode))


def constrained_loops(bm, layer):
    """Scan face-major constrained (position, face_index) flat list, or None."""
    module = _load()
    function = getattr(module, 'constrained_loops', None)
    if function is None:
        return None
    _main_thread()
    return function(bm, layer)


def collect_loops(bm):
    """Fresh face-major handles, or None when the optional method is absent."""
    global _loop_collects
    module = _load()
    function = getattr(module, 'collect_loops', None)
    if function is None:
        return None
    _main_thread()
    value = function(bm)
    _loop_collects += 1
    return value


def constraint_records(bm, layer):
    """Read current marks without retaining handles or updating mesh indices."""
    global _record_reads
    module = _load()
    function = getattr(module, 'constraint_records', None)
    if function is None:
        return None
    _main_thread()
    value = function(bm, layer)
    _record_reads += 1
    return value


def island_area(faces, layer):
    """Current ordered face areas, or None if the optional method is absent."""
    global _area_reads
    module = _load()
    function = getattr(module, 'island_area', None)
    if function is None:
        return None
    _main_thread()
    value = function(faces, layer)
    _area_reads += 1
    return value


def diagnostics(reset=False):
    global _captures, _reads, _writes, _loop_collects, _record_reads, _area_reads
    value = dict(attempted=_attempted, loaded=_module is not None, error=_error,
                 captures=_captures, reads=_reads, writes=_writes,
                 loop_collects=_loop_collects, record_reads=_record_reads,
                 area_reads=_area_reads)
    if reset:
        _captures = _reads = _writes = _loop_collects = _record_reads = _area_reads = 0
    return value
