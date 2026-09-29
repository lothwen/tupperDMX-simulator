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
uv add <package>         # add a dep
uv lock --upgrade        # refresh lockfile
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
- `audio/`, `dmx/`, `midi/`, `software_controller/`, `webremote/` — widget-free subsystems (`webremote`'s
  `state`/`commands`/`peaks` are Qt-free too).
- `stage/` (render engine) and `timeline/` are **top-level**: QtWidgets importing nothing from `ui/` (only `core/`, PyQt6,
  `tupperlogger`), which is what earns them the root.
- `stage/stage3d/` — the **EXPERIMENTAL** 3D bar prototype, shown in the 2D stage's place behind
  `window.stage_3d_experimental`; it lives under `stage/` only so every build ships it, and the 2D engine's rules below
  don't bind it. The app reaches it through `embedded.py` alone, lazily from `MainWindow`. Its standalone launcher
  (`scripts/stage3d_bar.py`, with its own Art-Net listener or controller) stays in `scripts/` because `stage/` may not
  import `dmx/` or `software_controller/`.
- `ui/` — three layers: `ui/widgets/` (leaf toolkit, no app knowledge, safe to import), `ui/fixture/` (fixture editing —
  the timeline reaches in for `console` and `affordances`), and the root.
- `fixtures/` — strict OFL `*.json` + optional `*.tdmx.json` sidecar + optional `*.effects/` artwork folder. A **subfolder
  is the manufacturer** and namespaces the key (`fixtures/ADJ/focus-spot-4z.json` → `"ADJ/Focus Spot 4Z"`); a root file
  keeps its **bare** key, which is what saved patches refer to — hence the artwork folder is `ledbar.effects/`, not
  `ledbar/`.

**No eager re-exports at any level; import the submodule you want.** `ui/__init__.py` is a docstring because re-exporting
the window and its dialogs made `from ui.widgets import icons` — which the timeline tracks do — drag in the stage engine,
MIDI worker, DMX manager and web remote. Same for `timeline/widgets/__init__.py` and `clip_edit`.

## Orchestration

`app/core.py` (`AppCore`) is the ONE orchestration object: it constructs and wires every widget-free collaborator and owns
count-in/voice-cues, `preview_clip`, the transport, `save_config` and the idempotent `shutdown()` (flush FIRST, then
webremote→media→audio→engine→dmx→midi). `ui/main_window.py` is the desktop *view*: it builds the widget tree, aliases
those collaborators onto itself, and connects widget halves riding AppCore signals. Headless force-enables the web remote
in memory (never persisted), never downloads TTS.

## The Show project layer (`core/show/` + `app/show_manager.py`)

A show is a **folder** `Name.show/` — `show.json` plus owned `midis/`, `audio/` (imported bundles), show-local `fixtures/`
and `versions/`. `core/show/` is Qt-free: owned midis stored relative and referenced audio absolute, **missing paths
preserved** for relinking, loading a show activates its fixture-defs layer, and `.showpack` bundles collect ALL audio,
backings AND guides. `ShowManager` (QtCore-only, **attach FIRST** in `AppCore.__init__`) **aliases**
`config.playlist`/`config.fixtures` onto the show's objects, so every reader mutates the show in place. Persistence
funnels through `save_config()` → `notify_changed()` (saved iff content changed) and `flush()` (~3 s debounced `.mid`
autosave, forced on song switch/close/Save; opening snapshot + at most one automatic snapshot per 5 min).

## Audio and the transport

**ONE stereo QAudioSink feeds every path.** The Qt-free mixer carries backing/click/voice buses with per-bus
volume/mute/RMS and the L/R modes `stereo` | `split_backing_left` | `split_backing_right`. The engine is a **push-mode**
sink, a 15 ms timer topping up a ~120 ms buffer — push on purpose, since a Python pull-QIODevice deadlocks inside
`QAudioSink.stop()` on the GIL. **That sink and its feed live on their own `TimeCriticalPriority` QThread**
(`_FeedWorker`), because the stage blocks the Qt thread for 46–129 ms at a time — measured, inside Qt/GL C++ with no
Python frame above `app.exec()` — which emptied an 80 ms buffer and cost a dropout roughly every 1.2 s of playback. The
GIL is free during those blocks, which is exactly why another thread escapes them. Open and close are **BLOCKING** queued
calls so the old order (open → retune → feed) survives; only the periodic top-up is concurrent, and the sink is created,
written and stopped on that thread and nowhere else. What the device grants is often less than `_BUFFER_SECONDS` asks
(80 ms of 120 here), so the negotiated size is logged in ms and warned about. `load_backing` dedupes an in-flight decode of the same path (restarting QAudioDecoder
mid-decode aborts). **Decoding runs on its own QThread and must**: it costs one `bufferReady` slot call per buffer —
thousands per song **The pipeline rate is the device's, not ours**: `SAMPLE_RATE` (44.1 kHz) is only the *reference* — what the
TTS cache is written at and what the pipeline starts at. Stereo formats are offered to the device best-first and
the first that actually **starts** wins. TTS is piper + a WAV cache: **nothing synthesizes during playback — cache-first always**.
`app/media_controller.py` owns the timeline position (`time.monotonic()` master clock — **never `time.time()`**, which
steps when NTP corrects an SBC that has no RTC; engine slaved with 150 ms drift resync). `monotonic_at()` is that clock's
public face: the metronome phases its grid on the instant a position was TRUE, not on "now", because a seek runs a whole
transport tick in between.

