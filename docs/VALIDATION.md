# Version and validation status / 版本与验收边界

| Track | Meaning / 含义 |
| --- | --- |
| Earlier published 1.7.49 | CPU, optional CUDA engine and stable-ABI reader release. Its package and factory-GUI checks remain historical. 更早的公开版本。 |
| Accepted candidate 1.7.51 | Native loop collection and sparse constraint-record reading. Baseline for the 53 comparisons below; not a separately published release. 已验收候选，是本页 53 性能对照基线。 |
| Accepted candidate 1.7.53 | Operation-local geometry and CPU workspace reuse. Exact-output, CPU/GPU, dense host and memory observations are recorded below. 已验收性能对照候选。 |
| Previous published 1.7.55 | Recovery precheck input reuse; final package, GPU, GUI and modal checks passed. 上一已发布版，验收记录见下文。 |
| Previous published release, 1.7.57 | CPU native reference-frame batching; solver equations unchanged. Final package, numeric, integration, GPU, GUI and modal checks passed. 已发布版，列出的最终验收均已通过。 |
| Current release, 1.7.58 | Final ZIP and 21 gates passed. 当前正式版，最终包与 21 项验收通过。 |
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
- `test_recovery_input_host.py`: live-BMesh versus supplied-numeric-input
  recovery comparison with the existing cross oracle; see [`BUILD.md`](BUILD.md).
- `test_uv_area_host.py` and `test_transaction_state_host.py`: live/numeric area, fallback and transaction rollback checks; see [BUILD.md](BUILD.md).
- `test_triangle_frames_host.py`: mathutils numeric oracle, triangle-frame
  geometry preparation and ABI failure sentinels; see [`BUILD.md`](BUILD.md).
- `check_public_tree.py`: source parsing, packaging metadata, private-path/
  credential-pattern scan, absent excluded adaptation, and required notices.

Historical CPU and CPU+GPU summaries are retained in
[`publication_checks.json`](../publication_checks.json) and
[`gpu_publication_checks.json`](../gpu_publication_checks.json); they describe
1.7.42, not the 1.7.57 release. The 1.7.58 final archive identity and gates are recorded in [release_checks.json](../release_checks.json).
These summaries omit machine paths and raw logs.
A passed automated check is not user acceptance of every GUI transformation or
production mesh.

公开验收只标记实际运行过的项目。GPU 路由和结果只对记录中的构建、主机和用例成立，
不代表所有显卡、驱动、网格或工作流均已通过。原用户安装、资产和场景均未由这些测试修改。

## Previous release: 1.7.55 / 上一发行版：1.7.55

