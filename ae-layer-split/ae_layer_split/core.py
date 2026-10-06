# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene analysis, Setup Layers and Remove Setup.

Nothing in here touches the UI; the operators in ``operators.py`` call
``setup_layers`` / ``remove_setup`` and turn ``SetupError`` into reports.

How the split works
-------------------
CHAR_layer
    Everything that is not the character is a *holdout*: it renders as
    transparent but still occludes the character (so a pillar in front of the
    character cuts it out correctly) and still casts shadows/reflections.
BG_layer
    The character collection is excluded, or set to *Indirect Only* in Cycles so
    its shadows and bounce light stay on the background.

Holdout is set per collection, but Blender ORs it across every collection an
object is in, and a collection's direct objects can't be treated one by one.
So background collections that also contain the character, the shadow catcher
floor, lights or cameras are "split": their sub-collections are handled one by
one, and their direct objects are linked into a helper collection
(``AE_Split_Holdout``) that is a holdout in CHAR_layer only. Objects lying
directly in the Scene Collection are handled the same way.

Shadow catcher (Cycles)
    ``Object.is_shadow_catcher`` is global, but the floor must be a normal
    floor in BG_layer and a shadow catcher in CHAR_layer. The floor is therefore
    moved into ``AE_Split_Floor`` (visible in BG_layer only) and a linked
    duplicate sharing its mesh is put in ``AE_Split_ShadowCatcher`` (CHAR_layer
    only). Remove Setup moves the floor back and deletes the duplicate.

Undo after save/reopen
    Every original value is stored as JSON in ``scene["ae_split_state"]``.
    Remove Setup reads only that, so it works in a later session too.
