// SPDX-License-Identifier: GPL-3.0-or-later
// Read-only CPython stable-ABI adapter. No Blender struct layouts or writes.
#define Py_LIMITED_API 0x030B0000
#include <Python.h>
#include <memory>
#include <new>
#include <array>
#include <cstdint>
#include <vector>

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
PyObject* version(PyObject*, PyObject*) { return PyLong_FromLong(2); }
PyMethodDef methods[] = {
    {"collect_loops", collect_loops, METH_O, "Collect a fresh face-major loop list without reading UVs or changing the mesh."},
    {"constraint_records", constraint_records, METH_VARARGS, "Read sparse face-major constraint records using the caller's current vertex indices."},
    {"capture", capture, METH_VARARGS, "Read BMesh slots through public Python APIs, without writes."},
    {"read_uv", read_uv, METH_VARARGS, "Read current UVs from a live loop sequence, without writes."},
    {"version", version, METH_NOARGS, "Reader API version."},
    {nullptr, nullptr, 0, nullptr}
};
PyModuleDef module = {PyModuleDef_HEAD_INIT, "_uvck_reader", nullptr, -1, methods};
}
PyMODINIT_FUNC PyInit__uvck_reader() { return PyModule_Create(&module); }
