# Known issues / 已知问题

This document describes release 1.7.54. Candidate 1.7.53 evidence and the final
1.7.54 package/integration checks are recorded in [VALIDATION.md](VALIDATION.md).
Factory-GUI checks inject Blender
events; they are not manual OS mouse or user-install acceptance.

- **Grid tool topology.** The public quadrification replacement has a narrower
  supported topology than general UV tools. An integer-grid chart can contain
  regular grid holes, but poles, inconsistent cycles, non-manifold links,
  disconnected inputs and non-quads are rejected without a partial write.
  不支持的拓扑会安全拒绝；不保证适用任意 UV 岛或与其它插件的结果一致。
- **No RizomUV equivalence.** This repository does not contain its proprietary
  solver. Strong constraints can have no nonfolding feasible solution. Recovery
  and rollback can leave the UVs unchanged; inspect the operation's outcome.
  约束不相容时可能保持原 UV，不承诺任何输入都能成功展开。
- **Folded or overlapping input charts.** Fifteen synthetic recovery host cases
  passed historically, but they do not replay every user report. In three
  historical replays, two inputs remain unresolved and were safely rejected
  with `constraint_triangle_conflict`; the original UVs were retained, with no
  new flips, pin, outside-scope or topology changes, and matching source hashes
  before and after. A third corner case completed. This is not a general repair
  of folded layouts, and this release does not claim to fix those two inputs.
  Mixed winding, near-degenerate triangles, self-overlaps or
  incompatible anchors can still be rejected or restored unchanged. 折叠或重叠输入仍可能安全拒绝；
  合成恢复用例不等于旧用户案例已普遍修复。
- **Performance varies.** Mesh extraction, topology, recovery, safety checks,
  overlays and solver iterations contribute to complete operation time. The
  accepted 1.7.53-versus-1.7.51 comparison observed about 2.0% lower total time
  over 540 paired complete operations; the separate dense ABBA comparison
  observed about 1.8% lower time over 18 pairs. These use candidate 1.7.51 as
  their baseline, not published 1.7.49, and cannot be added to earlier timing
  percentages. They are observations, not a deadline or portable speed claim.
  GPU checks verify routing and results, not a performance guarantee.
  这些记录以 1.7.51 候选为基线，不可与更早的百分比相加；不得把 C++／GPU 内核计时当作完整操作承诺。
- **Temporary memory.** CPU scratch survives until its solve ends; prepared
  island geometry survives until the current protected strength loop ends.
  A 131,072-triangle CPU probe observed about 2.1 MiB more peak working set in
  candidate 1.7.53 than 1.7.51. This is a process observation, not a universal
  bound or proof of no leak. Large multi-island memory pressure is unvalidated.
  多岛临时几何与工作数组可能增加瞬时内存；单一合成用例不能证明没有泄漏或给出通用内存上限。
- **Native platforms.** Windows x64 / MSVC is the supported build path.
  macOS/Linux native engines are not supplied or validated.
- **GPU scope.** The package includes an optional CUDA engine for eligible large
  solves. It targets NVIDIA compute capability 8.9+ and drivers supporting
  CUDA 12.4+. The prebuilt extension does not need the CUDA Toolkit; unsupported
  hardware and some special solve paths use CPU. Native numerical checks,
  including an actual GPU route and invalid-PTX CPU fallback, passed for the
  accepted candidate and the final 1.7.54 package. The final packaged-host run
  also exercised three natural GPU operations. This does not cover
  every GPU model, driver, mesh or Blender workflow. GPU 路径已按验收记录检查，
  但不代表所有显卡、驱动、网格或工作流都已通过。
- **Native reader compatibility.** The package includes a Windows x64 CPython
  stable-ABI reader built for Python 3.11 and newer. A missing or incompatible
  reader can use the Python fallback, which may be slower. The release
  build requires the reader, but runtime compatibility with every Blender build is not
  guaranteed. Python and CUDA runtimes are not bundled. 原生读取器只随目标包提供，
  缺失或不兼容时会回退 Python；并不捆绑 Python 或 CUDA 运行时。
  Older API 2 readers can lack the new collection methods; those calls use
  Python and do not obtain their native-path performance benefit.
- **Anchors and selection.** Pins and shape constraints can conflict with new
  edge directions. Seam sides require UV-aware selection; Selection Sync can
  select both mesh-edge UV copies.
- **Acceptance boundaries.** Final 1.7.54 native-reader and Python-reader
  factory-GUI routes each passed 24 modal combinations across sync on/off, whole/partial
  selection, G/R/S and confirm/cancel. Each combination injected six
  `MOUSEMOVE` events and was sampled while Blender's modal operator was active;
  confirms included undo/redo. Blender event simulation was used; no physical
  OS mouse was moved. Manual interaction, third-party add-on interactions and
  persistent user preferences in an installed environment remain unverified.
  Four additional synthetic integration groups also passed. These are bounded
  functional cases, not many-island stress coverage.
  工厂 GUI 通过了事件注入式 modal 验收，不能代替人工鼠标或用户安装环境验收。

Report a minimal reproduction with Blender version, extension commit, native
build metadata, selection-sync state and parameters. Use a synthetic or
shareable mesh, remove file paths and personal data, and distinguish a wrong
result from a safe rejection. 请提供可公开的最小复现；不要上传私人场景或完整日志路径。