"""

import json
import os
import re

import bpy

from . import compat

#: Version of the code that is running. Must match blender_manifest.toml and
#: bl_info (test.py checks this).
VERSION = "1.1.3"

STATE_KEY = "ae_split_state"
TAG = "ae_split"                      # custom property marking add-on data
FLOOR_COLLS_KEY = "ae_split_orig_collections"

CHAR_LAYER = "CHAR_layer"
BG_LAYER = "BG_layer"
ADDON_LAYERS = (CHAR_LAYER, BG_LAYER)

COLL_HOLDOUT = "AE_Split_Holdout"           # extra links of loose background objects
COLL_KEEP = "AE_Split_KeepLights"           # lights/cameras that live inside CHAR
COLL_FLOOR = "AE_Split_Floor"               # the real floor (BG_layer only)
COLL_CATCHER = "AE_Split_ShadowCatcher"     # shadow catcher copy (CHAR_layer only)

FRAME_NAME = "AE_Split_Frame"
FRAME_LABEL = "AE Split"
MAIN_SUBDIR = "_main"
EXR_PREFIX = "AE_split_"
MASTER_SENTINEL = "<Scene Collection>"

# Object types that must never be holdout / excluded.
KEEP_TYPES = {"CAMERA", "LIGHT", "LIGHT_PROBE", "SPEAKER"}


class SetupError(Exception):
    """A user-facing problem; the message is shown with ``self.report``."""


# -----------------------------------------------------------------------------
# Installed vs running version
# -----------------------------------------------------------------------------

_MANIFEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blender_manifest.toml")
_disk_version_cache = {}


def disk_version(manifest_path=_MANIFEST):
    """Version in the manifest on disk, or None if it can't be read.

    Installing a new zip over an enabled add-on replaces the files, but
    Blender keeps running the already loaded (old) Python code until it is
    restarted. Comparing this with VERSION detects that.
    """
    try:
        mtime = os.path.getmtime(manifest_path)
    except OSError:
        return None
    cached = _disk_version_cache.get(manifest_path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(manifest_path, encoding="utf-8") as f:
            match = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
    except OSError:
        return None
    version = match.group(1) if match else None
    _disk_version_cache[manifest_path] = (mtime, version)
    return version


def stale_version_warning(manifest_path=_MANIFEST):
    """Message if the installed files are newer than the running code, else None."""
    installed = disk_version(manifest_path)
    if installed and installed != VERSION:
        return (f"Add-on files are version {installed} but Blender still runs {VERSION}. "
                "Restart Blender to finish the update.")
    return None


# -----------------------------------------------------------------------------
# State stored on the scene
# -----------------------------------------------------------------------------

def get_state(scene):
    raw = scene.get(STATE_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def set_state(scene, state):
    scene[STATE_KEY] = json.dumps(state)


def is_active(scene):
    return get_state(scene) is not None


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------

def is_addon_collection(coll):
    return bool(coll.get(TAG))


def is_cycles(scene):
    return scene.render.engine == "CYCLES"


def output_dir(settings):
    """Output folder from the settings, always ending with a slash."""
    path = (settings.output_path or "//render/").strip()
    return path.rstrip("/\\") + "/"


def scene_collections(scene):
    """Every collection in the scene, including the master collection."""
    return [scene.collection] + list(scene.collection.children_recursive)


def collection_in_scene(scene, coll):
    return coll is not None and (
        coll == scene.collection or coll in scene.collection.children_recursive
    )


def iter_layer_collections(layer_coll):
    """Depth-first (parents before children) walk of a LayerCollection tree."""
    yield layer_coll
    for child in layer_coll.children:
        yield from iter_layer_collections(child)


def find_layer_collection_chains(view_layer, collection):
    """All LayerCollections of ``collection`` in ``view_layer``, recursively.

    A collection can be linked under several parents, so there may be more
    than one. Each result is the chain from the top-level LayerCollection down
    to the match (the master LayerCollection is left out).
    """
    chains = []

    def walk(layer_coll, chain):
        for child in layer_coll.children:
            child_chain = chain + [child]
            if child.collection == collection:
                chains.append(child_chain)
            walk(child, child_chain)

    walk(view_layer.layer_collection, [])
    return chains


def find_layer_collections(view_layer, collection):
    return [chain[-1] for chain in find_layer_collection_chains(view_layer, collection)]


def _layer_path(chain):
    return "/".join(lc.collection.name for lc in chain)


def _iter_with_paths(view_layer):
    """Yield ``(path, layer_collection)`` for every non-master LayerCollection."""

    def walk(layer_coll, chain):
        for child in layer_coll.children:
            child_chain = chain + [child]
            yield _layer_path(child_chain), child
            yield from walk(child, child_chain)

    yield from walk(view_layer.layer_collection, [])


def snapshot_layer_flags(view_layer):
    return {
        path: [lc.exclude, lc.holdout, lc.indirect_only]
        for path, lc in _iter_with_paths(view_layer)
    }


def _set_flag(layer_coll, attr, value):
    # Only write when needed: writing 'exclude' runs an update that also
    # touches the children.
    if getattr(layer_coll, attr) != value:
        setattr(layer_coll, attr, value)


def apply_layer_flags(view_layer, snapshot, attrs=("exclude", "holdout", "indirect_only")):
    index = {"exclude": 0, "holdout": 1, "indirect_only": 2}
    for path, lc in _iter_with_paths(view_layer):
        flags = snapshot.get(path)
        if flags is None:
            continue
        for attr in attrs:
            _set_flag(lc, attr, bool(flags[index[attr]]))


def reset_layer_flags(view_layer):
    for _path, lc in _iter_with_paths(view_layer):
        _set_flag(lc, "exclude", False)
        _set_flag(lc, "holdout", False)
        _set_flag(lc, "indirect_only", False)


def set_collection_flags(view_layer, collection, **flags):
    """Set flags on every LayerCollection of ``collection`` in ``view_layer``."""
    for lc in find_layer_collections(view_layer, collection):
        for attr, value in flags.items():
            _set_flag(lc, attr, value)


def user_view_layers(scene):
    return [vl for vl in scene.view_layers if vl.name not in ADDON_LAYERS]


def reference_view_layer(context):
    """The user's view layer that new layers copy their exclude flags from."""
    scene = context.scene
    view_layer = getattr(context, "view_layer", None)
    if view_layer is not None and view_layer.name not in ADDON_LAYERS:
        return view_layer
    users = user_view_layers(scene)
    return users[0] if users else None