- **An offset moves ONE audio track** (`audio_time = timeline_time - active_offset()`) — clips, song parts and markers
  are anchored in timeline seconds, and the other track has its own offset, so nothing else follows on its own. The drag
  therefore **commits once on mouseRelease** (the only moment Δ is knowable) and `ui/offset_dialog.py` asks what should:
  the show (`MidiManager.shift_all` + `core.markers.shift_markers`, both checked movable before either moves) and, by
  checkbox, the other track — which takes the **correction, never the offset**, since the two files have independent
  lead-ins. Guide drags ask too: the guide is the reference you design against. **A shift crossing t=0 is REFUSED, not
  clamped** (a partial shift looks aligned while sitting the remainder out from the music) — except BLACKOUTs, which
  are gap markers, not content: they never count as the show's start, and one pushed before t=0 is dropped. The dialog disables "Move
  show" and says why, so Advanced can retype the offset without the dialog changing shape. **It quotes only ABSOLUTE
  offsets, `from → to`** — the numbers `show.json` stores. Advanced's beats field is the one relative figure, an INPUT
  counted from the old offset at `bpm_at(old offset)`; it writes the seconds field, which stays the value in force.
  **A drag in progress is a proposal**: the live signal moves
  the SETLIST chip and nothing else (no controller offset, no config, nothing per-frame in the audio path), so Cancel
  reverts by just repositioning the item.
- **Count-in is negative timeline time.** With `song.precount`, playback begins at `-(bars × beats per bar)`; audio/MIDI/lights
  wait for t ≥ 0; pausing resets to 0 and seeking cancels it. AppCore speaks the numbers on the LAST count-in bar when
  `config.audio.count_in_voice` is on.
- **Guide track**: a song may carry a full-mix reference; the BACKING|GUIDE monitor picks which file feeds the engine. It
  is **session-only in-memory state** (always BACKING at launch), so headless/web-remote never inherit it.
  MIDI/DMX/metronome/cues are timeline-driven and identical under either.
- **Backing and guide are equal citizens**: either, both or neither. The timeline spans the **longest** of backing, guide
  and MIDI, declared once per load (`Timeline.set_song_tracks`); the web remote takes the same maximum
  (`webremote.state.song_duration`).
- **The click is SCHEDULED INTO THE OUTPUT, never played by a timer.** Beat times come from the tempo map, convert to
  absolute output frames against one anchor (`_anchor_time` ↔ `_anchor_frame`, taken at start/seek), and are queued up to
  `LOOKAHEAD_S` = 0.5 s ahead of the playhead; `Mixer.schedule()` places each beep at its exact sample. That inversion is
  the whole design: a timer-driven beep lands wherever the Qt thread happened to be free, and this app's stage takes that
  thread away for up to 129 ms, so the beat used to move with the graphics load. A scheduled beep is already in the
  buffer before the stall begins — **the pump's own jitter never reaches a beat**, which is why it may run on the Qt
  thread at all. **WHEN a beat falls is the tempo map's answer, not `60/bpm`**, and the downbeat comes from the beat
  INDEX rather than a free-running counter, so the accent survives a tempo change; a tempo marker needs no restart
  because the queued frames were already computed from the map that carries it. The anchor is taken with
  `MediaController.monotonic_at()`, since a position read plus "now" is wrong by however long the read took — a whole
  transport tick, on a seek. **A beat the playhead has already passed is DROPPED, never crowded in behind it**: what
  survives a stall is the GRID, so a 1.5 s block costs whole beats and moves none. `stop_metronome` un-queues the beats
  placed ahead but lets a beep already sounding finish, or a clean stop becomes a pop. The spoken count-in rides
  `beat_scheduled` (number, timeline time, frame) and is placed at the beat's OWN frame; `beat_occurred` still fires when
  the output *reaches* a beat, which is what keeps the web remote's indicator honest.
  **The click is three layers**: `core/click.py` (Qt-free) says WHAT each beat is — time, number in the bar, sound *kind* —
  and whether the click is on at a position (stop-click); `audio/click.py` (Qt-free) says what each kind SOUNDS like
  at the output rate; `app/metronome.py` says WHEN, and `Metronome.follow()` is the ONE start/seek/retime entry, so
  AppCore never asks the stop-click rule itself. **The window never starts or stops the click** — it once did, after
  AppCore on the same signal, and its bare restart undid both the stop-click and the anchor.
  **Free-run** (the toolbar's ♩▴ click panel, and the web remote's FREE-RUN CLICK card) is the one click that is not the
  song's: the metronome counts a map of its own, and `follow()` — so every play and seek — ends it, as does a transport
  stop; AppCore's retime on `tempo_map_changed` leaves it alone.
