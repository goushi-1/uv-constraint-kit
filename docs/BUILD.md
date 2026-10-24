# Build from source / 从源码构建

The 1.7.49 Windows x64 package includes CPU and GPU engines plus the CPython
stable-ABI reader. Its recorded package SHA-256 is
`4a5116593650ab6d69e29ed55fdd4d25d47002561cca61f7d868444be3031784` (44 files).

## Requirements / 工具

- Python 3.11+ for the repository scripts; NumPy for numerical tests.
- Optional reader build: CPython 3.11+ development headers and the Windows
  x64 stable-ABI import library `python3.lib`. These are build dependencies;
  Blender supplies Python at runtime.
- CMake 3.20+, Visual Studio 2022 with C++ desktop tools and Windows SDK.
- Blender 5.2+ for host tests; Blender's bundled Python already includes NumPy.
- GPU build from source: CUDA Toolkit / NVRTC **12.4**, installed separately.
  The current kernel target is `compute_89`; runtime GPU use requires an NVIDIA
  GPU with compute capability 8.9+ and a driver supporting CUDA 12.4+.
- The prebuilt CPU+GPU extension does not require the CUDA Toolkit. Unsupported
  hardware or GPU initialization failures use the CPU path.

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

## CPU+GPU package / CPU+GPU 正式包

Build the CPU and GPU engines in a separate directory so the existing CPU build
remains intact. Set `CUDA_PATH` to your Toolkit, or pass the include and NVRTC
directories explicitly:

```sh
python scripts/build_native.py --build-dir build/native_gpu --gpu --cuda-include "$CUDA_PATH/include" --nvrtc-dir "$CUDA_PATH/bin"
python tests/test_native.py --engine-dir build/native_gpu/bin/windows-x64 --gpu
python scripts/package.py --native-dir build/native_gpu/bin/windows-x64 --require-gpu --output dist/uv_constraint_kit-cpu-gpu.zip
```

In PowerShell, environment references use `$env:CUDA_PATH`; in cmd use
`%CUDA_PATH%`. Directory values may also be passed literally.

The GPU builder uses NVRTC with C++17, disabled FMA/FTZ and precise division
and square root; the host library dynamically loads the installed NVIDIA
driver. Its generated `gpu.json` identifies the DLL, PTX, source and build
key. `--require-gpu` fails packaging unless both GPU binaries and the metadata
have passed the package checks. Do not distribute CUDA Toolkit headers, NVRTC
DLLs, or system driver DLLs as if they were this project's GPL source.

GPU 构建使用 NVRTC 12.4；运行时通过系统 Driver API 加载。符合条件的大网格会自动分派
到 GPU；小网格、部分特殊求解和 GPU 不可用时使用 CPU。GPU 失败会从原始输入回退。
预构建扩展无需安装 Toolkit；其它硬件走 CPU 路径。

## Optional CPython stable-ABI mesh reader / 可选 CPython stable ABI 读取器

The optional Windows `.pyd` reader captures BMesh data through Blender's public
Python API and passes numeric buffers to the existing engines; it does not use
private BMesh memory layouts. `mesh_reader.cpp` targets the CPython limited API
with `Py_LIMITED_API=0x030B0000` (minimum Python 3.11). The package does not
include Python DLLs, a standard library, or any other Python runtime files.
Blender supplies the interpreter at runtime. If the reader is unavailable, the
Python implementation remains available as fallback unless package validation
is explicitly told to require the reader.

Provide a Python development include directory containing `Python.h` and the
stable-ABI import library named exactly `python3.lib`:

```sh
python scripts/build_native.py --build-dir build/native_gpu_reader --gpu --reader --python-include "$PYTHON_INCLUDE" --python-library "$PYTHON_LIBRARY" --cuda-include "$CUDA_PATH/include" --nvrtc-dir "$CUDA_PATH/bin"
python tests/test_native.py --engine-dir build/native_gpu_reader/bin/windows-x64 --gpu
python scripts/package.py --native-dir build/native_gpu_reader/bin/windows-x64 --require-gpu --require-reader --output dist/uv_constraint_kit-1.7.49.zip
```

`--reader` requires both `--python-include` and `--python-library`; the latter
must be `python3.lib`, not a versioned import library such as `python313.lib`.
These arguments are invalid without `--reader`. `--require-reader` requires
validated `reader.json` metadata and a matching `.pyd`, preventing a release
package from silently omitting the reader. `--require-gpu` and
`--require-reader` check package presence and metadata; they do not prove
compatibility with every Blender build or machine.

PowerShell uses `$env:PYTHON_INCLUDE`, `$env:PYTHON_LIBRARY`, and
`$env:CUDA_PATH`; cmd uses `%PYTHON_INCLUDE%`, `%PYTHON_LIBRARY%`, and
`%CUDA_PATH%`. Python and CUDA SDK/runtime files are not distributed.

## Host tests / 宿主测试

The numeric safety test runs outside Blender and needs NumPy:

```sh
python tests/test_safety.py
```

An optional `--output test-results/safety.json` writes a JSON report; the
default is a short console result without creating a report file.

```sh
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_quad_grid_host.py
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_host.py
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_selection_host.py
```

These background commands create disposable synthetic meshes and do not save a
scene or preferences. They do not test GUI undo or modal mouse-event handling;
the factory GUI tests below cover those paths.

Run the GPU host check against the extracted final extension ZIP on a supported
NVIDIA system. For a reader-enabled package, confirm `reader.json` and the
`.pyd` are present and inspect reader diagnostics in the report. This
deliberately uses Blender's factory **GUI** process (no
`--background`) and exercises natural backend dispatch:

```sh
blender --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_gpu_host.py -- --addon-root extracted/uv_constraint_kit --output test-results/gpu-host.json
```

The reader-enabled packaged-host report covers nine operations, including
natural GPU dispatch, and passed on the recorded Blender 5.2.3 host. Native
numerical checks also exercised real GPU execution and bad-PTX fallback to CPU.
These synthetic checks verify routing and outputs; they are not a performance
multiplier benchmark and do not cover every host.

## Factory GUI operation and modal-event checks / 工厂 GUI 操作及模态事件检查

Run the confirmed G/R/S operation and undo/redo checks once with each reader
route:

```sh
blender --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_gui_host.py -- --addon-root extracted/uv_constraint_kit --output test-results/gui-native.json --reader native
blender --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_gui_host.py -- --addon-root extracted/uv_constraint_kit --output test-results/gui-python.json --reader python
```

The modal test requires Blender's event-simulation option. It injects queued
`MOUSEMOVE` events into the disposable factory window and samples G/R/S while
the native transform remains active; it does not move the operating-system
mouse. Run both reader routes:

```sh
blender --enable-event-simulate --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_modal_host.py -- --addon-root extracted/uv_constraint_kit --output test-results/modal-native.json --reader native
blender --enable-event-simulate --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_modal_host.py -- --addon-root extracted/uv_constraint_kit --output test-results/modal-python.json --reader python
```

The accepted run covered 24 gesture combinations per route: UV sync on/off,
whole/partial selection, G/R/S, and confirm/cancel. Each gesture sampled six
mouse-move events. Confirmed gestures were checked through undo and redo;
canceled gestures were checked for restoration. These are automated factory
window events, not hand-operated mouse tests or validation of a user's installed
Blender environment. No user scene or preferences are saved.

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
