# UV Constraint Kit

[English](README.md)

Blender UV 边与形状约束工具。Python 负责界面、选区、撤回和叠加层，
C++17 引擎负责数值计算；仓库同时包含可选 CUDA Driver API 引擎源码。

**当前为 1.7.43 实验分支。** 在主分支 1.7.42 源码预览的基础上启用受保护
数值数组试算，尚未证明稳定的整体提速。默认预览请使用 `main`；两个分支均含
独立四边形化和选择同步修复。详见[版本与验收状态](docs/VALIDATION.md)。

## 功能

- 按 UV 接缝单侧标记水平、垂直、直线约束。
- 优化时保留固定点和面积，检查失败后恢复原 UV。
- 对选中边界圆形化、方形化，保存圆形和方形约束。
- 对称、多组实时联动，以及解除联动时复制约束。
- 可配置颜色的叠加层，中英文界面及 F9 参数。
- 对兼容的纯四边形 UV 岛进行独立整数格四边形化。

这是独立求解实现，不包含 RizomUV 的专有引擎，不保证结果、收敛或性能一致。
本项目与 Rizom-Lab、Blender 或依赖项目作者没有隶属关系。

## 安装入口

需要 **Blender 5.2 或更新版本**；本次公开版宿主验证使用 Blender 5.2.3。
原生引擎的构建目标为 **Windows x64 / MSVC**。

```sh
python scripts/build_native.py
python scripts/package.py --native-dir build/native/bin/windows-x64 --output dist/uv_constraint_kit.zip
```

在 Blender「偏好设置 → 获取扩展 → 从磁盘安装」中安装生成的 ZIP 并启用。
进入编辑模式，在 UV 编辑器 N 面板的「UV Constraint」分类操作，F9 调整参数。

不传 `--native-dir` 可打包仅 Python 的扩展，但运行较慢。GitHub 的源码下载
ZIP 不是可直接安装的 Blender 扩展包。详见[安装说明](docs/INSTALL.md)和
[构建说明](docs/BUILD.md)。

## 测试与限制

```sh
python tests/test_native.py --engine-dir build/native/bin/windows-x64
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_quad_grid_host.py
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_host.py
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_selection_host.py
```

测试只生成合成网格，请在独立工厂配置进程中运行；不保存用户场景或偏好。
原生数值测试需要 NumPy，可使用 Blender 自带 Python。

使用前请阅读[已知问题](docs/KNOWN_ISSUES.md)。新四边形化对极点、不一致拓扑
闭环和混合多边形可能安全拒绝。GPU 并不加速所有步骤，不能用局部引擎计时
代替完整操作计时。

## 版本区分

- `main`：1.7.42 源码预览，含独立四边形化。
- `experimental/numeric-trials`：1.7.43 实验分支，受保护求解的中间坐标留在
  数组中，尚未证明稳定的整体提速。
- 历史本地正式版本与隔离基线见[验收记录](docs/VALIDATION.md)，原始资产、
  用户场景与历史测试转储不进入仓库。

## 许可

GNU GPL **3.0 或后续版本**，不提供担保。见 [LICENSE](LICENSE) 及
[第三方声明](THIRD_PARTY_NOTICES.md)。UniV 改编对称部分保留版权与许可；
四边形化为独立实现，旧第三方改编模块不公开。
