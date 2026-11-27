// SPDX-License-Identifier: GPL-3.0-or-later
// CPython stable-ABI BMesh batch adapter. No Blender struct layouts.
#define Py_LIMITED_API 0x030B0000
#include <Python.h>
#include <memory>
#include <new>
#include <array>
#include <cstdint>
#include <cstring>
#include <vector>
#include <cmath>

namespace {
struct Release { void operator()(PyObject* value) const { Py_XDECREF(value); } };
using Ref = std::unique_ptr<PyObject, Release>;
struct PythonError {};
Ref checked(PyObject* value) {
    if (!value) throw PythonError{};
    return Ref(value);
}
void appended(PyObject* list, PyObject* value) {
    if (PyList_Append(list, value) < 0) throw PythonError{};
}
template<class Function> void each(PyObject* sequence, Function function) {
    Ref iterator = checked(PyObject_GetIter(sequence));
    while (PyObject* item = PyIter_Next(iterator.get())) {
        Ref owned(item);
        function(item);
    }
    if (PyErr_Occurred()) throw PythonError{};
}
Ref name(const char* text) { return checked(PyUnicode_FromString(text)); }
Ref attr(PyObject* object, const Ref& key) {
    return checked(PyObject_GetAttr(object, key.get()));
}
std::array<double, 2> coordinate(PyObject* loop, PyObject* layer, const Ref& uv_name) {
    Ref slot = checked(PyObject_GetItem(loop, layer));
    Ref vector = attr(slot.get(), uv_name);
    Py_buffer view{};
    if (PyObject_GetBuffer(vector.get(), &view, PyBUF_FORMAT) == 0) {
        if (view.format && view.format[0] == 'f' && view.format[1] == '\0' &&
                view.len == static_cast<Py_ssize_t>(2 * sizeof(float))) {
            float xy[2];
            std::memcpy(xy, view.buf, sizeof(xy));
            PyBuffer_Release(&view);
            return {static_cast<double>(xy[0]), static_cast<double>(xy[1])};
        }
        if (view.format && view.format[0] == 'd' && view.format[1] == '\0' &&
                view.len == static_cast<Py_ssize_t>(2 * sizeof(double))) {
            double xy[2];
            std::memcpy(xy, view.buf, sizeof(xy));
            PyBuffer_Release(&view);
            return {xy[0], xy[1]};
        }
        PyBuffer_Release(&view);
    } else {
        PyErr_Clear();
    }
    if (PyObject_Length(vector.get()) != 2) {
        if (PyErr_Occurred()) throw PythonError{};
        PyErr_SetString(PyExc_ValueError, "expected two UV coordinates");
        throw PythonError{};
    }
    Ref u = checked(PySequence_GetItem(vector.get(), 0));
    Ref v = checked(PySequence_GetItem(vector.get(), 1));
    double x = PyFloat_AsDouble(u.get());
    if (PyErr_Occurred()) throw PythonError{};
    double y = PyFloat_AsDouble(v.get());
    if (PyErr_Occurred()) throw PythonError{};
    return {x, y};
}
template<class T> Ref bytes(const std::vector<T>& values) {
    if (values.size() > static_cast<size_t>(PY_SSIZE_T_MAX) / sizeof(T)) {
        PyErr_SetString(PyExc_OverflowError, "mesh capture buffer exceeds Python size limit");
        throw PythonError{};
    }
    return checked(PyBytes_FromStringAndSize(reinterpret_cast<const char*>(values.data()),
                    static_cast<Py_ssize_t>(values.size() * sizeof(T))));
}
PyObject* collect_loops(PyObject*, PyObject* bm) {
    try {
        Ref faces_name = name("faces"), loops_name = name("loops"), extend_name = name("extend");
        Ref loops = checked(PyList_New(0));
        Ref extend = attr(loops.get(), extend_name);
        Ref faces = attr(bm, faces_name);
        each(faces.get(), [&](PyObject* face) {
            Ref corners = attr(face, loops_name);
            Ref result = checked(PyObject_CallFunctionObjArgs(extend.get(), corners.get(), nullptr));
        });
        return loops.release();
    } catch (const PythonError&) { return nullptr; }
    catch (const std::bad_alloc&) { return PyErr_NoMemory(); }
    catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native loop collection failed");
        return nullptr;
    }
}
PyObject* constraint_records(PyObject*, PyObject* arguments) {
    PyObject *bm, *layer;
    if (!PyArg_ParseTuple(arguments, "OO:constraint_records", &bm, &layer)) return nullptr;
    try {
        Ref faces_name = name("faces"), loops_name = name("loops"), edge_name = name("edge");
        Ref vertices_name = name("verts"), index_name = name("index");
        Ref records = checked(PyList_New(0)), zero = checked(PyLong_FromLong(0));
        Ref faces = attr(bm, faces_name);
        Py_ssize_t position = 0;
        each(faces.get(), [&](PyObject* face) {
            Ref corners = attr(face, loops_name);
            each(corners.get(), [&](PyObject* loop) {
                Ref kind = checked(PyObject_GetItem(loop, layer));
                int unmarked = PyObject_RichCompareBool(kind.get(), zero.get(), Py_EQ);
                if (unmarked < 0) throw PythonError{};
                if (!unmarked) {
                    Ref edge = attr(loop, edge_name);
                    Ref vertices = attr(edge.get(), vertices_name);
                    Ref first = checked(PySequence_GetItem(vertices.get(), 0));
                    Ref second = checked(PySequence_GetItem(vertices.get(), 1));
                    Ref first_index = attr(first.get(), index_name);
                    Ref second_index = attr(second.get(), index_name);
                    Ref at = checked(PyLong_FromSsize_t(position));
                    appended(records.get(), at.get());
                    appended(records.get(), first_index.get());
                    appended(records.get(), second_index.get());
                    appended(records.get(), kind.get());
                }
                if (position == PY_SSIZE_T_MAX) {
                    PyErr_SetString(PyExc_OverflowError, "mesh loop position exceeds Python size limit");
                    throw PythonError{};
                }
                ++position;
            });
        });
        return records.release();
    } catch (const PythonError&) { return nullptr; }
    catch (const std::bad_alloc&) { return PyErr_NoMemory(); }
    catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native constraint record read failed");
        return nullptr;
    }
}
PyObject* capture(PyObject*, PyObject* arguments) {
    PyObject *bm, *layer;
    if (!PyArg_ParseTuple(arguments, "OO:capture", &bm, &layer)) return nullptr;
    try {
        Ref faces_name = name("faces"), edges_name = name("edges"), loops_name = name("loops");
        Ref vertex_name = name("vert"), index_name = name("index"), uv_name = name("uv");
        Ref radial_name = name("link_loops");
        Ref loops = checked(PyList_New(0)), positions = checked(PyDict_New());
        std::vector<double> coordinates;
        std::vector<std::int64_t> sizes, vertices, offsets{0}, members;
        Ref faces = attr(bm, faces_name);
        Py_ssize_t nf_hint = PyObject_Length(faces.get());
        if (nf_hint > 0) {
            sizes.reserve(static_cast<size_t>(nf_hint));
            coordinates.reserve(static_cast<size_t>(nf_hint) * 8);
            vertices.reserve(static_cast<size_t>(nf_hint) * 4);
        } else {
            PyErr_Clear();
        }
        Py_ssize_t position = 0;
        each(faces.get(), [&](PyObject* face) {
            Ref corners = attr(face, loops_name);
            Py_ssize_t length = PyObject_Length(corners.get());
            if (length < 0) throw PythonError{};
            sizes.push_back(length);
            each(corners.get(), [&](PyObject* loop) {
                auto point = coordinate(loop, layer, uv_name);
                Ref vertex = attr(loop, vertex_name);
                Ref index = attr(vertex.get(), index_name);
                long long vertex_index = PyLong_AsLongLong(index.get());
                if (PyErr_Occurred()) throw PythonError{};
                Ref at = checked(PyLong_FromSsize_t(position));
                appended(loops.get(), loop);
                if (PyDict_SetItem(positions.get(), loop, at.get()) < 0) throw PythonError{};
                coordinates.push_back(point[0]);coordinates.push_back(point[1]);
                vertices.push_back(vertex_index);
                ++position;
            });
        });
        Ref edges = attr(bm, edges_name);
        Py_ssize_t ne_hint = PyObject_Length(edges.get());
        if (ne_hint > 0) offsets.reserve(static_cast<size_t>(ne_hint) + 1);
        else PyErr_Clear();
        if (position > 0) members.reserve(static_cast<size_t>(position));
        Py_ssize_t radial_count = 0;
        each(edges.get(), [&](PyObject* edge) {
            Ref radial = attr(edge, radial_name);
            each(radial.get(), [&](PyObject* loop) {
                // Borrowed dictionary value; only its numeric position is copied.
                PyObject* at = PyDict_GetItemWithError(positions.get(), loop);
                if (!at) {
                    if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "radial loop absent from faces");
                    throw PythonError{};
                }
                long long member = PyLong_AsLongLong(at);
                if (PyErr_Occurred()) throw PythonError{};
                members.push_back(member);
                ++radial_count;
            });
            offsets.push_back(radial_count);
        });
        if (radial_count != position) {
            PyErr_SetString(PyExc_ValueError, "inconsistent BMesh radial loop count");
            throw PythonError{};
        }
        Ref uv_bytes = bytes(coordinates), size_bytes = bytes(sizes), vertex_bytes = bytes(vertices);
        Ref offset_bytes = bytes(offsets), member_bytes = bytes(members);
        return PyTuple_Pack(7, loops.get(), positions.get(), uv_bytes.get(), size_bytes.get(),
                            vertex_bytes.get(), offset_bytes.get(), member_bytes.get());
    } catch (const PythonError&) {
        return nullptr;
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native mesh capture failed");
        return nullptr;
    }
}
PyObject* read_uv(PyObject*, PyObject* arguments) {
    PyObject *loops, *layer;
    if (!PyArg_ParseTuple(arguments, "OO:read_uv", &loops, &layer)) return nullptr;
    try {
        Ref uv_name = name("uv");
        std::vector<double> coordinates;
        Py_ssize_t nl_hint = PyObject_Length(loops);
        if (nl_hint > 0) coordinates.reserve(static_cast<size_t>(nl_hint) * 2);
        else PyErr_Clear();
        each(loops, [&](PyObject* loop) {
            auto point = coordinate(loop, layer, uv_name);
            coordinates.push_back(point[0]);coordinates.push_back(point[1]);
        });
        return bytes(coordinates).release();
    } catch (const PythonError&) { return nullptr; }
    catch (const std::bad_alloc&) { return PyErr_NoMemory(); }
    catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native UV read failed");
        return nullptr;
    }
}

