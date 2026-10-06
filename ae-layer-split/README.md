# AE Layer Split (Blender add-on)

One click splits your scene into a **character layer** and a **background layer**, ready for compositing in After Effects.

- `CHAR_layer`: the character only. Everything else is a *holdout*, so a pillar in front of the character still cuts it out. Optionally the character's shadow lands on a transparent shadow catcher floor (Cycles).
- `BG_layer`: everything except the character. In Cycles the character can stay *Indirect Only*, so its shadow and bounce light remain on the background.
- The compositor writes both as RGBA PNG sequences in separate folders, or as one multilayer EXR per frame.

Supported and tested: **Blender 4.2 LTS, 4.5 LTS, 5.0, 5.2 LTS**. The 5.0 compositor changes (compositor node group, new File Output node) are detected automatically.

## Install

1. Download `dist/ae_layer_split-1.0.0.zip`. Don't unzip it.
2. In Blender 4.2 or newer: **Edit → Preferences → Get Extensions → ⌄ (top right) → Install from Disk…** and pick the zip. You can also drag the zip into the Blender window.
   - Legacy route (same zip, uses `bl_info`): **Preferences → Add-ons → ⌄ → Install from Disk…**, then tick *AE Layer Split*.
3. In the 3D Viewport press **N** and open the **AE Split** tab.

To rebuild the zip from source: `python build.py`. No Blender needed.

## Use

1. **Character**: pick the character collection, or select the character (or just its rig) and press **Make CHAR from Selected**. This creates a `CHAR` collection holding the selection plus the whole rig (armature and child meshes). The objects are moved out of their old collections so they don't also count as background (Ctrl+Z undoes it).
2. **Background** (read only): every other top-level collection. If the character collection sits inside another collection, that collection is listed as *all but CHAR*.
3. **Options**
   - *Keep character shadows/bounce on BG*: in Cycles the character is *Indirect Only* in `BG_layer`. EEVEE has no Indirect Only, so there the character is simply excluded and its shadow will be missing on BG (the panel warns you).
   - *Shadow catcher floor* (optional, Cycles only): the character's shadow on this floor is rendered into the CHAR layer's alpha.
   - *Output*: **PNG RGBA sequence** (8 or 16 bit) or **OpenEXR Multilayer** (half or full float).
   - *Output folder*: defaults to `//render/`, next to the .blend. Save the .blend first.
4. Press **Setup Layers** (or **Setup + Render Animation**). If you change options later, press Setup Layers again; it re-applies cleanly.
5. **Remove Setup** puts everything back. This also works after saving and reopening the file.

Output layout:

```
render/
  CHAR/CHAR_0001.png …      (PNG mode)
  BG/BG_0001.png …
  AE_split_0001.exr …       (EXR mode: layers "CHAR" and "BG")
  _main/                    Blender's own render output, can be deleted
```

### What Setup changes (and Remove Setup restores)

| Change | Restored |
|---|---|
| Film → Transparent on, Post Processing → Compositing on | yes |
| Your view layers keep their contents but get *Use for Rendering* turned off | yes |
| `CHAR_layer` / `BG_layer` created (or reused) | deleted |
| Compositor: Render Layers + File Output nodes in a frame labelled **AE Split** (your own nodes are left alone) | nodes removed |
| Render output path → `<output>/_main/` so the main render doesn't mix with the layers | yes |
| Helper collections `AE_Split_*` (see below) | deleted |

The originals are stored as JSON in the scene custom property `ae_split_state`. Every operator supports Ctrl+Z.

Helper collections, and why they exist:

- `AE_Split_Holdout`: Blender can only hold out whole collections. Background objects that sit directly in the Scene Collection, or next to the character, lights or the floor in the same collection, are *also* linked here. Linking doesn't move anything.
- `AE_Split_KeepLights`: lights or cameras inside the character collection, kept in `BG_layer`.
- `AE_Split_Floor` / `AE_Split_ShadowCatcher`: the shadow catcher flag is global in Blender, so the floor is moved to `AE_Split_Floor` (rendered in BG only) and a linked duplicate that shares its mesh becomes the shadow catcher (CHAR only). Remove Setup moves the floor back and deletes the duplicate.

Lights and cameras are never holdout or excluded.

## Import into After Effects

**PNG sequences**
1. **File → Import → File…**, open `CHAR/`, select `CHAR_0001.png`, tick **PNG Sequence**, then Import. Do the same for `BG/`.
2. Select each footage item → **File → Interpret Footage → Main…** → **Alpha: Straight – Unmatted**. Blender writes PNG alpha straight (un-premultiplied); "Premultiplied" gives dark fringes.
3. Set the frame rate in the same dialog to match Blender (Output Properties → Frame Rate).
4. Put `CHAR` above `BG` in a comp.

**OpenEXR multilayer**
1. Import `AE_split_0001.exr` as an **OpenEXR Sequence**. AE reads EXR natively.
2. Put the footage in a comp twice. On each copy apply **Effect → 3D Channel → EXtractoR** (bundled with AE). Click the channel list and pick `CHAR.R / CHAR.G / CHAR.B / CHAR.A` on one and the `BG.*` channels on the other.
3. EXR is linear and premultiplied. Work in a linear (32-bpc) project, or set the project's working color space so it matches your Blender view transform (AgX/Filmic is applied to PNGs only, not to EXR).

The EXR is written single-part (interleaved), the layout older and newer AE versions both read.

## Test

```
blender --background --python-exit-code 1 --python test.py            # full test incl. a tiny Cycles render
blender --background --python-exit-code 1 --python test.py -- --no-render
```

The test loads the add-on from the `ae_layer_split/` folder next to it (no install needed). It builds a dummy scene (cube character in a nested collection, plane floor, wall, a foreground pillar, a sun, loose objects) and checks:

- view layers
- holdout / indirect-only per object
- shadow catcher swap
- compositor nodes and links
- the alpha of real rendered PNG pixels (character opaque, pillar cut-out, caught shadow, transparent wall)
- EXR channels
- Remove Setup, also after save + reopen
- EEVEE behaviour
- Make CHAR from Selected
- error cases
- the panel draw

## Source layout

```
ae_layer_split/
  __init__.py              bl_info + register/unregister
  blender_manifest.toml    Extensions manifest (4.2+)
  compat.py                4.x vs 5.x compositor / File Output API
  core.py                  scene analysis, Setup Layers, Remove Setup
  properties.py            scene settings (scene.ae_split)
  operators.py             the four operators
  ui.py                    N-panel
test.py                    background test
build.py                   builds dist/ae_layer_split-<version>.zip
```

## Known limits

- An object that is in the character collection *and* in a background collection is treated as character. Make CHAR from Selected avoids this by moving objects.
- New view layers copy the *exclude* checkboxes of your current view layer, but not its passes or other per-layer settings.
- EEVEE: no Indirect Only and no shadow catcher, so the character's shadow can't be split off. Use Cycles for that.