def rig_of(obj):
    """Armature that drives ``obj`` (the object itself, a parent, or a modifier)."""
    if obj.type == "ARMATURE":
        return obj
    parent = obj.parent
    while parent is not None:
        if parent.type == "ARMATURE":
            return parent
        parent = parent.parent
    for mod in getattr(obj, "modifiers", []):
        if mod.type == "ARMATURE" and mod.object is not None:
            return mod.object
    return None


# -----------------------------------------------------------------------------
# Scene analysis (shared by the panel and Setup)
# -----------------------------------------------------------------------------

def analyze_scene(scene, char_coll, floor=None):
    """Work out what counts as background.

    Returns a dict with
      ``bg_collections``  collections that can be holdout as a whole
      ``loose_objects``   background objects that need the helper collection
      ``kept_objects``    lights/cameras outside the character (never holdout)
      ``char_keep``       lights/cameras inside the character collection
      ``top_level``       ``[(collection, partial)]`` for the panel list
    """
    char_objs = set(char_coll.all_objects) if char_coll else set()
    result = {
        "bg_collections": [],
        "loose_objects": [],
        "kept_objects": [],
        "char_keep": [o for o in char_objs if o.type in KEEP_TYPES],
        "top_level": [],
    }

    def needs_split(coll):
        if char_coll is not None and (coll == char_coll or char_coll in coll.children_recursive):
            return True
        objs = set(coll.all_objects)
        if floor is not None and floor in objs:
            return True
        if objs & char_objs:
            return True
        return any(o.type in KEEP_TYPES for o in objs)

    seen_colls = set()
    seen_objs = set()

    def walk(coll):
        for obj in coll.objects:
            if obj in seen_objs or obj in char_objs or obj == floor:
                continue
            seen_objs.add(obj)
            if obj.type in KEEP_TYPES:
                result["kept_objects"].append(obj)
            else:
                result["loose_objects"].append(obj)
        for child in coll.children:
            if child in seen_colls or child == char_coll or is_addon_collection(child):
                continue
            seen_colls.add(child)
            if child.hide_render:
                continue        # never rendered, nothing to hold out
            if needs_split(child):
                walk(child)
            else:
                result["bg_collections"].append(child)

    walk(scene.collection)

    # Objects found loose but also inside a whole-holdout collection are
    # already covered; drop them from the loose list.
    covered = set()
    for coll in result["bg_collections"]:
        covered.update(coll.all_objects)
    result["loose_objects"] = [o for o in result["loose_objects"] if o not in covered]

    for child in scene.collection.children:
        if child == char_coll or is_addon_collection(child):
            continue
        partial = char_coll is not None and char_coll in child.children_recursive
        result["top_level"].append((child, partial))
    return result


def collect_issues(context, settings):
    """Return ``(errors, warnings)`` for the current settings.

    Errors block Setup; warnings are shown in the panel and reported.
    """
    scene = context.scene
    errors, warnings = [], []
    char = settings.char_collection
    floor = settings.shadow_floor
    engine = scene.render.engine

    if char is None:
        errors.append("No character collection set. Pick one or use 'Make CHAR from Selected'.")
    elif not collection_in_scene(scene, char):
        errors.append(f"Collection '{char.name}' is not linked in scene '{scene.name}'.")
    elif len(char.all_objects) == 0:
        errors.append(f"Character collection '{char.name}' is empty.")
    elif is_addon_collection(char):
        errors.append(f"'{char.name}' is a helper collection of this add-on; pick your character collection.")

    if floor is not None:
        if char is not None and floor in set(char.all_objects):
            errors.append(f"Shadow catcher floor '{floor.name}' is inside the character collection.")
        elif floor.name not in scene.objects:
            errors.append(f"Shadow catcher floor '{floor.name}' is not in this scene.")
        elif engine != "CYCLES":
            warnings.append("Shadow catcher floor is Cycles-only; it is ignored with "
                            f"{compat.engine_label(engine)}.")

    if scene.camera is None:
        warnings.append("Scene has no active camera; rendering will fail.")

    if engine in compat.EEVEE_ENGINES and settings.keep_shadows:
        warnings.append("EEVEE: 'Indirect Only' is Cycles-only, so the character is "
                        "excluded from BG and its shadow/bounce will be missing there.")
    elif engine == "BLENDER_WORKBENCH":
        warnings.append("Workbench does not support holdout; use Cycles or EEVEE.")

    if char is not None and not errors:
        if char.hide_render:
            warnings.append(f"Character collection '{char.name}' is disabled for rendering.")
        char_objs = set(char.all_objects)
        for obj in char_objs:
            if obj.type == "ARMATURE":
                missing = [c for c in obj.children_recursive if c not in char_objs]
                if missing:
                    warnings.append(f"{len(missing)} child object(s) of rig '{obj.name}' are not in "
                                    f"'{char.name}' and will be treated as background.")

    if settings.output_path.startswith("//") and not bpy.data.filepath:
        warnings.append("Save the .blend first: '//' output paths are relative to it.")

    return errors, warnings


