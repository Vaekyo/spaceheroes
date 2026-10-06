# SPDX-License-Identifier: GPL-3.0-or-later
"""AE Layer Split.

Splits a scene into a character layer (CHAR_layer) and a background layer
(BG_layer) and wires the compositor to write them as separate image sequences
(or one multilayer EXR) for compositing in After Effects.

Works as a Blender 4.2+ extension (blender_manifest.toml) and as a legacy
add-on (bl_info below). Version-specific API differences (the 5.0 compositor
node group and File Output changes) live in ``compat.py``.
"""

bl_info = {
    "name": "AE Layer Split",
    "author": "vaekyo",
    "version": (1, 1, 1),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > AE Split",
    "description": "One-click character / background view layer split and camera export for After Effects",
    "category": "Render",
}

from . import properties, operators, ui  # noqa: E402

_modules = (properties, operators, ui)


def register():
    for module in _modules:
        module.register()


def unregister():
    for module in reversed(_modules):
        module.unregister()
