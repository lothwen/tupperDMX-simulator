# Build Instructions

This document explains how to build TupperDMX Simulator for different operating systems using the integrated uv build system.

## Quick Start

```bash
# Install build dependencies
make dev

# Build for current platform
make build

# Build executable only (no installer)
uv run python scripts/build.py
```

## Platform-Specific Builds

### Windows
```bash
make build-windows
```
Creates:
- `dist/TupperDMX-Simulator.exe` - Standalone executable
- `dist/TupperDMX-Simulator-Setup.exe` - Windows installer (requires NSIS)

**Requirements:**
- NSIS 3.0+ (for installer creation)
- Visual Studio Build Tools (for some dependencies)

### macOS
```bash
make build-macos
```
Creates:
- `dist/TupperDMX-Simulator.app` - macOS app bundle
- `dist/TupperDMX-Simulator.dmg` - Disk image for distribution

**Requirements:**
- Xcode Command Line Tools
- macOS 10.15+ (Catalina)

### Linux
```bash
make build-linux
```
Creates:
- `dist/TupperDMX-Simulator` - Standalone executable

**System Dependencies:**
```bash
# Ubuntu/Debian
sudo apt-get install libasound2-dev portaudio19-dev

# Fedora/CentOS
sudo dnf install alsa-lib-devel portaudio-devel

# Arch Linux
sudo pacman -S alsa-lib portaudio
```

USB DMX dongles use the stock OS serial drivers (Windows FTDI VCP driver
via Windows Update, Linux `ftdi_sio` — add the user to the `dialout`/`uucp`
group for port access). No libftdi/libusb setup is needed.

## Build Configuration

### PyInstaller Spec File
The build uses `build.spec` for PyInstaller configuration:
- **Hidden Imports:** Includes all necessary PyQt6, audio, and MIDI modules
- **Data Files:** Bundles assets, icons, and configuration
- **Platform Icons:** Automatically uses appropriate icon format
- **Size Optimization:** Excludes unnecessary modules

### Boot Splash Screen

`build.spec` declares a PyInstaller `Splash` target using
`assets/images/splash.png`. This screen is drawn by the **bootloader** in
Tcl/Tk, not by Qt — which is the whole point: the build is onefile, so the
splash is already up while the archive unpacks, seconds before Python (let
alone Qt) exists.

During onefile extraction — the slowest part of startup, and the part that
happens before any Python runs — the *bootloader* writes the name of each file
it unpacks into the status line (`PyQt6/Qt6/lib/libQt6Core.so.6` and friends).
That is PyInstaller's own behaviour and there is no switch for it. The progress
bar stays at 0 throughout, because nothing can drive it until Python starts;
`main.py`'s milestones then take over both the line and the bar.

`core/splash.py` drives it from the application side and is a **silent no-op**
whenever there is no splash to talk to (a development run, or the macOS build),
so `main.py` calls it unconditionally. It reports five milestones — Qt,
configuration, show, interface, ready — and closes the splash immediately
before `main_window.show()`; the splash is topmost and undecorated, and would
otherwise cover the main window and the config-error/crash dialogs.

**Platform support:** Windows and Linux only. `Splash()` raises `SystemExit`
on macOS (the screen runs on a secondary thread, and Cocoa forbids UI work off
the main thread), so the spec skips it there and the mac build simply has none.
The `tkinter` entry in `excludes` is unrelated and harmless: `Splash` bundles
the Tcl/Tk libraries it needs by itself.

A splash is a nicety and an executable is not, so a `Splash` that cannot be
built is caught and warned about rather than taking the build down with it.

**Linux: `LD_LIBRARY_PATH` is required with a uv-managed Python.** Splash needs
the *paths* to the Tcl and Tk shared libraries, which PyInstaller resolves by
reading the dynamic-link entries of `_tkinter`. In a python-build-standalone
interpreter (what `uv python install` gives you) `_tkinter.so` names
`libtcl9.0.so`/`libtcl9tk9.0.so` with **no RPATH** — the interpreter finds them
through its own, PyInstaller cannot, and the build logs

```
ERROR: Could not determine the path to Tcl and/or Tk shared library ...
```

and produces an executable with no splash. `scripts/build.py` handles this for
you (`Builder._pyinstaller_env`), so `make build` and `make build-linux` need
nothing extra. Only a hand-rolled invocation has to set it:

```bash
LD_LIBRARY_PATH="$(uv run python -c 'import sys; print(sys.base_prefix)')/lib" \
  uv run pyinstaller --clean --noconfirm build.spec
```

Windows is unaffected (DLLs sit next to `_tkinter.pyd`), and macOS has no
splash to build.

**Previewing without a build.** A full onefile build is a miserable loop for
positioning text and a bar, so `scripts/preview_splash.py` generates the same
Tcl `build.spec` produces, runs it in a real Tk interpreter and rehearses
`main.py`'s five milestones over the same IPC protocol:

```bash
uv run --with pyinstaller python scripts/preview_splash.py
uv run --with pyinstaller python scripts/preview_splash.py --image new_art.png
uv run --with pyinstaller python scripts/preview_splash.py --step 3 --hold 10
```

`--step` is how long each milestone holds (default 0.9 s) and `--hold` how long
the finished splash stays up (default 2 s); the defaults run through in about
six seconds, which is too quick to judge a layout. It reads the constants out
of `build.spec`, so it cannot drift from the build, and it refuses to start
when they fall outside the artwork rather than showing you a splash with
nothing on it.

**The progress bar is an extension, not a PyInstaller feature.** Upstream
offers a status *line* only (`pyi_splash.update_text`). `_patch_splash_templates()`
in `build.spec` fills in the `# Implement other procedures here` marker that
PyInstaller's own IPC dispatcher carries, adding an `update_progress` command,
and appends two rectangles plus a `trace` to the canvas template.
`tests/test_splash.py` fails loudly if a PyInstaller upgrade moves either
foothold; a failed patch is not fatal at build time — the status line survives
and `core.splash.update(progress=...)` goes quiet.

**Changing the artwork.** The current splash is **640×300**. Everything
positional lives in one block at the top of the splash section in `build.spec`,
and every coordinate there is in **image pixels**:

| Constant | Current | Meaning |
| --- | --- | --- |
| `SPLASH_TEXT_POS` | `(161, 186)` | status line origin, its **lower-left** corner |
| `SPLASH_TEXT_SIZE` | `9` | **points**, not pixels — see below |
| `SPLASH_TEXT_COLOR` | `#9aa4b2` | status line colour |
| `SPLASH_BAR_RECT` | `(161, 196, 573, 199)` | `(x0, y0, x1, y1)` of the bar |
| `SPLASH_BAR_TRACK` / `SPLASH_BAR_FILL` | `#2a2e35` / `#6b8cff` | unfilled and filled colours |

**Resizing the image means rescaling these.** They are absolute pixel offsets,
not fractions, so halving the artwork puts the text and the bar off the bottom
of it — where they are drawn but invisible, which looks exactly like a broken
splash. `SPLASH_TEXT_SIZE` is the exception: it is in points, independent of the
image, so it does *not* scale with the artwork and has to be re-judged by eye.
Two things catch a mismatch: `scripts/preview_splash.py` refuses to run and
names the offending constant, and `tests/test_splash.py` fails with *"the
progress bar sits inside the artwork"*.

Constraints on the image itself:

- **PNG.** Any other format needs Pillow at build time, which is not a build
  dependency.
- **Leave the status text and bar bands empty.** They are drawn *over* the
  image, so any baked-in "loading" caption collides with the live status line.
- Any size works — `_png_size()` hands the artwork's own dimensions to
  `max_img_size`, so PyInstaller never tries to resize (which would need
  Pillow). Remember it is shown at 1:1, on a display as small as 1280×800.
- **Windows only:** magenta (`#ff00ff`) is the transparency key colour. Do not
  use it in the image, the text or the bar.

**Keep the status strings free of `…`.** Tk renders U+2026 raised in some of the
default fonts it may pick, so `Building the interface…` reads as
`Building the interface***`. `main.py`'s milestones and the spec's
`text_default` use three plain dots.

### Build Script Features
- **Cross-platform:** Works on Windows, macOS, and Linux
- **Icon Conversion:** Automatically creates platform-specific icons
- **Clean Builds:** Removes previous artifacts
- **uv Integration:** Uses uv for dependency management
- **Installer Creation:** Platform-specific installers when tools available

## Advanced Usage

### Manual Build Steps
```bash
# 1. Install build dependencies
uv sync --extra build

# 2. Clean previous builds (optional)
rm -rf build/ dist/

# 3. Create icons
python scripts/build.py --icons-only

# 4. Build executable
uv run pyinstaller --clean --noconfirm build.spec

# 5. Create installer (platform-specific)
makensis scripts/windows_installer.nsi  # Windows
hdiutil create -srcfolder dist/App.app dist/App.dmg  # macOS
```

### Custom Build Options

**Modify `pyproject.toml`:**
```toml
[tool.pyinstaller]
name = "YourAppName"
console = false  # Set to true for console app
icon = "path/to/icon.png"
```

**Environment Variables:**
- `UPX_DISABLE=1` - Disable UPX compression
- `PYINSTALLER_DEBUG=1` - Enable debug mode

## Distribution

### File Sizes (Approximate)
- **Windows:** 150-200 MB (executable), 80-120 MB (installer)
- **macOS:** 120-180 MB (app bundle), 60-100 MB (DMG)
- **Linux:** 120-180 MB (executable)

### Installer Features

**Windows (NSIS):**
- Start menu shortcuts
- Desktop shortcut
- Add/Remove Programs entry
- Automatic VC++ Redistributable
- Uninstaller