The only source change reuses the UV input already captured by the current
operation during recovery planning, while that snapshot is unchanged. Existing
checks, overlap validation, default fresh reads after seeding and rollback are
retained. C++, CUDA, mesh-reader source and native binaries are unchanged; the
release reuses source-matched artifacts from 1.7.54 without a fresh rebuild.
Its archive identity is retained in the
[tagged 1.7.55 release_checks.json](https://github.com/goushi-1/uv-constraint-kit/blob/v1.7.55/release_checks.json).

| Previous release evidence | Status |
| --- | --- |
| Final ZIP | Passed, 44 members, 535,534 bytes |
| Final ZIP SHA-256 | `715445ec4669823c815086b87faa4933290600e3f41058e8b8605d802441448c` |
| Eight package jobs | Passed: standalone CPU+GPU and bad-PTX CPU retry; host (5); selection (9); GPU (9, including 3 natural GPU, CPU/GPU UV delta 0); GUI native/Python (7 groups each); modal native/Python (24 gestures and 144 samples each) |
| Modal drawing and undo/redo | Passed; active drawing had no errors, confirmed operations covered undo/redo |
| Public `test_recovery_input_host.py` | Passed, 384 cases with required native reader; GPU not required |
| Private recovery-plan comparison | Passed, 576 direct 1.7.54-versus-1.7.55 comparisons |
| Frozen dense native inputs | Passed, 3 inputs; outputs and solver statistics matched exactly |
| Additional final integration groups | Passed, 4 groups; matched baseline 1.7.54 |
| Complete-operation comparison | 540 pairs: 67.8708471 s (1.7.54) → 67.1553843 s (1.7.55), -1.054%; rounds -0.903% and -1.205% |
| Dense ABBA comparison | 18 pairs: 14.499352 s → 13.7531216 s, -5.147%; rounds -4.429% and -5.856% |

The public 384-case recovery test compares live BMesh input with supplied
numeric input using the existing cross oracle; it does not use a private
1.7.54 baseline. `--require-native` requires the mesh reader, not a native solve
for every case; NaN topology inputs may use the existing Python fallback. The
576-case private comparison is a separate direct 1.7.54-versus-1.7.55 check.

Timing values are observations from limited cases on the recorded machine, not
a portable speed claim or a sub-second promise. They are a direct 1.7.54 to
1.7.55 comparison and are not added to the historical 1.7.53-versus-1.7.51
results below. Package checks passed for the listed hosts and cases; this does
not establish acceptance for every machine, asset or workflow.

公开恢复输入测试的 384 项是 live BMesh 与给定数字输入、沿用旧 cross oracle 的比较，不使用私有 1.7.54 基线；`--require-native` 要求读取器，但含 NaN 的 topology 用例允许现有 Python fallback。另有独立的 576 项私有 1.7.54 对 1.7.55 直接对照。性能数据是特定机器和有限用例的观测，不承诺通用提速或低于一秒；这些数据与下方历史 1.7.53 对 1.7.51 结果分开，不相加。检查结果仅覆盖所列主机和用例，不代表所有机器、资产或工作流。

## Current release: 1.7.58 / 当前发行版

The rebuilt stable-ABI reader adds optional live per-face UV area calculation; old readers without the method retain Python fallback. CPU and GPU binaries are source-matched artifacts from 1.7.57. Core and safety share initial numeric UV input and each candidate triangle array. Solver formulas, ordering and checks remain unchanged.

| Evidence | Status |
| --- | --- |
| Final ZIP | Passed 21 gates; 44 members, 538,881 bytes; SHA-256 `09f1ae1a5994d9e746538b87fbec650cf2530a1aabf6a121ba849404ce43d23a` |
| Final package jobs | Eight passed: CPU/GPU and bad-PTX retry, host, selection, GPU, GUI native/Python and modal native/Python |
| Factory modal | Each reader: 24 gestures, 144 injected move samples; no manual OS mouse or user-install acceptance |
| Public tests | Six groups passed; area 698 cases plus 16 fallback cases (thread, error and bad index); transaction 12 checks; triangle frames 104 numeric, four-face geometry and two ABI sentinels; recovery 384; geometry four groups |
| Direct 1.7.57 → exact final 1.7.58 | 540 complete-operation pairs, 22 exact fields and 918 exact native events: 66.1200718 → 62.4257842 s, -5.58724%; phases -5.61249% and -5.56219% |
| Direct dense ABBA | 18 pairs: 13.1601646 → 12.2268262 s, -7.092148%; both phases faster |
| Source-stage memory | Compared to area58 source, not public57: 32,768 triangles, CPU/AUTO peak working set +1.03/+1.91 MiB, private bytes -1.59/-10.98 MiB; single process pair per backend, not a bound or leak proof |

The direct timings are limited local observations, not a portable speed or sub-second guarantee; some small cases regressed. The two existing partial-mix safe rejections in transaction checks are not completed alignments. Two actual same-axis three-corner conflicts remain safely rejected. Factory GUI events are injected; manual interaction in a user installation was not tested.

最终包通过 21 项验收。直接对照已发布 1.7.57 与最终 1.7.58：540 对完整操作耗时下降 5.58724%，18 对密集 ABBA 下降 7.092148%；两阶段均改善，但部分小用例变慢，不承诺通用提速或低于一秒。内存观察使用 area58 源码基线，不是 1.7.57 对比或内存上限。两项已有半强度安全拒绝不计为完成对齐；两例实际同轴三角冲突仍安全拒绝。

See [BUILD.md](BUILD.md) for the public area and transaction test commands.

## Previous release: 1.7.57 / 上一发行版：1.7.57

The release batches read-only CPU reference-triangle frame preparation. The
CPU native library was newly compiled; solver equations and solver behavior
are unchanged. GPU and reader source and binaries are reused from 1.7.55. The
three rejected 1.7.56 candidates are excluded. All final checks passed. The
historical archive identity and release gates are recorded at the [v1.7.57 tag](https://github.com/goushi-1/uv-constraint-kit/blob/v1.7.57/release_checks.json).

| Final release evidence | Status |
| --- | --- |
| ZIP identity | Passed, 44 members, 537,086 bytes |
| ZIP SHA-256 | `afde872538beada4be471d8088c02fb5fbb7f095d056d57bd0a3ce92d2c27297` |
| Sealed release gates | Passed, 19/19 |
| Source audit | Passed, 76 public files |
| Eight package jobs | Passed: standalone CPU/GPU and bad-PTX CPU retry; host (5); selection (9); GPU (9, including 3 natural GPU with CPU/GPU UV delta 0); GUI native/Python (7 each); modal native/Python (24 gestures and 144 samples each) |
| Integration and public geometry | Passed, 4 groups each |
| Public `test_triangle_frames_host.py` | Passed on Blender 5.2.3: 104 numeric cases, one four-face/seven-triangle geometry fixture, two ABI sentinel cases; no solver run |
| Earlier candidate checks | Passed: 1,288 mathutils cases, 128 geometry cases, 6 ABI cases, 8 CPU solve cases and 8 energy/gradient cases |
| Complete-operation comparison | 540 pairs: 67.858162 s (1.7.55) → 66.2183058 s (1.7.57), -2.4166%; rounds -4.081% and -0.691% |
| Dense ABBA comparison | 18 pairs: 14.479992 s → 13.6930876 s, -5.4344%; rounds -9.555% and -1.030% |
| Large geometry memory probe | 131,072 triangles in two independent processes; geometry arrays byte-identical; peak working set -38.496 MiB and private bytes -45.148 MiB observed |

These timing and memory results are limited observations from the recorded
machine and cases. Some small groups regressed; no general speedup or
sub-second operation is promised. The memory probe is not a bound or leak
proof. Floating-point behavior was verified only with Blender 5.2.3. Older
native libraries without the optional frame-batch symbol use the existing
Python fallback. The public test does not run a solver. The 1.7.56 candidates
were rejected and are not part of this package.

1.7.57 正式版批量准备只读 CPU 参考三角帧；CPU 原生库为新编译，求解公式和行为未变。GPU 与读取器源码、二进制沿用 1.7.55。三个已淘汰的 1.7.56 候选均未纳入。该包身份见 v1.7.57 标签，1.7.55 历史包记录见 v1.7.55 标签。计时是本机有限用例观测，部分小组变慢，不承诺通用提速或低于一秒。内存测量不是上限或无泄漏证明。浮点行为仅在 Blender 5.2.3 验证；旧原生库缺少可选符号时会回退 Python。公开测试不运行求解器。

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

## Previous release: 1.7.54 final automated acceptance / 上一发行版最终包验收

Previous release archive: `uv_constraint_kit-1.7.54.zip`, 535,387 bytes, 44 members.
SHA-256: `625fd5b813c2d16d166bb99460ee8428a3a8dcda1c51e66a38f2a8537a64cee5`.
The source/test identities and gates are bound in the
[1.7.54 tagged release_checks.json](https://github.com/goushi-1/uv-constraint-kit/blob/v1.7.54/release_checks.json).
The checks below ran against
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
