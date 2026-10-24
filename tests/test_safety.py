# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone numeric equivalence checks for safety triangle health.

This test needs only the Python standard library and NumPy. It AST-extracts
the two numeric functions so importing Blender-only add-on modules is avoided.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
from pathlib import Path
import sys

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SAFETY_SOURCE = ROOT / "uv_constraint_kit" / "safety.py"
SEED = 481748


def load_numeric_functions():
    source = SAFETY_SOURCE.read_text(encoding="utf8")
    tree = ast.parse(source, filename="uv_constraint_kit/safety.py")
    wanted = {"_area", "_triangle_health"}
    nodes = {node.name: node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in wanted}
    if set(nodes) != wanted:
        missing = sorted(wanted - set(nodes))
        raise AssertionError("Missing numeric safety function(s): " + ", ".join(missing))
    module = ast.Module(body=[nodes[name] for name in ("_area", "_triangle_health")],
                        type_ignores=[])
    namespace = {"math": math, "np": np}
    exec(compile(ast.fix_missing_locations(module), "uv_constraint_kit/safety.py", "exec"),
         namespace)
    return namespace["_area"], namespace["_triangle_health"]


def reference_health(area, triangles):
    """Reproduce the prior per-triangle loop and its Python-float input path."""
    flips = degenerate = 0
    finite = True
    for row in triangles:
        # The explicit float32 case preserves NumPy scalar arithmetic in the
        # tuple rows, as the dtype fallback is intended to do.
        keep_float32 = (isinstance(triangles, list) and row and
                        isinstance(row[0][0], np.float32))
        points = [tuple(value if keep_float32 else float(value) for value in point)
                  for point in row]
        signed_area = area(points)
        flips += signed_area < -1e-12
        degenerate += abs(signed_area) < 1e-14
        finite &= all(math.isfinite(value) for point in points for value in point)
    return int(flips), int(degenerate), bool(finite)


def triangle_for_area(signed_area):
    return ((0.0, 0.0), (2.0 * float(signed_area), 0.0), (0.0, 1.0))


def make_cases():
    rng = np.random.default_rng(SEED)
    random64 = rng.normal(size=(4096, 3, 2)).astype(np.float64)
    inverted64 = random64[:, [0, 2, 1], :].copy()

    collinear64 = np.empty((256, 3, 2), dtype=np.float64)
    base = rng.normal(size=(256, 2))
    delta = rng.normal(size=(256, 2))
    collinear64[:, 0] = base
    collinear64[:, 1] = base + delta
    collinear64[:, 2] = base + 2.0 * delta

    threshold_values = []
    for threshold in (-1e-12, 1e-14, -1e-14):
        threshold_values.extend((
            math.nextafter(threshold, -math.inf),
            threshold,
            math.nextafter(threshold, math.inf),
        ))
    threshold64 = np.asarray([triangle_for_area(value) for value in threshold_values],
                             dtype=np.float64)

    extreme64 = np.asarray([
        ((0.0, 0.0), (2e150, 0.0), (0.0, 1e150)),
        ((0.0, 0.0), (2e-150, 0.0), (0.0, 1e-150)),
        ((1e308, 1e308), (-1e308, 1e308), (1e308, -1e308)),
        ((0.0, 0.0), (2e308, 0.0), (0.0, 1.0)),
    ], dtype=np.float64)
    damaged64 = np.asarray([
        ((0.0, 0.0), (np.nan, 0.0), (0.0, 1.0)),
        ((0.0, 0.0), (np.inf, 0.0), (0.0, 1.0)),
        ((np.inf, 0.0), (1.0, 1.0), (0.0, -np.inf)),
    ], dtype=np.float64)
    float32_array = rng.normal(size=(512, 3, 2)).astype(np.float32)
    float32_rows = [tuple(tuple(value for value in point) for point in row)
                    for row in float32_array]

    return [
        ("random_float64", random64, "float64"),
        ("inverted_float64", inverted64, "float64"),
        ("degenerate_collinear_float64", collinear64, "float64"),
        ("threshold_exact_and_adjacent_float64", threshold64, "float64"),
        ("extreme_scales_float64", extreme64, "float64"),
        ("nan_inf_float64", damaged64, "float64"),
        ("empty_array_float64", np.empty((0, 3, 2), dtype=np.float64), "float64"),
        ("empty_list", [], "python-list"),
        ("random_float32", float32_rows, "tuple[np.float32]"),
    ]


def run_checks():
    area, triangle_health = load_numeric_functions()
    cases = []
    failures = []
    original_err = np.geterr().copy()
    try:
        for err_mode in ("warn", "raise"):
            np.seterr(all=err_mode)
            for name, triangles, input_dtype in make_cases():
                err_before = np.geterr().copy()
                try:
                    expected = reference_health(area, triangles)
                    actual_raw = triangle_health(triangles)
                    actual = (int(actual_raw[0]), int(actual_raw[1]), bool(actual_raw[2]))
                    err_after = np.geterr().copy()
                    row = {
                        "name": name,
                        "input_dtype": input_dtype,
                        "count": len(triangles),
                        "numpy_err_mode": err_mode,
                        "expected": list(expected),
                        "actual": list(actual),
                        "equal": actual == expected,
                        "seterr_unchanged": err_after == err_before,
                    }
                    cases.append(row)
                    if not row["equal"] or not row["seterr_unchanged"]:
                        failures.append({"case": name, "mode": err_mode,
                                         "expected": list(expected), "actual": list(actual),
                                         "seterr_before": err_before, "seterr_after": err_after})
                except Exception as exc:
                    cases.append({"name": name, "input_dtype": input_dtype,
                                  "count": len(triangles), "numpy_err_mode": err_mode,
                                  "error": f"{type(exc).__name__}: {exc}"})
                    failures.append({"case": name, "mode": err_mode,
                                     "error": f"{type(exc).__name__}: {exc}"})
    finally:
        np.seterr(**original_err)

    expected_case_count = 18
    if len(cases) != expected_case_count:
        failures.append({"case": "case_count", "expected": expected_case_count,
                         "actual": len(cases)})

    return {
        "passed": not failures,
        "scope": "numeric equivalence for safety._triangle_health versus the _area loop; no Blender import, timing, or product acceptance claim",
        "seed": SEED,
        "case_count": len(cases),
        "numpy_seterr_restored": np.geterr() == original_err,
        "checks": cases,
        "failures": failures,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        help="Optional path for a JSON report; omitted by default")
    args = parser.parse_args()
    report = run_checks()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n",
                               encoding="utf8")
    if report["passed"]:
        print(f"PASS: {report['case_count']} numeric checks; NumPy error policy restored")
        return 0
    print(f"FAIL: {len(report['failures'])} issue(s) across {report['case_count']} checks",
          file=sys.stderr)
    for failure in report["failures"]:
        print(json.dumps(failure, allow_nan=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