PyObject* write_uv(PyObject*, PyObject* arguments) {
    PyObject *loops, *layer, *uv_bytes;
    if (!PyArg_ParseTuple(arguments, "OOO:write_uv", &loops, &layer, &uv_bytes)) return nullptr;
    char* raw = nullptr;
    Py_ssize_t nbytes = 0;
    if (PyBytes_AsStringAndSize(uv_bytes, &raw, &nbytes) < 0) return nullptr;
    if (nbytes % static_cast<Py_ssize_t>(2 * sizeof(double)) != 0) {
        PyErr_SetString(PyExc_ValueError, "invalid UV byte buffer length");
        return nullptr;
    }
    const Py_ssize_t count = nbytes / static_cast<Py_ssize_t>(2 * sizeof(double));
    try {
        Ref uv_name = name("uv");
        Py_ssize_t idx = 0;
        each(loops, [&](PyObject* loop) {
            if (idx >= count) {
                PyErr_SetString(PyExc_ValueError, "loop count exceeds UV buffer");
                throw PythonError{};
            }
            double xy[2];
            std::memcpy(xy, raw + idx * 2 * sizeof(double), sizeof(xy));
            Ref slot = checked(PyObject_GetItem(loop, layer));
            Ref fu = checked(PyFloat_FromDouble(xy[0]));
            Ref fv = checked(PyFloat_FromDouble(xy[1]));
            Ref pair = checked(PyTuple_Pack(2, fu.get(), fv.get()));
            if (PyObject_SetAttr(slot.get(), uv_name.get(), pair.get()) < 0) throw PythonError{};
            ++idx;
        });
        if (idx != count) {
            PyErr_SetString(PyExc_ValueError, "UV buffer length exceeds loop count");
            return nullptr;
        }
        Py_RETURN_NONE;
    } catch (const PythonError&) { return nullptr; }
    catch (const std::bad_alloc&) { return PyErr_NoMemory(); }
    catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native UV write failed");
        return nullptr;
    }
}

