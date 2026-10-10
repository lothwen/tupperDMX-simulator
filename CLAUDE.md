# CLAUDE.md

Guidance for Claude Code (claude.ai/code) here: what you **cannot** derive by reading the code — layering rules,
invariants, and reasons behind decisions that look arbitrary. Not an index of files or functions.

TupperDMX Simulator is a PyQt6 lighting-control app: MP3 playback synced with MIDI-triggered DMX sequences and a
metronome, fixtures on a virtual stage in real time, plus a pure-Python "software controller" emulating a hardware DMX
controller over MIDI.

## Development Commands

```bash
uv sync                  # install deps + create .venv
uv run python main.py    # run the app
uv run python main.py --headless   # no GUI (SBC/rack): web remote is the only interface
uv run pytest            # headless tests (offscreen Qt + shared QApplication in tests/conftest.py)
```

Build/distribution and CI live in `BUILD.md`; each test file also runs standalone via its `main()`. **PyQt6 is pinned to
6.8 LTS** — 6.11 crashed QMediaPlayer on pause/resume; playback no longer uses it, but QAudioSink/QAudioDecoder ride the
same stack. TTS voice models (~60 MB) download on demand into `userprefs_dir()/tts_voices/`, never bundled.

## Packages and layering

- `main.py` — the only root source file: parses `--headless` **before any Qt import** (sets `QT_QPA_PLATFORM=offscreen`).
- `core/` — the **Qt-free** domain layer (config, show/, paths, OFL parsing, fixture library and state model, note layout,
  clip model, I/O routing).
- `app/` — the shared core: QtCore/QtMultimedia only, **never** QtWidgets or `ui/`.
- `audio/`, `dmx/`, `midi/`, `software_controller/`, `webremote/`, `autoprogram/` — widget-free subsystems
  (`webremote`'s `state`/`commands`/`peaks` are Qt-free too; `autoprogram`'s layering is in its own section).
- `stage/` (render engine) and `timeline/` are **top-level**: QtWidgets packages that never import the `ui/` root (the
  window, its dialogs), which is what earns them the root. `stage/` imports nothing from `ui/` at all; `timeline/` uses
  only `ui/widgets/` and `ui/fixture/`'s `console` and `affordances`, and asks the window for the rest by signal.
- `stage/stage3d/` — the **EXPERIMENTAL** 3D bar prototype (`window.stage_3d_experimental`), under `stage/` only so
  every build ships it; the 2D engine's rules don't bind it. The app reaches it only through `embedded.py`, lazily from
  `MainWindow`; its launcher `scripts/stage3d_bar.py` stays in `scripts/` because `stage/` may not import `dmx/` or
  `software_controller/`.
- `ui/` — three layers: `ui/widgets/` (leaf toolkit, no app knowledge, safe to import), `ui/fixture/` (fixture editing),
  and the root.
- `fixtures/` — fixture definitions (see Fixtures and clips).

**No eager re-exports at any level; import the submodule you want.** `ui/__init__.py` is a docstring because re-exporting
the window and its dialogs made `from ui.widgets import icons` — which the timeline tracks do — drag in the stage engine,
MIDI worker, DMX manager and web remote. Same for `timeline/widgets/__init__.py` and `clip_edit`.

**No code is written per fixture.** What a channel does comes from its OFL capabilities and what a fixture is from its
OFL categories, never from its type name: the stage, the editing controls, the clips and the auto program all read them,
so a fixture straight from OFL works with no sidecar.

## Orchestration

`app/core.py` (`AppCore`) is the ONE orchestration object: it constructs and wires every widget-free collaborator and owns
count-in/voice-cues, `preview_clip`, the transport, `save_config` and the idempotent `shutdown()` (flush FIRST, then
webremote→media→audio→engine→dmx→midi). `ui/main_window.py` is the desktop *view*: it builds the widget tree, aliases
those collaborators onto itself, and connects widget halves riding AppCore signals. Headless force-enables the web remote
in memory (never persisted), never downloads TTS.