- **`audio/diagnostics.py` records the click's own timing** — `TimingMonitor`, armed on play and summarised in one log
  line per pass, is the only reason a glitch at a show is answerable afterwards. The metronome reports every beat's
  scheduling error, the audio feed every top-up and the sink's own underrun verdict (`UnderrunError`/`IdleState` — the
  ONE place a dropout is observable at all). Two constraints: **aggregates, not history** (the feed runs ~66×/s for a
  whole show) and **warnings are rate-limited**, because the thread whose starvation is being reported is the thread that
  would write the line, and on the SBC that line goes to flash. Since the click is scheduled, the health metric is
  **`min lead`** — the smallest margin any beat was queued with — not the beat's error, which is 0 by construction; the
  opening beat is excluded from it, being placed AT the playhead by design. `StallAttributor`
  (`TUPPERDMX_STALL_TRACE=1`, opt-in because its sampler competes for the GIL it measures) names what blocks the Qt
  thread and, from its OWN lateness per wake against Python's 5 ms switch interval, says whether the GIL was held
  (Python — threading cannot help) or free (C++ — it can).

## The marker track (`core/markers.py`, `core/tempo_map.py`, `ui/markers_dialog.py`)

Four things ride a song's timeline that are neither notes nor audio: a spoken **voice cue**, a **tempo change**, a
**stop-click**, and the **segue** out of the song.

- **Voice TTS keeps its own `SongConfig.voice_tts` list.** It is the one kind that pre-renders to PCM off-thread,
  
- **`BpmState` owns the `TempoMap`**, because base tempo + markers together ARE the song's tempo: a map assembled
  independently by the metronome and by the ruler is a map that can disagree with itself. `bpm` stays the BASE tempo.
- **The tempo map is NOT written to the `.mid`.** `midi.codec.apply_tempo` rescales every delta tick to hold each event's
  wall-clock time and replaces *every* `set_tempo` with one value, so extra tempo metas would drag the whole sequence
  with them. 
- **A tempo marker never writes `bpm_state`.** The base BPM is what the `.mid` persists and what the show autosave
  watches
- **Beats are counted continuously across a change** — a marker dropped mid-bar shortens the rest of that bar rather than
  restarting the bar numbering. Negative time extrapolates at the FIRST segment's tempo, which is what lets
  `count_in_duration()` be `-seconds_at_beat(-bars × beats_per_bar)` and land exactly on beat 1 at t=0.
- **A song has ONE meter** (`SongConfig.meter`, in `show.json`, absent = 4/4), carried by the map (`TempoMap.meter`).
  **The BPM ALWAYS counts quarter notes** (the user's call — a first cut counted the written beat and was reversed): a
  map BEAT is one click of the meter's own note, so a /8 beat is half a quarter — 6/8 at 120 is six clicks a bar,
  0.25 s apart, two to each quarter. `bpm_at()`, tempo markers and the `.mid` stay in quarters; the click, grid,
  count-in and bar.beat count beats. The grid's `divisions` keeps its toolbar meaning in any meter (1 = bars, 4 =
  beats, 8 = half-beats).
- **A stop-click's effect outlives its crossing.** `_reset_marker_pointer` deliberately does not replay markers behind
  the playhead, so every seek/resume/start re-asks `click_stopped_at()` instead.
- **The scene's x axis is SECONDS, so a tempo change moves the grid and never a clip.** That is what makes a tempo map
  safe to add to a song that is already programmed. 
- **The dialog's `TimeSpin` holds exact seconds**; bar.beat resolves only to the beat, so a value merely *looked at* in
  bar.beat would come back quantized.

## I/O routing (`config.io` + `core/io_routing.py`)

All routing is explicit in Settings ▸ Input / Output — no toolbar selectors, no localhost magic. Applying `config.io` sets
MIDI routing and drives `DMXManager.apply_io(io)`, which owns the software `DMXController` lifecycle → its output *sinks*
→ the stage-input worker. With the defaults ("All software") the controller runs and feeds the stage **in-process** via
`StageFrameBridge` — no UDP loopback. Cardinalities: MIDI out 1→N (a stateful command protocol — fan-out fine, **fan-in
forbidden**), controller DMX out 1→N (idempotent snapshots), stage DMX in = exactly one selectable feed.

**The one routing decision that can come from outside Settings is which USB dongle sends**: headless has no dialog, so
`--dmx-usb INDEX|auto` (indices from `--list-dmx-usb`, hence that list is **sorted**) pins the controller's USB output
leg. It goes to `Config.override_io()`, NOT into `config.io` — `effective_io()` lays it over the prefs' view at every
`apply_io_config()`, so it survives a live prefs reload and `save()` never writes it. `auto` **owns the leg**: with
nothing detected the USB output is off rather than falling back to a remembered device, while an explicit index that
isn't there exits 2 (a typo, not a missing dongle). The buildroot init script boots with `--dmx-usb auto`.

## The render engine (`stage/`)

