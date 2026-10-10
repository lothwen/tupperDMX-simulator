# -*- mode: python ; coding: utf-8 -*-
import os
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Build configuration
app_name = "TupperDMX-Simulator"
main_script = "main.py"

# Capture git metadata at build time and freeze it into _buildinfo.py.
# The runtime accessor in core/buildinfo.py imports this module when the app is
# frozen by PyInstaller; in dev runs the file may be absent and a live `git`
# fallback is used instead.
def _generate_buildinfo() -> None:
    project_root = Path(SPECPATH).resolve()

    def git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return ""

    commit = git("rev-parse", "--short", "HEAD") or "unknown"
    tag = git("describe", "--tags", "--always", "--dirty") or "unknown"
    branch = git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    build_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    output = project_root / "_buildinfo.py"
    output.write_text(
        "# ABOUTME: Auto-generated at PyInstaller build time. Do not edit by hand.\n"
        "# ABOUTME: Captures git metadata so the frozen app can show its provenance.\n"
        f"BUILD_DATE = {build_date!r}\n"
        f"GIT_COMMIT = {commit!r}\n"
        f"GIT_TAG = {tag!r}\n"
        f"GIT_BRANCH = {branch!r}\n",
        encoding="utf-8",
    )

_generate_buildinfo()

# Data files and assets.
# config.json holds shipped, read-only data (fixtures + midi_note_map) and is
# resolved at runtime via sys._MEIPASS. User-writable preferences live in
# userprefs.json under the platform's user-config dir (see core.config.userprefs_path)
# and are intentionally NOT bundled here.
data_files = [
    ("assets", "assets"),
    ("config.json", "."),
    ("fixtures", "fixtures"),
    ("webremote/static", "webremote/static"),
]

# piper needs its espeak-ng-data phoneme tables next to the module.
from PyInstaller.utils.hooks import collect_data_files
data_files += collect_data_files("piper")

# Hidden imports for PyQt6 and other dependencies
hidden_imports = [
    # Core Python modules
    "dataclasses",
    "inspect",
    "typing",
    "collections",
    "functools",
    "itertools",
    "pathlib",
    
    # PyQt6 modules
    "PyQt6.QtCore",
    "PyQt6.QtGui", 
    "PyQt6.QtWidgets",
    "PyQt6.QtMultimedia",
    "PyQt6.QtMultimediaWidgets",
    "PyQt6.QtOpenGL",
    # QOpenGLWidget lives here, NOT in QtOpenGL — without this the GPU stage
    # path dies with an ImportError in a frozen build only, never in dev.
    "PyQt6.QtOpenGLWidgets",

    # MIDI
    "mido",
    "mido.backends",
    "mido.backends.rtmidi",
    "rtmidi",
    "python_rtmidi",
    
    # DMX
    "pyartnet",
    "serial",
    "serial.tools.list_ports",
    
    # Other
    "mutagen",
    "numpy",

    # TTS (voice models are downloaded at runtime into the userprefs dir,
    # never bundled; piper ships its espeak-ng phoneme data as package data —
    # collected via collect_data_files below)
    "piper",
    "onnxruntime",

    # Build metadata
    "core.buildinfo",
    "_buildinfo",
]

# Excluded modules to reduce size
excludes = [
    "tkinter",
    "doctest",
    "pdb",
    "profile",
    "pstats",
    "trace",
]

# Boot splash screen.
#
# This one is drawn by the *bootloader* (Tcl/Tk), not by Qt, which is the whole
# point: in onefile mode it is already on screen while the archive is still
# unpacking, seconds before Python exists. `core/splash.py` drives it from the
# app side.
#
# Not supported on macOS: `Splash()` raises SystemExit there, because the screen
# runs on a secondary thread and Cocoa forbids UI work off the main thread. The
# mac build therefore simply has no splash, and `core.splash` no-ops.
#
# Note that `excludes` below drops the `tkinter` module — that is fine and
# expected. Splash bundles the Tcl/Tk shared libraries and the handful of .tcl
# files it needs by itself; it only reads the Analysis TOCs to notice whether
# the *application* also uses tkinter (it does not).
splash_image = "assets/images/splash.png"

# Both are in image pixels with the origin at the top left, so they have to be
# rescaled whenever the artwork is. The status line is anchored by its LOWER-
# left corner. Nothing outside this block needs to know. Sized for the 640x300
# splash.png; scripts/preview_splash.py refuses to run if they fall off it.
SPLASH_TEXT_POS = (161, 186)
SPLASH_TEXT_SIZE = 9              # points, not pixels: independent of the image
SPLASH_TEXT_COLOR = "#9aa4b2"
SPLASH_BAR_RECT = (161, 196, 573, 199)  # x0, y0, x1, y1
SPLASH_BAR_TRACK = "#2a2e35"
SPLASH_BAR_FILL = "#6b8cff"


