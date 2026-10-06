# SPDX-License-Identifier: GPL-3.0-or-later
"""Export the Blender camera (and anchor objects) to After Effects as a .jsx.

Running the script in After Effects (File > Scripts > Run Script File...)
builds a comp that matches the render, imports the CHAR/BG renders, and adds
a keyframed 3D camera plus a 3D null per anchor, so AE text and graphics stick
to the 3D scene with no tracking.

Coordinate systems
------------------
Blender: right-handed, Z up, metres. A camera looks down its local -Z, +Y up.
AE:      right-handed, X right, Y down, Z into the screen, pixels. A camera
         looks down its local +Z, +Y down. Rotations follow the right-hand rule
         (positive Z rotation = clockwise on screen).

    AE vector = C * Blender vector,  C: (x, y, z) -> (x, -z, y)

An orientation becomes ``R_ae = C * R_blender * F`` with F = diag(1, -1, -1).
F maps AE-local axes onto Blender-local axes. For a camera, that turns
"-Z forward, +Y up" into "+Z forward, +Y down". For an anchor, an upright
Blender plane or text (rotation X = 90 degrees, facing -Y) becomes an AE layer
at zero rotation, facing the viewer.

Rotation order
--------------
AE's per-layer X/Y/Z rotation order is easy to get wrong, so each rotation
axis gets its own layer, chained by parenting:

    <name>        null: position + Y rotation
    '- <name> X   null: X rotation
       '- <name> Z  camera / null: Z rotation (+ zoom or scale)

World = T * Ry * Rx * Rz, which is ``to_euler('ZXY')`` in mathutils terms.
The order is fixed by the parenting, not by AE's internal convention. The
per-axis signs (right-handed, positive) were cross-checked against Blender's
long-standing "Export: Adobe After Effects" add-on (see test.py).
"""

import json
import math
import os

import bpy
from mathutils import Matrix, Vector

from . import core

ANCHOR_PROP = "ae_split_anchor"
ANCHOR_NAME = "AE_TEXT_ANCHOR"
JSX_NAME = "ae_camera.jsx"

# Blender world -> AE world axes.
C = Matrix(((1, 0, 0), (0, 0, -1), (0, 1, 0)))
# AE local -> Blender local axes (camera and layers alike).
F = Matrix(((1, 0, 0), (0, -1, 0), (0, 0, -1)))


# -----------------------------------------------------------------------------
# Math
# -----------------------------------------------------------------------------

def render_size(scene):
    """Final render size in pixels (resolution x percentage)."""
    r = scene.render
    pct = r.resolution_percentage / 100.0
    return max(4, round(r.resolution_x * pct)), max(4, round(r.resolution_y * pct))


def fps(scene):
    return scene.render.fps / scene.render.fps_base


def camera_zoom(cam_data, width, height, pixel_aspect=(1.0, 1.0)):
    """AE camera zoom (focal length in pixels) matching Blender's projection."""
    ax, ay = pixel_aspect
    fit = cam_data.sensor_fit
    if fit == "AUTO":
        fit = "HORIZONTAL" if width * ax >= height * ay else "VERTICAL"
        sensor = cam_data.sensor_width
    else:
        sensor = cam_data.sensor_width if fit == "HORIZONTAL" else cam_data.sensor_height
    if fit == "HORIZONTAL":
        return width * cam_data.lens / sensor
    # Vertical fit: focal length relative to the height, expressed in
    # horizontal pixels (AE zoom is measured along X).
    return height * cam_data.lens / sensor * (ay / ax)