**Two stages, in this order — the OFL *category* picks the body and the kind of light it makes; the *capabilities* its
channels declare pick the layers that shape that light.** All 17 categories and all 43 capability types resolve.

- `registry.renderer_for(fdef)` maps a category to a **profile**, never a type name. The fixture's own category order wins
  (OFL lists the most important first), falling back to channel shape (fog ⇒ hazer, pans ⇒ mover).
- `optics.py` — **what light a fixture throws**: `BeamKind` is `NONE` (surface light, no shaft), `WASH` or `HARD`.
  `beam_kind()` decides **whether** there is a shaft, `resting_half_angle()` **how wide** — kept apart on purpose.
  Precedence: a `stage.beam` sidecar hint, then beam-shaping evidence, then the declared lens (spread ≥2.5× is a **zoom
  range**, not a gate), else `beam_fallback`. **Not evidence of hardness**: `Frost`, a bare `WheelSlot`,
  `physical.lens.degreesMinMax`. `test_stage_optics.py` carries the archetype table.
- `profiles/` declares only housing, origins, emission and `beam_fallback`; **it never reads a capability.** `features/`
  are the capability layers mutating a shared `BeamParams`, **discovered once at construction** — a "Moving Head" that is
  really a wash simply has no gobo layer. No per-fixture code anywhere.
- `geometry.Aim`: a fixture is aimed in a **room** (unit vector from pan/tilt), then flattened onto the front elevation,
  so `(axis_deg, foreshorten)` IS that vector and the fog march can rebuild it. **Home (DMX 127/128) is straight down**;
  **tilt swings the beam, pan turns the plane it swings in** — at tilt home *pan cannot move a beam at all*.
  `PanTiltFeature` slews pan/tilt, never the projection.
