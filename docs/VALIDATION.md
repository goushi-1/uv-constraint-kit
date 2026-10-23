# Version and validation status / 版本与验收边界

| Track | Meaning / 含义 |
| --- | --- |
| Historical 1.7.38 | Existing local package and installation inspected during publication. Its synchronized edge-selection bug is fixed in this preview. The previous quadrification adaptation is excluded. 本地既有打包版本，不作为本次公开稳定版重发。 |
| Historical 1.7.39 | Isolated development baseline: 540 complete-operation comparison records and 14 regression groups. Not a formally released or live-installed acceptance. 保留数值求解基线来源，不等于新版全部功能已验收。 |
| `main`, 1.7.42 | First source preview, based on the validated solver baseline and new independent grid tool. New build/host checks are described below. 预览版，需要实际工作流反馈。 |
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

The publication check results are summarized in
[`publication_checks.json`](../publication_checks.json).
This summary omits machine paths and raw logs. A passed automated check is not
user acceptance of real GUI transformations or every production mesh.

本次公开树结果由仓库内脚本重跑生成。只有明确运行过的项目标记为通过；GPU 未运行时
记录为未验收，而不会伪装成通过。原用户安装、资产和场景均不由这些测试修改。

On 2026-10-24, a read-only live-scene comparison confirmed that the old selector
counted 19 edges where 18 were selected: sync displayed mesh edge mode while
the hidden UV-only mode retained vertex mode. The fixed selector returned 18,
excluded the single gap, and left all UVs and selection flags unchanged. This
is selector evidence, not a full live optimization or installed-version test.

2026-10-24 已对当前场景只读核对选区修复；未替换用户安装，也未保存场景。
