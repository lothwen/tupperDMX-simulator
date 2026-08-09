# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Project Overview

TupperDMX Simulator is a PyQt6 lighting-control app that syncs MP3 playback with
MIDI-triggered DMX sequences and a metronome, renders fixtures on a virtual stage
in real time, and ships a pure-Python "software controller" emulating a hardware
DMX controller via MIDI.

## Development Commands

```bash
uv sync                  # install deps + create .venv
uv run python main.py    # run the app
uv run python main.py --headless   # no GUI (SBC/rack): web remote is the only interface
uv add <package>         # add a dep
uv lock --upgrade        # refresh lockfile
uv run pytest            # headless tests (offscreen Qt + shared QApplication in tests/conftest.py)
```

Build/distribution commands and CI details live in `BUILD.md`. Each test file is
also runnable standalone via its `main()` — `uv run python tests/test_clip_ops.py`
prints `ALL TESTS PASSED` (`test_crash_repro.py` takes a `--sync` flag that way).

### Key Dependencies
PyQt6 (**pinned to 6.8 LTS** — 6.11 crashed QMediaPlayer on pause/resume; playback
no longer uses QMediaPlayer but QAudioSink/QAudioDecoder ride the same stack),
mido/python-rtmidi, custom asyncio Art-Net UDP receiver, librosa (BPM), pyserial
(USB DMX via stock VCP/ftdi_sio), platformdirs, aiohttp (web remote), mutagen
(duration probes), piper-tts + onnxruntime (offline TTS; voice models ~60 MB
downloaded on demand into `userprefs_dir()/tts_voices/`, never bundled).

## Architecture Overview

### Core Components

- **`app/` — the shared core (no QtWidgets, never imports `ui/`)**: `core.py`
  (`AppCore`) is the ONE orchestration object — it constructs and wires every
  widget-free collaborator (ShowManager, BpmState, AudioEngine, DMXManager,
  MidiManager, MediaController, AudioController, PlaylistCore, WebRemoteManager),
  owns the count-in/voice-cue logic, `preview_clip`, `apply_io_config`/
  `apply_audio_config`, the transport, `save_config` and the idempotent `shutdown()`
  (flush FIRST, then webremote→media→audio→engine→dmx→midi). Plus `bpm.py`
  (`BpmState`, clamps 40–300), `playlist.py` (`PlaylistCore`: nav + song-load,
  signals `song_loaded`/`audio_missing`/`setlist_changed`) and `headless.py`. Widget
  halves ride AppCore signals: `software_controller_changed` (emitted AFTER
  controller seeding), `patch_reloaded`/`show_reloaded`, `config_save_failed`,
  `precount_changed`.
- **`ui/main_window.py` — `MainWindow`**: the desktop view over `AppCore` — builds
  the widget tree, aliases the core's collaborators onto itself, connects the widget
  halves; `closeEvent` → `core.shutdown()`. `main.py` parses `--headless` BEFORE any
  Qt import (sets `QT_QPA_PLATFORM=offscreen`). Headless force-enables the web remote
  in memory (never persisted) and never downloads TTS voices.
- **`core/show/` + `app/show_manager.py` — the Show project layer**: a show is a
  **folder** `Name.show/` (`show.json` + owned `midis/` + `audio/` for imported
  bundles + show-local `fixtures/` + `versions/`). Qt-free: `model.py`, `manifest.py`
  (dict ↔ `Show`; owned midis relative, referenced audio absolute, missing paths
  PRESERVED for relinking), `store.py` (atomic JSON, `allocate_midi_path`; loading a
  show activates its fixture-defs layer), `migrate.py` (startup chain
  last→recents→create + one-time legacy-prefs migration), `versions.py` (25 unpinned
  retained, pinned never pruned, undoable restore), `relink.py`, `bundle.py`
  (`.showpack` zip collecting ALL audio, backings AND guides). `ShowManager`
  (QtCore-only, attach FIRST in `AppCore.__init__`) **aliases**
  `config.playlist`/`config.fixtures` onto the show's objects so every reader mutates
  the show in place; persistence funnels through `save_config()` → `notify_changed()`
  (saved iff content changed) and `flush()` (~3 s debounced `.mid` autosave, forced
  on song switch/close/Save; opening snapshot + ≤1 automatic snapshot per 5 min).
- **`audio/`**: ONE stereo QAudioSink feeds every path. Qt-free `mixer.py`
  (backing/click/voice buses, per-bus volume/mute/RMS, and the L/R routing modes
  `stereo` | `split_backing_left` | `split_backing_right`); `engine.py`
  (**push-mode** sink, a 15 ms Qt-thread timer topping up the ~120 ms buffer — push
  on purpose, a Python pull-QIODevice deadlocks inside `QAudioSink.stop()` on the
  GIL; Float32 w/ Int16 fallback; `load_backing` dedupes an in-flight decode of the
  same path, since restarting QAudioDecoder mid-decode aborts); `decode.py` (async →
  int16 (n,2)@44100, whole song in RAM); `tts.py` (piper + WAV cache keyed
  sha1(voice|text); **nothing synthesizes during playback — cache-first always**,
  every path degrading to None when piper/voice is missing).
- **`app/media_controller.py`**: backing playback + MIDI sync. Owns timeline position
  (the `time.time()` master clock; engine slaved with 150 ms drift resync).
  **Count-in = negative timeline time**: with `song.precount` on, playback begins at
  `-(bars×4 beats)`; audio/MIDI/lights wait for t ≥ 0; pause during count-in resets
  to 0, seeking cancels it. Fires the song's **voice cues**. **Guide track**: a song
  may carry a full-mix reference (`song.guide` + `guide_offset`) and the monitor
  (`set_monitor`, `MONITOR_BACKING`/`MONITOR_GUIDE`) picks which file feeds the
  engine via `active_file()`/`active_offset()`, missing guides falling back silently.
  The monitor is **session-only in-memory state** (always BACKING at launch), so
  headless/web-remote can never inherit it.
- **`app/audio_controller.py`**: drift-free metronome scheduler.
  `start_metronome(timeline_position_sec)` aligns beats to the timeline (floor-based,
  so negative count-in positions number correctly toward beat 1 at t=0); beeps via
  `engine.trigger_click`. AppCore speaks the count-in numbers on the LAST count-in
  bar when `config.audio.count_in_voice` is on.
- **`dmx/` — `DMXManager`**: I/O coordinator driven by `config.io` via `apply_io()` —
  owns the software `DMXController` lifecycle (emits `software_controller_changed`),
  builds its output *sinks* (`StageFrameBridge` in-process → stage, `ArtNetSender`,
  `USBDMXSender`), and runs the stage-input worker (`ArtnetWorker` on 0.0.0.0, or
  `USBDMXWorker`) when the stage input isn't the internal controller.
