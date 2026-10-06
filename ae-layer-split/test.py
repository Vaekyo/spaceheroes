"""Automated test for the AE Layer Split add-on.

Run with:

    blender --background --python-exit-code 1 --python test.py
    blender --background --python-exit-code 1 --python test.py -- --no-render

The add-on is imported from the ``ae_layer_split`` folder next to this file
(no install needed). It builds a dummy scene (cube character, plane floor,
wall, a foreground pillar, a light, a camera), runs the operators and checks
the view layers, holdouts, compositor nodes, the Remove Setup undo (also after
save + reopen) and, unless ``--no-render`` is given, renders two tiny Cycles
frames and checks the alpha of the written PNG / EXR files.
"""

import math
import os
import shutil
import struct
import sys
import tempfile
import traceback

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
DO_RENDER = "--no-render" not in ARGS

FAILURES = []
TMP = tempfile.mkdtemp(prefix="ae_split_test_")


def check(cond, msg):
    if cond:
        print(f"  ok    {msg}")
    else:
        print(f"  FAIL  {msg}")
        FAILURES.append(msg)


# -----------------------------------------------------------------------------
# Load the add-on from this folder
# -----------------------------------------------------------------------------

bpy.ops.wm.read_factory_settings(use_empty=True)   # also disables installed add-ons
sys.path.insert(0, HERE)
import ae_layer_split  # noqa: E402
from ae_layer_split import compat, core  # noqa: E402

ae_layer_split.register()
print(f"Blender {bpy.app.version_string} | node-group compositor API: "
      f"{compat.USE_COMPOSITOR_NODE_GROUP} | new File Output API: {compat.NEW_FILE_OUTPUT_API}")


# -----------------------------------------------------------------------------
# Scene building helpers
# -----------------------------------------------------------------------------

def box_mesh(name, size=(1, 1, 1)):
    sx, sy, sz = (s / 2 for s in size)
    verts = [(x, y, z) for x in (-sx, sx) for y in (-sy, sy) for z in (-sz, sz)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    return mesh


def plane_mesh(name, size):
    s = size / 2
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(-s, -s, 0), (s, -s, 0), (s, s, 0), (-s, s, 0)], [], [(0, 1, 2, 3)])
    return mesh


def add_obj(name, data, coll, location=(0, 0, 0), rotation=(0, 0, 0)):
    obj = bpy.data.objects.new(name, data)
    obj.location = location
    obj.rotation_euler = rotation
    coll.objects.link(obj)
    return obj


def new_coll(name, parent):
    coll = bpy.data.collections.new(name)
    parent.children.link(coll)
    return coll


def build_scene(engine):
    """Dummy scene.

    Scene Collection
    |- Camera, Rock (loose object)
    |- Characters
    |  |- Crate (background object directly in the character's parent)
    |  '- Hero      <- character collection (nested!)
    |     '- HeroCube
    |- Set: Floor, Wall, Sun
    |- FG: Pillar (in front of the hero)
    '- Hidden (hide_render): Ghost
    """
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = engine
    scene.render.resolution_x = scene.render.resolution_y = 64
    scene.render.resolution_percentage = 100
    scene.frame_start, scene.frame_end = 1, 2
    if engine == "CYCLES":
        scene.cycles.samples = 16
        scene.cycles.device = "CPU"
    master = scene.collection

    cam = add_obj("Camera", bpy.data.cameras.new("Camera"), master,
                  (0, -10, 1.5), (math.radians(90), 0, 0))
    scene.camera = cam
    add_obj("Rock", box_mesh("Rock"), master, (3.5, 1, 0.5))

    chars = new_coll("Characters", master)
    add_obj("Crate", box_mesh("Crate"), chars, (-4, 1, 0.5))
    hero = new_coll("Hero", chars)
    add_obj("HeroCube", box_mesh("HeroCube", (2, 2, 2)), hero, (0, 0, 1))

    set_coll = new_coll("Set", master)
    add_obj("Floor", plane_mesh("Floor", 30), set_coll)
    add_obj("Wall", plane_mesh("Wall", 30), set_coll, (0, 4, 0), (math.radians(90), 0, 0))
    sun = bpy.data.lights.new("Sun", "SUN")
    sun.energy = 3
    # Light travels along (1, 1, -1): lights the cube's front, shadow to the right/back.
    direction = Vector((1, 1, -1)).normalized()
    sun_obj = add_obj("Sun", sun, set_coll)
    sun_obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()

    fg = new_coll("FG", master)
    add_obj("Pillar", box_mesh("Pillar", (0.6, 0.6, 3)), fg, (-0.8, -2.5, 1.5))

    hidden = new_coll("Hidden", master)
    hidden.hide_render = True
    add_obj("Ghost", box_mesh("Ghost"), hidden, (0, -5, 1))
    return scene


