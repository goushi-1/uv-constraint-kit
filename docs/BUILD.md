# Build from source / 从源码构建

## Requirements / 工具

- Python 3.11+ for the repository scripts; NumPy for numerical tests.
- CMake 3.20+, Visual Studio 2022 with C++ desktop tools and Windows SDK.
- Blender 5.2+ for host tests; Blender's bundled Python already includes NumPy.
- Optional GPU: CUDA Toolkit / NVRTC **12.4**, separately installed by you.
  The current kernel target is `compute_89`; the loader requires NVIDIA compute
  capability 8.9+ and a driver advertising CUDA 12.4+.

当前原生构建只支持 Windows x64 / MSVC。CUDA/NVRTC、SDK 头文件和驱动不随仓库提供。
其它操作系统的 Python 回退存在，但未作为支持平台验收。

## CPU

From the repository root / 在仓库根目录执行：

```sh
python scripts/build_native.py
python tests/test_native.py --engine-dir build/native/bin/windows-x64
python scripts/package.py --native-dir build/native/bin/windows-x64 --output dist/uv_constraint_kit.zip
```

The build script invokes CMake with `Visual Studio 17 2022`, x64, C++17,
`/O2 /fp:precise /MT`, and generates checksum-named DLLs plus `engine.json`.
The loader verifies SHA-256 and the C ABI. Copying an arbitrary DLL into the
extension folder does not satisfy these checks.

构建只写 `build/`；不修改用户 Blender 安装。安装包把 `blender_manifest.toml`
放在 ZIP 根，使用明确白名单，排除缓存、日志、测试和构建来源记录。

## Optional CUDA / 可选 CUDA

With `CUDA_PATH` pointing to your Toolkit, or explicit directories:

```sh
python scripts/build_native.py --gpu --cuda-include "$CUDA_PATH/include" --nvrtc-dir "$CUDA_PATH/bin"
python tests/test_native.py --engine-dir build/native/bin/windows-x64 --gpu
python scripts/package.py --native-dir build/native/bin/windows-x64 --output dist/uv_constraint_kit_gpu.zip
```

In PowerShell, environment references use `$env:CUDA_PATH`; in cmd use
`%CUDA_PATH%`. Directory values may also be passed literally.

The GPU builder uses NVRTC with C++17, disabled FMA/FTZ and precise division
and square root; the host library dynamically loads the installed NVIDIA
driver. Its generated `gpu.json` identifies the DLL, PTX, source and build
key. Do not distribute CUDA Toolkit headers, NVRTC DLLs, or system driver DLLs
as if they were this project's GPL source.

GPU 构建使用 NVRTC 12.4；运行时通过系统 Driver API 加载。自动分派门槛目前为
32,768 个三角形，且自动迭代开启或迭代预算至少 50。部分接触求解只走 CPU。
失败会从原始输入回退，不保证 GPU 对每个场景都更快。

## Host tests / 宿主测试

```sh
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_quad_grid_host.py
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_host.py
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_selection_host.py
```

These commands create disposable synthetic meshes. They do not save a scene
or preferences. They do not prove real mouse gesture or GUI undo/redo behavior;
use [VALIDATION.md](VALIDATION.md) to distinguish those acceptance levels.

## README examples / 说明图

```sh
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python scripts/generate_examples.py
```

This regenerates `docs/images/*.svg` from actual add-on operations on synthetic
meshes, with a shared before/after scale. It requires a factory background
process with no input scene. The images explain behavior; they are not benchmarks.

此命令会覆盖仓库内功能对照图，不保存场景或偏好；图中布局来自插件实际求解结果。

## Reproducibility / 可复现范围

Source inputs, build scripts, metadata and artifact checksums are reproducible
inputs and records. PE timestamps, compiler/SDK versions and local CUDA caches
mean **bit-identical builds are not promised**. `build/`, provenance and raw test
outputs are ignored and must not be committed: they can contain machine paths.

源码和构建步骤可重跑，但不承诺跨编译器、SDK 和机器逐字节一致。提交源码前可运行
`python scripts/check_public_tree.py` 检查公开文件，不要把本机 provenance/log 上传。