def _png_size(path):
    """(width, height) of a PNG, or None if the file is not one.

    Splash compares the image against `max_img_size` and needs Pillow to shrink
    anything larger — which is not a build dependency here. Reading the real
    dimensions and handing them back as the limit means a redesigned splash
    image can be any size without the build suddenly demanding Pillow.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack("!II", header[16:24])


def _patch_splash_templates():
    """Add an `update_progress` IPC command and a progress bar to the splash.

    PyInstaller's splash screen ships a status *line* and nothing else; the
    generated Tcl even carries an `# Implement other procedures here` marker
    inside its IPC dispatcher. `build_script()` composes the script out of
    module-level template strings at call time, so filling that marker in and
    appending two rectangles to the canvas is enough to get a real bar —
    without vendoring PyInstaller's script wholesale.

    Failing to patch is not fatal: the splash keeps its status line and
    `core.splash.update(progress=...)` quietly does nothing.
    """
    from PyInstaller.building import splash_templates

    dispatch_marker = "# Implement other procedures here"
    if dispatch_marker not in splash_templates.ipc_script:
        print("warning: splash IPC template changed shape; no progress bar",
              file=sys.stderr)
        return False

    splash_templates.ipc_script = splash_templates.ipc_script.replace(
        dispatch_marker,
        """# update_progress command, sent by core/splash.py
        if {[string match "update_progress*" $cmd]} {
            global progress_value
            set first [expr {[string first "(" $cmd] + 1}]
            set last [expr {[string last ")" $cmd] - 1}]

            set progress_value [string range $cmd $first $last]
        }""",
    )

    x0, y0, x1, y1 = SPLASH_BAR_RECT
    # One command per line on purpose: the script is minified by stripping and
    # re-joining lines, which a Tcl backslash-continuation survives but does
    # not need to.
    splash_templates.splash_canvas_setup += f"""
# Progress bar: a static track plus a fill whose right edge follows the global
# progress_value (0.0 - 1.0), pushed over IPC by core/splash.py.
.root.canvas create rectangle {x0} {y0} {x1} {y1} -fill "{SPLASH_BAR_TRACK}" -outline "" -tag progress_track
.root.canvas create rectangle {x0} {y0} {x0} {y1} -fill "{SPLASH_BAR_FILL}" -outline "" -tag progress_fill

proc canvas_progress_update {{canvas tag _var - -}} {{
    upvar $_var var
    set edge [expr {{{x0} + ({x1} - {x0}) * $var}}]
    $canvas coords $tag {x0} {y0} $edge {y1}
}}
trace add variable progress_value write [list canvas_progress_update .root.canvas progress_fill]
set progress_value 0.0
"""
    return True


# Platform-specific configurations
if sys.platform == "win32":
    icon_file = "assets/icons/TupperDMX-icon.ico"
    console = False
    windowed = True
elif sys.platform == "darwin":
    icon_file = "assets/icons/TupperDMX-icon.icns"
    console = False
    windowed = True
else:  # Linux
    icon_file = "assets/icons/TupperDMX-icon.png"
    console = False
    windowed = True

block_cipher = None

a = Analysis(
    [main_script],
    pathex=[],
    binaries=[],
    datas=data_files,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)


# Linux: strip the executable-stack flag off any bundled shared library.
#
# python-build-standalone (what `uv python install` gives you, and what CI and
# every dev machine here builds against) ships `libpython3.x.so` with
# PT_GNU_STACK marked RWE — it asks the loader for an executable stack. Linux
# 6.x and newer refuse that for a shared object, so the frozen app dies at
# startup, before any of our code runs:
#
#   Failed to load Python shared library '/tmp/_MEIxxxxxx/libpython3.14.so.1.0':
#   cannot enable executable stack as shared object requires: Invalid argument
#
# The flag is spurious (CPython does not need an executable stack; it survives
# from an assembly object with no .note.GNU-stack section) and distro builds do
# not have it — Arch's own libpython is RW. Rather than depend on `execstack`
# or a patchelf new enough to have --clear-execstack, clear PF_X directly: it
# is one bit in one field of the program header.
PT_GNU_STACK = 0x6474E551
PF_X = 0x1


def _execstack_flags_offset(path):
    """Offset of PT_GNU_STACK's p_flags in `path`, if it has PF_X set.

    Returns (offset, flags) or None — None meaning "nothing to do here",
    which covers non-ELF files, 32-bit ELF (never shipped) and the normal
    case of a library that already declares a non-executable stack.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(64)
            if len(header) < 64 or header[:4] != b"\x7fELF":
                return None
            if header[4] != 2:  # not ELFCLASS64
                return None
            endian = "<" if header[5] == 1 else ">"
            e_phoff = struct.unpack_from(endian + "Q", header, 0x20)[0]
            e_phentsize = struct.unpack_from(endian + "H", header, 0x36)[0]
            e_phnum = struct.unpack_from(endian + "H", header, 0x38)[0]
            if not e_phoff or e_phentsize < 8:
                return None
            handle.seek(e_phoff)
            table = handle.read(e_phentsize * e_phnum)
    except OSError:
        return None

    for index in range(e_phnum):
        base = index * e_phentsize
        if base + 8 > len(table):
            break
        p_type, p_flags = struct.unpack_from(endian + "II", table, base)
        if p_type == PT_GNU_STACK and (p_flags & PF_X):
            # p_flags is the second field of a 64-bit program header.
            return e_phoff + base + 4, p_flags, endian
    return None