## The show and persistence (`core/show/`, `app/show_manager.py`, `core/config.py`)

A show is a **folder** `Name.show/`: `show.json` (the setlist, `current_song_index`, the fixture patch, `favorites`)
plus owned `midis/`, `audio/` (imported bundles), show-local `fixtures/` and `versions/`. `core/show/` is Qt-free: owned
midis stored relative and referenced audio absolute, **missing paths preserved** for relinking, loading a show activates
its fixture-defs layer, and `.showpack` bundles collect ALL audio, backings AND guides. `ShowManager` (QtCore-only,
**attach FIRST** in `AppCore.__init__`) **aliases** `config.playlist`/`config.fixtures` onto the show's objects, so
every reader mutates the show in place. Persistence funnels through `save_config()` → `notify_changed()` (saved iff
content changed) and `flush()` (~3 s debounced `.mid` autosave, forced on song switch/close/Save; opening snapshot + at
most one automatic snapshot per 5 min).

- **The `.mid` stays canonical**: what plain MIDI can't carry (scene `group_id`, `scene_name`, per-clip fades) rides as
  `tdmx1:{json}` text metas, and "Export MIDI…" writes an `interop=True` copy without them. Audio is *referenced in
  place* unless it arrived in a bundle.
- A patch entry is type + `dmx_start` + stable MIDI `channel` + `mode` (`""` = the type's default mode, so a renamed
  mode never strands a patch) + `hanging`, defaulting to **true** so an older patch keeps throwing light at the floor. A
  new manifest key needs no `format_version` bump when its absence loads as empty.
- **`userprefs.json` is MACHINE-level only** (a usable GL driver is a property of the box) and **re-read while the app
  runs** (`app/prefs_watcher.py`): editing it over the share is the only way to change a setting on a headless rack box.
  The shipped `config.json` is only the seed patch for "+ New" shows: a first launch copies
  `assets/demo.show/` instead, file by file and never loading it in place, since the bundle may be read-only.