def blender_to_ae(matrix_world, width, height, px_per_unit, prev_euler=None):
    """Convert a Blender world matrix to AE values.

    Returns ``(position, (rx, ry, rz) in degrees, uniform scale, euler)``.
    ``euler`` is passed back in as ``prev_euler`` on the next frame so the
    angles stay continuous (no jumps at +/-180 degrees).
    """
    loc, _rot, scale = matrix_world.decompose()
    rot3 = matrix_world.to_3x3().normalized()
    r_ae = C @ rot3 @ F
    euler = r_ae.to_euler("ZXY", prev_euler) if prev_euler else r_ae.to_euler("ZXY")
    p = C @ loc * px_per_unit
    position = (p.x + width / 2.0, p.y + height / 2.0, p.z)
    uniform = (abs(scale.x) + abs(scale.y) + abs(scale.z)) / 3.0
    angles = tuple(math.degrees(a) for a in (euler.x, euler.y, euler.z))
    return position, angles, uniform, euler


def ae_rotation_matrix(rx, ry, rz):
    """AE world rotation of the rig (degrees): Ry * Rx * Rz."""
    return (Matrix.Rotation(math.radians(ry), 3, "Y")
            @ Matrix.Rotation(math.radians(rx), 3, "X")
            @ Matrix.Rotation(math.radians(rz), 3, "Z"))


def ae_project(cam_sample, point_ae, width, height):
    """Project an AE world point through an exported camera sample (pixels, Y down).

    This is the pinhole model AE uses; the test compares it with Blender's own
    projection.
    """
    pos = Vector(cam_sample["position"])
    rot = ae_rotation_matrix(*cam_sample["rotation"])
    p = rot.transposed() @ (Vector(point_ae) - pos)
    z = cam_sample["zoom"]
    return width / 2.0 + z * p.x / p.z, height / 2.0 + z * p.y / p.z


# -----------------------------------------------------------------------------
# Sampling
# -----------------------------------------------------------------------------

def anchor_objects(context):
    """Anchors: tagged Empties in the scene plus selected objects (not the camera)."""
    scene = context.scene
    result = [o for o in scene.objects if o.get(ANCHOR_PROP)]
    for obj in getattr(context, "selected_objects", []) or []:
        if obj not in result and obj != scene.camera and obj.type != "CAMERA":
            result.append(obj)
    return result


def sample_scene(scene, cam, anchors, px_per_unit):
    """Sample camera and anchors on every frame of the scene range."""
    width, height = render_size(scene)
    aspect = (scene.render.pixel_aspect_x, scene.render.pixel_aspect_y)
    frames = list(range(scene.frame_start, scene.frame_end + 1, max(1, scene.frame_step)))
    rate = fps(scene)
    cam_samples = []
    obj_samples = {o.name: [] for o in anchors}
    prev = {}
    original = scene.frame_current
    try:
        for f in frames:
            scene.frame_set(f)
            t = (f - scene.frame_start) / rate
            pos, rot, _s, prev["__cam__"] = blender_to_ae(
                cam.matrix_world, width, height, px_per_unit, prev.get("__cam__"))
            cam_samples.append({
                "frame": f, "time": t, "position": pos, "rotation": rot,
                "zoom": camera_zoom(cam.data, width, height, aspect),
            })
            for obj in anchors:
                pos, rot, scale, prev[obj.name] = blender_to_ae(
                    obj.matrix_world, width, height, px_per_unit, prev.get(obj.name))
                obj_samples[obj.name].append({
                    "frame": f, "time": t, "position": pos, "rotation": rot, "scale": scale,
                })
    finally:
        scene.frame_set(original)
    return cam_samples, obj_samples


def collect_warnings(scene, cam):
    warnings = []
    data = cam.data
    if data.type != "PERSP":
        warnings.append(f"Camera '{cam.name}' is {data.type.lower()}; AE cameras are perspective "
                        "only, the export will not match.")
    if abs(data.shift_x) > 1e-6 or abs(data.shift_y) > 1e-6:
        warnings.append("Camera lens shift is not supported by AE cameras; set Shift X/Y to 0 "
                        "for an exact match.")
    if abs(scene.render.pixel_aspect_x - scene.render.pixel_aspect_y) > 1e-6:
        warnings.append("Non-square pixel aspect: the comp uses it, but double-check the match.")
    if any(m.camera for m in scene.timeline_markers):
        warnings.append("Timeline markers switch cameras; only the active camera is exported.")
    return warnings