- **Rigging** (`FixtureInstance.hanging`, persisted): flown = housing upside down + beam at the floor. **Every profile
  draws its chassis as it stands on the deck, into `body.housing`** — the one item a flown fixture turns 180°, **never the
  body item** (the GPU offsets a body's lights by *position* alone); the light's half of the flip is `Aim`'s.
  `add_fixture` *guesses* rigging from where the fixture lands and writes it down.
- `metrics.preview_def` **throws the real scale away**, normalising every body to one size: a **beam is 1220 units and a
  halo caps at 400**. A raster halo is the item's bounding rect — move it through `GlowingEllipse.set_glow_radius` or the
  old halo stays painted.
- `clock.py` is ONE 30 Hz timer for everything that animates, subscribed weakly.

**Atmosphere**: a beam is invisible in clean air, so visibility is the one genuinely cross-fixture quantity.
`StageController` owns one `Atmosphere`; any body resolving a fog output contributes, sources combine without exceeding
full, and the level drifts on the shared clock (~1.5 s attack, ~12 s decay; `Fog` 0.6 vs `Haze` 1.0).
`config.window.stage_haze` goes `set_haze_baseline` → `Atmosphere.settle` (**straight to target, no drift**) →
`refresh_air()`; **both halves are load-bearing**, since a body reads `atmosphere.level` only when it draws.

**`puffs.py` — smoke that outlives its channel.** Parcels carry a birth time, so a machine switched off does not clear the
stage. **Every quantity is derived from a parcel's age, never integrated**: it rises and *stops* rising, disperses as
`sqrt(2·K·age)`, curls with X and Y **decorrelated**. **Both renderers read the same field and the same `PARCEL_*`
shape.** Qt composites `SourceOver`, so parcels **saturate rather than add**.

**`light.py` — the GPU's view of the same frame.** A brush is 8-bit sRGB and two overlapping do not add, so every emission
also describes its light as data: `LightPrimitive`s (POINT / CONE / QUAD / LINE / BLOB) in one growable `array('f')`. They
are **body-local**, colour **linear**, `intensity` **unbounded** — which is how two lights add past full instead of
clipping. Haze is NOT baked in; it reaches the shader as a uniform. `cone_geometry()` is shared by raster cone and light
list. A **LINE is thinner than the pixel that draws it**, so the vertex shader widens its quad to `MIN_LINE_HALF_PX`
device pixels and hands the brightness back.

### `gpu/` — the optional HDR renderer (`config.window.stage_gpu`, off by default)

`StageView.setViewport(QOpenGLWidget())`, light composited in `drawForeground` inside `beginNativePainting()`: primitives
accumulate into an RGBA16F FBO with `glBlendFunc(GL_ONE, GL_ONE)` — light **adds** — then exposure, an ACES tonemap, an
additive composite. `_restore()` puts blend/scissor/viewport back. In GPU mode raster stops drawing **spill** while faces
stay lit; everything fails soft to raster. Four constraints, in `gpu/layer.py`: a `QOpenGLWidget` *parented into* the
viewport renders opaque and hides the scene; the format must `setRenderableType(OpenGL)` or Wayland/EGL hands out GL ES;
it must NOT set a version/profile on X11/Wayland (EGL fails) but MUST on macOS, where Cocoa otherwise hands out
legacy 2.1; draw calls come from `QOpenGLFunctions_4_1_Core`, with 3.3 core as the floor. **`QWidget.grab()`
cannot read back a GL viewport** — screenshots need raster.

**`gpu/fog.py` — volumetric fog** (`config.window.fog.enabled`, off by default, needs `stage_gpu`; after the Settings
dialog `MainWindow` must call `StageController.apply_stage_config()`, which hands back a **new** `StageFogConfig`). It
sits between accumulate and resolve, so fog lands in the HDR buffer *before* the tonemap and is simply more light. Three
passes: a quarter-scale RG16F **medium map** (depth extent stored density-**weighted**), the **march**, a **temporal**
average. The projection is orthographic — no depth buffer needed; cost follows lit area, each light one bounded quad.

- **`STRIDE` stays 16**: a BLOB's `spin`/`variant` ride in the `half_width`/`softness` slots a parcel ignores.
- **Lights and media share ONE instance buffer** (media after lights), so `_upload_instances()` sits outside
  `_accumulate`'s `count == 0` early return — else a fog-only stage segfaults the medium pass.
- **The segment is solved, not swept**: each ray is clipped to the bounding-sphere chord, then the cone or laser tube.
  `FOG_POINT_REACH` is 10 lamp-radii, **not** the sprite's `GLOW_REACH` of 60.
- **The air is ragged and drifts** — two `turbulence()` octaves clamped at zero. `_frame_signature` excludes time;
  `air_is_drifting()` answers instead. **Baseline takes all of `turbulence()`, a parcel 0.4.** **Ambient** lights baseline
  haze at 0.09.
- **Three** fog framebuffers, not two: averaging A into B while sampling B ramps fog to white. `QOpenGLFramebufferObject`
  hands out **GL_NEAREST**, so `_smooth()` must set GL_LINEAR. **An empty light list is not an empty frame**: the guard
  also asks `_air_worth_drawing()`, and `_accumulate` still clears the HDR buffer.
- **The stage clip is OFF for the march** (GL scissors are absolute framebuffer pixels, the clip surface pixels); clipping
  is `_resolve`'s job. The march is dithered, so `request_repaint` asks for `CONVERGE_FRAMES` more frames after the rig
  settles **and then stops** — the only thing here that can spin; reset by `_frame_signature()`, **not** by the march
  running.

**`debug_overlay.py` — the stage's own readout**, unboxed text in the stage image's bottom-right corner, shown ONLY
while logging is at DEBUG. That level IS its switch — re-read every refresh, so Settings ▸ Logging (or an edit to
`userprefs.json` on a rack box) toggles it live and it holds no preference of its own. It answers the two questions the
picture cannot: **what one repaint costs the Qt thread**, which is the number the audio feed lives on, and **whether the
GPU path is really running** — every failure in `gpu/` is soft, so the `draw` line is written only by a layer that is
actually drawing and its ABSENCE is the raster path (which driver, `_initialise` logs once). `StageView`'s frame probe is
installed only while the panel shows, and the panel re-renders only when its text changes, so a still stage stays still.

## Domain layer

- **`core/fixture_state.py`** — the Qt-free photometric model covering **all 43 OFL capability types**. **Emitters** come
  from matrix pixel keys, matrix groups, or — with no matrix — repeated channels (how a multi-cell blinder resolves
  without special-casing). `capability_at(fdef, index, values)` is the ONE "what is this channel doing now"; it takes the
  whole slice, since a switching channel's meaning lives on another slot. `solve_color(fdef, rgb)` **inverts the SAME
  model** the controller's palettes write through.
- **`core/ofl.py`** — the Open Fixture Library (12.5.1) parser. `resolve_mode()` flattens one mode to ordered DMX slots,
  matrix inserts expanded, fine channels linked to their coarse channel, unresolvable slots **kept in place so they still
  consume an address**. A switching channel's `channel` holds the resolution at the trigger's default, so labels stay
  static while `fixture_state` picks the live one per frame. Lenient by design: unknown capability types are preserved,
  not rejected.
- **`core/fixture_library.py`** — a definition is **one or two files plus an optional folder**: `fixtures/<x>.json`
  (strict OFL), an optional `fixtures/<x>.tdmx.json` sidecar for what OFL cannot express (scene `banks`, `chases`,
  `song_parts`, per-channel `label`/`flags`, `stage` hints, the `effects` artwork map), and an optional
  `fixtures/<x>.effects/` folder holding that artwork. **Artwork is resolved per fixture, never by name across all of
  them**: `effect_asset()` reads the sidecar's map against that def's own folder, so two fixtures may each have an
  `01-ON.gif`. Anything travelling with a definition (`.showpack`, the fork into the user layer) carries all three. **What
  a channel *does* is never declared there**; a fixture straight from OFL works with no sidecar. Loaded in three layers —
  shipped, `userprefs_dir()/fixtures/`, the open show's — later overriding by key, memoized, **sidecars never crossing
  layers**. **No summarised channel description, no role enum**: everything reads capabilities. `save_fixture_banks()` is
  the ONE writer: it round-trips the **raw** sidecar JSON so unmodelled keys survive, and **forks the definition into the
  user layer** when the source dir isn't persistable. The OFL document itself is never written. It writes `banks` and
  `chases` **separately** (`save_fixture_chases()` is the chases-only call), because the Scenes and Chases tabs each hold
  their own copy of the block and a write carrying both would put the other's unsaved work on disk.
- **`software_controller/`** — a pure-Python DMX controller (no hardware) managing a 512-channel universe patched from
  `config.fixtures`. `handle_note_on` uses a **generalized note map**: the pitch is a universal *verb*, the MIDI **channel
  selects the fixture** (its index in the patch). Layout: 1–23 channel-offset sets (velocity×2), 24–35 scene loaders,
  36–47 bank select, 48 take-manual / 49 manual / 50 blackout-fixture, 51 blackout-all, 52–59 timing/palette/scene step,
  60 START, 61–69 song parts, 72–75 colour macros. START has no `song_index` — velocity packs the palette (`hue_index =
  velocity // 6`; `mode = PALETTE_MODES[velocity % 6]`).
- **Notes 60 and 71 are per-fixture STATE, not clips.** Both live at t=0, at most one per MIDI channel, in
  `midi_notes` **only** — `describe_note` returns None for either, so the codec keeps them in the `.mid` (they persist,
  they replay) and drops them from `midi_events`, `note_applies_to` refuses every drop, and `shift_all_events` leaves
  them where they are. **Absence IS the default**: velocity 0 is a note_off outside `VALUE_BEARING_NOTES`, so a default
  palette (hue 0, mode 0) and all-flags-off setup have no representation on disk. The palette is edited in
  `ui/fixture/palette_tab.py` — a tab of `AddFixtureDialog` shown only for a **Color Changer**, and one of the dialog's
  answers rather than a library write, so it waits for OK (`apply_edits` in edit mode, `apply_palette(channel)` from the
  designer in add mode, once the channel exists). Song-scoped, since the `.mid` is: the tab edits whichever song is
  loaded. `scripts/migrate_start_note.py` converted existing shows once; **the app carries no migration code**.
- **`webremote/`** — embedded tablet remote, off by default; plain HTTP on the stage LAN, no auth. aiohttp HTTP+WS on its
  own asyncio loop in a QThread; inbound commands execute on the Qt thread, cues/blackout/panic through `preview_clip` so
  `config.io` routing applies. Panic sends note 51 only when `blackout_all_state` is off (blackout is a toggle). The
  manager **polls** two things on its tick rather than trusting signals: the blackout toggle and the free-run's map (a
  retime from the desktop panel emits nothing). The page's free-run tempo/meter is a client-side DRAFT that follows the
  current song until edited and mirrors the running click; it reaches the app only while the click runs, as a retime.