- **The playlist manager** (`ui/playlist_dialog.py`) edits every song live, with no OK/Cancel. Only the **current**
  song goes through media loading, AppCore setters and `bpm_state`; any other song is plain data, because `_flush_midi`
  writes to whatever song is current (so a reorder keeps `current_song_index` on the same song OBJECT, and an unloaded
  song's BPM is rewritten into its `.mid` on disk). `SongConfig.enabled` + `PlaylistConfig.neighbour` is the ONE
  running-order rule next/prev, the segue, the nav buttons and the web remote follow. **Importing from another show
  reads its manifest with `song_import.peek_show`, NEVER `store.load_show`**, which would switch the global fixture-defs
  layer and create folders in the other show. A delete may remove only files inside the show that no other song uses.

## Audio and the transport

**`docs/AUDIO.md` is the reference** for `audio/`, `app/media_controller.py`, `app/metronome.py`, `core/click.py`, the
offset dialog and the timing diagnostics; read it before changing any of them. The rules here are the ones that break
code elsewhere:

- **ONE stereo `QAudioSink` feeds every path, from its own `TimeCriticalPriority` feed thread.** The stage blocks the Qt
  thread for 46–129 ms at a time (inside Qt/GL C++, with the GIL free), and while the feed lived there it cost a dropout
  roughly every 1.2 s. Never open a second sink or a `QMediaPlayer`, and never touch the sink off that thread.
- **The transport clock is `time.monotonic()`, never `time.time()`**, which steps when NTP corrects an SBC with no RTC
  and would move every cue with it. `MediaController.monotonic_at()` is its public face: phase on the instant a position
  was true, not on "now".
- **An offset moves ONE audio track** (`audio_time = timeline_time - active_offset()`). Clips, song parts and markers
  are anchored in timeline seconds and the other track has its own offset, so nothing follows on its own; whatever
  should follow is moved explicitly, after asking (`ui/offset_dialog.py`).
- **Count-in is negative timeline time.** With `song.precount`, playback starts at `-count_in_duration()` and
  audio/MIDI/lights wait for t ≥ 0, so a position can be negative. Pausing resets to 0; seeking cancels it.
- **The click is SCHEDULED INTO THE OUTPUT, never played by a timer**: each beat is placed at its exact output frame up
  to 0.5 s ahead, so a Qt-thread stall cannot move it. `Metronome.follow()` is the ONE start/seek/retime entry and only
  AppCore calls it. **The window never starts or stops the click**: it once did, and its bare restart undid both the
  stop-click and the anchor.

## The marker track (`core/markers.py`, `core/tempo_map.py`, `ui/markers_dialog.py`)

Four things ride a song's timeline that are neither notes nor audio: a spoken **voice cue**, a **tempo change**, a
**stop-click**, and the **segue** out of the song. How the click reads them is `docs/AUDIO.md` §6, how the ruler draws
them `docs/TIMELINE.md` §10.

- **Voice TTS keeps its own `SongConfig.voice_tts` list**, apart from `control_markers`: it is the one kind that
  pre-renders to PCM off-thread, and shows written before control markers existed already persist it under its own key
  (the oldest as `voice_cues`, still read).
- **`BpmState` owns the `TempoMap`**, because base tempo + markers together ARE the song's tempo: a map assembled
  independently by the metronome and by the ruler is a map that can disagree with itself. `bpm` stays the BASE tempo.
- **The tempo map is NOT written to the `.mid`, and a tempo marker never writes `bpm_state`.** The base BPM is what the
  `.mid` persists and what the autosave watches, and `midi.codec.apply_tempo` rescales every delta tick and replaces
  *every* `set_tempo` with one value, so extra tempo metas would drag the whole sequence with them.
- **Beats are counted continuously across a change** — a marker dropped mid-bar shortens the rest of that bar rather than
  restarting the bar numbering. Negative time extrapolates at the FIRST segment's tempo, which is what lets
  `count_in_duration()` be `-seconds_at_beat(-bars × beats_per_bar)` and land exactly on beat 1 at t=0.
- **A song has ONE meter, and the BPM ALWAYS counts quarter notes** (the user's call — a first cut counted the written
  beat and was reversed). A map beat is one click of the meter's own note, so 6/8 at 120 is six clicks a bar, two to
  each quarter: `bpm_at()`, tempo markers and the `.mid` count quarters; the click, grid, count-in and bar.beat count
  beats.

## The auto program (`autoprogram/` + `app/autoprogram.py`, ☰ ▸ Auto program…)

**`docs/AUTOPROGRAM.md` is the reference**, including the show-design rules in its §5, most of them the user's explicit
calls; read it before changing what a take generates. What reaches outside the package:

- **Layered one way**: `streaming` → `analysis` → `colours`/`looks`/`cues`/`director` → `writer` → `take` → `replay` →
  `score`. Up to `director` it is Qt-free and never sees `MidiManager`. **`writer` is the only module that writes
  MIDI**, inside `MidiManager.bulk_edit()`. `take` is the one pipeline; the app and `scripts/autoprogram_cli.py` both
  call it.
- **No scipy, at runtime or in dev.** The analysis was tuned on scipy's filters, and the numpy ports reproduce them to
  float32 rounding; a "simpler" filter would move every threshold.
- **A generated `.mid` must reload exactly as authored.** `writer`'s limits (`MIN_GAP`, `MIN_FADE`, `FADE_MARGIN`,
  one-channel looks as clips) are set against the load-time fade and scene detection, so changing that can break them.
- **Generating never touches the show** (a worker thread, a copy of the patch, a `.mid` in a temp folder).
  **Applying** flushes, takes a pinned snapshot `before auto-program — <title>`, then calls
  `ShowManager.write_song_midi`; the current song reloads inside `begin/end_song_load`, so the autosave is not armed.
  Desktop only.
- **`replay` drives a `DMXController` built with its own `clock` and `rng`**, never by patching
  `software_controller.main`, because the live controller runs in the same process.
