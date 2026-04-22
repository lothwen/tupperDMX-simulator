# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TupperDMX Simulator is a PyQt6-based lighting control application that synchronizes MP3 audio playback with MIDI-triggered DMX lighting sequences and provides an integrated metronome. The application visualizes DMX-controlled LED fixtures and blinders in real-time on a virtual stage.

## Development Commands

### Setup and Running (uv - Recommended)
```bash
# Install dependencies and create virtual environment
uv sync

# Run the application
uv run python main.py

# Activate managed virtual environment
uv shell

# Add new dependencies
uv add package-name

# Update dependencies
uv lock --upgrade
```

### Legacy Setup (pip)
```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python main.py
```

### Key Dependencies
- PyQt6: GUI framework and multimedia playback
- mido/python-rtmidi: MIDI handling
- Custom async Art-Net UDP receiver (replaces pyartnet)
- librosa: Audio analysis for BPM detection
- pylibftdi/pyserial: USB DMX interface support

## Architecture Overview

### Core Components

**Main Application (`main.py`)**
- `MainWindow`: Central orchestrator that coordinates all UI components and managers
- Handles playback state synchronization between media, metronome, and timeline
- Manages file loading, seeking, and configuration persistence

**Media Playback (`ui/media_controller.py`)**
- `MediaController`: Controls MP3 playback and MIDI synchronization
- Maintains timeline position and handles play/pause/stop operations
- Synchronizes audio playback with MIDI output timing
- Key methods: `start_playback()`, `play_pause_playback()`, `stop_playback()`, `seek_to_position()`

**Audio & Metronome (`ui/audio_controller.py`)**
- `AudioController`: Generates synchronized metronome beats using QAudioSink
- Critical feature: `start_metronome(timeline_position_sec)` synchronizes beats to timeline position
- Uses calculated timing delays to maintain beat accuracy when resuming or seeking
- Generates high/low pitch beeps for downbeat/regular beat distinction

**DMX Management (`dmx/`)**
- `DMXManager`: Factory pattern for Art-Net and USB DMX output
- `ArtnetWorker`: Async UDP listener using asyncio; automatically starts `DMXController` when Art-Net IP is localhost
- `USBWorker`: Serial communication for FTDI/direct USB DMX interfaces
- Emits `software_controller_changed` signal when controller lifecycle events occur
- Supports multiple DMX sources with hot-swapping capabilities

**MIDI Management (`midi/`)**
- `MidiManager`: Handles MIDI file parsing and real-time playback
- `MidiWorker`: Threaded MIDI output synchronized to timeline
- Supports seeking and port switching during playback
- "Software emulated" port option routes MIDI note_on messages directly to `DMXController.handle_note_on()`

**Stage Visualization (`ui/stage_controller.py`)**
- `StageController`: Renders virtual stage with DMX-controlled fixtures
- Updates fixture colors/intensity in real-time based on DMX data
- Supports LED bars and blinder fixtures with configurable positions

**Software Controller (`software_controller/`)**
- `DMXController`: Pure-Python DMX lighting controller that simulates hardware controllers via MIDI messages
- Enables full testing and live lighting design without physical hardware
- Manages a 96-channel DMX universe: LED bar (7 channels), Blinder1/2 (8 channels each)
- Responds to 53 MIDI note pitches mapped to scene selection, color control, song parts, and blinder control
- `ArtNetSender`: Transmits properly formatted Art-Net UDP packets to DMX receivers
- `LedScene`/`BlinderScene` dataclasses load from `software_controller/dmx_config.json`
- `dmx_config.json`: Defines scene banks (ANIMATIONS, FILLS, FILLS2, MULTICOLOR, STROBES, TUBES, RADARS, BLINDER_SCENES) and 15+ chase sequences

**Configuration (`config.py`)**
- Comprehensive dataclass-based configuration with validation
- Handles fixture definitions, MIDI note mapping, and user preferences
- Automatic config file generation and migration
- Includes playlist management (song list with current index), audio offset, and window geometry

### UI Architecture

The application uses a modular UI design:
- `ToolbarManager` (`ui/toolbar_manager.py`): Creates media controls, BPM spinner, source selectors, and playlist navigation (prev/next/remove song with counter)
- `Timeline` (`timeline/manager.py`): Waveform visualization with beat detection, seeking, and MIDI event editing
- `DMXDisplayWidget` (`ui/dmx_display.py`): Real-time DMX channel monitor
- `AudioController` (`ui/audio_controller.py`): Drift-free metronome generation and audio processing
- `ErrorDialogManager` (`ui/error_dialogs.py`): Centralized error handling with user-friendly dialogs and retry functionality

### Key Synchronization Logic