**macOS (DMG):**
- Drag-and-drop installation
- App bundle with metadata
- High DPI support
- Code signing ready

## CI/CD Integration

The project includes GitHub Actions workflow (`.github/workflows/build.yml`) that:
- Runs the test suite (`uv run pytest`, headless Qt) on Ubuntu; the build
  jobs depend on it, so a red test blocks the release
- Builds for all platforms simultaneously
- Creates installers automatically
- Uploads artifacts for download
- Creates GitHub releases for tags

### Triggering Builds
```bash
# Push to trigger CI builds
git tag v0.1.0
git push origin v0.1.0

# Manual workflow dispatch also available in GitHub UI
```

## Troubleshooting

### Common Issues

**Missing Dependencies:**
```bash
# Install missing audio libraries
uv add pillow soundfile  # For audio processing

# Install missing PyQt6 modules
uv add pyqt6-tools  # For additional Qt tools
```

**Linux: "cannot enable executable stack as shared object requires":**

```
[PYI-49616:ERROR] Failed to load Python shared library
  '/tmp/_MEIxG7ERt/libpython3.14.so.1.0': cannot enable executable stack
  as shared object requires: Invalid argument
```

The app dies at startup, before any of its own code runs. Nothing to do with
this project's code: python-build-standalone (what `uv python install`
provides) ships `libpython3.x.so` with `PT_GNU_STACK` marked **RWE**, asking
the loader for an executable stack, and Linux 6.x and newer refuse that for a
shared object. Distro builds are unaffected — Arch's own libpython is `RW`.

`build.spec` fixes this automatically (`_clear_execstack`, applied to
`a.binaries` right after `Analysis`): it copies any offending library into the
build directory, clears the `PF_X` bit of that one program-header field, and
repoints the TOC at the copy. The flag is spurious — CPython does not need an
executable stack, it survives from an assembly object with no
`.note.GNU-stack` section. A patched build logs:

```
note: cleared the executable-stack flag on libpython3.14.so.1.0
```

To confirm a library by hand:

```bash
readelf -lW /path/to/libpython3.14.so.1.0 | grep GNU_STACK   # want RW, not RWE
```

**Build Failures:**
```bash
# Clean everything and rebuild
make clean
make dev
make build
```

**Large File Sizes:**
- Review hidden imports in `build.spec`
- Add modules to `excludes` list
- Enable UPX compression (may cause issues)

**Icon Issues:**
- Ensure PNG icon exists at `assets/icons/TupperDMX-icon.png`
- Install Pillow for Windows ICO conversion: `uv add pillow`
- macOS requires `sips` command (built-in)

**GPU stage rendering (Settings ▸ Logging ▸ "Render the stage light on the
GPU"):**

Off by default. When on, the stage's light is accumulated in a floating-point
buffer and tonemapped with ACES, which needs a working desktop-OpenGL driver.
Everything degrades to the normal renderer on its own — a machine with no
usable driver logs one warning and looks exactly as it did before — so the
symptom to look for is "it never got faster/prettier", not a crash.

- The `PyQt6.QtOpenGLWidgets` hidden import in `build.spec` is required.
  `QOpenGLWidget` does **not** live in `QtOpenGL`, and a missing import here
  fails only in the frozen build, never in development.
- Remote sessions (RDP, VNC) and VMs frequently have no usable GL. Force
  software rendering with `QT_OPENGL=software`, or just leave the setting off —
  a lighting desk on venue hardware is exactly where the raster path earns its
  keep.
- On Linux the layer forces a *desktop* GL context. A Wayland/EGL session
  otherwise hands out OpenGL ES, where the shaders will not compile.
- Screenshots: `QWidget.grab()` cannot read back a GL viewport and returns a
  blank image for the whole stage view. Turn the setting off when capturing the
  stage programmatically (the `verify` skill included).

### Debug Mode
```bash
# Enable PyInstaller debug output
PYINSTALLER_DEBUG=1 uv run python scripts/build.py

# Test executable with console output
uv run pyinstaller --debug=all --console build.spec
```

## Dependencies

### Build Dependencies
- `pyinstaller>=6.0` - Cross-platform executable builder
- `pyinstaller-hooks-contrib` - Additional hooks for complex packages

### Runtime Dependencies
All runtime dependencies are automatically detected and bundled:
- PyQt6 (GUI framework, and — via QtMultimedia/ffmpeg — the only audio decoder)
- numpy (PCM, the stage engine, waveform binning)
- mido/python-rtmidi (MIDI)
- pyartnet (DMX Art-Net)
- pyserial (USB DMX)

### System Requirements

**Windows:**
- Windows 10/11 (64-bit)
- Visual C++ Redistributable (included in installer)

**macOS:**
- macOS 10.15+ (Catalina)
- Intel or Apple Silicon

**Linux:**
- glibc 2.28+ (Ubuntu 18.04+)
- X11 or Wayland display server
- ALSA/PulseAudio for audio