- **`midi/`**: `MidiManager` is a thin lifecycle/signals (`data_changed`) facade
  delegating edits to collaborators sharing one `MidiDocument` (state +
  `reconcile_blackouts`). `MidiWorker` does timeline-synced threaded output with
  **fan-out** per `MidiRouting`: `note_on` verbs to `DMXController.handle_note_on()`
  and/or the full stream to an external mido port, independently.
- **`stage/controller.py`**: virtual stage + scenario designer. Owns one flat
  `render_objs` list (one object per *body*; a fixture with a pixel matrix
  contributes one per `bars[]` entry, all fed the same DMX slice) and slices
  `dmx_data` per instance. Which class draws a fixture comes from
  `registry.renderer_for(fdef)` — never from its type name. `set_design_mode()` makes
  fixtures movable; add/change-mode/remove/drag/**re-rig** mutate `config.fixtures`
  and emit `fixture_added/moved/removed/selected`. Driven by `ui/fixture/designer.py`
  (Menu ▸ "Design stage"). `led_bars`/`blinders` survive as read-only views.
  **Rigging** (`FixtureInstance.hanging`, persisted, the inspector's RIGGING toggle):
  flown = housing upside down + beam at the floor, standing = the reverse.
  `set_fixture_hanging` rebuilds the bodies, as a mode change does. `add_fixture`
  *guesses* it from where the fixture lands (`is_flown_height`, `DECK_TOP` at ⅔
  down) and writes it down — a guess re-evaluated live would turn a fixture over
  mid-drag. A profile with `rigs_inverted = False` (hazer, smoke, fan, stand) ignores
  it and the toggle is hidden (`can_rig_inverted`).
- **`ui/fixture/browser.py`**: the fixture browser — a search/grouping rail and
  tree over a detail panel rendering the OFL document (General, Channels, Modes,
  Scenes). The footer is the patch decision: DMX start (live overlap/overflow check
  against `config.get_used_dmx_channels()`), MIDI channel (**0-15, the raw MIDI
  channel** — every surface is 0-based) and a `PatchBar`; OK stays disabled until the
  patch is legal. `values()` → `(type, dmx_start, channel, mode)`, `mode` empty for a
  single-mode type; `suggest_free_dmx_start()`/`used_midi_channels()` live here and
  the inspector validates the same two things.
- **`ui/fixture/scenes_tab.py`**: edits the fixture *library* (a type's sidecar scene
  banks), not the patch — so the dialog's OK/Cancel doesn't apply. It opens on a blank
  **draft** and one button writes: **Create** turns the draft into a scene in
  `_target_bank()`, **Save** overwrites the selected one; a pending change to the banks
  themselves (`_dirty`) takes the button first, and leaving with work pending asks
  (`save_pending_changes()`). The `_SceneTree` keeps **bank sections** with their
  scenes beneath — both orders are what MIDI addresses (36+n bank, 24+n scene), so
  never flattened; reorder is drag-and-drop resolved to `move_requested(...)` and
  applied to the data (Qt never moves the items, or their roles go stale). Writes via
  `fixture_library.save_fixture_banks`.
- **`ui/fixture/console.py`**: one fixture's channels as faders over a live preview,
  shared by the Scenes tab and the clip editor. `FaderStrip` (one fader per channel,
  coloured by `channel_family`; `set_addressable_limit` greys slots past MIDI note
  23, `set_present` dims channels a scene doesn't carry; it IS its faders) and
  `OutputCard`. `ScenePreview` **is** the stage engine — `renderer_for(fdef)`, one
  body per `body_count()`, fed raw DMX — so a look previews exactly as on stage;
  **`clear()` it before it goes away**, the bodies own timers on the shared clock.
  Everything but the *size* comes from the stage: a preview holds one fixture with
  nothing to compare it against, so `metrics.preview_def` normalises every body to
  the same size and the frame is `_MARGIN` around it plus `emission_reach` on the
  side `emission_rises` says the light uses.
- **`ui/fixture/affordances.py`**: controls that beat a fader, chosen from the
  fixture's capabilities and never from its name. `ColorWheel` writes through
  `solve_color` (`wheel_applies()` asks it rather than `color_slots`, which counts
  Cold/Warm White). `CapabilityGrid` turns a channel carrying ≥4 discrete
  `Effect`/`ColorPreset`/`WheelSlot`/`Maintenance` capabilities into tiles valued from
  their own `dmxRange`, **all animating at once**. `assets/animations/*.gif` is
  optional artwork matched by `effectName`, so it can never appear on a fixture that
  didn't declare those effects. `affordance_for(fdef, index)` is the lookup.

### The render engine (`stage/`)

**Two stages, in this order — the OFL *category* picks the body and the kind of light
it makes; the *capabilities* its channels declare pick the layers that shape that
light.** All 17 categories and all 43 capability types resolve;
`test_capability_coverage.py` and `test_renderer_registry.py` fail loudly if either
gains an entry nobody handled.

- `registry.py` maps a category to a **profile** via `@register_renderer(...)`; the
  fixture's own category order wins (OFL lists the most important first), falling
  back to the shape of its channels (makes fog ⇒ hazer, pans ⇒ mover) and finally to
  `GenericStageFixture`.
- `optics.py` — **what kind of light a fixture throws**, a three-valued `BeamKind`:
  `NONE` (a surface light, no shaft in the air), `WASH` (soft cone) or `HARD` (a
  defined edge). Qt-free, may import only from `core`, keeping it below the
  `registry`↔`profiles` cycle. `beam_kind(fdef, fallback)` decides **whether** there
  is a shaft, `resting_half_angle(fdef, kind)` **how wide** — kept apart on purpose,
  since conflating them drew an ARRI SkyPanel with a 57.5° beam. Rule order: a
  `stage.beam` sidecar hint wins outright; then beam-shaping evidence
  (`Prism`/`Iris`/`Blade…`/`Focus`, or a wheel carrying a `Gobo`/`Prism`/`Iris`
  **slot**) ⇒ HARD; then the declared lens — ≤ `HARD_MAX_DEGREES` (12°) ⇒ HARD,
  > `WASH_MAX_DEGREES` (65°) ⇒ NONE, else WASH, with a spread ≥ `ZOOM_RANGE_RATIO`
  (2.5×) read as a **zoom range** rather than a gate; then a bare `Zoom`/`BeamAngle`
  ⇒ WASH; else the profile's `beam_fallback`. **What is NOT evidence matters as
  much**: `Frost` rides on wash heads and a bare `WheelSlot` is whatever its slots
  are — reading either as hardness drew 32 wash movers as spots; and OFL's
  `physical.lens.degreesMinMax` is the *beam angle*, which every emitting fixture
  has. `test_stage_optics.py` carries the archetype table.
- `profiles/` — the body and its chassis animation: `lamp.py` (Color Changer /
  Dimmer / generic fallback), `array.py` (Pixel Bar / Matrix, Blinder, Strobe),
  `motorised.py` (Moving Head, Scanner, Flower, Laser), `atmospheric.py` (Hazer,
  Smoke, Fan, Stand). A profile declares only its housing, `aim_origin()`,
  `emission_for(fdef)`, `paint_housing()` and `beam_fallback`; it never reads a
  capability. A lamp body stays `NONE`, a moving head falls back to `WASH`, a scanner
  to `HARD`.
- `lamp_is_lens`: a moving head throws **everything it makes down its beam**, so its
  lamp is that beam's aperture, not a second light beside it. It gets
  `cone.lens_glow_radii` — the halo's radius **in lamp radii** — and `lens_glow_scale`
  divides by whichever baseline is asking, so one number drives both the raster
  `glow_radius_multiplier` and the POINT's radius. Never zero, it **grows with the
  aim** (squared in `beam.foreshorten`), and it is ignored on a body that throws no
  shaft (`Lensed.lens_only`). The raster halo is the item's bounding rect, so it must
  move through `GlowingEllipse.set_glow_radius` or the old halo stays painted; a lens
  fades on `glow.DIFFUSE_GLOW`, not the classic `SHARP_GLOW`, whose original alphas
  (180/130/90/40/0) keep every other fixture rendering byte-identically.
- `emission/` — how the light is drawn: `cone.py` (`WashLamp`, `LampGlow` — the same
  lamps with no cone ever built, so a stray zoom channel cannot conjure one —
  `HardBeam`, `BeamFan`), `surface.py` (`MatrixCells`, `BulbArray`, `FlatPanel`),
  `volumetric.py` (`LaserFan`, `FogPlume`), `NoEmission`. `emission_for_kind(kind)`
  is the join from `optics.py`; `emission_reach(fdef)` is a per-class constant and
  `emission_rises(fdef)` says which way it is spent — a beam falls down the stage,
  a plume climbs, and reserving the room below a fog machine framed it out. An
  emission that draws a face draws **one item per zone** from the state's emitters,
  so a segmented wash shows every segment while staying a wash.
- `features/` — the capability layers, each naming the OFL types it consumes and
  mutating the shared `BeamParams`: dimmer, colour, colour wheel, shutter (all ten
  `shutterEffect` waveforms), zoom, iris, frost, gobo, prism, blades, pan/tilt.
  **Discovered once at construction** from `all_capability_types(fdef)`, so a "Moving
  Head" that is really a wash simply has no gobo layer — graceful degradation with no
  per-fixture code anywhere.
- `frame.py` (`Frame` + `BeamParams`, the contract between the two stages;
  `half_angle_deg` is the **resting** spread — `None` only when the kind is `NONE` —
  and `ZoomFeature` overwrites it every frame, so a live zoom wins), `geometry.py`
  (`Aim`: the fixture is aimed in a **room** — a unit vector from pan/tilt — and only
  then flattened onto the front elevation, so `(axis_deg, foreshorten)` IS that vector
  and the fog march can rebuild it. **Home (half travel, DMX 127/128) is straight
  down**; **tilt swings the beam and pan turns the plane it swings in** — at pan home
  tilt walks it toward the viewer and only shortens it, a quarter turn away the same
  tilt sweeps it across the stage, and at tilt home *pan cannot move a beam at all*.
  `PanTiltFeature` slews the pan/tilt, never the projection, and the housings read
  `aim.pan_offset()` / `frame.beam` so a yoke can never have turned where its beam
  did not — plus `Spinner`, `Slew`), `clock.py` (ONE 30 Hz timer for everything that
  animates, subscribed weakly), `wheelart.py` (procedural gobo/gel/prism art from the
  slot's own OFL description), and the three that used to be one `base.py`:
  `body.py` (`FixtureLabel`, `DesignableFixture`, and `BodyFixture`, which runs the
  pipeline: reset the beam → each feature shapes it → the emission draws it → the
  profile repaints its chassis. **Every profile draws its chassis the way it stands
  on the deck, into `body.housing`** — one item `ChassisPart` re-parents to itself,
  which a flown fixture turns 180°. **Never the body item**: the GPU offsets each
  body's lights by its *position* alone, so a turned body would send the raster and
  the shader two different beams; the light's own half of the flip is `Aim`'s, and
  `emit_origin()` is `aim_origin()` carried through it), `glow.py` (`GlowingEllipse`
  and the `SHARP_GLOW` /
  `DIFFUSE_GLOW` falloffs) and `metrics.py` (the mm→px helpers anchored by
  `MM_PER_STAGE_PX`, `body_size`/`body_count`/`body_spacing`/`declared_cell_count`
  — Qt-free, since a body is sized and placed before there is a state to resolve,
  plus `preview_def`, which throws the real scale away: it rewrites a definition's
  `size_px`/`body_spacing` so every body previews at one size, keeping its aspect
  ratio, because a **beam is 1220 units and a halo caps at 400 whatever body made
  them** — real dimensions put a scanner head inside its own beam and left an ARRI
  panel no room for its glow).
- **Atmosphere**: a beam is invisible in clean air, so visibility is the one genuinely
  cross-fixture quantity. `StageController` owns one `Atmosphere`; any body resolving
  a fog output contributes, sources combine without exceeding full, and the level
  drifts on the shared clock (~1.5 s attack, ~12 s decay). The manual baseline
  `config.window.stage_haze` goes through `set_haze_baseline` → `Atmosphere.settle`
  (**straight to the target, no drift**) → `refresh_air()`; both halves are
  load-bearing, since a body reads `atmosphere.level` only on the frame it draws.
  `Fog` is weighed **below** `Haze` (0.6 vs 1.0), and a definition declaring no
  `fogType` falls back to `BodyFixture.default_fog_kind`.
- **`puffs.py` — smoke that outlives its channel.** A `PuffField` holds discrete
  parcels with a birth time, so a machine switched off does not clear the stage.
  **Every quantity is derived from a parcel's age, never integrated**, so the field
  can tick at 30 Hz or twice an hour and look the same: it rises **and stops rising**
  (on its OWN `rise`/`decay`, ±`rise_spread`, or the whole bank draws a hard line
  where every parcel ran out of lift at once), **disperses** (`sqrt(2·K·age)` in its
  own direction — a random walk, and the term that actually fills a room; a constant
  velocity biased away from centre was the reported "two-branch tree"), curls on
  `WANDER_OCTAVES` with X and Y **decorrelated** (one shared sine is a parcel sliding
  down a diagonal), grows by diffusion (`sqrt(r0² + 2·D·age)`) and thins because it
  grew (`(r0/r)²`). Two specs: `HAZER` (`max_output` **0.35**, 100 parcels, 20 s each,
  ~600 px of walk) and `SMOKE` (1.0, 400 over the same 20 s). A ring, so an hour of
  running holds a fixed count — and both are sized so `capacity / rate_hz` IS
  `lifetime_s`, so it wraps exactly as the oldest parcel expires and the whole life
  still hangs *after* the channel closes. A parcel leaves the nozzle
  `NOZZLE_DENSITY`× thicker than it settles at, so
  the ceiling a BLOB carries is `spec.density * NOZZLE_DENSITY`, not `spec.density`
  (and past 1.0 the raster plume clamps where the GPU does not). Qt-free; **both**
  renderers read the same
  field **and the same `PARCEL_*` shape** — `_PlumeItem` bakes those constants into a
  sprite sheet, `MEDIUM_FRAG` rebuilds the identical disc-times-two-octaves per
  fragment, and a parcel rasterised as a clean Gaussian (which is what the GPU did)
  is a *sphere*: the eye finds the circles however many are stacked, which is why the
  GPU path read worse than the raster fallback. `_PlumeItem` caps at
  `PLUME_MAX_ITEMS` and skips below `PLUME_MIN_ALPHA`; `PLUME_ALPHA` and `PLUME_SHARE`
  are both well under what "compensating for skipped parcels" would suggest, because
  Qt composites `SourceOver` — parcels *saturate* rather than add, so **alpha does not
  stand in for count** and over-compensating draws separate balls, not a bank.
- **`light.py` — the third stage, the GPU's view of the same frame.** A brush is
  8-bit sRGB: it cannot hold "twice as bright as white", and two overlapping do not
  add, so every emission also describes its light as data.
  `Emission.emit_lights(frame, out)` (a fifth hook beside
  `build`/`render`/`cleanup`/`reach`) appends `LightPrimitive`s — POINT / CONE / QUAD
  / LINE / BLOB — to a `LightList`: one growable `array('f')`, 16 floats each. They
  are **body-local** (dragging needs a repaint, not a re-render), colour is **linear**
  and `intensity` is **unbounded** — which is how two lights add past full instead of
  clipping — with `config.window.stage_exposure` scaling it. Haze is deliberately NOT
  baked in; it reaches the shader as a uniform. `cone_geometry()` is shared by the
  raster cone and the light list so the two cannot drift. A **LINE is thinner than
  the pixel that draws it** — `LASER_HALF_WIDTH_PX` is ~1 scene unit and the view
  fits 1536 of them into a few hundred — and GL has no coverage, so `LIGHT_VERT`
  widens its quad to `MIN_LINE_HALF_PX` device pixels (from `u_px_per_scene`, the
  one thing a shader cannot derive) and hands the brightness back. Widen without
  dimming and a fan is a smear; do neither and it rasterises to dashes, which is
  what it did.

#### `gpu/` — the optional HDR renderer

`config.window.stage_gpu`, off by default. `StageView.setViewport(QOpenGLWidget())`
with the light composited in `drawForeground` inside `beginNativePainting()`: every
primitive accumulates into an RGBA16F FBO with `glBlendFunc(GL_ONE, GL_ONE)` — light
**adds** — then exposure, an ACES filmic tonemap (Stephen Hill's fitted RRT+ODT) and
an additive composite. Both passes are `glScissor`ed to the stage image and
`_restore()` puts blend/scissor/viewport back (`endNativePainting` only restores what
Qt tracks). In GPU mode `set_raster_light_visible(False)` stops the raster drawing
**spill** while faces stay lit. Everything fails soft to raster and logs once. Four
constraints, documented in `gpu/layer.py`: a `QOpenGLWidget` *parented into* the
viewport renders opaque and hides the scene; the format must
`setRenderableType(OpenGL)` or Wayland/EGL hands out GL ES where nothing compiles; it
must NOT set a version/profile or EGL fails outright; draw calls come from
`QOpenGLFunctions_4_1_Core`. `ScenePreview` attaches the same layer, gated on the
stage's GL having *succeeded*, keeping offscreen/headless callers on raster.
`QWidget.grab()` cannot read back a GL viewport — scripted screenshots need raster.

**`gpu/fog.py` — volumetric fog** (`config.window.fog.enabled`, off by default, needs
`stage_gpu`; switch on the stage's edit toolbar as well as in Settings, after which
`MainWindow` must call `StageController.apply_stage_config()` because the dialog hands
back a **new** `StageFogConfig`). Inserted between accumulate and resolve, so fog
lands in the HDR buffer *before* the tonemap and is simply more light. Three passes: a
**medium map** (BLOBs additively rasterised into a **quarter**-scale RG16F picture of
the air — an eighth resolves *where* a parcel is, which was enough while one was a
smooth Gaussian, and aliases the per-parcel noise that now shapes it — with depth
extent stored density-**weighted** so it averages rather than sums), the **march**,
and a **temporal** average. The projection is orthographic, so a pixel's view ray is
exactly `(scene_x, scene_y, −camera_z + t)` and depth needs no buffer (**`STRIDE`
stays 16** — a BLOB's `spin` and `variant` ride in the `half_width`/`softness` slots
a parcel has no use for); cost follows lit area because each light gets **one bounded
quad**, the only fullscreen draw being ambient.

- **Lights and media share ONE instance buffer**, media packed after lights, which is
  why `LightLayer._upload_instances()` is its own step and not the light pass's: it
  used to sit behind `_accumulate`'s `count == 0` early return, so a stage whose only
  emitter was a fog machine (a hazer through a blackout) uploaded nothing and the
  medium pass drew out of a buffer that had never been allocated — a segfault.

- **The segment is solved, not swept** — a beam is a thin sheet in depth (~66 units)
  while an even march steps ~58, so the shaft never appeared: each ray is clipped to
  the bounding-sphere chord, then for a cone to the cone (`cone_segment`) and for a
  laser to its tube (`line_segment`, `LINE_FOG_RADIUS`). The laser is the worse case
  — one pixel in ten got a single sample and the rest none, so a fan marched as
  speckle that moved with the dither. `scatter_gain` (Henyey-Greenstein over its
  **broadside** value), `BEAM_RADIANCE` and a `FOG_POINT_REACH`
  of 10 lamp-radii — **not** the sprite's `GLOW_REACH` of 60 — calibrate it.
- **The air is ragged and drifts** (`fog.texture`, `fog.texture_scale`, the
  `turbulence()` fbm at `FLOW_PX_S`): two octaves clamped at zero, the holes being
  what read as volume, with `TEXTURE_CONTRAST` stretching the noise, `FINE_WARP`
  domain-warping the fine octave and `FINE_DRIFT` shearing the two. Since it flows,
  `_frame_signature` excludes time (it would make `_settle` reset its own countdown):
  `air_is_drifting()` answers instead and `_advance_atmosphere` paces redraws.
- **Ambient** lights baseline haze at `AMBIENT_HAZE_WEIGHT` (0.09) — not full (the
  backdrop is already a lit room), not zero (or HAZE shows nothing without a shaft),
  and machine smoke is *not* scaled by it. With fog on the march owns the shaft and
  `LIGHT_FRAG`'s CONE keeps only its lens core (`u_fog_active`).
- **The baseline takes all of `turbulence()`; a parcel takes `MEDIUM_TEXTURE_WEIGHT`
  (0.4) of it.** Smooth height fog has nothing else to break it up, but smoke arrives
  from the medium map already ragged, already turning and already drifting with the
  machine that made it — multiplying that by a second field on its own current is
  what made GPU smoke read as a texture sliding behind a pile of blobs.
- **Three** fog framebuffers, not two: averaging A into B while sampling B is a
  feedback loop that ramped fog to white over four frames. `QOpenGLFramebufferObject`
  also hands out **GL_NEAREST**, so `_smooth()` sets GL_LINEAR. And **an empty light
  list is not an empty frame**: the guard also asks `_air_worth_drawing()`, and
  `_accumulate` still clears the HDR buffer, or a dark stage keeps the last lit one.
- **The stage clip is switched OFF for the march**: GL scissors in absolute
  framebuffer pixels while the clip is in *surface* pixels and every fog target is a
  different size — left on, the march loses everything left of the letterbox bar and
  the few-hundred-texel medium map entirely. Clipping is `_resolve`'s job. The march
  is also dithered, so `request_repaint` asks for `CONVERGE_FRAMES` more frames after
  the rig settles **and then stops** — the only thing here that can spin; that
  countdown is reset by `_frame_signature()` changing, **not** by the march running.

### Domain layer

- **`core/fixture_state.py`**: the Qt-free photometric model covering **all 43 OFL
  capability types** through a dispatch table (`_HANDLERS`).
  `resolve_state(fdef, dmx) -> FixtureState`, grouped into frozen records: `color`,
  `optics`, `motion`, `strobe`, `program`, `atmosphere`, `wheels` — with **the flat
  names the rest of the app reads (`rgb`, `emitters`, `shutter`, `effect`, …) kept as
  read-only views onto them**. Colours mix additively from `REFERENCE_RGB` (the 13
  OFL `ColorIntensity` colours). **Emitters** come from matrix pixel keys, matrix
  groups, or — with no matrix — repeated channels (the n-th channel of a colour is
  the n-th cell), which is how a multi-cell blinder resolves without special-casing.
  `capability_at(fdef, index, values)` is the ONE "what is this channel doing right
  now" — it takes the whole slice on purpose, because a switching channel's meaning
  lives on another slot. `solve_color(fdef, rgb)` is the **inverse** (light in, DMX
  out) the controller's palettes write through: R/G/B straight through where the
  fixture mixes them (zeroing its other emitters), a greedy fit otherwise (CMY, RGBA),
  `{}` for a whites-only fixture — inverting the SAME model, so what the controller
  writes is what the stage draws.
- **`core/ofl.py`**: the Open Fixture Library (12.5.1) parser. `resolve_mode()`
  flattens one mode to ordered DMX slots with matrix inserts expanded (all 7
  `repeatFor` orders × `perPixel`/`perChannel`; for `eachPixelXYZ` **X varies
  fastest**), fine channels linked to their coarse channel, and unresolvable slots
  kept in place so they still consume an address. `$pixelKey` expands over **pixels
  and pixel groups alike** (group channels name `pixel_group` instead of `pixel_key`).
  **Switching channels** carry `switch_options` plus `switch_of` (the trigger's slot
  index, which may be patched later), while `channel` holds the resolution at the
  trigger's default so labels stay static and `fixture_state` picks the live one per
  frame. **Wheels** are parsed (`wheels` → `Wheel`/`WheelSlot`); `slot_at(wheel,
  number)` resolves a 1-based, possibly fractional number, wrapping, returning the
  *pair* either side mid-change so the renderer can crossfade. `parse_entity()` turns
  OFL's units and keywords ("fast", "6500K", "540deg") into canonical floats across
  all 17 entity kinds. Deliberately lenient — unknown capability types are preserved,
  not rejected; document metadata is parsed but never interpreted.
- **`core/fixture_library.py`**: the fixture-type *library* and the OFL→app bridge. A
  definition is **one or two files**: `fixtures/<x>.json` (a strict OFL document)
  plus an optional `fixtures/<x>.tdmx.json` sidecar (`core/fixture_sidecar.py`)
  carrying what OFL cannot express — scene `banks`, `chases`, `song_parts` tags,
  per-channel `label`/`flags`, the effect→animation map and `stage` hints (`size_px`,
  `bodies`, `body_spacing`, `cells`, `beam`, which overrides `optics.beam_kind()` for
  a fixture whose gate is not a DMX channel; mode blocks merge `channels` per key but
  replace `stage` **wholesale**). What a channel *does* is never declared there, and
  a fixture straight from OFL works with no sidecar. Loaded in three layers — shipped,
  `userprefs_dir()/fixtures/`, and the open show's (`set_show_fixtures_dir()`); later
  layers override by key; memoized; **sidecars never cross layers**. Each layer is
  scanned **recursively** and a subfolder is the **manufacturer**
  (`fixtures/ADJ/focus-spot-4z.json` → `"ADJ/Focus Spot 4Z"`); a file at a fixtures
  dir's root keeps its **bare** key, which is what saved patches refer to.
  `FixtureDef` is one type at **one mode**: `resolve_fixture(type, mode)` is the
  canonical accessor, plus `fixture_modes()`/`fixture_document()`. **A channel has no
  summarised description** — no role enum: everything reads its capabilities through
  `dominant_capability()` (most DMX wins, `NoFunction` never counts), `color_slots()`,
  `capability_types()` and `all_capability_types()` (the "does it do X *anywhere*"
  question). `save_fixture_banks()` is the ONE writer: it round-trips the **raw**
  sidecar JSON so unmodelled keys survive, picks the block via `banks_mode_key()`, and
  **forks the definition into the user layer** when the source dir isn't persistable
  (a frozen `_MEIPASS` is writable but deleted on exit), copying the OFL document so
  the key is unchanged. The OFL document itself is never written: a type's DMX
  footprint is untouchable from the editor.
- **`software_controller/`**: pure-Python DMX controller (no hardware needed).
  `DMXController` manages a 96-channel universe patched from `config.fixtures`.
  `handle_note_on(channel, pitch, velocity)` uses a **generalized note map**: the
  pitch is a universal *verb* and the MIDI **channel selects the fixture** (its index
  in the patch). Layout: 1–23 channel-offset sets (velocity×2), 24–35 scene loaders,
  36–47 bank select, 48 take-manual / 49 manual / 50 blackout-fixture, 51
  blackout-all, 52–59 timing/palette/scene step, 60 START, 61–69 song parts, 72–75
  colour macros. Colour verbs go through `solve_color()`; a scene hands its colour to
  the live palette with the 255 sentinel on R/G/B. The 20 Hz loop fans each universe
  snapshot to pluggable *sinks*. Old positional `.mid` are upgraded by
  `scripts/migrate_midi_v2.py`.
- **`webremote/`**: embedded tablet remote (off by default; plain HTTP on the stage
  LAN, no auth; headless force-enables it). `WebRemoteWorker` runs an aiohttp HTTP+WS
  app on its own asyncio loop in a QThread; `WebRemoteManager` subscribes to existing
  signals, publishes snapshots plus 10 Hz `pos` / per-beat `beat` events, and executes
  inbound commands on the Qt thread — cues/blackout/panic via `preview_clip` so
  `config.io` routing applies. Blackout is a toggle: panic sends note 51 only when
  `blackout_all_state` is off. Qt-free `state.py`/`commands.py`/`peaks.py`. The
  no-build page in `static/` reflows one CSS grid into tablet, portrait-rail and
  phone (≤600px) layouts; live widgets include the KEY chip, UP NEXT pill, AUTO
  COUNT-IN card, RMS-fed CLICK/BACKING meters and a countdown while `position < 0`.

### Configuration & persistence (`core/config.py` + `core/show/`)

- **`config.json`** (shipped, read-only): `fixtures`, the *seed* patch for brand-new
  shows; resolved by `shipped_config_path()` (`_MEIPASS`).
- **`userprefs.json`** (user-writable, MACHINE-level only): `window` — incl.
  `stage_haze`/`stage_gpu`/`stage_exposure` and the nested `fog` (`StageFogConfig`;
  there is deliberately no `fog_density`, because that IS `stage_haze`) — `artnet`
  (the controller's *output* target), `volume`, `logging`, `io` (`IOConfig`, the
  explicit routing), `audio` (`AudioConfig`: device id, click/voice routing mode,
  `count_in_bars`, `count_in_voice`, `tts_voice`), `webremote` (enable/port + the
  default cue set seeding NEW shows) and `last_show_path`/`recent_shows`.
  Machine-level because whether there is a usable GL driver is a property of the box.
  Resolved by `userprefs_path()` via `platformdirs`, written atomically. Legacy
  `last_dmx_source`/`last_midi_port` migrate one-time into `io`; legacy
  `playlist`/`fixtures` are still *parsed* (the show migration adopts them) but never
  written again.
- **`<Name>.show/show.json`** (the SHOW manifest): the setlist (`songs` with
  `id`/`title`/`key`/`precount`/`voice_cues`/`offset`/`guide`+`guide_offset`/paths + a
  derived `cache` of duration/bpm), `current_song_index`, the fixture **patch** (type
  + `dmx_start` + stable MIDI `channel` + optional `mode`, where `""` means the type's
  first declared mode so a renamed mode never strands a patch, + `hanging`, which
  defaults to **true** so a patch written before fixtures could say which way up they
  are rigged keeps throwing its light at the floor) and `webremote_cues`.
  Owned `.mid` files live in `midis/`; audio is *referenced in place* unless it
  arrived in a bundle. The `.mid` stays canonical — extras plain MIDI can't carry
  (stable scene `group_id`, `scene_name`, per-clip fades) ride as `tdmx1:{json}` text
  metas (`midi/codec.py`; "Export MIDI…" writes an `interop=True` copy without them).

`Config.load()` reads shipped+prefs; the startup show comes from
`core/show/migrate.open_or_create_startup_show()` (first launch converts the legacy
prefs playlist/patch into `~/Documents/TupperDMX/Untitled.show/`). After
`ShowManager.attach()`, `config.playlist`/`config.fixtures` ARE the show's objects.

### UI Architecture

- `ToolbarManager` (`ui/toolbar_manager.py`): **SHOW** (New/Open/Save/Save as…, a "…"
  overflow with Import bundle…, a split ⤓ Export button whose ▾ menu offers interop
  Export MIDI… (`export_midi_action`, gated by `set_export_midi_enabled`)),
  **SETLIST** (an editable song-selector QComboBox — `activated()` → `song_selected`,
  edit+Enter → `song_title_edited` — plus BPM / offset / media chip, the per-song KEY
  line-edit and the 🔔 `song_precount_toggled`), **SONG FILES** (the live autosave
  chip and three cards MIDI / BACKING TRACK / GUIDE, each a load button over a path
  well, plus ▷ new-MIDI and ✕ `guide_clear_requested`), **VERSIONS** (a snapshot
  picker + Restore emitting `restore_version_requested(version_id)`, fed by
  `set_versions(list_versions(show))`), and the ☰ menu.
  `PlaylistController.update_song_counter()` is the central setlist refresh;
  `MainWindow._refresh_versions_ui` repopulates the picker on
  `ShowManager.versions_changed`.
- `SettingsDialog` (`ui/settings_dialog.py`): modal userprefs editor, two-column
  **Input / Output** page. Left: three numbered routing cards editing `config.io`
  (① sequencer MIDI outputs, ② internal controller + DMX outputs incl. the Art-Net
  target, ③ stage input radios), quick-mode presets, a Logging card and an **AUDIO
  card** editing `config.audio` (device, stereo/split routing, count-in bars + spoken
  toggle, TTS voice picker with Download/Test). Right: a live signal-flow graph
  (`ui/io_flow_widget.py`, from `io_routing.flow_state(...)`) over a validation strip
  from `io_routing.validate()` (hard errors block accept). The two universe spinboxes
  edit the one shared `ArtNetConfig.universe` and mirror each other.
- `Timeline` (`timeline/manager.py`): waveform, beat detection, seeking, clip-style
  MIDI editing — one `MidiEventsTrack` per fixture filter. The header has MASTER /
  CLICK / VOICE volume rows (each → its engine bus); the ruler draws 🗣 voice-cue
  markers, display-only — editing lives in the toolbar's 🗣 Cues button →
  `ui/voice_cues_dialog.py`. `ui/timeline_toolbar.py` also hosts the segmented
  `BACKING | GUIDE` monitor toggle → `MediaController.set_monitor`: the waveform
  draws the monitored track and dragging it writes that track's offset.
- `ClipLibraryWidget` (`ui/clip_library.py`): sidebar tabs ("Library", "DMX
  Channels"). One group per patched fixture (verbs named by `describe_note(note,
  fdef)`), one per scene bank, one for chases, plus a global "Show" group. Click
  previews, drag drops onto the timeline. A scene's preview payload **leads with note
  48 TAKE_MANUAL**, or the auto/song-part program overwrites it next frame.
- `DMXDisplayWidget` (`ui/dmx_display.py`): live channel monitor.
  `ErrorDialogManager` (`ui/error_dialogs.py`): centralized retry/reset dialogs.

## Critical Behavior Patterns

### Metronome Sync
On start/resume, compute beats elapsed (`total_beats_elapsed =
timeline_position_sec / beat_duration_sec + 1`) and the remainder to the next
boundary: play immediately if within `0.001s`, else a single-shot timer to the next
beat then regular intervals. Always restart on seek-during-playback or BPM change.

### Media Playback Timeline
`time.time()`-based; separate `current_timeline_time` / `playback_start_time` for
pause/resume, and `audio.offset` fine-tunes audio↔MIDI/DMX sync. The BACKING|GUIDE
monitor swaps which file the engine decodes (one track in RAM at a time; switching
mid-play drops out until the new decode lands and drift resync repositions it).
MIDI/DMX/metronome/cues are timeline-driven and identical under either.

### MIDI Event Management
- Clip-style: each event is a movable, resizable rectangle on its fixture lane, with
  three drag modes (MOVE, RESIZE_LEFT, RESIZE_RIGHT), neighbor clamping and
  zoom-aware edge-snap. Persistence happens **only** on `mouseRelease` via
  `*_committed` signals; mid-drag is purely visual.
- **BLACKOUTs are gap markers**, scoped per fixture (`BLACKOUT` for ledbar,
  `BLACKOUT_BLINDERS` for blinders), identified by part-name PREFIX
  (case-insensitive), not note number. `reconcile_blackouts()` forbids
  zero-duration/adjacent BLACKOUTs and reinserts them to keep clip extents; deleting
  a regular clip converts it to a fixture-matching BLACKOUT, not a hole.
- Scene clips: 7 simultaneous channel-control `note_on` events sharing a `group_id`,
  rendered as one grouped clip; drags route to the scene API (`add_scene`, group
  move/resize). Clips fade via `FadeEngine`; multi-selection (marquee/ctrl-click)
  moves/deletes groups via `SelectionEditor`.

### Clip Library Drag-and-Drop
MIME types `application/x-tupperdmx-midi-clip` (single note + velocity) and
`application/x-tupperdmx-led-scene` (full 7-channel payload).
`Timeline.can_accept_clip_drop()` validates the fixture under the cursor matches the
clip's (scene drops restricted to ledbar); `handle_scene_drop()` calls
`MidiManager.add_scene()`. Click-to-preview routes `preview_requested` →
`MainWindow._preview_clip()` without touching the timeline.

### I/O Routing (`config.io` + `core/io_routing.py`)
All routing is explicit in Settings ▸ Input / Output (no toolbar selectors, no
localhost magic). `MainWindow._apply_io_config()` applies `config.io`:
`MediaController.set_midi_routing(midi_routing_from_io(io))` (fan-out: internal
controller and/or one external port) and `DMXManager.apply_io(io)` (controller
lifecycle → sinks → stage input). With the defaults ("All software") the controller
runs and feeds the stage **in-process** via `StageFrameBridge` — no UDP loopback.
Cardinalities: MIDI out 1→N (stateful command protocol: fan-out fine, fan-in
forbidden), controller DMX out 1→N (idempotent snapshots), stage DMX in = exactly one
selectable feed.

Colour palettes via `generate_song_colors(mode, hue_index)` (HSL→RGB,
mutex-protected); `PALETTE_MODES = COMPLEMENTARY, ANALOGOUS, TRIADIC, COLOR+WHITE,
MONOCHROMATIC, SINGLE`. The `START` note has no `song_index` — its velocity encodes
the palette (`hue_index = velocity // 6`, 12 = random; `mode =
PALETTE_MODES[velocity % 6]`). `pick_scene_from_song_part()` picks per-part scenes
(all VERSEs share one hardcoded fill). `_apply_blend()` interpolates present↔future
DMX to avoid blackout artifacts.

## File Organization

Every package is described under Architecture above; this is the map.

- `main.py` — the only root source file: thin entry point (`--headless` parsed before
  any Qt import).
- `app/` — QtCore/QtMultimedia only, **NEVER** QtWidgets or `ui/`; no eager
  re-exports. `core.py`, `bpm.py`, `playlist.py`, `media_controller.py`,
  `audio_controller.py`, `show_manager.py`, `headless.py`.
- `core/` — Qt-free domain layer (no eager re-exports; import submodules directly):
  `config.py`, `show/`, `paths.py` (single source of `bundle_dir()`/`asset_path()`/
  `userprefs_dir()`/`default_shows_dir()`, dev vs `_MEIPASS`), `ofl.py`,
  `fixture_sidecar.py`, `fixture_library.py`, `channel_families.py`
  (`channel_family()`/`describe_channel()`: what a channel is in one word and one
  colour — the browser's chips, the Scenes faders and the clip labels all read it),
  `fixture_state.py`, `note_layout.py`, `io_routing.py`, `buildinfo.py`.
- `fixtures/` — shipped definitions: a strict OFL `*.json` + optional `*.tdmx.json`.
  A **subfolder is a manufacturer** and namespaces the key; files at the root keep
  their bare key and show as "Generic". `hazer` and `smoke-machine` are the generic
  fog machines — nothing shipped declared a `Fog`/`FogOutput`/`FogType` capability
  before them, so the fixture-driven half of the atmosphere was unreachable.
- `audio/` — `mixer.py`, `engine.py`, `decode.py`, `tts.py`.
- `stage/` — the render engine, a **top-level** package like `timeline/`: it is
  QtWidgets, but it imports nothing from `ui/` (only `core/`, PyQt6 and
  `tupperlogger`), which is what earns it the root. `controller.py`
  (`StageController`/`StageView`, also owns the `Atmosphere`) drives `body`,
  `glow`, `metrics`, `registry`, `optics`, `frame`, `geometry`, `clock`,
  `atmosphere`, `puffs`, `wheelart`, `light`, `profiles/`, `emission/`,
  `features/`, `gpu/`.
- `ui/` — the desktop view, in **three layers**, and **no eager re-exports at any
  of them**: `ui/__init__.py` is a docstring, because re-exporting the window and
  its dialogs meant `from ui.widgets import icons` — which the timeline tracks do
  — executed all of them and dragged in the stage engine, the MIDI worker, the DMX
  manager and the web remote (70+ modules to import an icon factory; it is 5 now).
  Import the submodule you want.
  - `ui/widgets/` — leaf toolkit, no app knowledge, safe for anyone to import:
    `icons.py`, `toggle_switch.py`, `chips.py`. `chips.FlowLayout` is the ONE flow
    layout (the clip library kept a second copy); margins are always zero, set them
    on the parent.
  - `ui/fixture/` — the fixture-editing subsystem: `browser.py` (was
    `add_fixture_dialog`), `scenes_tab.py`, `console.py`, `affordances.py` (was
    `channel_affordances`), `designer.py`. `timeline/widgets/`'s `clip_edit` and
    `verb_editors` reach in for `console` and `affordances`; with `ui.widgets` that
    is the whole of the timeline's dependency on `ui`.
  - the root — `main_window.py`, `playlist_controller.py` (widget half; guides
    relink silently) and the rest of the dialogs/panels named under Architecture
    (`dmx_display`, `clip_library`, `toolbar_manager`, `timeline_toolbar`,
    `settings_dialog`, `io_flow_widget`, `voice_cues_dialog`, `error_dialogs`,
    `about_dialog`). `StageController`/`StageView` are not here — import them from
    `stage.controller`.
- `dmx/` — `manager.py`, `artnet_worker.py`, `usb_worker.py`, `base.py`.
- `midi/` — `manager.py`, `document.py`, `codec.py`, `fade_engine.py`,
  `clip_editor.py`, `scene_editor.py`, `selection_editor.py`, `constants.py`,
  `worker.py`.
- `software_controller/` — `main.py`, `artnet_sender.py`, `usb_dmx_sender.py` (FTDI
  DMX512 TX: break/MAB/start-code, self-disabling), `scene_loader.py`. Banks/chases
  live in the `fixtures/*.json` defs (no `dmx.py`/`dmx_config.json`).
- `webremote/` — `server.py`, `manager.py`, Qt-free `state.py`/`commands.py`/
  `peaks.py`, `static/` (bundled via build.spec).
- `timeline/` — `manager.py`, `tracks/`, `widgets/`. `clip_edit` is NOT re-exported
  from `widgets/__init__.py` — it pulls in the stage render engine via
  `ui.fixture.console`, which the tracks themselves import; import it from
  `.widgets.clip_edit` directly.
- `tests/` — headless characterization tests, all GL-free (CI's offscreen platform
  cannot make a context). `ofl_factory.py` builds synthetic OFL documents so a
  capability nobody ships a fixture for can still be exercised;
  `test_capability_coverage.py`, `test_renderer_registry.py` and
  `test_stage_optics.py` carry the three tables that fail loudly when OFL gains a
  capability type, category or wheel-slot type nobody handled; `test_light_list.py`,
  `test_tonemap.py`, `test_fog_math.py`, `test_fog_medium.py` and
  `test_stage_features.py` pin the photometry, the ACES fit, the march landing inside
  the beam, smoke outliving its channel and the HAZE slider taking effect at once.
  NB **`[255] * channel_count` is not a "fixture on" frame** — on anything with a
  shutter it selects a strobe, so use `_lit_frame()`.
- `tupperlogger/` — custom logger with file rotation.

## Timeline Architecture Notes

`RulerTrack` / `WaveformTrack` / one `MidiEventsTrack` per `fixture_filter` extend
`BaseTrack`; Timeline exposes `PIXELS_PER_SECOND` and `fixture_at_y()` for drop
routing. Tracks separate **content items** (cleared on redraw) from **header items**
(persistent). Waveform/MIDI heights resize via `TrackResizeHandle` (persisted as
`window.waveform_track_height` / `window.midi_events_track_height`). Cross-component
sync via `MidiManager.data_changed` (events cached in `_cached_events`); scene groups
collapse to one `DraggableMidiEvent` via `build_render_units()`.

### Editing a clip (`timeline/widgets/clip_edit.py`)

**A clip is edited against the fixture it lives on, never against a table.** Its note
is a *verb*, its MIDI channel selects the fixture, its velocity is that verb's
parameter — so `Timeline._fdef(channel)` resolves the patch entry first and everything
follows from that `FixtureDef`. One dispatcher, `on_midi_event_edit_by_id` — group →
`_edit_scene`, note 60 → the untouched `StartEventEditDialog` (its velocity packs a
hue and a palette mode), else `ClipEditDialog`; `on_add_event_requested` is the third
door.

- **Two pages, one shell.** A scene group gets the `ui/fixture/console.py` console;
  any other clip gets its verb's own editor from `verb_editors.py`, picked by
  `editor_class_for(note)` off a table whose predicates mirror `handle_note_on`'s
  match arms. **Retyping resets the velocity** (`default_velocity_for`):
  channel-at-200 is velocity 100, which carried across would select chase #100.
- **The picker is `note_layout.verb_choices(fdef)`**, gated by the same
  `note_applies_to` drag-and-drop validates with, so the dialog can never author a
  clip a drop would have refused. Song parts are excluded on a fixture lane.
- **Only `present | touched` is submitted.** `present` is what the group had a member
  for on open; untouched channels stay out and render dimmed — submitting every fader
  would silently widen a scene, since an absent channel holds the last frame's value
  while a present one at its default *forces* it. Growth goes through
  `update_scene(..., remove=...)`, which never drops below 2 members.
- **Only a fixture's first 23 slots are reachable** (24 up are scene-load), so
  `field_note_map` caps there and `FaderStrip.set_addressable_limit` greys the tail.
- **Live output** (`live_messages` → `Timeline.preview_requested` →
  `AppCore.preview_clip`) is opt-in, never persisted, 30 ms-throttled, and locked out
  while the transport plays or the lane is muted/locked. Payloads **lead with note 48
  TAKE_MANUAL**. Cancel re-sends the opening values *once* — a re-send, not a restore.
- **"Save as scene…" writes the fixture library immediately**, independent of
  OK/Cancel — it edits a type every show shares. It then re-resolves `fdef` and emits
  `library_changed` so the clip-library panel rebuilds.
- Writes go through `move_event`/`move_scene` for the time (they clamp and keep the
  BLACKOUT markers honest, unlike `edit_event(time=...)`), then
  **`_schedule_refresh()` — never `refresh_from_manager()`**.

## Common Debugging

```bash
# Tail today's log
tail -f logs/tupperDMX_$(date +%Y%m%d).log

# Reset user prefs: delete userprefs.json from the platformdirs path above.
# Show data is NOT in prefs — each show is a self-contained <Name>.show/ folder
# (default under ~/Documents/TupperDMX/), with its own versions/ history.

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

- Timeline editing dialogs need both `QDialog` and `QMessageBox` imported; error
  dialogs require a parent widget reference for consistent styling.
