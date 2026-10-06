# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene settings shown in the AE Split panel (``scene.ae_split``)."""

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty


def _floor_poll(_self, obj):
    return obj.type == "MESH"


class AESplitSettings(bpy.types.PropertyGroup):
    char_collection: PointerProperty(
        name="Character",
        description="Collection holding the character (rig, meshes, props it carries)",
        type=bpy.types.Collection,
    )
    keep_shadows: BoolProperty(
        name="Keep character shadows/bounce on BG",
        description=(
            "Cycles: the character is 'Indirect Only' in BG_layer, so it is invisible "
            "but still casts shadows and bounce light onto the background. "
            "EEVEE: the character is simply excluded"
        ),
        default=True,
    )
    shadow_floor: PointerProperty(
        name="Shadow catcher floor",
        description=(
            "Optional (Cycles only). This floor becomes a shadow catcher in CHAR_layer, "
            "so the character's shadow is rendered onto transparency"
        ),
        type=bpy.types.Object,
        poll=_floor_poll,
    )
    output_format: EnumProperty(
        name="Output",
        items=(
            ("PNG", "PNG RGBA sequence (separate folders)",
             "CHAR/ and BG/ sub-folders with RGBA PNG frames"),
            ("EXR", "OpenEXR Multilayer (one file)",
             "One multilayer EXR per frame with a CHAR and a BG layer"),
        ),
        default="PNG",
    )
    png_depth: EnumProperty(
        name="Color depth",
        items=(("8", "8-bit", "8 bits per channel"),
               ("16", "16-bit", "16 bits per channel")),
        default="8",
    )
    exr_depth: EnumProperty(
        name="Color depth",
        items=(("16", "Half float", "16-bit half float"),
               ("32", "Full float", "32-bit float")),
        default="16",
    )
    output_path: StringProperty(
        name="Output folder",
        description="Folder the layers are written to ('//' = next to the .blend file)",
        subtype="DIR_PATH",
        default="//render/",
    )
    show_bg_list: BoolProperty(name="Show background list", default=True)

    # Camera -> After Effects export
    px_per_unit: FloatProperty(
        name="Pixels per unit",
        description="How many AE pixels one Blender unit (metre) becomes. Only scales the "
                    "3D space in AE; the camera match is the same for any value",
        default=100.0, min=0.001, soft_min=1.0, soft_max=1000.0,
    )
    text_placeholder: StringProperty(
        name="Placeholder text",
        description="Text layer created in AE, parented to the first anchor and placed "
                    "between BG and CHAR (leave empty for none)",
        default="TEXT",
    )
    import_renders: BoolProperty(
        name="Import rendered layers",
        description="Let the AE script import the CHAR/BG renders (straight alpha) into the comp",
        default=True,
    )


def register():
    bpy.utils.register_class(AESplitSettings)
    bpy.types.Scene.ae_split = PointerProperty(type=AESplitSettings)


def unregister():
    del bpy.types.Scene.ae_split
    bpy.utils.unregister_class(AESplitSettings)
