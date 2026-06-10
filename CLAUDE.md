# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Project Overview

TupperDMX Simulator is a PyQt6 lighting-control app that syncs MP3 playback
with MIDI-triggered DMX sequences and a metronome, renders LED bars and blinders
on a virtual stage in real time, and ships a pure-Python "software controller"
emulating a hardware DMX controller via MIDI when targeting localhost Art-Net.

## Development Commands

```bash
uv sync                  # install deps + create .venv
uv run python main.py    # run the app
uv add <package>         # add a dep
uv lock --upgrade        # refresh lockfile
```

Build/distribution commands and CI details live in `BUILD.md`.

Headless characterization tests live in `tests/`. Run the whole suite with
`uv run pytest` (offscreen Qt + a shared QApplication are set up in
`tests/conftest.py`; CI runs this too). Each file is also still runnable
standalone via its `main()` — `uv run python tests/test_clip_ops.py` prints
`ALL TESTS PASSED` — which is handy for the Qt GUI repro (`test_crash_repro.py`
supports a `--sync` flag that way).

### Key Dependencies
PyQt6 (GUI + multimedia; **pinned to 6.8 LTS** — 6.11 crashed on pause/resume),
mido/python-rtmidi (MIDI), custom asyncio Art-Net UDP receiver, librosa (BPM
detection), pylibftdi/pyserial (USB DMX), platformdirs (user-config paths).

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
- **`midi/` — `MidiManager` + `MidiWorker`**: `MidiManager` is a thin
  lifecycle/signals (`data_changed`) facade delegating edits to collaborators
  that share one `MidiDocument` (state + `reconcile_blackouts`). `MidiWorker`
  does timeline-synced threaded output; `"Software emulated"` routes `note_on`
  to `DMXController.handle_note_on()`.
- **`ui/stage_controller.py` — `StageController`**: Virtual stage rendering.
  Updates fixture color/intensity from live DMX data.
- **`software_controller/`**: Pure-Python DMX controller (no hardware needed).
  `DMXController` manages a 96-channel universe (LED bar 1–7, Blinder1 8–15,
  Blinder2 16–23) and reacts to MIDI pitches (channel control 1–11, scene select
  12–19, colors 20–23, bank select 24–25, blinders 26–32, song-part markers
  38–52). `ArtNetSender` emits Art-Net UDP; scenes/banks/chases load from
  `software_controller/dmx_config.json`.

### Configuration (`config.py`)

Storage splits across two files:
- **`config.json`** (shipped, read-only): `fixtures` + `midi_note_map`, bundled
  via PyInstaller, resolved at runtime by `shipped_config_path()` (`_MEIPASS`).
- **`userprefs.json`** (user-writable): everything else (`window`, `artnet`,
  `playlist`, `metronome`, `volume`, `logging`, `last_dmx_source`,
  `last_midi_port`), resolved by `userprefs_path()` via `platformdirs`
  (e.g. `~/.config/tupperDMX/` on Linux; macOS/Windows app-data equivalents).

`Config.load()` reads both; `Config.save()` writes only `userprefs.json`
(created eagerly with defaults on first launch).

### UI Architecture

- `ToolbarManager` (`ui/toolbar_manager.py`): media controls, BPM, source
  selectors, playlist nav (prev/next/remove + counter), and a menu opening the
  About dialog (`ui/about_dialog.py`, shows build info).
- `Timeline` (`timeline/manager.py`): waveform, beat detection, seeking, and
  clip-style MIDI editing — one `MidiEventsTrack` per fixture filter
  (`ledbar`, `blinder`).
- `ClipLibraryWidget` (`ui/clip_library.py`): right sidebar tabs ("Library",
  "DMX Channels"). `QToolBox` with one section per fixture (from
  `config.midi_note_map`) plus one per LED scene bank (from `dmx_config.json`).
  Click previews; drag drops onto the timeline; per-fixture pages hide notes 1–7.
- `DMXDisplayWidget` (`ui/dmx_display.py`): live channel monitor.
- `ErrorDialogManager` (`ui/error_dialogs.py`): centralized retry/reset dialogs.

## Critical Behavior Patterns

### Metronome Sync
On start/resume, compute beats elapsed
(`total_beats_elapsed = timeline_position_sec / beat_duration_sec + 1`) and the
remainder to the next boundary: play immediately if within `0.001s`, else
single-shot timer to the next beat then switch to regular intervals. Always
restart on seek-during-playback or BPM change.

### Media Playback Timeline
- `time.time()`-based tracking; separate `current_timeline_time` /
  `playback_start_time` for pause/resume. MP3 position synced via
  `QMediaPlayer.setPosition()`. `audio.offset` fine-tunes audio↔MIDI/DMX sync.

### MIDI Event Management
- Clip-style: each event is a movable, resizable rectangle on its fixture lane.
  Three drag modes (MOVE, RESIZE_LEFT, RESIZE_RIGHT) with neighbor clamping and
  zoom-aware edge-snap (beat lines are snap targets). Persistence happens **only**
  on `mouseRelease` via `*_committed` signals; mid-drag is purely visual.
- **BLACKOUTs are gap markers**, scoped per fixture (`BLACKOUT` for ledbar,
  `BLACKOUT_BLINDERS` for blinders), identified by part-name PREFIX
  (case-insensitive), not note number. `reconcile_blackouts()` forbids
  zero-duration/adjacent BLACKOUTs and reinserts them to keep clip extents.
  Deleting a regular clip converts it to a fixture-matching BLACKOUT, not a hole.
