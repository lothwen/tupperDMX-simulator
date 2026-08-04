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
uv run python main.py --headless   # no GUI (SBC/rack): web remote is the only interface
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
PyQt6 (GUI + multimedia; **pinned to 6.8 LTS** — 6.11 crashed QMediaPlayer on
pause/resume; playback no longer uses QMediaPlayer, but QAudioSink/QAudioDecoder
still ride the same Qt Multimedia stack), mido/python-rtmidi (MIDI), custom
asyncio Art-Net UDP receiver, librosa (BPM detection), pyserial (USB DMX via
the stock VCP/ftdi_sio drivers), platformdirs (user-config paths), aiohttp
(embedded web-remote server), mutagen (setlist duration probes),
piper-tts + onnxruntime (offline TTS for voice cues + spoken count-in; voice
models ~60 MB download on demand into `userprefs_dir()/tts_voices/`, never
bundled).

## Architecture Overview

### Core Components

- **`app/` — the shared application core (no QtWidgets, never imports `ui/`)**:
  `core.py` (`AppCore`) is the ONE orchestration object — it constructs and
  wires every widget-free collaborator (ShowManager, BpmState, AudioEngine,
  DMXManager, MidiManager, MediaController, AudioController, PlaylistCore,
  WebRemoteManager) plus the Fase-4 logic (spoken count-in, voice-cue TTS
  rendering, `on_song_precount_toggled`/`set_count_in_bars`), and owns
  `preview_clip`, `apply_io_config`/`apply_audio_config`, the transport
  methods, `save_config` and the idempotent `shutdown()` (flush FIRST, then
  webremote→media→audio→engine→dmx→midi). `bpm.py` (`BpmState`: the
  logic-level BPM holder — clamps 40–300, dedupes; widgets are views of it),
  `playlist.py` (`PlaylistCore`: nav + song-load logic, signals
  `song_loaded`/`audio_missing`/`setlist_changed`), `headless.py`
  (`run_headless`: QGuiApplication + offscreen, SIGINT/SIGTERM → shutdown).
  Widget halves ride AppCore signals: `software_controller_changed` (emitted
  AFTER controller seeding), `patch_reloaded`/`show_reloaded`,
  `config_save_failed`, `precount_changed`.
- **`ui/main_window.py` — `MainWindow`**: the desktop view over `AppCore`.
  Builds the widget tree, aliases the core's collaborators onto itself
  (`self.media_controller = core.media_controller`, …) and connects the
  widget halves (dialogs, timeline/toolbar refresh, stage/DMX monitors);
  `closeEvent` → `core.shutdown()`. `main.py` (repo root) is the thin
  launcher: parses `--headless` BEFORE any Qt import (sets
  `QT_QPA_PLATFORM=offscreen`), then runs GUI (QApplication + theme +
  `Config.load()` + `open_or_create_startup_show()` + logging) or
  `app.headless.run_headless(config)`. Headless force-enables the web remote
  in memory when prefs disabled it (never persisted); TTS voices are never
  downloaded headless — provision `userprefs_dir()/tts_voices/` from the
  desktop (Settings ▸ AUDIO ▸ Download) or by copying it over.
- **`core/show/` + `app/show_manager.py` — the Show project layer**: a show
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
  `bundle.py` (`.showpack` zip: collect & save ALL audio — backings AND
  guides into `audio/`, the show's own `audio/` shipped wholesale with its
  filenames reserved against collisions — plus non-shipped defs).
  `ShowManager` (QtCore-only, owned by `AppCore`, attach FIRST in `__init__`)
  **aliases** `config.playlist`/`config.fixtures` onto the show's objects so
  every existing reader mutates the show in place; persistence funnels through
  `AppCore.save_config()` → `notify_changed()` (manifest saved iff its
  serialized content changed) and `flush()` (debounced ~3 s autosave of the
  active song's `.mid`, forced on song switch / close / manual Save; opening
  snapshot + ≤1 automatic snapshot per 5 min of writes).
- **`audio/` — the audio engine (Fase 4 live player)**: ONE stereo QAudioSink
  feeds every audio path. Qt-free `mixer.py` (`Mixer`: backing/click/voice
  buses, per-bus volume/mute/RMS, one-shot clips, and the L/R **routing
  modes** — `stereo` | `split_backing_left` | `split_backing_right`, split =
  backing downmixed mono on one channel, click+voice on the other);
  `engine.py` (`AudioEngine`: **push-mode** sink on the configured output
  device — a 15 ms Qt-thread timer tops up the ~120 ms buffer; push on
  purpose: a Python pull-QIODevice deadlocks/aborts inside QAudioSink.stop(),
  the audio thread blocking on the GIL — Float32 w/ Int16 fallback,
  end-of-backing watchdog; `load_backing` dedupes an in-flight decode of the
  same path — restarting QAudioDecoder mid-decode aborts with "no audio
  data");
  `decode.py` (async QAudioDecoder → int16 (n,2)@44100, whole song in RAM);
  `tts.py` (piper render + WAV cache keyed sha1(voice|text) in
  `userprefs_dir()/tts_cache/`; count-in words per voice language;
  **nothing synthesizes during playback — cache-first always**, and every
  path degrades gracefully to None when piper/voice is missing).
- **`app/media_controller.py` — `MediaController`**: backing playback + MIDI
  sync through the engine. Owns timeline position (the `time.time()` master
  clock; engine slaved with 150 ms drift resync, audio waits while
  `t − offset < 0`); key methods `start_playback`, `play_pause_playback`,
  `stop_playback`, `seek_to_position`. **Count-in = negative timeline time**:
  with `song.precount` on, `start_playback` begins at `-(bars×4 beats)`
  (`set_count_in(enabled, bars)` + `set_bpm`); audio/MIDI/lights wait for
  t ≥ 0; pause during count-in resets to 0, seeking cancels it. Also fires
  the song's **voice cues** (`set_voice_cues([(t, samples)])`, pointer reset
  on stop/seek) onto the voice bus. **Guide track**: each song can carry an
  optional full-mix design reference (`song.guide` + `guide_offset`); the
  editor's monitor (`set_monitor`, `MONITOR_BACKING`/`MONITOR_GUIDE`) picks
  which file feeds the engine via `active_file()`/`active_offset()` — a
  missing guide silently falls back to the backing. The monitor is
  **session-only in-memory state** (always BACKING at launch, never
  persisted), so headless/web-remote/live playback can never inherit it.