# -----------------------------------------------------------------------------
# Setup Layers
# -----------------------------------------------------------------------------

def _new_collection(scene, name, state):
    coll = bpy.data.collections.new(name)
    coll[TAG] = 1
    scene.collection.children.link(coll)
    state["collections"].append(coll.name)
    return coll


def _record_view_layers(scene, state):
    """Remember and disable the user's view layers."""
    for vl in user_view_layers(scene):
        state["view_layer_use"][vl.name] = bool(vl.use)
        vl.use = False


def _ensure_addon_view_layer(scene, name, state):
    vl = scene.view_layers.get(name)
    if vl is None:
        vl = scene.view_layers.new(name)
        state["created_view_layers"].append(vl.name)
    else:
        # A layer with this name already existed: keep a copy of its flags.
        state["reused_view_layers"][name] = {
            "use": bool(vl.use),
            "flags": snapshot_layer_flags(vl),
        }
        reset_layer_flags(vl)
    vl.use = True
    return vl


def _move_floor(scene, floor, state):
    """Move the floor into COLL_FLOOR and add a shadow catcher copy."""
    orig = []
    for coll in list(floor.users_collection):
        if coll == scene.collection:
            orig.append(MASTER_SENTINEL)
        elif coll in scene.collection.children_recursive:
            orig.append(coll.name)
    # Per user view layer: was the floor rendered there?
    state["floor_layer_visible"] = {
        vl.name: floor.name in vl.objects for vl in user_view_layers(scene)
    }
    state["floor"] = floor.name
    state["floor_collections"] = orig
    floor[FLOOR_COLLS_KEY] = json.dumps(orig)   # backup in case the floor is renamed

    floor_coll = _new_collection(scene, COLL_FLOOR, state)
    state["floor_collection"] = floor_coll.name
    floor_coll.objects.link(floor)
    for name in orig:
        coll = scene.collection if name == MASTER_SENTINEL else bpy.data.collections.get(name)
        if coll is not None and floor.name in coll.objects:
            coll.objects.unlink(floor)

    catcher = floor.copy()               # linked duplicate: shares the mesh
    catcher.name = floor.name + "_AE_catcher"
    catcher[TAG] = 1
    catcher.is_shadow_catcher = True
    catcher_coll = _new_collection(scene, COLL_CATCHER, state)
    state["catcher_collection"] = catcher_coll.name
    catcher_coll.objects.link(catcher)
    state["catcher"] = catcher.name
    return floor_coll, catcher_coll


def _copy_excludes(ref_vl, vl):
    if ref_vl is None:
        return
    apply_layer_flags(vl, snapshot_layer_flags(ref_vl), attrs=("exclude",))


def _include_chain(view_layer, collection):
    """Include ``collection`` and all its parents in ``view_layer``."""
    for chain in find_layer_collection_chains(view_layer, collection):
        for lc in chain:
            _set_flag(lc, "exclude", False)
            _set_flag(lc, "holdout", False)
            _set_flag(lc, "indirect_only", False)