## Configuration & persistence (`core/config.py` + `core/show/`)

- **`config.json`** (shipped, read-only): the *seed* patch for brand-new shows.
- **`userprefs.json`** (user-writable, **MACHINE-level only** — a usable GL driver is a property of the box): `window`
  (incl. `stage_haze`, `stage_gpu`, `stage_exposure`, `ui_scale`, nested `fog`; deliberately no `fog_density`, because
  that IS `stage_haze`), `artnet`, `volume`, `logging`, `io`, `audio`, `webremote`, `last_show_path`/`recent_shows`. Legacy
  `playlist`/`fixtures` are parsed (the show migration adopts them) but never written again.
- **`<Name>.show/show.json`** (the SHOW manifest): the setlist, `current_song_index`, the fixture **patch**,
  `favorites`. A song's `voice_tts`, `control_markers` and `segue`/`segue_time` ride the song entry (see The marker
  track); adding them needed no `format_version` bump, since a manifest without the keys loads with none. A patch entry is type + `dmx_start` + stable MIDI `channel` + optional `mode` (`""` means the type's
  first declared mode, so a renamed mode never strands a patch) + `hanging`, defaulting to **true** so a patch written
  before fixtures declared their rigging keeps throwing light at the floor. Owned `.mid` files live in `midis/`; audio is
  *referenced in place* unless it arrived in a bundle. **The `.mid` stays canonical** — extras plain MIDI can't carry
  (scene `group_id`, `scene_name`, per-clip fades) ride as `tdmx1:{json}` text metas; "Export MIDI…" writes an
  `interop=True` copy without them.

After `ShowManager.attach()`, `config.playlist`/`config.fixtures` ARE the show's objects. **userprefs.json is re-read
while the app runs** — `AppCore` watches it (`app/prefs_watcher.py`), because editing that file over the share is the only
way to change a setting on a headless rack box.

## UI notes

- **1280×800 is the supported minimum display.** Both toolbars are wider than that with everything shown, so each folds
  its low-priority half on a narrow window: the top bar's SHOW card into the ☰ menu, the timeline row's Rows/zoom/Fit zone into a "View ▾" popup.