def run(op, **kwargs):
    """Call an operator; ERROR reports raise RuntimeError from Python, map that to CANCELLED."""
    try:
        return op(**kwargs)
    except RuntimeError as err:
        print(f"  (operator reported: {str(err).strip()})")
        return {"CANCELLED"}


def obj(name):
    return bpy.data.objects[name]


def in_layer(name, vl):
    return name in vl.objects


def configure(scene, fmt="PNG", floor=True, keep_shadows=True):
    s = scene.ae_split
    s.char_collection = bpy.data.collections["Hero"]
    s.keep_shadows = keep_shadows
    s.shadow_floor = obj("Floor") if floor else None
    s.output_format = fmt
    s.output_path = os.path.join(TMP, fmt.lower()) + os.sep
    return s


def our_nodes(tree):
    return [n for n in tree.nodes if n.get(core.TAG)]


def layer_coll(vl, coll_name):
    lcs = core.find_layer_collections(vl, bpy.data.collections[coll_name])
    return lcs[0] if lcs else None


# -----------------------------------------------------------------------------
# Image helpers
# -----------------------------------------------------------------------------

def load_pixels(path):
    img = bpy.data.images.load(path, check_existing=False)
    w, h = img.size
    px = list(img.pixels[:])
    bpy.data.images.remove(img)
    return w, h, px


def sample(scene, image, world_point, channel=3, radius=1):
    """Average of one channel around the pixel that ``world_point`` projects to."""
    w, h, px = image
    co = world_to_camera_view(scene, scene.camera, Vector(world_point))
    cx, cy = int(co.x * w), int(co.y * h)
    if not (0 <= cx < w and 0 <= cy < h):
        raise ValueError(f"test point {world_point} is outside the camera frame")
    vals = []
    for y in range(max(cy - radius, 0), min(cy + radius + 1, h)):
        for x in range(max(cx - radius, 0), min(cx + radius + 1, w)):
            vals.append(px[(y * w + x) * 4 + channel])
    return sum(vals) / len(vals)


def exr_channels(path):
    """Channel names from an OpenEXR header (single-part or multi-part)."""
    with open(path, "rb") as f:
        data = f.read(1 << 16)
    assert data[:4] == b"\x76\x2f\x31\x01", "not an EXR file"
    multipart = bool(struct.unpack("<i", data[4:8])[0] & 0x1000)
    chans, i = [], 8
    while True:                       # one header per part
        n_attrs = 0
        while True:                   # attributes of one header
            end = data.index(b"\0", i)
            name = data[i:end]
            i = end + 1
            if not name:
                break
            n_attrs += 1
            end = data.index(b"\0", i)
            i = end + 1
            size = struct.unpack("<i", data[i:i + 4])[0]
            i += 4
            value = data[i:i + size]
            i += size
            if name == b"channels":
                j = 0
                while value[j] != 0:
                    end = value.index(b"\0", j)
                    chans.append(value[j:end].decode())
                    j = end + 1 + 16
        if not multipart or n_attrs == 0:
            return chans


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

