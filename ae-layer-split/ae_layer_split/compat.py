# SPDX-License-Identifier: GPL-3.0-or-later
"""Version compatibility layer.

Everything that differs between Blender 4.2-4.5 and 5.0+ goes through here:

* Compositor tree
    - 4.x : ``scene.use_nodes`` + ``scene.node_tree`` (a tree owned by the scene)
    - 5.0+: ``scene.compositing_node_group`` (a regular node group data-block);
            ``scene.node_tree`` is gone and ``scene.use_nodes`` is deprecated.
* File Output node
    - 4.x : ``base_path`` + ``file_slots`` (per-input sub path) / ``layer_slots``
            (multilayer EXR)
    - 5.0+: ``directory`` + ``file_name`` + ``file_output_items``; the image type
            is chosen with ``format.media_type``. Each input is written to
            ``{directory}{file_name}{item name}{frame}.ext``.
* EEVEE engine id: ``BLENDER_EEVEE_NEXT`` in 4.2-4.5, ``BLENDER_EEVEE`` in 5.0+.

APIs are detected by feature (RNA property present or not) rather than by
version number, so point releases that move things around keep working.
"""

import bpy

_SCENE_PROPS = bpy.types.Scene.bl_rna.properties
_FILE_OUTPUT_PROPS = bpy.types.CompositorNodeOutputFile.bl_rna.properties

#: True on Blender 5.0+, where the compositor is a node group assigned to the scene.
USE_COMPOSITOR_NODE_GROUP = (
    "compositing_node_group" in _SCENE_PROPS and "node_tree" not in _SCENE_PROPS
)

#: True on Blender 5.0+, where the File Output node uses ``file_output_items``.
NEW_FILE_OUTPUT_API = "file_output_items" in _FILE_OUTPUT_PROPS

EEVEE_ENGINES = {"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"}

COMPOSITOR_GROUP_NAME = "AE Split Compositor"


def engine_label(engine):
    """Human readable name for a render engine id."""
    if engine == "CYCLES":
        return "Cycles"
    if engine in EEVEE_ENGINES:
        return "EEVEE"
    if engine == "BLENDER_WORKBENCH":
        return "Workbench"
    return engine.replace("_", " ").title()


def eevee_engine_id():
    """Engine id of EEVEE for the running Blender version."""
    items = bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items
    for ident in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        if ident in items:
            return ident
    return "BLENDER_EEVEE"


# -----------------------------------------------------------------------------
# Compositor tree
# -----------------------------------------------------------------------------

def get_compositor_tree(scene):
    """Return the scene's compositor node tree, or None if there is none."""
    if USE_COMPOSITOR_NODE_GROUP:
        return scene.compositing_node_group
    return scene.node_tree


def ensure_compositor_tree(scene):
    """Make sure the scene has an active compositor tree.

    Returns ``(tree, info)`` where ``info`` is a JSON-serialisable dict that
    ``restore_compositor_tree`` uses to undo exactly what was done here.
    """
    info = {"use_node_group_api": USE_COMPOSITOR_NODE_GROUP}

    if USE_COMPOSITOR_NODE_GROUP:
        tree = scene.compositing_node_group
        info["created_group"] = None
        if tree is None:
            tree = bpy.data.node_groups.new(COMPOSITOR_GROUP_NAME, "CompositorNodeTree")
            scene.compositing_node_group = tree
            info["created_group"] = tree.name
        return tree, info

    # Blender 4.x: enabling use_nodes on a scene without a tree makes Blender
    # create a default tree (Render Layers + Composite, + Viewer in 4.5).
    # Remember those default nodes so Remove Setup can delete them again.
    info["use_nodes"] = bool(scene.use_nodes)
    had_tree = scene.node_tree is not None
    before = {n.name for n in scene.node_tree.nodes} if had_tree else set()
    scene.use_nodes = True
    tree = scene.node_tree
    info["auto_created_nodes"] = [] if had_tree else [
        n.name for n in tree.nodes if n.name not in before
    ]
    return tree, info