- **`config.window.ui_scale`** (0.75–1.50, Settings ▸ Interface) is applied by `ui/theme.py` rewriting the theme's pixel values on load **A size set in Python is invisible to all of this
- **MIDI channels are 0-15 everywhere** — the raw MIDI channel, on every surface.
- The fixture browser's footer is the patch decision; OK stays disabled until the patch is legal, and the stage
  inspector refuses the same two (address overlapping/overflowing the universe, MIDI channel already used) one field at
  a time — warn and put the old value back.
- **A DMX clash is rig-wide, so the timeline shows it too**: a lane's address row (`TrackInfo`) goes red via a `state` property.
- `ui/fixture/scenes_tab.py` edits the fixture **library** (a type's sidecar scene banks), not the patch — the dialog's
  OK/Cancel doesn't apply; one button writes. Its tree keeps **bank sections** with scenes beneath, because both orders
  are what MIDI addresses (36+n bank, 24+n scene); reorder is applied to the data, never by letting Qt move the items
  (their roles go stale).
- `ui/fixture/chases_tab.py` is the same editor for a type's `chases`: same list, same "the list writes as it happens,
  the Save button carries only what the editor shows". A step is **(bank NAME, index in that bank)**, so a chase survives
  a bank rename and not a scene reorder.**`CHASE_TIMING_ENABLED` is OFF**, because `update_chase` runs every chase forwards
  on one per-fixture CHASE TIME (note 54): with it off the tab builds no direction/units/fade/hold/level control and Run
  steps at the engine's own `DEFAULT_CHASE_TIME`/`DEFAULT_BLEND_TIME`.
  **SONG PARTS chips are the one control the runtime honours in full** — `song_part_refs` reads the tags off a chase as
  it does off a scene, and an untagged chase is reachable only by note 70.
- **`AddFixtureDialog` has two modes, and `instance=` is the whole difference.**
- **In edit mode nothing is written until OK.** 
- All three
  entry points (a track header, a clip card's menu, the clip library's `+ New`) emit from inside a widget a patch edit
  destroys — `rebuild_for_patch` and the clip library's `_rebuild` both `deleteLater` — and a modal `exec()` is an
  event loop that runs those deletions, so a synchronous open returns into a freed C++ object.
- `ScenePreview` (`ui/fixture/console.py`) **is** the stage engine, fed raw DMX, so a look previews exactly as on stage;
  **`clear()` it before it goes away** — the bodies own timers on the shared clock.
- `ui/fixture/affordances.py` — controls chosen from a fixture's capabilities and **never from its name**.
- `MainWindow._on_fixture_selected` is **gated on edit mode**, not on the emission: `change_fixture_mode` and
  `set_fixture_hanging` also emit `fixture_selected`, and the editor drives both with the stage nowhere near the
  designer.

## The clip model and favourites (`core/clips.py`)

**A clip is derived, never stored**: `build_groups(config)` regenerates every descriptor from the patch plus the fixture
library, so a repatch or a sidecar edit shows up on the next rebuild. It is **Qt-free because three widget-free consumers
need the same answer** — the library panel renders cards, the timeline decodes drop payloads (`CLIP_MIME_TYPE` lives here,
which is what stops `timeline/` importing `ui/`), and the web remote resolves the show's favourites.

- **A favourite stores a symbolic `ref`, never resolved MIDI.** 
- **A per-fixture group carries `fixture`/`mode`; the Show group carries neither** — a bank group is labelled after the
  *bank*, so its label cannot say which fixture it came from, and "this group is about no one fixture" has to be
  answerable rather than inferred (the clip library's `+ New` and its card menus both turn on it).
- **A scene keys on `(bank, name)` with its index as a TIEBREAKER ONLY.** Reorder is a data operation. The index earns its place because names repeat (the shipped ledbar pads FILLS2 with four identical rows).
- **Resolution failure is a display state, never a delete.** 
- Favourites are **show content** (`show.json`'s `favorites`), so there is no machine-level fallback and no prefs seed — a
  new show simply starts with none.

## Timeline and clip editing

- Tracks separate **content items** (cleared on redraw) from **header items** (persistent). Cross-component sync rides
  `MidiManager.data_changed`; scene groups collapse to one `DraggableMidiEvent` via `build_render_units()`.
- Clip drags have three modes (MOVE, RESIZE_LEFT, RESIZE_RIGHT) with neighbour clamping and zoom-aware edge-snap.
  **Persistence happens only on `mouseRelease`** via `*_committed` signals; mid-drag is visual.
- **BLACKOUTs are gap markers**, scoped per fixture and identified by part-name **PREFIX** (case-insensitive), *not* note
  number. `reconcile_blackouts()` forbids zero-duration/adjacent BLACKOUTs and reinserts them to keep clip extents;
  deleting a regular clip converts it to a fixture-matching BLACKOUT, not a hole.
- Scene clips are 7 simultaneous `note_on` events sharing a `group_id`, rendered as one clip; drags route to the scene
  API.
- **Song sections (notes 61-69) are START markers**: each box on the waveform band runs to the next start, so every edit
  moves a BOUNDARY. `timeline/arrangement.py` holds the Qt-free rules — starts never reorder or stack (a beat apart), a
  drop splits the section it lands in or retypes the one starting there. **A lone box's body never moves** (the audio
  under it doesn't); only a 2+ selection drags as a group. **The bar is the section, the waveform below it is the
  audio**: a box spans the whole band (its tooltip, menu and boundary grips reach down it) but IGNORES a plain left
  press below the bar, so the scene hands it to the waveform item, where a drag slides the offset and a CLICK (under
  `CLICK_SLOP_PX` of travel, since Qt moves a movable item on the first pixel) seeks — otherwise a song
  sectioned end to end has nothing left to grab, and the drag is what opens the offset dialog (the band menus' "Set
  audio offset…" is the only other way in). They snap to BEATS (Shift = whole bars, Snap off = none — the tempo map's grid in either
  ruler mode); Alt+←/→ nudges them, since bare arrows seek.
- **The selection is ONE selection across both halves of the scene.** The rubber band, started on empty lane space and
  dragged up into the strip, catches section boxes alongside the clips it crossed, and `timeline/selection.py` then owns
  what has to hold for the pair: Delete removes both, and a group move is **one delta from either grip** — grab a clip or
  grab a box, everything selected travels the same distance, clamped by the INTERSECTION of the clips' room and the
  sections' (both contain 0, so the intersection does). Each grip keeps its own feel for that delta — a box snaps its own
  start to the grid, a clip does not — and the whole thing commits in one `history_batch`, so one Ctrl+Z. Alt+←/→ is the
  keyboard's group move and follows the same rule. What counts as "2+" is therefore `Timeline.selection_size()`, both
  halves, which is also what keeps a LONE box inert.

**A clip is edited against the fixture it lives on, never against a table.** Its note is a *verb*, its MIDI channel
selects the fixture, its velocity is that verb's parameter — so `Timeline._fdef(channel)` resolves the patch entry first
and everything follows from that `FixtureDef`.

- A scene group gets the console; any other clip gets its verb's editor from `verb_editors.py`, off a table whose
  predicates mirror `handle_note_on`'s match arms. **Retyping resets the velocity**: channel-at-200 is velocity 100, which
  carried across would select chase #100. The picker is gated by the same `note_applies_to` drag-and-drop validates with,
  so the dialog can never author a clip a drop would refuse.
- **Only `present | touched` is submitted.** Submitting every fader would silently widen a scene, since an absent channel
  holds the last frame's value while a present one at its default *forces* it. Growth goes through `update_scene(...,
  remove=...)`, which never drops below 2 members.
- **Only a fixture's first 23 slots are reachable** (24 up are scene-load), so `field_note_map` caps there and the fader
  strip greys the tail.
- **Live output is opt-in**, never persisted, 30 ms-throttled, and locked out while the transport plays or the lane is
  muted/locked. Payloads **lead with note 48 TAKE_MANUAL**, or the auto/song-part program overwrites them next frame.
  Cancel re-sends the opening values *once* — a re-send, not a restore.
- **"Save as scene…" writes the fixture library immediately**, independent of OK/Cancel — it edits a type every show
  shares.
- Writes go through `move_event`/`move_scene` for the time (they clamp and keep BLACKOUT markers honest, unlike
  `edit_event(time=...)`), then **`_schedule_refresh()` — never `refresh_from_manager()`**.

## Testing notes

Tests are headless and GL-free (CI's offscreen platform cannot make a context). `ofl_factory.py` builds synthetic OFL
documents so a capability nobody ships a fixture for can still be exercised. `test_capability_coverage.py`,
`test_renderer_registry.py` and `test_stage_optics.py` carry the three tables that fail loudly when OFL gains a capability
type, category or wheel-slot type nobody handled. NB **`[255] * channel_count` is not a "fixture on" frame** — on anything
with a shutter it selects a strobe, so use `_lit_frame()`.

## Common Debugging

```bash
# Measure THIS box's click timing (timer accuracy, click->output latency, card
# clock ppm, stall recovery). Silent; runs on the rack box under plain python3.
uv run python scripts/click_probe.py all

# Name whatever is blocking the Qt thread (audio feed late / underruns). Opt-in:
# the sampler competes for the GIL it measures. Each report says whether the GIL
# was HELD (Python — threading the audio would not help) or free (C++ — it would).
TUPPERDMX_STALL_TRACE=1 uv run python main.py

# Tail today's log (in `logs/` unless userprefs' logging.log_dir names another folder)
tail -f logs/tupperDMX_$(date +%Y%m%d).log

# Enumerate MIDI outputs
python -c "import mido; print(mido.get_output_names())"

# Smoke-test the software controller — it emits NOTHING without sinks, so give it one
uv run python -c "
from software_controller.main import DMXController
from software_controller.artnet_sender import ArtNetSender
import time
c = DMXController(); c.set_sinks([ArtNetSender(ip='127.0.0.1', port=6454)])
c.start_background(); time.sleep(2); c.stop()"
```

Reset user prefs by deleting `userprefs.json` from the platformdirs path. Show data is NOT in prefs — each show is a
self-contained `<Name>.show/` folder with its own `versions/` history.