- Scene clips: 7 simultaneous channel-control `note_on` events sharing a
  `group_id`, rendered as one grouped clip; drags route to the scene API
  (`add_scene`, group move/resize). Clips support fade-in/out via `FadeEngine`;
  multi-selection (marquee/ctrl-click) moves/deletes groups via `SelectionEditor`.

### Clip Library Drag-and-Drop
- MIME types: `application/x-tupperdmx-midi-clip` (single note + velocity),
  `application/x-tupperdmx-led-scene` (full 7-channel scene payload).
- `Timeline.can_accept_clip_drop()` validates fixture-under-cursor matches the
  clip's fixture (scene drops restricted to ledbar); `handle_scene_drop()`
  calls `MidiManager.add_scene()` to expand the payload into 7 grouped events.
- Click-to-preview: `preview_requested` → `MainWindow._preview_clip()` routes
  to active MIDI output (or software controller) without touching the timeline.

### Software Controller Activation Flow
Set the DMX source IP to `127.0.0.1`: `ArtnetWorker` detects localhost,
auto-instantiates `DMXController` (which loops DMX to `ArtNetSender`), and
`DMXManager` emits `software_controller_changed` → `MainWindow` → `MidiManager`.
Then select `"Software emulated"` as the MIDI port so `MidiWorker` routes
`note_on` to `DMXController.handle_note_on()`.

Color palettes via `generate_song_colors(mode, hue_index)` (HSL→RGB,
mutex-protected); `PALETTE_MODES = COMPLEMENTARY, ANALOGOUS, TRIADIC,
COLOR+WHITE, MONOCHROMATIC, SINGLE`. The `START` note has no `song_index` — its
velocity encodes the palette (`hue_index = velocity // 6`, 12 = random hue;
`mode = PALETTE_MODES[velocity % 6]`). `pick_scene_from_song_part()` picks
per-part scenes (all VERSEs share one hardcoded fill). `_apply_blend()`
interpolates present↔future DMX to avoid blackout artifacts.

## File Organization

- `ui/` — `media_controller.py`, `audio_controller.py`, `stage_controller.py`,
  `dmx_display.py`, `clip_library.py`, `toolbar_manager.py`, `error_dialogs.py`,
  `about_dialog.py`.
- `dmx/` — `manager.py` (factory + hot-swap), `artnet_worker.py` (asyncio UDP,
  auto-starts software controller on localhost), `usb_worker.py` (FTDI/serial),
  `base.py`, `factory.py`.
- `midi/` — `manager.py` (facade: worker lifecycle, signals, data proxies),
  `document.py` (`MidiDocument`: state, queries, mutators, `reconcile_blackouts`),
  `codec.py` (`mido.MidiFile` ↔ event/note lists), `fade_engine.py`,
  `clip_editor.py` (single-clip CRUD/move/resize), `scene_editor.py`
  (scene-group ops), `selection_editor.py` (multi-select), `constants.py`,
  `worker.py` (threaded output, `"Software emulated"` port).
- `software_controller/` — `main.py` (`DMXController`), `artnet_sender.py`,
  `dmx.py` (`LedScene`/`BlinderScene`), `scene_loader.py` (`load_led_scenes()`
  → immutable `LedScene` records), `dmx_config.json` (scene banks + chases).
- `timeline/` — `manager.py` (coordinator, one `MidiEventsTrack` per fixture
  filter), `tracks/` (`base.py`, `ruler.py`, `waveform.py`, `midi_events.py`),
  `widgets/` (`tooltip.py`, `content_view.py`, `midi_event_edit.py`,
  `scene_edit.py` — gradient slider/color wheel/animation grid,
  `start_event_edit.py` — START-event editor, `resize_handle.py`).
- `tests/` — headless characterization tests (standalone scripts; see above).
- `tupperlogger/` — custom logger with file rotation.
- `config.py` — dataclass config, two-file split (see Configuration).
- `fixtures.py` — DMX fixture defs + channel mapping.

## Timeline Architecture Notes

- `RulerTrack` / `WaveformTrack` / one `MidiEventsTrack` per `fixture_filter`
  extend `BaseTrack`; Timeline exposes `PIXELS_PER_SECOND` and `fixture_at_y()`
  for drop routing.
- Tracks separate **content items** (cleared on redraw) from **header items**
  (persistent, e.g. metronome controls). Waveform/MIDI heights resize via
  `TrackResizeHandle` (persisted as `window.waveform_track_height` /
  `window.midi_events_track_height`). Cross-component sync via
  `MidiManager.data_changed` (events cached in `_cached_events`); scene groups
  collapse to one `DraggableMidiEvent` via `build_render_units()`.

## Common Debugging

```bash
# Tail today's log
tail -f logs/tupperDMX_$(date +%Y%m%d).log

# Reset user prefs: delete userprefs.json from the platformdirs path above
# (shipped config.json is read-only and stays put)

# Enumerate MIDI outputs
python -c "import mido; print(mido.get_output_names())"

# Smoke-test software controller (Art-Net UDP → 127.0.0.1:6454)
uv run python -c "from software_controller.main import DMXController; c = DMXController(); c.start(); import time; time.sleep(2); c.stop()"
```

## Key Import Reminders

- Timeline editing dialogs need both `QDialog` and `QMessageBox` imported;
  error dialogs require a parent widget reference for consistent styling.
