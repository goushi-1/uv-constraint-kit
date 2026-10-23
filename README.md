# UV Constraint Kit

[简体中文](README.zh-CN.md)

UV edge and shape constraints for Blender. Python owns the interface,
selection, undo and overlays; a C++17 engine accelerates numeric solves.
An optional CUDA Driver API engine is included as source.

**Source preview, not a stable release.** The public 1.7.42 tree introduces
independent quadrification. Historical isolated acceptance of the 1.7.39 solver
does not constitute acceptance of every feature in this new tree. See
[validation and version status](docs/VALIDATION.md).

## Features

- Horizontal, vertical and straight-edge constraints on individual UV seam sides.
- Optimize with pins, area preservation and checked rollback.
- Circle and rectangle boundary constraints, including selected holes.
- Symmetry, multiple live symmetry groups, and constraint transfer on detach.
- Configurable overlays and English / Simplified Chinese interface.
- Independent grid quadrification for compatible quad charts.

This is an independent solver, not RizomUV's proprietary engine. Results,
convergence and performance can differ. The project is not affiliated with
Rizom-Lab, Blender, or the authors of its dependencies.

## Quick start

Requires **Blender 5.2 or newer**; publication checks use Blender 5.2.3.
Native engine builds currently target **Windows x64 / MSVC**.

```sh
python scripts/build_native.py
python scripts/package.py --native-dir build/native/bin/windows-x64 --output dist/uv_constraint_kit.zip
```

Install the resulting ZIP through Blender's **Preferences → Get Extensions →
Install from Disk**, then enable UV Constraint Kit. In Edit Mode, use the
**UV Constraint** sidebar in the UV Editor. F9 opens operation parameters.

A source-only ZIP can also be built without `--native-dir`; it uses the slower
Python fallback. GitHub's repository source download is not an installable
extension ZIP. See [installation](docs/INSTALL.md) and [build instructions](docs/BUILD.md).

## Tests and limitations

```sh
python tests/test_native.py --engine-dir build/native/bin/windows-x64
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_quad_grid_host.py
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_host.py
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_selection_host.py
```

Use disposable Blender processes. Tests create synthetic meshes and do not
save user scenes or preferences. CPU numerical tests need NumPy; Blender's
bundled Python already supplies it.

Read [known issues](docs/KNOWN_ISSUES.md) before production use. Pole vertices,
inconsistent quad cycles and mixed polygon charts may be rejected by the grid
tool. Optional GPU execution does not accelerate every operation and is not a
replacement for end-to-end benchmarks.

## Versions

- `main`: 1.7.42 source preview, with independent quadrification.
- `experimental/numeric-trials`: separately marked 1.7.43 experiment, retaining
  intermediate protected solver coordinates in arrays. No stable global
  performance gain has been demonstrated.
- Historical local releases and benchmarks are described in
  [validation](docs/VALIDATION.md); private assets and test dumps are excluded.

## License

GNU GPL **3.0 or later**, without warranty. See [LICENSE](LICENSE) and
[third-party notices](THIRD_PARTY_NOTICES.md). The UniV-derived symmetry code
retains its copyright and license. The quadrification code is independently
implemented; the previous third-party adaptation is not published here.
