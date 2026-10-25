# UV Constraint Kit

[简体中文](README.zh-CN.md)

**Keep the UV edges you care about straight or shaped while the rest of the
island adjusts around them, inside Blender.**

> **Version 1.7.55 — Windows x64 CPU+GPU with the native reader.**
> [Download the extension](https://github.com/goushi-1/uv-constraint-kit/releases/download/v1.7.55/uv_constraint_kit-1.7.55.zip)
> · [Validation scope](docs/VALIDATION.md).

Select an edge run and give it a horizontal, vertical or straight-line
constraint. Select a closed loop to make a circular opening or a rectangular
cutout. With **Optimize after marking** enabled, the affected island updates
when you add or remove a constraint. Constraints remain available for later
optimization, rather than being a one-time alignment.

The [1.7.55 release](https://github.com/goushi-1/uv-constraint-kit/releases/tag/v1.7.55)
provides **`uv_constraint_kit-1.7.55.zip`**. Requires **Blender 5.2+**.
[Known issues](docs/KNOWN_ISSUES.md).
Eligible large solves use the GPU automatically; small meshes and some special
solve paths stay on the CPU. GPU use requires an NVIDIA GPU with compute
capability 8.9+ and a driver supporting CUDA 12.4+. The prebuilt extension does
not require the CUDA Toolkit. Other hardware and unsupported GPU setups use the
CPU path. Published builds remain in the
[release history](https://github.com/goushi-1/uv-constraint-kit/releases).

## What changed in 1.7.55

Recovery prechecks now reuse the operation's already captured UV input while
that input is still unchanged. Existing checks, fresh reads after seeding and
rollback remain in place. This release changes no C++, CUDA, reader source or
native binary; it reuses source-matched artifacts from 1.7.54.

In local comparisons, 540 pairs of complete operations took 67.8708471 s on
1.7.54 and 67.1553843 s on 1.7.55 (-1.054%; two rounds: -0.903% and -1.205%). A
separate dense 18-pair ABBA comparison took 14.499352 s and 13.7531216 s
(-5.147%; two rounds: -4.429% and -5.856%). These are limited observations on
the recorded machine and cases, not a universal speed claim or a sub-second
promise. Frozen dense inputs produced identical outputs and solver statistics;
576 private recovery-plan comparisons and 384 public recovery-input cases passed.
All eight package jobs, final GPU/GUI/modal checks and four integration groups
also passed; see [validation](docs/VALIDATION.md).

## Start with an existing UV layout

1. Install the release ZIP through **Preferences → Get Extensions → menu →
   Install from Disk**, then enable the extension. GitHub's **Code → Download
   ZIP** is repository source, not an installable extension.
2. Open an unwrapped mesh in **Edit Mode**. In the UV Editor, press **N** and
   open **UV Constraint**.
3. Use **Edge** selection mode and select only the UV edges you want to control.
   Turn **UV Selection Sync off** when you want just one side of a seam.
4. Apply a horizontal, vertical or straight-line constraint. For circularize
   or rectify, select a **complete closed edge loop**, rather than an open run.
5. Leave **Optimize after marking** on to see the island update, or turn it off
   to collect constraints and press **Optimize** when ready. **F9** adjusts the
   last operation. Select marked edges and use **Unconstrain** to release them.

You can constrain several separate runs without selecting the whole island.
The remaining UVs in the affected island adjust; an unselected gap between
runs does not receive an extra constraint.

## What each tool does

The before/after figures use synthetic meshes and actual plugin operations.
They explain the tools; they are not production-mesh acceptance evidence or
performance benchmarks.

### Horizontal constraints

Keep a selected run level in UV space. For example, mark the top and bottom
edges of a panel, then optimize its remaining UVs. Open runs and several
separate runs can be selected.

![Horizontal edge constraint: before and after](docs/images/constrain-horizontal.svg)

*Synthetic example: a selected run receives a horizontal constraint.*

### Vertical constraints

Keep a selected run upright. Combine horizontal and vertical constraints to
control the straight sides of a hard-surface part while its interior adjusts.
You do not have to mark every edge of the island.

![Vertical edge constraint: before and after](docs/images/constrain-vertical.svg)

*Synthetic example: a selected run receives a vertical constraint.*

### Straight-line constraints

Straighten a run along its current general direction, without forcing it onto
the horizontal or vertical axis. Use this for a slanted border that should be
straight while retaining its slant.

![Straight-line edge constraint: before and after](docs/images/constrain-aligned.svg)

*Synthetic example: a selected run is straightened along its existing direction.*

### Optimize

Redistribute the UVs of the affected island with its edge and shape constraints
in place. Use it after moving UVs or collecting marks with automatic
optimization off. Pins and protection settings can limit which changes are
possible; incompatible results can be rejected.

![UV optimization: before and after](docs/images/optimize.svg)

*Synthetic example: fix the outer boundary and optimize the disturbed interior UVs.*

### Remove constraints

Select marked edges and use **Unconstrain** to release them. With automatic
optimization on, the island is solved again under the remaining constraints.
This frees the selected edges; it does not restore an earlier UV layout.
An empty edge selection leaves other constraints alone.

![Removing selected constraints: before and after](docs/images/unconstrain.svg)

*Synthetic example: the selected constraint marks are removed.*

### Circularize

Select a complete closed loop and make it circular. The loop can surround an
opening **inside** an island, so it need not be the island's outer border.
Point spacing defaults to **Even**; F9 offers other spacing choices.

With automatic optimization on, the rest of the island adjusts around the
circle. The circular constraint is stored: shape another hole in the same
island, and earlier circles remain part of later optimization. With the switch
off, the target shape is recorded for the next Optimize operation.

![Circular boundary constraint: before and after](docs/images/circularize.svg)

*Synthetic example: a selected closed loop receives a circular constraint.*

### Rectify: rectangle or square

Select a complete closed loop to fit it to a rectangle. In F9, choose
**Target shape → Square** for equal sides. Like circularize, this stores a
constraint on the chosen opening or border and adjusts the rest of its island
when automatic optimization is on.

For a plate with several cutouts, shape one loop at a time. Existing circular
and rectangular constraints remain part of subsequent optimization, provided
the constraints and pins are compatible.

![Rectangular boundary constraint: before and after](docs/images/rectangularize.svg)

*Synthetic example: the selected hole boundary is constrained to a square.*

### Quadrify an island

Select part of a compatible quad island and use **Quadrify Island Grid** to
lay out that connected island as an axis-aligned quad grid. F9 controls face
proportions and averaging. This reorganizes the island's quad layout, rather
than only making a boundary rectangular.

The independent grid tool supports regular all-quad charts, including compatible
grid holes. Poles, mixed polygons, non-manifold links and inconsistent grid
cycles are rejected without a partial result. It is not a universal tool for
arbitrary topology.

![Quad grid layout: before and after](docs/images/quadrify.svg)

*Synthetic example: a compatible quad island is laid out as a grid.*

### Symmetry and live links

Mirror UVs between mesh faces matching across a chosen **local 3D symmetry
axis**. In F9, choose the UV direction and center; **By Cursor** uses the UV
cursor by default. Direct symmetry copies the layout and compatible
constraints once.

Enable **Live symmetry link** to keep the target following source edits or
optimization. Multiple groups can stay active with their own settings.
Matching sides can be separate UV islands or two parts of **one symmetric
island**. Tinted fill identifies linked targets; its color and opacity are
configurable in preferences.

Use **Stop symmetry link** to keep the current target UVs and transfer the
source's compatible constraints to the detached target. While linked, the
target follows source coordinates rather than being solved independently.

![Direct and live UV symmetry](docs/images/symmetry.svg)

*Synthetic example: direct symmetry copies once; live symmetry keeps a target
following later source edits.*

## Scope and limitations

- With Selection Sync off, edge constraints can affect just one UV seam side.
  Visibility and selection determine the operation's scope.
- Circularize and rectify need complete closed loops. Open runs use the
  horizontal, vertical or straight-line tools.
- Constraint colors and line width are configurable. Colored overlays show
  stored constraints; selection highlighting identifies selected marks.
- Conflicting constraints, pins or protection settings can leave UVs unchanged.
  Conflicting or unsupported islands can be rejected.

This is an independent solver, not RizomUV's proprietary engine. Results,
convergence and performance can differ. The project is not affiliated with
Rizom-Lab, Blender, or the authors of its dependencies. See
[known issues](docs/KNOWN_ISSUES.md) and [validation scope](docs/VALIDATION.md).

## Building from source

Python handles Blender's interface, selection, undo and overlays; C++17 engines
handle numeric solves. The 1.7.55 release reuses the source-matched CPU,
optional CUDA and CPython stable-ABI mesh reader artifacts from 1.7.54; these
native sources and binaries were not rebuilt or changed. Native builds target
**Windows x64 / MSVC**; host checks used Blender 5.2.3. Building the CUDA engine
from source requires CUDA Toolkit / NVRTC 12.4; the prebuilt ZIP needs only a
supported NVIDIA driver. The reader uses Blender's bundled Python at runtime.
Neither Python nor CUDA runtime files are distributed.
The commands below are a separate full-rebuild recipe; they were not used to
produce this release.

```sh
python scripts/build_native.py --build-dir build/native_gpu_reader --gpu --reader --python-include "$PYTHON_INCLUDE" --python-library "$PYTHON_LIBRARY" --cuda-include "$CUDA_PATH/include" --nvrtc-dir "$CUDA_PATH/bin"
python tests/test_native.py --engine-dir build/native_gpu_reader/bin/windows-x64 --gpu
python scripts/package.py --native-dir build/native_gpu_reader/bin/windows-x64 --require-gpu --require-reader --output dist/uv_constraint_kit-1.7.55.zip
```

`--reader` requires `--python-include` containing `Python.h` and
`--python-library` pointing to the stable-ABI import library named
`python3.lib`. Do not pass a versioned library such as `python313.lib`.
`--require-reader` makes packaging fail if the reader binary or validated
metadata is missing. The reader is optional for other builds; those packages
retain the Python fallback.

A Python-only ZIP can still be built without `--native-dir`, using the slower
fallback. See [installation](docs/INSTALL.md) and [build instructions](docs/BUILD.md).

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

For packaged reader/GPU host checks and factory GUI acceptance, see [build
instructions](docs/BUILD.md) and [validation status](docs/VALIDATION.md). The
host checks verify backend routing and result agreement; they are not a
performance multiplier claim. Factory GUI testing used queued mouse-move events
to sample active G/R/S transforms and undo/redo. It does not cover manual mouse
input or a user's installed Blender environment. The 1.7.55 package checks and
their scope are listed in the validation record.

Read [known issues](docs/KNOWN_ISSUES.md) before production use. Pole vertices,
inconsistent quad cycles and mixed polygon charts may be rejected by the grid
tool. GPU execution is limited to eligible paths and does not guarantee a speedup.

## Versions

- Current release: 1.7.55. Its only source change reuses an unchanged captured
  UV input during recovery prechecks; final package checks passed.
- Previous published release: 1.7.54. The 1.7.53-versus-1.7.51 results are
  historical and separate from the 1.7.54-versus-1.7.55 observations above.
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
