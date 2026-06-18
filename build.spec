# -*- mode: python ; coding: utf-8 -*-
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Build configuration
app_name = "TupperDMX-Simulator"
main_script = "main.py"

# Capture git metadata at build time and freeze it into _buildinfo.py.
# The runtime accessor in buildinfo.py imports this module when the app is
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
    build_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    output = project_root / "_buildinfo.py"
    output.write_text(
        "# ABOUTME: Auto-generated at PyInstaller build time. Do not edit by hand.\n"
        "# ABOUTME: Captures git metadata so the frozen app can show its provenance.\n"
        f"BUILD_DATE = {build_date!r}\n"
        f"GIT_COMMIT = {commit!r}\n"
        f"GIT_TAG = {tag!r}\n",
        encoding="utf-8",
    )

_generate_buildinfo()

# Data files and assets.
# config.json holds shipped, read-only data (fixtures + midi_note_map) and is
# resolved at runtime via sys._MEIPASS. User-writable preferences live in
# userprefs.json under the platform's user-config dir (see config.userprefs_path)
# and are intentionally NOT bundled here.
data_files = [
    ("assets", "assets"),
    ("config.json", "."),
    ("fixtures", "fixtures"),
    ("software_controller/dmx_config.json", "software_controller"),
]

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
    
    # Audio processing
    "librosa",
    "librosa.core",
    "librosa.feature",
    "librosa.util",
    "soundfile",
    "audioread",
    
    # MIDI
    "mido",
    "mido.backends",
    "mido.backends.rtmidi",
    "rtmidi",
    "python_rtmidi",
    
    # DMX
    "pyartnet",
    "pylibftdi",
    "serial",
    
    # Other
    "mutagen",
    "matplotlib",
    "numpy",
    "scipy",

    # Build metadata
    "buildinfo",
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

# Platform-specific configurations
if sys.platform == "win32":
    icon_file = "assets/icons/metronome_icon.ico"
    console = False
    windowed = True
elif sys.platform == "darwin":
    icon_file = "assets/icons/metronome_icon.icns" 
    console = False
    windowed = True
else:  # Linux
    icon_file = "assets/icons/metronome_icon.png"
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

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
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
        version="1.0.0",
        info_plist={
            "CFBundleName": app_name,
            "CFBundleDisplayName": app_name,
            "CFBundleVersion": "1.0.0",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.15.0",
        },
    )