def restore_compositor_tree(scene, info):
    """Undo ``ensure_compositor_tree`` (call after the add-on's nodes are removed)."""
    if not info:
        return
    if USE_COMPOSITOR_NODE_GROUP:
        name = info.get("created_group")
        group = bpy.data.node_groups.get(name) if name else None
        if group is not None:
            if scene.compositing_node_group == group:
                scene.compositing_node_group = None
            # Only delete it if nothing else (another scene, a user) uses it.
            if group.users == 0:
                bpy.data.node_groups.remove(group)
        return

    tree = scene.node_tree
    if tree is not None:
        for name in info.get("auto_created_nodes", []):
            node = tree.nodes.get(name)
            if node is not None:
                tree.nodes.remove(node)
    if "use_nodes" in info:
        scene.use_nodes = info["use_nodes"]


# -----------------------------------------------------------------------------
# File Output node
# -----------------------------------------------------------------------------

def _configure_format(fmt, multilayer, depth):
    """Set image format settings on a File Output node's ``format``."""
    if NEW_FILE_OUTPUT_API:
        # media_type filters the file_format enum, so it has to be set first.
        fmt.media_type = "MULTI_LAYER_IMAGE" if multilayer else "IMAGE"
    if multilayer:
        fmt.file_format = "OPEN_EXR_MULTILAYER"
        fmt.color_mode = "RGBA"
        fmt.color_depth = depth          # '16' = half float, '32' = full float
        fmt.exr_codec = "ZIP"
        if hasattr(fmt, "use_exr_interleave"):
            # 5.0+ writes multi-part EXRs by default; the single-part
            # (interleaved) layout is what 4.x writes and what most
            # compositing apps, After Effects included, read reliably.
            fmt.use_exr_interleave = True
    else:
        fmt.file_format = "PNG"
        fmt.color_mode = "RGBA"
        fmt.color_depth = depth          # '8' or '16' bits per channel
        fmt.compression = 15


def new_file_output(tree, directory, file_prefix, layer_names, multilayer, depth):
    """Create a File Output node and return ``(node, input_sockets)``.

    * PNG (``multilayer=False``): ``layer_names`` must hold one entry; frames are
      written to ``{directory}{file_prefix}####.png``.
    * EXR multilayer: one input / EXR layer per entry of ``layer_names``; frames
      are written to ``{directory}{file_prefix}####.exr``.

    ``directory`` must end with a slash.
    """
    node = tree.nodes.new("CompositorNodeOutputFile")
    _configure_format(node.format, multilayer, depth)

    if NEW_FILE_OUTPUT_API:
        node.directory = directory
        items = node.file_output_items
        items.clear()
        if multilayer:
            node.file_name = file_prefix
            for name in layer_names:
                items.new("RGBA", name)
        else:
            # The item name is appended to file_name, so put the prefix there.
            node.file_name = ""
            items.new("RGBA", file_prefix)
        # 5.0 adds a trailing "extend" socket; only the real items are returned.
        return node, list(node.inputs)[: len(items)]

    if multilayer:
        node.base_path = directory + file_prefix
        node.layer_slots.clear()
        for name in layer_names:
            node.layer_slots.new(name)
    else:
        node.base_path = directory
        node.file_slots.clear()
        node.file_slots.new(layer_names[0])
        node.file_slots[0].path = file_prefix
    return node, list(node.inputs)


def describe_file_output(node):
    """Return ``(directory_or_base_path, [input names / sub paths])`` (for tests/UI)."""
    if NEW_FILE_OUTPUT_API:
        return node.directory + node.file_name, [i.name for i in node.file_output_items]
    if node.format.file_format == "OPEN_EXR_MULTILAYER":
        return node.base_path, [s.name for s in node.layer_slots]
    return node.base_path, [s.path for s in node.file_slots]
