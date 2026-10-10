# Stage

*The stage draws the patched rig in real time from DMX. It is a front-elevation picture of a room: a photograph for a
backdrop, with every patched fixture drawn on it from its OFL definition. Nothing is drawn by fixture name. The OFL
category picks the body and the kind of light it makes. The capabilities its channels declare pick the layers that
shape that light. The same engine draws the scene preview in the fixture dialogs.*

There are two renderers. The **raster path** (QGraphicsScene items painted with 8-bit brushes) always runs. The
optional **GPU path** (`config.window.stage_gpu`) adds every fixture's light into one HDR buffer, tonemaps it, and can
march volumetric fog through it.

This document covers `stage/` except `stage/stage3d/`, the experimental 3D bar prototype (see
`docs/3d-stage-draft.md` and `docs/3d-stage-phase-2b.md`).

Written 2026-10-04 against `betatest1-fixes` (commit `891bade`).

---

## 1. What it does

For every DMX frame:

1. `MainWindow.update_dmx` hands the 512-byte universe to `StageController.update_fixtures_with_dmx`.
2. The controller cuts out each patched fixture's slice. A slice identical to the previous frame's is skipped.
3. The slice is resolved once per fixture into a `FixtureState` (`core.fixture_state.resolve_state`): intensity,
   colour, emitters, optics, motion, strobe, program, fog output and wheels.
4. Each body of the fixture runs its pipeline. It resets a `BeamParams`, lets each capability feature modify it, and
   hands it to the emission. The emission repaints its scene items and writes the same light as data into a
   `LightList`.
5. If the GPU layer is on, the view repaints once. The layer accumulates every body's light list, marches fog and
   tonemaps the result over the scene.

Between DMX frames, a shared 30 Hz clock re-renders the bodies that animate on their own (a strobe, a spinning gobo, a
slewing head, a plume) and drifts the haze level.

```
DMX universe (512 bytes)
  │ StageController.update_fixtures_with_dmx    one slice per patched fixture; unchanged slices skipped
  ▼
FixtureState   core.fixture_state.resolve_state, once per fixture
  │ BodyFixture.set_state                        every body of that fixture
  ▼
BeamParams.reset(resting half-angle)             optics.resting_half_angle
  │ Feature.apply × N, by `order`                dimmer, colour, wheels, shutter, zoom … pan/tilt
  ▼
Emission.render(frame)        ──▶ QGraphicsItems (raster: lamps, cones, cells, plume)
Profile.paint_housing(frame)  ──▶ chassis tint, yoke / head / mirror transforms
Emission.emit_lights(frame)   ──▶ LightList (linear float, body-local)
  │ _light_sink → viewport().update()            GPU path only
  ▼
LightLayer.render   pack → accumulate (HDR) → fog march → temporal → ACES resolve, added over the scene
```

---

## 2. Where the code lives

| File | Qt | What it does |
|---|---|---|
| `stage/__init__.py` | — | Imports every profile and feature so they register, and re-exports the engine's public names. |
| `stage/registry.py` | none\* | `renderer_for(fdef)`: OFL category → profile class, with fallbacks from the channels. |
| `stage/optics.py` | none | `BeamKind` and `beam_kind()`: does the fixture throw a shaft, and is it soft or hard. `resting_half_angle()`: how wide. |
| `stage/metrics.py` | none\* | Body size, body count, spacing and cell count from the definition. `preview_def()` for the scene preview. |
| `stage/frame.py` | none | `BeamParams` (features write it, emissions read it) and `Frame`. |
| `stage/geometry.py` | none | `Aim` (pan/tilt → picture), `Slew`, `Spinner`. |
| `stage/atmosphere.py` | none | The stage-wide haze level. |
| `stage/puffs.py` | none | Smoke parcels with a lifetime, the `HAZER` and `SMOKE` specs, and the parcel shape constants. |
| `stage/light.py` | none | `LightList`: light as primitives for the GPU. |
| `stage/clock.py` | QtCore | `CLOCK`, the one 30 Hz animation timer. |
| `stage/body.py` | QtWidgets | `BodyFixture` (the pipeline), `DesignableFixture` (designer mixin) and `FixtureLabel`. |
| `stage/glow.py` | QtWidgets | `GlowingEllipse`, the lamp with a radial halo, and its two falloff curves. |
| `stage/wheelart.py` | QtGui | Gel colours and procedural gobo patterns from wheel slots. |
| `stage/features/` | none / QtGui | Capability layers: `level.py` (dimmer, colour, shutter), `wheels.py` (colour wheel, gobo), `beamshape.py` (zoom, iris, frost, prism, blades, pan/tilt). |
| `stage/emission/` | QtWidgets | How light is drawn: `cone.py` (lamps, wash and hard beams, flower), `surface.py` (matrix cells, bulbs, panel), `volumetric.py` (laser fan, fog plume). |
| `stage/profiles/` | QtWidgets | What a fixture looks like, per category: `lamp.py`, `array.py`, `motorised.py`, `atmospheric.py`. `chassis.py` paints the housings. |
| `stage/controller.py` | QtWidgets | `StageController` (scene, patch sync, designer, haze and GPU switches) and `StageView` (the `QGraphicsView`). |
| `stage/debug_overlay.py` | QtWidgets | The fps / paint-cost / GPU readout shown at DEBUG log level. |
| `stage/gpu/layer.py` | QtGui, QtOpenGL | `LightLayer`, the HDR pass, and the GL surface-format setup. |
| `stage/gpu/fog.py` | QtGui, QtOpenGL | `FogPass`: medium map, ray march and temporal history. |
| `stage/gpu/shaders.py` | none | The GLSL, as Python strings. |

\* Imports Qt modules lazily, inside a function.

Outside `stage/`:

| File | What it does with the stage |
|---|---|
| `ui/main_window.py` | Builds `StageController`, forwards DMX, applies Settings and live prefs reloads, docks and undocks the view. |
| `ui/fixture/designer.py` | The edit-mode toolbar (HAZE, EXPOSURE and FOG controls) and the fixture inspector. |
| `ui/fixture/console.py` | `ScenePreview`: the same engine drawing one fixture from a scene's values. |
| `main.py` | Sets the GL surface format before `QApplication` when `stage_gpu` is on. |
| `core/fixture_state.py` | `resolve_state()`, the photometric model every frame starts from. |
| `core/fixture_sidecar.py` | Reads the sidecar's `stage` hints (§17). |
| `core/config.py` | `stage_haze`, `stage_gpu`, `stage_exposure` and `StageFogConfig`. |

### Layering

- **`stage/` imports only `core/`, PyQt6 and `tupperlogger`.** It never imports `ui/`, `dmx/` or
  `software_controller/`. That is what lets it live at the top level.
- **Importing `stage` registers everything.** `stage/__init__.py`, `stage/profiles/__init__.py` and
  `stage/features/__init__.py` import their modules for the `@register_renderer` and `@register_feature` side effects.
  This is the one place in the app where a package imports its submodules eagerly, and it is on purpose.
- **`stage.gpu` is not imported eagerly.** It pulls in `QtOpenGL`, which the raster path, the tests and a headless run
  never need. Import `stage.gpu.layer` where it is used.
- **`optics.py` may import only from `core/`.** Importing a profile or an emission there would close the
  `registry → profiles → registry` cycle that `registry._CHANNEL_SHAPE_FALLBACKS()` works around with a lazy import.
- **Profiles never read a capability.** Features read capabilities. Emissions read `BeamParams` and `FixtureState`.
  Profiles draw a housing and pick an emission. No code anywhere names a fixture type.

---

## 3. Coordinates and scale

- **Scene units are backdrop pixels.** The scene rect is the background image's size:
  `assets/images/stage_background.png`, 1536 × 1024. With no image it falls back to that same size, so saved fixture
  positions stay valid. `StageView` fits the whole scene into the widget with `fitInView(KeepAspectRatio)` and fills
  the letterbox in black.
- **Angles are degrees in the picture, with Y growing down.** 0° points right and 90° points straight down. 90°
  (`STRAIGHT_DOWN_DEG`) is where every beam pointed before fixtures could aim, so a fixture with no pan or tilt still
  points there.
- **Body size** (`metrics.body_size`): a `stage.size_px` hint wins. Otherwise the OFL dimensions in mm are divided by
  `MM_PER_STAGE_PX` = 2.5 and clamped to 12–640 units. A fixture with no dimensions is 200 mm square. The scale is
  anchored on the LED bar: 1000 mm tall draws as 400 units.
- **Bodies per fixture** (`metrics.body_count`): one, unless a `stage.bodies` hint says otherwise or the fixture is a
  matrix whose pixels are spaced more than 1.5× their own width apart (`SEPARATE_BODY_RATIO`). In that case each pixel
  column is its own housing. The shipped LED bar is eight bars on one DMX slice. A 24-pixel bar or a 5×5 flower is one
  body.
