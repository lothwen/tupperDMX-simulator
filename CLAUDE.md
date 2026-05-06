# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Project Overview

TupperDMX Simulator is a PyQt6 lighting-control app that synchronizes MP3 audio
playback with MIDI-triggered DMX sequences and an integrated metronome. It
visualizes DMX-controlled LED bars and blinders in real time on a virtual
stage, and ships a pure-Python "software controller" that emulates a hardware
DMX controller via MIDI when targeting localhost Art-Net.

## Development Commands

```bash
uv sync                  # install deps + create .venv
uv run python main.py    # run the app
uv add <package>         # add a dep
uv lock --upgrade        # refresh lockfile
```

Build/distribution commands and CI details live in `BUILD.md`.

### Key Dependencies
PyQt6 (GUI + multimedia), mido/python-rtmidi (MIDI), custom asyncio Art-Net UDP
receiver, librosa (BPM detection), pylibftdi/pyserial (USB DMX),
platformdirs (user-config paths).

## Architecture Overview

### Core Components

- **`main.py` — `MainWindow`**: Central orchestrator. Coordinates UI/managers,
  syncs playback state across media/metronome/timeline, persists config.
- **`ui/media_controller.py` — `MediaController`**: MP3 playback + MIDI sync.
  Owns timeline position; key methods `start_playback`, `play_pause_playback`,
  `stop_playback`, `seek_to_position`.
- **`ui/audio_controller.py` — `AudioController`**: Drift-free metronome via
  QAudioSink. `start_metronome(timeline_position_sec)` aligns beats to
  timeline; high pitch on downbeat (1), low on 2/3/4.
- **`dmx/` — `DMXManager`**: Factory for Art-Net (`ArtnetWorker`, asyncio UDP)
  and USB (`USBWorker`, FTDI/serial). When Art-Net target is `127.0.0.1`,
  `ArtnetWorker` auto-spawns the software `DMXController` and emits
  `software_controller_changed`. Hot-swap supported.
- **`midi/` — `MidiManager` + `MidiWorker`**: File parsing, real-time editing,
  threaded output synced to timeline. The `"Software emulated"` port routes
  `note_on` directly to `DMXController.handle_note_on()`.
- **`ui/stage_controller.py` — `StageController`**: Virtual stage rendering.
  Updates fixture color/intensity from live DMX data.
- **`software_controller/`**: Pure-Python DMX controller (no hardware needed).
  `DMXController` manages a 96-channel universe (LED bar 1–7, Blinder1 8–15,
  Blinder2 16–23) and reacts to 53 MIDI pitches (channel control 1–11, scene
  select 12–19, colors 20–23, bank select 24–25, blinders 26–32, song-part
  markers 38–52). `ArtNetSender` emits Art-Net UDP. Scenes/banks/chases are
  loaded from `software_controller/dmx_config.json`.

### Configuration (`config.py`)

Storage is split across two files:
- **`config.json`** (shipped, read-only): `fixtures` and `midi_note_map`.
  Bundled via PyInstaller (`build.spec` `datas`), resolved at runtime by
  `shipped_config_path()` (handles `sys._MEIPASS` when frozen).
