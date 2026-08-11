# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository. It records
what you **cannot** derive by reading the code — layering rules, invariants, and the
reasons behind decisions that look arbitrary. It is not an index of files or functions.

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

Build/distribution and CI details live in `BUILD.md`. Each test file is also runnable
standalone via its `main()`.

**PyQt6 is pinned to 6.8 LTS** — 6.11 crashed QMediaPlayer on pause/resume; playback no
longer uses QMediaPlayer, but QAudioSink/QAudioDecoder ride the same stack. TTS voice
models (~60 MB) download on demand into `userprefs_dir()/tts_voices/`, never bundled.

## Packages and layering

- `main.py` — the only root source file: a thin entry point that parses `--headless`
  **before any Qt import** (it sets `QT_QPA_PLATFORM=offscreen`).
- `core/` — the **Qt-free** domain layer (config, show/, paths, OFL parsing, the
  fixture library and state model, note layout, I/O routing).
- `app/` — the shared core: QtCore/QtMultimedia only, **never** QtWidgets or `ui/`.
- `audio/`, `dmx/`, `midi/`, `software_controller/`, `webremote/` — the widget-free
  subsystems (`webremote`'s `state`/`commands`/`peaks` are Qt-free too).
- `stage/` (the render engine) and `timeline/` are **top-level**: they are QtWidgets,
  but import nothing from `ui/` (only `core/`, PyQt6, `tupperlogger`), which is what
  earns them the root.
- `ui/` — the desktop view in three layers: `ui/widgets/` (leaf toolkit, no app
  knowledge, safe for anyone to import), `ui/fixture/` (the fixture-editing subsystem —
  the timeline reaches in for `console` and `affordances`), and the root.
- `fixtures/` — shipped definitions: a strict OFL `*.json` + optional `*.tdmx.json`
  sidecar. A **subfolder is the manufacturer** and namespaces the key
  (`fixtures/ADJ/focus-spot-4z.json` → `"ADJ/Focus Spot 4Z"`); a file at the root
  keeps its **bare** key, which is what saved patches refer to.

**No eager re-exports at any level; import the submodule you want.** `ui/__init__.py`
is a docstring because re-exporting the window and its dialogs meant
`from ui.widgets import icons` — which the timeline tracks do — dragged in the stage
engine, the MIDI worker, the DMX manager and the web remote (70+ modules to import an
icon factory). For the same reason `timeline/widgets/__init__.py` does **not**
re-export `clip_edit`.

## Architecture

### Orchestration

`app/core.py` (`AppCore`) is the ONE orchestration object: it constructs and wires
every widget-free collaborator, owns the count-in/voice-cue logic, `preview_clip`, the
transport, `save_config` and the idempotent `shutdown()` (flush FIRST, then
webremote→media→audio→engine→dmx→midi). `ui/main_window.py` is the desktop *view* over
it — it builds the widget tree, aliases the core's collaborators onto itself and
connects the widget halves, which ride AppCore signals. Headless force-enables the web
remote in memory (never persisted) and never downloads TTS voices.

### The Show project layer (`core/show/` + `app/show_manager.py`)

A show is a **folder** `Name.show/` — `show.json` plus owned `midis/`, `audio/` (for
imported bundles), show-local `fixtures/` and `versions/`. `core/show/` is Qt-free:
owned midis are stored relative and referenced audio absolute, **missing paths are
preserved** for relinking, loading a show activates its fixture-defs layer, and
`.showpack` bundles collect ALL audio, backings AND guides.

`ShowManager` (QtCore-only, **attach FIRST** in `AppCore.__init__`) **aliases**
`config.playlist`/`config.fixtures` onto the show's objects, so every reader mutates
the show in place. Persistence funnels through `save_config()` → `notify_changed()`
(saved iff content changed) and `flush()` (~3 s debounced `.mid` autosave, forced on
song switch/close/Save; opening snapshot + at most one automatic snapshot per 5 min).

### Audio and the transport

**ONE stereo QAudioSink feeds every path.** The Qt-free mixer carries backing/click/
voice buses with per-bus volume/mute/RMS and the L/R routing modes `stereo` |
`split_backing_left` | `split_backing_right`. The engine is a **push-mode** sink with
a 15 ms Qt-thread timer topping up a ~120 ms buffer — push on purpose, because a
Python pull-QIODevice deadlocks inside `QAudioSink.stop()` on the GIL. `load_backing`
dedupes an in-flight decode of the same path, since restarting QAudioDecoder mid-decode
aborts. TTS is piper + a WAV cache: **nothing synthesizes during playback — cache-first
always**, every path degrading to None when piper or the voice is missing.

`app/media_controller.py` owns the timeline position (a `time.time()` master clock;
the engine is slaved with 150 ms drift resync).

- **Count-in is negative timeline time.** With `song.precount` on, playback begins at
  `-(bars × 4 beats)`; audio/MIDI/lights wait for t ≥ 0; pausing during the count-in
  resets to 0 and seeking cancels it. AppCore speaks the numbers on the LAST count-in
  bar when `config.audio.count_in_voice` is on.
- **Guide track**: a song may carry a full-mix reference and the BACKING|GUIDE monitor
  picks which file feeds the engine. The monitor is **session-only in-memory state**
  (always BACKING at launch), so headless/web-remote can never inherit it.
  MIDI/DMX/metronome/cues are timeline-driven and identical under either.
- **Metronome sync**: on start/resume, compute the beats elapsed and the remainder to
  the next boundary — play immediately if within `0.001s`, else a single-shot timer to
  the next beat then regular intervals. Always restart on seek-during-playback or BPM
  change. Beat alignment is floor-based, so negative count-in positions number correctly
  toward beat 1 at t=0.

### I/O routing (`config.io` + `core/io_routing.py`)

All routing is explicit in Settings ▸ Input / Output — no toolbar selectors, no
localhost magic. Applying `config.io` sets the MIDI routing and drives
`DMXManager.apply_io(io)`, which owns the software `DMXController` lifecycle → its
output *sinks* → the stage-input worker. With the defaults ("All software") the
controller runs and feeds the stage **in-process** via `StageFrameBridge` — no UDP
loopback. Cardinalities: MIDI out 1→N (a stateful command protocol — fan-out is fine,
**fan-in is forbidden**), controller DMX out 1→N (idempotent snapshots), stage DMX in =
exactly one selectable feed.

### The render engine (`stage/`)

**Two stages, in this order — the OFL *category* picks the body and the kind of light
it makes; the *capabilities* its channels declare pick the layers that shape that
light.** All 17 categories and all 43 capability types resolve.

- `registry.renderer_for(fdef)` maps a category to a **profile** — never a type name.
  The fixture's own category order wins (OFL lists the most important first), falling
  back to the shape of its channels (makes fog ⇒ hazer, pans ⇒ mover).
- `optics.py` — **what kind of light a fixture throws**: `BeamKind` is `NONE` (a
  surface light, no shaft in the air), `WASH` or `HARD`. `beam_kind()` decides
  **whether** there is a shaft and `resting_half_angle()` **how wide** — kept apart on
  purpose, since conflating them drew an ARRI SkyPanel with a 57.5° beam. A `stage.beam`
  sidecar hint wins outright, then beam-shaping evidence, then the declared lens (a
  spread ≥2.5× read as a **zoom range** rather than a gate), else the profile's
  `beam_fallback`. **What is NOT evidence matters as much**: `Frost` rides on wash
  heads, a bare `WheelSlot` is whatever its slots are, and `physical.lens.degreesMinMax`
  is the beam angle every emitting fixture has — reading any of them as hardness drew 32
  wash movers as spots. `test_stage_optics.py` carries the archetype table.
- `profiles/` declares only the housing, its origins, its emission and its
  `beam_fallback`; **it never reads a capability.** `features/` are the capability
  layers mutating a shared `BeamParams`, **discovered once at construction** — so a
  "Moving Head" that is really a wash simply has no gobo layer. Graceful degradation,
  no per-fixture code anywhere.
- `geometry.Aim`: a fixture is aimed in a **room** (a unit vector from pan/tilt) and
  only then flattened onto the front elevation, so `(axis_deg, foreshorten)` IS that
  vector and the fog march can rebuild it. **Home (half travel, DMX 127/128) is
  straight down**; **tilt swings the beam and pan turns the plane it swings in** — at
  tilt home *pan cannot move a beam at all*. `PanTiltFeature` slews the pan/tilt, never
  the projection, so a yoke can never have turned where its beam did not.
- **Rigging** (`FixtureInstance.hanging`, persisted): flown = housing upside down +
  beam at the floor. **Every profile draws its chassis the way it stands on the deck,
  into `body.housing`** — the one item a flown fixture turns 180°, **never the body
  item**: the GPU offsets each body's lights by its *position* alone, so a turned body
  would send the raster and the shader two different beams; the light's half of the flip
  is `Aim`'s. `add_fixture` *guesses* rigging from where the fixture lands and writes it
  down — a guess re-evaluated live would turn a fixture over mid-drag.
- `metrics.preview_def` **throws the real scale away**, normalising every body to one
  size, because a **beam is 1220 units and a halo caps at 400 whatever body made them**
  — real dimensions put a scanner head inside its own beam and left an ARRI panel no
  room for its glow. (A raster halo is the item's bounding rect, so it must move
  through `GlowingEllipse.set_glow_radius` or the old halo stays painted.)
- `clock.py` is ONE 30 Hz timer for everything that animates, subscribed weakly.

**Atmosphere**: a beam is invisible in clean air, so visibility is the one genuinely
cross-fixture quantity. `StageController` owns one `Atmosphere`; any body resolving a
fog output contributes, sources combine without exceeding full, and the level drifts on
the shared clock (~1.5 s attack, ~12 s decay; `Fog` weighs 0.6 against `Haze`'s 1.0).
The manual baseline `config.window.stage_haze` goes through `set_haze_baseline` →
`Atmosphere.settle` (**straight to the target, no drift**) → `refresh_air()`; **both
halves are load-bearing**, since a body reads `atmosphere.level` only on the frame it
draws.

**`puffs.py` — smoke that outlives its channel.** Parcels carry a birth time, so a
machine switched off does not clear the stage. **Every quantity is derived from a
parcel's age, never integrated**, so the field looks the same at 30 Hz or twice an
hour: it rises and *stops* rising, disperses as `sqrt(2·K·age)` in its own direction (a
random walk — a constant velocity biased away from centre was the reported "two-branch
tree"), and curls with X and Y **decorrelated** (one shared sine is a parcel sliding
down a diagonal). **Both renderers read the same field and the same `PARCEL_*` shape** —
a parcel drawn as a clean Gaussian is a *sphere*, and the eye finds the circles. Qt
composites `SourceOver`, so parcels **saturate rather than add**: alpha does not stand
in for count, and over-compensating draws separate balls.

**`light.py` — the GPU's view of the same frame.** A brush is 8-bit sRGB: it cannot
hold "twice as bright as white" and two overlapping do not add, so every emission also
describes its light as data — `LightPrimitive`s (POINT / CONE / QUAD / LINE / BLOB) in
one growable `array('f')`. They are **body-local** (dragging needs a repaint, not a
re-render), colour is **linear** and `intensity` is **unbounded**, which is how two
lights add past full instead of clipping. Haze is deliberately NOT baked in; it reaches
the shader as a uniform. `cone_geometry()` is shared by the raster cone and the light
list so the two cannot drift. A **LINE is thinner than the pixel that draws it** and GL
has no coverage, so the vertex shader widens its quad to `MIN_LINE_HALF_PX` device
pixels and hands the brightness back — widen without dimming and a fan is a smear; do
neither and it rasterises to dashes.

#### `gpu/` — the optional HDR renderer

`config.window.stage_gpu`, off by default. `StageView.setViewport(QOpenGLWidget())`
with the light composited in `drawForeground` inside `beginNativePainting()`: every
primitive accumulates into an RGBA16F FBO with `glBlendFunc(GL_ONE, GL_ONE)` — light
**adds** — then exposure, an ACES filmic tonemap and an additive composite. `_restore()`
puts blend/scissor/viewport back, since `endNativePainting` only restores what Qt
tracks. In GPU mode the raster stops drawing **spill** while faces stay lit. Everything
fails soft to raster and logs once. Four constraints, documented in `gpu/layer.py`: a
`QOpenGLWidget` *parented into* the viewport renders opaque and hides the scene; the
format must `setRenderableType(OpenGL)` or Wayland/EGL hands out GL ES where nothing
compiles; it must NOT set a version/profile or EGL fails outright; draw calls come from
`QOpenGLFunctions_4_1_Core`. **`QWidget.grab()` cannot read back a GL viewport** —
scripted screenshots need raster.

**`gpu/fog.py` — volumetric fog** (`config.window.fog.enabled`, off by default, needs
`stage_gpu`; after the Settings dialog, `MainWindow` must call
`StageController.apply_stage_config()` because it hands back a **new**
`StageFogConfig`). It sits between accumulate and resolve, so fog lands in the HDR
buffer *before* the tonemap and is simply more light. Three passes: a quarter-scale
RG16F **medium map** (an eighth aliased the per-parcel noise; depth extent is stored
density-**weighted** so it averages rather than sums), the **march**, and a **temporal**
average. The projection is orthographic, so a pixel's view ray needs no depth buffer,
and cost follows lit area — each light gets **one bounded quad**.

- **`STRIDE` stays 16**: a BLOB's `spin` and `variant` ride in the
  `half_width`/`softness` slots a parcel has no use for.
- **Lights and media share ONE instance buffer**, media packed after lights, which is
  why `_upload_instances()` is its own step and not part of the light pass: behind
  `_accumulate`'s `count == 0` early return, a stage whose only emitter was a fog
  machine uploaded nothing and the medium pass drew out of a buffer that had never been
  allocated — a segfault.
- **The segment is solved, not swept.** A beam is a thin sheet in depth (~66 units)
  while an even march steps ~58, so the shaft never appeared: each ray is clipped to the
  bounding-sphere chord, then to the cone or the laser's tube. A `FOG_POINT_REACH` of 10
  lamp-radii (**not** the sprite's `GLOW_REACH` of 60) calibrates it.
- **The air is ragged and drifts** — two `turbulence()` octaves clamped at zero, the
  holes being what read as volume. Since it flows, `_frame_signature` excludes time (it
  would make `_settle` reset its own countdown); `air_is_drifting()` answers instead.
  **The baseline takes all of `turbulence()`; a parcel takes 0.4 of it**, because smoke
  arrives from the medium map already ragged and drifting with the machine that made
  it — a second field on its own current made GPU smoke read as a texture sliding behind
  blobs. **Ambient** lights baseline haze at 0.09: not full (the backdrop is already a
  lit room), not zero (or HAZE shows nothing without a shaft).
- **Three** fog framebuffers, not two: averaging A into B while sampling B is a feedback
  loop that ramped fog to white over four frames. `QOpenGLFramebufferObject` also hands
  out **GL_NEAREST**, so `_smooth()` must set GL_LINEAR. And **an empty light list is
  not an empty frame**: the guard also asks `_air_worth_drawing()`, and `_accumulate`
  still clears the HDR buffer or a dark stage keeps the last lit one.
- **The stage clip is switched OFF for the march** (GL scissors in absolute framebuffer
  pixels while the clip is in *surface* pixels, and every fog target is a different
  size); clipping is `_resolve`'s job. The march is dithered, so `request_repaint` asks
  for `CONVERGE_FRAMES` more frames after the rig settles **and then stops** — the only
  thing here that can spin; that countdown is reset by `_frame_signature()`, **not** by
  the march running.

### Domain layer

- **`core/fixture_state.py`** — the Qt-free photometric model covering **all 43 OFL
  capability types**. **Emitters** come from matrix pixel keys, matrix groups, or — with
  no matrix — repeated channels, which is how a multi-cell blinder resolves without
  special-casing. `capability_at(fdef, index, values)` is the ONE "what is this channel
  doing right now"; it takes the whole slice on purpose, because a switching channel's
  meaning lives on another slot. `solve_color(fdef, rgb)` is the **inverse** the
  controller's palettes write through — inverting the SAME model, so what the controller
  writes is what the stage draws.
- **`core/ofl.py`** — the Open Fixture Library (12.5.1) parser. `resolve_mode()`
  flattens one mode to ordered DMX slots with matrix inserts expanded, fine channels
  linked to their coarse channel, and unresolvable slots **kept in place so they still
  consume an address**. A switching channel's `channel` holds the resolution at the
  trigger's default, so labels stay static while `fixture_state` picks the live one per
  frame. Deliberately lenient: unknown capability types are preserved, not rejected.
- **`core/fixture_library.py`** — a definition is **one or two files**:
  `fixtures/<x>.json` (strict OFL) plus an optional `fixtures/<x>.tdmx.json` sidecar
  carrying what OFL cannot express (scene `banks`, `chases`, `song_parts`, per-channel
  `label`/`flags`, `stage` hints). **What a channel *does* is never declared there**,
  and a fixture straight from OFL works with no sidecar. Loaded in three layers —
  shipped, `userprefs_dir()/fixtures/`, the open show's — later layers overriding by
  key, memoized, and **sidecars never cross layers**. **A channel has no summarised
  description and there is no role enum**: everything reads its capabilities.
  `save_fixture_banks()` is the ONE writer: it round-trips the **raw** sidecar JSON so
  unmodelled keys survive, and **forks the definition into the user layer** when the
  source dir isn't persistable. The OFL document itself is never written — a type's DMX
  footprint is untouchable from the editor.
- **`software_controller/`** — a pure-Python DMX controller (no hardware needed)
  managing a 96-channel universe patched from `config.fixtures`. `handle_note_on` uses a
  **generalized note map**: the pitch is a universal *verb* and the MIDI **channel
  selects the fixture** (its index in the patch). Layout: 1–23 channel-offset sets
  (velocity×2), 24–35 scene loaders, 36–47 bank select, 48 take-manual / 49 manual /
  50 blackout-fixture, 51 blackout-all, 52–59 timing/palette/scene step, 60 START,
  61–69 song parts, 72–75 colour macros. START has no `song_index` — its velocity packs
  the palette (`hue_index = velocity // 6`; `mode = PALETTE_MODES[velocity % 6]`).
- **`webremote/`** — embedded tablet remote, off by default; plain HTTP on the stage
  LAN, no auth. aiohttp HTTP+WS on its own asyncio loop in a QThread; inbound commands
  execute on the Qt thread, and cues/blackout/panic go through `preview_clip` so
  `config.io` routing applies. Blackout is a toggle: panic sends note 51 only when
  `blackout_all_state` is off.

### Configuration & persistence (`core/config.py` + `core/show/`)

Three files, with a strict split of what belongs where:

- **`config.json`** (shipped, read-only): the *seed* patch for brand-new shows.
- **`userprefs.json`** (user-writable, **MACHINE-level only** — whether there is a
  usable GL driver is a property of the box): `window` (incl. `stage_haze`,
  `stage_gpu`, `stage_exposure` and the nested `fog`; there is deliberately no
  `fog_density`, because that IS `stage_haze`), `artnet`, `volume`, `logging`, `io`,
  `audio`, `webremote` and `last_show_path`/`recent_shows`. Legacy `playlist`/`fixtures`
  are still parsed (the show migration adopts them) but never written again.
- **`<Name>.show/show.json`** (the SHOW manifest): the setlist, `current_song_index`,
  the fixture **patch** and `webremote_cues`. A patch entry is type + `dmx_start` +
  stable MIDI `channel` + optional `mode` (where `""` means the type's first declared
  mode, so a renamed mode never strands a patch) + `hanging`, which defaults to **true**
  so a patch written before fixtures could say which way up they are rigged keeps
  throwing light at the floor. Owned `.mid` files live in `midis/`; audio is *referenced
  in place* unless it arrived in a bundle. **The `.mid` stays canonical** — extras plain
  MIDI can't carry (stable scene `group_id`, `scene_name`, per-clip fades) ride as
  `tdmx1:{json}` text metas; "Export MIDI…" writes an `interop=True` copy without them.

After `ShowManager.attach()`, `config.playlist`/`config.fixtures` ARE the show's
objects.

### UI notes

- **MIDI channels are 0-15 everywhere** — the raw MIDI channel, on every surface.
- The fixture browser's footer is the patch decision; OK stays disabled until the patch
  is legal, and the inspector validates the same two things.
- `ui/fixture/scenes_tab.py` edits the fixture **library** (a type's sidecar scene
  banks), not the patch — so the dialog's OK/Cancel doesn't apply; one button writes.
  Its tree keeps **bank sections** with their scenes beneath, because both orders are
  what MIDI addresses (36+n bank, 24+n scene) — never flattened; reorder is applied to
  the data, never by letting Qt move the items (their roles go stale).
- `ScenePreview` (`ui/fixture/console.py`) **is** the stage engine, fed raw DMX, so a
  look previews exactly as on stage; **`clear()` it before it goes away** — the bodies
  own timers on the shared clock.
- `ui/fixture/affordances.py` — controls chosen from a fixture's capabilities and
  **never from its name**.

## Timeline and clip editing

- Tracks separate **content items** (cleared on redraw) from **header items**
  (persistent). Cross-component sync rides `MidiManager.data_changed`; scene groups
  collapse to one `DraggableMidiEvent` via `build_render_units()`.
- Clip drags have three modes (MOVE, RESIZE_LEFT, RESIZE_RIGHT) with neighbour clamping
  and zoom-aware edge-snap. **Persistence happens only on `mouseRelease`** via
  `*_committed` signals; mid-drag is purely visual.
- **BLACKOUTs are gap markers**, scoped per fixture and identified by part-name
  **PREFIX** (case-insensitive), *not* note number. `reconcile_blackouts()` forbids
  zero-duration/adjacent BLACKOUTs and reinserts them to keep clip extents; deleting a
  regular clip converts it to a fixture-matching BLACKOUT, not a hole.
- Scene clips are 7 simultaneous `note_on` events sharing a `group_id`, rendered as one
  clip; drags route to the scene API.

**A clip is edited against the fixture it lives on, never against a table.** Its note is
a *verb*, its MIDI channel selects the fixture, its velocity is that verb's parameter —
so `Timeline._fdef(channel)` resolves the patch entry first and everything follows from
that `FixtureDef`.

- A scene group gets the console; any other clip gets its verb's editor from
  `verb_editors.py`, off a table whose predicates mirror `handle_note_on`'s match arms.
  **Retyping resets the velocity**: channel-at-200 is velocity 100, which carried across
  would select chase #100. The picker is gated by the same `note_applies_to`
  drag-and-drop validates with, so the dialog can never author a clip a drop would have
  refused.
- **Only `present | touched` is submitted.** Submitting every fader would silently widen
  a scene, since an absent channel holds the last frame's value while a present one at
  its default *forces* it. Growth goes through `update_scene(..., remove=...)`, which
  never drops below 2 members.
- **Only a fixture's first 23 slots are reachable** (24 up are scene-load), so
  `field_note_map` caps there and the fader strip greys the tail.
- **Live output is opt-in**, never persisted, 30 ms-throttled, and locked out while the
  transport plays or the lane is muted/locked. Payloads **lead with note 48
  TAKE_MANUAL**, or the auto/song-part program overwrites them next frame. Cancel
  re-sends the opening values *once* — a re-send, not a restore.
- **"Save as scene…" writes the fixture library immediately**, independent of
  OK/Cancel — it edits a type every show shares.
- Writes go through `move_event`/`move_scene` for the time (they clamp and keep the
  BLACKOUT markers honest, unlike `edit_event(time=...)`), then **`_schedule_refresh()`
  — never `refresh_from_manager()`**.

## Testing notes

Tests are headless and GL-free (CI's offscreen platform cannot make a context).
`ofl_factory.py` builds synthetic OFL documents so a capability nobody ships a fixture
for can still be exercised. `test_capability_coverage.py`, `test_renderer_registry.py`
and `test_stage_optics.py` carry the three tables that fail loudly when OFL gains a
capability type, category or wheel-slot type nobody handled.

NB **`[255] * channel_count` is not a "fixture on" frame** — on anything with a shutter
it selects a strobe, so use `_lit_frame()`.

## Common Debugging

```bash
# Tail today's log
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

Reset user prefs by deleting `userprefs.json` from the platformdirs path. Show data is
NOT in prefs — each show is a self-contained `<Name>.show/` folder with its own
`versions/` history.
