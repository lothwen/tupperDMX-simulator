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
- **`dmx/` — `DMXManager`**: I/O coordinator driven by `config.io` via
  `apply_io()`: owns the software `DMXController`'s lifecycle (emits
  `software_controller_changed`), builds its DMX output *sinks*
  (`StageFrameBridge` in-process feed → stage, `ArtNetSender` → network,
  `USBDMXSender` → dongle), and runs the stage-input worker (`ArtnetWorker`
  pure listener on 0.0.0.0, or `USBDMXWorker` FTDI reader) when the stage
  input isn't the internal controller.
- **`midi/` — `MidiManager` + `MidiWorker`**: `MidiManager` is a thin
  lifecycle/signals (`data_changed`) facade delegating edits to collaborators
  that share one `MidiDocument` (state + `reconcile_blackouts`). `MidiWorker`
  does timeline-synced threaded output with **fan-out** per `MidiRouting`
  (`io_routing.py`): `note_on` verbs to `DMXController.handle_note_on()`
  and/or the full stream to an external mido port, independently.
- **`ui/stage_controller.py` — `StageController`**: Virtual stage rendering +
  scenario designer. Updates fixture color/intensity from live DMX data;
  dispatches render per type (bespoke `LedBar`/`Blinder`, else the data-driven
  `fixtures.GenericFixture`). `set_design_mode()` makes fixtures movable/
  selectable; `add_fixture`/`remove_selected`/drag-to-move mutate `config.fixtures`
  and emit `fixture_added/moved/removed/selected`. The floating
  `ui/fixture_designer.py` panel (Menu ▸ "Design stage") drives it.
- **`fixture_library.py`**: data-driven fixture-type *library* — `FixtureDef`
  (channels with semantic role + flags, render hint, plus scene `banks`/`chases`)
  loaded from `fixtures/*.json` (shipped `fixtures/` + writable
  `userprefs_dir()/fixtures/`, user overrides by name; memoized). Every type —
  including the built-in `ledbar`/`blinder` — is a library def;
  `software_controller.build_fixture_types()` builds the whole `FIXTURE_TYPES`
  registry from it (no Python singletons), and
  `config.fixture_channel_count()`/`known_fixture_types()` derive from it.
- **`software_controller/`**: Pure-Python DMX controller (no hardware needed).
  `DMXController` manages a 96-channel universe, patched data-driven from
  `config.fixtures` (type name + DMX address; default `ledbar`@1, two
  `blinder`s@33/65). `handle_note_on(channel, pitch, velocity)` uses a
  **generalized note map**: the pitch is a universal *verb* and the MIDI
  **channel selects the fixture** (its index in the patch); per-fixture verbs
  resolve against that fixture's own def. Layout: 1–23 channel-offset sets
  (velocity×2), 24–35 scene loaders, 36–47 bank select, 48 take-manual /
  49 manual / 50 blackout-fixture, 51 blackout-all (global), 52–59 timing /
  palette / scene step, 60 START, 61–69 song parts (global), 72–75 color macros.
  The 20 Hz output loop fans each universe snapshot to pluggable *sinks*
  (`set_sinks()`; `ArtNetSender` UDP, `USBDMXSender` FTDI DMX TX, or the Qt
  `StageFrameBridge`); each type's scene banks/chases live in its
  `fixtures/*.json` def. (Saved `.mid` from the old positional layout are
  upgraded by `scripts/migrate_midi_v2.py`.)

### Configuration (`config.py`)

Storage splits across two files:
- **`config.json`** (shipped, read-only): `fixtures` (a *seed* patch) +
  `midi_note_map`, bundled via PyInstaller, resolved at runtime by
  `shipped_config_path()` (`_MEIPASS`).
- **`userprefs.json`** (user-writable): everything else (`window`, `artnet`
  — the controller's Art-Net *output* target — `playlist`, `metronome`,
  `volume`, `logging`, and `io` — the explicit I/O routing, `IOConfig`),
  resolved by `userprefs_path()` via `platformdirs`
  (e.g. `~/.config/tupperDMX/` on Linux; macOS/Windows app-data equivalents).
  Legacy `last_dmx_source`/`last_midi_port` keys migrate one-time into `io`
  (`Config._migrate_legacy_io`) and are dropped on the next save.

`Config.load()` reads both; `Config.save()` writes only `userprefs.json`
(created eagerly with defaults on first launch). The fixture **patch** is seeded
read-only from `config.json`, but once the scenario designer edits it
(`mark_patch_dirty()`) — or `userprefs.json` already carries a `fixtures` key —
`save()` persists `fixtures` into `userprefs.json`, which then *overrides* the
shipped seed on the next load. A default install that never edits the patch keeps
reading the shipped seed (so shipped-config updates still reach it).

### UI Architecture

- `ToolbarManager` (`ui/toolbar_manager.py`): file loading/export, playlist
  nav (prev/next/remove + counter), and the ☰ menu (Settings…, About).
- `SettingsDialog` (`ui/settings_dialog.py`): modal userprefs editor. Its
  **Input / Output** section edits `config.io` by block (① sequencer MIDI
  outputs = checkboxes, ② internal controller enable + DMX output checkboxes
  incl. the Art-Net target fields, ③ stage input radios), with quick-mode
  presets, a live "resulting flow" strip, and validation from
  `io_routing.validate()` (hard errors block accept; soft warnings inline).