def _clear_execstack(binaries):
    """Return `binaries` with every executable-stack library swapped for a fix.

    The originals live in the read-only interpreter install, so each one is
    copied into the build directory and patched there; the TOC entry is
    repointed at the copy so PyInstaller bundles the fixed file.
    """
    if not sys.platform.startswith("linux"):
        return binaries

    from PyInstaller.config import CONF
    patched_dir = Path(CONF.get("workpath", ".")) / "execstack-fixed"

    result = []
    fixed = 0
    for entry in binaries:
        dest_name, src_name = entry[0], entry[1]
        found = _execstack_flags_offset(src_name) if src_name else None
        if found is None:
            result.append(entry)
            continue

        offset, flags, endian = found
        patched_dir.mkdir(parents=True, exist_ok=True)
        target = patched_dir / os.path.basename(src_name)
        shutil.copy2(src_name, target)
        os.chmod(target, os.stat(target).st_mode | 0o200)  # the source may be read-only
        with open(target, "r+b") as handle:
            handle.seek(offset)
            handle.write(struct.pack(endian + "I", flags & ~PF_X))
        print(f"note: cleared the executable-stack flag on {dest_name}")
        result.append((dest_name, str(target), *entry[2:]))
        fixed += 1

    if fixed:
        print(f"note: {fixed} shared librar{'y' if fixed == 1 else 'ies'} patched "
              "(see the PT_GNU_STACK comment in build.spec)")
    return result


a.binaries = _clear_execstack(a.binaries)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
splash = None
if sys.platform == "darwin":
    print("note: macOS does not support the PyInstaller splash screen; skipping.")
elif not Path(splash_image).exists():
    print(f"warning: splash image {splash_image} not found; building without one.",
          file=sys.stderr)
else:
    _patch_splash_templates()
    try:
        splash = Splash(
            splash_image,
            binaries=a.binaries,
            datas=a.datas,
            text_pos=SPLASH_TEXT_POS,
            text_size=SPLASH_TEXT_SIZE,
            text_color=SPLASH_TEXT_COLOR,
            text_default="Starting up...",
            # Must be >= the artwork or Splash tries to resize and demands
            # Pillow; it is compared as a plain tuple, so hand it the image's
            # own size.
            max_img_size=_png_size(splash_image) or (1280, 600),
            always_on_top=True,
        )
    except (SystemExit, Exception) as exc:
        # A splash is a nicety; an executable is not. Splash() aborts the whole
        # build via SystemExit when it cannot find the Tcl/Tk shared libraries,
        # which is exactly what happens on Linux with a uv-managed (python-
        # build-standalone) interpreter: its _tkinter.so names libtcl/libtk with
        # no RPATH, so PyInstaller cannot resolve them even though `import
        # tkinter` works. See BUILD.md for the LD_LIBRARY_PATH workaround.
        splash = None
        print(f"warning: splash screen unavailable ({exc}); building without one.",
              file=sys.stderr)

# The splash target and its binaries go between the scripts and the rest.
splash_targets = [splash, splash.binaries] if splash is not None else []

exe = EXE(
    pyz,
    a.scripts,
    *splash_targets,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=console,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file if Path(icon_file).exists() else None,
)

# macOS app bundle
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name=f"{app_name}.app",
        icon=icon_file if Path(icon_file).exists() else None,
        bundle_identifier="com.tupperdmx.simulator",
        version="1.1.0",
        info_plist={
            "CFBundleName": app_name,
            "CFBundleDisplayName": app_name,
            "CFBundleVersion": "1.1.0",
            "CFBundleShortVersionString": "1.1.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.15.0",
        },
    )