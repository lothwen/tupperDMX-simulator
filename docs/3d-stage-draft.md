# Draft: a real 3D stage

*A sketch of what it would take to render the virtual stage as a room — walls,
ceiling and floor — with fixtures placed in space, light that collides with
surfaces, and a camera. Not an implementation plan: an architecture draft and a
list of the decisions that would have to be made.*

Written 2026-08-07 against the `ofl-render-engine` branch.

---

## 1. Where we actually are

Two halves of the codebase, and they are in very different places.

**The domain half is already done and is 100% reusable.** `core/ofl.py`,
`core/fixture_state.py`, `core/fixture_library.py`, `stage/optics.py`,
`stage/features/`, `stage/frame.py` — 43 capability types resolving to a
`BeamParams`, category→profile dispatch, colour solved in both directions. None
of it knows or cares how many dimensions the renderer has. This is the
expensive, correctness-critical part of a lighting visualiser, and it is
finished.

**The rendering half is a picture, not a world**, and about a quarter of it
survives.

What already thinks in 3D, surprisingly: the fog march. `stage/gpu/shaders.py`
has an orthographic camera at `u_camera_z`, world-space rays
`(scene_x, scene_y, −camera_z + t)`, an analytic ray/cone quadratic in
`cone_segment()`, blobs with real `z`/`z_extent`, Henyey-Greenstein phase and
Beer-Lambert transmittance. That is a small volumetric renderer already, and it
ports nearly unchanged.

What is irreducibly flat:

| Thing | Why it cannot follow |
|---|---|
| `stage/body.py`, `stage/glow.py`, the `render()` half of every `emission/` class | ~49 `QGraphics*` items; a 2D scene graph |
| `stage/geometry.py` `Aim` | pan→screen swing via `PAN_TO_SCREEN = 0.5`, clamped to ±80°; `foreshorten` is a fudge for having no depth |
| `axis_deg` + `foreshorten` as the beam's orientation | 123 references across `stage/`; a 3D beam has a direction vector, not an angle-in-the-picture |
| `stage/controller.py`'s `QGraphicsScene` + letterboxed `fitInView` | the whole view layer |
| The backdrop PNG | there is no floor to hit — that is the ask |

So: **the 3D stage is a second renderer beside the current one, not an
evolution of it.** It reuses everything above `Frame`/`BeamParams` and replaces
everything below.

One nice discovery: the units are already physical. `MM_PER_STAGE_PX = 2.5` in
`stage/metrics.py` means the 1536×1024 stage image *is* 3.84 m × 2.56 m, and
`BEAM_LENGTH_PX = 1220` is a 3.05 m throw. Coherent, just a small room. There is
a real scale decision at migration time, not merely a unit relabel.

---

## 2. The five things that have to change

### a. A world, with a room in it

New Qt-free module, `stage/world.py`: dimensions in metres, a `Room`
(width/depth/height, per-surface albedo), and the transform stack.
Rendering-agnostic and unit-testable, same posture as `stage/optics.py`.

Where the room lives is a decision. It should go in the **show** manifest next
to the patch, because fixture positions are meaningless without the room they
are in — a rig exported to a `.showpack` and opened elsewhere has to arrive in
its own venue. `window` config keeps only the renderer knobs (exposure, GPU,
fog), which is the existing machine-vs-show split.

### b. Fixtures get a transform

`FixtureInstance` today is `x: int, y: int` in image pixels
(`core/config.py`). It needs position in metres plus orientation — hung from a
truss pointing down, floor-standing pointing up, tilted on a base — because
that is what decides where its beam lands.

Migration for existing patches: `x → x_m` by `MM_PER_STAGE_PX`, image-`y` →
height above floor (flipped; the image's bottom is the floor), `z = 0` (the rig
plane), orientation defaulted per profile (a mover hangs, a par stands).
`bars[]` entries get the same treatment. Reversible, and old shows keep opening
on the 2D stage untouched.

### c. Light primitives become 3D

`stage/light.py` is the cleanest seam in the engine: 16 floats, one
`array('f')`, five kinds. The 3D version is the same idea with the slots
re-spent — `(x,y,z)` position, `(dx,dy,dz)` direction, half-angle, length — and
`foreshorten`/`axis_deg` deleted. `STRIDE` stays 16 if `hardness`/`softness` get
packed; 20 if not, and widening costs nothing.

`emit_lights()` then becomes the *only* output of an emission instead of the
second of two. Every `render()` (QGraphicsItem painting) goes away, and
`emit_lights` is already written for all of them.

### d. A camera

Perspective view-projection replacing `StageView._scene_to_clip`'s affine.
Orbit/pan/dolly, plus saved viewpoints (front of house, followspot position,
plan view for rigging).

**Known landmine:** `stage/gpu/fog.py` states "this stage's camera never moves
except on a resize — the history is valid or it is gone," and
`_frame_signature()` deliberately excludes time so `_settle()` cannot reset its
own countdown. A moving camera invalidates temporal accumulation every frame.
Either add motion-vector reprojection, or drop the temporal pass while the
camera moves and let it re-converge on release. The second is honest and cheap;
do that first.

### e. Light collides with surfaces

The actual feature. Two independent problems — do not conflate them.

**Surface lighting** — the pool on the floor. Per pixel you need `(P, N,
albedo)` and the light list. For a spot: vector to the lamp, angle against the
beam axis, radial falloff, inverse-square, `N·L`, gobo sampled by projecting `P`
into the beam's own frame. The elliptical smear of an oblique beam falls out of
the geometry for free — no special case anywhere.