- **A palette only borrows the Palette tab's maths**: the show never touches note 60 or the live colour cycling.

## I/O routing (`config.io` + `core/io_routing.py`)

All routing is explicit in Settings ▸ Input / Output — no toolbar selectors, no localhost magic. Applying `config.io` sets
MIDI routing and drives `DMXManager.apply_io(io)`, which owns the software `DMXController` lifecycle → its output *sinks*
→ the stage-input worker. With the defaults ("All software") the controller runs and feeds the stage **in-process** via
`StageFrameBridge` — no UDP loopback. Cardinalities: MIDI out 1→N (a stateful command protocol — fan-out fine, **fan-in
forbidden**), controller DMX out 1→N (idempotent snapshots), stage DMX in = exactly one selectable feed.

**Only the USB output leg can be chosen outside Settings**, because headless has no dialog: `--dmx-usb INDEX|auto`
(indices from `--list-dmx-usb`, hence that list is **sorted**) goes to `Config.override_io()`, NOT into `config.io`, so
it survives a live prefs reload and `save()` never writes it. `auto` **owns the leg** (nothing detected means USB off,
never a remembered device), while an explicit index that isn't there exits 2. The buildroot init script boots with
`--dmx-usb auto`.

## The render engine (`stage/`)

**`docs/STAGE.md` is the reference** for `stage/` (all but `stage3d/`); read it before changing the engine. The OFL
category picks a fixture's body and its capabilities pick the layers that shape its light. What reaches outside it:

- **The GPU path (`config.window.stage_gpu`, with the volumetric fog under it) is opt-in and fails soft to raster.**
  `QWidget.grab()` cannot read back a GL viewport, so screenshots need raster. After the Settings dialog, `MainWindow`
  must call `StageController.apply_stage_config()`: Settings replaces `config.window.fog` with a new object, which the
  GPU layer would otherwise never see.

## Fixtures and clips (`core/ofl.py`, `core/fixture_*.py`, `core/clips.py`, `ui/fixture/`)

**`docs/FIXTURES.md` is the reference** for the fixture library, the photometric model, the fixture dialogs and the clip
model; read it before changing any of them. What reaches outside them:

- **A definition is a strict OFL `*.json` the app never writes**, plus an optional `*.tdmx.json` sidecar and
  `*.effects/` artwork folder, in three layers (shipped, user, the open show's; a later one wins by key). **A subfolder
  is the manufacturer and namespaces the key**; a root file keeps the bare key saved patches use. A sidecar never
  changes the DMX footprint, and unresolvable slots keep their address, because a `.mid` binds note N to the N-th
  channel.
- **`save_fixture_banks` / `save_fixture_chases` are the only writers**, and write banks and chases separately (the two
  tabs each hold their own copy). Bank order, scene order and chase order are MIDI addresses.
- **The MIDI channel is a fixture's identity**: unique, refused when duplicated, never reassigned. Open the edit dialog
  only through `MainWindow.open_fixture_editor`, which defers itself because every caller is a widget an edit destroys.
- **A clip is derived, never stored**, and **a favourite stores a symbolic ref, never MIDI**; one that stops resolving
  is shown dimmed, never deleted.

## Domain layer

- **`software_controller/`** — a pure-Python DMX controller (no hardware) managing a 512-channel universe patched from
  `config.fixtures`. `handle_note_on`'s pitch is a universal *verb* and the MIDI **channel selects the fixture** (the
  patch entry's stable `channel`); the note table is `docs/TIMELINE.md` §3.2.
- **Notes 60 (palette) and 71 (setup flags) are per-fixture STATE, not clips**: at t=0, at most one per channel, in
  `midi_notes` only, so they persist and replay but never draw, drop or shift (`docs/TIMELINE.md` §3.7). **Absence IS
  the default.** `scripts/migrate_start_note.py` converted old shows once; **the app carries no migration code**.
- **`webremote/`** — embedded tablet remote, off by default; plain HTTP on the stage LAN, no auth, aiohttp on its own
  asyncio loop in a QThread. Inbound commands run on the Qt thread, cues/blackout/panic through `preview_clip` so
  `config.io` routing applies. The manager **polls** the blackout toggle and the free-run's map on its tick rather than
  trusting signals (a retime from the desktop panel emits nothing). Panic sends note 51 only when blackout is off.

## UI notes

- **1280×800 is the supported minimum display.** Both toolbars are wider than that with everything shown, so each folds
  its low-priority half on a narrow window: the top bar's SHOW card into the ☰ menu, the timeline row's Rows/zoom/Fit
  zone into a "View ▾" popup.
- **`config.window.ui_scale`** (0.75–1.50, Settings ▸ Interface) is applied by `ui/theme.py` rewriting the theme's
  pixel values on load. **A size set in Python is invisible to all of this**: move it into the stylesheet, or multiply
  it by `theme.applied_scale()` as the playlist and auto-program dialogs do (the timeline re-measures in
  `set_ui_scale`).
- **MIDI channels are 0-15 everywhere** — the raw MIDI channel, on every surface.

## Timeline and clip editing

**`docs/TIMELINE.md` is the reference** for `timeline/` (the clip editor included) and the editing half of `midi/`
(`MidiManager`'s editing API, `MidiDocument`, the clip/scene/selection editors, the fade engine); read it before
changing either. What reaches outside them:

- **Every MIDI edit goes through `MidiManager`**, and the `.mid` is rebuilt from `midi_notes`, never from
  `midi_events`: a change made only to the UI list shows on screen and is lost on the next rebuild. Move a clip with
  `move_event`/`move_scene`, never `edit_event(time=...)`, which neither finds room nor keeps the BLACKOUTs honest, and
  wrap a multi-call edit in `history_batch()`: one user action, one undo step.
- **A clip has no length**: it runs until the next event on its lane, so only another event can end it. That event is
  a BLACKOUT gap marker, identified by part-name PREFIX (never by note number) and never drawn.
- **A scene is only the channels it carries**: at runtime an absent channel holds the last frame's value, while a
  present one at its default *forces* it. So the clip editor submits only `present | touched`, and a scene grows or
  shrinks only through `update_scene(..., remove=)`.
- **A drag is visual until release, and the commit's redraw is deferred**: from a scene item's handler, menu or drop,
  call `_schedule_refresh()`, never `refresh_from_manager()`, or the redraw frees the item while Qt is still unwinding
  its event.
- **`core/note_layout.py`, `verb_editors._TABLE` and `handle_note_on` change together**, or the picker, the drops and
  the runtime disagree about what a note means on a fixture. Lanes bind by MIDI channel and display by the patch's
  `order`; never find a lane's fixture by its row.

## Testing notes

Tests are headless and GL-free (CI's offscreen platform cannot make a context). `ofl_factory.py` builds synthetic OFL
documents so a capability nobody ships a fixture for can still be exercised. `test_capability_coverage.py`,
`test_renderer_registry.py` and `test_stage_optics.py` carry the three tables that fail loudly when OFL gains a capability
type, category or wheel-slot type nobody handled. NB **`[255] * channel_count` is not a "fixture on" frame** — on anything
with a shutter it selects a strobe, so use `_lit_frame()`.

## Common Debugging

```bash
# Click timing and Qt-thread stalls: scripts/click_probe.py, TUPPERDMX_STALL_TRACE=1 (docs/AUDIO.md §8)

# Tail today's log (in `logs/` unless userprefs' logging.log_dir names another folder)
tail -f logs/tupperDMX_$(date +%Y%m%d).log
```

The software controller emits NOTHING until it has sinks: a smoke test needs
`c.set_sinks([software_controller.artnet_sender.ArtNetSender(ip=..., port=6454)])` before `c.start_background()`.

Reset user prefs by deleting `userprefs.json` from the platformdirs path. Show data is NOT in prefs — each show is a
self-contained `<Name>.show/` folder with its own `versions/` history.
