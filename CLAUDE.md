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
detection), pyserial (USB DMX via the stock VCP/ftdi_sio drivers),
platformdirs (user-config paths), aiohttp (embedded web-remote server),
mutagen (setlist duration probes).

## Architecture Overview

### Core Components

- **`ui/main_window.py` — `MainWindow`**: Central orchestrator. Coordinates
  UI/managers, syncs playback state across media/metronome/timeline, persists
  config. `main.py` (repo root) is just the thin launcher (QApplication +
  theme + `Config.load()` + `open_or_create_startup_show()` + logging).
- **`core/show/` + `ui/show_controller.py` — the Show project layer**: a show
  is a **folder** `Name.show/` (`show.json` manifest + owned `midis/` +
  `audio/` for imported bundles + show-local `fixtures/` defs + `versions/`
  snapshots). The Qt-free package holds `model.py` (`Show`, `ShowError`,
  `song_status()`), `manifest.py` (dict ↔ `Show`; owned midis stored relative,
  referenced audio absolute, missing paths PRESERVED for relinking),
  `store.py` (atomic JSON, create/load/save/duplicate, `allocate_midi_path`;
  loading a show activates its fixture-defs layer), `migrate.py` (startup
  chain last→recents→create; one-time legacy-userprefs migration),
  `versions.py` (snapshots: show.json+midis, retention 25 unpinned, pinned
  named ones never pruned, undoable restore), `relink.py` (basename search),
  `bundle.py` (`.showpack` zip: collect & save audio + non-shipped defs).
  `ShowManager` (Qt, owned by `MainWindow`, attach FIRST in `__init__`)
  **aliases** `config.playlist`/`config.fixtures` onto the show's objects so
  every existing reader mutates the show in place; persistence funnels through
  `MainWindow.save_config()` → `notify_changed()` (manifest saved iff its
  serialized content changed) and `flush()` (debounced ~3 s autosave of the
  active song's `.mid`, forced on song switch / close / manual Save; opening
  snapshot + ≤1 automatic snapshot per 5 min of writes).
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
  (`core/io_routing.py`): `note_on` verbs to `DMXController.handle_note_on()`
  and/or the full stream to an external mido port, independently.
- **`ui/stage_controller.py` — `StageController`**: Virtual stage rendering +
  scenario designer. Updates fixture color/intensity from live DMX data;
  dispatches render per type (bespoke `LedBar`/`Blinder`, else the data-driven
  `GenericFixture`, all in `ui/stage_fixtures.py`). `set_design_mode()` makes fixtures movable/
  selectable; `add_fixture`/`remove_selected`/drag-to-move mutate `config.fixtures`
  and emit `fixture_added/moved/removed/selected`. The floating
  `ui/fixture_designer.py` panel (Menu ▸ "Design stage") drives it.
- **`core/fixture_library.py`**: data-driven fixture-type *library* — `FixtureDef`
  (channels with semantic role + flags, render hint, plus scene `banks`/`chases`)
  loaded from `fixtures/*.json` in three layers — shipped `fixtures/`, writable
  `userprefs_dir()/fixtures/`, and the open show's `fixtures/`
  (`set_show_fixtures_dir()`); later layers override by name; memoized. Every type —
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
- **`webremote/`**: Embedded tablet remote (Settings ▸ WEB REMOTE, off by
  default; plain HTTP on the stage LAN, no auth). `WebRemoteWorker`
  (`server.py`) runs an aiohttp HTTP+WS app on its own asyncio loop in a
  QThread (ArtNetWorker pattern); `WebRemoteManager` (`manager.py`, owned by
  `MainWindow`) subscribes to existing signals, publishes full-state snapshots
  plus 10 Hz `pos` / per-beat `beat` events, and executes inbound commands on
  the Qt thread — transport via MainWindow/PlaylistController, cues/blackout/
  panic via `_preview_clip` (so `config.io` routing applies). Blackout is a
  toggle: panic stops playback and sends note 51 only when
  `blackout_all_state` is off. Qt-free logic in `state.py` (snapshot schema,
  section spans, mtime-cached duration/BPM probes), `commands.py` (action
  whitelist/parse) and `peaks.py` (waveform bins for the canvas). The
  no-build page lives in `webremote/static/` and is responsive: a CSS-grid
  (`grid-template-areas`) reflows the same blocks into a 3-column tablet
  layout, a portrait-tablet rail layout, and a single-column phone layout
  (≤600px) where the body scrolls under a sticky header + fixed transport,
  the setlist becomes a "+"-triggered drawer, and a slim seek bar replaces
  waveform-tap. Phase-3/4 widgets (count-in, click/backing meters, key,
  show name) are inert `placeholder` elements.

