# SPDX-License-Identifier: GPL-3.0-or-later
"""Chinese terms for UV Constraint Kit -- the single source of truth.

Deliberately a plain dict in a module with **no imports at all**, for two
reasons:

* `i18n.py` turns it into a `bpy.app.translations` catalog, so the add-on's own
  UI follows Blender's interface language (and therefore F10).
* `_tools/merge_use_cn_terms.py` can read it with a plain Python interpreter and
  merge the same pairs into the `use_cn_666` add-on's `custom_terms.json`, so its
  label-replacement pass agrees with ours instead of fighting it.

Keys are the English strings that appear in the code; values are the Chinese the
user sees when F10 is on Chinese.

Rules for a key, both learned from `use_cn_666`'s 17280-entry term table:

* **Whole phrases only, never bare words.** That table already maps
  ``"Margin" -> "保证金"`` and ``"Overlay" -> "重叠"`` -- both wrong in a UV
  context. A bare msgid either collides with a translation written for something
  else or silently overrides it. Hence ``"Unwrap margin"`` and not ``"Margin"``.
* **Keep the ``%d`` / ``%.2f`` placeholders byte-identical.** `t()` is applied to
  the template and the formatting happens afterwards, so a template that gained
  or lost a placeholder raises at report time instead of falling back.

Keeping the keys byte-identical to the literals in `ui.py` / `ops.py` /
`prefs.py` matters for the same reason a typo matters anywhere: it produces a
control that stays English with no error. `_tools/i18n_check.py` asserts every
key here is really used, and that every user-visible string in the source is
covered.
"""

