# UV Constraint Kit

[简体中文](README.zh-CN.md)

**Keep the UV edges you care about straight or shaped while the rest of the
island adjusts around them, inside Blender.**

Select an edge run and give it a horizontal, vertical or straight-line
constraint. Select a closed loop to make a circular opening or a rectangular
cutout. With **Optimize after marking** enabled, the affected island updates
when you add or remove a constraint. Constraints remain available for later
optimization, rather than being a one-time alignment.

[Download the Windows x64 CPU preview](https://github.com/goushi-1/uv-constraint-kit/releases/tag/v1.7.42-preview.1)
— choose **`uv_constraint_kit-1.7.42-preview.zip`** in the release assets.
Requires **Blender 5.2+**. This is a **preview, not a stable production release**;
try it on a copy of your work. [Known issues](docs/KNOWN_ISSUES.md).

## Start with an existing UV layout

1. Install the preview ZIP through **Preferences → Get Extensions → menu →
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

*Synthetic example: a selected closed loop receives a rectangular constraint.*

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
  The preview is not guaranteed to solve every island.

This is an independent solver, not RizomUV's proprietary engine. Results,
convergence and performance can differ. The project is not affiliated with
Rizom-Lab, Blender, or the authors of its dependencies. See
[known issues](docs/KNOWN_ISSUES.md) and [validation scope](docs/VALIDATION.md).

## Building from source

Python handles Blender's interface, selection, undo and overlays; a C++17 engine
handles numeric solves. Optional CUDA Driver API engine source is included.
The downloadable preview above includes the **CPU engine**, not a GPU build.
Native builds target **Windows x64 / MSVC**; publication host checks use
Blender 5.2.3.

```sh
python scripts/build_native.py
python scripts/package.py --native-dir build/native/bin/windows-x64 --output dist/uv_constraint_kit.zip
```

A Python-only ZIP can be built without `--native-dir`, using the slower fallback.
See [installation](docs/INSTALL.md) and [build instructions](docs/BUILD.md).

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

- `main`: 1.7.42 preview, with independent quadrification and the disconnected-edge
  selection fix.
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
