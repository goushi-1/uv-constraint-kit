# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone synthetic native checks; no bpy, asset files, or Blender process."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def load_native(path: Path):
    spec = importlib.util.spec_from_file_location("uvck_native_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._required = True
    return module


def fixture(side: int):
    y, x = np.mgrid[:side + 1, :side + 1] / side
    uv = np.column_stack((x.ravel(), y.ravel()))
    u, v = uv[:, 0].copy(), uv[:, 1].copy()
    # Keep the boundary displacement below a cell width at high resolutions.
    # Otherwise hard-projecting the boundary itself folds the initial triangles,
    # so prevent_flips correctly rejects before either backend can optimize.
    scale = min(1., 16. / side)
    uv[:, 0] += scale * .025 * np.sin(v * np.pi * 2) * (.3 + np.sin(u * np.pi))
    uv[:, 1] += scale * .018 * np.sin(u * np.pi * 2) * (.3 + np.sin(v * np.pi))
    a = (np.arange(side)[:, None] * (side + 1) + np.arange(side)).ravel()
    b, d = a + 1, a + side + 1
    c = d + 1
    triangles = np.stack((np.column_stack((a, b, c)), np.column_stack((a, c, d))), axis=1).reshape(-1, 3)
    inverse = np.tile(np.array([[[side, -side], [0, side]], [[side, 0], [-side, side]]], float),
                      (side * side, 1, 1))
    weights = np.full(len(triangles), 1. / len(triangles))
    chains = [(1, np.arange(side + 1)), (1, np.arange(side * (side + 1), (side + 1) ** 2)),
              (2, np.arange(0, (side + 1) ** 2, side + 1)),
              (2, np.arange(side, (side + 1) ** 2, side + 1))]
    return uv, triangles, inverse, weights, chains


def solve(native, data, backend):
    uv, tri, inverse, weights, chains = data
    before = uv.copy()
    result = native.solve(uv, tri, inverse, weights, chains, {}, [], [],
                          1., .3, None, 1., 0., 50, False, True, False)
    assert result is not None and result[0] == 0, "Native solve did not return a solution"
    output = result[1]
    assert np.array_equal(uv, before), "Native solve mutated its input"
    assert np.isfinite(output).all() and not np.array_equal(output, before)
    for kind, ids in chains:
        axis = 1 if kind == 1 else 0
        assert np.ptp(output[ids, axis]) < 1e-10, "Hard axis constraint was not satisfied"
    diagnostics = native.diagnostics()
    assert diagnostics["gpu"]["last_backend"] == backend, diagnostics
    assert diagnostics["fallbacks"] == 0, diagnostics
    if backend == "GPU":
        gpu = diagnostics["gpu"]
        assert not gpu["disabled"] and gpu["fallbacks"] == 0, gpu
        assert gpu["driver"]["sessions"] > 0 and gpu["driver"]["energy"] > 0, gpu
        assert gpu["driver"]["errors"] == 0, gpu
    return output, diagnostics["last_solve"].copy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-dir", type=Path, default=ROOT / "build" / "native" / "bin" / "windows-x64")
    parser.add_argument("--gpu", action="store_true", help="Require real GPU execution on a 32,768-triangle fixture")
    args = parser.parse_args()
    folder = args.engine_dir.resolve()
    cpu = json.loads((folder / "engine.json").read_text(encoding="utf8"))
    names = ["engine.json", cpu["library"]]
    if args.gpu:
        gpu = json.loads((folder / "gpu.json").read_text(encoding="utf8"))
        names.extend(("gpu.json", gpu["library"], gpu["ptx"]))
    for name in names:
        if not isinstance(name, str) or Path(name).name != name:
            parser.error("Engine metadata contains an invalid filename")
        if not (folder / name).is_file():
            parser.error(f"Engine artifact missing: {name}")
    native = load_native(ROOT / "uv_constraint_kit" / "native_engine.py")
    # Only this isolated module's resource base changes. No add-on or installation
    # is edited, and Windows DLLs remain in their build directory until exit.
    native.__file__ = str(folder.parent.parent / "native_engine.py")
    try:
        small = fixture(16)
        first, stats = solve(native, small, "CPU")
        second, repeated_stats = solve(native, small, "CPU")
        assert np.array_equal(first, second) and stats == repeated_stats, "CPU solve was not deterministic"
        assert not native.diagnostics()["gpu"]["attempted"], "Small CPU test loaded CUDA"
        if args.gpu:
            large = fixture(128)
            threshold = native._GPU_MIN_TRIANGLES
            native._GPU_MIN_TRIANGLES = 2 ** 63 - 1
            expected, expected_stats = solve(native, large, "CPU")
            native._GPU_MIN_TRIANGLES = threshold
            actual, actual_stats = solve(native, large, "GPU")
            assert np.max(np.abs(expected - actual)) <= 1e-10, "CPU/GPU UV mismatch"
            for key in ("accepted", "evaluations", "reciprocal", "finite"):
                assert expected_stats[key] == actual_stats[key], f"CPU/GPU decision mismatch: {key}"
            native.shutdown_gpu()
            assert not native.diagnostics()["gpu"]["loaded"], "GPU shutdown did not release the session"
            # An actual driver/PTX failure must restart the CPU solve using the
            # original buffers, then skip GPU on the next solve in this session.
            failed = load_native(ROOT / "uv_constraint_kit" / "native_engine.py")
            failed.__file__ = native.__file__
            missing = folder / "intentionally_missing_test.ptx"
            assert not missing.exists()
            try:
                with failed._gpu_lock:
                    lib = failed._load_gpu()
                    assert lib is not None
                    assert lib.uvck_gpu_configure(str(missing).encode('utf8'), 1) == 0
                for repeat in range(2):
                    uv, tri, inverse, weights, chains = large
                    before = uv.copy()
                    result = failed.solve(uv, tri, inverse, weights, chains, {}, [], [],
                                          1., .3, None, 1., 0., 50, False, True, False)
                    assert result is not None and result[0] == 0
                    assert np.array_equal(result[1], expected) and np.array_equal(uv, before)
                    state = failed.diagnostics()["gpu"]
                    assert state["disabled"] and state["last_backend"] == "CPU"
                    assert state["calls"] == state["fallbacks"] == 1, state
                    assert "PTX" in state["reason"], state
            finally:
                failed.shutdown_gpu()
    finally:
        native.shutdown_gpu()
    print(json.dumps({"passed": True, "cpu": True, "gpu_required_and_executed": args.gpu,
                      "real_gpu_failure_cpu_retry": args.gpu,
                      "scope": "synthetic native buffer checks; not Blender UI acceptance"}, indent=2))


if __name__ == "__main__":
    main()
