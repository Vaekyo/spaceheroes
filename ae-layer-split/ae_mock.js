// Minimal After Effects scripting mock, used by test.py to run the generated
// ae_camera.jsx without After Effects.
//
//   node ae_mock.js <script.jsx> <out.json> [--no-jump]
//
// It implements just the part of the AE object model the script uses, and
// enforces the AE rules that have bitten us:
//   * setting a hidden property throws (a one-node camera's Point of
//     Interest; Orientation / X / Y Rotation on a 2D layer),
//   * layer.property(name) returns null for unknown match names,
//   * `layer.parent = p` (old AE, no setParentWithJump) compensates the
//     child's transform, so any value the script relies on must be set again.
// The resulting comp is written as JSON so test.py can rebuild the camera.
"use strict";
const fs = require("fs");
const vm = require("vm");

const [scriptPath, outPath, ...flags] = process.argv.slice(2);
const withJump = !flags.includes("--no-jump");

const AutoOrientType = { NO_AUTO_ORIENT: "NO_AUTO_ORIENT", CAMERA_OR_POINT_OF_INTEREST: "CAMERA_OR_POI", ALONG_PATH: "ALONG_PATH" };
const AlphaMode = { STRAIGHT: "STRAIGHT", PREMULTIPLIED: "PREMULTIPLIED", IGNORE: "IGNORE" };
const ParagraphJustification = { CENTER_JUSTIFY: "CENTER", LEFT_JUSTIFY: "LEFT" };

class Property {
  constructor(layer, matchName, value, isHidden) {
    this.layer = layer;
    this.matchName = matchName;
    this.value = value;
    this.keys = null;
    this.isHidden = isHidden || (() => false);
  }
  check(v) {
    if (this.isHidden()) {
      throw new Error(`Can not 'set value' with property '${this.matchName}' of '${this.layer.name}', because the property or a parent property is hidden.`);
    }
    if (Array.isArray(this.value) && (!Array.isArray(v) || v.length !== this.value.length)) {
      throw new Error(`${this.matchName}: expected ${this.value.length} values, got ${JSON.stringify(v)}`);
    }
  }
  setValue(v) {
    this.check(v);
    if (this.keys) { throw new Error(`${this.matchName}: setValue on a keyframed property`); }
    this.value = v;
  }
  setValuesAtTimes(times, values) {
    if (times.length !== values.length) { throw new Error(`${this.matchName}: times/values length mismatch`); }
    values.forEach((v) => this.check(v));
    this.keys = { times: times.slice(), values: values.slice() };
  }
}

class Group {
  constructor(props) { this.props = props; }
  property(name) { return this.props[name] || null; }
}

let nextId = 1;

class Layer {
  constructor(comp, name, kind, source) {
    this.id = nextId++;
    this.comp = comp;
    this.name = name;
    this.kind = kind;
    this.source = source || null;
    this._threeD = kind === "camera";
    this.autoOrient = kind === "camera" ? AutoOrientType.CAMERA_OR_POINT_OF_INTEREST : null;
    this._parent = null;
    this.shy = false;
    const w = comp.width, h = comp.height;
    const not3D = () => !this._threeD;
    const t = {
      "ADBE Anchor Point": new Property(this, "ADBE Anchor Point",
        kind === "camera" ? [w / 2, h / 2, 0] : kind === "null" ? [50, 50, 0] : [0, 0, 0],
        () => kind === "camera" && this.autoOrient === AutoOrientType.NO_AUTO_ORIENT),
      "ADBE Position": new Property(this, "ADBE Position", kind === "camera" ? [w / 2, h / 2, -2666.7] : [w / 2, h / 2, 0]),
      "ADBE Scale": new Property(this, "ADBE Scale", [100, 100, 100]),
      "ADBE Orientation": new Property(this, "ADBE Orientation", [0, 0, 0], not3D),
      "ADBE Rotate X": new Property(this, "ADBE Rotate X", 0, not3D),
      "ADBE Rotate Y": new Property(this, "ADBE Rotate Y", 0, not3D),
      "ADBE Rotate Z": new Property(this, "ADBE Rotate Z", 0),
      "ADBE Opacity": new Property(this, "ADBE Opacity", 100),
    };
    this.groups = { "ADBE Transform Group": new Group(t) };
    if (kind === "camera") {
      this.groups["ADBE Camera Options Group"] = new Group({
        "ADBE Camera Zoom": new Property(this, "ADBE Camera Zoom", 2666.7),
        "ADBE Camera Depth of Field": new Property(this, "ADBE Camera Depth of Field", 0),
      });
    }
    if (kind === "text") {
      this.textDocument = { text: name, fontSize: 36, justification: "LEFT" };
      const doc = new Property(this, "ADBE Text Document", this.textDocument);
      doc.check = () => {};
      this.groups["ADBE Text Properties"] = new Group({ "ADBE Text Document": doc });
    }
  }
  get threeDLayer() { return this._threeD; }
  set threeDLayer(v) {
    if (this.kind === "camera") { throw new Error("camera layers are always 3D"); }
    this._threeD = !!v;
  }
  property(name) { return this.groups[name] || null; }
  get index() { return this.comp._layers.indexOf(this) + 1; }
  get parent() { return this._parent; }
  set parent(p) {
    this._parent = p;
    // Old-style parenting keeps the world transform by rewriting the child's
    // values. The mock just scrambles them: the script must set them again.
    const t = this.groups["ADBE Transform Group"].props;
    for (const n of ["ADBE Position", "ADBE Orientation", "ADBE Rotate X", "ADBE Rotate Y", "ADBE Rotate Z"]) {
      if (!t[n].keys && !t[n].isHidden()) {
        t[n].value = Array.isArray(t[n].value) ? t[n].value.map((x) => x + 123.4) : t[n].value + 12.3;
      }
    }
  }
  moveBefore(other) { const a = this.comp._layers; a.splice(a.indexOf(this), 1); a.splice(a.indexOf(other), 0, this); }
  moveToBeginning() { const a = this.comp._layers; a.splice(a.indexOf(this), 1); a.unshift(this); }
  sourceRectAtTime() {
    const d = this.textDocument;
    return { left: 0, top: -0.7 * d.fontSize, width: 0.6 * d.fontSize * d.text.length, height: 0.7 * d.fontSize };
  }
}
if (withJump) {
  Layer.prototype.setParentWithJump = function (p) { this._parent = p; };
}