def test_png_cycles():
    print("\n[Cycles + PNG + shadow catcher + keep shadows]")
    scene = build_scene("CYCLES")
    user_vl = scene.view_layers[0]
    orig_path = scene.render.filepath
    configure(scene, "PNG")

    result = run(bpy.ops.ae_split.setup_layers)
    check(result == {"FINISHED"}, "Setup Layers finished")
    check(core.is_active(scene), "state stored in scene custom property")
    check(scene.render.film_transparent, "Film > Transparent enabled")
    check(scene.render.use_compositing, "compositing enabled")
    check("_main" in scene.render.filepath, "main render output goes to _main/")

    char_vl = scene.view_layers.get("CHAR_layer")
    bg_vl = scene.view_layers.get("BG_layer")
    check(char_vl is not None and bg_vl is not None, "CHAR_layer and BG_layer exist")
    check(char_vl.use and bg_vl.use, "add-on layers are used for rendering")
    check(not user_vl.use, f"user layer '{user_vl.name}' no longer renders")

    # CHAR_layer
    check(in_layer("HeroCube", char_vl) and not obj("HeroCube").holdout_get(view_layer=char_vl),
          "CHAR: character visible, not holdout (nested collection found)")
    for name in ("Wall", "Pillar", "Crate", "Rock"):
        check(obj(name).holdout_get(view_layer=char_vl), f"CHAR: {name} is holdout")
    check(layer_coll(char_vl, "FG").holdout, "CHAR: FG layer collection is holdout")
    check(not layer_coll(char_vl, "Hero").holdout, "CHAR: Hero layer collection not holdout")
    for name in ("Sun", "Camera"):
        check(in_layer(name, char_vl) and not obj(name).holdout_get(view_layer=char_vl),
              f"CHAR: {name} neither holdout nor excluded")
    check(not in_layer("Floor", char_vl), "CHAR: real floor not rendered")
    catcher = bpy.data.objects.get("Floor_AE_catcher")
    check(catcher is not None and catcher.is_shadow_catcher, "shadow catcher copy exists")
    check(catcher is not None and catcher.data == obj("Floor").data, "shadow catcher shares the floor mesh")
    check(catcher is not None and in_layer(catcher.name, char_vl)
          and not catcher.holdout_get(view_layer=char_vl), "CHAR: shadow catcher visible, not holdout")
    check(not obj("Floor").is_shadow_catcher, "real floor is not a shadow catcher")
    holdout_coll = bpy.data.collections.get(core.COLL_HOLDOUT)
    check(holdout_coll is not None and "Ghost" not in holdout_coll.objects,
          "render-hidden objects are not pulled into the holdout helper")

    # BG_layer
    check(in_layer("HeroCube", bg_vl) and obj("HeroCube").indirect_only_get(view_layer=bg_vl),
          "BG: character is Indirect Only")
    check(layer_coll(bg_vl, "Hero").indirect_only, "BG: Hero layer collection indirect only")
    for name in ("Wall", "Pillar", "Crate", "Rock", "Floor", "Sun"):
        check(in_layer(name, bg_vl) and not obj(name).holdout_get(view_layer=bg_vl),
              f"BG: {name} visible, not holdout")
    check(not in_layer("Floor_AE_catcher", bg_vl), "BG: shadow catcher excluded")
    check(not in_layer("Floor_AE_catcher", user_vl), "user layer: shadow catcher hidden")
    check(in_layer("Floor", user_vl), "user layer: floor still there")

    # Compositor
    tree = compat.get_compositor_tree(scene)
    nodes = our_nodes(tree)
    frames = [n for n in nodes if n.type == "FRAME"]
    check(len(frames) == 1 and frames[0].label == "AE Split", "one 'AE Split' frame")
    rls = [n for n in nodes if n.bl_idname == "CompositorNodeRLayers"]
    check(sorted(n.layer for n in rls) == ["BG_layer", "CHAR_layer"], "Render Layers node per view layer")
    outs = [n for n in nodes if n.bl_idname == "CompositorNodeOutputFile"]
    check(len(outs) == 2, "two File Output nodes for PNG")
    for n in outs:
        path, items = compat.describe_file_output(n)
        short = "CHAR" if "CHAR" in n.name else "BG"
        check(path.rstrip("/\\").endswith(short) or path.endswith(short + "/"),
              f"{short} output goes to {short}/ ({path})")
        check(items == [short + "_"], f"{short} file prefix {short}_ ({items})")
        check(n.format.file_format == "PNG" and n.format.color_mode == "RGBA"
              and n.format.color_depth == "8", f"{short} output is 8-bit RGBA PNG")
        link = n.inputs[0].links[0] if n.inputs[0].is_linked else None
        check(link is not None and link.from_socket.name == "Image"
              and link.from_node.layer == f"{short}_layer",
              f"{short} output fed by {short}_layer Image (RGBA)")
    check(all(n.parent == frames[0] for n in nodes if n.type != "FRAME"), "nodes are inside the frame")

    # Running setup again must not stack nodes / layers
    check(run(bpy.ops.ae_split.setup_layers) == {"FINISHED"}, "Setup Layers runs a second time")
    check(len(our_nodes(compat.get_compositor_tree(scene))) == 5, "no duplicate nodes after re-setup")
    check(len(scene.view_layers) == 3, "no duplicate view layers after re-setup")
    check(len([o for o in bpy.data.objects if o.name.startswith("Floor_AE_catcher")]) == 1,
          "no duplicate shadow catcher after re-setup")

    if DO_RENDER:
        bpy.ops.render.render(animation=True)
        base = os.path.join(TMP, "png")
        files = {f: os.path.isfile(os.path.join(base, f))
                 for f in ("CHAR/CHAR_0001.png", "CHAR/CHAR_0002.png",
                           "BG/BG_0001.png", "BG/BG_0002.png")}
        check(all(files.values()), f"PNG sequences written {files}")
        if all(files.values()):
            char_img = load_pixels(os.path.join(base, "CHAR/CHAR_0001.png"))
            bg_img = load_pixels(os.path.join(base, "BG/BG_0001.png"))
            hero_pt, pillar_pt = (0.5, -1, 1), (-0.8, -2.8, 1.5)
            wall_pt, shadow_pt, open_floor = (-3, 4, 4), (1.6, 0.4, 0), (-1.5, -4, 0)
            check(sample(scene, char_img, hero_pt) > 0.95, "CHAR png: character opaque")
            check(sample(scene, char_img, hero_pt, channel=0) > 0.05, "CHAR png: character is lit")
            check(sample(scene, char_img, pillar_pt) < 0.05, "CHAR png: foreground pillar cuts the character out")
            check(sample(scene, char_img, wall_pt) < 0.05, "CHAR png: wall transparent")
            check(sample(scene, char_img, shadow_pt) > 0.2, "CHAR png: shadow caught on transparent floor")
            check(sample(scene, char_img, open_floor) < 0.1, "CHAR png: unshadowed floor transparent")
            check(sample(scene, bg_img, wall_pt) > 0.95, "BG png: wall opaque")
            check(sample(scene, bg_img, pillar_pt) > 0.95, "BG png: pillar opaque")
            check(sample(scene, bg_img, open_floor) > 0.95, "BG png: floor opaque")
        check(os.path.isdir(os.path.join(base, "_main")), "main render isolated in _main/")

    check(run(bpy.ops.ae_split.remove_setup) == {"FINISHED"}, "Remove Setup finished")
    check_restored(scene, user_vl.name, orig_path)


