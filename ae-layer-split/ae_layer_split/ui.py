# SPDX-License-Identifier: GPL-3.0-or-later
"""Sidebar panel: 3D Viewport > N > AE Split."""

import bpy

from . import compat, core

MAX_LISTED = 12


class AESPLIT_PT_main(bpy.types.Panel):
    bl_label = "AE Layer Split"
    bl_idname = "AESPLIT_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AE Split"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.ae_split
        active = core.is_active(scene)
        cycles = core.is_cycles(scene)

        # --- Character -------------------------------------------------------
        box = layout.box()
        box.label(text="Character", icon="ARMATURE_DATA")
        row = box.row(align=True)
        row.prop(settings, "char_collection", text="")
        row.operator("ae_split.make_char_from_selected", text="Make CHAR from Selected",
                     icon="OUTLINER_COLLECTION")

        # --- Background (read only) ------------------------------------------
        box = layout.box()
        header = box.row()
        header.prop(settings, "show_bg_list", text="",
                    icon="TRIA_DOWN" if settings.show_bg_list else "TRIA_RIGHT", emboss=False)
        header.label(text="Background (auto)", icon="WORLD")
        if settings.show_bg_list:
            self._draw_background(box, scene, settings)

        # --- Options ----------------------------------------------------------
        box = layout.box()
        box.label(text="Options", icon="PREFERENCES")
        col = box.column()
        engine_row = col.row()
        engine_row.label(text=f"Render engine: {compat.engine_label(scene.render.engine)}",
                         icon="SHADING_RENDERED")

        col.prop(settings, "keep_shadows")
        if settings.keep_shadows and not cycles:
            col.label(text="Cycles only: character shadow will be missing on BG", icon="ERROR")

        col.prop(settings, "shadow_floor")
        if settings.shadow_floor is not None and not cycles:
            col.label(text="Shadow catcher is Cycles only (ignored)", icon="ERROR")

        col.prop(settings, "output_format", text="")
        if settings.output_format == "PNG":
            col.prop(settings, "png_depth", expand=True)
        else:
            col.prop(settings, "exr_depth", expand=True)
        col.prop(settings, "output_path", text="")

        # --- Buttons ----------------------------------------------------------
        col = layout.column(align=True)
        col.scale_y = 1.6
        col.operator("ae_split.setup_layers", icon="RENDERLAYERS")
        col.operator("ae_split.setup_and_render", icon="RENDER_ANIMATION")
        col.operator("ae_split.remove_setup", icon="TRASH")

        # --- Status -----------------------------------------------------------
        self._draw_status(layout, context, settings, active)

    @staticmethod
    def _draw_background(box, scene, settings):
        char = settings.char_collection
        if char is None or not core.collection_in_scene(scene, char):
            box.label(text="Pick a character collection first.", icon="INFO")
            return
        floor = settings.shadow_floor if core.is_cycles(scene) else None
        info = core.analyze_scene(scene, char, floor)
        col = box.column(align=True)
        if not info["top_level"]:
            col.label(text="(no other collections)")
        for coll, partial in info["top_level"][:MAX_LISTED]:
            text = f"{coll.name}  (all but {char.name})" if partial else coll.name
            col.label(text=text, icon="OUTLINER_COLLECTION")
        if len(info["top_level"]) > MAX_LISTED:
            col.label(text=f"... and {len(info['top_level']) - MAX_LISTED} more")
        loose = [o for o in scene.collection.objects
                 if o not in set(char.all_objects) and o.type not in core.KEEP_TYPES]
        if loose:
            col.label(text=f"+ {len(loose)} object(s) in Scene Collection", icon="OBJECT_DATA")
        n_kept = len(info["kept_objects"]) + len(info["char_keep"])
        if n_kept:
            col.label(text=f"{n_kept} light(s)/camera(s) stay visible in both layers",
                      icon="LIGHT")

    @staticmethod
    def _draw_status(layout, context, settings, active):
        box = layout.box()
        if active:
            state = core.get_state(context.scene) or {}
            box.label(text=f"Setup ACTIVE ({state.get('format', '?')})", icon="CHECKMARK")
            box.label(text="Changed options? Press Setup Layers again.", icon="INFO")
        else:
            box.label(text="Setup not active", icon="RADIOBUT_OFF")
        errors, warnings = core.collect_issues(context, settings)
        for msg in errors:
            box.label(text=msg, icon="CANCEL")
        for msg in warnings:
            box.label(text=msg, icon="ERROR")


classes = (AESPLIT_PT_main,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
