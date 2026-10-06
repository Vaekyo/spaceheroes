"""Build the installable zip: ``python build.py`` -> dist/ae_layer_split-<version>.zip

The zip holds a single ``ae_layer_split/`` folder with ``__init__.py`` (bl_info)
and ``blender_manifest.toml``, so it installs both as a Blender 4.2+ extension
and as a legacy add-on. Plain Python, no Blender needed.
"""

import os
import re
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = "ae_layer_split"


def main():
    src = os.path.join(HERE, PKG)
    with open(os.path.join(src, "blender_manifest.toml"), encoding="utf-8") as f:
        version = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M).group(1)

    os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
    out = os.path.join(HERE, "dist", f"{PKG}-{version}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(os.listdir(src)):
            if name.endswith((".py", ".toml")):
                zf.write(os.path.join(src, name), f"{PKG}/{name}")
    print(out)


if __name__ == "__main__":
    main()