def check_restored(scene, user_vl_name, orig_path):
    vl_names = [vl.name for vl in scene.view_layers]
    check(vl_names == [user_vl_name], f"only the user's view layer is left ({vl_names})")
    user_vl = scene.view_layers[user_vl_name]
    check(user_vl.use, "user view layer renders again")
    check(core.STATE_KEY not in scene, "state property removed")
    check(not scene.render.film_transparent, "Film > Transparent restored")
    check(scene.render.filepath == orig_path, "render output path restored")
    check("Floor" in bpy.data.collections["Set"].objects, "floor back in 'Set'")
    check(len(obj("Floor").users_collection) == 1, "floor only in its original collection")
    check(bpy.data.objects.get("Floor_AE_catcher") is None, "shadow catcher copy deleted")
    check(not any(c.get(core.TAG) for c in bpy.data.collections), "helper collections deleted")
    check(not any(lc.holdout or lc.indirect_only or lc.exclude
                  for lc in core.iter_layer_collections(user_vl.layer_collection)),
          "user layer collections untouched")
    tree = compat.get_compositor_tree(scene)
    check(tree is None or not our_nodes(tree), "add-on nodes removed")
    if compat.USE_COMPOSITOR_NODE_GROUP:
        check(scene.compositing_node_group is None
              or scene.compositing_node_group.name != compat.COMPOSITOR_GROUP_NAME,
              "add-on compositor node group removed")
    else:
        check(not scene.use_nodes, "use_nodes restored")
        check(tree is None or len(tree.nodes) == 0, "Blender's auto-created default nodes removed")


def add_user_compositor_node(scene):
    """A node the user made before running the add-on; it must survive."""
    if compat.USE_COMPOSITOR_NODE_GROUP:
        tree = bpy.data.node_groups.new("User Comp", "CompositorNodeTree")
        scene.compositing_node_group = tree
    else:
        scene.use_nodes = True
        tree = scene.node_tree
    blur = tree.nodes.new("CompositorNodeBlur")
    blur.name = "UserBlur"
    return tree


