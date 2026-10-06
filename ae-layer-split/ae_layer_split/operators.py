# SPDX-License-Identifier: GPL-3.0-or-later
"""Operators: Make CHAR from Selected, Setup Layers, Setup + Render, Remove Setup."""

import traceback

import bpy
from bpy.props import BoolProperty

from . import ae_export, core


def _report_warnings(op, warnings):
    for warning in warnings:
        op.report({"WARNING"}, warning)


def _run_setup(op, context):
    """Shared by Setup and Setup + Render. Returns True on success."""
    try:
        warnings = core.setup_layers(context, context.scene.ae_split)
    except core.SetupError as err:
        op.report({"ERROR"}, str(err))
        return False
    except Exception as err:  # unexpected: report instead of a Python traceback popup
        traceback.print_exc()
        op.report({"ERROR"}, f"AE Split setup failed and was rolled back: {err}")
        return False
    _report_warnings(op, warnings)
    return True


class AESPLIT_OT_make_char(bpy.types.Operator):
    """Create a collection named CHAR from the selected objects (and their whole rig)"""

    bl_idname = "ae_split.make_char_from_selected"
    bl_label = "Make CHAR from Selected"
    bl_options = {"REGISTER", "UNDO"}

    move: BoolProperty(
        name="Move (unlink from other collections)",
        description=(
            "Remove the objects from their other collections in this scene. Needed so "
            "they are not treated (and held out) as background as well"
        ),
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def execute(self, context):
        selected = list(context.selected_objects)
        if not selected:
            self.report({"ERROR"}, "Select the character objects (or its rig) first.")
            return {"CANCELLED"}
        if core.is_active(context.scene):
            self.report({"ERROR"}, "Remove the current AE Split setup before changing the character.")
            return {"CANCELLED"}

        objects = core.gather_character_objects(selected)
        coll = core.make_char_collection(context.scene, objects, move=self.move)
        context.scene.ae_split.char_collection = coll
        self.report({"INFO"}, f"{len(objects)} object(s) in '{coll.name}'.")
        return {"FINISHED"}


class AESPLIT_OT_setup(bpy.types.Operator):
    """Create CHAR_layer and BG_layer, holdouts and compositor outputs (no render)"""

    bl_idname = "ae_split.setup_layers"
    bl_label = "Setup Layers"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def execute(self, context):
        if not _run_setup(self, context):
            return {"CANCELLED"}
        self.report({"INFO"}, "AE Split: CHAR_layer and BG_layer are set up.")
        return {"FINISHED"}


class AESPLIT_OT_setup_render(bpy.types.Operator):
    """Run Setup Layers, then render the animation frame range"""

    bl_idname = "ae_split.setup_and_render"
    bl_label = "Setup + Render Animation"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def execute(self, context):
        if context.scene.camera is None:
            self.report({"ERROR"}, "The scene has no active camera; set one before rendering.")
            return {"CANCELLED"}
        if not _run_setup(self, context):
            return {"CANCELLED"}
        if bpy.app.background:
            bpy.ops.render.render(animation=True)
        else:
            # Opens the render window and renders without blocking the UI.
            bpy.ops.render.render("INVOKE_DEFAULT", animation=True)
        return {"FINISHED"}


class AESPLIT_OT_remove(bpy.types.Operator):
    """Revert everything AE Split created or changed"""

    bl_idname = "ae_split.remove_setup"
    bl_label = "Remove Setup"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.scene is not None and core.is_active(context.scene)

    def execute(self, context):
        try:
            removed = core.remove_setup(context)
        except Exception as err:
            traceback.print_exc()
            self.report({"ERROR"}, f"AE Split: could not fully remove the setup: {err}")
            return {"CANCELLED"}
        if not removed:
            self.report({"WARNING"}, "AE Split is not set up in this scene.")
            return {"CANCELLED"}
        self.report({"INFO"}, "AE Split setup removed.")
        return {"FINISHED"}


class AESPLIT_OT_add_text_anchor(bpy.types.Operator):
    """Add an upright Empty at the 3D cursor marking where AE text should sit"""

    bl_idname = "ae_split.add_text_anchor"
    bl_label = "Add Text Anchor"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def execute(self, context):
        empty = ae_export.add_text_anchor(context)
        for obj in context.selected_objects:
            obj.select_set(False)
        empty.select_set(True)
        context.view_layer.objects.active = empty
        self.report({"INFO"}, f"Added '{empty.name}'. Move it to where the text should be.")
        return {"FINISHED"}


class AESPLIT_OT_export_camera(bpy.types.Operator):
    """Write a .jsx that rebuilds the camera, anchors and renders in After Effects"""

    bl_idname = "ae_split.export_camera_jsx"
    bl_label = "Export Camera to AE (.jsx)"
    bl_options = {"REGISTER"}       # only writes a file, changes no Blender data

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def execute(self, context):
        try:
            path, warnings = ae_export.export_camera_jsx(context, context.scene.ae_split)
        except core.SetupError as err:
            self.report({"ERROR"}, str(err))
            return {"CANCELLED"}
        except OSError as err:
            self.report({"ERROR"}, f"Could not write the .jsx: {err}")
            return {"CANCELLED"}
        _report_warnings(self, warnings)
        self.report({"INFO"}, f"AE Split {core.VERSION}: camera exported to {path}  "
                              "(AE: File > Scripts > Run Script File)")
        return {"FINISHED"}


classes = (
    AESPLIT_OT_make_char,
    AESPLIT_OT_add_text_anchor,
    AESPLIT_OT_export_camera,
    AESPLIT_OT_setup,
    AESPLIT_OT_setup_render,
    AESPLIT_OT_remove,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