def _build_compositor(scene, settings, state):
    tree, info = compat.ensure_compositor_tree(scene)
    state["compositor"] = info
    nodes_created = state["nodes"]

    # Place the add-on's nodes below whatever the user already has.
    existing = [n for n in tree.nodes if n.type != "FRAME"]
    if existing:
        x0 = min(n.location.x for n in existing)
        y0 = min(n.location.y for n in existing) - 450
    else:
        x0, y0 = 0.0, 0.0

    def add(idname, name, location):
        node = tree.nodes.new(idname)
        node.name = name
        node[TAG] = 1
        node.location = location
        nodes_created.append(node.name)
        return node

    frame = add("NodeFrame", FRAME_NAME, (x0 - 40, y0 + 40))
    frame.label = FRAME_LABEL

    render_layers = {}
    for i, layer in enumerate(ADDON_LAYERS):
        rl = add("CompositorNodeRLayers", f"AE_Split_RL_{layer}", (x0, y0 - i * 330))
        rl.scene = scene
        rl.layer = layer
        rl.label = layer
        render_layers[layer] = rl

    out = output_dir(settings)
    if settings.output_format == "EXR":
        node, sockets = compat.new_file_output(
            tree, out, EXR_PREFIX, ["CHAR", "BG"], multilayer=True, depth=settings.exr_depth)
        node.name = "AE_Split_Output_EXR"
        node.label = "AE Split EXR"
        node[TAG] = 1
        node.location = (x0 + 400, y0 - 150)
        nodes_created.append(node.name)
        # The Image output is RGBA, so alpha travels with it into each EXR layer.
        tree.links.new(render_layers[CHAR_LAYER].outputs["Image"], sockets[0])
        tree.links.new(render_layers[BG_LAYER].outputs["Image"], sockets[1])
        outputs = [node]
    else:
        outputs = []
        for i, (layer, short) in enumerate(((CHAR_LAYER, "CHAR"), (BG_LAYER, "BG"))):
            node, sockets = compat.new_file_output(
                tree, out + short + "/", short + "_", [short],
                multilayer=False, depth=settings.png_depth)
            node.name = f"AE_Split_Output_{short}"
            node.label = f"AE Split {short} PNG"
            node[TAG] = 1
            node.location = (x0 + 400, y0 - i * 330)
            nodes_created.append(node.name)
            # RGBA PNG: alpha comes from the Image socket.
            tree.links.new(render_layers[layer].outputs["Image"], sockets[0])
            outputs.append(node)

    for node in list(render_layers.values()) + outputs:
        node.parent = frame


