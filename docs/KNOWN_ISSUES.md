# Known issues / 已知问题

This document describes the 1.7.49 release and the limits of its validation.
The factory-GUI event-modal checks passed for both the native-reader and Python
routes. They inject Blender events; they are not manual OS mouse or user-install
acceptance.

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
  passed, but they do not replay every historical user report. In three
  historical replays, two inputs remain unresolved and were safely rejected
  with `constraint_triangle_conflict`; the original UVs were retained, with no
  new flips, pin, outside-scope or topology changes, and matching source hashes
  before and after. A third corner case completed. This is not a general repair
  of folded layouts. Mixed winding, near-degenerate triangles, self-overlaps or
  incompatible anchors can still be rejected or restored unchanged. 折叠或重叠输入仍可能安全拒绝；
  合成恢复用例不等于旧用户案例已普遍修复。
- **Performance varies.** Mesh extraction, topology, recovery, safety checks,
  overlays and solver iterations contribute to complete operation time. The
  earlier 1.7.48 comparison measured 9.8% less total time against published
  1.7.42; it is not a 1.7.49-versus-1.7.47 result. A separate dense CPU case
  measured 2.4% and 13.9% on two order-reversed runs, so no stable fixed
  multiplier is established. The 1.7.49 exact comparison against 1.7.48 had
  five observed fixture maxima of 0.455, 0.343, 0.270, 0.062 and 0.046 seconds;
  none exceeded one second in that run. These are observations, not a deadline
  guarantee or portable speed claim. GPU checks verify backend routing and
  results, not a performance guarantee. 这些记录不代表固定提速或时限保证；不得把 C++／GPU 内核计时当作完整操作承诺。
- **Native platforms.** Windows x64 / MSVC is the supported build path.
  macOS/Linux native engines are not supplied or validated.
- **GPU scope.** The package includes an optional CUDA engine for eligible large
  solves. It targets NVIDIA compute capability 8.9+ and drivers supporting
  CUDA 12.4+. The prebuilt extension does not need the CUDA Toolkit; unsupported
  hardware and some special solve paths use CPU. Native numerical checks,
  including an actual GPU route and invalid-PTX CPU fallback, and the packaged
  host checks passed for the recorded package and cases. This does not cover
  every GPU model, driver, mesh or Blender workflow. GPU 路径已按验收记录检查，
  但不代表所有显卡、驱动、网格或工作流都已通过。
- **Native reader compatibility.** The package includes a Windows x64 CPython
  stable-ABI reader built for Python 3.11 and newer. A missing or incompatible
  reader can use the Python fallback, which may be slower. Packaging requires
  the reader, but runtime compatibility with every Blender build is not
  guaranteed. Python and CUDA runtimes are not bundled. 原生读取器只随目标包提供，
  缺失或不兼容时会回退 Python；并不捆绑 Python 或 CUDA 运行时。
- **Anchors and selection.** Pins and shape constraints can conflict with new
  edge directions. Seam sides require UV-aware selection; Selection Sync can
  select both mesh-edge UV copies.
- **Acceptance boundaries.** Native-reader and Python-reader factory-GUI
  routes each passed 24 modal combinations across sync on/off, whole/partial
  selection, G/R/S and confirm/cancel. Each combination injected six
  `MOUSEMOVE` events and was sampled while Blender's modal operator was active;
  confirms included undo/redo. Blender event simulation was used; no physical
  OS mouse was moved. Manual interaction, third-party add-on interactions and
  persistent user preferences in an installed environment remain unverified.
  工厂 GUI 通过了事件注入式 modal 验收，但未测试人工鼠标和用户安装环境。

Report a minimal reproduction with Blender version, extension commit, native
build metadata, selection-sync state and parameters. Use a synthetic or
shareable mesh, remove file paths and personal data, and distinguish a wrong
result from a safe rejection. 请提供可公开的最小复现；不要上传私人场景或完整日志路径。