**Metronome Beat Synchronization**
When starting/resuming metronome:
1. Calculate current beat position from timeline: `total_beats_elapsed = (timeline_position_sec / beat_duration_sec) + 1`
2. Determine time since last beat: `time_since_last_beat = (total_beats_elapsed % 1) * beat_duration_sec`
3. If close to beat boundary (`< 0.001s`), play immediately; otherwise use single-shot timer for next beat
4. After first synchronized beat, switch to regular intervals

**Media Playback Timeline**
- Uses `time.time()` for precise timeline tracking during playback
- Maintains separate `current_timeline_time` and `playback_start_time` for pause/resume
- Synchronizes MP3 position with timeline using `QMediaPlayer.setPosition()`

## Development Notes

### Critical Behavior Patterns

**Metronome Synchronization**
- Always restart metronome when seeking during playback to maintain beat sync
- BPM changes require metronome restart with current timeline position
- Beat counting uses 1-based indexing (1,2,3,4) with high pitch on beat 1

**Media Controller State Management**
- Preserves MIDI synchronization structure while supporting audio-only playback
- Uses timeline-based positioning rather than media player position for accuracy
- Handles pause/resume with precise timing calculations

**Configuration System**
- All settings auto-persist to `config.json` with validation and defaults
- File paths validated for existence before saving
- DMX channel mapping calculated dynamically from fixture definitions
- Comprehensive dataclass-based structure with automatic migration and error recovery

**MIDI Event Management**
- Interactive MIDI event editing via timeline right-click context menus
- Drag-and-drop event timing adjustment with visual feedback
- Real-time synchronization between timeline display and MidiManager data
- Support for adding, editing, and deleting events with proper validation

**Error Handling & Recovery**
- Centralized error dialog system with user-friendly messages
- Automatic retry mechanisms for recoverable connection errors
- Graceful degradation when hardware components are unavailable
- Configuration reset options for corrupted settings

**Playlist Management**
- Song list with current index persisted in configuration
- Toolbar navigation buttons (prev/next/remove) with song counter
- Signals: `prev_song_requested`, `next_song_requested`, `remove_song_requested`

**Audio Offset**
- Configurable audio offset stored in config (`audio.offset`)
- Allows fine-tuning sync between audio playback and MIDI/DMX

### File Organization
- `ui/`: All UI components and controllers
  - `media_controller.py`: Audio playback and timeline synchronization
  - `audio_controller.py`: Drift-free metronome generation and audio processing
  - `stage_controller.py`: Virtual stage visualization with fixture rendering
  - `dmx_display.py`: Real-time DMX channel monitoring widget
  - `toolbar_manager.py`: Application toolbar with controls, playlist navigation, and status displays
  - `error_dialogs.py`: User-friendly error handling and recovery dialogs
- `dmx/`: DMX protocol implementations and workers
  - `manager.py`: Factory pattern DMX source management with hot-swapping; propagates `software_controller_changed` signal
  - `artnet_worker.py`: Async Art-Net UDP listener (asyncio); auto-starts software controller when target is localhost
  - `usb_worker.py`: USB DMX interface support (FTDI/serial)
  - `base.py`: Common DMX worker interface and utilities
  - `factory.py`: Worker factory pattern
- `midi/`: MIDI file parsing and playback workers
  - `manager.py`: MIDI data management, file I/O, and real-time editing
  - `worker.py`: Threaded MIDI output synchronized to timeline; supports "Software emulated" port
- `software_controller/`: Pure-Python software-emulated DMX controller
  - `main.py`: `DMXController` — manages 96-channel DMX universe with 53 MIDI note mappings, scene banks, chase sequences, color palette generation, and HSL blending
  - `artnet_sender.py`: `ArtNetSender` — sends Art-Net UDP packets to DMX receivers
  - `dmx.py`: `LedScene`/`BlinderScene` dataclasses for fixture configuration
  - `dmx_config.json`: Scene banks (FILLS, TUBES, ANIMATIONS, etc.) and chase definitions
- `tupperlogger/`: Custom logging system with file rotation
- `timeline/`: Modular timeline package with track-based architecture
  - `manager.py`: Main Timeline coordinator and public interface
  - `tracks/base.py`: BaseTrack interface for all timeline tracks
  - `tracks/ruler.py`: Time ruler with persistent metronome controls
  - `tracks/waveform.py`: Audio waveform visualization with DraggableWaveform
  - `tracks/midi_events.py`: Interactive MIDI event boxes with drag-and-drop editing
  - `tracks/piano_roll.py`: Piano keys and note visualization
  - `widgets/tooltip.py`: CustomTooltip for detailed event information
  - `widgets/content_view.py`: ContentView with zoom/pan functionality
  - `widgets/midi_event_edit.py`: MidiEventEditDialog for event editing