class Comp {
  constructor(name, width, height, pixelAspect, duration, frameRate) {
    Object.assign(this, { name, width, height, pixelAspect, duration, frameRate });
    if (!(width >= 4 && width <= 30000 && height >= 4 && height <= 30000)) { throw new Error("bad comp size"); }
    if (!(duration > 0 && frameRate > 0)) { throw new Error("bad comp duration/fps"); }
    this._layers = [];
    this.displayStartFrame = 0;
    const comp = this;
    this.layers = {
      addNull() { return comp._add(new Layer(comp, "Null", "null", { name: "Null" })); },
      addCamera(name, center) {
        if (!Array.isArray(center) || center.length !== 2) { throw new Error("addCamera(name, [x, y])"); }
        return comp._add(new Layer(comp, name, "camera"));
      },
      addText(text) {
        const l = new Layer(comp, text, "text");
        l.textDocument.text = text;
        return comp._add(l);
      },
      add(item) { return comp._add(new Layer(comp, item.name, "footage", item)); },
    };
  }
  _add(layer) { this._layers.unshift(layer); return layer; }
  openInViewer() {}
}

const alerts = [];
const comps = [];
const imported = [];
const sandbox = {
  app: {
    project: {
      items: { addComp(...a) { const c = new Comp(...a); comps.push(c); return c; } },
      importFile(opts) {
        const item = { name: opts.file.path.split("/").pop(), path: opts.file.path, sequence: opts.sequence,
                       mainSource: { alphaMode: "PREMULTIPLIED", conformFrameRate: 0 } };
        imported.push(item);
        return item;
      },
    },
    beginUndoGroup() {}, endUndoGroup() {},
  },
  File: function (path) { this.path = path; this.exists = fs.existsSync(path); },
  ImportOptions: function (file) { this.file = file; this.sequence = false; },
  AutoOrientType, AlphaMode, ParagraphJustification,
  alert(msg) { alerts.push(String(msg)); },
};
vm.createContext(sandbox);

let error = null;
try {
  vm.runInContext(fs.readFileSync(scriptPath, "utf8"), sandbox, { filename: scriptPath });
} catch (e) {
  error = String(e && e.stack || e);
}

function dumpProp(p) {
  return p.keys ? { keys: p.keys } : { value: p.value };
}
const out = {
  error,
  alerts,
  imported: imported.map((i) => ({ path: i.path, sequence: i.sequence, alphaMode: i.mainSource.alphaMode, fps: i.mainSource.conformFrameRate })),
  comps: comps.map((c) => ({
    name: c.name, width: c.width, height: c.height, pixelAspect: c.pixelAspect,
    duration: c.duration, frameRate: c.frameRate, displayStartFrame: c.displayStartFrame,
    layers: c._layers.map((l) => {
      const props = {};
      for (const [gname, g] of Object.entries(l.groups)) {
        for (const [pname, p] of Object.entries(g.props)) { props[pname] = dumpProp(p); }
      }
      return { index: l.index, id: l.id, name: l.name, kind: l.kind, threeD: l.threeDLayer,
               autoOrient: l.autoOrient, parent: l.parent ? l.parent.id : null, shy: l.shy,
               source: l.source && l.source.path ? l.source.path : null,
               text: l.textDocument || null, props };
    }),
  })),
};
fs.writeFileSync(outPath, JSON.stringify(out));
