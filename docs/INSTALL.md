# Installation / 安装说明

## English

1. Use Blender 5.2 or newer. Windows x64 is the currently tested native target.
2. Build an extension ZIP using [BUILD.md](BUILD.md), or use a release asset
   explicitly labeled as an installable extension. GitHub's source archive has
   a repository wrapper and is not an extension ZIP.
3. In Blender, open Preferences → Get Extensions → menu → Install from Disk.
   Select the ZIP, install it, then enable UV Constraint Kit.
4. Edit a mesh with an active UV map. Open the UV Editor sidebar → UV Constraint.
5. Select UV edges, mark a constraint, and use Optimize. F9 adjusts the previous
   operation. Auto optimize can be toggled beside Optimize.

The [1.7.49 release](https://github.com/goushi-1/uv-constraint-kit/releases/tag/v1.7.49)
contains the CPU engine, optional GPU engine and native mesh reader in one ZIP.
Eligible large solves use NVIDIA compute
capability 8.9+ / CUDA driver 12.4+ automatically; unsupported hardware uses CPU.
Installing the prebuilt ZIP does not require the CUDA Toolkit.

For a seam-side selection, disable UV Selection Sync and select the desired
UV edge copy. Visibility and selection affect the operation's scope.

Do not install multiple copies of the same extension ID. Restart Blender after replacing a loaded native
engine: Windows can keep a DLL mapped until its process exits.

Without a native engine, the Python implementation remains available but can
be substantially slower. This is visible through build choices, not an extra
runtime solver selector.

## 简体中文

1. 使用 Blender 5.2 或更新版本；原生引擎目前验证 Windows x64。
2. 按[构建说明](BUILD.md)生成安装包，或下载明确标注为扩展安装包的 Release
   附件。GitHub 的源码 ZIP 不能直接作为扩展安装。
3. 偏好设置 → 获取扩展 → 菜单 → 从磁盘安装，选择 ZIP 后启用。
4. 进入带 UV 贴图网格的编辑模式，打开 UV 编辑器 N 面板 → UV Constraint。
5. 选边并标记约束，执行优化。F9 调整上次操作参数；优化旁的开关控制自动优化。

[1.7.49 安装包](https://github.com/goushi-1/uv-constraint-kit/releases/tag/v1.7.49)
在同一个 ZIP 内包含 CPU、可选 GPU 引擎和原生网格读取器。符合条件的大型求解自动使用 NVIDIA 计算能力 8.9+、
支持 CUDA 12.4+ 驱动的 GPU；其它硬件使用 CPU。安装预构建包无需 CUDA Toolkit。

只处理接缝一侧时，关闭 UV 选择同步，再选择目标 UV 边。可见性与选区决定作用范围。

不同时安装相同扩展 ID 的多个副本；替换已加载原生引擎后
重启 Blender，避免 Windows 保持旧 DLL 映射。仅 Python 包可用，但运行可能明显较慢。