PyObject* write_uv_indexed(PyObject*, PyObject* arguments) {
    PyObject *loops, *layer, *idx_bytes, *uv_bytes;
    if (!PyArg_ParseTuple(arguments, "OOOO:write_uv_indexed", &loops, &layer, &idx_bytes, &uv_bytes)) return nullptr;
    char *idx_raw = nullptr, *uv_raw = nullptr;
    Py_ssize_t idx_nbytes = 0, uv_nbytes = 0;
    if (PyBytes_AsStringAndSize(idx_bytes, &idx_raw, &idx_nbytes) < 0) return nullptr;
    if (PyBytes_AsStringAndSize(uv_bytes, &uv_raw, &uv_nbytes) < 0) return nullptr;
    if (idx_nbytes % static_cast<Py_ssize_t>(sizeof(std::int64_t)) != 0 ||
        uv_nbytes % static_cast<Py_ssize_t>(2 * sizeof(double)) != 0) {
        PyErr_SetString(PyExc_ValueError, "invalid indexed UV byte buffer length");
        return nullptr;
    }
    const Py_ssize_t count = idx_nbytes / static_cast<Py_ssize_t>(sizeof(std::int64_t));
    if (count != uv_nbytes / static_cast<Py_ssize_t>(2 * sizeof(double))) {
        PyErr_SetString(PyExc_ValueError, "index and UV buffer counts differ");
        return nullptr;
    }
    const Py_ssize_t total = PyObject_Length(loops);
    if (total < 0) return nullptr;
    try {
        Ref uv_name = name("uv");
        for (Py_ssize_t k = 0; k < count; ++k) {
            std::int64_t pos = 0;
            std::memcpy(&pos, idx_raw + k * sizeof(std::int64_t), sizeof(pos));
            if (pos < 0 || pos >= total) {
                PyErr_SetString(PyExc_IndexError, "loop index out of range");
                throw PythonError{};
            }
            Ref loop = checked(PySequence_GetItem(loops, static_cast<Py_ssize_t>(pos)));
            double xy[2];
            std::memcpy(xy, uv_raw + k * 2 * sizeof(double), sizeof(xy));
            Ref slot = checked(PyObject_GetItem(loop.get(), layer));
            Ref fu = checked(PyFloat_FromDouble(xy[0]));
            Ref fv = checked(PyFloat_FromDouble(xy[1]));
            Ref pair = checked(PyTuple_Pack(2, fu.get(), fv.get()));
            if (PyObject_SetAttr(slot.get(), uv_name.get(), pair.get()) < 0) throw PythonError{};
        }
        Py_RETURN_NONE;
    } catch (const PythonError&) { return nullptr; }
    catch (const std::bad_alloc&) { return PyErr_NoMemory(); }
    catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native indexed UV write failed");
        return nullptr;
    }
}

