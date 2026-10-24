# Version and validation status / 版本与验收边界

| Track | Meaning / 含义 |
| --- | --- |
| Historical 1.7.38 | Existing local package and installation inspected during publication. Its synchronized edge-selection bug is fixed in this preview. The previous quadrification adaptation is excluded. 本地既有打包版本，不作为本次公开稳定版重发。 |
| Historical 1.7.39 | Isolated development baseline: 540 complete-operation comparison records and 14 regression groups. Not a formally released or live-installed acceptance. 保留数值求解基线来源，不等于新版全部功能已验收。 |
| Historical 1.7.42-preview.1 | CPU-only source preview, based on the validated solver baseline and new independent grid tool. Superseded as the default download by preview.2; retained as a release attachment. 原 CPU-only 预览附件仍保留。 |
| Historical 1.7.42-preview.2 | CPU+GPU preview package. Its GPU routing, fallback, packaged host checks and regression results remain historical evidence. 作为历史附件保留。 |
| `experimental/numeric-trials`, 1.7.43 | Protected numeric-trial experiment. The earlier 1.7.41 experiment matched 540 operations and 15 groups but warm totals increased 0.70% (36.571 → 36.827 seconds). No stable global gain; not the default. 实验路径，不作为性能提升版宣传。 |
| `main`, 1.7.49 | Release package with CPU, optional GPU engine and CPython stable-ABI reader. Package and local factory GUI acceptance are recorded below; manual OS mouse use and user-installed environments are not covered. 当前正式版本；验收边界见下文。 |

Historical timing values are observations on a particular machine, not a
portable benchmark or a promise for the public tree. Private asset fixtures,
raw reports, screen captures, local paths and old package binaries are not
included. 旧数据只用于说明开发状态，不替代本仓库的可复跑测试。

## Public tests / 公开测试

- `test_native.py`: synthetic CPU input immutability, finite results, hard-axis
  residuals and repeatability; optional explicit GPU differential checks.
- `test_safety.py`: AST-extracted area and triangle-health numeric comparisons,
  including threshold, dtype, non-finite and NumPy error-state cases.
- `test_quad_grid_host.py`: actual quad rectification, orientation, scope and
  unsupported-topology rejection, using newly generated meshes.
- `test_host.py`: constraint solve, pin/scope invariants, independent grid
  integration, selected circle constraints, symmetry and live links.
- `test_selection_host.py`: disconnected edge runs, sync/mesh mode resolution,
  invalid selection flags, face scope, overlay tint, and mark/clear with and
  without automatic solving.
- `test_gui_host.py` and `test_modal_host.py`: factory-GUI operation and queued
  event-modal checks; see [`BUILD.md`](BUILD.md) for commands and scope.
- `check_public_tree.py`: source parsing, packaging metadata, private-path/
  credential-pattern scan, absent excluded adaptation, and required notices.

Historical CPU and CPU+GPU summaries are retained in
[`publication_checks.json`](../publication_checks.json) and
[`gpu_publication_checks.json`](../gpu_publication_checks.json); they describe
1.7.42, not the 1.7.49 release. These summaries omit machine paths and raw logs.
A passed automated check is not user acceptance of every GUI transformation or
production mesh.

公开验收只标记实际运行过的项目。GPU 路由和结果只对记录中的构建、主机和用例成立，
不代表所有显卡、驱动、网格或工作流均已通过。原用户安装、资产和场景均未由这些测试修改。

## 1.7.49 release acceptance / 1.7.49 发布验收

The release ZIP `uv_constraint_kit-1.7.49.zip` contains 44 files and has
SHA-256 `4a5116593650ab6d69e29ed55fdd4d25d47002561cca61f7d868444be3031784`.
The CPU, GPU and native-reader package was rebuilt by the unified build script.
Native numerical checks passed, including an actual GPU route and fallback from
invalid PTX; the packaged Blender host report passed its nine checks with the
reader required. These results apply to the tested package, Blender host and
cases only.

- A fresh 270-operation comparison (54 operations in each of five retained
  fixture groups) matched 1.7.48 exactly across all 22 recorded fields. The
  observed maximum times by fixture group were 0.455, 0.343, 0.270, 0.062 and
  0.046 seconds. None exceeded one second in this run; these measurements do
  not guarantee an operation deadline or a portable speed.
- Eighteen numeric safety cases and ten probe call-sequencing/fresh-read checks
  passed. Fifteen synthetic recovery host cases passed. These tests do not
  establish a blanket fix for historical folded layouts.
- Native-reader and Python-reader factory-GUI modal routes each passed 24
  combinations: sync on/off, whole/partial selection, G/R/S, and confirm/cancel.
  Each combination injected six `MOUSEMOVE` events, for 144 samples per route
  and 288 total. The native and Python routes each recorded 410 draw calls and
  312 modal calls with no draw errors. Confirmed operations included undo/redo;
  cancellation restored the captured state. The tests used Blender's event
  simulation in factory GUI and sampled while the modal operator was active.
  They did not move a physical OS mouse and do not cover manual interaction,
  third-party add-ons or a user-installed environment.
- Three historical folded-input replays were run. Two remain known unresolved:
  the operation safely rejected them with `constraint_triangle_conflict`,
  retained the original UVs and introduced no new flips, pin, outside-scope or
  topology changes; source assets had matching hashes before and after. One
  corner case completed. This does not claim a general repair of folded input.

## Performance context / 性能背景

The earlier 1.7.48 comparison against public 1.7.42 measured 9.8% lower total
time in that run. It is not a 1.7.49-versus-1.7.47 result. A separate dense
case had two order-reversed observations of 2.4% and 13.9%; the spread does not
support a fixed multiplier. The current 1.7.49-versus-1.7.48 comparison is an
exactness check with per-fixture observations above, not a new performance
claim. 这些都是特定运行观察，不代表固定提速或时限保证。

On 2026-10-24, a read-only live-scene comparison confirmed that the old selector
counted 19 edges where 18 were selected: sync displayed mesh edge mode while
the hidden UV-only mode retained vertex mode. The fixed selector returned 18,
excluded the single gap, and left all UVs and selection flags unchanged. This
is selector evidence, not a full live optimization or installed-version test.

2026-10-24 已对当前场景只读核对选区修复；未替换用户安装，也未保存场景。