def test_exr_and_reopen():
    print("\n[Cycles + EXR multilayer + existing compositor + save/reopen]")
    scene = build_scene("CYCLES")
    user_tree = add_user_compositor_node(scene)
    user_tree_name = user_tree.name
    n_user_nodes = len(user_tree.nodes)
    configure(scene, "EXR", floor=False)

    check(run(bpy.ops.ae_split.setup_layers) == {"FINISHED"}, "Setup Layers finished")
    tree = compat.get_compositor_tree(scene)
    check(tree.name == user_tree_name, "user's compositor tree reused")
    check("UserBlur" in tree.nodes, "user's node kept")
    outs = [n for n in our_nodes(tree) if n.bl_idname == "CompositorNodeOutputFile"]
    check(len(outs) == 1, "one File Output node for EXR")
    if outs:
        node = outs[0]
        path, items = compat.describe_file_output(node)
        check(node.format.file_format == "OPEN_EXR_MULTILAYER", "format is multilayer EXR")
        check(items == ["CHAR", "BG"], f"EXR layers CHAR and BG ({items})")
        check(all(s.is_linked for s in node.inputs[:2]), "both EXR inputs connected")
    char_vl = scene.view_layers["CHAR_layer"]
    check(in_layer("Floor", char_vl) and obj("Floor").holdout_get(view_layer=char_vl),
          "no catcher set: floor is a plain holdout in CHAR")

    if DO_RENDER:
        bpy.ops.render.render(animation=True)
        exr = os.path.join(TMP, "exr", core.EXR_PREFIX + "0001.exr")
        check(os.path.isfile(exr), "multilayer EXR written")
        if os.path.isfile(exr):
            chans = exr_channels(exr)
            with open(exr, "rb") as f:
                flags = struct.unpack("<i", f.read(8)[4:8])[0]
            check(not flags & 0x1000, "EXR is single-part (widest After Effects compatibility)")
            for layer in ("CHAR", "BG"):
                mine = sorted(c for c in chans if c.startswith(layer + "."))
                check(all(any(c.endswith("." + ch) for c in mine) for ch in "RGBA"),
                      f"EXR has {layer} RGBA channels ({mine})")

    # Save, reopen, then remove: the state must survive the file round trip.
    blend = os.path.join(TMP, "reopen.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    bpy.ops.wm.open_mainfile(filepath=blend)
    scene = bpy.context.scene
    check(core.is_active(scene), "state survives save + reopen")
    check(run(bpy.ops.ae_split.remove_setup) == {"FINISHED"}, "Remove Setup after reopen")
    tree = compat.get_compositor_tree(scene)
    check(tree is not None and tree.name == user_tree_name and "UserBlur" in tree.nodes
          and len(tree.nodes) == n_user_nodes, "user's compositor tree and nodes untouched")
    check(scene.view_layers.get("CHAR_layer") is None, "view layers removed after reopen")
    check(not scene.render.film_transparent, "film restored after reopen")
    if not compat.USE_COMPOSITOR_NODE_GROUP:
        check(scene.use_nodes, "use_nodes kept on (user had it on)")


def test_eevee():
    print("\n[EEVEE]")
    scene = build_scene(compat.eevee_engine_id())
    # A rim light that lives inside the character collection.
    add_obj("RimLight", bpy.data.lights.new("RimLight", "POINT"), bpy.data.collections["Hero"], (0, 2, 3))
    configure(scene, "PNG", floor=True, keep_shadows=True)
    errors, warnings = core.collect_issues(bpy.context, scene.ae_split)
    check(not errors, "no blocking errors")
    check(any("Cycles-only" in w or "Cycles only" in w for w in warnings), "Cycles-only warnings shown")
    check(run(bpy.ops.ae_split.setup_layers) == {"FINISHED"}, "Setup Layers finished")
    char_vl = scene.view_layers["CHAR_layer"]
    bg_vl = scene.view_layers["BG_layer"]
    check(not in_layer("HeroCube", bg_vl), "BG: character excluded (no indirect only in EEVEE)")
    check(in_layer("RimLight", bg_vl), "BG: light inside the character collection is kept")
    check(in_layer("RimLight", char_vl) and not obj("RimLight").holdout_get(view_layer=char_vl),
          "CHAR: character's light visible")
    check(bpy.data.objects.get("Floor_AE_catcher") is None, "no shadow catcher in EEVEE")
    check(obj("Floor").holdout_get(view_layer=char_vl), "CHAR: floor is holdout")
    check(run(bpy.ops.ae_split.remove_setup) == {"FINISHED"}, "Remove Setup finished")
    check(core.STATE_KEY not in scene, "state removed")
    check([c.name for c in obj("RimLight").users_collection] == ["Hero"], "light back in 'Hero' only")


def test_make_char_and_errors():
    print("\n[Make CHAR from Selected + error handling]")
    scene = build_scene("CYCLES")
    vl = bpy.context.view_layer
    rig = add_obj("Rig", bpy.data.armatures.new("Rig"), bpy.data.collections["Set"])
    body = add_obj("Body", box_mesh("Body"), bpy.data.collections["Set"])
    body.parent = rig
    sword = add_obj("Sword", box_mesh("Sword"), scene.collection)
    sword.modifiers.new("Armature", "ARMATURE").object = rig

    for o in scene.objects:
        o.select_set(False)
    check(run(bpy.ops.ae_split.make_char_from_selected) == {"CANCELLED"}, "nothing selected -> error")

    body.select_set(True)            # selecting one mesh of the rig is enough
    vl.objects.active = body
    check(run(bpy.ops.ae_split.make_char_from_selected) == {"FINISHED"}, "Make CHAR finished")
    char = scene.ae_split.char_collection
    check(char is not None and char.name.startswith("CHAR"), "CHAR collection created and picked")
    check(char is not None and {"Rig", "Body"} <= set(char.objects.keys()), "armature and child mesh in CHAR")
    check("Body" not in bpy.data.collections["Set"].objects, "moved out of 'Set'")
    check("Sword" not in char.objects.keys(), "unselected object with only a modifier is left alone")

    # Error cases
    scene.ae_split.char_collection = None
    check(run(bpy.ops.ae_split.setup_layers) == {"CANCELLED"}, "no character -> error")
    empty = new_coll("Empty", scene.collection)
    scene.ae_split.char_collection = empty
    check(run(bpy.ops.ae_split.setup_layers) == {"CANCELLED"}, "empty character collection -> error")
    check(not core.is_active(scene) and scene.view_layers.get("CHAR_layer") is None,
          "failed setup leaves nothing behind")
    scene.ae_split.char_collection = char
    scene.ae_split.shadow_floor = body
    check(run(bpy.ops.ae_split.setup_layers) == {"CANCELLED"}, "floor inside character -> error")
    scene.ae_split.shadow_floor = None
    scene.camera = None
    check(run(bpy.ops.ae_split.setup_and_render) == {"CANCELLED"}, "render without camera -> error")
    check(bpy.ops.ae_split.remove_setup.poll() is False, "Remove Setup disabled when not set up")


class FakeLayout:
    """Stands in for UILayout so the panel's draw() can run in background mode.

    It checks that every drawn property and operator really exists.
    """

    def __init__(self, log):
        self.log = log

    def _child(self, *args, **kwargs):
        return FakeLayout(self.log)

    box = row = column = split = _child

    def prop(self, data, name, **kwargs):
        if name not in data.bl_rna.properties:
            self.log.append(f"unknown property {name}")

    def operator(self, idname, **kwargs):
        mod, op = idname.split(".")
        if not hasattr(getattr(bpy.ops, mod), op):
            self.log.append(f"unknown operator {idname}")

    def label(self, text="", **kwargs):
        self.log.append(None)


def test_panel_draw():
    print("\n[Panel draw]")
    from ae_layer_split import ui
    scene = build_scene("CYCLES")
    configure(scene, "PNG")
    panel = ui.AESPLIT_PT_main
    for label, action in (("before setup", None), ("while active", "setup_layers")):
        if action:
            run(getattr(bpy.ops.ae_split, action))
        log = []
        fake = type("FakePanel", (), {"layout": FakeLayout(log),
                                      "_draw_background": staticmethod(panel._draw_background),
                                      "_draw_status": staticmethod(panel._draw_status)})()
        try:
            panel.draw(fake, bpy.context)
            problems = [m for m in log if m]
            check(not problems, f"panel draws {label} {problems if problems else ''}")
        except Exception as err:
            traceback.print_exc()
            check(False, f"panel draws {label} ({err})")
    run(bpy.ops.ae_split.remove_setup)


def main():
    tests = (test_png_cycles, test_exr_and_reopen, test_eevee, test_make_char_and_errors,
             test_panel_draw)
    for test in tests:
        try:
            test()
        except Exception:
            traceback.print_exc()
            FAILURES.append(f"{test.__name__} raised")
    if "--keep" not in ARGS: shutil.rmtree(TMP, ignore_errors=True)
    print("tmp:", TMP)
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print(f"ALL TESTS PASSED (Blender {bpy.app.version_string}, render checks: {DO_RENDER})")


main()