- **Cells on one face** (`metrics.declared_cell_count`): a `stage.cells` hint, else the most times any one
  `ColorIntensity` colour repeats across the channels. That is how a two-cell blinder gets two bulbs before any DMX has
  arrived.
- **How far light reaches**: a beam is `BEAM_LENGTH_PX` = 1220 units and a laser 1.4× that. A plume reserves 520
  (`PLUME_REACH_PX`). A raster halo is capped at a radius of 400 units (`cone.MAX_GLOW_RADIUS_PX`) and a GPU halo at
  800 (`layer.MAX_GLOW_RADIUS_PX`).
- **The scene preview throws the real scale away.** `metrics.preview_def` resizes every body so its longer side is
  15 % of its own reach (340 units if it throws nothing), keeping its aspect ratio and spacing. The light is drawn in
  absolute units, so at real scale a 20 mm scanner head disappears inside its beam and a 4 m panel leaves no room for
  its glow.

---

## 4. A body and its frame (`body.py`)

`BodyFixture` is one drawable body of one patched fixture. It is a `QGraphicsObject` with the `DesignableFixture`
mixin in front of it in the MRO.

### 4.1 Construction

In this order:

1. Size from `metrics.body_size`. Rigging comes from the patch (`hanging`), and is forced to standing when the
   profile has `rigs_inverted = False`.
2. An `Aim` built from the fixture's declared pan and tilt ranges. `default_half_angle` comes from
   `optics.resting_half_angle(fdef, profile.beam_kind(fdef))`.
3. `self.housing`, a `QGraphicsItemGroup`. For a flown body it is turned 180° about its centre. The profile's
   `build_housing()` draws the chassis into it, standing on the deck.
4. The emission: `profile.emission_for(fdef)(self)`. It builds its own child items.
5. The features: `build_features(fdef, self)`. They are chosen **once**, from the capability types the definition
   declares.
6. An empty `LightList`, and a fog flag if the definition declares `Fog`, `FogOutput` or `FogType`.
7. A clock subscription, only if the emission, the profile or any feature animates.

### 4.2 One frame (`render()`)

1. Fill the reusable `Frame`: the state, `haze = atmosphere.level` and `t = CLOCK.now()`. Reset the `BeamParams` to
   the resting half-angle.
2. Run every feature's `apply(frame)`, in `order`.
3. Set `beam.visible = beam.level > 0`.
4. If the fixture makes fog, report its output to the atmosphere.
5. Call `emission.render(frame)`, then `paint_housing(frame)`.
6. Clear the light list, call `emission.emit_lights(frame, lights)`, and call the light sink if the GPU layer set one.

`BeamParams` and `LightList` are reused frame to frame on purpose. At 20–100 Hz across a rig, allocation is the cost
that matters.

A body re-renders when:

- its fixture's DMX slice changes (`set_state`);
- the clock ticks and the body animates;
- the haze baseline changes (`StageController.refresh_air`);
- its light moves between raster and GPU (`set_light_on_gpu`).

### 4.3 Cleanup

`cleanup()` unsubscribes from the clock, releases the atmosphere contribution, clears the light sink and drops the
back-references between the body, its emission and its features. Without that, the body sits in a reference cycle
until a cyclic collection runs at some arbitrary moment. Freeing a `QGraphicsObject` whose children Qt already
deleted, in the middle of an unrelated Qt call, aborts the process. **Every caller that removes a body calls
`cleanup()` first.**

---

## 5. Choosing the body: registry and profiles

### 5.1 `renderer_for(fdef)`

1. The fixture's own `categories`, in order (OFL lists the most important first). The first one with a registered
   profile wins.
2. If none is registered (for example `Other`), the channels decide, most specific first:

| The definition has | It draws as |
|---|---|
| a `matrix` | `PixelBarFixture` |
| `Fog`, `FogOutput` or `FogType` | `HazerFixture` |
| `Pan`, `Tilt`, `PanContinuous` or `TiltContinuous` | `MovingHeadFixture` |
| `ColorIntensity`, `ColorPreset` or `ColorTemperature` | `ColorChangerFixture` |
| `Intensity` | `DimmerFixture` |
| none of these | `GenericStageFixture` |

The fallbacks read `all_capability_types`, not the channel's dominant capability: a pan channel is a pan channel even
if most of its range is a program.

### 5.2 The profiles

| OFL category | Profile | Emission | Housing |
|---|---|---|---|
| Color Changer, Dimmer | `ColorChangerFixture`, `DimmerFixture` (`lamp.py`) | from `beam_kind`: `LampGlow`, `WashLamp` or `HardBeam` | Bracket, can (or a panel when wider than 1.8× its height), recessed face. |
| — (fallthrough) | `GenericStageFixture` | same | Same. Registers no category. |
| Pixel Bar, Matrix | `PixelBarFixture` (`array.py`) | `MatrixCells` | A frame laid over the face. Children are clipped to the rounded outline. |
| Blinder | `BlinderFixture` | `BulbArray` | Housing and reflector tray behind the bulbs. |
| Strobe | `StrobeFixture` | `FlatPanel` | A frame over the face, like the bar. |
| Moving Head | `MovingHeadFixture` (`motorised.py`) | from `beam_kind`, falling back to WASH | Base, U-shaped yoke, head with bezel and lens. |
| Scanner, Barrel Scanner | `ScannerFixture` | from `beam_kind`, falling back to HARD | Body, neck, and a mirror that turns half as far as the beam. |
| Flower, Effect | `FlowerFixture` | `BeamFan` | Round can with a ring of lenses that spins with the rosette. |
| Laser | `LaserFixture` | `LaserFan` | Projector box with an emitter window. |
| Hazer | `HazerFixture` (`atmospheric.py`) | `FogPlume` (`puffs.HAZER`) | Case on feet, nozzle on top. |
| Smoke | `SmokeFixture` | `FogPlume` (`puffs.SMOKE`) | Same case. |
| Fan | `FanFixture` | `NoEmission` | A blade spinning inside a cage, at the rotation speed, else intensity × 4 Hz. |
| Stand | `StandFixture` | `NoEmission` | A post on a base if taller than wide, a crossbar on legs if wider. |

Sixteen categories are registered. The seventeenth OFL category, `Other`, always goes to the channel fallbacks.

### 5.3 What a profile declares

A profile is a `BodyFixture` subclass. It overrides these hooks and class attributes and nothing else:

| Hook / attribute | Default | Meaning |
|---|---|---|
| `build_housing()` | nothing | Build the chassis items, once. |
| `emission_for(fdef)` | `NoEmission` | Which emission class draws the light. |
| `paint_housing(frame)` | nothing | Per frame: tint the chassis, turn the yoke, tilt the mirror. |
| `aim_origin()` | bottom centre | Where light leaves the body, in housing coordinates as drawn standing. `emit_origin()` mirrors it for a flown body. |
| `beam_fallback` | `NONE` | What a definition with no declared optics throws. Moving Head: `WASH`. Scanner: `HARD`. |
| `forced_features` | `()` | Features attached even without their capabilities. Moving Head and Scanner force `PanTiltFeature`. |
| `lamp_is_lens` | False | The lamp is the aperture the beam leaves through, not a light of its own. Moving Head, Flower. |
| `draws_own_aperture` | False | The housing draws its own glass, so an unlit lamp leaves its stand-in disc off. Moving Head, Flower. |
| `profile_animates` | False | The chassis moves on its own. Fan. |
| `default_fog_kind` | `""` | Fog kind when the definition declares no `fogType`. Hazer `"Haze"`, Smoke `"Fog"`. |
| `rigs_inverted` | True | False for things that only stand on the deck: Hazer, Smoke, Fan, Stand. |

### 5.4 Chassis (`profiles/chassis.py`)

Every housing is built from `ChassisPart` items. A part is a `QPainterPath` filled with a top-lit vertical gradient,
with a dark cosmetic contour, an optional "barrel" gradient that darkens the sides, and optional details.

- **The palette is warm and dark because the backdrop is.** Where fixtures hang, the backdrop is RGB 4–12 with almost
  no blue. `SHELL_*` is the casing, `DARK_*` the recesses, `LENS_*` the glass. Mirrors have their own cool metallic
  colours.
- **Details (vents, screws, panels) draw only above `LOD_DETAIL` = 0.55 on-screen scale.** Half the catalogue renders
  under 60 scene units.
- **`set_tint(rgb, weight)` is the only per-frame call.** It adds up to `TINT_WEIGHT` = 30 % of the fixture's own
  colour as spill.
- `rounded()`, `frame()` (a border with a hole, for faces that fill the body) and `bracket()` (the U a fixture hangs
  in) are the shared shapes.