# -----------------------------------------------------------------------------
# JSX
# -----------------------------------------------------------------------------

def _num(v):
    return float(f"{v:.5f}")


def _times(samples):
    # Full precision: rounded times would put keys between frames.
    return [float(f"{s['time']:.9f}") for s in samples]


def footage_paths(scene, settings):
    """Absolute paths of the first frame of each render the add-on writes."""
    out = bpy.path.abspath(core.output_dir(settings))
    first = str(scene.frame_start).zfill(4)
    if settings.output_format == "EXR":
        return {"EXR": os.path.join(out, f"{core.EXR_PREFIX}{first}.exr")}
    return {
        "BG": os.path.join(out, "BG", f"BG_{first}.png"),
        "CHAR": os.path.join(out, "CHAR", f"CHAR_{first}.png"),
    }


def build_jsx(scene, settings, cam_samples, obj_samples):
    """Return the ExtendScript source (ES3 syntax: var, function, no arrows)."""
    width, height = render_size(scene)
    rate = fps(scene)
    n_frames = len(cam_samples)
    data = {
        "version": core.VERSION,
        "compName": f"{scene.name}_AE_Split",
        "width": width,
        "height": height,
        "pixelAspect": _num(scene.render.pixel_aspect_x / scene.render.pixel_aspect_y),
        "fps": _num(rate),
        "duration": _num(max(n_frames, 1) * max(1, scene.frame_step) / rate),
        "startFrame": scene.frame_start,
        "footage": {k: v.replace("\\", "/") for k, v in footage_paths(scene, settings).items()}
                   if settings.import_renders else {},
        "text": settings.text_placeholder,
        "textSize": _num(max(settings.px_per_unit, 1.0)),
        "camera": {
            "name": scene.camera.name,
            "times": _times(cam_samples),
            "position": [[_num(c) for c in s["position"]] for s in cam_samples],
            "rx": [_num(s["rotation"][0]) for s in cam_samples],
            "ry": [_num(s["rotation"][1]) for s in cam_samples],
            "rz": [_num(s["rotation"][2]) for s in cam_samples],
            "zoom": [_num(s["zoom"]) for s in cam_samples],
        },
        "anchors": [
            {
                "name": name,
                "times": _times(samples),
                "position": [[_num(c) for c in s["position"]] for s in samples],
                "rx": [_num(s["rotation"][0]) for s in samples],
                "ry": [_num(s["rotation"][1]) for s in samples],
                "rz": [_num(s["rotation"][2]) for s in samples],
                "scale": [_num(s["scale"] * 100.0) for s in samples],
            }
            for name, samples in obj_samples.items()
        ],
    }
    # json.dumps escapes non-ASCII and quotes, so it is a valid ES3 literal.
    return (_JSX_TEMPLATE.replace("__VERSION__", core.VERSION)
            .replace("__DATA__", json.dumps(data, indent=None)))


