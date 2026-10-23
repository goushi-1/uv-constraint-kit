# SPDX-License-Identifier: GPL-3.0-or-later
"""Workflow and overlay preferences; solver parameters live on Optimize/F9."""

import bpy
from bpy.props import BoolProperty, FloatProperty
from bpy.props import FloatVectorProperty

from .const import (
    C_ALIGNED,
    C_CIRCULAR,
    C_HORIZONTAL,
    C_RECTANGULAR,
    C_VERTICAL,
    DEFAULT_UV_EDGE_WIDTH,
    RIZOM_SELECTED_BLEND,
    RIZOM_SRGB,
    RIZOM_SRGB_SELECTED,
    SHAPE_SRGB,
)


class UVCKPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    auto_optimize: BoolProperty(
        name="Optimize after marking",
        description="Re-solve the UV island as soon as a constraint is marked, "
                    "so the effect is visible immediately. Turn it off to mark "
                    "only and leave the UVs alone, which is what RizomUV does "
                    "-- its own message is that you have to update the "
                    "optimization to see the result",
        default=True,
    )

    show_overlay: BoolProperty(
        name="Show constraint colours",
        description="Highlight constrained edges with RizomUV's colours in the "
                    "UV editor and the 3D viewport",
        default=True,
    )

    uv_edge_width: FloatProperty(
        name="UV editor line width",
        description="Line width in pixels for constrained edges in the UV "
                    "editor. Defaults to 2",
        default=DEFAULT_UV_EDGE_WIDTH,
        min=1.0,
        max=16.0,
        precision=1,
    )

    color_horizontal: FloatVectorProperty(
        name="Horizontal colour",
        description="Overlay colour for horizontally constrained edges. "
                    "Defaults to the value in RizomUV's own preferences: "
                    "EdgeConstrainedHorizontaly = 0.5 0.5 1",
        subtype="COLOR",
        size=3,
        min=0.0,
        max=1.0,
        default=RIZOM_SRGB[C_HORIZONTAL],
    )

    color_vertical: FloatVectorProperty(
        name="Vertical colour",
        description="Overlay colour for vertically constrained edges. Defaults "
                    "to the value in RizomUV's own preferences: "
                    "EdgeConstrainedVerticaly = 0.4 0.8 0.4",
        subtype="COLOR",
        size=3,
        min=0.0,
        max=1.0,
        default=RIZOM_SRGB[C_VERTICAL],
    )

    color_aligned: FloatVectorProperty(
        name="Aligned colour",
        description="Overlay colour for aligned constrained edges. Defaults to "
                    "the value in RizomUV's own preferences: "
                    "EdgeConstrainedAligned = 1 1 0",
        subtype="COLOR",
        size=3,
        min=0.0,
        max=1.0,
        default=RIZOM_SRGB[C_ALIGNED],
    )

    color_circular: FloatVectorProperty(
        name="Circular colour",
        description="Overlay colour for circular constraint lines",
        subtype="COLOR",
        size=3,
        min=0.0,
        max=1.0,
        default=SHAPE_SRGB[C_CIRCULAR],
    )

    color_rectangular: FloatVectorProperty(
        name="Rectangular colour",
        description="Overlay colour for rectangular constraint lines",
        subtype="COLOR",
        size=3,
        min=0.0,
        max=1.0,
        default=SHAPE_SRGB[C_RECTANGULAR],
    )

    tint_selected_edges: BoolProperty(
        name="Tint selected constrained edges",
        description="A constrained edge that is also selected is drawn in a "
                    "blend of its constraint colour and RizomUV's selected-edge "
                    "colour, so it is obvious which constrained edges the next "
                    "operation will act on",
        default=True,
    )

    color_selected: FloatVectorProperty(
        name="Selected colour",
        description="Colour blended into a constrained edge while it is "
                    "selected. Defaults to the value in RizomUV's own "
                    "preferences: Selected = 0 1 1",
        subtype="COLOR",
        size=3,
        min=0.0,
        max=1.0,
        default=RIZOM_SRGB_SELECTED,
    )

    selected_blend: FloatProperty(
        name="Selected tint",
        description="How far towards the selected colour a selected constrained "
                    "edge is pushed. 0 leaves it as the plain constraint colour; "
                    "1 replaces it, losing the constraint hue",
        default=RIZOM_SELECTED_BLEND,
        min=0.0,
        max=1.0,
        precision=2,
    )

    symmetry_fill_color: FloatVectorProperty(
        name="Linked target fill colour",
        description="Fill colour for the live symmetry target preview",
        subtype="COLOR",
        size=3,
        min=0.0,
        max=1.0,
        default=RIZOM_SRGB_SELECTED,
    )

    symmetry_fill_opacity: FloatProperty(
        name="Linked target fill opacity",
        description="Opacity for the live symmetry target fill",
        subtype="FACTOR",
        default=0.12,
        min=0.0,
        max=1.0,
        precision=2,
    )

    def colors(self):
        """{kind: (r, g, b)} as currently configured."""
        return {
            C_HORIZONTAL: tuple(self.color_horizontal),
            C_VERTICAL: tuple(self.color_vertical),
            C_ALIGNED: tuple(self.color_aligned),
            C_CIRCULAR: tuple(self.color_circular),
            C_RECTANGULAR: tuple(self.color_rectangular),
        }

    def selected_color(self):
        """(r, g, b) of the "this edge is selected" tint."""
        return tuple(self.color_selected)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        col = layout.column(heading="Workflow")
        col.prop(self, "auto_optimize")

        layout.separator()
        col = layout.column(heading="Overlay colours")
        col.prop(self, "show_overlay")
        sub = col.column()
        sub.active = self.show_overlay
        sub.prop(self, "uv_edge_width")
        sub.separator()
        # Each swatch carries its own name. These used to be drawn with
        # `text=""`, which left four bare colour rectangles with nothing on
        # screen saying which constraint each one colours -- the wording only
        # existed as the properties' RNA names, i.e. visible on hover at best.
        # With `use_property_split` on, the property's own name is drawn to the
        # left of the swatch, which is the "label in front of the colour bar"
        # that was asked for. Those names are already source strings with
        # `terms_zh` entries, so they follow F10 like everything else.
        for name in ("color_horizontal", "color_vertical", "color_aligned",
                     "color_circular", "color_rectangular"):
            sub.prop(self, name)
        sub.separator()
        sub.prop(self, "tint_selected_edges")
        sel = sub.column()
        sel.active = self.tint_selected_edges
        sel.prop(self, "color_selected")
        sel.prop(self, "selected_blend")

        layout.separator()
        col = layout.column(heading="Live symmetry overlay")
        col.prop(self, "symmetry_fill_color")
        col.prop(self, "symmetry_fill_opacity", slider=True)

        layout.separator()
        box = layout.box()
        box.label(text="Horizontal, vertical and aligned colours default to "
                       "RizomUV's own preferences", icon="INFO")