- **`userprefs.json`** (user-writable): everything else (`window`, `artnet`,
  `playlist`, `metronome`, `volume`, `logging`, `last_dmx_source`,
  `last_midi_port`). Resolved by `userprefs_path()` via `platformdirs`:
  - Linux: `~/.config/tupperDMX/`
  - macOS: `~/Library/Application Support/tupperDMX/`
  - Windows: `%APPDATA%\tupperDMX\`

`Config.load()` reads both; `Config.save()` writes only `userprefs.json`.
First launch creates `userprefs.json` eagerly with defaults.

### UI Architecture

- `ToolbarManager` (`ui/toolbar_manager.py`): media controls, BPM, source
  selectors, playlist nav (prev/next/remove + counter).
- `Timeline` (`timeline/manager.py`): waveform, beat detection, seeking, and
  clip-style MIDI editing — one `MidiEventsTrack` per fixture filter
  (`ledbar`, `blinder`).
- `ClipLibraryWidget` (`ui/clip_library.py`): right sidebar tabs ("Library",
  "DMX Channels"). `QToolBox` with one section per fixture (from
  `config.midi_note_map`) plus one per LED scene bank (from
  `software_controller/dmx_config.json`). Click previews; drag drops onto
  the timeline. Per-fixture pages hide notes 1–7 (covered by scene pages).
- `DMXDisplayWidget` (`ui/dmx_display.py`): live channel monitor.
- `ErrorDialogManager` (`ui/error_dialogs.py`): centralized retry/reset dialogs.

## Critical Behavior Patterns

### Metronome Sync
On start/resume:
1. `total_beats_elapsed = (timeline_position_sec / beat_duration_sec) + 1`
2. `time_since_last_beat = (total_beats_elapsed % 1) * beat_duration_sec`
3. If within `0.001s` of a boundary, play immediately; otherwise schedule a
   single-shot timer to the next beat, then switch to regular intervals.
- Always restart metronome on seek-during-playback or BPM change.

### Media Playback Timeline
- `time.time()`-based tracking; separate `current_timeline_time` /
  `playback_start_time` for pause/resume. MP3 position synced via
  `QMediaPlayer.setPosition()`. `audio.offset` fine-tunes audio↔MIDI/DMX sync.

### MIDI Event Management
- Clip-style: each event is a movable, resizable rectangle on its fixture lane.
  Three drag modes (MOVE, RESIZE_LEFT, RESIZE_RIGHT) with neighbor clamping
  and edge-snap. Persistence happens **only** on `mouseRelease` via
  `*_committed` signals; mid-drag is purely visual.
- **BLACKOUTs are gap markers**, scoped per fixture (`BLACKOUT` for ledbar,
  `BLACKOUT_BLINDERS` for blinders), identified by part-name PREFIX
  (case-insensitive), not by note number. `MidiManager.reconcile_blackouts()`
  enforces: no zero-duration BLACKOUTs, no adjacent BLACKOUTs, BLACKOUTs
  reinserted to maintain intended clip extents.
- Deleting a regular clip converts it to a fixture-matching BLACKOUT —
  preserves the gap-marker invariant instead of leaving a hole.
- Scene clips: 7 simultaneous channel-control `note_on` events sharing a
  `group_id`, rendered as one grouped clip; drags route to scene API
  (`add_scene`, group move/resize).

### Clip Library Drag-and-Drop
- MIME types: `application/x-tupperdmx-midi-clip` (single note + velocity),
  `application/x-tupperdmx-led-scene` (full 7-channel scene payload).
- `Timeline.can_accept_clip_drop()` validates fixture-under-cursor matches
  the clip's fixture; scene drops restricted to ledbar.
- `Timeline.handle_scene_drop()` calls `MidiManager.add_scene()` to expand
  scene payload into 7 grouped events.
- Click-to-preview: `preview_requested` → `MainWindow._preview_clip()` routes
  to active MIDI output (or directly to software controller) without
  modifying the timeline.

### Software Controller Activation Flow
1. Set Art-Net IP to `127.0.0.1` in DMX source selector.
2. `ArtnetWorker` detects localhost, auto-instantiates `DMXController`, which
   starts a background loop sending DMX to `ArtNetSender`.
3. `DMXManager` emits `software_controller_changed(controller)` → `MainWindow`
   passes it to `MidiManager`.
4. Select `"Software emulated"` in MIDI port dropdown — `MidiWorker` now
   routes `note_on` to `DMXController.handle_note_on()`.

Color palettes via `generate_song_colors()` (ANALOGOUS / COMPLEMENTARY /
TRIADIC, HSL→RGB, mutex-protected). `_apply_blend()` interpolates between
present and future DMX states to avoid blackout artifacts.

## File Organization

- `ui/` — `media_controller.py`, `audio_controller.py`, `stage_controller.py`,
  `dmx_display.py`, `clip_library.py`, `toolbar_manager.py`, `error_dialogs.py`.
- `dmx/` — `manager.py` (factory + hot-swap, propagates
  `software_controller_changed`), `artnet_worker.py` (asyncio UDP, auto-starts
  software controller on localhost), `usb_worker.py` (FTDI/serial), `base.py`,
  `factory.py`.
- `midi/` — `manager.py` (data + file I/O + real-time editing), `worker.py`
  (threaded output, supports `"Software emulated"` port).
- `software_controller/` — `main.py` (`DMXController`), `artnet_sender.py`,
  `dmx.py` (`LedScene`/`BlinderScene`), `scene_loader.py` (`load_led_scenes()`
  → immutable `LedScene` records), `dmx_config.json` (scene banks + chases).
- `timeline/` — `manager.py` (coordinator, one `MidiEventsTrack` per fixture
  filter), `tracks/` (`base.py`, `ruler.py`, `waveform.py`, `midi_events.py`),
  `widgets/` (`tooltip.py`, `content_view.py`, `midi_event_edit.py`,
  `scene_edit.py`, `resize_handle.py`).
- `tupperlogger/` — custom logger with file rotation.
- `config.py` — dataclass config, two-file split (see Configuration).
- `fixtures.py` — DMX fixture defs + channel mapping.

## Timeline Architecture Notes

- Tracks inherit from `BaseTrack`: `RulerTrack`, `WaveformTrack`, one
  `MidiEventsTrack` per `fixture_filter` (`"ledbar"` / `"blinder"`).
- Timeline exposes `PIXELS_PER_SECOND` and `fixture_at_y()` for drop routing.
- Tracks separate **content items** (cleared on redraw) from **header items**
  (persistent — e.g. metronome controls). Waveform/MIDI track heights are
  resizable via `TrackResizeHandle`, persisted as
  `window.waveform_track_height` / `window.midi_events_track_height`.
- Cross-component sync via `MidiManager.data_changed`; events cached in
  `_cached_events`. `DraggableMidiEvent` emits `move_committed` /
  `left_edge_committed` / `right_edge_committed` on release. Scene groups
  collapse to one `DraggableMidiEvent` via `build_render_units()`.

## Common Debugging

```bash
# Tail today's log
tail -f logs/tupperDMX_$(date +%Y%m%d).log

# Reset user prefs (shipped config.json is read-only and stays put)
# Linux:   rm ~/.config/tupperDMX/userprefs.json
# macOS:   rm ~/Library/Application\ Support/tupperDMX/userprefs.json
# Windows: del %APPDATA%\tupperDMX\userprefs.json

# Enumerate MIDI outputs
python -c "import mido; print(mido.get_output_names())"

# Enumerate audio outputs
python -c "from PyQt6.QtMultimedia import QMediaDevices; print([d.description() for d in QMediaDevices.audioOutputs()])"

# Smoke-test software controller (Art-Net UDP → 127.0.0.1:6454)
uv run python -c "from software_controller.main import DMXController; c = DMXController(); c.start(); import time; time.sleep(2); c.stop()"
```

## Key Import Reminders

- Include `QMessageBox` in PyQt6 imports for dialog boxes; timeline editing
  needs both `QDialog` and `QMessageBox`. Error dialogs require a parent
  widget reference for consistent styling.
