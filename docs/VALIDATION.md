# Version and validation status / 版本与验收边界

| Track | Meaning / 含义 |
| --- | --- |
| Historical 1.7.38 | Existing local package and installation inspected during publication. Its synchronized edge-selection bug is fixed in this preview. The previous quadrification adaptation is excluded. 本地既有打包版本，不作为本次公开稳定版重发。 |
| Historical 1.7.39 | Isolated development baseline: 540 complete-operation comparison records and 14 regression groups. Not a formally released or live-installed acceptance. 保留数值求解基线来源，不等于新版全部功能已验收。 |
| `main`, 1.7.42-preview.1 | CPU-only source preview, based on the validated solver baseline and new independent grid tool. Superseded as the default download by preview.2; retained as a release attachment. 原 CPU-only 预览附件仍保留。 |
| `main`, 1.7.42-preview.2 | CPU+GPU preview package. GPU routing, fallback, packaged host checks and regression results are summarized below. 这是当前默认预览下载。 |
| `experimental/numeric-trials`, 1.7.43 | Protected numeric-trial experiment. The earlier 1.7.41 experiment matched 540 operations and 15 groups but warm totals increased 0.70% (36.571 → 36.827 seconds). No stable global gain; not the default. 实验路径，不作为性能提升版宣传。 |

Historical timing values are observations on a particular machine, not a
portable benchmark or a promise for the public tree. Private asset fixtures,
raw reports, screen captures, local paths and old package binaries are not
included. 旧数据只用于说明开发状态，不替代本仓库的可复跑测试。

## Public tests / 公开测试

- `test_native.py`: synthetic CPU input immutability, finite results, hard-axis
  residuals and repeatability; optional explicit GPU differential checks.
- `test_quad_grid_host.py`: actual quad rectification, orientation, scope and
  unsupported-topology rejection, using newly generated meshes.
- `test_host.py`: constraint solve, pin/scope invariants, independent grid
  integration, selected circle constraints, symmetry and live links.
- `test_selection_host.py`: disconnected edge runs, sync/mesh mode resolution,
  invalid selection flags, face scope, overlay tint, and mark/clear with and
  without automatic solving.
- `check_public_tree.py`: source parsing, packaging metadata, private-path/
  credential-pattern scan, absent excluded adaptation, and required notices.

The CPU preview checks are summarized in
[`publication_checks.json`](../publication_checks.json); CPU+GPU package checks
are summarized in [`gpu_publication_checks.json`](../gpu_publication_checks.json).
These summaries omit machine paths and raw logs. A passed automated check is
not user acceptance of every GUI transformation or production mesh.

公开验收只标记实际运行过的项目。GPU 路由和结果只对记录中的构建、主机和用例成立，
不代表所有显卡、驱动、网格或工作流均已通过。原用户安装、资产和场景均不由这些测试修改。

## CPU+GPU preview acceptance / CPU+GPU 预览验收

- The final extracted CPU+GPU ZIP passed native numerical checks, a real bad-PTX
  fallback-to-CPU check, and the packaged Blender host check in a factory GUI.
- The nine host operations covered three small CPU cases, three large CPU cases
  and three naturally dispatched GPU cases. Across the three CPU/GPU result
  pairs, the measured maximum UV difference was `0` (test tolerance `2e-6`). These cases validate routing
  and result agreement; they do not establish a performance multiplier.
- Five host cases and nine selection regressions passed. The GPU factory GUI
  process exited normally. Real user mouse interaction and undo behavior were
  not tested.

最终解包的 CPU+GPU ZIP 通过原生数值检查、真实坏 PTX 回退 CPU 检查，以及工厂 GUI
中的打包宿主检查。9 次宿主操作包括小网格 CPU 3 次、大网格 CPU 3 次和自然分派 GPU
3 次；3 组 CPU/GPU 结果实测最大 UV 差值为 `0`（验收阈值 `2e-6`）。这些用例只验证路由与结果，不代表性能倍数。
5 项宿主检查、9 项选区回归通过；GPU 工厂 GUI 正常退出。真实用户鼠标交互和撤回行为未验收。

On 2026-10-24, a read-only live-scene comparison confirmed that the old selector
counted 19 edges where 18 were selected: sync displayed mesh edge mode while
the hidden UV-only mode retained vertex mode. The fixed selector returned 18,
excluded the single gap, and left all UVs and selection flags unchanged. This
is selector evidence, not a full live optimization or installed-version test.

2026-10-24 已对当前场景只读核对选区修复；未替换用户安装，也未保存场景。