- **`app/audio_controller.py` — `AudioController`**: Drift-free metronome
  scheduler. `start_metronome(timeline_position_sec)` aligns beats to
  timeline (floor-based math, so negative count-in positions number
  correctly toward beat 1 at t=0); beeps play via `engine.trigger_click` —
  high pitch on downbeat (1), low on 2/3/4. AppCore speaks the count-in
  numbers ("uno dos tres cuatro", pre-rendered per voice) on the beats of
  the LAST count-in bar when `config.audio.count_in_voice` is on.
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
  scenario designer. Owns one flat `render_objs` list (one render object per
  *body*: a fixture with a pixel matrix contributes one per `bars[]` entry, all
  fed the same DMX slice) and slices `dmx_data` per instance in
  `update_fixtures_with_dmx`. Which class draws a fixture comes from
  `ui/stage/registry.renderer_for(fdef)` — never from its type name.
  `set_design_mode()` makes fixtures movable/selectable;
  `add_fixture`/`change_fixture_mode`/`remove_selected`/drag-to-move mutate
  `config.fixtures` and emit `fixture_added/moved/removed/selected`. The floating
  `ui/fixture_designer.py` panel (Menu ▸ "Design stage") drives it.
  `led_bars`/`blinders`/`generic_fixtures` survive as read-only views over
  `render_objs`.
- **`ui/add_fixture_dialog.py` — `AddFixtureDialog`**: the fixture browser the
  designer's "Add fixture" opens. A left rail (search over name/manufacturer/
  category, a Manufacturer|Category grouping toggle, the tree — a fixture with
  several categories appears under each) over a detail panel rendering the OFL
  document: General (physical / metadata / links / comment cards), Channels
  (one row per DMX slot — address, name, a capability-family chip — expanding
  into its capability ranges), Modes (a radio card per mode with its
  channel chips) and Scenes (below). The footer is the patch decision: DMX start
  (live "occupies A – B" + overlap/overflow check against
  `config.get_used_dmx_channels()`),
  MIDI channel (spin + 16 buttons, taken ones flagged; **0-15, the raw MIDI
  channel** — every surface that shows a channel is 0-based: this dialog, the
  inspector and the timeline lane headers) and a `PatchBar` painting the
  universe. OK stays disabled until the patch is legal.
  `values()` → `(type, dmx_start, channel, mode)`, with `mode` empty for a
  single-mode type. `suggest_free_dmx_start()`/`used_midi_channels()` live here
  too — the inspector validates the same two things. The channel-family table
  (capability type → chip label + colour) lives in `core/channel_families.py`,
  shared with the Scenes tab's faders and the timeline's clip labels;
  `ui/fixture_chips.py` is only the Qt chip widget over it.
- **`ui/fixture_scenes_tab.py` — `FixtureScenesTab`**: the browser's Scenes tab,
  which edits the fixture *library* (a type's sidecar scene banks) rather than
  the patch — so the dialog's OK/Cancel doesn't apply to it. It opens on a blank
  **draft** (`_draft`: a scene in no bank, faders at the channel defaults,
  nothing selected), and the single button beside the name is the only thing
  that writes: **Create** turns the draft into a scene in `_target_bank()` (the
  selected bank, else the first), **Save** overwrites the scene picked from the
  list. A pending change to the banks themselves (delete/reorder/edit → `_dirty`)
  takes the button first — otherwise it could never be written — leaving the
  draft for the press after. Leaving the fixture or closing the dialog with work
  pending asks whether to write it (`save_pending_changes()`). Left: a `_SceneTree` of **bank sections** with
  their scenes beneath — both orders are what MIDI addresses (36+n picks the
  bank, 24+n the scene), so they are never flattened — a colour swatch per scene
  painted by `resolve_state`, `+ New` (scene = back to a blank draft | bank),
  Duplicate/Delete and
  inline rename. Reorder is drag-and-drop, resolved to a
  `move_requested(src_bank, src_index, dst_bank, position)` and applied to the
  data (Qt never moves the items, or their bank/index roles would go stale).
  Right: the scene name, its `song_parts` tags as toggles, and the shared
  **console** from `ui/fixture_console.py` (below). Persistence is
  `core.fixture_library.save_fixture_banks`.
- **`ui/fixture_console.py` — the fixture console**: one fixture's channels as
  faders over a live preview, shared by the Scenes tab and the timeline's clip
  editor so a look is edited the same way wherever you meet it. `FaderStrip`
  (one `ChannelFader` per `fdef.channels[i]`, coloured by `channel_family`;
  `set_addressable_limit` greys the slots past MIDI note 23, `set_present` dims
  the channels a scene doesn't carry; it IS its faders — `len`/iteration/
  indexing all work) and `OutputCard` (the SIMULATED OUTPUT read-out beside
  `ScenePreview`). `ScenePreview` **is** the stage engine —
  `renderer_for(fdef)`, one body per `body_count()`, fed raw DMX — so a look
  previews exactly as it will appear on stage, animations, beam cone and strobe
  included; **`clear()` it before it goes away**, the bodies own timers on the
  shared 30 Hz clock. Plus the value helpers (`default_values`, `scene_values`,
  `values_to_fields`/`fields_to_values`) and `summary_html`.
- **`ui/channel_affordances.py`**: controls that beat a fader for particular
  channels, both chosen from the fixture's own capabilities and never from its
  name. `ColorWheel` writes through `fixture_state.solve_color`, so a hue lands
  on whatever the fixture actually mixes (RGB straight through, a greedy fit for
  CMY/RGBA, nothing for a whites-only blinder — `wheel_applies()` asks
  `solve_color` rather than `color_slots`, which counts Cold/Warm White).
  `CapabilityGrid` turns a channel carrying ≥4 discrete `Effect`/`ColorPreset`/
  `WheelSlot`/`Maintenance` capabilities into a grid of tiles, one per
  capability, valued from its own `dmxRange` — **all animating at once**, so the
  whole menu is on screen and the look is picked by eye. That is what the LED
  bar's animation channel is (35 `Effect` capabilities at [0,3] [4,7] … plus a
  `NoFunction` tail, exactly the `index*4` the old hardcoded grid re-derived).
  `assets/animations/*.gif` is now optional artwork matched to a capability by
  its `effectName`, so it can never appear on a fixture that didn't declare
  those effects. `affordance_for(fdef, index)` is the one lookup.