PyObject* read_pins(PyObject*, PyObject* arguments) {
    PyObject *loops, *layer;
    if (!PyArg_ParseTuple(arguments, "OO:read_pins", &loops, &layer)) return nullptr;
    try {
        Ref pin_name = name("pin_uv");
        std::vector<std::uint8_t> mask;
        Py_ssize_t nl_hint = PyObject_Length(loops);
        if (nl_hint > 0) mask.reserve(static_cast<size_t>(nl_hint));
        else PyErr_Clear();
        each(loops, [&](PyObject* loop) {
            Ref slot = checked(PyObject_GetItem(loop, layer));
            Ref flag = attr(slot.get(), pin_name);
            int truth = PyObject_IsTrue(flag.get());
            if (truth < 0) throw PythonError{};
            mask.push_back(static_cast<std::uint8_t>(truth ? 1 : 0));
        });
        return bytes(mask).release();
    } catch (const PythonError&) { return nullptr; }
    catch (const std::bad_alloc&) { return PyErr_NoMemory(); }
    catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native pin read failed");
        return nullptr;
    }
}

PyObject* write_pins(PyObject*, PyObject* arguments) {
    PyObject *loops, *layer, *mask_bytes;
    if (!PyArg_ParseTuple(arguments, "OOO:write_pins", &loops, &layer, &mask_bytes)) return nullptr;
    char* raw = nullptr;
    Py_ssize_t count = 0;
    if (PyBytes_AsStringAndSize(mask_bytes, &raw, &count) < 0) return nullptr;
    try {
        Ref pin_name = name("pin_uv");
        Py_ssize_t idx = 0;
        each(loops, [&](PyObject* loop) {
            if (idx >= count) {
                PyErr_SetString(PyExc_ValueError, "loop count exceeds pin mask");
                throw PythonError{};
            }
            Ref slot = checked(PyObject_GetItem(loop, layer));
            PyObject* val = raw[idx] ? Py_True : Py_False;
            if (PyObject_SetAttr(slot.get(), pin_name.get(), val) < 0) throw PythonError{};
            ++idx;
        });
        if (idx != count) {
            PyErr_SetString(PyExc_ValueError, "pin mask length exceeds loop count");
            return nullptr;
        }
        Py_RETURN_NONE;
    } catch (const PythonError&) { return nullptr; }
    catch (const std::bad_alloc&) { return PyErr_NoMemory(); }
    catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native pin write failed");
        return nullptr;
    }
}

