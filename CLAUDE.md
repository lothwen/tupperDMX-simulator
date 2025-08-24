# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TupperDMX Simulator is a PyQt6-based lighting control application that synchronizes MP3 audio playback with MIDI-triggered DMX lighting sequences and provides an integrated metronome. The application visualizes DMX-controlled LED fixtures and blinders in real-time on a virtual stage.

## Development Commands

### Setup and Running
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
- `ToolbarManager`: Creates media controls, BPM spinner, and source selectors
- `Timeline`: Waveform visualization with beat detection and seeking
- `DMXDisplayWidget`: Real-time DMX channel monitor
- `AudioController`: Metronome generation and audio processing

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

### File Organization
- `ui/`: All UI components and controllers  
- `dmx/`: DMX protocol implementations and workers
- `midi/`: MIDI file parsing and playback workers
- `tupperlogger/`: Custom logging system with file rotation

### Common Debugging Commands
```bash
# Check log files
tail -f logs/tupperDMX_$(date +%Y%m%d).log

# Test DMX connectivity (from config)
# Art-Net: Usually 127.0.0.1:6454
# USB: Check device enumeration in /dev/
```