- **`ui/stage/` — the render engine**: **two stages, in this order — the OFL
  *category* picks the body and the kind of light it makes; the *capabilities*
  its channels declare pick the layers that shape that light.** All 17 OFL
  categories and all 43 capability types resolve; see `tests/
  test_capability_coverage.py` and `test_renderer_registry.py`, whose tables
  fail loudly if either gains an entry nobody handled.
  - `registry.py` maps a category to a **profile** via `@register_renderer(...)`;
    the fixture's own category order wins (OFL lists the most important first),
    falling back to the shape of its channels — a fixture that makes fog is a
    hazer, one that pans is a mover — and finally to `GenericStageFixture`
    ("Other" is the only category with no profile of its own).
  - `optics.py` — **what kind of light a fixture throws**, as a three-valued
    `BeamKind`: `NONE` (a surface light — a panel, a flood, a strip: no shaft in
    the air), `WASH` (a soft cone) or `HARD` (a defined edge you can see the end
    of). Qt-free and may import only from `core`, which keeps it below the
    `registry`↔`profiles` cycle. `beam_kind(fdef, fallback)` decides **whether**
    there is a shaft; `resting_half_angle(fdef, kind)` decides **how wide** —
    kept apart on purpose, because conflating them is what drew an ARRI SkyPanel
    with a 57.5° beam. Rule order: a `stage.beam` sidecar hint wins outright;
    then beam-shaping evidence (`Prism`/`Iris`/`Blade…`/`Focus`, or a wheel
    carrying a `Gobo`/`Prism`/`Iris` **slot**) ⇒ HARD; then the declared lens —
    narrow even wide open (≤ `HARD_MAX_DEGREES` 12°) ⇒ HARD, wider than
    `WASH_MAX_DEGREES` (65°) ⇒ NONE, else WASH, with a spread ≥
    `ZOOM_RANGE_RATIO` (2.5×) read as a **zoom range** whose tight end is the
    zoom working rather than a gate; then a bare `Zoom`/`BeamAngle` channel ⇒
    WASH; else the profile's `beam_fallback`. **What is deliberately NOT
    evidence matters as much**: `Frost` softens an edge (and rides on wash
    heads), and a bare `WheelSlot` is whatever its slots are — a colour wheel
    declares it exactly as a gobo wheel does. Reading either as hardness is what
    drew 32 wash movers as spots. Note OFL's `physical.lens.degreesMinMax` is the
    *beam angle*, which every emitting fixture has; its mere presence never meant
    "throws a beam". `tests/test_stage_optics.py` carries the archetype table and
    fails loudly if OFL gains a wheel-slot type nobody classified.
  - `profiles/` — the body and its chassis animation, one class per kind:
    `lamp.py` (Color Changer / Dimmer / the generic fallback), `array.py`
    (Pixel Bar / Matrix, Blinder, Strobe), `motorised.py` (Moving Head, Scanner /
    Barrel Scanner, Flower / Effect, Laser), `atmospheric.py` (Hazer / Smoke,
    Fan, Stand). A profile declares only its housing, `aim_origin()`,
    `emission_for(fdef)`, `paint_housing()` and `beam_fallback` — what its
    fixtures throw when the definition declares no optics at all, since the
    document has no answer and the body shape is the only evidence left. A lamp
    body stays `NONE` (an unlensed lamp is likelier a panel than a spot); a
    moving head falls back to `WASH` and a scanner to `HARD`, because a fixture
    that aims a beam nobody drew is just a moving box. It never reads a
    capability.
  - `emission/` — how the light is drawn: `cone.py` (`WashLamp`, `LampGlow` —
    the same lamps with no cone ever built, so a stray zoom channel cannot
    conjure one — `HardBeam` with its gobo plane and prism copies, `BeamFan`),
    `surface.py` (`MatrixCells`, `BulbArray`, `FlatPanel`), `volumetric.py`
    (`LaserFan`, `FogPlume`), `NoEmission`. `emission_for_kind(kind)` is the join
    from `optics.py` to these classes, used by every profile whose body is a lamp
    in a housing; the ones that are a shape of their own (a flower's rosette, a
    laser's fan, a bar's cells) name their emission directly. `emission_reach(fdef)`
    answers "how far does this fixture's light get" for the scene preview's
    viewport, and is now a **per-class constant** — by the time an emission has
    been picked the question is already settled, which is what picking `WashLamp`
    over `LampGlow` decided.
    An emission that draws a face draws **one item per zone**, from the state's
    emitters: `MatrixCells` a cell per pixel, `BulbArray` a bulb per emitter and
    `WashLamp` a lamp per `cell_count()` — so a wash whose face is segmented
    (the Wild Wash Pro's four) shows every segment while staying a wash, its
    cone taking their mix. One zone is the overwhelming case and draws exactly
    as a single lamp always did.
  - `features/` — the capability layers, each naming the OFL types it consumes
    and mutating the shared `BeamParams`: dimmer, colour, colour wheel, shutter
    (all ten `shutterEffect` waveforms), zoom, iris, frost, gobo, prism, blades,
    pan/tilt. **Discovered once at construction** from
    `all_capability_types(fdef)`, so a "Moving Head" that is really a wash with
    no gobo wheel simply has no gobo layer — graceful degradation with no
    per-fixture code anywhere.
  - `frame.py` (`Frame` + `BeamParams`, the contract between the two stages;
    `half_angle_deg` is the fixture's **resting** spread from
    `optics.resting_half_angle` — the mid-point of a fixed lens, the *geometric*
    mean of a zoom range, `None` only when the kind is `NONE` — and `ZoomFeature`
    still overwrites it every frame, so a live zoom always wins),
    `geometry.py` (`Aim`: pan/tilt onto a flat front elevation — pan swings the
    beam across the picture, tilt rotates and foreshortens it; **a fixture with
    no pan/tilt comes out straight down at full length**, exactly as before —
    plus `Spinner` and `Slew`), `atmosphere.py` (the stage-wide haze level; see
    below), `clock.py` (ONE 30 Hz timer for everything that animates, subscribed
    weakly so a body that leaves without cleanup stops ticking), `wheelart.py`
    (procedural gobo/gel/prism art from the slot's own OFL description — no
    vendored SVGs), `base.py` (`FixtureLabel`, `DesignableFixture`,
    `GlowingEllipse`, the mm→px helpers `body_size`/`body_count`/`body_spacing`
    anchored by `MM_PER_STAGE_PX`, and `BodyFixture` — which runs the pipeline:
    reset the beam → each feature shapes it → the emission draws it → the
    profile repaints its chassis). `beam.py`/`pixelbar.py`/`blinder.py`/
    `wash.py`/`strobe.py`/`generic.py` remain as shims re-exporting the names
    that moved.
  - **Atmosphere**: a beam is invisible in clean air, so beam visibility is the
    one genuinely cross-fixture quantity — a hazer downstage lights up a mover's
    beam across the rig. `StageController` owns one `Atmosphere`; any body whose
    capabilities resolve a fog output contributes to it (not just the Hazer
    profile — plenty of movers have one built in), sources combine without ever
    exceeding full, and the level drifts on the shared clock with a ~1.5 s
    attack and ~12 s decay because haze hangs in a room. The manual baseline is
    `config.window.stage_haze`, edited by the HAZE slider on the edit toolbar.
  - `FixtureInstance.multiply_factor` (the blinder's overdriven-glare look) is
    still applied by exactly one profile, via `BodyFixture.overdrive()`.
  - **`light.py` — the third stage, and the GPU's view of the same frame.** A
    brush is 8-bit sRGB: it cannot hold "twice as bright as white", and two of
    them overlapping do not add. Both are things a rig does, so every emission
    also describes its light as data. `Emission.emit_lights(frame, out)` (a
    fifth hook beside `build`/`render`/`cleanup`/`reach`, no-op by default)
    appends `LightPrimitive`s — POINT / CONE / QUAD / LINE / BLOB — to a
    `LightList`: one growable `array('f')`, 16 floats per primitive, cleared by
    resetting a write index, for the same reason `BeamParams` is reused.
    Coordinates are **body-local** (the layer adds the body's scene position
    while packing, so dragging a fixture needs a repaint and not a re-render),
    colour is **linear** and `intensity` is **unbounded** — which is where
    `multiply_factor` finally survives instead of being clipped twice on one
    line. Haze is deliberately NOT baked in: it is stage-wide and reaches the
    shader as a uniform, so the list stays a statement about the fixture.
    `cone_geometry()` in `emission/cone.py` is shared by the raster cone and
    the light list so the two views of one beam cannot drift.
  - **`gpu/` — the optional HDR renderer** (`config.window.stage_gpu`, off by
    default; Settings ▸ Logging). `StageView.setViewport(QOpenGLWidget())` and
    the light composited in `drawForeground` inside `beginNativePainting()`:
    every primitive accumulates into an RGBA16F FBO with
    `glBlendFunc(GL_ONE, GL_ONE)` — light **adds** — then exposure
    (`config.window.stage_exposure`), an ACES filmic tonemap (Stephen Hill's
    fitted RRT+ODT) and an additive composite over the scene. Both passes are
    `glScissor`ed to the stage image, and `_restore()` puts blend/scissor/
    viewport back, because `endNativePainting` only restores what Qt itself
    tracks. In GPU mode `set_raster_light_visible(False)` stops the raster
    drawing **spill** (halos, cones, bloom) while faces stay lit — the face is
    the fixture, the halo is what it emits. Everything fails soft to raster and
    logs once. Four hard-won constraints, all in `gpu/layer.py`'s docstrings: a
    `QOpenGLWidget` *parented into* the viewport renders opaque and hides the
    scene (no attribute rescues it); the surface format must
    `setRenderableType(OpenGL)` or Wayland/EGL hands out GL ES where nothing
    compiles; it must NOT set a version/profile or EGL fails outright; and the
    draw calls come from `QOpenGLFunctions_4_1_Core` (PyOpenGL is not a
    substitute). `ScenePreview` attaches the same layer, gated on the stage's
    GL having *succeeded* — which also keeps every offscreen/headless caller on
    raster, since nothing sets `STAGE_GPU_ACTIVE` there. Note `QWidget.grab()`
    cannot read back a GL viewport: scripted stage screenshots need raster.
- **`core/fixture_state.py`**: the Qt-free photometric model, covering **all 43
  OFL capability types** through a dispatch table (`_HANDLERS`) rather than a
  branch chain — "what does this app do with capability X" is one dict lookup.
  `resolve_state(fdef, dmx) -> FixtureState`, grouped into frozen records:
  `color` (rgb, temperature, per-cell `emitters`, wheel gel), `optics` (zoom,
  focus, iris, frost, prism facets/spin, blades, beam offset), `motion` (pan,
  tilt, continuous spins, move speed, rotation), `strobe` (all ten shutter
  effects + rate), `program` (the fixture's own effect + speed/parameter/sound),
  `atmosphere` (fog output and kind) and `wheels` (which slot is in the beam,
  and how far the wheel has turned between two). **The flat names the rest of
  the app reads — `rgb`, `emitters`, `shutter`, `effect`, `zoom_deg`,
  `beam_deg`, … — survive as read-only views onto those groups**, so
  `note_layout`, the timeline swatches, the clip library and `solve_color` need
  no changes. Colours mix additively from `REFERENCE_RGB` (the 13 OFL
  `ColorIntensity` colours). **Emitters** come from matrix pixel keys, from
  matrix *groups*, or — with no matrix — from repeated channels: the n-th
  channel of a colour (or the n-th plain dimmer) is the n-th cell, which is how
  a multi-cell blinder resolves without special-casing.
  `capability_at(fdef, index, values)` is the ONE public "what is this channel
  doing right now" — it takes the whole slice on purpose, because a switching
  channel's meaning lives on another slot, so a per-channel read describes the
  wrong channel. The Scenes tab's read-outs go through it too.
  `solve_color(fdef, rgb)` is the **inverse** — light in, DMX out — that the
  controller's palette/colour macros write through: R/G/B straight through where
  the fixture mixes them (zeroing its other emitters so a stale amber can't tint
  the result), a greedy fit onto whatever it does mix otherwise (CMY, RGBA
  without blue), and `{}` for a whites-only fixture like the blinder, which has
  no hue to set. Inverting the SAME additive `REFERENCE_RGB` model means what
  the controller writes is what the stage draws.
- **`core/ofl.py`**: the Open Fixture Library (schema 12.5.1) parser. Document →
  `OflFixture`; `resolve_mode()` flattens one mode to ordered DMX slots with
  matrix inserts expanded (all 7 `repeatFor` orders × `perPixel`/`perChannel`;
  for `eachPixelXYZ` **X varies fastest**), fine channels linked to their coarse
  channel, and unused/unresolvable slots kept in place so they still consume an
  address. `$pixelKey` expands over **pixels and pixel groups alike** — a mode
  addressing `Red Master` or `Red 1/2` is as normal as one addressing `Red 1`,
  and group channels claim no `pixel_key` since a group drives several cells —
  they name it in `pixel_group` instead, so a renderer can still find the cells
  a master channel commands.
  **Switching channels** (a capability's `switchChannels`) resolve too: the slot
  carries `switch_options` plus `switch_of`, the slot index of the trigger
  channel that selects between them (trigger may be patched later in the mode),
  while `channel` holds the resolution at the trigger's default so labels
  and the clip library stay static. `core/fixture_state.py` picks the live one
  per frame — the same address really is a strobe in one program and an effect
  speed in another. **Wheels** are parsed too (`wheels` → `Wheel`/`WheelSlot`,
  raw-prop passthrough like `Capability`): a `WheelSlot` capability says which
  slot is in the beam, and this says what that slot IS — a gel colour, a gobo
  resource, a prism's facet count. `slot_at(wheel, number)` resolves a 1-based
  (possibly fractional) slot number, wrapping, and returns the *pair* either
  side when the wheel is mid-change so the renderer can crossfade;
  `wheel_names(cap, channel)` handles OFL's three forms (a name, an array, or
  absent — meaning the wheel named after the channel). `parse_entity()` turns
  OFL's units and keywords ("fast", "50%", "6500K", "540deg") into canonical
  floats across all 17 entity kinds; `"angle"` is kept as an alias of the real
  `rotationAngle`, which backs nine capability types. Deliberately lenient —
  unknown capability types are preserved, not rejected. The document's own metadata (`meta` →
  `OflMeta` authors/dates/import plugin, `comment`, `links`, `rdm`, plus
  `powerConnectors`/`DMXconnector`/bulb lumens on `OflPhysical`) is parsed but
  never interpreted — nothing in the render path reads it; it exists for the
  fixture browser.
- **`core/fixture_library.py`**: the fixture-type *library* and the OFL→app
  bridge. A definition is **one or two files**: `fixtures/<x>.json` (a strict OFL
  document) plus an optional `fixtures/<x>.tdmx.json` sidecar
  (`core/fixture_sidecar.py`) carrying what OFL has nowhere to put — scene
  `banks`, `chases`, `song_parts` tags, per-channel `label`/`flags` overrides,
  the effect→animation map and `stage` render hints (five keys, all optional:
  `size_px`, `bodies`, `body_spacing`, `cells`, and `beam` —
  `"none"|"wash"|"hard"`, which overrides `ui/stage/optics.beam_kind()` for the
  fixture whose gate is not a DMX channel and so cannot be read from the data,
  an ETC Source Four's lens tube being the standing example. Mode blocks merge
  `channels` per key but replace `stage` **wholesale**, so a named mode's block
  must repeat every key it still wants from `"*"`). What a channel *does* is
  never declared there — that is what its capabilities say. A fixture taken straight from
  the OFL project works with no sidecar; it simply has no scene banks.
  Loaded in three layers — shipped `fixtures/`, writable
  `userprefs_dir()/fixtures/`, and the open show's `fixtures/`
  (`set_show_fixtures_dir()`); later layers override by key; memoized;
  **sidecars never cross layers** (a sidecar only pairs with the OFL document
  in its own folder). Each layer is scanned **recursively**, and a subfolder is
  the **manufacturer**: `fixtures/ADJ/focus-spot-4z.json` registers as
  `"ADJ/Focus Spot 4Z"` (`FixtureDef.manufacturer`), so two makers can ship a
  fixture of the same name without one silently replacing the other. A file
  directly in a fixtures dir keeps its **bare** key — that is what every saved
  patch and the shipped ledbar/blinder refer to. `FixtureDef` is one type
  resolved to **one mode**: `resolve_fixture(type, mode)` is the canonical
  accessor (`load_fixture_defs()` gives each type at its default mode),
  `fixture_modes(type)` lists the choices and `fixture_document(type)` returns
  the mode-independent OFL document behind it. The bridge adds only what OFL
  cannot express: `blend` is derived from the capability kind, and the knob
  flags — which aren't derivable — come from the sidecar. **A channel has no
  second, summarised description** (there is no role enum): what it does is its
  capabilities, and everything reads them through `dominant_capability()` (the
  type covering the most DMX wins, `NoFunction` never counts),
  `color_slots(fdef)` (OFL colour name → slot index, so amber and CMY are as
  addressable as red) and `capability_types(fdef)`.
  `all_capability_types(fdef)` is the "does this fixture do X *anywhere*"
  question the render engine's feature discovery and category fallback ask —
  every capability on every slot including switching alternates, as against
  `capability_types()`'s dominant-only summary. `color_sources(fdef)` names how
  a fixture can be given a colour (`mix`/`preset`/`temperature`/`wheel`);
  `color_slots()` deliberately stays mix-only, because it is the *write*
  direction feeding `solve_color` and a wheel position is not a colour level.
  `config.fixture_channel_count(type, mode)`/`known_fixture_types()` derive from
  it; `software_controller.fixture_type_for(name, mode)` compiles types lazily,
  tracking `fixture_library.cache_generation()` so a show opened later rebuilds
  them. `save_fixture_banks(type, mode, banks[, chases])` is the ONE writer (the
  Scenes tab): it round-trips the **raw** sidecar JSON so unmodelled keys and
  other modes' blocks survive, picks the mode block via
  `fixture_sidecar.banks_mode_key()` (whoever already supplies the banks wins,
  else `"*"` for a one-mode type and the named mode otherwise), and **forks the
  definition into the user layer** when the source dir isn't persistable — a
  frozen build's shipped `fixtures/` lives under `_MEIPASS`, which is writable
  and deleted on exit, so `_is_persistable()` treats the bundle as read-only
  whatever the mode bits say. The fork copies the OFL document too (a sidecar
  never pairs across layers) under the same manufacturer folder, so the key is
  unchanged and the user layer simply overrides the shipped def. Saving
  `clear_cache()`s, so new scenes are live without a restart. The OFL document
  is never written: a type's DMX footprint, and every note binding resting on
  it, is untouchable from the editor.
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
  Colour verbs go through `fixture_state.solve_color()`, so a palette or a macro
  drives whatever emitters the fixture actually has (an RGBA par's amber is
  zeroed rather than left over from the last scene, a CMY mover is fitted, a
  whites-only blinder is left alone). A scene hands its colour to the live
  palette by setting the fixture's Red/Green/Blue channels to the 255 sentinel.
  The 20 Hz output loop fans each universe snapshot to pluggable *sinks*
  (`set_sinks()`; `ArtNetSender` UDP, `USBDMXSender` FTDI DMX TX, or the Qt
  `StageFrameBridge`); each type's scene banks/chases live in its
  `fixtures/*.json` def. (Saved `.mid` from the old positional layout are
  upgraded by `scripts/migrate_midi_v2.py`.)
- **`webremote/`**: Embedded tablet remote (Settings ▸ WEB REMOTE, off by
  default; plain HTTP on the stage LAN, no auth; headless mode force-enables
  it in memory). `WebRemoteWorker`
  (`server.py`) runs an aiohttp HTTP+WS app on its own asyncio loop in a
  QThread (ArtNetWorker pattern); `WebRemoteManager` (`manager.py`, owned by
  `AppCore`) subscribes to existing signals, publishes full-state snapshots
  plus 10 Hz `pos` / per-beat `beat` events, and executes inbound commands on
  the Qt thread — transport via AppCore/PlaylistCore, cues/blackout/
  panic via `preview_clip` (so `config.io` routing applies). Blackout is a
  toggle: panic stops playback and sends note 51 only when
  `blackout_all_state` is off. Qt-free logic in `state.py` (snapshot schema,
  section spans, mtime-cached duration/BPM probes), `commands.py` (action
  whitelist/parse) and `peaks.py` (waveform bins for the canvas). The
  no-build page lives in `webremote/static/` and is responsive: a CSS-grid
  (`grid-template-areas`) reflows the same blocks into a 3-column tablet
  layout, a portrait-tablet rail layout, and a single-column phone layout
  (≤600px) where the body scrolls under a sticky header + fixed transport,
  the setlist becomes a "+"-triggered drawer, and a slim seek bar replaces
  waveform-tap. The Fase-4 widgets are live: KEY chip (per-song `key`),
  UP NEXT count-in pill (next song's precount / "SEGUE"), the AUTO COUNT-IN
  card (toggle = current song `precount`, stepper = global bars → WS commands
  `set_precount` / `set_countin_bars`), CLICK/BACKING meters fed by real bus
  RMS (`levels` on the 10 Hz `pos` event), and a countdown treatment while
  `position < 0` (count-in).

### Configuration & persistence (`core/config.py` + `core/show/`)

Storage splits across three places:
- **`config.json`** (shipped, read-only): `fixtures` (the *seed* patch for
  brand-new shows), bundled via PyInstaller, resolved at runtime by
  `shipped_config_path()` (`_MEIPASS`).
- **`userprefs.json`** (user-writable, MACHINE-level only): `window` — incl.
  the stage's `stage_haze`/`stage_gpu`/`stage_exposure`, machine-level because
  whether there is a usable GL driver is a property of the box — `artnet`
  — the controller's Art-Net *output* target — `volume` (audio/metronome/voice
  buses), `logging`, `io` — the explicit I/O routing, `IOConfig` — `audio`
  (`AudioConfig`: output device id, click/voice channel routing mode, global
  `count_in_bars`, `count_in_voice`, `tts_voice`), `webremote` (server
  enable/port + the default cue set that seeds NEW shows) and
  `last_show_path`/`recent_shows`.
  Resolved by `userprefs_path()` via `platformdirs` (e.g. `~/.config/tupperDMX/`
  on Linux). Written atomically (temp + `os.replace`). Legacy
  `last_dmx_source`/`last_midi_port` keys migrate one-time into `io`; legacy
  `playlist`/`fixtures` keys are still *parsed* (they're the source the
  one-time show migration adopts) but never written again.
- **`<Name>.show/show.json`** (the SHOW manifest, per project): the setlist
  (`songs` with `id`/`title`/`key`/`precount`/`voice_cues` — the per-song
  spoken-guide list `[{time, text}]` — /`offset`/`guide`+`guide_offset` —
  the optional full-mix design reference, referenced in place like the
  backing — /paths + a derived
  `cache` of duration/bpm), `current_song_index`, the fixture **patch**
  (`fixtures` — each entry a type + `dmx_start` + stable MIDI `channel` +
  optional `mode`, where `""` means the type's first declared mode so a mode
  renamed in a definition never strands a saved patch) and `webremote_cues`. Owned `.mid` files live in `midis/` and
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

- `ToolbarManager` (`ui/toolbar_manager.py`): SHOW section
  (header; + New/📂 Open/💾 Save/Save as… buttons,
  a "…" overflow menu with Import bundle…, and the blue split ⤓ Export
  QToolButton — main click exports the full .showpack, the ▾ menu offers
  interop Export MIDI… (`export_midi_action`, gated by
  `set_export_midi_enabled`) and Export bundle; footer with show name +
  setlist/patch stats), SETLIST
  section (header "N songs"; a row of prev / an editable song-selector
  QComboBox — a "05/11" counter badge + the full setlist; activated() picks a
  song via `song_selected`, edit+Enter renames via `song_title_edited` / next /
  remove 🗑; footer BPM / offset / derived "linked" media chip + per-song KEY
  line-edit (`song_key_edited`) and 🔔 count-in toggle
  (`song_precount_toggled`)), SONG FILES
  section (header with the live autosave chip — the ~3 s .mid debounce;
  three accent-coloured cards 🎹 MIDI / 🎵 BACKING TRACK / 🎧 GUIDE, each a
  🗁 load button over a right-anchored path well (overflow clips on the
  left, keeping the filename end visible) that swaps for a dashed
  "+ Load …" action while empty, plus the ▷ new-MIDI and ✕ guide-clear
  (`guide_clear_requested` → `MainWindow.clear_guide_track`) header
  actions), VERSIONS section (header; a snapshot-picker
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
  **Input / Output** page. An **AUDIO card** (between WEB REMOTE and Logging)
  edits `config.audio`: output-device combo (QMediaDevices), the
  stereo/split routing selector, count-in bars + spoken-count toggle, and the
  TTS voice picker with Download (background thread) and Test buttons.
  Left: three numbered routing cards editing `config.io`
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
  (`ledbar`, `blinder`). Header panel has MASTER / CLICK / VOICE volume rows
  (each → its engine bus); the ruler draws 🗣 voice-cue markers
  (`set_voice_cues`), display-only — editing lives in the timeline toolbar's
  🗣 Cues button → `ui/voice_cues_dialog.py` (per-song table editor with
  TTS preview). The timeline toolbar (`ui/timeline_toolbar.py`) also hosts
  the segmented `BACKING | GUIDE` monitor toggle (amber when GUIDE is
  active, disabled without a guide) → `MediaController.set_monitor`: the
  waveform draws the monitored track, and dragging it writes that track's
  offset (`offset` vs `guide_offset`) via
  `MainWindow.on_waveform_offset_changed`.
- `ClipLibraryWidget` (`ui/clip_library.py`): right sidebar tabs ("Library",
  "DMX Channels"). One group per patched fixture (its channel verbs, blackout
  and colour macros, all named by `describe_note(note, fdef)`), one per scene
  bank (`scene_loader.load_scenes(type, mode)`) and one for its chases, plus a
  global "Show" group (START / song parts / blackout-all). Click previews; drag
  drops onto the timeline. A scene's preview payload **leads with note 48
  TAKE_MANUAL**, or the auto/song-part program overwrites it on the next frame.
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
- The BACKING|GUIDE monitor swaps which file the engine decodes (one track
  in RAM at a time; switching mid-play just drops out until the new decode
  lands and the drift resync repositions it). `guide_offset` plays the same
  timeline-alignment role for the guide that `offset` plays for the backing.
  MIDI/DMX/metronome/cues are timeline-driven and identical under either.

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

- `main.py` (repo root, the only root source file) — thin entry point
  (`--headless` parsed before any Qt import); the window lives in
  `ui/main_window.py`, the headless runner in `app/headless.py`.
- `app/` — the shared application core (QtCore/QtMultimedia only — NEVER
  QtWidgets or `ui/`; no eager re-exports): `core.py` (`AppCore`), `bpm.py`
  (`BpmState`), `playlist.py` (`PlaylistCore`), `media_controller.py`,
  `audio_controller.py`, `show_manager.py` (`ShowManager`: aliasing,
  autosave, open/restore), `headless.py` (`run_headless`).
- `core/` — Qt-free shared domain layer (no eager re-exports; import
  submodules directly): `config.py` (dataclass config — see Configuration &
  persistence), `show/` (the show-project package: `model.py`, `manifest.py`,
  `store.py`, `migrate.py`, `versions.py`, `relink.py`, `bundle.py`),
  `paths.py` (single source of `bundle_dir()`/`asset_path()`/
  `userprefs_dir()`/`default_shows_dir()`, dev vs. PyInstaller `_MEIPASS`),
  `ofl.py` (the Open Fixture Library 12.5.1 parser: capabilities, modes,
  matrix/fine-channel expansion, unit/entity parsing), `fixture_sidecar.py`
  (the `.tdmx.json` sidecar: banks, chases, label/flag overrides, effect assets,
  stage hints — plus the raw-JSON writer `banks_mode_key()`/`apply_banks()`/
  `write_sidecar()` the scene editor round-trips through),
  `fixture_library.py` (the 3-layer loader + the OFL bridge: `FixtureDef`,
  `resolve_fixture()`, `fixture_modes()`, `save_fixture_banks()`, the
  capability lookups `dominant_capability()`/`color_slots()`/
  `capability_types()`, plus the
  per-type presentation colours `GROUP_COLORS`/`color_for_type()`),
  `channel_families.py` (`channel_family()`/`describe_channel()`: what a channel
  is, in one word and one colour, from its capabilities — the browser's chips,
  the Scenes tab's faders and the timeline's clip labels all read it),
  `fixture_state.py` (`resolve_state()` → `FixtureState`: the Qt-free
  photometric model the stage renderers consume — all 43 OFL capability types,
  grouped into `color`/`optics`/`motion`/`strobe`/`program`/`atmosphere`/
  `wheels` with the old flat names kept as views — plus its inverse
  `solve_color()`),
  `note_layout.py` (note → label/colour lookup; channel-control verbs name
  themselves via `channel_families` and scene swatches via `resolve_state`),
  `io_routing.py` (pure routing logic: `MidiRouting`, presets, `validate()`,
  `flow_text()`, and `flow_state()` → `FlowState`, the render-ready
  signal-flow graph state), `buildinfo.py` (build metadata accessor).
- `fixtures/` (repo root) — shipped fixture-type definitions: a strict OFL
  `*.json` per type plus its optional `*.tdmx.json` sidecar. A **subfolder is a
  manufacturer** and namespaces the type key (`ADJ/Focus Spot 4Z`); files at the
  root keep their bare key and show as "Generic" in the browser.
- `audio/` — the audio engine package (no eager re-exports): `mixer.py`
  (Qt-free three-bus stereo mixer + routing modes + beep synthesis),
  `engine.py` (`AudioEngine`: the push-mode QAudioSink bridge on the
  configured output device), `decode.py` (async QAudioDecoder → PCM),
  `tts.py` (piper voices/download, sha1-keyed WAV render cache, count-in
  words).
- `ui/` — `main_window.py` (`MainWindow`, the desktop view over `AppCore`),
  `playlist_controller.py` (widget half of the playlist: timeline/toolbar
  refresh + relink dialogs — guides relink silently, no dialog — over
  `app.playlist.PlaylistCore`),
  `stage_controller.py` (also owns the stage's `Atmosphere` and
  `sync_from_config()`), `stage/` (the two-stage render engine: `base.py` —
  `DesignableFixture` design-mode move/select + drag-release write-back,
  `FixtureLabel`, `GlowingEllipse`, geometry helpers and the `BodyFixture`
  pipeline — `registry.py`, `frame.py`, `geometry.py`, `clock.py`,
  `atmosphere.py`, `wheelart.py`, `profiles/` (`lamp.py`, `array.py`,
  `motorised.py`, `atmospheric.py`), `emission/` (`cone.py`, `surface.py`,
  `volumetric.py`), `features/` (`level.py`, `beamshape.py`, `wheels.py`), and
  `beam.py`/`pixelbar.py`/`blinder.py`/`wash.py`/`strobe.py`/`generic.py` as
  shims for the names that moved),
  `fixture_designer.py` (edit-mode toolbar + the per-selection inspector,
  incl. the mode combo shown only for multi-mode types),
  `add_fixture_dialog.py` (the fixture browser + patch footer),
  `fixture_scenes_tab.py` (its Scenes tab: bank/scene list over the shared
  console, written to the `.tdmx.json` sidecar by its own Create/Save button),
  `fixture_console.py` (`FaderStrip` + `OutputCard` + `ScenePreview` +
  `summary_html`: one fixture's channels as a console, shared with the
  timeline's clip editor),
  `channel_affordances.py` (`ColorWheel` via `solve_color`, `CapabilityGrid`
  over a channel's discrete capabilities, `affordance_for`),
  `fixture_chips.py` (the Qt chip widget; the family table it shows lives in
  `core/channel_families.py`),
  `dmx_display.py`, `clip_library.py`, `toolbar_manager.py`,
  `settings_dialog.py` (incl. the Input/Output section + AUDIO card),
  `io_flow_widget.py`, `voice_cues_dialog.py` (per-song spoken-cue editor),
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
  `scene_loader.py` (`load_scenes(type, mode)` → immutable `Scene` records
  sourced from any fixture def's banks). Scene banks/chases live in the
  `fixtures/*.json` defs (no more `dmx.py`/`dmx_config.json`).
- `webremote/` — `server.py` (`WebRemoteWorker`: aiohttp HTTP+WS in a
  QThread), `manager.py` (`WebRemoteManager`: Qt bridge — worker lifecycle,
  snapshots, command execution), Qt-free `state.py` / `commands.py` /
  `peaks.py`, and `static/` (the shipped tablet page: `index.html`,
  `style.css`, `app.js`; bundled via build.spec).
- `timeline/` — `manager.py` (coordinator, one `MidiEventsTrack` per fixture
  filter), `tracks/` (`base.py`, `ruler.py`, `waveform.py`, `midi_events.py`),
  `widgets/` (`tooltip.py`, `content_view.py`, `clip_edit.py` — the generalized
  `ClipEditDialog`, `verb_editors.py` — one velocity editor per verb class,
  `start_event_edit.py` — START-event editor, `resize_handle.py`).
  `clip_edit` is NOT re-exported from `widgets/__init__.py`: it pulls in the
  stage render engine (via `ui.fixture_console`) and that package is imported by
  the tracks themselves — import it from `.widgets.clip_edit` directly.
- `tests/` — headless characterization tests (standalone scripts; see above).
  `ofl_factory.py` builds synthetic OFL documents so a capability nobody ships a
  fixture for can still be exercised; `test_capability_coverage.py`,
  `test_renderer_registry.py` and `test_stage_optics.py` carry the three tables
  that fail loudly when OFL gains a capability type, a category or a wheel-slot
  type nobody handled. `test_stage_optics.py`'s archetype table is also where
  the beam classifier's traps are recorded — a narrow zoom, a zero-minimum lens,
  a colour wheel that is not a gate. `test_light_list.py` pins the photometry
  the GPU layer consumes (a blinder's overdrive surviving past full scale, a
  fixture with no optics throwing no cone, a prism dividing by 1/√N) and
  `test_tonemap.py` mirrors the ACES fit in Python, asserting its properties
  AND that the shader's matrices are that same published pair transposed
  exactly once — both GL-free, because CI's offscreen platform cannot make a
  context at all. NB **`[255] * channel_count` is not a "fixture on" frame**:
  on anything with a shutter it selects a strobe, and any assertion about
  brightness then depends on when the test ran (`_lit_frame()` raises only the
  level channels).
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

### Editing a clip (`timeline/widgets/clip_edit.py`)

**A clip is edited against the fixture it lives on, never against a table.** Its
note is a *verb*, its MIDI channel selects the fixture, and its velocity is that
verb's parameter — so `Timeline._fdef(channel)` (plus `_fixture_type` /
`_dmx_start` / `_is_locked`) resolves the patch entry first and everything the
dialog offers follows from that `FixtureDef`.

One dispatcher, `on_midi_event_edit_by_id` — group → `_edit_scene`, note 60 →
the untouched `StartEventEditDialog` (its velocity packs a hue and a palette
mode, which is not a verb parameter like the others), else `ClipEditDialog`.
`on_add_event_requested` is the third door.

- **Two pages, one shell.** A scene group gets the `ui/fixture_console.py`
  console (a fader per channel over the live preview, plus a `ColorWheel` where
  `solve_color` can place a hue). Any other clip gets its verb's own editor from
  `verb_editors.py`, picked by `editor_class_for(note)` off a table whose
  predicates mirror `handle_note_on`'s match arms — a chase index and a blend
  time are not the same control. **Retyping resets the velocity**
  (`default_velocity_for`): channel-at-200 is velocity 100, and carried across
  it would silently select chase #100.
- **The picker is `note_layout.verb_choices(fdef)`**, gated by the same
  `note_applies_to` drag-and-drop validates with, so the dialog can never author
  a clip a drop would have refused. Song parts are excluded on a fixture lane
  (they are global and carry no channel).
- **Only `present | touched` is submitted.** `present` is what the group had a
  member for on open; untouched channels stay out and render dimmed. Submitting
  every fader would silently widen a scene, and an absent channel holds the last
  frame's value while a present one at its default *forces* it — different looks.
  Growth goes through `update_scene(..., remove=...)`, which inserts missing
  members and never drops below 2 (one member stops being a group).
- **Only a fixture's first 23 slots are reachable**: the channel verbs are notes
  1–23 and 24 up are scene-load, so `field_note_map` caps there and
  `FaderStrip.set_addressable_limit` greys the tail.
- **Live output** (`live_messages` → `Timeline.preview_requested` →
  `AppCore.preview_clip`) is opt-in, never persisted, 30 ms-throttled, and locked
  out while the transport plays (`Timeline.set_transport_playing`) or the lane is
  muted/locked. Scene and channel-verb payloads **lead with note 48 TAKE_MANUAL**
  or the auto/song-part program overwrites them on its next frame — the clip
  library's previews do the same. Cancel re-sends the opening values *once*; that
  is a re-send, not a restore.
- **"Save as scene…" writes the fixture library immediately**, independent of
  OK/Cancel — it edits a type every show shares, not this clip. It then
  re-resolves `fdef` (`save_fixture_banks` clears the cache) and emits
  `library_changed` so the clip-library panel rebuilds.
- Writes go through `move_event`/`move_scene` for the time (they clamp and keep
  the BLACKOUT markers honest, unlike `edit_event(time=...)`), then
  **`_schedule_refresh()` — never `refresh_from_manager()`**.

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
