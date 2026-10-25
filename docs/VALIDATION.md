# Version and validation status / 版本与验收边界

| Track | Meaning / 含义 |
| --- | --- |
| Previous published 1.7.49 | CPU, optional CUDA engine and stable-ABI reader release. Its package and factory-GUI checks remain historical. 上一公开基线。 |
| Accepted candidate 1.7.51 | Native loop collection and sparse constraint-record reading. Baseline for the 53 comparisons below; not a separately published release. 已验收候选，是本页 53 性能对照基线。 |
| Accepted candidate 1.7.53 | Operation-local geometry and CPU workspace reuse. Exact-output, CPU/GPU, dense host and memory observations are recorded below. 已验收性能对照候选。 |
| This source tree, 1.7.54 | Integrated release with its final archive, factory-GUI and four additional integration groups checked separately. 当前整合发行版，最终包检查见下表。 |
| Earlier previews and `experimental/numeric-trials` | Historical or experimental tracks; their reports do not validate the current package and are not cumulative speed claims. 历史／实验路径，单独保留证据边界。 |

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
- `test_geometry_host.py`: independent synthetic multi-island continuation,
  fixed boundaries, actual fill-holes unwrap and actual shared-UV fitting;
  requires the native CPU engine and a disposable factory GUI.
- `check_public_tree.py`: source parsing, packaging metadata, private-path/
  credential-pattern scan, absent excluded adaptation, and required notices.

Historical CPU and CPU+GPU summaries are retained in
[`publication_checks.json`](../publication_checks.json) and
[`gpu_publication_checks.json`](../gpu_publication_checks.json); they describe
1.7.42, not 1.7.54. Current version, final artifact identity and gates are
recorded in [`release_checks.json`](../release_checks.json).
These summaries omit machine paths and raw logs.
A passed automated check is not user acceptance of every GUI transformation or
production mesh.

公开验收只标记实际运行过的项目。GPU 路由和结果只对记录中的构建、主机和用例成立，
不代表所有显卡、驱动、网格或工作流均已通过。原用户安装、资产和场景均未由这些测试修改。

## Accepted 1.7.53 candidate / 已验收的 1.7.53 候选

The accepted candidate contains 44 source/runtime/native files. Its CPU and
reader metadata match the corresponding source and binary hashes; GPU source,
DLL and PTX identities remain unchanged. The 1.7.54 integration reuses those
checked components when their source hashes match. That reuse does not skip
the final archive and integration checks below.

- **Prepared geometry:** lifecycle invalidation and sequential-trial checks
  passed on CPU and Python routes. The exercised cases retained inputs,
  outputs and solver statistics. Refreshing UV groups or the initial snapshot
  invalidates reuse; fill-holes and nonempty shared groups retain independent
  preparation. Current UVs, density layers, signs, inverse frames, anchors and
  constraint weights are recomputed for every solve.
- **CPU workspace:** eight solve and eight objective/gradient comparisons
  against the accepted CPU baseline matched recorded numerical outputs and solver
  statistics. Native checks also passed with actual GPU execution and an
  invalid-PTX failure followed by CPU retry. These checks cover the recorded
  buffers and host, not every hardware or input.
- **Complete operators:** two order-reversed passes produced 540 paired
  operations across five retained fixtures (1,080 executions). All 22 recorded
  input/output fields matched candidate 1.7.51, including UVs, marks, selection,
  pins, topology, settings and operation outcomes. Package identities remained
  unchanged throughout.
- **Dense host:** four independent factory-GUI processes in baseline,
  candidate, candidate, baseline order produced 18 paired complete operations
  (36 executions) on the side-96 / 18,432-triangle fixture. Public helper hashes,
  UV/mark/pin/selection/XYZ hashes, operation results and numerical statistics
  matched; all solves naturally used CPU with no GPU initialization.
- **Memory observation:** separate CPU-only processes solved the same public
  131,072-triangle fixture twice with a manual 50-iteration budget. Repeats
  preserved input buffers, output hashes and solver statistics. Peak working
  set was 81,924,096 bytes for 1.7.51 and 84,123,648 bytes for 1.7.53, about
  **2.1 MiB higher**. Working sets were close after releasing the fixture, but
  this does not prove absence of leaks or establish a memory bound. Large
  multi-island pressure remains unvalidated.

## Complete-operation timing / 完整操作耗时

| 1.7.53 versus candidate 1.7.51 | Baseline total | Candidate total | Observed change |
| --- | --- | --- | --- |
| 540 paired complete operations | 70.2472 s | 68.8448 s | About 2.0% less |
| Separate dense ABBA, 18 pairs | 15.0701 s | 14.8049 s | About 1.8% less |

These totals include complete registered mark/clear/optimize operations; they
are not native-kernel timings. Order was reversed and each process's first
operation was retained. These observations use **candidate 1.7.51** as the
baseline. They are not 1.7.54-versus-1.7.49 measurements and cannot be added to
earlier percentages. No fixed speed multiplier or operation deadline is promised.

以上是特定构建和用例的完整操作观测，不能把内核计时替代完整耗时，也不能与更早基线的
百分比相加。54 最终包另行核对了身份和整合行为。

## 1.7.54 final automated acceptance / 1.7.54 最终包自动化验收

Final archive: `uv_constraint_kit-1.7.54.zip`, 535,387 bytes, 44 members.
SHA-256: `625fd5b813c2d16d166bb99460ee8428a3a8dcda1c51e66a38f2a8537a64cee5`.
The source/test identities and gates are bound in
[`release_checks.json`](../release_checks.json). The checks below ran against
the extracted final ZIP using Blender 5.2.3 LTS, build `e13b728c6880`.

| Final check | Status |
| --- | --- |
| Final ZIP, member/CRC checks, source and native-binary identity | Passed, 44 members |
| Packaging regression and numeric safety | Passed, 7 and 18 cases |
| CPU/GPU numerical checks and invalid-PTX CPU retry | Passed |
| Host and selection checks, required native reader | Passed, 5 and 9 cases |
| Natural backend host checks | Passed, 9 operations; 3 natural GPU; CPU/GPU UV delta 0 |
| Factory-GUI confirmed operations, native/Python readers | Passed, 7 groups per route |
| Active G/R/S, confirm/cancel, undo/redo, native/Python readers | Passed, 24 gestures and 144 move samples per route |
| Four additional integration groups | Passed, 4/4; matched sealed 1.7.51 outputs |
| Public synthetic geometry regression | Passed, 4/4 |

Runtime files match sealed 1.7.53 except the version manifest and NOTICE.
The native CPU, reader and GPU artifacts were reused from validated,
source-matched builds; no unified fresh rebuild is claimed. Public tests use
synthetic meshes; private asset replays and raw timing records remain excluded.
Four additional groups cover multiple islands, fixed boundary success/conflict,
an actual annulus unwrap with fill-holes, and actual shared-UV fitting.

## Retained limits / 保留限制

Two historical folded-input cases remain known unresolved. Their earlier
replays safely returned `constraint_triangle_conflict`, retained original UVs
and left pins, outside scope and topology unchanged; one corner case completed.
Neither geometry reuse nor workspace reuse claims to repair those conflicts.

Final 1.7.54 factory-GUI tests passed 24 event-modal combinations per reader
route, including sync on/off, whole/partial selection, G/R/S, confirm/cancel and
undo/redo. Queued Blender events were sampled while the modal operator remained
active. They were not physical OS mouse tests. Manual interaction, third-party
add-ons, user-installed environments and multi-island memory pressure remain
unverified by this release evidence.
