# ABOUTME: Dev preview of the PyInstaller boot splash — no build required.
# ABOUTME: Runs the REAL generated Tcl in Tk and rehearses main.py's milestones.
"""Show the boot splash exactly as a frozen build would, in a couple of seconds.

A full onefile build takes minutes, which is a miserable loop for positioning a
status line and a progress bar. This generates the same Tcl script `build.spec`
hands to PyInstaller — same constants, same template patch — runs it in a real
Tk interpreter, and drives it over the same IPC protocol `core/splash.py` uses.
What you see here is what the bootloader draws.

    uv run --with pyinstaller python scripts/preview_splash.py
    uv run --with pyinstaller python scripts/preview_splash.py --image other.png

Needs PyInstaller (the optional `build` extra) and a display. On macOS there is
no splash to preview; see BUILD.md.
"""

import argparse
import ast
import base64
import os
import re
import socket
import struct
import sys
import time
import tkinter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "build.spec")

# Mirrors run_gui() in main.py — keep the two in step so the preview rehearses
# what the app actually reports.
MILESTONES = [
    ("Starting Qt...", 0.10),
    ("Loading configuration...", 0.30),
    ("Opening the show...", 0.50),
    ("Building the interface...", 0.75),
    ("Ready", 1.00),
]


def spec_namespace():
    """Exec the splash constants and helpers straight out of build.spec.

    The spec only runs inside PyInstaller (it leans on injected globals like
    SPECPATH and Analysis), so the relevant pieces are lifted from its AST.
    That keeps this preview honest: it cannot drift from the real build.
    """
    with open(SPEC, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    picked = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name)
                and (t.id.startswith("SPLASH_") or t.id == "splash_image")
                for t in node.targets):
            picked.append(node)
        elif (isinstance(node, ast.FunctionDef)
              and node.name in ("_png_size", "_patch_splash_templates")):
            picked.append(node)
    namespace = {"sys": sys, "struct": struct}
    exec(compile(ast.Module(body=picked, type_ignores=[]), SPEC, "exec"), namespace)
    return namespace


def build_tcl(ns):
    """The exact script PyInstaller would generate, minified the same way."""
    from PyInstaller.building import splash_templates

    ns["_patch_splash_templates"]()
    script = splash_templates.build_script(
        text_options={
            "pad_x": ns["SPLASH_TEXT_POS"][0],
            "pad_y": ns["SPLASH_TEXT_POS"][1],
            "color": ns["SPLASH_TEXT_COLOR"],
            "font": "TkDefaultFont",
            "font_size": ns["SPLASH_TEXT_SIZE"],
            "default_text": "Starting up...",
        },
        always_on_top=True,
    )
    # Splash.generate_script() strips comments and collapses runs of spaces.
    script = "\n".join(
        line for line in map(str.strip, script.splitlines())
        if line and not line.startswith("#")
    )
    return re.sub(" +", " ", script)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=None,
                        help="preview a different PNG instead of the one build.spec names")
    parser.add_argument("--step", type=float, default=0.9,
                        help="seconds to hold each milestone (default: 0.9)")
    parser.add_argument("--hold", type=float, default=2.0,
                        help="seconds to keep the finished splash up (default: 2.0)")
    args = parser.parse_args()

    if sys.platform == "darwin":
        print("macOS builds have no splash screen (see BUILD.md).", file=sys.stderr)
        return 1

    ns = spec_namespace()
    image = args.image or os.path.join(ROOT, ns["splash_image"])
    size = ns["_png_size"](image)
    if size is None:
        print(f"{image}: not a PNG. The splash only accepts PNG.", file=sys.stderr)
        return 1
    width, height = size
    x0, y0, x1, y1 = ns["SPLASH_BAR_RECT"]
    tx, ty = ns["SPLASH_TEXT_POS"]
    outside = []
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        outside.append(f"SPLASH_BAR_RECT {ns['SPLASH_BAR_RECT']}")
    if not (0 <= tx <= width and 0 <= ty <= height):
        outside.append(f"SPLASH_TEXT_POS {ns['SPLASH_TEXT_POS']}")
    if outside:
        print(f"{' and '.join(outside)} falls outside the {width}x{height} image.\n"
              "These are image pixels, so resizing the artwork means rescaling "
              "them too. Edit the constants in build.spec.", file=sys.stderr)
        return 1

    print(f"image  : {image} ({size[0]}x{size[1]})")
    print(f"text   : {ns['SPLASH_TEXT_POS']} size {ns['SPLASH_TEXT_SIZE']} {ns['SPLASH_TEXT_COLOR']}")
    print(f"bar    : {ns['SPLASH_BAR_RECT']} {ns['SPLASH_BAR_FILL']} on {ns['SPLASH_BAR_TRACK']}")

    with open(image, "rb") as handle:
        payload = base64.b64encode(handle.read()).decode("ascii")

    root = tkinter.Tk()
    # Hidden only until the script has built the canvas, or an empty default
    # window flashes first. It must be shown again afterwards: the splash Tcl
    # packs its canvas into `.` itself and never deiconifies, because under the
    # real bootloader `.` was never withdrawn in the first place.
    root.withdraw()
    root.tk.call("set", "_image_data", payload)
    try:
        root.tk.eval(build_tcl(ns))
    except tkinter.TclError as exc:
        print(f"the splash script failed: {exc}", file=sys.stderr)
        return 1
    root.deiconify()

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            root.update()
            time.sleep(0.01)

    pump(0.3)
    # Drive it the way core/splash.py does, over the bootloader's own IPC.
    port = int(root.tk.eval("set env(_PYI_SPLASH_IPC)"))
    sock = socket.create_connection(("127.0.0.1", port))
    try:
        for text, progress in MILESTONES:
            sock.sendall(f"update_text({text})".encode("utf-8") + b"\x0d")
            sock.sendall(f"update_progress({progress:.4f})".encode("utf-8") + b"\x0d")
            print(f"  {progress:5.0%}  {text}")
            pump(args.step)
        pump(args.hold)
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
