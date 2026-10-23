# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared constants and the RizomUV palette."""

# ---------------------------------------------------------------------------
# Mesh attribute written by this add-on
# ---------------------------------------------------------------------------
# Constraint type per **UV edge**, stored as an INT attribute on the CORNER
# (loop) domain: the value on a loop describes the UV edge that runs from that
# loop's vertex to `loop.link_loop_next`'s vertex.
#
# Storing per UV edge rather than per mesh edge is what keeps seams correct. A
# mesh edge that is a UV seam belongs to two UV islands, so a per-mesh-edge
# constraint would silently also constrain -- and colour -- the copy of that
# edge living in the *other* island. Blender's own `BMEdge.select` has the same
# seam-sharing problem, which is why the UV selection is read from the loops
# instead.
#
#   0 = none, 1 = horizontal, 2 = vertical, 3 = aligned
CONSTRAINT_ATTR = "uvck_constraint"

C_NONE = 0
C_HORIZONTAL = 1
C_VERTICAL = 2
C_ALIGNED = 3
# Overlay-only shape kinds. These values are not written to CONSTRAINT_ATTR.
C_CIRCULAR = 4
C_RECTANGULAR = 5

# ---------------------------------------------------------------------------
# RizomUV palette -- copied verbatim from RizomUV's own preferences file
# (Documents/RizomUV/<version>/RizomUVPreferences.1.0.0.txt):
#
#   EdgeConstrainedHorizontaly  vector3d 0.5 0.5 1
#   EdgeConstrainedVerticaly    vector3d 0.4 0.8 0.4
#   EdgeConstrainedAligned      vector3d 1 1 0
#   Selected                    vector3d 0 1 1
#   ConstrainedEdgeThickness    double   3.5
#   SelectedEdgeThickness       double   2
#   EdgeThickness               double   1.8
#
# RizomUV stores those as display (sRGB) values, and that is exactly what the
# overlay hands to the GPU: Blender's UI writes shader output straight to the
# window framebuffer, so reusing the numbers verbatim reproduces RizomUV's
# on-screen colour. Verified by sampling rendered pixels -- see overlay.py.
# ---------------------------------------------------------------------------
RIZOM_SRGB = {
    C_HORIZONTAL: (0.5, 0.5, 1.0),   # 淡蓝
    C_VERTICAL: (0.4, 0.8, 0.4),     # 绿
    C_ALIGNED: (1.0, 1.0, 0.0),      # 黄
}

# Shape constraint overlay colours (user-adjustable in add-on preferences).
SHAPE_SRGB = {
    C_CIRCULAR: (1.0, 0.56, 0.22),
    C_RECTANGULAR: (1.0, 0.3, 0.58),
}

# RizomUV's own "this edge is selected" colour, from the same preferences block:
#
#   Colors table {
#       Selected              vector3d 0 1 1        <- 青
#       SelectedOpositeEdge   vector3d 0 0.5 0.5
#   }
#   SelectedEdgeThickness     double 2
#
# It is needed because a constrained edge that is *also* selected has to look
# different from one that is not -- otherwise there is no way to tell, while
# several constrained edges are on screen, which of them the next operation is
# going to act on. RizomUV solves that by blending the two colours; so does
# `overlay._color_for`.
RIZOM_SRGB_SELECTED = (0.0, 1.0, 1.0)

# How far to push a constrained edge towards the selected colour, as a fraction.
# 1.0 would replace the constraint colour outright, which loses the information
# the colour carries; the default keeps the constraint hue readable while making
# the selection unmistakable.
RIZOM_SELECTED_BLEND = 0.85

RIZOM_EDGE_THICKNESS = 1.8
RIZOM_CONSTRAINED_EDGE_THICKNESS = 3.5
RIZOM_SELECTED_EDGE_THICKNESS = 2.0

# The overlay's own default line width is deliberately *not* RizomUV's 3.5 --
# the user asked for 2. RizomUV's number is kept above for reference and is
# still quoted in the tooltip.
DEFAULT_UV_EDGE_WIDTH = 2.0

