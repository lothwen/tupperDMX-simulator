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
  I/O routing).
- `app/` — the shared core: QtCore/QtMultimedia only, **never** QtWidgets or `ui/`.
- `audio/`, `dmx/`, `midi/`, `software_controller/`, `webremote/` — widget-free subsystems (`webremote`'s
  `state`/`commands`/`peaks` are Qt-free too).
- `stage/` (render engine) and `timeline/` are **top-level**: QtWidgets importing nothing from `ui/` (only `core/`, PyQt6,
  `tupperlogger`), which is what earns them the root.
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
`QAudioSink.stop()` on the GIL. `load_backing` dedupes an in-flight decode of the same path (restarting QAudioDecoder
mid-decode aborts). TTS is piper + a WAV cache: **nothing synthesizes during playback — cache-first always**.
`app/media_controller.py` owns the timeline position (`time.time()` master clock, engine slaved with 150 ms drift resync).

- **Count-in is negative timeline time.** With `song.precount`, playback begins at `-(bars × 4 beats)`; audio/MIDI/lights
  wait for t ≥ 0; pausing resets to 0 and seeking cancels it. AppCore speaks the numbers on the LAST count-in bar when
  `config.audio.count_in_voice` is on.
- **Guide track**: a song may carry a full-mix reference; the BACKING|GUIDE monitor picks which file feeds the engine. It
  is **session-only in-memory state** (always BACKING at launch), so headless/web-remote never inherit it.
  MIDI/DMX/metronome/cues are timeline-driven and identical under either.
- **Backing and guide are equal citizens**: either, both or neither. The timeline spans the **longest** of backing, guide
  and MIDI, declared once per load (`Timeline.set_song_tracks`); the web remote takes the same maximum
  (`webremote.state.song_duration`).
- **Metronome sync**: on start/resume compute beats elapsed and the remainder to the next boundary — play immediately if
  within `0.001s`, else a single-shot timer to the next beat, then regular intervals. Always restart on
  seek-during-playback or BPM change. Alignment is floor-based, so negative count-in positions number correctly toward
  beat 1 at t=0.

## I/O routing (`config.io` + `core/io_routing.py`)

All routing is explicit in Settings ▸ Input / Output — no toolbar selectors, no localhost magic. Applying `config.io` sets
MIDI routing and drives `DMXManager.apply_io(io)`, which owns the software `DMXController` lifecycle → its output *sinks*
→ the stage-input worker. With the defaults ("All software") the controller runs and feeds the stage **in-process** via
`StageFrameBridge` — no UDP loopback. Cardinalities: MIDI out 1→N (a stateful command protocol — fan-out fine, **fan-in
forbidden**), controller DMX out 1→N (idempotent snapshots), stage DMX in = exactly one selectable feed.

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
it must NOT set a version/profile or EGL fails; draw calls come from `QOpenGLFunctions_4_1_Core`. **`QWidget.grab()`
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
  user layer** when the source dir isn't persistable. The OFL document itself is never written.
- **`software_controller/`** — a pure-Python DMX controller (no hardware) managing a 96-channel universe patched from
  `config.fixtures`. `handle_note_on` uses a **generalized note map**: the pitch is a universal *verb*, the MIDI **channel
  selects the fixture** (its index in the patch). Layout: 1–23 channel-offset sets (velocity×2), 24–35 scene loaders,
  36–47 bank select, 48 take-manual / 49 manual / 50 blackout-fixture, 51 blackout-all, 52–59 timing/palette/scene step,
  60 START, 61–69 song parts, 72–75 colour macros. START has no `song_index` — velocity packs the palette (`hue_index =
  velocity // 6`; `mode = PALETTE_MODES[velocity % 6]`).
- **`webremote/`** — embedded tablet remote, off by default; plain HTTP on the stage LAN, no auth. aiohttp HTTP+WS on its
  own asyncio loop in a QThread; inbound commands execute on the Qt thread, cues/blackout/panic through `preview_clip` so
  `config.io` routing applies. Panic sends note 51 only when `blackout_all_state` is off (blackout is a toggle).

## Configuration & persistence (`core/config.py` + `core/show/`)

- **`config.json`** (shipped, read-only): the *seed* patch for brand-new shows.
- **`userprefs.json`** (user-writable, **MACHINE-level only** — a usable GL driver is a property of the box): `window`
  (incl. `stage_haze`, `stage_gpu`, `stage_exposure`, nested `fog`; deliberately no `fog_density`, because that IS
  `stage_haze`), `artnet`, `volume`, `logging`, `io`, `audio`, `webremote`, `last_show_path`/`recent_shows`. Legacy
  `playlist`/`fixtures` are parsed (the show migration adopts them) but never written again.
- **`<Name>.show/show.json`** (the SHOW manifest): the setlist, `current_song_index`, the fixture **patch**,
  `webremote_cues`. A patch entry is type + `dmx_start` + stable MIDI `channel` + optional `mode` (`""` means the type's
  first declared mode, so a renamed mode never strands a patch) + `hanging`, defaulting to **true** so a patch written
  before fixtures declared their rigging keeps throwing light at the floor. Owned `.mid` files live in `midis/`; audio is
  *referenced in place* unless it arrived in a bundle. **The `.mid` stays canonical** — extras plain MIDI can't carry
  (scene `group_id`, `scene_name`, per-clip fades) ride as `tdmx1:{json}` text metas; "Export MIDI…" writes an
  `interop=True` copy without them.

After `ShowManager.attach()`, `config.playlist`/`config.fixtures` ARE the show's objects. **userprefs.json is re-read
while the app runs** — `AppCore` watches it (`app/prefs_watcher.py`), because editing that file over the share is the only
way to change a setting on a headless rack box.

## UI notes

- **MIDI channels are 0-15 everywhere** — the raw MIDI channel, on every surface.
- The fixture browser's footer is the patch decision; OK stays disabled until the patch is legal, and the inspector
  validates the same two things.
- `ui/fixture/scenes_tab.py` edits the fixture **library** (a type's sidecar scene banks), not the patch — the dialog's
  OK/Cancel doesn't apply; one button writes. Its tree keeps **bank sections** with scenes beneath, because both orders
  are what MIDI addresses (36+n bank, 24+n scene); reorder is applied to the data, never by letting Qt move the items
  (their roles go stale).
- `ScenePreview` (`ui/fixture/console.py`) **is** the stage engine, fed raw DMX, so a look previews exactly as on stage;
  **`clear()` it before it goes away** — the bodies own timers on the shared clock.
- `ui/fixture/affordances.py` — controls chosen from a fixture's capabilities and **never from its name**.

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

Reset user prefs by deleting `userprefs.json` from the platformdirs path. Show data is NOT in prefs — each show is a
self-contained `<Name>.show/` folder with its own `versions/` history.