### Configuration & persistence (`core/config.py` + `core/show/`)

Storage splits across three places:
- **`config.json`** (shipped, read-only): `fixtures` (the *seed* patch for
  brand-new shows), bundled via PyInstaller, resolved at runtime by
  `shipped_config_path()` (`_MEIPASS`).
- **`userprefs.json`** (user-writable, MACHINE-level only): `window`, `artnet`
  — the controller's Art-Net *output* target — `volume`, `logging`, `io` — the
  explicit I/O routing, `IOConfig` — `webremote` (server enable/port + the
  default cue set that seeds NEW shows) and `last_show_path`/`recent_shows`.
  Resolved by `userprefs_path()` via `platformdirs` (e.g. `~/.config/tupperDMX/`
  on Linux). Written atomically (temp + `os.replace`). Legacy
  `last_dmx_source`/`last_midi_port` keys migrate one-time into `io`; legacy
  `playlist`/`fixtures` keys are still *parsed* (they're the source the
  one-time show migration adopts) but never written again.
- **`<Name>.show/show.json`** (the SHOW manifest, per project): the setlist
  (`songs` with `id`/`title`/`key`/`precount`/`offset`/paths + a derived
  `cache` of duration/bpm), `current_song_index`, the fixture **patch**
  (`fixtures`) and `webremote_cues`. Owned `.mid` files live in `midis/` and
  are written by the autosave; audio is *referenced in place* (absolute path)
  unless it arrived inside a bundle. The `.mid` stays the canonical sequence
  format — authoring extras plain MIDI can't carry (stable scene `group_id`,
  custom `scene_name`, per-clip fades) ride inside it as `tdmx1:{json}` text
  metas (`midi/codec.py`; "Export MIDI…" writes an `interop=True` copy
  without them).

`Config.load()` reads shipped+prefs; the startup show is resolved by
`core/show/migrate.open_or_create_startup_show()` (first launch converts the
legacy prefs playlist/patch into `~/Documents/TupperDMX/Untitled.show/`,
copying the `.mid`s in and leaving originals untouched). After
`ShowManager.attach()`, `config.playlist`/`config.fixtures` ARE the show's
objects, so all pre-show code paths keep working unchanged.

### UI Architecture