- `config.py`: Comprehensive configuration management with dataclass validation
- `fixtures.py`: DMX fixture definitions and channel mapping utilities

## Important Development Patterns

### Timeline Architecture (Modular Design)
- **Track-Based System**: Timeline uses individual track objects for different content types
- **BaseTrack Interface**: All tracks inherit from BaseTrack providing common functionality
- **Separation of Concerns**: Each track handles its own rendering and interaction logic
- **Persistent Controls**: Metronome and audio controls persist across BPM changes and redraws
- **Content vs Header Separation**: Tracks maintain separate item lists for content and headers
- **Coordinate Management**: Timeline manager provides PIXELS_PER_SECOND conversion utilities

### Timeline and MIDI Integration
- Timeline manager coordinates all tracks via `refresh_from_manager()` calls
- MIDI events cached in `_cached_events` and `_cached_notes` for UI performance
- Event editing emits `data_changed` signal for cross-component synchronization
- Track coordinate conversion: `time_seconds = x_pos / timeline.PIXELS_PER_SECOND`
- Interactive editing: DraggableMidiEvent handles mouse events and emits timing changes

### Error Handling Standards
- All file operations wrapped with try/catch and user-friendly error dialogs
- Hardware connections use retry dialogs for recoverable errors
- Configuration errors offer reset options to prevent application crashes
- Logging integrated throughout with structured error messages

### UI State Management
- Toolbar state synchronized with playback, BPM, and connection status
- Volume and mute controls mirrored between timeline and toolbar
- Configuration persistence handled automatically on state changes
- Window geometry and last-used settings preserved between sessions

### Threading and Performance
- DMX output runs in dedicated worker threads with QThread
- MIDI playback synchronized via worker threads with timeline position
- Audio processing uses QAudioSink for low-latency metronome generation
- UI updates batched to avoid blocking main thread during intensive operations
- Timeline tracks render independently for better performance isolation

### Software Controller Architecture

The `software_controller/` module provides hardware-free DMX control via MIDI messages:

**Activation Flow:**
1. Set Art-Net IP to `127.0.0.1` (localhost) in the DMX source selector
2. `ArtnetWorker` detects localhost target and auto-instantiates `DMXController`
3. `DMXController` starts a background loop thread sending DMX data to `ArtNetSender`
4. `DMXManager` emits `software_controller_changed(controller)` to `MainWindow`
5. `MainWindow.on_software_controller_changed()` passes the controller to `MidiManager`
6. Select "Software emulated" in the MIDI port dropdown
7. `MidiWorker` now routes all `note_on` messages to `DMXController.handle_note_on()`

**MIDI Note Mapping (pitch 0–52):**
- **1–11**: Channel control — brightness, RGB color channels, animation, speed, direction
- **12–19**: Scene selection (8 scenes per active bank)
- **20–23**: Predefined colors (white, orange, cyan, pink)
- **24–25**: Bank selection (FILLS, TUBES)
- **26–32**: Blinder controls
- **38–52**: Song part markers (START, INTRO, VERSE, CHORUS, BREAK, BRIDGE, INST, SOLO, OUTRO, END) and master controls

**DMX Universe Layout:**
- LED bar: channels 1–7 (brightness, R, G, B, animation, speed, direction)
- Blinder 1: channels 8–15
- Blinder 2: channels 16–23

**Color Palette Generation:**
- `generate_song_colors()` produces palettes using ANALOGOUS, COMPLEMENTARY, or TRIADIC modes
- HSL-to-RGB conversion with hue rotation for smooth color transitions
- Thread-safe palette state with mutex locking

**Scene Blending:**
- `_apply_blend()` interpolates between present and future DMX states
- Configurable transition duration; immediate apply for non-blended channels
- Prevents blackout artifacts during scene changes

### Timeline Development Guidelines
- **Adding New Tracks**: Inherit from BaseTrack and implement required methods
- **Persistent UI Elements**: Use separate item lists for content vs permanent header items
- **Track Communication**: Use timeline manager as coordinator, avoid direct track-to-track calls
- **Redrawing Logic**: Only clear content items during redraws, preserve header items
- **Widget Reuse**: Timeline widgets can be imported and used in other UI components
- **Testing Tracks**: Individual tracks can be unit tested independently of full timeline

## Build System and Distribution

The project includes a comprehensive cross-platform build system integrated with uv for creating distributable executables and installers.

### Build Commands

**Development Setup:**
```bash
# Install build dependencies
make dev
# or: uv sync --extra build

# Run application in development
make run
# or: uv run python main.py
```

