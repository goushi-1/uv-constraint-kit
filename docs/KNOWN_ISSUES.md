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
  array/cache experiments have not demonstrated stable global gains.
  不能将 C++／GPU 内核计时当成 Blender 完整操作提速。
- **Native platforms.** Windows x64 / MSVC is the supported build path.
  macOS/Linux native engines are not supplied or validated.
- **GPU scope.** Current CUDA source targets NVIDIA 8.9+ / CUDA driver 12.4+;
  GPU execution is optional and is not used by every path. No new large-GPU
  acceptance is implied by this first source publication.
- **Anchors and selection.** Pins and shape constraints can conflict with new
  edge directions. Seam sides require UV-aware selection; Selection Sync can
  select both mesh-edge UV copies.
- **Acceptance boundaries.** Synthetic headless tests do not prove GUI gestures,
  all third-party add-on interactions, persistent user preferences, or natural
  shutdown. Manual confirmation in a disposable GUI session remains useful.

Report a minimal reproduction with Blender version, extension commit, native
build metadata, selection-sync state and parameters. Use a synthetic or
shareable mesh, remove file paths and personal data, and distinguish a wrong
result from a safe rejection. 请提供可公开的最小复现；不要上传私人场景或完整日志路径。