- A part's Qt parent is `parent.housing` when the parent has one. A profile can pass `self`, and the part still lands
  inside the group that a flown fixture turns over.

---

## 6. What light a fixture throws (`optics.py`)

`BeamKind` is `NONE` (a surface light with no shaft), `WASH` (a soft-edged cone) or `HARD` (a defined edge you can see
the end of). Two questions are kept apart on purpose: `beam_kind()` says whether there is a shaft, and
`resting_half_angle()` says how wide it is. A fixture can be HARD and wide.

`beam_kind(fdef, fallback)` takes the first answer it finds:

1. **A `stage.beam` sidecar hint** (`"none"`, `"wash"`, `"hard"`). It exists for gates that are not DMX channels,
   such as an ETC Source Four's lens tube.
2. **Shaping optics → HARD**: any of the capabilities `Prism`, `PrismRotation`, `Iris`, `IrisEffect`,
   `BladeInsertion`, `BladeRotation`, `Focus`, or a wheel slot of type `Gobo`, `AnimationGoboStart`,
   `AnimationGoboEnd`, `Prism` or `Iris`.
3. **The declared lens** (`physical.lens.degreesMinMax`). See the table below.
4. **A `Zoom` or `BeamAngle` channel with no lens → WASH.**
5. **The profile's fallback.** A WASH fallback is classified as a 75° lens (`DEFAULT_WASH_DEGREES`). That is past the
   wash limit, so a lens-less wash head becomes a soft source with no shaft. A HARD fallback stays HARD.

These are **not** evidence of a hard edge: `Frost` (it softens, and lives on wash heads), a bare `WheelSlot` (colour
wheels declare one too), and the slot types `Open`, `Closed`, `Color` and `Frost`.

Reading the lens:

| Lens | Kind |
|---|---|
| wide end ≤ 12° (`HARD_MAX_DEGREES`) | HARD |
| fixed lens (wide / narrow < 2.5, `ZOOM_RANGE_RATIO`), narrow end ≤ 12° | HARD |
| fixed lens, ≤ 65° (`WASH_MAX_DEGREES`) | WASH |
| fixed lens, > 65° | NONE |
| zoom range (ratio ≥ 2.5), narrow end ≤ 65° | WASH. A zoom's tight end is not a gate. |
| zoom range, narrow end > 65° | NONE |

A lens declared as `[0, x]` takes its narrow end from the wide one.

`resting_half_angle(fdef, kind)` is None for NONE. Otherwise it is the narrow end of a fixed lens, the **geometric**
mean of a zoom range (19–180° rests at 58°, not 100°), or the kind's default (14° hard, 75° wash). It is halved and
clamped to 1–89°. A live zoom channel overrides it every frame.

`emission.cone.emission_for_kind` maps NONE → `LampGlow`, WASH → `WashLamp`, HARD → `HardBeam`. Lamp profiles,
moving heads and scanners go through it. The flower, laser, bar, blinder and strobe name their emission directly.

---

## 7. Shaping the light: features (`features/`)

A feature is a `Feature` subclass registered with `@register_feature`. It declares the OFL capability `types` it
reads, an `order`, and whether it `animates`. `build_features` attaches a feature only if the definition declares one
of its types, or the profile forces it. This happens once, at construction. A "Moving Head" that is really a wash with
no gobo wheel simply has no gobo layer.

| Order | Feature | Attaches on | Writes |
|---|---|---|---|
| 10 | `DimmerFeature` | always | `level` = `state.intensity` |
| 20 | `ColorFeature` | always | `rgb` = `state.color.rgb` |
| 25 | `ColorWheelFeature` | any wheel capability | multiplies `rgb` by the gel(s) in the beam; mid-change slots crossfade |
| 30 | `ShutterFeature` ✱ | `ShutterStrobe`, `StrobeSpeed`, `StrobeDuration` | `gate`, and `level *= gate` |
| 40 | `ZoomFeature` | `Zoom`, `BeamAngle` | `half_angle_deg` from the live zoom |
| 50 | `IrisFeature` ✱ | `Iris`, `IrisEffect` | `aperture` (0.02–1), pulsing with an iris effect |
| 55 | `FrostFeature` | `Frost`, `FrostEffect` | `softness` |
| 60 | `GoboFeature` ✱ | any wheel capability | `gobo`, `gobo_next`, `gobo_blend`, `gobo_spin_deg`; a `Closed` slot sets `level` = 0 |
| 70 | `PrismFeature` ✱ | `Prism`, `PrismRotation` | `copies`: one angle per facet, at most 8 |
| 80 | `BladeFeature` | `BladeInsertion`, `BladeRotation`, `BladeSystemRotation` | `blades` |
| 90 | `PanTiltFeature` ✱ | `Pan`, `Tilt`, `PanContinuous`, `TiltContinuous`, `PanTiltSpeed`, `BeamPosition` | `axis_deg`, `foreshorten` |

✱ animates, so the body keeps the clock running.

- **Dimmer and colour always attach.** `resolve_state` has already folded intensity, mixing, presets and colour
  temperature into one level and one RGB. A fixture with none of those resolves to full white, which is right for a
  fixture whose only channel is a strobe.
- **The shutter is a waveform over the flash phase**, for all ten OFL shutter effects: `Open`, `Closed`, `Strobe`
  (square), `Pulse` (sin²), `RampUp`, `RampDown`, `RampUpDown`, `Lightning` (random hard hits), `Spikes` (a short
  stab) and `Burst` (three quick flashes, then a rest). The rate is clamped to 0.5–25 Hz, default 8. The phase comes
  from the shared clock plus a per-body seed, so two lightning fixtures do not flash in lockstep. An unknown effect
  leaves the light alone.
- **A colour wheel filters; it does not replace.** A red gel over a green lamp is black, as on the real fixture.
- **`PanTiltFeature` slews the mechanics, not the picture.** It interpolates pan and tilt with `Slew` (time constant
  from `PanTiltSpeed`, default 80 ms) and only then projects through `Aim`, so a head crossing the stage swings through
  an arc. Continuous rotation rides on top of the slew (`Spinner`), so a servo never chases 359° back round to 0.

`BeamParams` (`frame.py`) is the contract between features and emissions:

| Field | Meaning |
|---|---|
| `rgb`, `level` | Colour (0–255) and brightness (0–1) after dimmer, shutter and wheels. |
| `gate` | The shutter's part of `level` on its own, so per-cell emissions can flash their cells. |
| `visible` | `level > 0`, set after the features run. |
| `half_angle_deg` | None means no optics. |
| `length_px` | 1220. |
| `aperture`, `softness` | Iris (1 = open) and frost (0 = hard). |
| `axis_deg`, `foreshorten` | Aim in the picture (§8). |
| `gobo`, `gobo_next`, `gobo_blend`, `gobo_spin_deg` | The wheel slot in the beam. |
| `copies` | Prism facet angles. `(0.0,)` is one beam. |
| `blades` | Framing shutter state. |

---

## 8. Aiming and rigging (`geometry.py`)

The stage is a flat front elevation with no floor, depth or camera. A fixture is aimed in a room, as a unit vector
built from pan and tilt, and only then flattened onto the picture.

- **Home is straight down** for a flown fixture: half of the declared travel (DMX 127/128). The ranges come from the
  fixture's own `Pan` and `Tilt` angles, else 540° and 270°.
- **Tilt swings the beam; pan turns the plane it swings in.** At pan home, tilting walks the beam toward the viewer, so
  on the picture it gets shorter and blooms. A quarter turn of pan away, the same tilt sweeps the beam across the stage
  at full length. **At tilt home, pan cannot move the beam at all.**
- `Aim.point(pan, tilt)` returns `(axis_deg, foreshorten)`: the beam's direction in the picture, and how much of the
  unit vector is left in the picture plane. Together they are the vector. The fog march rebuilds it as
  `(cos·f, sin·f, −√(1−f²))`, so the flat drawing and the volumetric one cannot disagree.
- `foreshorten` never goes below `MIN_FORESHORTEN` = 0.15. A beam aimed at the audience becomes a short bright bloom,
  never nothing. The drawn length is `length × (0.15 + 0.85 × foreshorten)` (`FORESHORTEN_FLOOR`). The fog march
  inverts that to recover the real throw.
- `Aim.turn()` is how far the beam has turned off the fixture's own straight-ahead, in housing-local degrees. Profiles
  use it for the head and the mirror. Using `beam.axis_deg` there would count the rigging twice.
- `Spinner` integrates a rate into an angle. `Slew` chases a target at a limited rate. Both clamp a single step to
  0.25 s, so a stall does not throw anything across the stage.

