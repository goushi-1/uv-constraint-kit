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


def diagnostics(reset=False):
    global _captures, _reads
    value = dict(attempted=_attempted, loaded=_module is not None, error=_error,
                 captures=_captures, reads=_reads)
    if reset:
        _captures = _reads = 0
    return value