**Volumetric truncation** — the shaft must *stop* at the floor. That is one line
in the existing march: clamp `t1` to the camera ray's surface hit. The march is
already segment-solved rather than swept, so it slots straight in.

Shadows are a third problem, and a white empty room does not have any. Skip
them; the light-list structure does not preclude a shadow pass later.

---

## 3. The architectural hinge

**The lighting math only ever needs `(P, N, albedo)` per pixel.** Where that
comes from is swappable, so picking the cheap source first costs nothing later:

- **Phase 1 — analytic box.** The room is six planes. Ray/box intersection in
  the fragment shader is ~20 instructions: no meshes, no depth buffer, no scene
  graph, one fullscreen quad. A white room is genuinely *easier* this way than
  with geometry, and it matches how `stage/gpu/` already works.
- **Later — G-buffer.** When you want a truss, a riser, a drum kit or actual
  fixture models: a conventional raster pass writes depth/normal/albedo, and the
  identical lighting function reads that instead. Same shader, different source
  of `(P, N)`.

Fixture bodies in phase 1 are impostors or SDF boxes/cylinders in the same
trace — enough to see a head pan and tilt, which is most of what the body is
*for* on a virtual stage. `scripts/stage3d_draft.py` is exactly this, working.

Stay on raw GL 4.1 through `QOpenGLFunctions_4_1_Core`. The context negotiation
in `stage/gpu/layer.py` — force desktop GL, never request a version, or
Wayland/EGL hands out GL ES — was hard-won and is not worth re-discovering under
Qt3D (effectively deprecated) or QRhi (barely exposed in PyQt6, and we are
pinned to 6.8).

---

## 4. Rough shape of the work

0. Units + `z` + orientation on the patch, migration, room in `show.json`. No
   visual change; the 2D stage ignores the new fields.
1. `Stage3DView`: white box, perspective camera, fixtures as boxes, no light at
   all. Proves camera, interaction and the pipeline.
2. 3D light list + direct surface lighting. **This is the "light hits the floor"
   moment**, and the one that will look like magic.
3. Port the march. Mostly mechanical — the scattering math is right already.
4. Gobos as projected textures (`stage/wheelart.py`'s procedural art renders
   into a texture once, code survives), real pan/tilt on bodies, prisms, blades.
5. 3D designer UI, room presets, camera bookmarks.

Phases 0–2 are where the payoff is concentrated.

---

## 5. What is genuinely hard, and it is not the shading

- **The designer UI.** Placing fixtures in 3D with a mouse is a real subsystem —
  drag-on-plane, gizmos, or a linked plan+elevation pair. Consistently
  underestimated, and probably the largest single chunk.
- **Deciding the raster path's fate.** GL fails soft to raster today, tests are
  GL-free by design (`tests/` cannot make a context under offscreen), and
  `ScenePreview` in `ui/fixture/console.py` *is* the stage engine. The 3D stage
  has to be optional, which means carrying two renderers for a while — or
  explicitly deciding the 2D stage becomes legacy.
- **Deleting the compensations, not porting them.** `PAN_TO_SCREEN`,
  `MAX_SWING_DEG`, `FORESHORTEN_FLOOR`, `GLOW_REACH = 60`, `BEAM_RADIANCE`, the
  drawn-length-vs-real-throw split — all of it exists to fake depth. In a room
  every one of them is wrong by construction. Porting them is the failure mode
  that produces a 3D stage that looks worse than the 2D one.
- **The character change.** Today the renderer's job is *a picture that reads*.
  In 3D its job is *physical consistency*, and the picture is a consequence.
  Different tuning, different bugs, different tests.

Good news on tests: the survivors are the Qt-free math ones — `test_light_list`,
`test_fog_math`, `test_stage_optics`, `test_capability_coverage` — and ray/box,
ray/cone and direct lighting are all pure functions testable the same way.

---

## 6. So, how far?

**Closer than it looks on the domain side, further than it looks on the
rendering side.** The expensive, boring, correctness-critical 60% — OFL, fixture
state, capabilities, optics, colour, atmosphere, tonemapping, scattering — is
done and transfers whole. What is missing is a world, a camera and a
surface-shading pass: a new renderer of maybe 1500–2500 lines of Python + GLSL
on top of the existing GPU infrastructure, plus a designer UI that is harder
than it sounds.

The single highest-leverage first move is **phase 0**: put `z` and orientation
on the patch and metres on the world, while the 2D stage keeps running. Nothing
renders differently, and every later phase stops being blocked on a data
migration.

---

## 7. The prototype

`scripts/stage3d_draft.py` is section 3's phase-1 recommendation, standing up:
an analytic white box, a perspective orbit camera, and the *real* `par_rgb` and
`moving-head` definitions driven by *real* DMX frames through
`core.fixture_state.resolve_state`, with `stage.optics` deciding whether each
one throws a shaft. Direct lighting on the room's surfaces, a volumetric march
for the shafts, and the app's own ACES resolve.

It also confirms the reuse claim from the other direction: the first thing that
looked wrong in 3D was the yoke stepping, and the fix was to import the 2D
stage's `Slew` unchanged. An 8-bit pan channel over 540 degrees moves 2.109
degrees at a time — it moved on 29 frames in 228 and stood still on the rest —
and a room makes that *more* visible, not less, because the pool on the floor
teleports a quarter of a metre with it. Everything in the support layer that is
about a fixture rather than about a picture transfers this way.

```bash
uv run python scripts/stage3d_draft.py
```

What it demonstrates, and what it deliberately does not, is documented in its
own module docstring.
