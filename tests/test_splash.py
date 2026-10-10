"""
The PyInstaller boot splash: the runtime accessor and the build-time Tcl patch.

Covers:
  * core.splash being a silent no-op in a development run (no pyi_splash), so
    main.py can call it unconditionally;
  * the progress-bar patch in build.spec still finding its footholds in
    PyInstaller's splash templates — this is the fragile half, since the bar is
    an extension of PyInstaller internals rather than a documented feature. If
    a PyInstaller upgrade renames the IPC dispatch marker or the canvas
    template, the build silently loses the bar; this fails instead.

The PyInstaller half is skipped when PyInstaller is absent (it lives in the
optional `build` extra, so `uv run pytest` normally does not have it).

Run with:
    uv run python tests/test_splash.py
"""

import ast
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import splash

SPEC = os.path.join(os.path.dirname(__file__), "..", "build.spec")


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _spec_namespace():
    """Exec just the splash constants and helpers out of build.spec.

    The spec as a whole only runs inside PyInstaller (it leans on injected
    globals like SPECPATH and Analysis), so the pieces under test are lifted
    out of its AST rather than imported.
    """
    with open(SPEC, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    picked = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id.startswith("SPLASH_")
                for t in node.targets):
            picked.append(node)
        elif (isinstance(node, ast.FunctionDef)
              and node.name in ("_png_size", "_patch_splash_templates")):
            picked.append(node)
    namespace = {"sys": sys, "struct": struct}
    exec(compile(ast.Module(body=picked, type_ignores=[]), SPEC, "exec"), namespace)
    return namespace


def test_runtime_is_a_noop_without_a_splash():
    print("test_runtime_is_a_noop_without_a_splash")
    _assert(splash.is_active() is False, "no splash in a development run")
    # None of these may raise: main.py calls them on every launch.
    splash.update("hello")
    splash.update(progress=0.5)
    splash.update("hello", 0.5)
    splash.update(progress=99.0)      # out of range, still harmless
    splash.close()
    splash.close()                    # idempotent
    _assert(True, "every entry point is a silent no-op")


def test_png_size_reads_the_real_artwork():
    print("test_png_size_reads_the_real_artwork")
    ns = _spec_namespace()
    image = os.path.join(os.path.dirname(SPEC), "assets", "images", "splash.png")
    _assert(os.path.exists(image), "the splash artwork is a PNG on disk")
    size = ns["_png_size"](image)
    _assert(size is not None and size[0] > 0 and size[1] > 0,
            f"_png_size read the header: {size}")
    x0, y0, x1, y1 = ns["SPLASH_BAR_RECT"]
    tx, ty = ns["SPLASH_TEXT_POS"]
    _assert(0 <= x0 < x1 <= size[0] and 0 <= y0 < y1 <= size[1],
            "the progress bar sits inside the artwork")
    _assert(0 <= tx <= size[0] and 0 <= ty <= size[1],
            "the status line sits inside the artwork")
    _assert(ns["_png_size"](SPEC) is None, "a non-PNG reads back as None")


def test_template_patch_still_applies():
    print("test_template_patch_still_applies")
    try:
        from PyInstaller.building import splash_templates
    except ImportError:
        print("  SKIP: PyInstaller not installed (optional `build` extra)")
        return

    ns = _spec_namespace()
    _assert(ns["_patch_splash_templates"]() is not False,
            "the patch found its footholds in PyInstaller's templates")

    script = splash_templates.build_script(
        text_options={
            "pad_x": ns["SPLASH_TEXT_POS"][0], "pad_y": ns["SPLASH_TEXT_POS"][1],
            "color": ns["SPLASH_TEXT_COLOR"], "font": "TkDefaultFont",
            "font_size": ns["SPLASH_TEXT_SIZE"], "default_text": "Starting up",
        },
        always_on_top=True,
    )
    for needle in ('string match "update_progress*"',
                   "global progress_value",
                   "-tag progress_track",
                   "-tag progress_fill",
                   "trace add variable progress_value write"):
        _assert(needle in script, f"generated script carries {needle!r}")
    _assert(script.count("{") == script.count("}"),
            "the generated Tcl has balanced braces")


def main():
    test_runtime_is_a_noop_without_a_splash()
    test_png_size_reads_the_real_artwork()
    test_template_patch_still_applies()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