def export_camera_jsx(context, settings, filepath=None):
    """Write the .jsx. Returns ``(path, warnings)``; raises core.SetupError."""
    scene = context.scene
    cam = scene.camera
    if cam is None:
        raise core.SetupError("The scene has no active camera to export.")
    if settings.output_path.startswith("//") and not bpy.data.filepath:
        raise core.SetupError("Save the .blend first: the output folder is relative to it.")
    if settings.px_per_unit <= 0:
        raise core.SetupError("Pixels per unit must be greater than 0.")

    anchors = anchor_objects(context)
    cam_samples, obj_samples = sample_scene(scene, cam, anchors, settings.px_per_unit)
    source = build_jsx(scene, settings, cam_samples, obj_samples)

    path = filepath or os.path.join(bpy.path.abspath(core.output_dir(settings)), JSX_NAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(source)
    return path, collect_warnings(scene, cam)


def add_text_anchor(context):
    """Create an upright Empty at the 3D cursor, tagged as an AE anchor."""
    scene = context.scene
    empty = bpy.data.objects.new(ANCHOR_NAME, None)
    empty.empty_display_type = "PLAIN_AXES"
    empty.empty_display_size = 0.5
    empty.location = scene.cursor.location
    empty.rotation_euler = (math.radians(90), 0.0, 0.0)  # upright, facing -Y
    empty[ANCHOR_PROP] = 1
    coll = context.collection or scene.collection
    if core.is_addon_collection(coll) or not core.collection_in_scene(scene, coll):
        coll = scene.collection
    coll.objects.link(empty)
    return empty


_JSX_TEMPLATE = r"""// AE Layer Split __VERSION__ (Blender add-on) generated this script.
// After Effects: File > Scripts > Run Script File... and pick this file.
(function () {
    var D = __DATA__;
    var missing = [];

    function prop(layer, group, name) {
        return layer.property(group).property(name);
    }
    function xform(layer, name) { return prop(layer, "ADBE Transform Group", name); }

    // isCamera: a one-node camera hides its Point of Interest (the
    // "ADBE Anchor Point" of camera layers), and setting a hidden property
    // throws, so leave it alone.
    function resetTransform(layer, isCamera) {
        if (!isCamera) { xform(layer, "ADBE Anchor Point").setValue([0, 0, 0]); }
        xform(layer, "ADBE Position").setValue([0, 0, 0]);
        xform(layer, "ADBE Orientation").setValue([0, 0, 0]);
        xform(layer, "ADBE Rotate X").setValue(0);
        xform(layer, "ADBE Rotate Y").setValue(0);
        xform(layer, "ADBE Rotate Z").setValue(0);
    }

    function setParent(child, parent) {
        if (child.setParentWithJump) { child.setParentWithJump(parent); }
        else { child.parent = parent; }
    }

    function same(a, b) {
        if (a instanceof Array) {
            for (var i = 0; i < a.length; i++) { if (a[i] !== b[i]) { return false; } }
            return true;
        }
        return a === b;
    }

    // Keyframe only what changes; a constant value stays a plain value so
    // the layer can still be dragged around in AE.
    function keys(property, times, values) {
        for (var i = 1; i < values.length; i++) {
            if (!same(values[i], values[0])) { property.setValuesAtTimes(times, values); return; }
        }
        property.setValue(values[0]);
    }

    function newNull(comp, name) {
        var n = comp.layers.addNull();
        n.name = name;
        n.threeDLayer = true;
        n.source.name = name;
        return n;
    }

    // Three chained layers: top (position + Y), middle (X), leaf (Z).
    function buildRig(comp, name, leaf, d, leafIsCamera) {
        var top = newNull(comp, name);
        var mid = newNull(comp, name + " X");
        resetTransform(top);
        resetTransform(mid);
        setParent(mid, top);
        setParent(leaf, mid);
        resetTransform(mid);
        resetTransform(leaf, leafIsCamera);
        keys(xform(top, "ADBE Position"), d.times, d.position);
        keys(xform(top, "ADBE Rotate Y"), d.times, d.ry);
        keys(xform(mid, "ADBE Rotate X"), d.times, d.rx);
        keys(xform(leaf, "ADBE Rotate Z"), d.times, d.rz);
        mid.shy = true;
        return top;
    }

    function importSequence(path, straightAlpha) {
        var file = new File(path);
        if (!file.exists) { missing.push(path); return null; }
        var options = new ImportOptions(file);
        options.sequence = true;
        var item = app.project.importFile(options);
        item.mainSource.conformFrameRate = D.fps;
        // AE refuses an alpha mode on footage that has no alpha channel.
        if (straightAlpha && item.mainSource.hasAlpha) { item.mainSource.alphaMode = AlphaMode.STRAIGHT; }
        return item;
    }

    app.beginUndoGroup("AE Split: Blender camera");
    try {
    var comp = app.project.items.addComp(D.compName, D.width, D.height, D.pixelAspect,
                                         D.duration, D.fps);
    try { comp.displayStartFrame = D.startFrame; } catch (e) {}

    // Render plates (2D layers; the camera does not move them).
    var bgLayer = null, charLayer = null;
    if (D.footage.EXR) {
        var exr = importSequence(D.footage.EXR, false);
        if (exr) {
            bgLayer = comp.layers.add(exr); bgLayer.name = "BG (EXR: pick BG.* in EXtractoR)";
            charLayer = comp.layers.add(exr); charLayer.name = "CHAR (EXR: pick CHAR.* in EXtractoR)";
        }
    } else {
        if (D.footage.BG) {
            var bg = importSequence(D.footage.BG, true);
            if (bg) { bgLayer = comp.layers.add(bg); bgLayer.name = "BG"; }
        }
        if (D.footage.CHAR) {
            var ch = importSequence(D.footage.CHAR, true);
            if (ch) { charLayer = comp.layers.add(ch); charLayer.name = "CHAR"; }
        }
    }

    // Anchors.
    var firstAnchorLeaf = null;
    for (var i = 0; i < D.anchors.length; i++) {
        var a = D.anchors[i];
        var leaf = newNull(comp, a.name + " Z");
        buildRig(comp, a.name, leaf, a);
        keys(xform(leaf, "ADBE Scale"), a.times,
             (function (s) { var v = []; for (var k = 0; k < s.length; k++) { v.push([s[k], s[k], s[k]]); } return v; })(a.scale));
        leaf.shy = true;
        if (!firstAnchorLeaf) { firstAnchorLeaf = leaf; }
    }

    // Placeholder 3D text, between BG and CHAR.
    if (D.text) {
        var text = comp.layers.addText(D.text);
        text.threeDLayer = true;
        var src = text.property("ADBE Text Properties").property("ADBE Text Document");
        var doc = src.value;
        doc.fontSize = D.textSize;
        doc.justification = ParagraphJustification.CENTER_JUSTIFY;
        src.setValue(doc);
        if (firstAnchorLeaf) {
            setParent(text, firstAnchorLeaf);
            resetTransform(text);
        }
        var r = text.sourceRectAtTime(0, false);
        xform(text, "ADBE Anchor Point").setValue([r.left + r.width / 2, r.top + r.height / 2, 0]);
        if (!firstAnchorLeaf) { xform(text, "ADBE Position").setValue([D.width / 2, D.height / 2, 0]); }
        if (bgLayer) { text.moveBefore(bgLayer); }
    }
    if (charLayer) { charLayer.moveToBeginning(); }

    // Camera.
    var cam = comp.layers.addCamera(D.camera.name, [D.width / 2, D.height / 2]);
    cam.autoOrient = AutoOrientType.NO_AUTO_ORIENT;
    buildRig(comp, "Camera Rig", cam, D.camera, true);
    keys(prop(cam, "ADBE Camera Options Group", "ADBE Camera Zoom"), D.camera.times, D.camera.zoom);
    try { prop(cam, "ADBE Camera Options Group", "ADBE Camera Depth of Field").setValue(0); } catch (e) {}
    cam.moveToBeginning();

    comp.openInViewer();
    } catch (err) {
        app.endUndoGroup();
        alert("AE Split " + D.version + ": the script stopped with an error"
              + (err.line ? " (line " + err.line + ")" : "") + ":\n" + err.toString()
              + "\n\nUndo (Ctrl+Z) removes the half-built comp.");
        return;
    }
    app.endUndoGroup();

    var msg = "AE Split " + D.version + ": comp '" + D.compName + "' created with the Blender camera and "
            + D.anchors.length + " anchor(s).";
    if (missing.length) {
        msg += "\n\nRenders not found (render first, then re-run or import by hand):\n" + missing.join("\n");
    }
    alert(msg);
})();
"""