PyObject* read_uv_select(PyObject*, PyObject* loops) {
    try {
        Ref edge_name = name("uv_select_edge"), vert_name = name("uv_select_vert");
        std::vector<std::uint8_t> mask;
        Py_ssize_t nl_hint = PyObject_Length(loops);
        if (nl_hint > 0) mask.reserve(static_cast<size_t>(nl_hint));
        else PyErr_Clear();
        each(loops, [&](PyObject* loop) {
            Ref fe = attr(loop, edge_name);
            int te = PyObject_IsTrue(fe.get());
            if (te < 0) throw PythonError{};
            Ref fv = attr(loop, vert_name);
            int tv = PyObject_IsTrue(fv.get());
            if (tv < 0) throw PythonError{};
            mask.push_back(static_cast<std::uint8_t>((te ? 1 : 0) | (tv ? 2 : 0)));
        });
        return bytes(mask).release();
    } catch (const PythonError&) { return nullptr; }
    catch (const std::bad_alloc&) { return PyErr_NoMemory(); }
    catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native UV selection read failed");
        return nullptr;
    }
}

PyObject* write_uv_select(PyObject*, PyObject* arguments) {
    PyObject *loops, *mask_bytes;
    if (!PyArg_ParseTuple(arguments, "OO:write_uv_select", &loops, &mask_bytes)) return nullptr;
    char* raw = nullptr;
    Py_ssize_t count = 0;
    if (PyBytes_AsStringAndSize(mask_bytes, &raw, &count) < 0) return nullptr;
    try {
        Ref edge_name = name("uv_select_edge"), vert_name = name("uv_select_vert");
        Py_ssize_t idx = 0;
        each(loops, [&](PyObject* loop) {
            if (idx < count) {
                const unsigned char b = static_cast<unsigned char>(raw[idx]);
                if (PyObject_SetAttr(loop, edge_name.get(), (b & 1) ? Py_True : Py_False) < 0) throw PythonError{};
                if (PyObject_SetAttr(loop, vert_name.get(), (b & 2) ? Py_True : Py_False) < 0) throw PythonError{};
            }
            ++idx;
        });
        Py_RETURN_NONE;
    } catch (const PythonError&) { return nullptr; }
    catch (const std::bad_alloc&) { return PyErr_NoMemory(); }
    catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native UV selection write failed");
        return nullptr;
    }
}

PyObject* set_uv_select_all(PyObject*, PyObject* arguments) {
    PyObject *loops, *layer;
    int vert_flag = 0, edge_flag = 0, pin_mode = -1;
    if (!PyArg_ParseTuple(arguments, "OOiii:set_uv_select_all", &loops, &layer, &vert_flag, &edge_flag, &pin_mode)) return nullptr;
    try {
        Ref vert_name = name("uv_select_vert"), edge_name = name("uv_select_edge");
        Ref pin_name = (pin_mode >= 0) ? name("pin_uv") : Ref(nullptr);
        PyObject* v_val = vert_flag ? Py_True : Py_False;
        PyObject* e_val = edge_flag ? Py_True : Py_False;
        PyObject* p_val = (pin_mode > 0) ? Py_True : Py_False;
        each(loops, [&](PyObject* loop) {
            if (PyObject_SetAttr(loop, vert_name.get(), v_val) < 0) throw PythonError{};
            if (PyObject_SetAttr(loop, edge_name.get(), e_val) < 0) throw PythonError{};
            if (pin_mode >= 0) {
                Ref slot = checked(PyObject_GetItem(loop, layer));
                if (PyObject_SetAttr(slot.get(), pin_name.get(), p_val) < 0) throw PythonError{};
            }
        });
        Py_RETURN_NONE;
    } catch (const PythonError&) { return nullptr; }
    catch (const std::bad_alloc&) { return PyErr_NoMemory(); }
    catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native UV bulk selection write failed");
        return nullptr;
    }
}