def setup_layers(context, settings):
    """Run the full setup. Returns a list of warnings; raises SetupError."""
    scene = context.scene
    errors, warnings = collect_issues(context, settings)
    if errors:
        raise SetupError(errors[0])

    # Running Setup again: undo the previous run first so the stored
    # "original" values stay the real originals.
    if is_active(scene):
        remove_setup(context)

    cycles = is_cycles(scene)
    char = settings.char_collection
    floor = settings.shadow_floor if cycles else None
    ref_vl = reference_view_layer(context)

    state = {
        "version": 1,
        "engine": scene.render.engine,
        "format": settings.output_format,
        "film_transparent": bool(scene.render.film_transparent),
        "use_compositing": bool(scene.render.use_compositing),
        "render_filepath": scene.render.filepath,
        "view_layer_use": {},
        "created_view_layers": [],
        "reused_view_layers": {},
        "collections": [],
        "nodes": [],
        "compositor": None,
        "floor": None,
        "catcher": None,
    }
    # Store the state before changing anything so a failure half-way can be
    # rolled back with remove_setup().
    set_state(scene, state)

    try:
        # 1. Render settings.
        scene.render.film_transparent = True
        scene.render.use_compositing = True
        scene.render.filepath = output_dir(settings) + MAIN_SUBDIR + "/main_"

        # 2. Leave the user's view layers alone, just stop rendering them.
        _record_view_layers(scene, state)

        # 3. Shadow catcher floor (Cycles only).
        floor_coll = catcher_coll = None
        if floor is not None:
            floor_coll, catcher_coll = _move_floor(scene, floor, state)
        set_state(scene, state)

        # 4. Work out the background and create the helper collections.
        info = analyze_scene(scene, char, floor)
        holdout_coll = keep_coll = None
        # Linking an object into another collection would also enable it for
        # rendering, so only take objects the user's layer actually has.
        loose = [o for o in info["loose_objects"]
                 if ref_vl is None or o.name in ref_vl.objects]
        if loose:
            holdout_coll = _new_collection(scene, COLL_HOLDOUT, state)
            for obj in loose:
                holdout_coll.objects.link(obj)
        if info["char_keep"]:
            keep_coll = _new_collection(scene, COLL_KEEP, state)
            for obj in info["char_keep"]:
                keep_coll.objects.link(obj)
        set_state(scene, state)

        # 5. The two render layers.
        char_vl = _ensure_addon_view_layer(scene, CHAR_LAYER, state)
        bg_vl = _ensure_addon_view_layer(scene, BG_LAYER, state)
        set_state(scene, state)
        for vl in (char_vl, bg_vl):
            _copy_excludes(ref_vl, vl)
            _include_chain(vl, char)

        # CHAR_layer: background = holdout, character fully visible.
        for coll in info["bg_collections"]:
            set_collection_flags(char_vl, coll, holdout=True)
        if holdout_coll is not None:
            set_collection_flags(char_vl, holdout_coll, exclude=False, holdout=True)
        if keep_coll is not None:
            set_collection_flags(char_vl, keep_coll, exclude=False)
        if floor_coll is not None:
            set_collection_flags(char_vl, floor_coll, exclude=True)
            set_collection_flags(char_vl, catcher_coll, exclude=False, holdout=False)

        # BG_layer: character excluded or indirect only.
        if settings.keep_shadows and cycles:
            set_collection_flags(bg_vl, char, exclude=False, indirect_only=True)
        else:
            set_collection_flags(bg_vl, char, exclude=True)
        if holdout_coll is not None:
            set_collection_flags(bg_vl, holdout_coll, exclude=True)
        if keep_coll is not None:
            set_collection_flags(bg_vl, keep_coll, exclude=False)
        if floor_coll is not None:
            set_collection_flags(bg_vl, floor_coll, exclude=False)
            set_collection_flags(bg_vl, catcher_coll, exclude=True)

        # User layers: hide the helper collections so they look unchanged.
        for vl in user_view_layers(scene):
            for coll in (holdout_coll, keep_coll, catcher_coll):
                if coll is not None:
                    set_collection_flags(vl, coll, exclude=True)
            if floor_coll is not None:
                visible = state["floor_layer_visible"].get(vl.name, True)
                set_collection_flags(vl, floor_coll, exclude=not visible)

        # 6. Compositor.
        _build_compositor(scene, settings, state)
        set_state(scene, state)
    except Exception:
        remove_setup(context)
        raise

    return warnings


# -----------------------------------------------------------------------------
# Remove Setup
# -----------------------------------------------------------------------------

def _remove_nodes(scene, state):
    tree = compat.get_compositor_tree(scene)
    if tree is None:
        return
    names = set(state.get("nodes", []))
    for node in list(tree.nodes):
        if node.get(TAG) or node.name in names:
            # Remove children before the frame so nothing is re-parented.
            if node.type != "FRAME":
                tree.nodes.remove(node)
    for node in list(tree.nodes):
        if node.type == "FRAME" and (node.get(TAG) or node.name in names):
            tree.nodes.remove(node)