- `Timeline` (`timeline/manager.py`): waveform, beat detection, seeking, and
  clip-style MIDI editing — one `MidiEventsTrack` per fixture filter
  (`ledbar`, `blinder`).
- `ClipLibraryWidget` (`ui/clip_library.py`): right sidebar tabs ("Library",
  "DMX Channels"). `QToolBox` with one section per fixture (from
  `config.midi_note_map`) plus one per LED scene bank (from the `ledbar` def's
  banks via `scene_loader.load_led_scenes()`).
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

### I/O Routing (`config.io` + `io_routing.py`)
All routing is explicit in Settings ▸ Input / Output (no toolbar selectors,
no localhost magic). `MainWindow._apply_io_config()` applies `config.io`:
`MediaController.set_midi_routing(midi_routing_from_io(io))` (fan-out:
internal controller and/or one external port) and `DMXManager.apply_io(io)`
(controller lifecycle → sinks → stage input). With the defaults ("All
software" preset) the controller runs and feeds the stage **in-process** via
`StageFrameBridge` (cross-thread queued signal into `dmx_data_received`) —
no UDP loopback. `software_controller_changed` still reaches
`MainWindow` → `MidiManager` exactly as before. Cardinalities: MIDI out 1→N
(stateful command protocol: fan-out fine, fan-in forbidden), controller DMX
out 1→N (idempotent snapshots), stage DMX in = exactly one selectable feed.

Color palettes via `generate_song_colors(mode, hue_index)` (HSL→RGB,
mutex-protected); `PALETTE_MODES = COMPLEMENTARY, ANALOGOUS, TRIADIC,
COLOR+WHITE, MONOCHROMATIC, SINGLE`. The `START` note has no `song_index` — its
velocity encodes the palette (`hue_index = velocity // 6`, 12 = random hue;
`mode = PALETTE_MODES[velocity % 6]`). `pick_scene_from_song_part()` picks
per-part scenes (all VERSEs share one hardcoded fill). `_apply_blend()`
interpolates present↔future DMX to avoid blackout artifacts.

## File Organization

- `ui/` — `media_controller.py`, `audio_controller.py`, `stage_controller.py`,
  `fixture_designer.py` (scenario-designer panel + add dialog), `dmx_display.py`,
  `clip_library.py`, `toolbar_manager.py`, `settings_dialog.py` (incl. the
  Input/Output section), `error_dialogs.py`, `about_dialog.py`.
- `fixture_library.py` (repo root) — data-driven fixture-type library
  (`FixtureDef`, JSON loaders, role/flag maps). `fixtures/` (repo root) — shipped
  fixture-type definitions (`*.json`). `io_routing.py` (repo root) — pure
  routing logic: `MidiRouting`, presets, `validate()`, `flow_text()`.
- `dmx/` — `manager.py` (I/O coordinator: `apply_io`, `StageFrameBridge`,
  stage-input worker lifecycle), `artnet_worker.py` (asyncio UDP pure
  listener on 0.0.0.0), `usb_worker.py` (FTDI/serial reader), `base.py`.
- `midi/` — `manager.py` (facade: worker lifecycle, signals, data proxies),
  `document.py` (`MidiDocument`: state, queries, mutators, `reconcile_blackouts`),
  `codec.py` (`mido.MidiFile` ↔ event/note lists), `fade_engine.py`,
  `clip_editor.py` (single-clip CRUD/move/resize), `scene_editor.py`
  (scene-group ops), `selection_editor.py` (multi-select), `constants.py`,
  `worker.py` (threaded output with controller/port fan-out).
- `software_controller/` — `main.py` (`DMXController`), `artnet_sender.py`,
  `usb_dmx_sender.py` (FTDI DMX512 TX: break/MAB/start-code, self-disabling),
  `scene_loader.py` (`load_led_scenes()` → immutable `LedScene` records sourced
  from the `ledbar` fixture def's banks). Scene banks/chases live in the
  `fixtures/*.json` defs (no more `dmx.py`/`dmx_config.json`).
- `timeline/` — `manager.py` (coordinator, one `MidiEventsTrack` per fixture
  filter), `tracks/` (`base.py`, `ruler.py`, `waveform.py`, `midi_events.py`),
  `widgets/` (`tooltip.py`, `content_view.py`, `midi_event_edit.py`,
  `scene_edit.py` — gradient slider/color wheel/animation grid,
  `start_event_edit.py` — START-event editor, `resize_handle.py`).
- `tests/` — headless characterization tests (standalone scripts; see above).
- `tupperlogger/` — custom logger with file rotation.
- `config.py` — dataclass config, two-file split (see Configuration).
- `fixtures.py` — Qt render objects: bespoke `LedBar`/`Blinder`, the data-driven
  `GenericFixture`, and the `DesignableFixture` mixin (design-mode move/select +
  drag-release position write-back).

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

# Smoke-test software controller (emits nothing without sinks; give it one)
uv run python -c "
from software_controller.main import DMXController
from software_controller.artnet_sender import ArtNetSender
import time
c = DMXController(); c.set_sinks([ArtNetSender(ip='127.0.0.1', port=6454)])
c.start_background(); time.sleep(2); c.stop()"
```

## Key Import Reminders

- Timeline editing dialogs need both `QDialog` and `QMessageBox` imported;
  error dialogs require a parent widget reference for consistent styling.