**Rigging** (`FixtureInstance.hanging`, persisted in the show's patch, default true):

- Every profile draws its chassis standing on the deck, into `body.housing`. A flown body turns that group 180°.
  **Never turn the body item itself.** The GPU layer offsets a body's light by its position only (`LightLayer._pack`),
  so a turned body would send the raster path and the shader two different beams.
- The light's half of the flip belongs to `Aim`: a standing fixture adds 180° to `axis_deg`. `foreshorten` is
  unchanged.
- `emit_origin()` mirrors `aim_origin()` for a flown body, so the lens is at the other end.
- `StageController.add_fixture` guesses the rigging from the drop point. A fixture dropped above 66 % of the stage
  height (`DECK_TOP`) is flown. The guess is written down and the inspector can change it. It is never re-guessed
  while dragging.
- `set_fixture_hanging` and `change_fixture_mode` rebuild the fixture's bodies, because rigging and mode are read at
  construction. Both re-select the bodies that were selected.

---

## 9. Drawing the light: emissions (`emission/`)

An emission is the light, as distinct from the fixture. It never reads a capability.

| Method / attribute | Contract |
|---|---|
| `build()` | Create the child items, once. |
| `render(frame)` | Repaint the raster items for this frame. |
| `emit_lights(frame, out)` | Describe the same light as primitives, in body-local coordinates. Always called right after `render`, for the same frame. |
| `set_raster_light_visible(visible)` | Stop or resume painting the light in the raster scene. The chassis, unlit bulbs and dark cells stay. |
| `cleanup()` | Stop anything that ticks. |
| `reach(fdef)` (classmethod) | How far past the body the light can reach, answered before anything is built. |
| `reach_rises` | True when that reach is spent above the body (smoke). |
| `animates` | True when the emission changes between DMX frames on its own. |

`emission_reach(fdef)` and `emission_rises(fdef)` answer the last three questions for a definition. Both the stage and
the scene preview ask them.

| Emission | Used by | Raster items | Light primitives |
|---|---|---|---|
| `LampGlow` | lamp profiles that throw NONE | 1–24 `GlowingEllipse` lamps, glow 20× | POINT per lit lamp |
| `WashLamp` | WASH lamps and heads | lamps (glow 7×) and one `BeamCone` | POINT per lamp, one CONE |
| `HardBeam` ✱ | HARD lamps, heads and scanners | a lamp (glow 4×), 8 `BeamCone`s, a `GoboPlane` | POINT, one CONE per prism copy (hardness 1) |
| `BeamFan` ✱ | Flower | a lamp and 6 narrow cones | POINT and 6 CONEs |
| `MatrixCells` ✱ | Pixel Bar, Matrix | one `QGraphicsRectItem` per cell, with a drop-shadow bloom | QUAD per lit cell |
| `BulbArray` | Blinder | one `GlowingEllipse` per cell | POINT per bulb |
| `FlatPanel` | Strobe | one rect, with a drop-shadow bloom | QUAD |
| `LaserFan` ✱ | Laser | one `_LaserItem` painting the whole fan | LINE per beam |
| `FogPlume` ✱ | Hazer, Smoke | one `_PlumeItem` painting sampled parcels | BLOB per parcel |
| `NoEmission` | Fan, Stand | — | — |

✱ animates.

### 9.1 Cones and lamps (`cone.py`)

- **`cone_geometry(beam, hard)` is the one place a shaft's shape is worked out.** `BeamCone` draws a polygon from it,
  and `_emit_cone` hands the same two numbers to the GPU. The length is foreshortened. The spread is `tan(half_angle)`
  capped at 2× the length (`MAX_SPREAD_RATIO`), times the iris, times `1 + softness`, then widened as the beam turns
  toward the viewer. A hard beam uses 0.55× its half-angle (`HARD_BEAM_NARROWING`).
- **The raster cone is a gradient from the lens to nothing at the tip.** Its alpha is `peak × level × (0.25 + 0.75 ×
  haze)`, so a beam in clean air still shows a hint (`HAZE_FLOOR`). A hard beam's peak is 150 against a wash's 90.
  Below a `level` of 0.02 the cone hides.
- **A glow radius is counted in lamp radii and capped at 400 scene units.** It is 20× with no cone (the glow is the
  whole light), 7× around a wash cone and 4× around a hard beam. The cap exists because 20× on a large panel is a
  2650-unit halo on a 1536-unit stage.
- **A lens glows little until it points at you.** On a `lamp_is_lens` profile the halo is 0.15 lamp radii at rest,
  rising to 0.35 as the beam turns toward the viewer (squared, so the bloom belongs to the end of the turn). It uses
  `DIFFUSE_GLOW`, a falloff sampled from the shader's own POINT curve.
- **A wash with several zones** (a small matrix behind one lens) gets one lamp per zone, at most 24 (`MAX_LAMPS`,
  sampled evenly). Its cone takes the zones' average colour, unweighted by level, because the beam already carries the
  dimmer.
- **`HardBeam` draws one cone per prism copy.** The copies fan ±9° (`PRISM_SPREAD_DEG`) around the axis, and each
  takes `1/√n` of the level: a prism divides light. The `GoboPlane` draws the pattern at 78 % of the beam's length,
  spun and tinted.
- **`BeamFan` spreads six 4° beams across the front of the stage** (±63°), turned by `rosette_angle()`: the rotation
  channel's angle, or its speed integrated.

### 9.2 Surfaces (`surface.py`)

- **`MatrixCells`**: the grid is the matrix's X × Y. Its columns are divided between the bodies of a bar-set, and the
  longer axis is halved until it fits 256 cells per body (`MAX_CELLS`). Each cell takes its own emitter when the
  fixture drives pixels separately, and the fixture's colour otherwise.
- **When an effect program runs, the animation supplies the pattern and the DMX supplies the colour.**
  `effect_asset(fdef, program name)` resolves a GIF from the fixture's own `<stem>.effects/` folder, memoised per
  effect name. Each body reads its own columns of the image. The speed channel sets the `QMovie` speed: 1 Hz = 100 %.
- **There is one `QMovie` per animation file for the whole stage**, reference-counted. The last user stops it but
  never destroys it: freeing a `QMovie` mid-frame aborts the process.
- **On the GPU, a grid spills one face's worth of light, however many cells it has.** `CELL_GLOW` = 0.04 is shared
  out between the cells at construction. Each lit cell is a QUAD with a halo 50× its size (`CELL_HALO`). A per-cell
  constant made a fixture's output depend on how finely it was pixelated.
- **`BulbArray`** stacks one bulb per emitter. Bulbs beyond what the state drives stay dark.
- **`FlatPanel`** is one face. Its QUAD has intensity × 0.22 and a halo twice its size.

### 9.3 Things in the air (`volumetric.py`)

- **`LaserFan`** throws 24 beams over 50° while a program runs and 8 otherwise, narrowed by the iris. It is drawn as
  one item with a 2 px pen at alpha 200, so it stays saturated where a cone would fade. On the GPU each beam is a LINE
  of half-width 1.
- **`FogPlume`** keeps its parcels in a `puffs.PuffField`, not in the DMX (§10.2). The nozzle is half a body above
  `emit_origin()`. The raster plume paints at most 80 parcels (`PLUME_MAX_ITEMS`, every n-th one), each carrying up to
  1.8× alpha for the ones skipped (`PLUME_SHARE`), with a ceiling of 78/255 (`PLUME_ALPHA`). Each parcel is a sprite
  from a sheet of noisy discs. **Qt composites `SourceOver`, so overlapping parcels saturate rather than add**, and
  the low alpha keeps a bank of smoke from becoming a white lid. Parcels under 0.02 alpha are skipped; they are the
  largest and faintest. The plume item takes no mouse buttons, or it would swallow clicks meant for fixtures.

### 9.4 Glow (`glow.py`)

`GlowingEllipse` is the bulb every emission uses: an ellipse that paints its own radial gradient out to
`glow_radius_multiplier` × its radius.

- **The halo is the item's bounding rect.** Change it only through `set_glow_radius()`, which calls
  `prepareGeometryChange()`. Setting the attribute directly leaves the old halo painted on the scene.
- `glow_profile` is `SHARP_GLOW` (a flat core to an eighth of the radius, then roughly (1−t)²) or `DIFFUSE_GLOW` for
  lenses.
- With `draw_glow = False` (the GPU owns the light), a lit lamp paints only its face, in the glow colour. With
  `draw_unlit_face = False`, an unlit lamp paints nothing, so the housing's own glass shows.

### 9.5 Wheel art (`wheelart.py`)

OFL names a gobo by a resource id into an SVG library the app does not ship. Gobos are drawn procedurally instead,
from keywords in the resource id and slot name: `dots`, `lines`, `star`, `spiral`, `polygon`, and `breakup` as the
default. A seed from the slot's identity varies the pattern, so two gobos never look identical. Paths are cached per
(pattern, seed, radius). `slot_rgb` averages a split gel's colours. `slot_blocks_light` is true for a `Closed` slot.

---

## 10. The air

### 10.1 Atmosphere (`atmosphere.py`)

A beam is invisible in clean air, so how visible beams are is the one quantity shared across fixtures.
`StageController` owns one `Atmosphere`, and every body reads `atmosphere.level` when it draws.

- **Baseline**: `config.window.stage_haze` (default 0.30), the room's haze before any fixture runs.
- **Contributors**: every body whose definition declares a fog capability, whatever its category. Moving heads with a
  built-in hazer count. Each frame a contributor reports `state.atmosphere.output`, weighted by kind: `Haze` 1.0,
  `Fog` 0.6. Fog is denser but stays local.
- **Combining**: `target = 1 − (1 − baseline) × Π(1 − contribution)`. Sources never exceed 1.
- **Drift**: the controller advances the level on every clock tick, with a 1.5 s time constant going up and 12 s
  going down.
- **`settle()` jumps straight to the target.** `set_haze_baseline` calls it, then `refresh_air()`. **Both are
  needed.** Without the settle, a slider waits out a 12 s decay. Without the refresh, nothing redraws until the next
  DMX frame, because bodies only read the level when they draw.
- `DEFAULT_ATMOSPHERE` is for bodies with no stage (the scene preview): a 0.15 baseline and no contributors.

### 10.2 Smoke that outlives its channel (`puffs.py`)

A smoke machine that has just been closed is still full of smoke. That hang is what reads as smoke rather than an
effect switching off, so a plume cannot be a function of the current DMX value.

- **Every quantity is derived from a parcel's age, never integrated.** A field updated at 30 Hz, at 3 Hz, or twice an
  hour apart draws the same picture, and a stalled UI cannot leave smoke where it should never have reached.
- **Spawning**: `throttle = min(1, output / max_output)`, and the parcels owed accumulate at `throttle × rate_hz`. One
  step is capped at 0.25 s. The field is a ring of `capacity` parcels, sized so that `capacity / rate_hz` equals
  `lifetime_s`: at full throttle the oldest parcel is recycled exactly when it would have expired.
- **Motion** is computed from age and from values dealt once at birth by a deterministic hash. A parcel **rises and
  stops rising** (`rise × decay × (1 − e^(−age/decay))`). It **disperses** as a random walk (`√(2·K·age)`), squashed
  vertically and again downward because the floor is in the way. It **curls** on three octaves of sine, with X and Y
  on different frequencies. It **grows** by diffusion (`√(r₀² + 2·D·age)`). It **thins as it grows** (density ×
  (r₀/r)²). The lifetime only trims a tail nobody can see.
- `LivePuff` carries `spin` and `variant`, so both renderers draw a parcel turned the same way and with the same
  shape.

| Spec | `HAZER` | `SMOKE` |
|---|---|---|
| ceiling on output (`max_output`) | 0.35 | 1.0 |
| capacity / rate / lifetime | 100 / 5 Hz / 20 s | 400 / 20 Hz / 20 s |
| density, birth radius | 0.34, 95 | 0.85, 58 |
| rise speed × time | 46 px/s × 9 s | 200 px/s × 2.2 s |
| eddy dispersion (K) | 9000 px²/s | 4600 px²/s |
| lateral spread, wind | 22, 9 px/s | 66, 6 px/s |

**A parcel's shape lives in `puffs.py`, not in either renderer.** `PARCEL_FALLOFF`, `PARCEL_RIM`, the two noise
octaves and their mix, and `PARCEL_NOISE_GAIN` / `PARCEL_NOISE_FLOOR` are baked into the raster sprite sheet and
interpolated into `MEDIUM_FRAG`. The floor is negative on purpose, so the noise can carve holes in a parcel. It is
derived from the gain to keep the average modulation at 1, so the gain changes texture and not density. The two paths
hash their noise differently, so a parcel is not the identical picture on both: it is the same kind of picture.

---

## 11. The animation clock (`clock.py`)

`CLOCK` is one 30 Hz `QTimer` for everything that animates. Every subscriber in one tick sees the same monotonic
`now()`, so every strobe and gobo in the rig agrees on phase.

- **It is not a QObject.** `import stage` can run before the `QApplication` exists. The timer is made on the first
  subscribe, and remade if a dying `QApplication` took it.
- **Subscribing starts it, and the last unsubscribe stops it.**
- **Subscribers are held weakly** (`WeakMethod` for bound methods). A dead reference is dropped silently, and so is a
  callback whose C++ item Qt has already deleted (`RuntimeError`).
- **Subscribers**: every body that animates, and `StageController._advance_atmosphere`. The atmosphere subscription is
  unconditional, so on the app's stage the timer never stops, but that tick repaints nothing unless the haze level
  moved or the GPU fog's air is drifting.
- `tick_now()` runs one tick immediately, for tests.

---

## 12. Light as data (`light.py`)

A raster brush is 8-bit sRGB. It cannot hold "brighter than white", and two overlapping brushes do not add. The GPU
layer needs the light before it is flattened, so every emission also writes it as primitives:

- **Body-local coordinates.** The layer adds the body's scene position when it packs. A dragged fixture needs a
  repaint, not a re-render.
- **Linear colour** (`linear_rgb`, a 256-entry sRGB → linear table). It accepts floats, so an animated cell keeps its
  precision.
- **Unbounded intensity.** Two blinders on one patch land at twice full scale, and exposure decides how much of that
  shows.
- **One growable `array('f')`, 16 floats per primitive (`STRIDE`)**, cleared by resetting a counter. `STRIDE` is also
  the GPU vertex-buffer stride.

| Slot | 0 | 1–2 | 3–5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14–15 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| field | kind | x, y | rgb | intensity | radius | axis_deg | length | half_width | softness | hardness | foreshorten | w, h |

| Kind | Value | What it is | Fields used |
|---|---|---|---|
| POINT | 0 | a lamp or bulb | radius |
| CONE | 1 | a beam | axis_deg, length, half_width (the spread), softness, hardness, foreshorten |
| QUAD | 2 | a lit face (cell, panel) | w, h; radius is the halo past the face |
| LINE | 3 | a laser beam | axis_deg, length, half_width |
| BLOB | 4 | a parcel of medium, **not light** | radius; density in `intensity`; `z` in axis_deg, `z_extent` in length, `spin` in half_width, `variant` in softness |

`primitives()` and `of_kind()` unpack the buffer for tests. `layer.py` asserts the kind numbers, and that
`shaders.INSTANCE_ATTRIBUTES` adds up to `STRIDE`.

---

## 13. The controller and the view (`controller.py`)

### 13.1 `StageController`

`MainWindow` builds one, with the config. It owns:

- **The scene**, with `setItemIndexMethod(NoIndex)`. The BSP index did not purge child items removed implicitly (a
  bar's cells). The view painted ghosts of deleted fixtures, hit-testing found removed items, and a freed item left
  the index dangling, which crashed the app. NoIndex walks the live item list, which is cheap for a few hundred items.
- **The backdrop**, a `QGraphicsPixmapItem` at z −1000. `set_background_image()` swaps it and resizes the scene.
- **`render_objs`**, every body in patch order, and **`_render_groups`**, the `(FixtureInstance, [bodies])` pairs.
- **The atmosphere** and its clock subscription.
- **The light layer**, while the GPU path is on.
- **The debug overlay.**

It emits `fixture_added`, `fixture_moved`, `fixture_removed` and `fixture_selected`, each carrying the
`FixtureInstance` (`fixture_selected` carries None when nothing is selected).

**DMX in.** `update_fixtures_with_dmx(universe)` goes through each group: it cuts
`[dmx_start − 1, dmx_start − 1 + channel_count)`, skips the slice if it is short or unchanged since the last frame,
resolves it once, and calls `set_state` on every body. There is no scene-wide `update()` afterwards. Every changed
item already invalidated itself, and with NoIndex a blanket update repaints all 1536 × 1024.

**Patch changes are incremental.** Adding or removing a fixture touches only its own items, never the whole scene.
Tearing down items the view may be mid-paint on crashes. `sync_from_config()` brings the stage in line with a new patch
(opening a show rebinds `config.fixtures`). It removes what is gone and adds what is new, keeps the very same bodies
for fixtures in both, then re-sorts to patch order. A body's definition comes from
`resolve_fixture(type, mode)`; an unknown type is skipped with a warning. Saved `bars` beyond `body_count(fdef)` are
ignored.

**Air and exposure.**

- `set_haze_baseline(value)` writes `config.window.stage_haze`, settles the atmosphere and refreshes.
- `refresh_air()` re-renders every body and hands the layer the new haze.
- `set_exposure(value)` writes `config.window.stage_exposure` (0.1–4.0). Only the GPU path uses it.
- `set_fog(**values)` edits `config.window.fog`, validated through `Config._parse_fog`.
- `apply_stage_config()` re-reads fog and exposure into the layer. **Call it after Settings.** The dialog replaces
  `config.window.fog` with a new object, and the layer would keep the old one. It does not touch `stage_gpu`; that is
  `set_gpu_light()`'s job.

**GPU switches** (§14.1): `set_gpu_light(enabled)` returns whether the GPU path is on afterwards.
`suspend_gpu_light()` and `resume_gpu_light()` drop and rebuild the layer around a reparent.

### 13.2 `StageView`

A `QGraphicsView` with no scrollbars and no frame. It fits the scene on every resize and show.

- **Overlays** (`add_overlay(widget, anchor, reference, margin)`): widgets floated over a corner of the viewport or of
  the letterboxed stage image. Overlays that share an anchor and a reference form a row, in registration order. A
  hidden one takes no room.
- **The designer grid**: a 96-unit cosmetic grid drawn in `drawForeground`, in edit mode only. Nothing snaps to it.
- **`drawForeground` also runs the light layer** (`_draw_light`), between `beginNativePainting()` and
  `endNativePainting()`, clipped to the stage image in device pixels.
- **`_scene_to_clip()`** rebuilds the view transform as a 4×4 matrix for GL, because native painting bypasses the
  QPainter that would carry it.
- **The frame probe** (`set_frame_probe`) times each `paintEvent` with `perf_counter`. It is installed only while the
  debug overlay is showing.

### 13.3 The designer

`DesignableFixture` is the scenario-designer mixin on every body.

- Bodies are movable and selectable only in design mode, so playback is undisturbed.
- **A drag is visual until `mouseRelease`.** On release the move callback runs, and `_on_fixture_moved` writes the
  position to the instance, or to `bars[i]` for one bar of a multi-body fixture. Timeline clips follow the same
  convention.
- `FixtureLabel` ("name · DMX address") is a top-level scene item with `ItemIgnoresTransformations`, so it stays
  legible at any fit scale and a bar's drop-shadow does not blur it. Only the first body of a multi-body fixture shows
  one.
- Edit mode draws a thin blue outline on every fixture and a thicker, lighter one on the selected one.
- `remove_selected()` and `remove_fixture(instance)` detach first and announce afterwards. `can_rig_inverted(instance)`
  tells the inspector whether to offer the rigging switch.

The toolbar and the inspector themselves live in `ui/fixture/designer.py`.

### 13.4 Where the DMX comes from

`DMXManager.dmx_data_received` feeds `MainWindow.update_dmx`. With the default "All software" routing, the software
controller's frames cross into Qt through `StageFrameBridge`, a queued signal with no UDP loopback. Otherwise a
stage-input worker (an Art-Net listener or a USB reader) feeds it. Settings ▸ Input / Output picks exactly one stage
feed. `update_dmx` drives the 2D stage only while it is the stage on show; when the experimental 3D stage is in its
place, the 3D view gets the frame instead.

### 13.5 The debug readout (`debug_overlay.py`)

A label in the stage image's bottom-right corner, transparent to the mouse, shown **only while logging is at DEBUG**.
That level is its switch. It is re-read every 500 ms, so Settings ▸ Logging, or an edit to `userprefs.json` on a rack
box, toggles it live. It has no preference of its own. For example (right-aligned on screen):

```
fps  28.5   paint  12.3 /  41.0 ms
draw 2560×1440 ·   86 lights
fog  1280×720 ·  150 smoke ·    settled
```

- **The first line** is the frame rate and the last / worst paint over a 2 s window: what one repaint costs the Qt
  thread. A stage paint of 46–129 ms is the reason the audio feed has its own thread (see CLAUDE.md, *Audio and the
  transport*).
- **`draw`** is written only by a layer that is up and drawing. **Its absence means the raster path**, since every GPU
  failure is soft. Which driver is behind it is logged once by `LightLayer._initialise`.
- **`fog`** shows the march size, the number of smoke parcels and the convergence countdown.

The numbers are fixed-width, so the right-anchored panel does not jump. The text is recomputed on a timer and set only
when it changes, so a still stage reads "0 fps" and costs nothing.

---

## 14. The GPU renderer (`stage/gpu/`)

It is on by default (`config.window.stage_gpu`, Settings' Mid quality preset) and needs desktop OpenGL 3.3 core or
later.

### 14.1 Turning it on and off

- **At startup**, `main.py` calls `configure_surface_format()` **before `QApplication`** when `stage_gpu` is on — read
  straight from `userprefs.json`, with a missing file or key meaning the default, as `Config.load()` reads it.
  `StageController.__init__` then calls `_enable_gpu_light()`.
- **From Settings**, `MainWindow._apply_stage_gpu` calls `set_gpu_light()`. If the machine cannot honour the request,
  the user gets a warning dialog. At startup the fallback is silent.
- **`_enable_gpu_light()`** builds a `LightLayer`, swaps the view's viewport for a `QOpenGLWidget`
  (`attach_light_layer`), gives every body a light sink and calls `set_light_on_gpu(True)` on each. Then it sets
  `layer.STAGE_GPU_ACTIVE = True`.
- **Everything fails soft.** No GL widget, a GL ES context, no 3.3 core functions, a shader that does not compile, a
  draw that throws: each logs once, and the stage keeps drawing on the raster path. `LightLayer._failed` and
  `StageView._light_layer = None` latch for the session.
- **`STAGE_GPU_ACTIVE`** tells the scene preview that the stage really got a working context. It records what was
  achieved, not what was asked for, so headless and offscreen runs stay on raster for free.
- **Docking or undocking reparents the view, which gives the GL widget a new context.** The layer caches programs,
  buffers and framebuffers from the old context and never revalidates them. So `MainWindow` calls
  `suspend_gpu_light()` before the move and `resume_gpu_light()` on the next event-loop turn, which builds a fresh
  layer. `detach_light_layer()` drops the GL viewport rather than keeping it.

### 14.2 Four GL constraints (`layer.py`)

1. **The GL widget must BE the viewport.** A `QOpenGLWidget` parented into the viewport renders opaque and hides the
   scene, whatever transparency attributes it is given.
2. **The surface format must call `setRenderableType(OpenGL)`.** Without it Wayland/EGL hands out OpenGL ES, where
   none of the shaders compile.
3. **Asking for a version is platform-specific.** On X11/Wayland, do NOT call `setVersion` or `setProfile`: 3.3 Core
   fails with `EGL_BAD_MATCH` and takes Qt's backing store down with it. On macOS you MUST ask for 4.1 Core, or Cocoa
   hands out legacy 2.1. `apply_surface_format()` sets the format on each GL widget and also moves the app default, so
   a GPU stage switched on from Settings works without a restart.
4. **Draw calls come from `QOpenGLFunctions_4_1_Core`, falling back to `_3_3_Core`.** PyQt6 exposes `glDrawArrays`
   nowhere else. PyOpenGL is not a substitute: it errors with "no valid context" on desktop GL.

Two more facts: the GL viewport's back buffer is undefined each frame, so the view paints its letterbox black. And
`QWidget.grab()` cannot read back a GL viewport, so screenshots need the raster path.

### 14.3 One frame (`LightLayer.render`)

1. **Initialise** on first use, inside the current context.
2. **Pack** (`_pack`) every body's light list, offset by the body's position, into one buffer. With fog on, BLOBs are
   moved after the lights. With fog off they stay mixed in and draw as faint puffs.
3. **If nothing is lit and the air is not worth a frame, return.** An empty light list is not always an empty frame:
   with fog on, the baseline haze and the ambient light still draw (`_air_worth_drawing`).
4. **Compute the frame signature** for the fog's convergence (§14.5).
5. **Ensure the RGBA16F HDR framebuffer** at surface size, and set the scissor to the stage image.
6. **Upload the instance buffer.** This is its own step, before the light pass's `count == 0` return, because the
   medium pass reads the same buffer. A fog-only stage (one hazer, nothing else lit) once read past the end of an empty
   buffer here and crashed.
7. **Accumulate**: clear, `glBlendFunc(GL_ONE, GL_ONE)`, then one instanced unit quad per primitive.
8. **March the fog** (§15), with the scissor off.
9. **Resolve** over the scene.
10. **Restore** the scissor, blending, textures and viewport. `endNativePainting` only restores what Qt tracks, and
    leftover additive blending shows up as stale window content around the stage.

### 14.4 The passes (`shaders.py`)

| Pass | Program | Into | What it does |
|---|---|---|---|
| 1 | LIGHT | HDR framebuffer, surface size | One quad per primitive, additive. Details below. |
| 2 | MEDIUM | medium map | BLOBs rasterised into density (§15). |
| 3 | FOG | march framebuffer | The volumetric march (§15). |
| 4 | TEMPORAL | history framebuffer | `mix(history, current, 0.35)`. |
| 5 | RESOLVE | the screen, additive | `(hdr + fog) × exposure`, ACES fitted tonemap (Stephen Hill's RRT+ODT fit), gamma 1/2.2, added over the scene. |

The LIGHT pass, per primitive kind:

- **POINT**: an inverse-square halo with a hot core, spread over `GLOW_REACH` = 60 lamp radii and capped at 800 units.
- **CONE**: narrows toward the lens, with a hard or soft edge, inverse-square along the throw. It is scaled by the haze
  and by `forward_scatter(foreshorten)`, a Henyey-Greenstein phase normalised to 1 across the picture, so a beam aimed
  at the viewer is far brighter. With fog on, this pass keeps only the lens's near field and hands the shaft to the
  march.
- **QUAD**: spill only, a Gaussian falloff past the face. The face itself is the raster cell underneath.
- **LINE**: widened to at least 1 device pixel either side (`MIN_LINE_HALF_PX`), with the brightness divided back out.
  A thinner quad rasterises as a dashed line.
- **BLOB**: a faint additive puff, only when fog is off.

The shaders are Python strings, not files under `assets/`: a frozen build that cannot find its shaders fails on
someone else's machine. Constants both sides need (`FORESHORTEN_FLOOR`, the `PARCEL_*` shape) are interpolated from
Python. The ACES matrices are written transposed, because GLSL's `mat3` constructor fills columns; getting that wrong
fails silently, as a slight green or magenta tint.

### 14.5 Repaints

The stage repaints only when something changed, and a still rig costs nothing. The whole scheme rests on that.

- **A body that renders calls its `_light_sink`**, which is `StageController._on_light_changed` → `viewport().update()`.
  Qt coalesces updates per event-loop turn, so a rig where everything moves costs one repaint.
- **The atmosphere tick asks for a repaint only when the haze level moved, or when `air_is_drifting()`**: fog on,
  `texture` > 0, and something in the air. The march's noise flows with time, so textured air really changes every
  frame.
- **The fog march converges and then stops.** It is dithered and averaged over frames, so after the picture stops
  changing `_settle` asks for `CONVERGE_FRAMES` = 6 more repaints. The countdown is reset by `_frame_signature()` (the
  packed primitives, the counts, the haze, the scene rect and the fog settings), **never by the march merely
  running**, which would request a frame every frame. Time is deliberately not in the signature; the drift is driven
  from the controller's clock instead.

### 14.6 What the raster path keeps drawing in GPU mode

`set_light_on_gpu(True)` moves only the light. Hit-testing, selection and the designer still run on the scene items,
and housing tints are drawn in both modes.

| Emission | Raster keeps | Raster stops |
|---|---|---|
| `WashLamp`, `LampGlow`, `BulbArray` | each lamp's lit face | halos, cones |
| `HardBeam`, `BeamFan` | the lamp's face, the `GoboPlane` | lens halo, cones |
| `MatrixCells`, `FlatPanel` | the lit cells / panel | the drop-shadow bloom |
| `LaserFan`, `FogPlume` | — | the fan, the plume |

---

## 15. Volumetric fog (`gpu/fog.py`)

On by default, and it needs the GPU path. It runs between accumulate and resolve, so fog lands in the HDR buffer
before the tonemap: fog is just more light. **How much fog there is stays `stage_haze`.** These settings shape what it
looks like and what it costs.

`StageFogConfig` (`config.window.fog`, stored in `userprefs.json`):

| Field | Default | Range | Meaning |
|---|---|---|---|
| `enabled` | true | | Run the march. |
| `height`, `height_falloff` | 520, 2.4 | 1–4000, 0–12 | How far up from the floor the haze reaches, and how sharply it thins. |
| `color` | `#dfe4ee` | hex | The colour the air scatters. |
| `scattering` | 1.75 | 0–8 | How much light comes back to the eye. |
| `anisotropy` | 0.1 | −0.95–0.95 | Henyey-Greenstein g. |
| `texture`, `texture_scale` | 1.0, 260 | 0–3, 20–2000 | How ragged the air is, and its feature size in scene units. |
| `light_intensity`, `light_dir_deg` | 0.8, 255 | 0–4, 0–360 | A stage-wide key light on the medium, so smoke shows before any fixture does. |
| `march_steps` | 24 | 4–128 | Samples per ray. The biggest cost. |
| `max_distance` | 1400 | 100–8000 | Depth of the marched slab. |
| `jitter` | 0.8 | 0–2 | Ray-start dither. 0 brings the banding back. |
| `resolution` | 0.25 | 0.125–1 | Fraction of the surface size the march runs at. |

They are edited in Settings, and live from the edit-mode toolbar: the `FOG` switch, `TEXTURE`, height, falloff,
scatter, g, ambient and colour.

**The medium map.** BLOBs are drawn additively into an RGBA16F map of the whole scene rect at 0.25 scale
(`MEDIUM_SCALE`), holding density and a density-weighted depth extent. The map has its own projection, so smoke that
drifted off screen still thickens the air on screen. Each parcel is drawn as the `PARCEL_*` noisy disc, turned to its
`spin`.

**The march.** The projection is orthographic, so a pixel's view ray is exactly `(scene_x, scene_y, −camera_z + t)`
and no depth buffer is needed. The camera sits at 0.55 × `max_distance`.

- **One bounded quad per CONE, LINE and POINT**, plus one fullscreen pass for the ambient light (`u_ambient_pass`).
  QUADs and BLOBs are not light sources here. The cost follows lit screen area, not the number of lights.
- **Each ray segment is solved, not swept.** The ray is clipped to the light's bounding-sphere chord, then to the cone
  itself or to the laser's tube (radius `LINE_FOG_RADIUS` = 12). A 25° beam is about 66 units thick in depth against a
  1400-unit slab: 24 steps spread over the chord put 0–2 samples inside the beam and the shaft vanished.
- **Density** is the baseline haze (thinning with height above the floor) times `turbulence()`, plus the medium map.
  Smoke takes only 0.4 of the turbulence (`MEDIUM_TEXTURE_WEIGHT`), since it is already ragged. The ambient pass
  lights only 0.09 of the baseline haze (`AMBIENT_HAZE_WEIGHT`), because the backdrop photograph already contains its
  own air. Machine smoke is not scaled by that.
- **Extinction** is Beer-Lambert, normalised so full haze is an optical depth of 2.5 across the slab, whatever its
  depth. **The phase function** is Henyey-Greenstein divided by its broadside value, so `scattering` = 1 is "as bright
  as the beam always looked".
- **A POINT lights the air over 10 lamp radii** (`FOG_POINT_REACH`), not the sprite's 60. Integrated along a ray, 60
  turns a lamp into a screen-filling wash.
- **`turbulence()`** is two octaves of value noise on their own currents (`FLOW_PX_S` = (34, −22, 16) units/s; the
  fine octave runs faster and is domain-warped by the coarse one), stretched and clamped at zero so pockets of air can
  be empty. The time uniform wraps every 6 hours (`TIME_WRAP_S`) to keep float32 precision.

**Temporal.** There are three framebuffers, not two: the march writes its own, and the history ping-pongs between the
other two. Averaging A into B while sampling B is a feedback loop, and on the driver this was written against the fog
ramped to white. The history is thrown away whenever the size or the view matrix changes; there is no camera motion
to reproject.

**Two traps:**

- **`QOpenGLFramebufferObject` hands out `GL_NEAREST` textures.** `_smooth()` sets `GL_LINEAR` and clamp-to-edge on
  every fog target. Without it the half-size march shows its texels as blocks. The linear filter *is* the upsample.
- **The scissor is OFF for the march.** GL scissors are in absolute framebuffer pixels, and every fog target is a
  different size. With it on, the half-size march loses everything left of the clip, and the medium map (about 384
  texels wide) can miss the stage entirely. `_resolve` is scissored, and that is what confines the fog to the stage
  image.

---

## 16. The scene preview (`ui/fixture/console.py`)

`ScenePreview` is the stage engine drawing one fixture in the fixture dialogs, fed a scene's raw DMX instead of live
frames. A look previews exactly as it will look on stage.

- It builds one body per `body_count()` with `renderer_for()`, as the stage does, but on `metrics.preview_def(fdef)`
  sizes. Its viewport reserves room on the side the light uses (`emission_reach`, `emission_rises`).
- It uses the GPU layer only when `stage_gpu_active()` is true.
- Its bodies use `DEFAULT_ATMOSPHERE`.
- **`clear()` it before it goes away.** Its bodies hold clock subscriptions and shared movies.

---

## 17. Sidecar `stage` hints

A fixture's `<x>.tdmx.json` sidecar may carry a `stage` block per mode (parsed by `core/fixture_sidecar.py`). Every
key is optional:

| Key | Read by | Effect |
|---|---|---|
| `size_px: [w, h]` | `metrics.body_size` | On-stage body size, overriding the mm dimensions. |
| `bodies: n` | `metrics.body_count` | Separate housings on one DMX slice. |
| `body_spacing: n` | `metrics.body_spacing` | Gap between them, in scene units. |
| `cells: n` | `metrics.declared_cell_count`, `fixture_state` | Separately lit zones on one face. |
| `beam: "none" \| "wash" \| "hard"` | `optics.beam_kind` | Overrides the optics classification. |

**A named mode's `stage` block replaces the shared `"*"` block wholesale**, so it must repeat every key it still
wants. The shipped LED bar sets `body_spacing` and the shipped blinder sets `cells`.

---

## 18. Rules not to break

### The engine

- **Category picks the body; capabilities pick the features.** Never special-case a fixture by name. A new kind of
  light is a new profile module, and support for a new capability is a new feature module. Neither touches the
  controller.
- **Profiles and emissions never read a capability.** Emissions read `BeamParams` and `FixtureState`.
- **Features are chosen once, at construction**, never per frame.
- **`beam_kind` and `resting_half_angle` stay separate.** Whether there is a shaft and how wide it is are different
  questions.
- **A fixture with no pan or tilt draws straight down, at full length.**
- **Turn the housing, never the body item.** The GPU offsets light by position only.
- **Each shape has one source.** Raster and GPU both take a shaft's shape from `cone_geometry()`, a parcel's look
  from `puffs.PARCEL_*` and the foreshortening floor from `frame.FORESHORTEN_FLOOR`.
- **`emit_lights` must describe what `render` drew**, for the same frame, in body-local coordinates, linear colour
  and unclamped intensity.
- **Smoke is derived from age.** Do not integrate state in `puffs.py`.
- **Change a halo only through `set_glow_radius()`.**

### Performance and safety

- **A still rig costs nothing.** Unchanged DMX slices are skipped, only animating bodies subscribe to the clock, the
  atmosphere tick repaints only on a change, the fog converges and stops, and the debug probe exists only at DEBUG. A
  change that requests a repaint every frame breaks this.
- **No scene-wide `update()`.** Items invalidate themselves.
- **Never rebuild the whole scene** for a patch change; add and remove per fixture. Keep the scene at `NoIndex`.
- **Call `cleanup()` before a body leaves the scene**, and `clear()` the scene preview before it goes.
- **Never destroy a shared `QMovie`.**
- **No QObject at import time** in modules that `import stage` loads. `AnimationClock` and `Atmosphere` are plain
  Python for that reason.
- **A stage repaint blocks the Qt thread for tens of milliseconds.** Nothing time-critical may depend on that thread.
  The audio feed and the click are built around this.

### The GPU path

- Keep the four GL constraints of §14.2.
- **Upload the instances before the light pass's `count == 0` return.**
- **`STRIDE` stays 16.** A BLOB reuses spare slots for `z`, `z_extent`, `spin` and `variant`. A new field needs a slot,
  an `INSTANCE_ATTRIBUTES` entry and the shaders' attribute lists changed together. `layer.py` asserts the kinds and
  the stride.
- **Three fog framebuffers, `GL_LINEAR` on all of them, scissor off for the march.**
- **Restore the GL state** after native painting.
- **Call `apply_stage_config()` after Settings, and suspend and resume the GPU light around a reparent.**

---

## 19. Extending the engine

**A new kind of fixture body.** Add a `BodyFixture` subclass under `stage/profiles/` with
`@register_renderer("<OFL category>")`. Import it in `stage/profiles/__init__.py`, and export it from
`stage/__init__.py` if anything outside needs the class. Implement `build_housing` with `ChassisPart`s drawn standing
on the deck, plus `emission_for`, `paint_housing` and `aim_origin` as needed, and set the class attributes of §5.3.
Add the category to the tables in `tests/test_renderer_registry.py`.

**Support for a new capability.** If `core/fixture_state.py` does not model it yet, add it there first
(`tests/test_capability_coverage.py` fails until every OFL type resolves). Then add a `Feature` subclass in
`stage/features/` with `types`, `order` and `animates`, decorate it with `@register_feature`, and import its module in
`stage/features/__init__.py`. Write the result into `BeamParams`. If no field fits, add one, and teach the emissions
that should honour it to read it.

**A new emission.** Subclass `Emission` and implement `build`, `render`, `emit_lights`, `set_raster_light_visible`,
`cleanup` and `reach`. Draw many shapes with one item where you can, as the laser fan and the plume do: dozens of
`QGraphicsItem`s per fixture is the cost that matters. Keep the per-frame work free of allocation.

**A new light primitive.** Add the kind to `light.py`, to `_KINDS` in `shaders.py` and to the assert at the bottom of
`layer.py`. Handle it in `LIGHT_VERT` / `LIGHT_FRAG`, and in the fog shaders if it lights the air.

---

## 20. Tests

All of them are headless and GL-free: CI's offscreen platform cannot create a GL context, so the shader maths are
mirrored in Python and tested there. `tests/ofl_factory.py` builds synthetic OFL definitions, so a capability or
category no shipped fixture has can still be exercised. Each file also runs standalone through its `main()`.

| File | What it pins |
|---|---|
| `test_renderer_registry.py` | Category → profile, the channel fallbacks, every OFL category resolving, and the shipped fixtures' renderers. |
| `test_stage_optics.py` | The `BeamKind` archetype table, and that the shaping and non-shaping slot sets cover all nine OFL slot types. |
| `test_capability_coverage.py` | Every OFL capability type resolves in `fixture_state`. |
| `test_stage_engine.py` | The two-stage pipeline: feature discovery, graceful degradation, the clock, matrix cells and zones, preview sizing. |
| `test_stage_features.py` | Gels, gobos, closed slots, the prism and the shutter waveforms. |
| `test_stage_geometry.py` | Aim, foreshortening, rigging (the housing turns, the body never does), fog machines refusing to fly. |
| `test_stage_render_parity.py` | Glow colour and intensity, cell colours and the animation chosen for given DMX slices. |
| `test_light_list.py` | The light list both renderers draw from. |
| `test_fog_medium.py` | Smoke outliving its channel, parcel motion and the machine specs. |
| `test_fog_math.py`, `test_tonemap.py` | The march and the tonemap, in Python. |
| `test_stage_designer.py`, `test_stage_edit_ui.py` | Incremental add and remove, edit-mode overlays and grid, the toolbar and the inspector. |
| `test_stage_undock.py` | Overlay rows per corner, overlays surviving a viewport swap, and the GPU suspend / resume / switch logic without GL. |
| `test_stage_debug_overlay.py` | The readout appears only at DEBUG, takes its frame probe with it, and sits in the bottom-right corner. |

**`[255] * channel_count` is not a "fixture on" frame.** On anything with a shutter channel it selects a strobe. Use
`_lit_frame()` from `tests/test_light_list.py`.

---

## 21. Known limits and quirks

- **There is no depth.** The stage is a front elevation, and panning a head at tilt home does nothing visible, by
  design.
- **Gobos are procedural**, drawn from keywords rather than from OFL's SVG library.
- **Some modelled optics are not drawn.** `BladeFeature` writes `beam.blades`, but no emission reads it, so framing
  shutters have no visible effect. `GoboFeature` writes `gobo_next` and `gobo_blend`, but `GoboPlane` draws only the
  current slot, so a gobo change snaps instead of dissolving. Wheel shake and focus are not drawn. Only `HardBeam`
  draws prism copies. The GPU path draws no gobos; they stay a raster `GoboPlane`.
- **A lens halo does not come back after the GPU path is switched off at runtime.** `HardBeam` and `BeamFan` set
  `lamp.draw_glow = visible and not lens_only`, so returning to raster leaves a moving head's or flower's lens with no
  halo. A stage that starts on raster does draw one. `WashLamp` sets `draw_glow = visible` and is not affected. Found
  while writing this document; not fixed.
- **`body.housing` is not always the rigging group.** `LampFixture` and `BlinderFixture` reassign `self.housing` to
  their can part inside `build_housing`. The can was created inside the group, so the flip still works, but after
  construction `body.housing` on those two profiles is the can (rotation 0), not the group a flown fixture turned
  over.
- **Some comments about the medium map are out of date.** `gpu/fog.py`'s module docstring and
  `LightLayer._march_fog` call it an eighth of the stage. `MEDIUM_SCALE` is 0.25 and the format is RGBA16F.
- **`export_stage_image()` renders the scene with a QPainter**, so like `QWidget.grab()` it captures the raster path
  only.
- **Leftovers on the controller.** The `fixture_updated` and `stage_clicked` signals are declared and never emitted.
  The `led_bars`, `blinders` and `generic_fixtures` properties are kept for the designer tests and older call sites;
  rendering does not use them.