- `ToolbarManager` (`ui/toolbar_manager.py`): PROJECT · SHOW section
  (header with live autosave state; + New/📂 Open/💾 Save/Save as…/Import
  bundle buttons; footer with show name + setlist/patch stats), SETLIST
  section (header "N songs"; a row of prev / an editable song-selector
  QComboBox — a "05/11" counter badge + the full setlist; activated() picks a
  song via `song_selected`, edit+Enter renames via `song_title_edited` / next /
  remove 🗑; footer BPM / offset / derived "linked" media chip), SONG FILES
  section (audio + MIDI path displays with Load buttons and the ▷ Track new-MIDI
  action), EXPORT section (header + two stacked buttons: interop ⤓ Export MIDI…
  and the amber-accented 📦 Export bundle — the ▾ is a decorative cue, single
  click exports the full .showpack), VERSIONS section (header; a snapshot-picker
  QComboBox — newest first, autosave "● Autosave · time" vs pinned
  "📌 name · time" — + a Restore button emitting
  `restore_version_requested(version_id)` → `MainWindow.restore_version_from_toolbar`
  which confirms then rolls back; footer 📌 Snapshot action + "N autosaves ·
  M pinned" counts, fed by `set_versions(list_versions(show))`), and the ☰ menu
  (Settings…, About). `PlaylistController.update_song_counter()` is the central
  setlist refresh (counter + combobox `set_setlist` + nav + show stats);
  `MainWindow._refresh_versions_ui` repopulates the picker on
  `ShowManager.versions_changed` (emitted whenever a snapshot is created:
  open/attach, autosave `_maybe_snapshot`, restore) plus after a manual
  snapshot and at startup.
- `SettingsDialog` (`ui/settings_dialog.py`): modal userprefs editor, two-column
  **Input / Output** page. Left: three numbered routing cards editing `config.io`
  (① sequencer MIDI outputs = checkboxes, ② internal controller — an iOS-style
  `_ToggleSwitch` for `controller_enabled` greys the body — + DMX output checkboxes
  incl. the Art-Net target fields, ③ stage input radios) plus quick-mode presets
  and a compact Logging card. Right: a live signal-flow graph (`ui/io_flow_widget.py`
  `IOFlowWidget`, rendered from `io_routing.flow_state(io, ip, universe)` — nodes/
  edges light up, animated dashes MIDI=violet/DMX=amber, dead routes red, stage
  "BLACKOUT" pill) over a live validation strip from `io_routing.validate()` (hard
  errors block accept; soft warnings inline). The two universe spinboxes (blocks
  ②/③) edit the one shared `ArtNetConfig.universe` and mirror each other.
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

### I/O Routing (`config.io` + `core/io_routing.py`)
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

- `main.py` (repo root, the only root source file) — thin entry point; the
  window itself lives in `ui/main_window.py`.
- `core/` — Qt-free shared domain layer (no eager re-exports; import
  submodules directly): `config.py` (dataclass config — see Configuration &
  persistence), `show/` (the show-project package: `model.py`, `manifest.py`,
  `store.py`, `migrate.py`, `versions.py`, `relink.py`, `bundle.py`),
  `paths.py` (single source of `bundle_dir()`/`asset_path()`/
  `userprefs_dir()`/`default_shows_dir()`, dev vs. PyInstaller `_MEIPASS`),
  `fixture_library.py`
  (data-driven fixture-type library: `FixtureDef`, JSON loaders, role/flag
  maps, plus the per-type presentation colours `GROUP_COLORS`/
  `color_for_type()`), `note_layout.py` (note → label/colour lookup),
  `io_routing.py` (pure routing logic: `MidiRouting`, presets, `validate()`,
  `flow_text()`, and `flow_state()` → `FlowState`, the render-ready
  signal-flow graph state), `buildinfo.py` (build metadata accessor).
- `fixtures/` (repo root) — shipped fixture-type definitions (`*.json`).
- `ui/` — `main_window.py` (`MainWindow`), `media_controller.py`,
  `audio_controller.py`, `stage_controller.py`, `stage_fixtures.py` (Qt render
  objects: bespoke `LedBar`/`Blinder`, the data-driven `GenericFixture`, and
  the `DesignableFixture` mixin — design-mode move/select + drag-release
  position write-back), `fixture_designer.py` (scenario-designer panel + add
  dialog), `dmx_display.py`, `clip_library.py`, `toolbar_manager.py`,
  `show_controller.py` (`ShowManager`: aliasing, autosave, open/restore;
  snapshot restore is driven from the toolbar's VERSIONS picker),
  `settings_dialog.py` (incl. the Input/Output section), `io_flow_widget.py`,
  `error_dialogs.py`, `about_dialog.py`.
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
- `webremote/` — `server.py` (`WebRemoteWorker`: aiohttp HTTP+WS in a
  QThread), `manager.py` (`WebRemoteManager`: Qt bridge — worker lifecycle,
  snapshots, command execution), Qt-free `state.py` / `commands.py` /
  `peaks.py`, and `static/` (the shipped tablet page: `index.html`,
  `style.css`, `app.js`; bundled via build.spec).
- `timeline/` — `manager.py` (coordinator, one `MidiEventsTrack` per fixture
  filter), `tracks/` (`base.py`, `ruler.py`, `waveform.py`, `midi_events.py`),
  `widgets/` (`tooltip.py`, `content_view.py`, `midi_event_edit.py`,
  `scene_edit.py` — gradient slider/color wheel/animation grid,
  `start_event_edit.py` — START-event editor, `resize_handle.py`).
- `tests/` — headless characterization tests (standalone scripts; see above).
- `tupperlogger/` — custom logger with file rotation.

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
# (shipped config.json is read-only and stays put). Show data is NOT in
# prefs — each show is a self-contained <Name>.show/ folder (default under
# ~/Documents/TupperDMX/), with its own versions/ snapshot history.

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
