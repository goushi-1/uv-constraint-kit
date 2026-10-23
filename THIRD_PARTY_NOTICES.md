# Third-party notices / 第三方声明

## UniV UV Symmetrize

`uv_constraint_kit/symmetry.py` contains an adaptation of UniV UV Symmetrize
by Oxicid, licensed under **GPL-3.0-or-later**.

- Original project: <https://github.com/Oxicid/UniV>
- Reference version: <https://github.com/Oxicid/UniV/tree/v4.0.0>
- Retained source notice: `SPDX-FileCopyrightText: 2025 Oxicid`.
- The upstream v4.0.0 extension manifest additionally records `2026 Oxicid`.
- License text: [GNU GPL version 3](LICENSE).

The adaptation replaces integration with this extension's selection, panels,
translations, constraints, and undo; preserves face indices; corrects face-size
matching; and adds cursor/custom centers and live links. The adapted component
and this publication copy contain modifications made through 2026-10-23.
UniV does not need to be installed. Its branding, UI assets and complete
extension are not included.

`symmetry.py` 保留 UniV 的原始版权及 GPL 声明，集成部分经过修改。
无需安装 UniV，也未附带其完整插件或美术资源。

## Runtime and build dependencies

Blender supplies `bpy`, `bmesh`, `mathutils` and NumPy at runtime. These
dependencies are not vendored here and retain their own licenses.

Optional CUDA compilation uses a separately installed NVIDIA CUDA Toolkit and
NVRTC. NVIDIA SDK headers, compilers, NVRTC libraries and the system display
driver are **not** included or relicensed by this repository. Obtain them from
NVIDIA under the applicable terms:
<https://docs.nvidia.com/cuda/archive/12.4.1/eula/index.html>.

CUDA kernels and engine source in this repository carry this project's GPL
license; that statement does not grant rights to NVIDIA's SDK or driver.

本仓库不附带或重新授权 Blender、NumPy、NVIDIA SDK、NVRTC 或驱动。

## Independent quadrification

`quad_grid.py` implements integer-lattice consistency and mesh-edge spacing
for quadrilateral charts. The former third-party quadrification adaptation
is excluded from this repository and its Git history. This implementation
does not require a commercial UV extension.

四边形化使用本仓库独立编写的整数格算法；旧的第三方改编模块不在公开树中。
