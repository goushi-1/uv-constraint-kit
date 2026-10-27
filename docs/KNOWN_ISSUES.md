# Known issues / 已知问题

The 1.7.58 final ZIP passed the recorded 21 gates. It retains solver formulas, order and safety checks. See [VALIDATION.md](VALIDATION.md). Factory GUI checks inject Blender events; they are not manual OS mouse or user-install acceptance.

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
- **Partial-strength constraints.** A partial blend can miss the angular
  acceptance threshold even when its full step is feasible. A synthetic curved
  horizontal run at 50% strength is safely rejected with `constraint_residual`
  in both published 1.7.57 and 1.7.58; the complete input state is
  restored. The transaction regression checks that rollback, rather than
  counting the rejected solve as a completed alignment.
  半强度混合并不保证满足角度阈值；已有弯曲边界用例会安全拒绝并完整回滚，本版未改变此行为。
- **Performance varies.** In a direct local comparison of published 1.7.57 and the exact final 1.7.58 ZIP, 540 complete-operation pairs took 5.587% less time; 18 dense ABBA pairs took 7.092% less. Some small cases regressed. These observations are not portable or a sub-second guarantee. GPU checks verify routing and results, not a performance guarantee.
  直接对照结果仅适用于记录的有限本机用例，部分小用例变慢，不承诺通用提速或低于一秒。

- **Historical 1.7.57 measurements are limited.** The CPU frame-batch release measured
  -2.4166% over 540 complete-operation pairs and -5.4344%
  over 18 dense ABBA pairs, with regressions in some small groups. These results
  apply only to the recorded machine and cases. Geometry arrays matched byte
  for byte in two independent 131,072-triangle processes; observed peak working
  set fell by 38.496 MiB and private bytes by 45.148 MiB. This is not a memory
  bound or leak proof. The release passed its eight final package jobs. Float behavior was checked only with Blender
  5.2.3. Older native libraries without the optional frame-batch symbol use
  the Python fallback.
  1.7.57 正式版的计时和内存结果仅适用于记录的有限用例；部分小组变慢，不能视为通用承诺。
  浮点行为仅在 Blender 5.2.3 检查，缺少可选符号的旧库会回退 Python；最终包检查已通过。
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
  also exercised three natural GPU operations. Final 1.7.55 package checks
  passed nine host operations, including three natural GPU operations with
  CPU/GPU UV delta 0. This does not cover
  every GPU model, driver, mesh or Blender workflow. GPU 路径已按验收记录检查，
  但不代表所有显卡、驱动、网格或工作流都已通过。
- **Native reader compatibility.** The package includes a Windows x64 CPython
  stable-ABI reader built for Python 3.11 and newer. A missing or incompatible
  reader can use the Python fallback, which may be slower. The release
  build requires the reader, but runtime compatibility with every Blender build is not
  guaranteed. Python and CUDA runtimes are not bundled. 原生读取器只随目标包提供，
  缺失或不兼容时会回退 Python；并不捆绑 Python 或 CUDA 运行时。
  Older API 2 readers can lack the new collection or area methods; those calls use
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
  functional cases, not many-island stress coverage. Final 1.7.55 native-reader
  and Python-reader modal runs each passed 24 gestures and 144 move samples;
  active drawing reported no errors and confirmed operations covered undo/redo.
  工厂 GUI 通过了事件注入式 modal 验收，不能代替人工鼠标或用户安装环境验收。

Report a minimal reproduction with Blender version, extension commit, native
build metadata, selection-sync state and parameters. Use a synthetic or
shareable mesh, remove file paths and personal data, and distinguish a wrong
result from a safe rejection. 请提供可公开的最小复现；不要上传私人场景或完整日志路径。