# ---------------------------------------------------------------------------
# RizomUV's Optimize defaults, taken from a live UI snapshot rather than guessed
# ---------------------------------------------------------------------------
# These are the values RizomUV's Optimize panel actually starts at, read from
# `SnapshotFeatureTree` on the running application (the values are the last
# column; the widget names are RizomUV's own):
#
#   ZomidOptimizePowerUi                    Strength            Prefs.Optimize.Mix                1
#   ZomidOptimizeAnglesVsDistancesUi        Angles / Lengths    Prefs.Optimize.AngleDistanceMix   1
#   ZomidOptimizeIterationsUi               Optimize iterations Prefs.Optimize.Iterations         1
#   ZomidOptimizeKeepMetricUi               Keep real-world scale  Vars.Optimize.KeepMetric      true
#   ZomidOptimizeAlignBordersUi             Align borders       Vars.Unwrap.AlignBorders         false
#   ZomidOptimizeAlignBordersAngleUi        Align borders threshold  Vars.Unwrap.AlignBordersAngle  20
#   ZomidOptimizeAlignBordersPreOrientUi    Align borders auto-orient  Vars.Unwrap.AlignBordersPreOrient  true
#
# ⚠️ `keep_metric` is **False** here, and that is a deliberate departure from
# `ZOMID_OPTIMIZE_KEEP_METRIC_UI` (which the same snapshot reads as `true`).
# Three separate findings say the stored `true` is not the behaviour a user of
# *this* edition gets, and the user's own report -- "optimize enlarged the UV
# islands again" -- is the fourth:
#
#   1. The official help page for the control says, verbatim:
#          Keep real-world scale RealSpace only
#          Availability :: Not available in this edition — this control belongs
#                          to RizomUV RealSpace.
#      So the UI value is a leftover in the preferences file for a control this
#      edition does not offer. "RizomUV's default" for the edition in use is
#      therefore *off*, not the stored `true`.
#   2. The script API disagrees with the UI too: `Optimize()` left without the
#      `KeepMetric` key behaves as False.
#   3. Measured (experiment D, `RizomUV_Optimize研究_实验D岛屿隔离报告.md`):
#      `KeepMetric=True` sets each island's UV area to its *3D* area --
#      `sqrt(AreaUV/AreaXYZ)` comes out exactly 1.000000000, per island. In a
#      0..1 UV layout that is not a small correction: on the study's three
#      islands it was ×6.4, ×13.6 and ×4.2, and on a low-density island in this
#      project it measured ×5.4 and once ×51.
#   4. It also silently breaks `fix_boundary`. The border is held through the
#      relaxation and then the whole island is rescaled about its centre by that
#      factor, which moves every border vertex by (factor-1) × its distance from
#      the centre. With the factor above, "fix boundary" cannot be observed at
#      all.
#
# Consequence for the user: with this False, Optimize never grows or shrinks an
# island -- each island keeps the UV area it already had, which is the safe
# thing to do to somebody's existing texel density. Set it to True to get the
# RealSpace rescale back.
RIZOM_OPTIMIZE_DEFAULTS = {
    "mix": 1.0,
    "angle_distance_mix": 1.0,
    "keep_metric": False,
    "align_borders": False,
    "align_borders_angle": 20.0,
    "align_borders_pre_orient": True,
}

# Small helper used in a few places when comparing UV coordinates.
UV_EPS = 1e-5

# ---------------------------------------------------------------------------
# Translation msgids are deliberately *whole phrases*, never bare words
# ---------------------------------------------------------------------------
# Two reasons, both learned the hard way from `use_cn_666`'s term table (17280
# entries, checked by `_tools/i18n_check.py`):
#
#   * It already contains "Margin" -> "保证金" and "Overlay" -> "重叠". Bare
#     words are shared vocabulary, so a bare msgid either collides with a
#     translation written for a different context or -- worse -- wins over it.
#   * A translated phrase has to assemble from translated parts. `"%s: %d"` with
#     a bare kind name spliced in would translate the frame and leave the kind
#     in English.
#
# So each line that carries a constraint kind owns its own complete template.
# The count box and the operator report differ in wording, hence two maps.
#
# No leading whitespace in a msgid, deliberately: `use_cn_666` strips both sides
# of every source while loading, so an indented key would land in its table as a
# near-duplicate of the unindented one. Indentation is applied in `ui.py`, where
# it belongs.
KIND_COUNT = {
    C_HORIZONTAL: "Horizontal: %d",
    C_VERTICAL: "Vertical: %d",
    C_ALIGNED: "Aligned: %d",
}

KIND_REPORT = {
    C_HORIZONTAL: "Horizontal: %d edges",
    C_VERTICAL: "Vertical: %d edges",
    C_ALIGNED: "Aligned: %d edges",
}
