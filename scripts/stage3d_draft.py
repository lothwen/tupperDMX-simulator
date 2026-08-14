#!/usr/bin/env python3
# ABOUTME: Standalone 3D-stage prototype — a white room, lit by the app's own
# ABOUTME: par-rgbw and moving-head definitions driven by real DMX frames.
"""A draft of what a 3D virtual stage could look like.

Run it:

    uv run python scripts/stage3d_draft.py

Standalone on purpose: it opens its own window, owns its own GL context and
touches nothing in `ui/` or `stage/controller.py`. What it borrows is the half
of the app that a third dimension does not invalidate —

  * `core.fixture_library.resolve_fixture` loads the **real** `fixtures/*.json`
    definitions, at a real mode;
  * `core.fixture_state.resolve_state` turns a **real** DMX frame into colour,
    dimmer, pan, tilt and zoom, exactly as the 2D stage does;
  * `stage.optics.beam_kind` decides whether a fixture throws a shaft at all,
    and `resting_half_angle` how wide — so the 220 degree PAR washes without a
    beam while the moving head cuts one, with no per-fixture code here;
  * `stage.registry.renderer_for` supplies the category's `beam_fallback`;
  * `stage.geometry.Slew` gives the yoke its inertia — an 8-bit pan channel
    over 540 degrees steps 2.109 degrees at a time, and a room makes that
    quantisation more visible rather than less;
  * `stage.light.linear_rgb` is the same sRGB->linear table the GPU layer uses.

— which is the point being demonstrated. Everything below the `Frame` contract
is new, and everything above it was reused unchanged.

What is new, and what the draft in `docs/3d-stage-draft.md` calls phase 1:

  * the room is **analytic**. Six planes, one ray/box intersection in the
    fragment shader: no meshes, no depth buffer, no scene graph. Fixture bodies
    are signed-distance boxes and cylinders sphere-traced in the same pass.
  * **light collides with surfaces.** A camera ray hits a wall, and the shading
    loop asks every light what it puts there — cone angle, inverse square,
    N.L. The ellipse an oblique beam smears across the floor is not drawn, it
    is what the geometry does.
  * **the shaft stops at the floor**, because the volumetric march runs from the
    eye to that same surface hit and no further.
  * the resolve is the app's ACES fit, so two beams crossing add past full
    scale instead of clipping.

What it deliberately is NOT: gobo projection, prisms, shadows, strobe, colour
wheels, more than one body per fixture, any persistence, or any attempt at the
designer UI. Those are phases 2-5 in the draft, and none of them change the
shape of what is here.

Controls
    drag            orbit the camera
    wheel           dolly in and out
    W/S/A/D         walk the focus point around the room
    [ / ]           less / more haze
    - / =           exposure
    space           pause the animation
    R               reset the camera
    Esc             quit
"""

import math
import os
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QOpenGLContext, QSurfaceFormat
from PyQt6.QtWidgets import QApplication, QLabel
from PyQt6.QtOpenGLWidgets import QOpenGLWidget

from core.fixture_library import resolve_fixture
from core.fixture_state import FixtureState, resolve_state
from stage.geometry import Slew
from stage.light import linear_rgb
from stage.optics import BeamKind, beam_kind, resting_half_angle
from stage.registry import renderer_for

Vec3 = Tuple[float, float, float]

# --------------------------------------------------------------------------
# The room, in metres. The one unit decision the whole prototype rests on:
# everything here is a real length, which is precisely what the 2D stage cannot
# say (its scene units are background-image pixels).
# --------------------------------------------------------------------------
ROOM: Vec3 = (9.0, 5.0, 11.0)          # width (x), height (y), depth (z)
WALL_ALBEDO = 0.82                     # a white room, but not a perfect one
CHASSIS_ALBEDO = 0.08                  # fixture bodies: matte black, as they are

# How much light each fixture makes, in the arbitrary units the exposure control
# exists to absorb. Tuned by eye at exposure 1.0; the app would derive these
# from the definition's declared lumens/watts instead.
PAR_OUTPUT = 8.0
HEAD_OUTPUT = 16.0

MAX_LIGHTS = 8                         # mirrored in the shader

# How long the yoke takes to catch up with the DMX it is being sent, in seconds.
#
# Not a cosmetic smoothing: an 8-bit pan channel over a 540 degree range is
# 2.109 degrees per step, so a head crossing this room teleports the pool a
# quarter of a metre nine times a second and stands still in between. A real
# fixture has mass and a motor, which is what fills those gaps in, and this is
# the same `Slew` the 2D stage uses for exactly the same reason. A fixture's own
# Pan/Tilt Speed channel would set it (the 18ch mode has one); the head's other
# answer is its fine channels, which put the step at 0.008 degrees instead.
SLEW_TAU = 0.14


# ==========================================================================
# Small vector helpers. Deliberately plain tuples — nothing here is hot, and a
# dependency on numpy would make the "standalone" claim less true.
# ==========================================================================

