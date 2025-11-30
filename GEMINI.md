# GEMINI.md

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
- pyartnet: Art-Net DMX protocol
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
- `ArtnetWorker`/`USBWorker`: Threaded DMX data transmission
- Supports multiple DMX sources with hot-swapping capabilities

**MIDI Management (`midi/`)**
- `MidiManager`: Handles MIDI file parsing and real-time playback
- `MidiWorker`: Threaded MIDI output synchronized to timeline
- Supports seeking and port switching during playback

**Stage Visualization (`ui/stage_controller.py`)**
- `StageController`: Renders virtual stage with DMX-controlled fixtures
- Updates fixture colors/intensity in real-time based on DMX data
- Supports LED bars and blinder fixtures with configurable positions

**Configuration (`config.py`)**
- Comprehensive dataclass-based configuration with validation
- Handles fixture definitions, MIDI note mapping, and user preferences
- Automatic config file generation and migration

### UI Architecture

The application uses a modular UI design:
- `ToolbarManager` (`ui/toolbar_manager.py`): Creates media controls, BPM spinner, and source selectors
- `Timeline` (`timeline.py`): Waveform visualization with beat detection, seeking, and MIDI event editing
- `DMXDisplayWidget` (`ui/dmx_display.py`): Real-time DMX channel monitor
- `AudioController` (`ui/audio_controller.py`): Metronome generation and audio processing
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

### File Organization
- `ui/`: All UI components and controllers
  - `media_controller.py`: Audio playback and timeline synchronization
  - `audio_controller.py`: Metronome generation and audio processing
  - `stage_controller.py`: Virtual stage visualization with fixture rendering
  - `dmx_display.py`: Real-time DMX channel monitoring widget
  - `toolbar_manager.py`: Application toolbar with controls and status displays
  - `error_dialogs.py`: User-friendly error handling and recovery dialogs
- `dmx/`: DMX protocol implementations and workers
  - `manager.py`: Factory pattern DMX source management with hot-swapping
  - `artnet_worker.py`: Art-Net UDP protocol implementation
  - `usb_worker.py`: USB DMX interface support (FTDI/serial)
  - `base.py`: Common DMX worker interface and utilities
- `midi/`: MIDI file parsing and playback workers
  - `manager.py`: MIDI data management, file I/O, and real-time editing
  - `worker.py`: Threaded MIDI output synchronized to timeline
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
- pyartnet (Art-Net DMX protocol)
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

# Build system debugging
uv run python scripts/build.py --no-clean
ls -la dist/
./dist/TupperDMX-Simulator --version 2>/dev/null || echo "Test basic execution"
```

## Key Import Fixes
- Always include `QMessageBox` in PyQt6 imports when using dialog boxes
- Error dialogs require proper parent widget reference for styling consistency
- Timeline editing requires both `QDialog` and `QMessageBox` for event management