PyObject* constrained_loops(PyObject*, PyObject* arguments) {
    PyObject *bm, *layer;
    if (!PyArg_ParseTuple(arguments, "OO:constrained_loops", &bm, &layer)) return nullptr;
    try {
        Ref faces_name = name("faces"), loops_name = name("loops"), index_name = name("index");
        Ref out = checked(PyList_New(0)), zero = checked(PyLong_FromLong(0));
        Ref faces = attr(bm, faces_name);
        Py_ssize_t position = 0;
        each(faces.get(), [&](PyObject* face) {
            Ref corners = attr(face, loops_name);
            Ref face_index(nullptr);
            each(corners.get(), [&](PyObject* loop) {
                Ref kind = checked(PyObject_GetItem(loop, layer));
                int unmarked = PyObject_RichCompareBool(kind.get(), zero.get(), Py_EQ);
                if (unmarked < 0) throw PythonError{};
                if (!unmarked) {
                    if (!face_index) face_index = attr(face, index_name);
                    Ref at = checked(PyLong_FromSsize_t(position));
                    appended(out.get(), at.get());
                    appended(out.get(), face_index.get());
                }
                if (position == PY_SSIZE_T_MAX) {
                    PyErr_SetString(PyExc_OverflowError, "mesh loop position exceeds Python size limit");
                    throw PythonError{};
                }
                ++position;
            });
        });
        return out.release();
    } catch (const PythonError&) { return nullptr; }
    catch (const std::bad_alloc&) { return PyErr_NoMemory(); }
    catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native constrained loops scan failed");
        return nullptr;
    }
}

PyObject* island_area(PyObject*, PyObject* arguments) {
    PyObject *faces, *layer;
    if (!PyArg_ParseTuple(arguments, "OO:island_area", &faces, &layer)) return nullptr;
    try {
        Ref loops_name = name("loops"), uv_name = name("uv");
        std::vector<std::array<double, 2>> points;
        double total = 0.;
        // Caller supplies the sorted face order. Read all corners of each face
        // before arithmetic, as the Python pts list does; retain no wrappers.
        each(faces, [&](PyObject* face) {
            points.clear();
            Ref corners = attr(face, loops_name);
            each(corners.get(), [&](PyObject* loop) {
                points.push_back(coordinate(loop, layer, uv_name));
            });
            double acc = 0.;
            for (size_t k = 0; k < points.size(); ++k) {
                const auto& p = points[k];
                const auto& q = points[(k + 1) % points.size()];
                acc += p[0] * q[1] - q[0] * p[1];
            }
            total += std::abs(acc) * .5;
        });
        return PyFloat_FromDouble(total);
    } catch (const PythonError&) { return nullptr; }
    catch (const std::bad_alloc&) { return PyErr_NoMemory(); }
    catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native island area read failed");
        return nullptr;
    }
}

PyObject* version(PyObject*, PyObject*) { return PyLong_FromLong(2); }
PyMethodDef methods[] = {
    {"island_area", island_area, METH_VARARGS, "Sum current per-face UV areas in the supplied face and loop order, without writes."},
    {"collect_loops", collect_loops, METH_O, "Collect a fresh face-major loop list without reading UVs or changing the mesh."},
    {"constraint_records", constraint_records, METH_VARARGS, "Read sparse face-major constraint records using the caller's current vertex indices."},
    {"constrained_loops", constrained_loops, METH_VARARGS, "Scan face-major constrained (position, face_index) pairs without reading UVs."},
    {"capture", capture, METH_VARARGS, "Read BMesh slots through public Python APIs, without writes."},
    {"read_uv", read_uv, METH_VARARGS, "Read current UVs from a live loop sequence, without writes."},
    {"write_uv", write_uv, METH_VARARGS, "Write float64 UV pairs onto a live loop sequence through public Python APIs."},
    {"write_uv_indexed", write_uv_indexed, METH_VARARGS, "Write float64 UV pairs onto indexed slots of a live loop sequence."},
    {"read_pins", read_pins, METH_VARARGS, "Read pin_uv flags from a live loop sequence into a compact byte mask."},
    {"write_pins", write_pins, METH_VARARGS, "Write pin_uv flags from a compact byte mask onto a live loop sequence."},
    {"read_uv_select", read_uv_select, METH_O, "Read uv_select_edge/uv_select_vert flags into a compact byte mask."},
    {"write_uv_select", write_uv_select, METH_VARARGS, "Write uv_select_edge/uv_select_vert flags from a compact byte mask."},
    {"set_uv_select_all", set_uv_select_all, METH_VARARGS, "Bulk-set uv_select_vert, uv_select_edge, and optionally pin_uv on a loop sequence."},
    {"version", version, METH_NOARGS, "Reader API version."},
    {nullptr, nullptr, 0, nullptr}
};
PyModuleDef module = {PyModuleDef_HEAD_INIT, "_uvck_reader", nullptr, -1, methods};
}
PyMODINIT_FUNC PyInit__uvck_reader() { return PyModule_Create(&module); }