def vadd(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def vsub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vscale(a: Vec3, k: float) -> Vec3:
    return (a[0] * k, a[1] * k, a[2] * k)


def vcross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def vnorm(a: Vec3) -> Vec3:
    length = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
    return a if length < 1e-9 else vscale(a, 1.0 / length)


def rot_y(v: Vec3, radians: float) -> Vec3:
    """Rotate about the world's up axis — which is what a pan motor does."""
    cos, sin = math.cos(radians), math.sin(radians)
    return (v[0] * cos + v[2] * sin, v[1], -v[0] * sin + v[2] * cos)


def basis_from_dir(forward: Vec3, right_hint: Optional[Vec3] = None):
    """An orthonormal basis whose local +Z is `forward`.

    The fixture bodies are drawn in their own frame, so each one needs this;
    `right_hint` lets a moving head keep its yoke axis as the local X rather
    than whatever an arbitrary up-vector would produce.
    """
    z_axis = vnorm(forward)
    if right_hint is not None:
        x_axis = vnorm(right_hint)
    else:
        up = (0.0, 1.0, 0.0) if abs(z_axis[1]) < 0.95 else (0.0, 0.0, 1.0)
        x_axis = vnorm(vcross(up, z_axis))
    y_axis = vnorm(vcross(z_axis, x_axis))
    return x_axis, y_axis, z_axis


def hsv_rgb(hue: float, sat: float, val: float) -> Tuple[int, int, int]:
    """One colour, 0-255. `colorsys` would do, but this keeps the maths visible."""
    hue = hue % 1.0
    sector = int(hue * 6.0) % 6
    frac = hue * 6.0 - int(hue * 6.0)
    p = val * (1.0 - sat)
    q = val * (1.0 - sat * frac)
    t = val * (1.0 - sat * (1.0 - frac))
    rgb = [(val, t, p), (q, val, p), (p, val, t),
           (p, q, val), (t, p, val), (val, p, q)][sector]
    return tuple(int(round(c * 255)) for c in rgb)


# ==========================================================================
# The rig. Each fixture is a real FixtureDef, a place in the room, and a rule
# for what DMX it is being sent this second.
# ==========================================================================

def slot_index(fdef, cap_type: str, color: Optional[str] = None) -> int:
    """Which DMX slot of this fixture carries `cap_type`, or -1.

    By capability, never by channel name: it is the same question the whole app
    asks, and it is why this script works for both of the moving head's modes
    without knowing which one it got.
    """
    for index, spec in enumerate(fdef.channels):
        if spec.fine_of >= 0:
            continue
        for cap in spec.capabilities:
            if cap.type != cap_type:
                continue
            if color is None or cap.get("color") == color:
                return index
    return -1


@dataclass
class Emitter:
    """One light, as the shader wants it: a place, a direction and a cone."""
    position: Vec3
    direction: Vec3
    rgb: Vec3                # linear, already scaled by the fixture's output
    cos_outer: float
    cos_inner: float
    reach: float
    air: float               # how much of a shaft this makes, 0..1


@dataclass
class Fixture:
    """A patched fixture: its definition, where it hangs, and its live state."""
    key: str
    mode: str
    position: Vec3
    fdef: object = None
    state: Optional[FixtureState] = None
    aim: Vec3 = (0.0, -1.0, 0.0)
    kind: BeamKind = BeamKind.NONE
    slots: dict = field(default_factory=dict)

    def resolve(self) -> None:
        """Load the definition and ask the app what kind of light it throws."""
        self.fdef = resolve_fixture(self.key, self.mode)
        if self.fdef is None:
            raise SystemExit(f"fixture '{self.key}' is not in the library")
        # The profile's own fallback, exactly as the 2D stage gets it: a moving
        # head that declares nothing still has to throw something.
        fallback = getattr(renderer_for(self.fdef), "beam_fallback", BeamKind.NONE)
        self.kind = beam_kind(self.fdef, fallback)
        self.slots = {
            "intensity": slot_index(self.fdef, "Intensity"),
            "red": slot_index(self.fdef, "ColorIntensity", "Red"),
            "green": slot_index(self.fdef, "ColorIntensity", "Green"),
            "blue": slot_index(self.fdef, "ColorIntensity", "Blue"),
            "white": slot_index(self.fdef, "ColorIntensity", "White"),
            "shutter": slot_index(self.fdef, "ShutterStrobe"),
            "pan": slot_index(self.fdef, "Pan"),
            "tilt": slot_index(self.fdef, "Tilt"),
            "zoom": slot_index(self.fdef, "Zoom"),
        }

    # -- per frame -----------------------------------------------------
    def drive(self, dmx: bytes) -> None:
        """One DMX frame, through the app's own resolver."""
        self.state = resolve_state(self.fdef, dmx)

    def half_angle_deg(self) -> Optional[float]:
        """The live cone half-angle. A zoom channel wins, exactly as
        `stage.features.beamshape.ZoomFeature` decides it."""
        zoom = self.state.optics.zoom_deg if self.state else None
        if zoom is not None:
            return max(1.0, zoom) / 2.0
        return resting_half_angle(self.fdef, self.kind)

    def emitter(self, output: float) -> Optional[Emitter]:
        """This fixture as a light, or None while it is dark."""
        state = self.state
        if state is None or state.strobe.effect == "Closed":
            return None
        rgb255 = state.scaled_rgb()
        if max(rgb255) <= 0:
            return None

        half = self.half_angle_deg()
        if half is None:
            # A surface light: no shaft in the air, but it still throws light
            # onto whatever is in front of it. `beam_kind` said NONE, and this
            # is what NONE means in a room.
            half = 88.0
        half = min(half, 89.0)

        # A hard edge keeps its shape; a wash dissolves into one. Same
        # distinction `optics.py` draws, spent on the penumbra instead of on
        # whether to draw a cone.
        inner_scale = 0.86 if self.kind is BeamKind.HARD else 0.35
        air = {BeamKind.HARD: 1.0, BeamKind.WASH: 0.55}.get(self.kind, 0.12)

        linear = linear_rgb(rgb255)
        return Emitter(
            position=vadd(self.position, vscale(self.aim, self.lens_offset())),
            direction=self.aim,
            rgb=vscale(linear, output),
            cos_outer=math.cos(math.radians(half)),
            cos_inner=math.cos(math.radians(half * inner_scale)),
            reach=18.0 if self.kind is BeamKind.HARD else 10.0,
            air=air,
        )

    def body_half_extents(self) -> Vec3:
        """The chassis, at the size the definition says it is.

        `stage/metrics.py` asks the same question and answers it in
        millimetres-per-scene-pixel; in a room the conversion is the identity,
        which is most of the argument for having a room. Local Z is the aim, and
        a fixture throws light out of the end OFL calls its *height* — down a
        par's can, out of a moving head's lens.
        """
        dims = self.fdef.physical.dimensions_mm or (180.0, 200.0, 180.0)
        width, height, depth = (value / 1000.0 for value in dims)
        return (width / 2.0, depth / 2.0, height / 2.0)

    def lens_offset(self) -> float:
        """How far the lens sits in front of the fixture's own origin."""
        return self.body_half_extents()[2] + 0.04


# ==========================================================================
# The show: two fixtures and a minute of choreography.
# ==========================================================================

class Rig:
    """The whole prototype rig, and the DMX being sent to it."""

    def __init__(self):
        self.par = Fixture(key="par-rgbw", mode="6ch",
                           position=(2.35, 0.62, 6.3))
        self.head = Fixture(key="moving-head", mode="12ch",
                            position=(ROOM[0] * 0.5, ROOM[1] - 0.95, 4.6))
        for fixture in (self.par, self.head):
            fixture.resolve()

        # The PAR stands on the floor and rakes the back wall. Static — it has
        # no pan or tilt channel, and in a room that is a fact about the
        # fixture rather than something the renderer has to invent.
        self.par.aim = vnorm((0.22, 0.62, -0.75))

        # The yoke's inertia. One integrator per axis, exactly as a body on the
        # 2D stage carries one.
        self.pan_slew = Slew()
        self.tilt_slew = Slew()

        # Live read-outs for the HUD: what the DMX asked for, and where the
        # yoke has actually got to.
        self.pan_dmx_deg = 0.0
        self.tilt_dmx_deg = 0.0
        self.pan_deg = 0.0
        self.tilt_deg = 0.0
        self.zoom_deg = 0.0
        self.head_pan_rad = 0.0

    # -- choreography --------------------------------------------------
    def tick(self, t: float) -> None:
        self.par.drive(self._par_dmx(t))
        self.head.drive(self._head_dmx(t))
        self._aim_head()

    def _par_dmx(self, t: float) -> bytes:
        frame = bytearray(self.par.fdef.channel_count)
        rgb = hsv_rgb(0.58 + 0.12 * math.sin(t * 0.19), 0.85, 1.0)
        self._put(frame, self.par.slots, intensity=210,
                  red=rgb[0], green=rgb[1], blue=rgb[2])
        return bytes(frame)

    def _head_dmx(self, t: float) -> bytes:
        frame = bytearray(self.head.fdef.channel_count)
        # Pan and tilt as bytes, not as angles: the definition owns the mapping
        # (540 and 270 degrees here), and `resolve_state` applies it. The
        # amplitudes are chosen in bytes for that reason — +-30 of 255 is +-63
        # degrees of pan on this fixture and would be something else on another.
        pan = 128 + int(30 * math.sin(t * 0.42))
        tilt = 148 + int(18 * math.sin(t * 0.31 + 1.1))
        rgb = hsv_rgb(t * 0.055, 0.95, 1.0)
        self._put(frame, self.head.slots,
                  pan=pan, tilt=tilt, intensity=255,
                  # 240-255 is Open on this fixture; 255 is NOT "on" in general,
                  # which is the same trap `tests/_lit_frame` exists to avoid.
                  shutter=250,
                  red=rgb[0], green=rgb[1], blue=rgb[2],
                  zoom=128 + int(120 * math.sin(t * 0.23)))
        return bytes(frame)

    @staticmethod
    def _put(frame: bytearray, slots: dict, **values) -> None:
        for name, value in values.items():
            index = slots.get(name, -1)
            if 0 <= index < len(frame):
                frame[index] = max(0, min(255, int(value)))

    def _aim_head(self) -> None:
        """Turn pan and tilt into a direction in the room.

        This is the whole difference between the two renderers in six lines.
        `stage/geometry.py` maps pan onto a swing across a picture, halves it so
        the beam cannot swing back through the fixture's own body, and clamps
        the result to 80 degrees. Here pan is a rotation about the world's up
        axis and tilt a rotation about the yoke, because there is a world for
        them to be rotations in.

        What it does NOT change is that DMX arrives in steps. The angles the
        state resolver hands back are quantised to the byte that produced them,
        so both go through a `Slew` on the way to being a direction — see
        `SLEW_TAU`. The 2D stage does the same thing for the same reason, which
        is why this borrows its integrator rather than inventing one.
        """
        motion = self.head.state.motion
        pan_mid, tilt_mid = 270.0, 135.0
        self.pan_dmx_deg = motion.pan_deg if motion.pan_deg is not None else pan_mid
        self.tilt_dmx_deg = (motion.tilt_deg if motion.tilt_deg is not None
                             else tilt_mid)
        zoom = self.head.state.optics.zoom_deg
        self.zoom_deg = zoom if zoom is not None else 0.0

        # Wall-clock, not show time: the yoke keeps travelling while the
        # animation is paused, and stops where the DMX left it.
        now = time.monotonic()
        self.pan_deg = self.pan_slew.advance(self.pan_dmx_deg, now, SLEW_TAU)
        self.tilt_deg = self.tilt_slew.advance(self.tilt_dmx_deg, now, SLEW_TAU)

        pan = math.radians(self.pan_deg - pan_mid)
        tilt = math.radians(self.tilt_deg - tilt_mid)
        # Home is straight down — where a head hung from a truss rests.
        self.head_pan_rad = pan
        self.head.aim = rot_y((0.0, -math.cos(tilt), math.sin(tilt)), pan)

    # -- what the renderer needs ---------------------------------------
    def emitters(self) -> List[Emitter]:
        found = [self.par.emitter(PAR_OUTPUT), self.head.emitter(HEAD_OUTPUT)]
        return [e for e in found if e is not None][:MAX_LIGHTS]


# ==========================================================================
# Shaders
# ==========================================================================

VERTEX = """
#version 330
in vec2 a_corner;
out vec2 v_ndc;
void main() {
    v_ndc = a_corner;
    gl_Position = vec4(a_corner, 0.0, 1.0);
}
"""

FRAGMENT = """
#version 330

in vec2 v_ndc;
out vec4 fragColor;

const int   MAX_LIGHTS = 8;
const float PI = 3.14159265359;

// How many samples the volumetric march takes between the eye and whatever it
// hit. The one number to turn down on a slow GPU.
const int MARCH_STEPS = 48;
const int TRACE_STEPS = 112;

uniform vec3  u_cam_pos;
uniform vec3  u_cam_right;
uniform vec3  u_cam_up;
uniform vec3  u_cam_fwd;
uniform float u_tan_half_fov;
uniform float u_aspect;

uniform vec3  u_room;            // the box runs from the origin to here
uniform float u_wall_albedo;
uniform float u_chassis_albedo;
uniform float u_exposure;
uniform float u_haze;
uniform float u_time;

uniform int   u_light_count;
uniform vec3  u_light_pos[MAX_LIGHTS];
uniform vec3  u_light_dir[MAX_LIGHTS];
uniform vec3  u_light_rgb[MAX_LIGHTS];
uniform vec4  u_light_shape[MAX_LIGHTS];   // cos_outer, cos_inner, reach, air

// Fixture bodies, each in its own frame. `_z` is the aim, `_size` the half
// extents the definition's own `physical.dimensions` gave us.
uniform vec3 u_par_pos;
uniform vec3 u_par_x;
uniform vec3 u_par_y;
uniform vec3 u_par_z;
uniform vec3 u_par_size;
uniform vec3 u_head_pos;
uniform vec3 u_head_x;
uniform vec3 u_head_y;
uniform vec3 u_head_z;
uniform vec3 u_head_size;
uniform vec3 u_yoke_x;           // the yoke follows pan only
uniform vec3 u_yoke_z;

// ---------------------------------------------------------------- noise ---
float hash13(vec3 p) {
    p = fract(p * 0.1031);
    p += dot(p, p.yzx + 33.33);
    return fract((p.x + p.y) * p.z);
}

float vnoise(vec3 x) {
    vec3 i = floor(x);
    vec3 f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(mix(hash13(i + vec3(0, 0, 0)), hash13(i + vec3(1, 0, 0)), f.x),
                   mix(hash13(i + vec3(0, 1, 0)), hash13(i + vec3(1, 1, 0)), f.x), f.y),
               mix(mix(hash13(i + vec3(0, 0, 1)), hash13(i + vec3(1, 0, 1)), f.x),
                   mix(hash13(i + vec3(0, 1, 1)), hash13(i + vec3(1, 1, 1)), f.x), f.y),
               f.z);
}

// ------------------------------------------------------------ the room ----
// The camera is inside the box, so there is exactly one exit and its face is
// whichever slab runs out first. Twelve instructions, no geometry, no depth
// buffer: this is the whole argument for starting with an analytic room.
float room_exit(vec3 origin, vec3 dir, out vec3 normal) {
    vec3 safe = vec3(abs(dir.x) < 1e-6 ? 1e-6 : dir.x,
                     abs(dir.y) < 1e-6 ? 1e-6 : dir.y,
                     abs(dir.z) < 1e-6 ? 1e-6 : dir.z);
    vec3 inv = 1.0 / safe;
    vec3 far = max((vec3(0.0) - origin) * inv, (u_room - origin) * inv);
    float t = min(min(far.x, far.y), far.z);
    if (t == far.x)      { normal = vec3(safe.x > 0.0 ? -1.0 : 1.0, 0.0, 0.0); }
    else if (t == far.y) { normal = vec3(0.0, safe.y > 0.0 ? -1.0 : 1.0, 0.0); }
    else                 { normal = vec3(0.0, 0.0, safe.z > 0.0 ? -1.0 : 1.0); }
    return t;
}

// -------------------------------------------------------- the fixtures ---
float sd_box(vec3 p, vec3 b) {
    vec3 q = abs(p) - b;
    return length(max(q, 0.0)) + min(max(q.x, max(q.y, q.z)), 0.0);
}

// A cylinder along its own local +Z, which is how a lamp is built: the barrel
// points where the light goes.
float sd_can(vec3 p, float radius, float half_length) {
    vec2 d = vec2(length(p.xy) - radius, abs(p.z) - half_length);
    return min(max(d.x, d.y), 0.0) + length(max(d, 0.0));
}

vec3 to_local(vec3 p, vec3 origin, vec3 ax, vec3 ay, vec3 az) {
    vec3 q = p - origin;
    return vec3(dot(q, ax), dot(q, ay), dot(q, az));
}

// The rig as one distance field. Two fixtures is few enough that they are
// simply written out; a real implementation would upload a small instance
// buffer, or rasterise meshes into a G-buffer and keep the shading identical.
float map(vec3 p) {
    // -- the PAR: a can on a short stand, raking the back wall
    float barrel = max(u_par_size.x, u_par_size.y);
    vec3 lp = to_local(p, u_par_pos, u_par_x, u_par_y, u_par_z);
    float d = sd_can(lp, barrel, u_par_size.z);
    d = min(d, sd_can(lp - vec3(0.0, 0.0, u_par_size.z + 0.015),
                      barrel * 1.06, 0.018));                    // the lens rim
    d = min(d, sd_box(p - vec3(u_par_pos.x, u_par_pos.y * 0.5, u_par_pos.z),
                      vec3(0.028, u_par_pos.y * 0.5, 0.028)));   // the stem
    d = min(d, sd_box(p - vec3(u_par_pos.x, 0.012, u_par_pos.z),
                      vec3(barrel * 1.8, 0.012, barrel * 1.8))); // the base plate

    // -- the moving head, hung from the ceiling. Its yoke is proportioned off
    //    the head it has to carry, so a bigger definition builds a bigger rig.
    float arm_x = u_head_size.x + 0.05;
    float arm_y = u_head_size.z + 0.08;
    vec3 mp = p - u_head_pos;
    d = min(d, sd_box(mp - vec3(0.0, arm_y * 1.15 + 0.09, 0.0),
                      vec3(arm_x + 0.04, 0.045, u_head_size.y + 0.06)));

    // The yoke turns with pan alone; the head with pan and tilt. Drawing them
    // apart is what makes a moving head read as one.
    vec3 yp = vec3(dot(mp, u_yoke_x), mp.y, dot(mp, u_yoke_z));
    d = min(d, sd_box(yp - vec3(0.0, arm_y * 1.1, 0.0),
                      vec3(arm_x, 0.03, u_head_size.y * 0.95)));
    d = min(d, sd_box(yp - vec3( arm_x, arm_y * 0.5, 0.0),
                      vec3(0.028, arm_y * 0.6, u_head_size.y * 0.85)));
    d = min(d, sd_box(yp - vec3(-arm_x, arm_y * 0.5, 0.0),
                      vec3(0.028, arm_y * 0.6, u_head_size.y * 0.85)));

    vec3 hp = to_local(p, u_head_pos, u_head_x, u_head_y, u_head_z);
    d = min(d, sd_box(hp, u_head_size));
    d = min(d, sd_can(hp - vec3(0.0, 0.0, u_head_size.z + 0.015),
                      min(u_head_size.x, u_head_size.y) * 0.85, 0.016));
    return d;
}

vec3 map_normal(vec3 p) {
    vec2 e = vec2(0.0015, 0.0);
    return normalize(vec3(map(p + e.xyy) - map(p - e.xyy),
                          map(p + e.yxy) - map(p - e.yxy),
                          map(p + e.yyx) - map(p - e.yyx)));
}

float trace_bodies(vec3 origin, vec3 dir, float t_max) {
    float t = 0.03;
    for (int i = 0; i < TRACE_STEPS; ++i) {
        float d = map(origin + dir * t);
        if (d < 0.0012 + 0.0015 * t) { return t; }
        t += d * 0.92;
        if (t > t_max) { break; }
    }
    return -1.0;
}

// ------------------------------------------------------------- lighting ---
// How much of light `i` reaches `p`, before any surface orientation. The cone
// test IS the fixture's optics: `cos_outer` came from the definition's lens or
// its live zoom channel, and nothing here knows which.
float light_falloff(int i, vec3 p, out vec3 to_light) {
    vec3 offset = u_light_pos[i] - p;
    float dist = length(offset);
    if (dist < 1e-4) { to_light = vec3(0.0, 1.0, 0.0); return 0.0; }
    to_light = offset / dist;

    float reach = u_light_shape[i].z;
    if (dist > reach) { return 0.0; }
    float cone = smoothstep(u_light_shape[i].x, u_light_shape[i].y,
                            dot(-to_light, u_light_dir[i]));
    if (cone <= 0.0) { return 0.0; }

    float attenuation = 1.0 / (1.0 + 0.35 * dist * dist);
    return cone * attenuation * (1.0 - smoothstep(reach * 0.7, reach, dist));
}

vec3 shade_surface(vec3 p, vec3 n, float albedo) {
    vec3 total = vec3(0.03);            // the room is not in outer space
    for (int i = 0; i < u_light_count; ++i) {
        vec3 to_light;
        float reach = light_falloff(i, p, to_light);
        if (reach <= 0.0) { continue; }
        float lambert = max(dot(n, to_light), 0.0);
        if (lambert <= 0.0) { continue; }
        total += u_light_rgb[i] * (reach * lambert);
    }
    return total * albedo;
}

// Henyey-Greenstein, normalised against its own broadside value so that
// turning anisotropy up brightens the beam you are looking into rather than
// dimming everything else — the same convention `stage/gpu/shaders.py` uses.
float scatter_gain(float cosine, float g) {
    float denom = 1.0 + g * g - 2.0 * g * cosine;
    float side = 1.0 + g * g;
    return (pow(side, 1.5) / pow(max(denom, 1e-4), 1.5));
}

// Where along this ray it is worth sampling at all.
//
// A beam is a narrow cone in a large room: spreading 48 samples between the eye
// and the far wall puts two or three of them inside the shaft, and the shaft
// simply does not appear. That is the same lesson stage/gpu/shaders.py records
// — solve the segment, do not sweep it — so each beam's cone is intersected
// exactly and the march runs over the union of what came back.
//
// A wash is excluded on purpose: `beam_kind` said it throws no shaft, and a
// 176-degree cone would bound the march to the whole room again, undoing this.
bool air_segment(vec3 origin, vec3 dir, float t_end, out float t0, out float t1) {
    t0 = t_end;
    t1 = 0.0;
    bool found = false;

    for (int i = 0; i < u_light_count; ++i) {
        if (u_light_shape[i].w <= 0.25) { continue; }
        vec3 apex = u_light_pos[i];
        vec3 axis = u_light_dir[i];
        float reach = u_light_shape[i].z;

        // The bounding sphere first: four instructions, and the answer when the
        // cone solve degenerates on a ray running along the cone's own surface.
        vec3 co = origin - apex;
        float along = -dot(co, dir);
        float perp = dot(co, co) - along * along;
        if (perp > reach * reach) { continue; }
        float chord = sqrt(reach * reach - perp);
        float enter = along - chord;
        float leave = along + chord;

        // Then the cone itself: dot(p - apex, axis)^2 == cos^2 * |p - apex|^2.
        float kk = u_light_shape[i].x * u_light_shape[i].x;
        float dv = dot(dir, axis);
        float cv = dot(co, axis);
        float qa = dv * dv - kk;
        float qb = 2.0 * (dv * cv - kk * dot(dir, co));
        float qc = cv * cv - kk * dot(co, co);
        if (abs(qa) > 1e-5) {
            float disc = qb * qb - 4.0 * qa * qc;
            if (disc <= 0.0) { continue; }      // the ray misses the beam
            float root = sqrt(disc);
            float ta = (-qb - root) / (2.0 * qa);
            float tb = (-qb + root) / (2.0 * qa);
            enter = max(enter, min(ta, tb));
            leave = min(leave, max(ta, tb));
        }
        enter = max(enter, 0.0);
        leave = min(leave, t_end);
        if (leave <= enter) { continue; }

        // The quadratic solves the infinite DOUBLE cone, so the mirror image
        // behind the lens also comes back. It lights nothing, and one dot
        // product is cheaper than marching it.
        vec3 middle = origin + dir * (0.5 * (enter + leave)) - apex;
        if (dot(middle, axis) <= 0.0) { continue; }

        t0 = min(t0, enter);
        t1 = max(t1, leave);
        found = true;
    }
    return found && t1 > t0;
}

// Everything the air does along that segment. The far end is clamped to the
// surface hit, and that single clamp is what makes a shaft stop at the floor
// instead of passing through it.
vec3 march_air(vec3 origin, vec3 dir, float t_end) {
    if (u_haze <= 0.001) { return vec3(0.0); }
    float t0, t1;
    if (!air_segment(origin, dir, t_end, t0, t1)) { return vec3(0.0); }

    float dt = (t1 - t0) / float(MARCH_STEPS);
    float jitter = hash13(vec3(gl_FragCoord.xy, u_time * 60.0));
    vec3 accumulated = vec3(0.0);
    float transmittance = 1.0;

    for (int k = 0; k < MARCH_STEPS; ++k) {
        vec3 p = origin + dir * (t0 + (float(k) + jitter) * dt);
        // Haze settles: more of it round the ankles than in the truss.
        float height = exp(-max(p.y, 0.0) / 3.2);
        float texture = 0.62 + 0.75 * vnoise(p * 0.55 + vec3(0.0, 0.0, u_time * 0.06));
        float density = u_haze * height * texture;
        if (density < 1e-4) { continue; }

        float sigma = density * 0.85;
        vec3 radiance = vec3(0.0);
        for (int i = 0; i < u_light_count; ++i) {
            float air = u_light_shape[i].w;
            if (air <= 0.0) { continue; }
            vec3 to_light;
            float reach = light_falloff(i, p, to_light);
            if (reach <= 0.0) { continue; }
            float phase = scatter_gain(dot(dir, -to_light), 0.45);
            radiance += u_light_rgb[i] * (reach * air * phase);
        }
        accumulated += transmittance * sigma * radiance * dt * 0.8;
        transmittance *= exp(-sigma * dt * 0.25);
        if (transmittance < 0.02) { break; }
    }
    return accumulated;
}

// The lamp itself, seen down the barrel. Occluded for free: it is only added
// when the light sits in front of whatever the ray hit.
vec3 lamp_glow(vec3 origin, vec3 dir, float t_hit) {
    vec3 total = vec3(0.0);
    for (int i = 0; i < u_light_count; ++i) {
        float along = dot(u_light_pos[i] - origin, dir);
        if (along <= 0.0 || along > t_hit) { continue; }
        float across = length(origin + dir * along - u_light_pos[i]);
        float facing = max(dot(-dir, u_light_dir[i]), 0.0);
        float radius = mix(0.07, 0.15, facing);
        float core = exp(-(across * across) / (radius * radius));
        total += u_light_rgb[i] * core * (0.25 + 0.75 * facing) * 0.4;
    }
    return total;
}

// --------------------------------------------------------------- resolve --
// ACES filmic tonemap, Stephen Hill's fitted RRT+ODT (MIT, from BakingLab),
// lifted verbatim from stage/gpu/shaders.py so the prototype and the app agree
// about what "brighter than white" looks like. The matrices are transposed on
// purpose — see the note there.
const mat3 ACES_IN = mat3(
    0.59719, 0.07600, 0.02840,
    0.35458, 0.90834, 0.13383,
    0.04823, 0.01566, 0.83777);
const mat3 ACES_OUT = mat3(
     1.60475, -0.10208, -0.00327,
    -0.53108,  1.10813, -0.07276,
    -0.07367, -0.00605,  1.07602);

vec3 rrt_and_odt_fit(vec3 v) {
    vec3 a = v * (v + 0.0245786) - 0.000090537;
    vec3 b = v * (0.983729 * v + 0.4329510) + 0.238081;
    return a / b;
}

vec3 aces_fitted(vec3 colour) {
    colour = ACES_IN * colour;
    colour = rrt_and_odt_fit(colour);
    colour = ACES_OUT * colour;
    return clamp(colour, 0.0, 1.0);
}

void main() {
    vec3 dir = normalize(u_cam_fwd
                         + u_cam_right * (v_ndc.x * u_aspect * u_tan_half_fov)
                         + u_cam_up * (v_ndc.y * u_tan_half_fov));

    vec3 wall_normal;
    float t_wall = room_exit(u_cam_pos, dir, wall_normal);
    float t_body = trace_bodies(u_cam_pos, dir, t_wall);

    float t_hit = t_body > 0.0 ? t_body : t_wall;
    vec3 hit = u_cam_pos + dir * t_hit;
    vec3 normal = t_body > 0.0 ? map_normal(hit) : wall_normal;
    float albedo = t_body > 0.0 ? u_chassis_albedo : u_wall_albedo;

    vec3 hdr = shade_surface(hit, normal, albedo);
    hdr += march_air(u_cam_pos, dir, t_hit);
    hdr += lamp_glow(u_cam_pos, dir, t_hit);

    vec3 mapped = aces_fitted(hdr * u_exposure);
    fragColor = vec4(pow(max(mapped, 0.0), vec3(1.0 / 2.2)), 1.0);
}
"""


# ==========================================================================
# The view
# ==========================================================================

class Stage3DView(QOpenGLWidget):
    """One fullscreen quad, and a fragment shader that is the whole renderer."""

    def __init__(self, rig: Rig):
        super().__init__()
        self.rig = rig
        self.gl = None
        self.program = None
        self.vao = None
        self.quad = None
        self.failed = False

        # Camera: an orbit around a focus point, kept inside the room.
        self.focus: Vec3 = (ROOM[0] * 0.5, 1.85, ROOM[2] * 0.38)
        self.yaw = 0.0
        self.pitch = 0.02
        self.radius = 7.0
        self._drag = None

        self.exposure = 1.25
        self.haze = 0.32
        self.paused = False
        self.started = time.monotonic()
        self.show_time = 0.0

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(720, 460)

        self.hud = QLabel(self)
        self.hud.setStyleSheet(
            "color: #e8ecf4; background: rgba(0,0,0,150); padding: 8px 10px;"
            "font-family: monospace; font-size: 11px; border-radius: 4px;")
        self.hud.move(12, 12)

        # Measured in paintGL. `fps` is the interval between frames actually
        # painted, so it reports the truth whichever side is the bottleneck: the
        # timer, vsync, or the march. `draw_ms` is the CPU spent submitting one.
        self._last_paint: Optional[float] = None
        self.fps = 0.0
        self.draw_ms = 0.0

        # 60 Hz, not the stage's 30: `Slew` fills in the gaps between DMX steps,
        # and it can only do that on frames it is given.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)

    # -- camera ---------------------------------------------------------
    def camera_position(self) -> Vec3:
        horizontal = math.cos(self.pitch) * self.radius
        offset = (math.sin(self.yaw) * horizontal,
                  math.sin(self.pitch) * self.radius,
                  math.cos(self.yaw) * horizontal)
        position = vadd(self.focus, offset)
        # The analytic room only makes sense from the inside, so the camera is
        # kept there. A real implementation would let it leave and switch to the
        # box's exterior intersection instead.
        margin = 0.45
        return tuple(max(margin, min(ROOM[axis] - margin, position[axis]))
                     for axis in range(3))

    def camera_basis(self):
        position = self.camera_position()
        forward = vnorm(vsub(self.focus, position))
        right = vnorm(vcross(forward, (0.0, 1.0, 0.0)))
        up = vnorm(vcross(right, forward))
        return position, right, up, forward

    # -- animation ------------------------------------------------------
    def _tick(self) -> None:
        if not self.paused:
            self.show_time = time.monotonic() - self.started
        self.rig.tick(self.show_time)
        self._update_hud()
        self.update()

    def _update_hud(self) -> None:
        head = self.rig.head
        rgb = head.state.scaled_rgb() if head.state else (0, 0, 0)
        half = head.half_angle_deg() or 0.0
        self.hud.setText(
            f"{self.fps:5.1f} fps   draw {self.draw_ms:4.2f} ms"
            f"   room {ROOM[0]:.0f} x {ROOM[1]:.0f} x {ROOM[2]:.0f} m"
            f"   haze {self.haze:.2f}   exposure {self.exposure:.2f}\n"
            f"moving-head  pan {self.rig.pan_deg:6.1f}d (dmx {self.rig.pan_dmx_deg:6.1f})"
            f"  tilt {self.rig.tilt_deg:6.1f}d (dmx {self.rig.tilt_dmx_deg:6.1f})\n"
            f"             zoom {self.rig.zoom_deg:4.1f}d -> half {half:4.1f}d"
            f"  rgb {rgb}  [{head.kind.value}]\n"
            f"par-rgbw     static      "
            f"  rgb {self.rig.par.state.scaled_rgb() if self.rig.par.state else ()}"
            f"  [{self.rig.par.kind.value}]\n"
            f"drag orbit | wheel dolly | WASD walk | [ ] haze | - = exposure"
            f" | space pause | R reset")
        self.hud.adjustSize()

    # -- GL -------------------------------------------------------------
    def initializeGL(self) -> None:
        from PyQt6.QtOpenGL import (
            QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram,
            QOpenGLVersionFunctionsFactory, QOpenGLVersionProfile,
            QOpenGLVertexArrayObject,
        )

        context = QOpenGLContext.currentContext()
        if context is None or context.isOpenGLES():
            return self._give_up("no desktop OpenGL context")

        profile = QOpenGLVersionProfile()
        profile.setVersion(4, 1)
        profile.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        self.gl = QOpenGLVersionFunctionsFactory.get(profile, context)
        if self.gl is None:
            return self._give_up("no OpenGL 4.1 core functions")
        self.gl.initializeOpenGLFunctions()

        program = QOpenGLShaderProgram()
        if not program.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Vertex, VERTEX):
            return self._give_up(f"vertex shader: {program.log()}")
        if not program.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Fragment, FRAGMENT):
            return self._give_up(f"fragment shader: {program.log()}")
        if not program.link():
            return self._give_up(f"link: {program.log()}")
        self.program = program

        self.vao = QOpenGLVertexArrayObject()
        self.vao.create()

        corners = b""
        import struct
        for x, y in ((-1.0, -1.0), (1.0, -1.0), (-1.0, 1.0), (1.0, 1.0)):
            corners += struct.pack("ff", x, y)
        self.quad = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        self.quad.create()
        self.quad.bind()
        self.quad.allocate(corners, len(corners))
        self.quad.release()

        version = context.format()
        print(f"stage3d: OpenGL {version.majorVersion()}.{version.minorVersion()} ready")

    def _give_up(self, why: str) -> None:
        if not self.failed:
            print(f"stage3d: cannot render ({why})", file=sys.stderr)
        self.failed = True

    def paintGL(self) -> None:
        if self.failed or self.program is None:
            return
        started = time.perf_counter()
        if self._last_paint is not None:
            elapsed = started - self._last_paint
            if elapsed > 1e-6:
                instant = 1.0 / elapsed
                self.fps = (instant if self.fps <= 0.0
                            else self.fps * 0.9 + instant * 0.1)
        self._last_paint = started

        gl = self.gl
        ratio = self.devicePixelRatioF()
        width = max(1, int(self.width() * ratio))
        height = max(1, int(self.height() * ratio))

        gl.glViewport(0, 0, width, height)
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        gl.glClear(0x00004000)          # GL_COLOR_BUFFER_BIT
        gl.glDisable(0x0BE2)            # GL_BLEND — everything here is opaque

        self.vao.bind()
        self.program.bind()
        self._set_uniforms(width / height)

        self.quad.bind()
        corner = self.program.attributeLocation("a_corner")
        self.program.enableAttributeArray(corner)
        self.program.setAttributeBuffer(corner, 0x1406, 0, 2, 0)   # GL_FLOAT
        self.quad.release()

        gl.glDrawArrays(0x0005, 0, 4)   # GL_TRIANGLE_STRIP

        self.program.disableAttributeArray(corner)
        self.program.release()
        self.vao.release()

        spent = (time.perf_counter() - started) * 1000.0
        self.draw_ms = (spent if self.draw_ms <= 0.0
                        else self.draw_ms * 0.9 + spent * 0.1)

    def _set_uniforms(self, aspect: float) -> None:
        from PyQt6.QtGui import QVector3D, QVector4D

        def vec3(name: str, v: Sequence[float]) -> None:
            self.program.setUniformValue(name, QVector3D(*[float(c) for c in v]))

        position, right, up, forward = self.camera_basis()
        vec3("u_cam_pos", position)
        vec3("u_cam_right", right)
        vec3("u_cam_up", up)
        vec3("u_cam_fwd", forward)
        self.program.setUniformValue("u_tan_half_fov",
                                     float(math.tan(math.radians(52.0) * 0.5)))
        self.program.setUniformValue("u_aspect", float(aspect))

        vec3("u_room", ROOM)
        self.program.setUniformValue("u_wall_albedo", float(WALL_ALBEDO))
        self.program.setUniformValue("u_chassis_albedo", float(CHASSIS_ALBEDO))
        self.program.setUniformValue("u_exposure", float(self.exposure))
        self.program.setUniformValue("u_haze", float(self.haze))
        self.program.setUniformValue("u_time", float(self.show_time))

        emitters = self.rig.emitters()
        self.program.setUniformValue("u_light_count", int(len(emitters)))
        for index, emitter in enumerate(emitters):
            vec3(f"u_light_pos[{index}]", emitter.position)
            vec3(f"u_light_dir[{index}]", emitter.direction)
            vec3(f"u_light_rgb[{index}]", emitter.rgb)
            self.program.setUniformValue(
                f"u_light_shape[{index}]",
                QVector4D(float(emitter.cos_outer), float(emitter.cos_inner),
                          float(emitter.reach), float(emitter.air)))

        par_x, par_y, par_z = basis_from_dir(self.rig.par.aim)
        vec3("u_par_pos", self.rig.par.position)
        vec3("u_par_x", par_x)
        vec3("u_par_y", par_y)
        vec3("u_par_z", par_z)
        vec3("u_par_size", self.rig.par.body_half_extents())

        # The yoke turns with pan only; the head takes tilt on top, about that
        # same yoke axis — so the head's local X *is* the yoke's.
        yoke_x = rot_y((1.0, 0.0, 0.0), self.rig.head_pan_rad)
        yoke_z = rot_y((0.0, 0.0, 1.0), self.rig.head_pan_rad)
        head_x, head_y, head_z = basis_from_dir(self.rig.head.aim, yoke_x)
        vec3("u_head_pos", self.rig.head.position)
        vec3("u_head_x", head_x)
        vec3("u_head_y", head_y)
        vec3("u_head_z", head_z)
        vec3("u_head_size", self.rig.head.body_half_extents())
        vec3("u_yoke_x", yoke_x)
        vec3("u_yoke_z", yoke_z)

    # -- interaction ----------------------------------------------------
    def mousePressEvent(self, event) -> None:
        self._drag = event.position()

    def mouseMoveEvent(self, event) -> None:
        if self._drag is None:
            return
        delta = event.position() - self._drag
        self._drag = event.position()
        self.yaw -= delta.x() * 0.006
        self.pitch = max(-1.25, min(1.25, self.pitch + delta.y() * 0.005))
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        self._drag = None

    def wheelEvent(self, event) -> None:
        self.radius = max(1.2, min(16.0,
                                   self.radius * (0.9 if event.angleDelta().y() > 0
                                                  else 1.1)))
        self.update()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        step = 0.35
        forward = vnorm(vsub(self.focus, self.camera_position()))
        flat = vnorm((forward[0], 0.0, forward[2]))
        side = vnorm(vcross(flat, (0.0, 1.0, 0.0)))

        if key == Qt.Key.Key_Escape:
            self.window().close()
        elif key == Qt.Key.Key_Space:
            self.paused = not self.paused
            if not self.paused:
                self.started = time.monotonic() - self.show_time
        elif key == Qt.Key.Key_R:
            self.focus = (ROOM[0] * 0.5, 1.85, ROOM[2] * 0.38)
            self.yaw, self.pitch, self.radius = 0.0, 0.02, 7.0
        elif key == Qt.Key.Key_BracketLeft:
            self.haze = max(0.0, self.haze - 0.04)
        elif key == Qt.Key.Key_BracketRight:
            self.haze = min(1.2, self.haze + 0.04)
        elif key == Qt.Key.Key_Minus:
            self.exposure = max(0.1, self.exposure - 0.1)
        elif key in (Qt.Key.Key_Equal, Qt.Key.Key_Plus):
            self.exposure = min(4.0, self.exposure + 0.1)
        elif key == Qt.Key.Key_W:
            self.focus = vadd(self.focus, vscale(flat, step))
        elif key == Qt.Key.Key_S:
            self.focus = vsub(self.focus, vscale(flat, step))
        elif key == Qt.Key.Key_A:
            self.focus = vsub(self.focus, vscale(side, step))
        elif key == Qt.Key.Key_D:
            self.focus = vadd(self.focus, vscale(side, step))
        else:
            return super().keyPressEvent(event)

        self.focus = (max(0.6, min(ROOM[0] - 0.6, self.focus[0])),
                      max(0.4, min(ROOM[1] - 0.4, self.focus[1])),
                      max(0.6, min(ROOM[2] - 0.6, self.focus[2])))
        self.update()


def main() -> int:
    # Exactly what stage/gpu/layer.py's configure_surface_format() does, and for
    # the reasons documented there: force desktop GL or a Wayland/EGL session
    # hands out GL ES, and do NOT ask for a version or EGL refuses outright.
    fmt = QSurfaceFormat()
    fmt.setAlphaBufferSize(8)
    fmt.setDepthBufferSize(0)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication(sys.argv)
    rig = Rig()
    print(f"stage3d: {rig.par.key} -> {rig.par.kind.value}, "
          f"{rig.head.key} ({rig.head.fdef.mode}) -> {rig.head.kind.value}")

    view = Stage3DView(rig)
    view.setWindowTitle("TupperDMX — 3D stage draft")
    view.resize(1180, 720)
    view.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