TERMS = {
    'no safe constrained layout found while preserving fixed UV points': '保留当前固定点时未找到安全的约束结果',
    'Conflicting fixed UV coordinates: %s': '冲突固定点的 UV 坐标：%s',
    'Fixed UV points in target: %d': '目标中固定的 UV 点：%d',
    'Symmetry center X': '对称中心 X',
    'Symmetry center Y': '对称中心 Y',
    'Invalid symmetry center': '对称中心坐标必须为有限数值',
    'Live symmetry overlay': '实时对称叠加',
    'Linked target fill colour': '联动目标填充颜色',
    'Linked target fill opacity': '联动目标填充不透明度',
    'Quad face shape': '四边形面形状',
    'Selected UV islands already have the requested shape': '选中的 UV 岛屿已是目标形状',
    'Quad face shape: Per Face': '各面比例',
    'Quad face shape: Active Face': '起始面比例',
    'Quad face shape: Square': '正方形',
    'Average quad shape': '平均面形状',
    'Skip islands with non-quads': '跳过含非四边形的岛屿',
    'Island contains non-quad faces': '岛屿包含非四边形面，已跳过',
    'Quad has a zero-length mesh edge': '四边形存在零长度模型边',
    'Symmetrize': '对称',
    'Live symmetry link': '实时联动',
    'Keep target UV corners mirrored when source corners change': '修改源侧 UV 时，目标侧按保存的轴与中心实时镜像',
    'Stop symmetry link': '解除联动',
    'Keep the current UVs and stop source-to-target updates': '保留当前 UV，停止源侧到目标侧的更新',
    'Live symmetry linked; edit the source side to update the target': '已建立实时联动；编辑源侧会更新目标侧',
    'Live symmetry corner matching exceeded the threshold': '实时对称的顶点匹配超出阈值，已还原',
    'Live symmetry has ambiguous target corners': '实时对称的目标点对应不唯一，已还原',
    'Live symmetry has cyclic corner links': '实时对称存在循环对应，已还原',
    'Live symmetry has ambiguous linked corners': '实时对称的连接点对应不唯一，已还原',
    'Live symmetry data exceeds mesh storage capacity': '网格无法保存完整实时对称信息，已还原',
    'Circularize Island': '圆形化',
    'Circularize the boundary of each selected UV island and relax its interior': '将选中 UV 岛屿的外边界圆形化并优化内部',
    'Selected Boundary Only': '仅圆形化选中边界',
    'Circularize complete selected UV boundary loops and keep the rest of the island boundary fixed': '将完整选中的 UV 边界环圆形化，并保持岛屿其余边界不动',
    'Spacing': '间距方式',
    'Geometry': '几何长度',
    'Even': '均匀分布',
    'Keep the original UV edge length proportions': '保持原 UV 边长比例',
    'Use mesh edge lengths': '使用模型边长',
    'Space boundary points evenly': '沿边界均匀分布',
    'Respect Pins': '遵循固定点',
    'Prevent Overlap': '防止重叠',
    'Circularized %d islands / %d faces': '已圆形化 %d 个岛屿 / %d 个面',
    'Select a closed UV boundary loop first': '请先选中一个闭合的 UV 边界环',
    'Selected UV boundary must be complete, closed, and non-branching': '选中的 UV 边界必须完整闭合且不能分叉',
    'Selected UV boundary must be a complete closed loop': '选中的 UV 边界必须构成完整闭环',
    'The selected boundary has zero area': '选中的边界面积为零',
    'Selected Only': '仅选中',
    'Shape selected closed UV edge loops and optimize the rest of their islands': '仅对选中的闭合边环进行形状化，并优化岛屿其余部分',
    'Select a closed UV edge loop first': '请先选中闭合的边环',
    'Selected UV edges must form complete non-branching loops': '选中的边必须构成完整闭环，不能分叉',
    'Circular colour': '圆形约束颜色',
    'Rectangular colour': '方形约束颜色',
    'Overlay colour for circular constraint lines': '圆形约束线条的叠加颜色',
    'Overlay colour for rectangular constraint lines': '方形约束线条的叠加颜色',
    'The new shape conflicts with an existing shape constraint': '新形状与已有形状约束冲突',
    'Invalid or outdated shape constraints; remove the affected constraints first': '形状约束数据失效，请撤销拓扑修改后解除受影响的约束',
    'Shape constraints no longer match the mesh topology': '形状约束与当前网格拓扑不一致',
    'Invalid shape constraint data': '形状约束数据无效',
    'A shape constraint was split by a UV seam; remove it before optimizing': '形状约束被 UV 接缝拆分，请先解除该约束',
    'A shape constraint collapsed; restore or remove it first': '形状约束已塌缩，请先恢复或解除该约束',
    'Selected shapes share vertices with an existing shape constraint': '选中闭环与已有形状约束共用顶点',
    'Select the whole island containing the shape constraint': '请选中形状约束所在的完整岛屿',
    'Shape constraints conflict at a shared vertex': '形状约束在共用顶点处冲突',
    'Pinned UV points conflict with a shape constraint': '固定点与形状约束冲突',
    'Shape constraints are outdated; deselect UV edges before clearing the invalid shapes': '形状约束已失效，请先取消选边，再解除失效的形状约束',
    "Horizontal, vertical and aligned colours default to RizomUV's own preferences": '水平、垂直和对齐约束的默认颜色来自 RizomUV 偏好设置',
    'The target boundary would flip or collapse a triangle made only of fixed vertices': '目标边界会使完全由固定边界点组成的三角形翻转或塌缩，请先调整该处拓扑',
    'Rectify Island': '方形化',
    'Rectify selected UV islands as rectangles or squares': '将选中的 UV 岛屿整理为矩形或正方形',
    'Rectify complete selected UV edge loops and optimize the rest of their islands': '将选中的闭合 UV 边环方形化，并优化岛屿其余部分',
    'Target shape': '目标形状',
    'Target shape: Rectangle': '矩形',
    'Target shape: Square': '正方形',
    'Fit a rectangle': '整理为矩形',
    'Fit a square': '整理为正方形',
    'Boundary spacing': '边界间距方式',
    'Boundary spacing: Geometry': '按模型边长',
    'Boundary spacing: UV': '按原 UV 边长',
    'Boundary spacing: Even': '均匀分布',
    'Keep original UV edge proportions': '保持原 UV 边长比例',
    'Orient to axis': '对齐坐标轴',
    'Interior iterations': '内部迭代次数',
    'Respect pins': '遵循固定点',
    'Prevent overlap': '防止重叠',
    'Rectified %d islands / %d faces': '已方形化 %d 个岛屿 / %d 个面',
    'Quadrify Island Grid': '四边形化',
    'Quadrify selected UV islands': '将选中的 UV 岛屿四边形化',
    'Quad edge spacing': '四边形边长方式',
    'Quad edge spacing: Length Average': '平均长度',
    'Quad edge spacing: Length': '模型边长',
    'Quad edge spacing: Even': '均匀分布',
    'Use average mesh edge lengths': '使用平均模型边长',
    'Space edges evenly': '均匀分配边长',
    'Quadrified %d islands / %d faces': '已四边形化 %d 个岛屿 / %d 个面',
    'Preserve existing UV connections': '保持原 UV 连接',
    'Quadrification would split existing UV connections': '四边形化需要断开原 UV 连接，已还原；如需允许断开，可在 F9 关闭保持原 UV 连接',
    'New UV boundaries: %d': '新增 UV 断口：%d',
    'UV island has zero area': 'UV 岛屿面积为零',
    'Seed quad has zero UV area': '起始四边形的 UV 面积为零',
    'Seed quad has a zero-length 3D side': '起始四边形存在零长度模型边',
    'Seed quad is unavailable': '起始四边形不可用',
    'Island has no visible quad face': '岛屿中没有可见的四边形面',
    'Follow Active Quads was cancelled': '四边形展开已取消',
    'Follow Active Quads split the source UV island': '四边形展开会断开原 UV 岛屿',
    'Pinned UV conflicts with quadrification': '固定点与四边形化结果冲突',
    'Quadrification changed a constraint mark': '四边形化改变了约束标记，已还原',
    'Quadrification reversed a source triangle': '四边形化会翻转原 UV 面，已还原',
    'Area or centroid restoration failed': '无法保持原岛屿的面积或中心，已还原',
    'Quadrification failed the UV safety check': '四边形化结果未通过质量检查，已还原',
    'Quadrification violates a marked constraint': '四边形化结果与约束冲突，已还原',
    'Mesh has no UV map': '网格没有 UV 贴图',
    'Object is not an edit-mode mesh': '对象不是处于编辑模式的网格',
    'Could not prepare UV island': '无法读取 UV 岛屿',
    'No candidate quad seed': '未找到可用的起始四边形',
    'Select a point, edge, or face inside a UV island first': '请先在 UV 岛屿内选中一个点、边或面',
    'The island boundary is branched or non-manifold': '岛屿边界存在分叉或非流形结构',
    'The island has no closed UV boundary': '岛屿没有闭合的 UV 边界',
    'The island boundary has zero area': '岛屿边界面积为零',
    'The island boundary contains a zero-length edge': '岛屿边界包含零长度边',
    'The island boundary cannot form a safe circle': '岛屿边界无法构成安全的圆形',
    'The island contains disconnected UV vertices': '岛屿包含不连通的 UV 顶点',
    'The circular interior solve did not converge': '圆形化内部求解未能收敛',
    'The interior solve did not converge': '内部优化未能收敛',
    'Rectification needs at least four boundary vertices': '方形化需要至少四个边界顶点',
    'Pinned boundary vertices conflict with the target shape': '固定的边界顶点与目标形状冲突',
    'The target layout conflicts with pins or hole boundaries': '目标布局与固定点或孔洞边界冲突',
    'The target shape conflicts with constraints or would overlap': '目标形状与约束冲突或会产生重叠',
    'The target shape could not preserve the island area': '目标形状无法保持岛屿面积',
    'The island contains hidden faces': '岛屿包含隐藏面',
    'Pinned boundary vertices conflict with the circle': '固定的边界顶点与圆形化结果冲突',
    'The circular layout conflicts with pins or hole boundaries': '圆形布局与固定点或孔洞边界冲突',
    'The circle conflicts with constraints or would overlap': '圆形化结果与约束冲突或会产生重叠',
    'The circle could not preserve the island area': '圆形化无法保持岛屿面积',
    'Mirror UVs between faces paired across the local mesh symmetry axis': '按模型局部对称轴匹配面，并将一侧 UV 镜像到另一侧',
    'UV Axis': 'UV 轴向',
    '3D Axis': '模型轴向',
    'Flip UV Axis': '反转 UV 方向',
    'By Cursor': '以光标为中心',
    'Unlink': '断开连接',
    'Symmetry Threshold': '对称匹配阈值',
    'Unlink UV Corners': '断开 UV 连接',
    'Use UV Cursor': '以 UV 光标为中心',
    'UV cursor requires the UV editor': '以光标为中心需要在 UV 编辑器操作',
    'Finish the current transform before symmetrizing': '请完成当前变换后再执行 UV 对称',
    'No selected symmetric face pairs found': '未找到选中面的对称配对',
    'Symmetrized %d face pairs; %d source faces unmatched': '已对称 %d 对面；%d 个源面未匹配',
    "UV sync: seam edges affect both UV sides": "UV 同步开启：接缝边会作用于两侧 UV",
    "Unsafe result rejected; original UV layout restored": "优化结果未通过安全检查，已还原原始 UV 布局",
    # -- panels ------------------------------------------------------------
    "UV Constraint": "UV 约束",
    "Enter Edit Mode First": "请先进入编辑模式",
    "No constrained edges yet": "尚无约束边",
    "%d constrained edges": "约束边共 %d 条",
    "Horizontal: %d": "水平：%d",
    "Vertical: %d": "垂直：%d",
    "Aligned: %d": "对齐：%d",

    # -- operators ---------------------------------------------------------
    "Constrain Edge(s) Horizontally": "水平约束边",
    "Constrain Edge(s) Vertically": "垂直约束边",
    "Constrain Edge(s) Aligned": "约束边保持对齐",
    "Unconstrain Edge(s)": "解除约束边",
    "Unconstrain All Edges": "解除所有边约束",
    "Optimize": "优化",
    "Fix boundary": "固定边界条件",

    # -- operator tooltips -------------------------------------------------
    "Constrain the selected edges so they stay horizontal during unwrapping "
    "and optimization (RizomUV: Constrain Edge(s) Horizontally)":
        "约束所选边，使其在展开与优化过程中保持水平"
        "（RizomUV: Constrain Edge(s) Horizontally）",

    "Constrain the selected edges so they stay vertical during unwrapping and "
    "optimization (RizomUV: Constrain Edge(s) Vertically)":
        "约束所选边，使其在展开与优化过程中保持垂直"
        "（RizomUV: Constrain Edge(s) Vertically）",

    "Constrain the selected edges to stay a straight line along their current "
    "trend instead of being forced horizontal or vertical "
    "(RizomUV: Constrain Edge(s) Aligned)":
        "约束所选边，使其沿当前趋势方向保持为一条直线，"
        "而不是强行拉成水平或垂直（RizomUV: Constrain Edge(s) Aligned）",

    "Remove the constraints from the selected edges; with nothing selected, "
    "from every edge (RizomUV: Unconstrain Edge(s))":
        "移除所选边上的约束；若没有选中任何边，则移除全部边的约束"
        "（RizomUV: Unconstrain Edge(s)）",

    "All edges": "所有边",
    "Ignore the selection and drop every edge constraint":
        "忽略选区，直接解除全部边约束",

    "Re-solve the UV island they belong to, with the marked constraint edges "
    "held as hard constraints, to reduce distortion. The island's position, "
    "size and texel density are fitted back, so it neither grows nor lands on "
    "top of another island (RizomUV: Optimize)":
        "重解这些边所在的 UV 岛屿：让已标记的约束边作为硬约束参与展开求解，以降低形变。"
        "岛屿的位置、尺寸与纹素密度会被拟合还原，"
        "因此既不会变大，也不会压到别的岛屿上（RizomUV: Optimize）",

    "Hold the island's border UVs in place while optimizing, so the outline "
    "stays exactly put and only the interior is relaxed. Off by default. "
    "Turning it on equals RizomUV's 'Free selection border' left off, i.e. its "
    "border vertices are pinned and do not move":
        "优化时把岛屿边界上的 UV 钉住不动，只松弛内部，轮廓因此完全保持不变。"
        "默认关闭。打开后等价于 RizomUV 中不启用 Free selection border —— "
        "也就是边界顶点被钉住、完全不动",

    # -- reports -----------------------------------------------------------
    "Optimization time: %.3f s": "优化耗时：%.3f 秒",
    "Optimization failed: %s": "优化失败：%s",
    "Optimization skipped; UV unchanged": "已跳过优化，UV 未变化",
    "Select at least one edge in the UV editor first":
        "请先在 UV 编辑器中选中至少一条边",
    "This mesh has no UV map": "当前网格没有 UV",
    "Horizontal: %d edges": "水平：%d 条边",
    "Vertical: %d edges": "垂直：%d 条边",
    "Aligned: %d edges": "对齐：%d 条边",
    "This mesh has no constrained edges": "当前网格没有约束边",
    "Removed constraints from %d edges": "已解除 %d 条边的约束",
    "Nothing to unconstrain": "没有可解除的约束边",
    "None of the selected edges is constrained": "所选边中没有已约束的边",
    "Nothing was selected: removed constraints from all %d edges":
        "未选中任何边：已解除全部 %d 条边的约束",
    "Optimize strength is 0, nothing was changed (%d faces)":
        "优化强度为 0，未做任何改动（%d 个面）",
    "Re-solved %d faces (%d triangles)": "已重解 %d 个面（%d 个三角形）",
    "Re-solved %d islands / %d faces: worst angle %.2f deg -> %.2f deg, "
    "edge spread %.3f -> %.3f, density %.3f -> %.3f":
        "已重解 %d 个岛屿 / %d 个面：最差角度 %.2f° → %.2f°，"
        "边长离散度 %.3f → %.3f，密度 %.3f → %.3f",
    "; %d triangles remain flipped":
        "；仍有 %d 个翻转三角形",

    # -- preferences: headings and control labels -------------------------
    "Workflow": "工作流",
    "Optimize after marking": "标记后自动优化",
    "Re-solve": "重解",
    "Unwrap algorithm": "展开算法",
    "Angle Based": "角度法",
    "Conformal": "保角法",
    "Angle Based - smaller distortion, better for organic shapes":
        "角度法 —— 形变更小，更适合有机形状",
    "Conformal - faster, steadier on hard surfaces":
        "保角法 —— 更快，硬表面上更稳定",
    "Unwrap margin": "展开边距",
    "Relax iterations": "松弛迭代次数",
    "Relax step": "松弛步长",
    "Overlay colours": "叠加颜色",
    "Show constraint colours": "显示约束边颜色",
    "UV editor line width": "UV 编辑器线宽",
    "Line width": "线宽",
    "Horizontal colour": "水平约束边颜色",
    "Vertical colour": "垂直约束边颜色",
    "Aligned colour": "对齐约束边颜色",
    "Tint selected constrained edges": "高亮选中的约束边",
    "Selected colour": "选中颜色",
    "Selected tint": "选中叠加强度",
    "Colours default to the values in RizomUV's own preferences":
        "默认值取自 RizomUV 自身的偏好设置",

    # -- preferences: tooltips --------------------------------------------
    "Re-solve the UV island as soon as a constraint is marked, so the effect "
    "is visible immediately. Turn it off to mark only and leave the UVs alone, "
    "which is what RizomUV does -- its own message is that you have to update "
    "the optimization to see the result":
        "标记约束后立刻重解所在 UV 岛屿，以便马上看到效果。"
        "关闭后只做标记、不动 UV —— 这也是 RizomUV 的做法，"
        "它会提示你需要更新优化才能看到结果",

    "A constrained edge that is also selected is drawn in a blend of its "
    "constraint colour and RizomUV's selected-edge colour, so it is obvious "
    "which constrained edges the next operation will act on":
        "同时被选中的约束边会用「约束色」与「RizomUV 选中色」的混合色绘制，"
        "以便一眼看出下一次操作会对哪些约束边生效",

    "Colour blended into a constrained edge while it is selected. Defaults to "
    "the value in RizomUV's own preferences: Selected = 0 1 1":
        "约束边被选中时叠加进去的颜色。"
        "默认值取自 RizomUV 自身的偏好设置：Selected = 0 1 1",

    "How far towards the selected colour a selected constrained edge is pushed. "
    "0 leaves it as the plain constraint colour; 1 replaces it, losing the "
    "constraint hue":
        "选中的约束边向「选中颜色」靠拢的程度。"
        "0 表示保持原本的约束色不变；1 表示完全替换为选中色，"
        "但这样就看不出它属于哪一类约束了",

    "Algorithm used when the island is re-solved. Angle Based distorts organic "
    "shapes (characters, creatures) less; Conformal is faster and steadier on "
    "hard surfaces. The constrained edges take part in this solve as hard "
    "constraints, and the island's position and size are put back afterwards":
        "重解岛屿时使用的展开算法。角度法对有机形状（角色、生物）形变更小；"
        "保角法更快、在硬表面上更稳定。"
        "约束边会作为硬约束参与这次求解，岛屿的位置与尺寸之后会被还原",

    "Island spacing during the re-solve. The islands are only temporarily laid "
    "out while solving and are scaled back afterwards, so this only affects the "
    "solve itself":
        "重解时岛屿之间的间距。岛屿只是在求解过程中被临时铺开，之后会缩放回原样，"
        "所以这个值只影响求解过程本身",

    "Iterations of the in-place relaxation that runs after the re-solve: it puts "
    "the constraints exactly where they belong and smooths the transition "
    "between the held edges and the faces around them. Higher is smoother and "
    "slower":
        "重解之后进行的原地松弛迭代次数：它把约束精确地放到应有的位置上，"
        "并让被固定的边与周围面之间平滑过渡。数值越大越平滑，也越慢",

    "How much of each iteration's displacement is applied. Too large oscillates, "
    "too small converges slowly":
        "每次迭代实际施加的位移比例。太大容易来回震荡，太小则收敛缓慢",

    "Highlight constrained edges with RizomUV's colours in the UV editor and "
    "the 3D viewport":
        "在 UV 编辑器和 3D 视图中用 RizomUV 的颜色高亮显示约束边",

    "Line width in pixels for constrained edges in the UV editor. Defaults to 2":
        "UV 编辑器中约束边的线宽（像素）。默认 2",

    "Overlay colour for horizontally constrained edges. Defaults to the value "
    "in RizomUV's own preferences: EdgeConstrainedHorizontaly = 0.5 0.5 1":
        "水平约束边的叠加颜色。默认取 RizomUV 自身偏好设置中的值："
        "EdgeConstrainedHorizontaly = 0.5 0.5 1",

    "Overlay colour for vertically constrained edges. Defaults to the value in "
    "RizomUV's own preferences: EdgeConstrainedVerticaly = 0.4 0.8 0.4":
        "垂直约束边的叠加颜色。默认取 RizomUV 自身偏好设置中的值："
        "EdgeConstrainedVerticaly = 0.4 0.8 0.4",

    "Overlay colour for aligned constrained edges. Defaults to the value in "
    "RizomUV's own preferences: EdgeConstrainedAligned = 1 1 0":
        "对齐约束边的叠加颜色。默认取 RizomUV 自身偏好设置中的值："
        "EdgeConstrainedAligned = 1 1 0",
    'Auto iterations': '自动迭代',
    'Unfold iterations': '展开迭代次数',
    'Optimize iterations': '优化迭代次数',
    'Strength': '强度',
    'Angles / Lengths': '角度 / 长度',
    'Use density map': '使用密度贴图',
    'Prevent flips': '防止翻转',
    'Prevent overlaps': '防止重叠',
    'Overlap space': '重叠间隙',
    'Align borders': '对齐边界',
    'Threshold': '阈值',
    'Auto orient': '自动定向',
    'Fill holes': '填充孔洞',
    'Free selection borders': '释放选择边界',
    'Iterations': '迭代次数',
    'Stretch': '畸变',
    'Integrity checks': '完整性检查',
    'Boundary': '边界',
    'Fixed boundary conflicts with edge constraints; UV unchanged': '固定边界与约束方向冲突，UV 未修改',
    'Constraint mark saved, but optimization did not complete; any rejected UV layouts were restored.': '约束标记已保存，但优化未完成；未通过检查的 UV 布局已恢复。',
    'Constraints removed, but optimization did not complete; any rejected UV layouts were restored.': '约束已解除，但优化未完成；未通过检查的 UV 布局已恢复。',
    'fixed boundary conflicts with edge constraints': '固定边界与边约束冲突',
    'pinned vertices conflict with edge constraints': '固定点与边约束冲突',
    'exact collinearity would collapse UV triangles; no safe result found within tolerance': '完全共线会使 UV 三角形面积归零；容差内未找到安全结果',
    'Manual optimization iterations: %d; no safe result found with these settings': '手动优化次数：%d；本次设置下未找到安全结果',
    'non-finite UV result': 'UV 坐标出现非有限数值',
    'a constrained edge collapsed to zero length': '约束边长度变为零',
    'constraint residual exceeds tolerance': '约束偏差超过容差',
    'triangle flipped or became degenerate': '三角形翻转或退化',
    'a pinned UV vertex moved': '固定 UV 点发生移动',
    'UVs outside the target changed': '目标范围外的 UV 发生变化',
    'target island area changed': '目标岛面积发生变化',
    'a new overlap appeared within an island': '岛内出现新增重叠',
    'a new overlap appeared between islands': '岛之间出现新增重叠',
    'no safe candidate passed validation': '没有候选结果通过质量检查',
    'no safe initialization could repair the input UVs while preserving constraints': '保留约束时未找到可安全修复当前 UV 的初始展开',
    'Protection settings limited alignment; remaining deviation %.3f deg': '保护设置限制了对齐程度，剩余偏差 %.3f 度',
    'No visible selected UV or mesh faces to optimize': '当前没有可见的 UV 面可供优化',
}