**Building Executables:**
```bash
# Build for current platform
make build

# Clean previous builds
make clean

# Platform-specific builds (run on target platform)
make build-windows    # Windows executable + NSIS installer
make build-macos      # macOS app bundle + DMG
make build-linux      # Linux executable
```

**Direct Build Script:**
```bash
# Cross-platform build script
uv run python scripts/build.py

# With options
uv run python scripts/build.py --no-clean
```

### Build System Architecture

**Core Components:**
- `build.spec`: PyInstaller configuration with platform-specific settings
- `scripts/build.py`: Integrated build script with uv dependency management
- `scripts/windows_installer.nsi`: NSIS installer script for Windows
- `Makefile`: Convenient build commands for all platforms
- `.github/workflows/build.yml`: CI/CD automation for cross-platform builds

**PyInstaller Configuration:**
- **Hidden Imports**: Includes all PyQt6, audio, MIDI, and DMX modules
- **Data Files**: Bundles assets, icons, and configuration files  
- **Module Exclusions**: Optimized to reduce executable size
- **Platform Icons**: Automatic PNG/ICO/ICNS icon conversion

**uv Integration:**
- Build dependencies managed via `[project.optional-dependencies]`
- Seamless virtual environment handling
- Consistent dependency resolution across platforms

### CI/CD and Automated Builds

**GitHub Actions Workflow:**
- Builds for Windows, macOS, and Linux simultaneously
- Creates platform-specific installers automatically
- Uploads build artifacts for download
- Creates GitHub releases for tagged versions

**Triggering Builds:**
```bash
# Local development build
make build

# Trigger CI builds for all platforms
git tag v1.0.0
git push origin v1.0.0
```

### Build Artifacts

**File Sizes (Approximate):**
- Linux executable: ~230 MB (includes all dependencies)
- Windows executable: ~200 MB + installer (~100 MB)
- macOS app bundle: ~180 MB + DMG (~120 MB)

**Distribution Files:**
- **Linux**: `TupperDMX-Simulator` (executable)
- **Windows**: `TupperDMX-Simulator.exe` + `TupperDMX-Simulator-Setup.exe`
- **macOS**: `TupperDMX-Simulator.app` + `TupperDMX-Simulator.dmg`

### Build Dependencies

**Runtime Dependencies (Auto-bundled):**
- PyQt6 (GUI framework and multimedia)
- librosa (audio processing and BPM detection)
- mido/python-rtmidi (MIDI handling)
- Custom asyncio Art-Net UDP receiver (no longer uses pyartnet)
- pylibftdi/pyserial (USB DMX interfaces)
- numpy/scipy (scientific computing)

**Build-Only Dependencies:**
- PyInstaller 6.0+ (executable creation)
- pyinstaller-hooks-contrib (additional package hooks)

### Build Troubleshooting

**Common Issues:**
- Missing modules: Add to `hidden_imports` in `build.spec`
- Large file sizes: Review `excludes` list and data files
- Icon problems: Ensure PNG icon exists and install platform converters

**Debug Build:**
```bash
# Enable PyInstaller debug output
PYINSTALLER_DEBUG=1 uv run python scripts/build.py

# Test with console output
uv run pyinstaller --debug=all --console build.spec
```

**Platform Requirements:**
- **Windows**: NSIS 3.0+ (for installer), Visual C++ Redistributable
- **macOS**: Xcode Command Line Tools, macOS 10.15+
- **Linux**: Standard development tools, audio/USB system libraries

For detailed build instructions and troubleshooting, see `BUILD.md`.

## Common Debugging Commands
```bash
# Check log files
tail -f logs/tupperDMX_$(date +%Y%m%d).log

# Test DMX connectivity (from config)
# Art-Net: Usually 127.0.0.1:6454
# USB: Check device enumeration in /dev/

# Reset configuration if corrupted
rm config.json && python main.py

# Debug MIDI devices
python -c "import mido; print(mido.get_output_names())"

# Check audio devices
python -c "from PyQt6.QtMultimedia import QMediaDevices; print([d.description() for d in QMediaDevices.audioOutputs()])"

# Test software controller standalone (sends Art-Net UDP to localhost:6454)
uv run python -c "from software_controller.main import DMXController; c = DMXController(); c.start(); import time; time.sleep(2); c.stop()"

# Build system debugging
uv run python scripts/build.py --no-clean
ls -la dist/
./dist/TupperDMX-Simulator --version 2>/dev/null || echo "Test basic execution"
```

## Key Import Fixes
- Always include `QMessageBox` in PyQt6 imports when using dialog boxes
- Error dialogs require proper parent widget reference for styling consistency
- Timeline editing requires both `QDialog` and `QMessageBox` for event management