def _restore_floor(scene, state):
    floor = bpy.data.objects.get(state["floor"]) if state.get("floor") else None
    if floor is None:
        # Renamed since setup: find it by the backup property.
        floor = next((o for o in bpy.data.objects if FLOOR_COLLS_KEY in o), None)
    if floor is None:
        return
    raw = floor.get(FLOOR_COLLS_KEY)
    names = state.get("floor_collections") or (json.loads(raw) if raw else [])
    linked = False
    for name in names:
        coll = scene.collection if name == MASTER_SENTINEL else bpy.data.collections.get(name)
        if coll is None:
            continue
        if floor.name not in coll.objects:
            coll.objects.link(floor)
        linked = True
    if not linked and scene.collection not in floor.users_collection:
        scene.collection.objects.link(floor)
    if FLOOR_COLLS_KEY in floor:
        del floor[FLOOR_COLLS_KEY]


def remove_setup(context):
    """Revert everything Setup did. Returns False if nothing was set up."""
    scene = context.scene
    state = get_state(scene)
    if state is None:
        return False

    created = set(state.get("created_view_layers", []))

    # Never delete the view layer a window is showing.
    users = [vl for vl in scene.view_layers if vl.name not in created]
    wm = bpy.data.window_managers[0] if bpy.data.window_managers else None
    if wm is not None and users:
        for window in wm.windows:
            if window.scene == scene and window.view_layer.name in created:
                window.view_layer = users[0]

    # 1. View layers.
    for name in created:
        vl = scene.view_layers.get(name)
        if vl is not None and len(scene.view_layers) > 1:
            scene.view_layers.remove(vl)
    for name, data in state.get("reused_view_layers", {}).items():
        vl = scene.view_layers.get(name)
        if vl is not None:
            reset_layer_flags(vl)
            apply_layer_flags(vl, data["flags"])
            vl.use = data["use"]
    for name, use in state.get("view_layer_use", {}).items():
        vl = scene.view_layers.get(name)
        if vl is not None:
            vl.use = use

    # 2. Floor back where it was, shadow catcher copy deleted.
    if state.get("floor"):
        _restore_floor(scene, state)
    catcher = bpy.data.objects.get(state["catcher"]) if state.get("catcher") else None
    if catcher is not None and catcher.get(TAG):
        bpy.data.objects.remove(catcher, do_unlink=True)

    # 3. Helper collections (removing them only unlinks the user's objects).
    for name in state.get("collections", []):
        coll = bpy.data.collections.get(name)
        if coll is not None and coll.get(TAG):
            bpy.data.collections.remove(coll)

    # 4. Compositor.
    _remove_nodes(scene, state)
    compat.restore_compositor_tree(scene, state.get("compositor"))

    # 5. Render settings.
    if "film_transparent" in state:
        scene.render.film_transparent = state["film_transparent"]
    if "use_compositing" in state:
        scene.render.use_compositing = state["use_compositing"]
    if "render_filepath" in state:
        scene.render.filepath = state["render_filepath"]

    del scene[STATE_KEY]
    return True


# -----------------------------------------------------------------------------
# Make CHAR from Selected
# -----------------------------------------------------------------------------

def gather_character_objects(selected):
    """Selected objects plus, for every rig involved, the armature and its children."""
    result = []
    seen = set()

    def add(obj):
        if obj not in seen:
            seen.add(obj)
            result.append(obj)

    for obj in selected:
        add(obj)
        rig = rig_of(obj)
        if rig is not None:
            add(rig)
            for child in rig.children_recursive:
                add(child)
    return result


def make_char_collection(scene, objects, move=True, name="CHAR"):
    """Create (or reuse) the CHAR collection and put ``objects`` in it."""
    coll = bpy.data.collections.get(name)
    if coll is None or coll.library is not None or is_addon_collection(coll):
        coll = bpy.data.collections.new(name)
    if not collection_in_scene(scene, coll):
        scene.collection.children.link(coll)

    for obj in objects:
        if obj.name not in coll.objects:
            coll.objects.link(obj)
        if move:
            # Leave other scenes alone; only unlink from this scene's collections.
            in_scene = set(scene_collections(scene))
            for other in list(obj.users_collection):
                if other != coll and other in in_scene and not is_addon_collection(other):
                    other.objects.unlink(obj)
    return coll
