# Known issues / 已知问题

- **Preview status.** The public quadrification replacement has a narrower
  supported topology than general UV tools. An integer-grid chart can contain
  regular grid holes, but poles, inconsistent cycles, non-manifold links,
  disconnected inputs and non-quads are rejected without a partial write.
  不支持的拓扑会安全拒绝；不保证适用任意 UV 岛或与其它插件的结果一致。
- **No RizomUV equivalence.** This repository does not contain its proprietary
  solver. Strong constraints can have no nonfolding feasible solution. Recovery
  and rollback can leave the UVs unchanged; inspect the operation's outcome.
  约束不相容时可能保持原 UV，不承诺任何输入都能成功展开。
- **Performance varies.** Mesh extraction, topology, recovery, safety checks,
  overlays and solver iterations contribute to complete operation time. Recent
  array/cache experiments have not demonstrated stable global gains. The GPU
  host cases verify backend routing and results, not a performance multiplier.
  不能将 C++／GPU 内核计时或少量路由用例当成 Blender 完整操作提速。
- **Native platforms.** Windows x64 / MSVC is the supported build path.
  macOS/Linux native engines are not supplied or validated.
- **GPU scope.** The preview includes an optional CUDA engine for eligible large
  solves. It targets NVIDIA compute capability 8.9+ and drivers supporting CUDA
  12.4+. The prebuilt extension does not need the CUDA Toolkit; unsupported
  hardware and some special solve paths use CPU. GPU routes, fallback and
  selected CPU/GPU results passed the published checks, but this does not cover
  every GPU model, driver, mesh or Blender workflow. GPU 路径已按公开验收记录
  检查，但不代表所有显卡、驱动、网格或工作流都已通过。
- **Anchors and selection.** Pins and shape constraints can conflict with new
  edge directions. Seam sides require UV-aware selection; Selection Sync can
  select both mesh-edge UV copies.
- **Acceptance boundaries.** The GPU route check ran in a factory GUI process
  and exited normally. Real user mouse gestures and undo behavior, all third-
  party add-on interactions, and persistent user preferences remain unverified.
  工厂 GUI 验收不等于真实用户交互、撤回或所有第三方插件兼容性验收。

Report a minimal reproduction with Blender version, extension commit, native
build metadata, selection-sync state and parameters. Use a synthetic or
shareable mesh, remove file paths and personal data, and distinguish a wrong
result from a safe rejection. 请提供可公开的最小复现；不要上传私人场景或完整日志路径。
