# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional numeric-trial area sidecar; fresh metadata, owned output only."""
import ctypes as ct
import hashlib
import json
import math
from pathlib import Path
import platform
import sys
import numpy as np

_enabled=True
_attempted=False
_library=None
_error=None
_calls=_scaled=_noop=_rejected=0
_read_calls=_read_successes=_read_rejected=0
_DEFAULT_ERR=dict(divide='warn',over='warn',under='ignore',invalid='warn')


def _load():
    global _attempted,_library,_error
    if not _enabled:
        return None
    if not _attempted:
        _attempted=True
        try:
            if (sys.implementation.name!='cpython' or platform.system()!='Windows'
                or platform.machine().upper() not in ('AMD64','X86_64')):
                raise OSError('unsupported numeric-stage platform')
            folder=Path(__file__).resolve().parent/'bin'/'windows-x64'
            identity=json.loads((folder/'numeric_stage.json').read_text(encoding='utf8'))
            name=identity['library']
            if (identity['abi']!=1 or Path(name).name!=name or not name.startswith('uvck_numeric_')
                or not name.endswith('.dll') or list(sys.version_info[:3])!=identity['python_version']
                or np.__version__!=identity['numpy_version'] or identity.get('area_read_abi')!=1):
                raise OSError('unsupported numeric-stage identity/interpreter')
            filename=folder/name
            if filename.is_symlink() or hashlib.sha256(filename.read_bytes()).hexdigest()!=identity['sha256']:
                raise OSError('numeric-stage checksum mismatch')
            lib=ct.CDLL(str(filename))
            lib.uvck_numeric_stage_abi.argtypes=[]
            lib.uvck_numeric_stage_abi.restype=ct.c_int
            if lib.uvck_numeric_stage_abi()!=1:
                raise OSError('numeric-stage ABI mismatch')
            lib.uvck_numeric_area_scale.argtypes=[ct.c_int64]*3+[ct.c_void_p]*3+[ct.c_double]+[ct.c_void_p]*2
            lib.uvck_numeric_area_scale.restype=ct.c_int
            lib.uvck_numeric_area_read.argtypes=[ct.c_int64]*3+[ct.c_void_p]*4
            lib.uvck_numeric_area_read.restype=ct.c_int
            _library=lib
        except MemoryError:
            _attempted=False
            raise
        except Exception as exc:
            _error=type(exc).__name__+': '+str(exc)
    return _library


def diagnostics():
    return dict(enabled=_enabled,attempted=_attempted,loaded=_library is not None,
        error=_error,calls=_calls,scaled=_scaled,noop=_noop,rejected=_rejected,
        read_calls=_read_calls,read_successes=_read_successes,read_rejected=_read_rejected)


def scaled_copy(topo,faces,target,coordinates):
    """None requests original fallback; a returned array owns a valid trial.

    No coordinate, face, topology or BMesh wrapper is cached. Scatter touches
    only an unshared copy, so a sidecar rejection cannot alter the input.
    """
    global _calls,_scaled,_noop,_rejected,_error
    if (not _enabled or np.geterr()!=_DEFAULT_ERR or type(coordinates) is not np.ndarray
        or coordinates.dtype!=np.dtype('float64') or coordinates.ndim!=2 or coordinates.shape[1]!=2
        or not coordinates.flags.c_contiguous or not coordinates.flags.aligned
        or not coordinates.flags.writeable or type(target) not in (float,np.float64)):
        return None
    lib=_load()
    if lib is None:
        return None
    try:
        offsets=[0]
        positions=[]
        for fi in sorted(faces):
            positions.extend(topo.loop_pos[loop] for loop in topo.bm.faces[fi].loops)
            offsets.append(len(positions))
        offsets=np.asarray(offsets,dtype=np.int64)
        members=np.asarray(positions,dtype=np.int64)
        rows=np.empty((len(members),2),dtype=np.float64)
        stats=np.empty(4,dtype=np.float64)
        _calls+=1
        status=lib.uvck_numeric_area_scale(len(coordinates),len(offsets)-1,len(members),
            coordinates.ctypes.data,offsets.ctypes.data,members.ctypes.data,float(target),
            rows.ctypes.data,stats.ctypes.data)
        if status==2:
            _rejected+=1
            return None
        if status not in (0,1):
            raise ValueError('invalid numeric-stage return status')
        if status==1:
            if not np.isfinite(stats).all() or stats[0]<0.0 or stats[1]!=1.0:
                raise ValueError('invalid numeric-stage no-op factor')
            if target>0.0 and stats[0]>0.0 and len(members):
                expected=math.sqrt(float(target)/float(stats[0]))
                if 1e-3<expected<1e3 and abs(expected-1.0)>=1e-6:
                    raise ValueError('inconsistent numeric-stage no-op')
            _noop+=1
            return coordinates
        if (not np.isfinite(rows).all() or not np.isfinite(stats).all()
            or not (1e-3<float(stats[1])<1e3)
            or np.any(members<0) or np.any(members>=len(coordinates))
            or len(np.unique(members))!=len(members)):
            raise ValueError('invalid numeric-stage output')
        magnitude=np.abs(rows)
        if (stats[0]<=0.0 or abs(float(stats[1])-1.0)<1e-6
            or float(stats[1])!=math.sqrt(float(target)/float(stats[0]))
            or np.any(magnitude>float(np.finfo(np.float32).max))
            or np.any((magnitude>0.0)&(magnitude<float(np.finfo(np.float32).tiny)))):
            raise ValueError('numeric-stage range or factor mismatch')
        rounded=rows.astype(np.float32).astype(np.float64)
        if not np.array_equal(rows.view(np.uint64),rounded.view(np.uint64)):
            raise ValueError('numeric-stage output is not float32 exact')
        # A second owned array keeps all original bytes intact on any exception.
        result=coordinates.copy()
        result[members]=rows
        _scaled+=1
        return result
    except MemoryError:
        raise
    except Exception as exc:
        _rejected+=1
        _error=type(exc).__name__+': '+str(exc)
        return None


def read_area(topo,faces,coordinates):
    """Ordered numeric area or None; no live capture, writes or metadata cache."""
    global _read_calls,_read_successes,_read_rejected,_error
    if (not _enabled or np.geterr()!=_DEFAULT_ERR or type(coordinates) is not np.ndarray
        or coordinates.dtype!=np.dtype('float64') or coordinates.ndim!=2 or coordinates.shape[1]!=2
        or not coordinates.flags.c_contiguous or not coordinates.flags.aligned):
        return None
    lib=_load()
    if lib is None:
        return None
    try:
        offsets,positions=[0],[]
        for fi in sorted(faces):
            positions.extend(topo.loop_pos[loop] for loop in topo.bm.faces[fi].loops)
            offsets.append(len(positions))
        offsets=np.asarray(offsets,dtype=np.int64)
        members=np.asarray(positions,dtype=np.int64)
        output=ct.c_double()
        _read_calls+=1
        status=lib.uvck_numeric_area_read(len(coordinates),len(offsets)-1,len(members),
            coordinates.ctypes.data,offsets.ctypes.data,members.ctypes.data,ct.byref(output))
        if status==2:
            _read_rejected+=1
            return None
        if status!=0 or not math.isfinite(output.value) or output.value<0.0:
            raise ValueError('invalid numeric area-read result')
        _read_successes+=1
        # Match island_area's scalar type for float64-array coordinates.
        return np.float64(output.value) if len(members) else float(output.value)
    except MemoryError:
        raise
    except Exception as exc:
        _read_rejected+=1
        _error=type(exc).__name__+': '+str(exc)
        return None
