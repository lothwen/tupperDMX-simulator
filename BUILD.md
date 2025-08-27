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
sudo apt-get install libasound2-dev portaudio19-dev libusb-1.0-0-dev libftdi1-dev

# Fedora/CentOS
sudo dnf install alsa-lib-devel portaudio-devel libusb1-devel libftdi-devel

# Arch Linux
sudo pacman -S alsa-lib portaudio libusb libftdi
```

## Build Configuration

### PyInstaller Spec File
The build uses `build.spec` for PyInstaller configuration:
- **Hidden Imports:** Includes all necessary PyQt6, audio, and MIDI modules
- **Data Files:** Bundles assets, icons, and configuration
- **Platform Icons:** Automatically uses appropriate icon format
- **Size Optimization:** Excludes unnecessary modules

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
- Ensure PNG icon exists at `assets/icons/metronome_icon.png`
- Install Pillow for Windows ICO conversion: `uv add pillow`
- macOS requires `sips` command (built-in)

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
- PyQt6 (GUI framework)
- librosa (audio processing)
- mido/python-rtmidi (MIDI)
- pyartnet (DMX Art-Net)
- pylibftdi/pyserial (USB DMX)

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