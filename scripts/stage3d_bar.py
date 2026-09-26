#!/usr/bin/env python3
# ABOUTME: Second 3D-stage prototype — the bar in assets/images/stage_background.png
# ABOUTME: rebuilt as a room you can walk around, lit by real DMX-driven fixtures.
"""The 2D stage's backdrop photograph, rebuilt as a place.

Run it:

    uv run python scripts/stage3d_bar.py

The next iteration of `scripts/stage3d_draft.py`. That draft proved the pipeline
in an empty white box; this one asks whether the same pipeline can stand in for
the picture the 2D stage is painted over — `assets/images/stage_background.png`,
a small bar venue: brick side walls, a black drape, a raised plank deck, a
backline, rows of bistro chairs and a lighting pipe — rebuilt in metres, so a
camera can walk round it and every light in it hits something.

Still standalone, and for the same reason: it owns its window and its GL context
and touches nothing in `ui/` or `stage/controller.py`. What it borrows is what
the draft borrowed —

  * `core.fixture_library.resolve_fixture` for the **real** `par-rgbw` and
    `moving-head` definitions;
  * `core.fixture_state.resolve_state` to turn **real** DMX frames into colour,
    intensity, pan, tilt and zoom;
  * `stage.optics.beam_kind` / `resting_half_angle` to decide who throws a shaft
    and how wide, and `stage.registry.renderer_for` for the category fallback;
  * `stage.geometry.Slew` for the heads' inertia;
  * `stage.light.linear_rgb` and the app's ACES fit, so "brighter than white"
    means what it means everywhere else.

What is new, against the draft:

  * **a furnished room.** The architecture is still analytic (the room box and
    the deck are ray/box intersections), but everything standing in it — the
    drape, amps, guitar, drum kit, mic, chairs, pipes, fixtures and the bar at
    the back — is one signed-distance field, grouped behind bounding boxes so a
    ray only pays for what it passes near.
  * **materials.** Procedural brick with mortar relief, planks with grain and
    gaps, velour with sheen, tolex, grille cloth, chrome and cymbal bronze, all
    through one GGX/Lambert BRDF.
  * **light is occluded.** Soft shadows and ambient occlusion come from the same
    distance field that draws the props, so the drum kit shades the deck and a
    chair shades the floor under it with no extra geometry.
  * **one bounce.** Each spot's pool re-emits a little of what it received,
    tinted by the surface it landed on (`bounce_lights`). It is why the amps
    are not black on the side facing a lit deck.
  * **the whole rig the photo implies.** The five overhead cans, the two wall
    grazers and the two small backlights are where the photo has them; a
    front-of-house pair over the audience, out of its frame, is where its
    frontal light on the backline has to come from. Around them, house lights
    that no show addresses: the bar's pendants and back-bar strip, and two
    dimmed downlights over the seating.
  * **progressive accumulation.** Each frame is rendered with a sub-pixel
    jitter and averaged into a float buffer while nothing changes, so a still
    view converges to an anti-aliased, noise-free image and then stops costing
    the GPU anything; any change starts it again. This is the "drop temporal
    reuse while the camera moves" answer `docs/3d-stage-draft.md` asks for.

The venue follows phase 2b's axes (`docs/3d-stage-phase-2b.md`): metres, +X
audience right, +Y up, +Z toward the audience. The origin is the centre of the
deck's front edge at deck height, so the deck is y = 0 and the audience floor
sits a riser's height below it. The photograph is not a scale drawing (it was
never physically consistent — its backline is a size too big for its room), so
the dimensions are a real small venue, arranged to frame like the photo from
`PHOTO_VIEW`.

What it deliberately is NOT: gobos, prisms, strobe, per-cell emitters, shadows
inside the haze, or anything read from a show. The house look is fixed in
`Rig`; the only choreography is a slow sweep of the two heads.

Controls
    drag            orbit the camera
    wheel           dolly in and out
    W/S/A/D         walk the focus point around the room
    Q / E           lower / raise the focus point
    [ / ]           less / more haze
    - / =           exposure
    space           start / stop the heads' sweep
    R               back to the photograph's viewpoint
    Esc             quit
"""

import dataclasses
import math
import os
import struct
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
# The venue, in metres. Everything the shader also needs is emitted into it as
# constants by `scene_constants()`, so there is one place each number lives.
# --------------------------------------------------------------------------
HALF_WIDTH = 3.0            # brick wall to brick wall is 6 m
DECK_HEIGHT = 0.6           # the riser: the audience floor is at -0.6
CEILING_Y = 3.4             # above the deck; 4 m above the audience floor
BACK_WALL_Z = -3.4          # the black wall behind the drape
CURTAIN_Z = -3.2            # the drape's mean line; the deck runs back to it
CURTAIN_TOP = 2.95
REAR_WALL_Z = 8.5           # behind the audience, where the bar is

FRONT_PIPE = (3.25, -1.3)   # (y, z) of the overhead pipe the five cans hang on
BACK_PIPE = (3.02, -2.85)   # the pipe along the drape's border, for the heads
FOH_PIPE = (3.25, 2.3)      # front of house, over the first row: out of the photo's frame

CHAIR_ROWS, CHAIR_COLS = 4, 9
CHAIR_Z0, CHAIR_DZ, CHAIR_DX = 2.1, 0.8, 0.62

BAR_X, BAR_Z = 0.2, 7.55    # the counter's centre line
PENDANT_Y, PENDANT_DX = 1.45, 1.4
HOUSE_X, HOUSE_Z = 1.5, 3.4 # the two recessed downlights over the seating

# Where the photograph was taken from: its horizon, its vanishing point and the
# size of the room in it, solved for a camera 1 m above the deck and ~5.5 m in
# front of it with a 60 degree horizontal field (42 vertical at 3:2).
PHOTO_VIEW = dict(focus=(0.0, 1.3, -0.4), yaw=0.0, pitch=-0.05, radius=5.95)
FOV_VERTICAL_DEG = 42.0

# How much light each source makes, in the arbitrary units the exposure control
# exists to absorb. Tuned by eye against the photograph at exposure 1.0.
PAR_OUTPUT = 28.0
HEAD_OUTPUT = 40.0
PENDANT_OUTPUT = 5.5
BACKBAR_OUTPUT = 1.6
HOUSE_OUTPUT = 4.0

# What fraction of the light landing in a pool comes back off it. The surfaces'
# own albedo does most of the work; this only says how much of the pool a single
# virtual light at its centre may stand in for.
BOUNCE_GAIN = 0.8

MAX_LIGHTS = 18             # mirrored in the shader (as are the three below)
MAX_BOUNCES = 12            # only the patched fixtures' pools bounce
MAX_CANS = 9
MAX_HEADS = 2

# How many jittered frames a still view is averaged over before the scene pass
# stops running. Anti-aliasing is clean by ~16; the haze's dither by ~64.
MAX_SAMPLES = 96

# See `stage3d_draft.SLEW_TAU`: an 8-bit pan channel steps 2.1 degrees at a
# time, and a head with no mass would teleport its beam across the deck.
SLEW_TAU = 0.14


# ==========================================================================
# Small vector helpers — plain tuples, as in the draft: nothing here is hot.
# ==========================================================================

def vadd(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def vsub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vscale(a: Vec3, k: float) -> Vec3:
    return (a[0] * k, a[1] * k, a[2] * k)


def vmul(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] * b[0], a[1] * b[1], a[2] * b[2])


def vdot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def vcross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def vnorm(a: Vec3) -> Vec3:
    length = math.sqrt(vdot(a, a))
    return a if length < 1e-9 else vscale(a, 1.0 / length)


def rot_y(v: Vec3, radians: float) -> Vec3:
    """Rotate about the world's up axis — which is what a pan motor does."""
    cos, sin = math.cos(radians), math.sin(radians)
    return (v[0] * cos + v[2] * sin, v[1], -v[0] * sin + v[2] * cos)


def basis_from_dir(forward: Vec3, right_hint: Optional[Vec3] = None):
    """An orthonormal basis whose local +Z is `forward` (see the draft)."""
    z_axis = vnorm(forward)
    if right_hint is not None:
        x_axis = vnorm(right_hint)
    else:
        up = (0.0, 1.0, 0.0) if abs(z_axis[1]) < 0.95 else (0.0, 0.0, 1.0)
        x_axis = vnorm(vcross(up, z_axis))
    y_axis = vnorm(vcross(z_axis, x_axis))
    return x_axis, y_axis, z_axis


def tilt_axis(aim: Vec3) -> Vec3:
    """The horizontal axis a yoke turns about to point a lamp along `aim`.

    A can hung from a pipe tilts about a level bolt, whichever way it points, so
    its local X has to be horizontal — which an arbitrary up-vector basis does
    not promise once the aim is nearly vertical.
    """
    flat = (aim[0], 0.0, aim[2])
    if vdot(flat, flat) < 1e-6:
        return (1.0, 0.0, 0.0)
    return vnorm(vcross((0.0, 1.0, 0.0), flat))


def halton(index: int, base: int) -> float:
    """The low-discrepancy sequence the sub-pixel jitter walks."""
    result, fraction = 0.0, 1.0
    while index > 0:
        fraction /= base
        result += fraction * (index % base)
        index //= base
    return result


# ==========================================================================
# What the renderer is handed each frame.
# ==========================================================================

@dataclass(frozen=True)
class Emitter:
    """One light, as the shader wants it."""
    position: Vec3
    direction: Vec3
    rgb: Vec3                # linear, already scaled by the source's output
    cos_outer: float
    cos_inner: float
    radius: float            # the lens, in metres: sets shadow softness
    air: float               # how much of a shaft this makes, 0..1


@dataclass(frozen=True)
class Bounce:
    """A pool of light, standing in as a dim light of its own."""
    position: Vec3
    normal: Vec3
    rgb: Vec3


@dataclass(frozen=True)
class CanBody:
    """A static fixture on a yoke: its frame, its size and what it hangs from."""
    position: Vec3
    x: Vec3
    y: Vec3
    z: Vec3
    size: Vec3               # half extents; local Z is the aim
    mount: Vec3              # the clamp
    anchor: Vec3             # where the clamp's arm meets a wall (== mount on a pipe)
    light: int               # index into the frame's emitters, -1 while dark


@dataclass(frozen=True)
class HeadBody:
    """A moving head: the yoke turns with pan, the head with pan and tilt."""
    position: Vec3
    x: Vec3
    y: Vec3
    z: Vec3
    size: Vec3
    yoke_x: Vec3
    yoke_z: Vec3
    mount: Vec3
    light: int


@dataclass
class Frame:
    camera: Tuple[Vec3, Vec3, Vec3, Vec3]      # position, right, up, forward
    tan_half_fov: float
    emitters: List[Emitter]
    bounces: List[Bounce]
    cans: List[CanBody]
    heads: List[HeadBody]
    bulb_rgb: Vec3
    haze: float
    time: float
    exposure: float

    def signature(self) -> tuple:
        """Everything that changes the picture before the tonemap.

        Rounded, because a `Slew` approaches its target geometrically and never
        quite arrives: compared exactly, a head that has visibly stopped would
        keep restarting the accumulation forever. Exposure is left out on
        purpose — it is applied after the average, so changing it costs nothing.
        """
        return _quantise((self.camera, self.tan_half_fov, self.emitters,
                          self.bounces, self.cans, self.heads, self.bulb_rgb,
                          self.haze, self.time))


def _quantise(value):
    if isinstance(value, float):
        return round(value, 4)
    if dataclasses.is_dataclass(value):
        return _quantise(dataclasses.astuple(value))
    if isinstance(value, (tuple, list)):
        return tuple(_quantise(item) for item in value)
    return value


# ==========================================================================
# The rig
# ==========================================================================

def slot_index(fdef, cap_type: str, color: Optional[str] = None) -> int:
    """Which DMX slot carries `cap_type` — by capability, never by name."""
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
class Fixture:
    """A patched fixture: its definition, where it hangs, and its live state."""
    key: str
    mode: str
    position: Vec3               # the body's centre (a head's: its tilt pivot)
    mount: Vec3                  # the clamp on the pipe or wall arm
    anchor: Optional[Vec3] = None
    fdef: object = None
    state: Optional[FixtureState] = None
    aim: Vec3 = (0.0, -1.0, 0.0)
    kind: BeamKind = BeamKind.NONE
    slots: dict = field(default_factory=dict)

    def resolve(self) -> None:
        self.fdef = resolve_fixture(self.key, self.mode)
        if self.fdef is None:
            raise SystemExit(f"fixture '{self.key}' is not in the library")
        fallback = getattr(renderer_for(self.fdef), "beam_fallback", BeamKind.NONE)
        self.kind = beam_kind(self.fdef, fallback)
        self.slots = {
            "intensity": slot_index(self.fdef, "Intensity"),
            "red": slot_index(self.fdef, "ColorIntensity", "Red"),
            "green": slot_index(self.fdef, "ColorIntensity", "Green"),
            "blue": slot_index(self.fdef, "ColorIntensity", "Blue"),
            "shutter": slot_index(self.fdef, "ShutterStrobe"),
            "pan": slot_index(self.fdef, "Pan"),
            "tilt": slot_index(self.fdef, "Tilt"),
            "zoom": slot_index(self.fdef, "Zoom"),
        }

    def drive(self, dmx: bytes) -> None:
        self.state = resolve_state(self.fdef, dmx)

    def half_angle_deg(self) -> float:
        """The live cone half-angle.

        A zoom channel wins, as in the draft. A surface light (`beam_kind` said
        NONE) throws no shaft, but in a room it still lights what is in front of
        it, and it does so through its lens: asking `resting_half_angle` what it
        *would* be as a wash reads that lens the way the app reads any other.
        The draft's 88 degrees stood in for "everything in front", which in a
        furnished room floods the drape as brightly as the deck.
        """
        zoom = self.state.optics.zoom_deg if self.state else None
        if zoom is not None:
            return max(1.0, zoom) / 2.0
        half = resting_half_angle(self.fdef, self.kind)
        if half is None:
            half = resting_half_angle(self.fdef, BeamKind.WASH) or 60.0
        return min(half, 85.0)

    def body_half_extents(self) -> Vec3:
        """The chassis at the definition's own size; local Z is the aim."""
        dims = self.fdef.physical.dimensions_mm or (180.0, 200.0, 180.0)
        width, height, depth = (value / 1000.0 for value in dims)
        return (width / 2.0, depth / 2.0, height / 2.0)

    def lens_radius(self) -> float:
        size = self.body_half_extents()
        return min(size[0], size[1]) * 0.84

    def lens_position(self) -> Vec3:
        return vadd(self.position, vscale(self.aim, self.body_half_extents()[2] + 0.04))

    def emitter(self, output: float) -> Optional[Emitter]:
        state = self.state
        if state is None or state.strobe.effect == "Closed":
            return None
        rgb255 = state.scaled_rgb()
        if max(rgb255) <= 0:
            return None
        half = self.half_angle_deg()
        # A hard edge keeps its shape; a wash dissolves into one.
        inner_scale = 0.8 if self.kind is BeamKind.HARD else 0.3
        air = {BeamKind.HARD: 1.0, BeamKind.WASH: 0.55}.get(self.kind, 0.0)
        return Emitter(
            position=self.lens_position(),
            direction=self.aim,
            rgb=vscale(linear_rgb(rgb255), output),
            cos_outer=math.cos(math.radians(half)),
            cos_inner=math.cos(math.radians(half * inner_scale)),
            radius=self.lens_radius(),
            air=air,
        )


def angle_table(fdef, slot: int, axis: str) -> List[float]:
    """What every byte on one motion channel means, according to the app.

    Built by asking `resolve_state` rather than by reading a range off the
    definition, so a fixture whose pan is not linear, or not 540 degrees, is
    still aimed where the geometry says.
    """
    table = []
    for value in range(256):
        frame = bytearray(fdef.channel_count)
        frame[slot] = value
        motion = resolve_state(fdef, bytes(frame)).motion
        angle = motion.pan_deg if axis == "pan" else motion.tilt_deg
        table.append(angle if angle is not None else 0.0)
    return table


def nearest_byte(table: List[float], degrees: float) -> int:
    return min(range(256), key=lambda value: abs(table[value] - degrees))


class Rig:
    """The photograph's lighting, as real fixtures, plus the bar's house lights."""

    FRONT_XS = (-1.8, -0.9, 0.0, 0.9, 1.8)
    HEAD_XS = (-1.6, 1.6)

    def __init__(self):
        pipe_y, pipe_z = FRONT_PIPE
        # The five cans across the top of the photograph, hung on the overhead
        # pipe and pointed nearly straight down, so their pools land between
        # the backline and the lip — where the photo's deck is brightest.
        self.front = [
            Fixture("par-rgbw", "6ch", position=(x, pipe_y - 0.3, pipe_z),
                    mount=(x, pipe_y, pipe_z))
            for x in self.FRONT_XS
        ]
        # The two lamps high on the side walls, grazing the brick: the scallops
        # of light down both walls in the photo. Half a metre out on arms, so
        # the cone opens across the wall instead of licking up it.
        self.grazers = []
        for side in (-1.0, 1.0):
            x = side * (HALF_WIDTH - 0.7)
            self.grazers.append(Fixture(
                "par-rgbw", "6ch", position=(x, 3.02, -2.0),
                mount=(x, 3.22, -2.0), anchor=(side * HALF_WIDTH, 3.22, -2.0)))
        # The photograph lights its backline from the front, but every lamp it
        # shows is overhead and facing the camera. In a real room that light
        # comes from a front-of-house pipe over the audience, just out of the
        # frame — so there is one, and turning round shows it.
        foh_y, foh_z = FOH_PIPE
        self.foh = [
            Fixture("par-rgbw", "6ch", position=(x, foh_y - 0.3, foh_z),
                    mount=(x, foh_y, foh_z))
            for x in (-1.4, 1.4)
        ]
        # The two small lamps low on the drape's border are the backlight. Heads
        # rather than pars: a hard beam is what shows up in a bar's haze, and
        # they give the rig its one moving part.
        back_y, back_z = BACK_PIPE
        self.heads = [
            Fixture("moving-head", "12ch", position=(x, back_y - 0.4, back_z),
                    mount=(x, back_y, back_z))
            for x in self.HEAD_XS
        ]
        for fixture in self.cans() + self.heads:
            fixture.resolve()

        for can in self.front:
            target = (can.position[0] * 0.8, 0.0, -0.9)
            can.aim = vnorm(vsub(target, can.position))
        for can in self.grazers:
            side = 1.0 if can.position[0] > 0 else -1.0
            target = (side * HALF_WIDTH, 0.5, -1.4)
            can.aim = vnorm(vsub(target, can.position))
        for can in self.foh:
            target = (can.position[0] * 0.3, 0.7, -2.0)
            can.aim = vnorm(vsub(target, can.position))

        # Where each head is asked to point in the house look: crossed onto the
        # front of the deck, so the two shafts meet over the mic stand.
        self.head_targets = [(0.55, 0.0, -0.55), (-0.55, 0.0, -0.55)]
        head = self.heads[0]
        self.pan_table = angle_table(head.fdef, head.slots["pan"], "pan")
        self.tilt_table = angle_table(head.fdef, head.slots["tilt"], "tilt")
        # DMX 127/128 is home — straight down for a hung head (see CLAUDE.md,
        # "The render engine") — so that is what an angle is measured from.
        self.pan_home = self.pan_table[128]
        self.tilt_home = self.tilt_table[128]
        self.slews = [(Slew(), Slew()) for _ in self.heads]
        self.head_pan_rad = [0.0 for _ in self.heads]
        self.sweeping = False

        self.pendants = [(BAR_X + dx, PENDANT_Y - 0.06, BAR_Z)
                         for dx in (-PENDANT_DX, 0.0, PENDANT_DX)]

    def cans(self) -> List[Fixture]:
        return self.front + self.grazers + self.foh

    # -- the house look ------------------------------------------------
    # Tungsten-warm white throughout, as in the photograph: roughly 3000 K
    # from the pars, a touch cooler from the heads' discharge-white.
    def tick(self, t: float) -> None:
        for can in self.front:
            can.drive(self._par_dmx(can, intensity=205, rgb=(255, 180, 110)))
        for can in self.grazers:
            can.drive(self._par_dmx(can, intensity=225, rgb=(255, 172, 98)))
        for can in self.foh:
            can.drive(self._par_dmx(can, intensity=255, rgb=(255, 185, 118)))
        for index, head in enumerate(self.heads):
            head.drive(self._head_dmx(index, t))
            self._aim_head(index)

    def _par_dmx(self, can: Fixture, intensity: int, rgb) -> bytes:
        frame = bytearray(can.fdef.channel_count)
        # Warm white from the RGB emitters alone: this par folds its white
        # channel in additively and clamps, so any W at all reads as pure white.
        self._put(frame, can.slots, intensity=intensity,
                  red=rgb[0], green=rgb[1], blue=rgb[2])
        return bytes(frame)

    def _head_dmx(self, index: int, t: float) -> bytes:
        head = self.heads[index]
        pan, tilt = self._pan_tilt_towards(head.position, self.head_targets[index])
        if self.sweeping:
            phase = index * math.pi
            pan += 24.0 * math.sin(t * 0.35 + phase)
            tilt += 9.0 * math.sin(t * 0.23 + phase + 0.8)
        frame = bytearray(head.fdef.channel_count)
        self._put(frame, head.slots,
                  pan=nearest_byte(self.pan_table, pan),
                  tilt=nearest_byte(self.tilt_table, tilt),
                  intensity=190,
                  # 240-255 is Open on this fixture; 255 is NOT "on" in general.
                  shutter=250,
                  red=255, green=205, blue=160,
                  zoom=70)
        return bytes(frame)

    def _pan_tilt_towards(self, origin: Vec3, target: Vec3) -> Tuple[float, float]:
        """The inverse of `_aim_head`: which angles point `origin` at `target`."""
        d = vnorm(vsub(target, origin))
        tilt = math.degrees(math.acos(max(-1.0, min(1.0, -d[1]))))
        pan = math.degrees(math.atan2(d[0], d[2]))
        return self.pan_home + pan, self.tilt_home + tilt

    @staticmethod
    def _put(frame: bytearray, slots: dict, **values) -> None:
        for name, value in values.items():
            index = slots.get(name, -1)
            if 0 <= index < len(frame):
                frame[index] = max(0, min(255, int(value)))

    def _aim_head(self, index: int) -> None:
        """Pan about world up, tilt about the yoke; home is straight down."""
        head = self.heads[index]
        motion = head.state.motion
        pan_deg = motion.pan_deg if motion.pan_deg is not None else self.pan_home
        tilt_deg = motion.tilt_deg if motion.tilt_deg is not None else self.tilt_home
        now = time.monotonic()
        pan_slew, tilt_slew = self.slews[index]
        pan = math.radians(pan_slew.advance(pan_deg, now, SLEW_TAU) - self.pan_home)
        tilt = math.radians(tilt_slew.advance(tilt_deg, now, SLEW_TAU) - self.tilt_home)
        self.head_pan_rad[index] = pan
        head.aim = rot_y((0.0, -math.cos(tilt), math.sin(tilt)), pan)

    # -- what the renderer needs ---------------------------------------
    def build(self) -> Tuple[List[Emitter], List[CanBody], List[HeadBody], Vec3]:
        emitters: List[Emitter] = []

        def light_of(fixture: Fixture, output: float) -> int:
            emitter = fixture.emitter(output)
            if emitter is None:
                return -1
            emitters.append(emitter)
            return len(emitters) - 1

        cans = []
        for can in self.cans():
            light = light_of(can, PAR_OUTPUT)
            x_axis, y_axis, z_axis = basis_from_dir(can.aim, tilt_axis(can.aim))
            cans.append(CanBody(can.position, x_axis, y_axis, z_axis,
                                can.body_half_extents(), can.mount,
                                can.anchor or can.mount, light))

        heads = []
        for index, head in enumerate(self.heads):
            light = light_of(head, HEAD_OUTPUT)
            pan = self.head_pan_rad[index]
            yoke_x = rot_y((1.0, 0.0, 0.0), pan)
            yoke_z = rot_y((0.0, 0.0, 1.0), pan)
            x_axis, y_axis, z_axis = basis_from_dir(head.aim, yoke_x)
            heads.append(HeadBody(head.position, x_axis, y_axis, z_axis,
                                  head.body_half_extents(), yoke_x, yoke_z,
                                  head.mount, light))

        # The house lights are not patched fixtures: nothing in a show
        # addresses them, so they are written down as what they are — the
        # bar's pendants, the strip under the back bar's pelmet, and two
        # downlights over the seating, dimmed for the show as a venue would.
        warm = linear_rgb((255, 158, 82))
        down = (0.0, -1.0, 0.0)

        def house(position, direction, output, half, inner, radius):
            emitters.append(Emitter(position, direction, vscale(warm, output),
                                    math.cos(math.radians(half)),
                                    math.cos(math.radians(inner)), radius, 0.0))

        for position in self.pendants:
            house(position, down, PENDANT_OUTPUT, 62.0, 20.0, 0.04)
        for dx in (-1.0, 1.0):
            house((BAR_X + dx, -DECK_HEIGHT + 2.2, REAR_WALL_Z - 0.17),
                  vnorm((0.0, -1.0, 0.12)), BACKBAR_OUTPUT, 75.0, 30.0, 0.5)
        for side in (-1.0, 1.0):
            house((side * HOUSE_X, CEILING_Y - 0.08, HOUSE_Z), down, HOUSE_OUTPUT,
                  55.0, 15.0, 0.05)
        return emitters[:MAX_LIGHTS], cans, heads, warm


# ==========================================================================
# One bounce of indirect light
# ==========================================================================

# Roughly what each architectural surface reflects, linear. The shader's
# procedural textures average out near these; this only has to be close.
ALBEDO_DECK = (0.1, 0.068, 0.05)
ALBEDO_FLOOR = (0.045, 0.035, 0.028)
ALBEDO_BRICK = (0.18, 0.105, 0.07)
ALBEDO_BLACK = (0.014, 0.013, 0.013)


def first_surface(origin: Vec3, direction: Vec3) -> Optional[Tuple[Vec3, Vec3, Vec3]]:
    """Where a ray from `origin` first meets the room's architecture.

    Planes only — the props are ignored — which is fine for what this is used
    for: finding the middle of a pool of light, not whether a pixel is lit.
    """
    best = None
    ox, oy, oz = origin
    dx, dy, dz = direction

    def consider(t, normal, albedo, valid):
        nonlocal best
        if t > 1e-4 and (best is None or t < best[0]):
            point = vadd(origin, vscale(direction, t))
            if valid(point):
                best = (t, point, normal, albedo)

    if abs(dy) > 1e-6:
        consider(-oy / dy, (0.0, 1.0, 0.0), ALBEDO_DECK, lambda p: p[2] <= 0.0)
        consider((-DECK_HEIGHT - oy) / dy, (0.0, 1.0, 0.0), ALBEDO_FLOOR,
                 lambda p: p[2] >= 0.0)
        consider((CEILING_Y - oy) / dy, (0.0, -1.0, 0.0), ALBEDO_BLACK, lambda p: True)
    if abs(dx) > 1e-6:
        consider((-HALF_WIDTH - ox) / dx, (1.0, 0.0, 0.0), ALBEDO_BRICK, lambda p: True)
        consider((HALF_WIDTH - ox) / dx, (-1.0, 0.0, 0.0), ALBEDO_BRICK, lambda p: True)
    if abs(dz) > 1e-6:
        consider((CURTAIN_Z - oz) / dz, (0.0, 0.0, 1.0), ALBEDO_BLACK, lambda p: True)
        consider((REAR_WALL_Z - oz) / dz, (0.0, 0.0, -1.0), ALBEDO_BRICK, lambda p: True)
        consider(-oz / dz, (0.0, 0.0, 1.0), ALBEDO_BLACK, lambda p: p[1] <= 0.0)
    if best is None:
        return None
    return best[1], best[2], best[3]


def bounce_lights(emitters: Sequence[Emitter]) -> List[Bounce]:
    """Each light's pool, re-emitted as a dim Lambertian light of its own.

    A cone of half-angle θ carries 2π(1 − cos θ) of its intensity as flux; the
    surface keeps its albedo's share of that and sends it back over a
    hemisphere, which as a point source is flux / π along its normal.
    """
    bounces = []
    for emitter in emitters[:MAX_BOUNCES]:
        hit = first_surface(emitter.position, emitter.direction)
        if hit is None:
            continue
        point, normal, albedo = hit
        flux = 2.0 * (1.0 - emitter.cos_outer) * BOUNCE_GAIN
        rgb = vscale(vmul(emitter.rgb, albedo), flux)
        # Stood a little off the surface: it stands for a whole pool, not a
        # point on it, and a light on the surface would be infinitely bright.
        bounces.append(Bounce(vadd(point, vscale(normal, 0.35)), normal, rgb))
    return bounces[:MAX_BOUNCES]


# ==========================================================================
# The camera
# ==========================================================================

class OrbitCamera:
    """An orbit round a focus point, kept inside the room and above its floors."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.focus: Vec3 = PHOTO_VIEW["focus"]
        self.yaw = PHOTO_VIEW["yaw"]
        self.pitch = PHOTO_VIEW["pitch"]
        self.radius = PHOTO_VIEW["radius"]

    @staticmethod
    def _inside(point: Vec3, margin: float) -> Vec3:
        x = max(-HALF_WIDTH + margin, min(HALF_WIDTH - margin, point[0]))
        z = max(CURTAIN_Z + 0.2 + margin, min(REAR_WALL_Z - margin, point[2]))
        # Two floors: over the deck the floor is 0.6 m higher.
        floor = 0.0 if z < 0.1 else -DECK_HEIGHT
        y = max(floor + margin, min(CEILING_Y - margin, point[1]))
        return (x, y, z)

    def position(self) -> Vec3:
        horizontal = math.cos(self.pitch) * self.radius
        offset = (math.sin(self.yaw) * horizontal,
                  math.sin(self.pitch) * self.radius,
                  math.cos(self.yaw) * horizontal)
        return self._inside(vadd(self.focus, offset), 0.3)

    def basis(self):
        position = self.position()
        forward = vnorm(vsub(self.focus, position))
        right = vnorm(vcross(forward, (0.0, 1.0, 0.0)))
        up = vnorm(vcross(right, forward))
        return position, right, up, forward

    def walk(self, forward_step: float, side_step: float, up_step: float) -> None:
        forward = vnorm(vsub(self.focus, self.position()))
        flat = vnorm((forward[0], 0.0, forward[2]))
        side = vnorm(vcross(flat, (0.0, 1.0, 0.0)))
        moved = vadd(self.focus, vscale(flat, forward_step))
        moved = vadd(moved, vscale(side, side_step))
        moved = vadd(moved, (0.0, up_step, 0.0))
        self.focus = self._inside(moved, 0.4)


# ==========================================================================
# Shaders
# ==========================================================================

VERTEX = """
#version 330
in vec2 a_corner;
void main() {
    gl_Position = vec4(a_corner, 0.0, 1.0);
}
"""

SCENE_FRAGMENT = """
#version 330

out vec4 fragColor;

//@CONSTANTS@

const float PI = 3.14159265359;

// The three loops worth turning down on a slow GPU.
const int TRACE_STEPS = 200;
const int SHADOW_STEPS = 48;
const int MARCH_STEPS = 40;

const float AMBIENT = 0.012;        // the room is not in outer space
const float LENS_GAIN = 6.0;        // a lens seen head-on, relative to its beam
const float AIR_GAIN = 0.55;
const float BEAM_REACH = 14.0;      // bounds the shaft solve; the room is 12 m long

uniform vec2  u_resolution;
uniform vec2  u_jitter;             // sub-pixel offset for this sample
uniform float u_seed;               // which sample this is: decorrelates the dither
uniform vec3  u_cam_pos;
uniform vec3  u_cam_right;
uniform vec3  u_cam_up;
uniform vec3  u_cam_fwd;
uniform float u_tan_half_fov;
uniform float u_haze;
uniform float u_time;

uniform int   u_light_count;
uniform vec3  u_light_pos[MAX_LIGHTS];
uniform vec3  u_light_dir[MAX_LIGHTS];
uniform vec3  u_light_rgb[MAX_LIGHTS];
uniform vec4  u_light_shape[MAX_LIGHTS];   // cos_outer, cos_inner, lens radius, air

uniform int   u_bounce_count;
uniform vec3  u_bounce_pos[MAX_BOUNCES];
uniform vec3  u_bounce_nrm[MAX_BOUNCES];
uniform vec3  u_bounce_rgb[MAX_BOUNCES];

uniform int   u_can_count;
uniform vec3  u_can_pos[MAX_CANS];
uniform vec3  u_can_x[MAX_CANS];
uniform vec3  u_can_y[MAX_CANS];
uniform vec3  u_can_z[MAX_CANS];
uniform vec3  u_can_size[MAX_CANS];
uniform vec3  u_can_mount[MAX_CANS];
uniform vec3  u_can_anchor[MAX_CANS];
uniform int   u_can_light[MAX_CANS];

uniform int   u_head_count;
uniform vec3  u_head_pos[MAX_HEADS];
uniform vec3  u_head_x[MAX_HEADS];
uniform vec3  u_head_y[MAX_HEADS];
uniform vec3  u_head_z[MAX_HEADS];
uniform vec3  u_head_size[MAX_HEADS];
uniform vec3  u_yoke_x[MAX_HEADS];
uniform vec3  u_yoke_z[MAX_HEADS];
uniform vec3  u_head_mount[MAX_HEADS];
uniform int   u_head_light[MAX_HEADS];

uniform vec3  u_bulb_rgb;           // the house lights' colour, for their bulbs

// ------------------------------------------------------------ materials ---
// Props carry an id through the distance field; the architecture's ids are
// the faces the analytic intersection found.
const int M_CURTAIN = 1;
const int M_TOLEX = 2;
const int M_GRILLE = 3;
const int M_GRILLE_LIGHT = 4;
const int M_PANEL = 5;
const int M_KNOB = 6;
const int M_JEWEL = 7;
const int M_CHROME = 8;
const int M_CYMBAL = 9;
const int M_DRUMHEAD = 10;
const int M_SHELL = 11;
const int M_FLOORTOM = 12;
const int M_HOOP = 13;
const int M_GUITAR = 14;
const int M_NECK = 15;
const int M_BODY = 16;
const int M_PIPE = 17;
const int M_CHAIR = 18;
const int M_COUNTER = 19;
const int M_COUNTER_TOP = 20;
const int M_BRASS = 21;
const int M_BOTTLE = 22;
const int M_MIC = 23;
const int M_CONDUIT = 24;
const int M_BLACK = 25;
const int M_BULB = 26;
const int M_LENS_DARK = 27;
const int M_LENS = 32;              // + the light's index

const int A_WALL_L = 64;
const int A_WALL_R = 65;
const int A_FLOOR = 66;
const int A_CEILING = 67;
const int A_BACK = 68;
const int A_REAR = 69;
const int A_DECK = 70;
const int A_FASCIA = 71;

// ---------------------------------------------------------------- noise ---
float hash12(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

float hash13(vec3 p3) {
    p3 = fract(p3 * 0.1031);
    p3 += dot(p3, p3.zyx + 31.32);
    return fract((p3.x + p3.y) * p3.z);
}

float vnoise2(vec2 x) {
    vec2 i = floor(x);
    vec2 f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash12(i), hash12(i + vec2(1.0, 0.0)), f.x),
               mix(hash12(i + vec2(0.0, 1.0)), hash12(i + vec2(1.0, 1.0)), f.x), f.y);
}

float vnoise3(vec3 x) {
    vec3 i = floor(x);
    vec3 f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(mix(hash13(i + vec3(0, 0, 0)), hash13(i + vec3(1, 0, 0)), f.x),
                   mix(hash13(i + vec3(0, 1, 0)), hash13(i + vec3(1, 1, 0)), f.x), f.y),
               mix(mix(hash13(i + vec3(0, 0, 1)), hash13(i + vec3(1, 0, 1)), f.x),
                   mix(hash13(i + vec3(0, 1, 1)), hash13(i + vec3(1, 1, 1)), f.x), f.y),
               f.z);
}

float fbm2(vec2 p) {
    float value = 0.0;
    float amplitude = 0.5;
    for (int i = 0; i < 4; ++i) {
        value += amplitude * vnoise2(p);
        p = p * 2.03 + 17.1;
        amplitude *= 0.5;
    }
    return value;
}

// ----------------------------------------------------------- primitives ---
float sd_box(vec3 p, vec3 b) {
    vec3 q = abs(p) - b;
    return length(max(q, 0.0)) + min(max(q.x, max(q.y, q.z)), 0.0);
}

float sd_rbox(vec3 p, vec3 b, float r) {
    return sd_box(p, b - r) - r;
}

// A cylinder along its local Y.
float sd_cyl(vec3 p, float r, float h) {
    vec2 d = abs(vec2(length(p.xz), p.y)) - vec2(r, h);
    return min(max(d.x, d.y), 0.0) + length(max(d, 0.0));
}

float sd_cyl_z(vec3 p, float r, float h) { return sd_cyl(p.xzy, r, h); }
float sd_cyl_x(vec3 p, float r, float h) { return sd_cyl(p.yxz, r, h); }

float sd_capsule(vec3 p, vec3 a, vec3 b, float r) {
    vec3 pa = p - a;
    vec3 ba = b - a;
    float h = clamp(dot(pa, ba) / max(dot(ba, ba), 1e-8), 0.0, 1.0);
    return length(pa - ba * h) - r;
}

// A ring lying in the XZ plane.
float sd_torus(vec3 p, float big, float small) {
    return length(vec2(length(p.xz) - big, p.y)) - small;
}

float smin(float a, float b, float k) {
    float h = clamp(0.5 + 0.5 * (b - a) / k, 0.0, 1.0);
    return mix(b, a, h) - k * h * (1.0 - h);
}

mat2 rot2(float a) {
    float c = cos(a);
    float s = sin(a);
    return mat2(c, s, -s, c);
}

vec2 opU(vec2 a, vec2 b) { return a.x < b.x ? a : b; }

vec3 to_local(vec3 p, vec3 origin, vec3 ax, vec3 ay, vec3 az) {
    vec3 q = p - origin;
    return vec3(dot(q, ax), dot(q, ay), dot(q, az));
}

// Three legs splayed from a hub at `hub` height to feet `spread` out on the
// floor, by polar repetition: one capsule is evaluated, whichever leg is near.
float sd_tripod(vec3 p, float spread, float hub, float r, float phase) {
    const float SECTOR = 2.0943951;
    float a = atan(p.z, p.x) - phase;
    a = mod(a + 0.5 * SECTOR, SECTOR) - 0.5 * SECTOR;
    float radial = length(p.xz);
    vec3 q = vec3(radial * cos(a), p.y, radial * sin(a));
    return sd_capsule(q, vec3(spread, 0.012, 0.0), vec3(0.02, hub, 0.0), r);
}

// ------------------------------------------------------------ the drape ---
// Velour hung at about double fullness. The folds steepen the field, which is
// what the 0.55 pays for: the trace may never step further than the true
// distance, and a fold's slope makes the plane distance an over-estimate.
float curtain_fold(float x) {
    return 0.045 * sin(x * 18.0 + 1.1 * sin(x * 2.3)) + 0.012 * sin(x * 43.0 + 1.7);
}

float sd_drape(vec3 p) {
    float cloth = (p.z - CURTAIN_Z - curtain_fold(p.x)) * 0.55;
    cloth = max(cloth, p.y - CURTAIN_TOP);
    // The border hung in front of the drape's own track, down to below the
    // back pipe's height so the join with the ceiling never shows.
    float hem = CURTAIN_TOP - 0.34 + 0.012 * sin(p.x * 26.0);
    float front = CURTAIN_Z + 0.2 + 0.03 * sin(p.x * 26.0 + 0.7 * sin(p.x * 3.1));
    float border = max((p.z - front) * 0.6, hem - p.y);
    return min(cloth, border);
}

// ------------------------------------------------------------- backline ---
const vec3 CAB_L = vec3(-2.42, 0.38, -2.30);
const vec3 CAB_L_SIZE = vec3(0.38, 0.38, 0.18);
const vec3 AMP = vec3(-2.42, 0.905, -2.34);
const vec3 AMP_SIZE = vec3(0.36, 0.145, 0.13);
const vec3 COMBO = vec3(-1.30, 0.29, -2.72);
const vec3 COMBO_SIZE = vec3(0.31, 0.27, 0.13);
const vec3 CAB_R = vec3(2.35, 0.47, -2.62);
const vec3 CAB_R_SIZE = vec3(0.33, 0.47, 0.22);
const vec3 KICK = vec3(-0.08, 0.30, -2.10);
const vec3 GUITAR = vec3(-1.86, 0.0, -2.30);
const float GUITAR_LEAN = 0.25;
const vec3 MIC = vec3(0.05, 0.0, -0.40);

// A speaker cabinet: tolex box, grille on the front, steel corners.
vec2 sd_cabinet(vec3 p, vec3 centre, vec3 size) {
    vec3 q = p - centre;
    vec2 res = vec2(sd_rbox(q, size, 0.018), float(M_TOLEX));
    float grille = sd_box(q - vec3(0.0, 0.0, size.z),
                          vec3(size.x - 0.05, size.y - 0.05, 0.003));
    res = opU(res, vec2(grille, float(M_GRILLE)));
    // One corner protector, mirrored into all eight by the abs().
    float corners = sd_rbox(abs(q) - size + 0.018, vec3(0.032), 0.01);
    return opU(res, vec2(corners, float(M_CHROME)));
}

vec2 sd_amp_head(vec3 p) {
    vec3 q = p - AMP;
    vec2 res = vec2(sd_rbox(q, AMP_SIZE, 0.015), float(M_TOLEX));
    float face = AMP_SIZE.z;
    res = opU(res, vec2(sd_box(q - vec3(0.0, 0.05, face),
                               vec3(AMP_SIZE.x - 0.03, 0.055, 0.003)), float(M_PANEL)));
    res = opU(res, vec2(sd_box(q - vec3(0.0, -0.065, face),
                               vec3(AMP_SIZE.x - 0.03, 0.05, 0.002)), float(M_GRILLE)));
    vec3 k = q - vec3(-0.03, 0.045, face + 0.012);
    k.x -= clamp(floor(k.x / 0.055 + 0.5), -4.0, 4.0) * 0.055;
    res = opU(res, vec2(sd_cyl_z(k, 0.012, 0.011), float(M_KNOB)));
    res = opU(res, vec2(length(q - vec3(AMP_SIZE.x - 0.045, 0.045, face + 0.004)) - 0.009,
                        float(M_JEWEL)));
    float handle = sd_capsule(q, vec3(-0.12, AMP_SIZE.y + 0.012, 0.0),
                              vec3(0.12, AMP_SIZE.y + 0.012, 0.0), 0.012);
    return opU(res, vec2(handle, float(M_BLACK)));
}

vec2 sd_combo(vec3 p) {
    vec3 q = p - COMBO;
    vec2 res = vec2(sd_rbox(q, COMBO_SIZE, 0.015), float(M_TOLEX));
    float face = COMBO_SIZE.z;
    res = opU(res, vec2(sd_box(q - vec3(0.0, -0.035, face),
                               vec3(COMBO_SIZE.x - 0.035, COMBO_SIZE.y - 0.075, 0.003)),
                        float(M_GRILLE_LIGHT)));
    res = opU(res, vec2(sd_box(q - vec3(0.0, COMBO_SIZE.y - 0.035, face),
                               vec3(COMBO_SIZE.x - 0.03, 0.022, 0.003)), float(M_PANEL)));
    vec3 k = q - vec3(0.0, COMBO_SIZE.y - 0.035, face + 0.01);
    k.x -= clamp(floor(k.x / 0.05 + 0.5), -4.0, 4.0) * 0.05;
    res = opU(res, vec2(sd_cyl_z(k, 0.009, 0.009), float(M_KNOB)));
    float handle = sd_capsule(q, vec3(-0.1, COMBO_SIZE.y + 0.012, 0.0),
                              vec3(0.1, COMBO_SIZE.y + 0.012, 0.0), 0.012);
    return opU(res, vec2(handle, float(M_BLACK)));
}

// The guitar's own frame: upright, face toward +Z, origin at the bottom of the
// body. It leans back into its stand, toward the drape.
vec3 guitar_local(vec3 p) {
    vec3 g = p - GUITAR - vec3(0.0, 0.12, 0.02);
    g.yz = rot2(GUITAR_LEAN) * g.yz;
    return g;
}

vec2 sd_guitar(vec3 p) {
    vec3 q = p - GUITAR;
    vec3 m = vec3(abs(q.x), q.yz);
    float stand = sd_capsule(q, vec3(0.0, 0.02, -0.3), vec3(0.0, 0.8, -0.2), 0.009);
    stand = min(stand, sd_capsule(m, vec3(0.18, 0.012, 0.08), vec3(0.0, 0.05, -0.3), 0.009));
    stand = min(stand, sd_capsule(m, vec3(0.13, 0.13, 0.07), vec3(0.0, 0.1, -0.12), 0.008));
    stand = min(stand, sd_capsule(m, vec3(0.05, 0.84, -0.18), vec3(0.0, 0.8, -0.2), 0.007));
    vec2 res = vec2(stand, float(M_BLACK));

    vec3 g = guitar_local(p);
    // Lower bout a squat ellipse, upper bout a smaller circle, a soft waist.
    // The ellipse's squash over-estimates distance by its ratio; 0.89 undoes it.
    float lower = (length((g.xy - vec2(0.0, 0.19)) * vec2(1.0, 1.12)) - 0.19) * 0.89;
    float upper = length(g.xy - vec2(0.0, 0.43)) - 0.148;
    float outline = smin(lower, upper, 0.06);
    vec2 e = vec2(outline + 0.006, abs(g.z) - 0.02);
    float body = min(max(e.x, e.y), 0.0) + length(max(e, 0.0)) - 0.006;
    res = opU(res, vec2(body, float(M_GUITAR)));
    float neck = sd_rbox(g - vec3(0.0, 0.8, 0.01), vec3(0.024, 0.235, 0.013), 0.008);
    float head = sd_rbox(g - vec3(0.0, 1.105, 0.003), vec3(0.042, 0.085, 0.009), 0.012);
    return opU(res, vec2(min(neck, head), float(M_NECK)));
}

// A drum along its local Y: shell, a head on each end, a hoop round each head.
vec2 drum(vec3 q, float r, float h, int shell, int hoop) {
    vec2 res = vec2(sd_cyl(q, r, h), float(shell));
    vec3 e = vec3(q.x, abs(q.y) - h, q.z);
    res = opU(res, vec2(sd_cyl(e, r - 0.004, 0.0035), float(M_DRUMHEAD)));
    float ring = max(abs(length(q.xz) - r - 0.004) - 0.006, abs(e.y) - 0.013);
    return opU(res, vec2(ring, float(hoop)));
}

// A cymbal: a shallow cone with a bell, 3.6 mm thick.
float sd_cymbal(vec3 c, float radius) {
    float r = length(c.xz);
    float bow = max(r - radius, abs(c.y + 0.03 * r / radius) - 0.0018) * 0.9;
    float bell = max(length(c - vec3(0.0, -0.05, 0.0)) - 0.068, -c.y - 0.01);
    return min(bow, bell);
}

// A stand: tripod on the floor, a tube up the middle. `q` is at the floor.
float sd_stand(vec3 q, float spread, float top, float phase) {
    return min(sd_tripod(q, spread, 0.25, 0.007, phase),
               sd_capsule(q, vec3(0.0, 0.2, 0.0), vec3(0.0, top, 0.0), 0.009));
}

vec2 sd_drum_kit(vec3 p) {
    vec3 q = p - KICK;
    vec3 floor_q = q + vec3(0.0, KICK.y, 0.0);      // the same frame, at deck level

    // Bass drum on its side, resonant head to the audience, on two spurs.
    vec2 res = drum(q.xzy, 0.275, 0.2, M_SHELL, M_HOOP);
    float spurs = sd_capsule(vec3(abs(q.x), q.yz), vec3(0.25, -0.08, 0.1),
                             vec3(0.36, -0.295, 0.2), 0.008);
    res = opU(res, vec2(spurs, float(M_CHROME)));

    // Rack tom on the kick's mount, tipped toward the drummer.
    vec3 t = q - vec3(-0.13, 0.46, -0.1);
    t.yz = rot2(0.45) * t.yz;
    res = opU(res, drum(t, 0.15, 0.1, M_SHELL, M_CHROME));
    res = opU(res, vec2(sd_capsule(q, vec3(-0.05, 0.27, -0.08),
                                   vec3(-0.11, 0.38, -0.1), 0.012), float(M_CHROME)));

    // Floor tom on three legs.
    vec3 f = q - vec3(0.58, 0.19, -0.4);
    res = opU(res, drum(f, 0.2, 0.19, M_FLOORTOM, M_CHROME));
    res = opU(res, vec2(sd_tripod(floor_q - vec3(0.58, 0.0, -0.4), 0.3, 0.5, 0.008, 0.4),
                        float(M_CHROME)));

    // Snare on its basket stand.
    vec3 s = q - vec3(-0.46, 0.32, -0.46);
    s.yz = rot2(0.12) * s.yz;
    res = opU(res, drum(s, 0.178, 0.065, M_CHROME, M_CHROME));
    res = opU(res, vec2(sd_stand(floor_q - vec3(-0.46, 0.0, -0.46), 0.26, 0.55, 0.9),
                        float(M_CHROME)));

    // Hi-hat: two cymbals on a pull rod.
    vec3 h = floor_q - vec3(-0.86, 0.0, -0.3);
    res = opU(res, vec2(sd_stand(h, 0.28, 0.96, 0.2), float(M_CHROME)));
    res = opU(res, vec2(min(sd_cymbal(h - vec3(0.0, 0.92, 0.0), 0.178),
                            sd_cymbal(h - vec3(0.0, 0.945, 0.0), 0.178)), float(M_CYMBAL)));

    // Crash, stage left, tipped toward the drummer.
    vec3 cb = floor_q - vec3(-0.62, 0.0, -0.05);
    res = opU(res, vec2(sd_stand(cb, 0.3, 1.18, 1.1), float(M_CHROME)));
    vec3 cc = cb - vec3(0.0, 1.22, 0.0);
    cc.yz = rot2(0.3) * cc.yz;
    cc.xy = rot2(-0.15) * cc.xy;
    res = opU(res, vec2(sd_cymbal(cc, 0.23), float(M_CYMBAL)));

    // Ride, stage right, over the floor tom.
    vec3 rb = floor_q - vec3(0.86, 0.0, -0.18);
    res = opU(res, vec2(sd_stand(rb, 0.3, 1.02, 0.5), float(M_CHROME)));
    vec3 rc = rb - vec3(0.0, 1.06, 0.0);
    rc.yz = rot2(0.22) * rc.yz;
    rc.xy = rot2(0.18) * rc.xy;
    res = opU(res, vec2(sd_cymbal(rc, 0.26), float(M_CYMBAL)));

    // The throne.
    vec3 th = floor_q - vec3(0.0, 0.0, -0.8);
    res = opU(res, vec2(sd_cyl(th - vec3(0.0, 0.52, 0.0), 0.16, 0.035) - 0.012, float(M_BLACK)));
    res = opU(res, vec2(min(sd_tripod(th, 0.28, 0.18, 0.009, 0.0),
                            sd_capsule(th, vec3(0.0, 0.15, 0.0), vec3(0.0, 0.48, 0.0), 0.014)),
                        float(M_CHROME)));
    return res;
}

vec2 sd_mic_stand(vec3 p) {
    vec3 q = p - MIC;
    float base = min(sd_tripod(q, 0.26, 0.16, 0.008, 0.3),
                     sd_capsule(q, vec3(0.0, 0.12, 0.0), vec3(0.0, 0.9, 0.0), 0.011));
    base = min(base, sd_cyl(q - vec3(0.0, 0.92, 0.0), 0.017, 0.022));    // the clutch
    vec2 res = vec2(base, float(M_BLACK));
    res = opU(res, vec2(sd_capsule(q, vec3(0.0, 0.9, 0.0), vec3(0.0, 1.38, 0.0), 0.008),
                        float(M_CHROME)));
    // The mic in its clip, tipped a little toward the audience.
    vec3 m = q - vec3(0.0, 1.38, 0.0);
    m.yz = rot2(-0.3) * m.yz;
    res = opU(res, vec2(sd_capsule(m, vec3(0.0, 0.01, 0.0), vec3(0.0, 0.15, 0.0), 0.017),
                        float(M_MIC)));
    res = opU(res, vec2(length(m - vec3(0.0, 0.185, 0.0)) - 0.026, float(M_CHROME)));
    return res;
}

// Everything standing on the deck. Each item hides behind its own box: while
// the box is further than `B` away, its distance is a safe lower bound and the
// item's own field is never evaluated.
const float B = 0.15;

vec2 sd_backline(vec3 p) {
    vec2 res = vec2(1e5, 0.0);
    float b = sd_box(p - vec3(-2.42, 0.53, -2.32), vec3(0.42, 0.56, 0.24));
    res = opU(res, b > B ? vec2(b, 0.0)
                         : opU(sd_cabinet(p, CAB_L, CAB_L_SIZE), sd_amp_head(p)));
    b = sd_box(p - COMBO, COMBO_SIZE + 0.04);
    res = opU(res, b > B ? vec2(b, 0.0) : sd_combo(p));
    b = sd_box(p - CAB_R, CAB_R_SIZE + 0.04);
    res = opU(res, b > B ? vec2(b, 0.0) : sd_cabinet(p, CAB_R, CAB_R_SIZE));
    b = sd_box(p - GUITAR - vec3(0.0, 0.66, -0.12), vec3(0.24, 0.68, 0.26));
    res = opU(res, b > B ? vec2(b, 0.0) : sd_guitar(p));
    b = sd_box(p - KICK - vec3(0.0, 0.45, -0.3), vec3(1.3, 0.8, 0.82));
    res = opU(res, b > B ? vec2(b, 0.0) : sd_drum_kit(p));
    b = sd_box(p - MIC - vec3(0.0, 0.8, 0.0), vec3(0.32, 0.84, 0.32));
    res = opU(res, b > B ? vec2(b, 0.0) : sd_mic_stand(p));
    return res;
}

// ----------------------------------------------------------------- rig ----
// A can on a U-yoke: tilts about its own X, which is kept level; a bolt up to
// the clamp, and an arm back to the wall when the clamp is on one.
vec2 sd_can(vec3 p, int k) {
    vec3 c = u_can_pos[k];
    vec3 size = u_can_size[k];
    float radius = max(size.x, size.y);
    vec3 lp = to_local(p, c, u_can_x[k], u_can_y[k], u_can_z[k]);
    vec2 res = vec2(sd_cyl_z(lp, radius, size.z) - 0.004, float(M_BODY));
    float rim = max(abs(length(lp.xy) - radius * 0.92) - radius * 0.08,
                    abs(lp.z - size.z - 0.01) - 0.012);
    res = opU(res, vec2(rim, float(M_BODY)));
    float lens = sd_cyl_z(lp - vec3(0.0, 0.0, size.z + 0.002), radius * 0.84, 0.004);
    int light = u_can_light[k];
    res = opU(res, vec2(lens, float(light >= 0 ? M_LENS + light : M_LENS_DARK)));

    vec3 yx = u_can_x[k];
    vec3 yz = normalize(cross(yx, vec3(0.0, 1.0, 0.0)));
    vec3 yp = p - c;
    yp = vec3(dot(yp, yx), yp.y, dot(yp, yz));
    float arm_h = radius + 0.06;
    float yoke = sd_box(vec3(abs(yp.x) - radius - 0.018, yp.y - arm_h * 0.5, yp.z),
                        vec3(0.005, arm_h * 0.5 + 0.012, 0.022));
    yoke = min(yoke, sd_box(yp - vec3(0.0, arm_h, 0.0), vec3(radius + 0.023, 0.006, 0.022)));
    yoke = min(yoke, sd_cyl_x(vec3(abs(yp.x) - radius - 0.03, yp.y, yp.z), 0.018, 0.008));
    vec3 top = c + vec3(0.0, arm_h, 0.0);
    yoke = min(yoke, sd_capsule(p, top, u_can_mount[k], 0.008));
    yoke = min(yoke, sd_capsule(p, u_can_mount[k], u_can_anchor[k], 0.016));
    yoke = min(yoke, sd_rbox(p - u_can_mount[k], vec3(0.03, 0.035, 0.035), 0.008));
    return opU(res, vec2(yoke, float(M_BODY)));
}

// The draft's moving head, hung from a clamp: base on top, yoke turning with
// pan, head turning with pan and tilt.
vec2 sd_head(vec3 p, int k) {
    vec3 size = u_head_size[k];
    float arm_x = size.x + 0.05;
    float arm_y = size.z + 0.08;
    vec3 mp = p - u_head_pos[k];
    float base_y = arm_y * 1.15 + 0.09;
    float d = sd_rbox(mp - vec3(0.0, base_y, 0.0),
                      vec3(arm_x + 0.04, 0.05, size.y + 0.06), 0.012);
    vec3 yp = vec3(dot(mp, u_yoke_x[k]), mp.y, dot(mp, u_yoke_z[k]));
    d = min(d, sd_box(yp - vec3(0.0, arm_y * 1.1, 0.0), vec3(arm_x, 0.03, size.y * 0.95)));
    d = min(d, sd_box(vec3(abs(yp.x) - arm_x, yp.y - arm_y * 0.5, yp.z),
                      vec3(0.028, arm_y * 0.6, size.y * 0.85)));
    d = min(d, sd_capsule(p, u_head_pos[k] + vec3(0.0, base_y + 0.05, 0.0), u_head_mount[k], 0.01));
    d = min(d, sd_rbox(p - u_head_mount[k], vec3(0.035, 0.035, 0.035), 0.008));
    vec3 hp = to_local(p, u_head_pos[k], u_head_x[k], u_head_y[k], u_head_z[k]);
    d = min(d, sd_rbox(hp, size, 0.02));
    vec2 res = vec2(d, float(M_BODY));
    float lens = sd_cyl_z(hp - vec3(0.0, 0.0, size.z + 0.004),
                          min(size.x, size.y) * 0.85, 0.005);
    int light = u_head_light[k];
    return opU(res, vec2(lens, float(light >= 0 ? M_LENS + light : M_LENS_DARK)));
}

vec2 sd_rig(vec3 p) {
    float pipes = sd_capsule(p, vec3(-HALF_W, FRONT_PIPE_Y, FRONT_PIPE_Z),
                             vec3(HALF_W, FRONT_PIPE_Y, FRONT_PIPE_Z), 0.024);
    pipes = min(pipes, sd_capsule(p, vec3(-HALF_W, BACK_PIPE_Y, BACK_PIPE_Z),
                                  vec3(HALF_W, BACK_PIPE_Y, BACK_PIPE_Z), 0.024));
    pipes = min(pipes, sd_capsule(p, vec3(-HALF_W, FOH_PIPE_Y, FOH_PIPE_Z),
                                  vec3(HALF_W, FOH_PIPE_Y, FOH_PIPE_Z), 0.024));
    vec3 m = vec3(abs(p.x), p.yz);
    pipes = min(pipes, sd_capsule(m, vec3(2.2, FRONT_PIPE_Y, FRONT_PIPE_Z),
                                  vec3(2.2, CEIL_Y, FRONT_PIPE_Z), 0.012));
    pipes = min(pipes, sd_capsule(m, vec3(2.2, FOH_PIPE_Y, FOH_PIPE_Z),
                                  vec3(2.2, CEIL_Y, FOH_PIPE_Z), 0.012));
    vec2 res = vec2(pipes, float(M_PIPE));
    // The house downlights: a trim ring flush with the ceiling, a lit diffuser.
    vec3 hq = m - vec3(HOUSE_X, CEIL_Y, HOUSE_Z);
    res = opU(res, vec2(sd_cyl(hq, 0.075, 0.02), float(M_BLACK)));
    res = opU(res, vec2(sd_cyl(hq + vec3(0.0, 0.022, 0.0), 0.058, 0.003), float(M_BULB)));
    for (int k = 0; k < MAX_CANS; ++k) {
        if (k >= u_can_count) { break; }
        float bound = length(p - u_can_pos[k]) - 0.5;
        res = opU(res, bound > B ? vec2(bound, 0.0) : sd_can(p, k));
    }
    for (int k = 0; k < MAX_HEADS; ++k) {
        if (k >= u_head_count) { break; }
        float bound = length(p - u_head_pos[k] - vec3(0.0, 0.2, 0.0)) - 0.5;
        res = opU(res, bound > B ? vec2(bound, 0.0) : sd_head(p, k));
    }
    return res;
}

// ------------------------------------------------------------- audience ---
// A bentwood bistro chair, back toward +Z: the photo shows the backs of them.
float sd_chair(vec3 q) {
    float seat = sd_cyl(q - vec3(0.0, 0.455, 0.0), 0.2, 0.012) - 0.005;
    vec3 m = vec3(abs(q.x), q.yz);
    float legs = sd_capsule(m, vec3(0.16, 0.0, -0.18), vec3(0.14, 0.44, -0.13), 0.013);
    legs = min(legs, sd_capsule(m, vec3(0.16, 0.0, 0.2), vec3(0.14, 0.44, 0.14), 0.013));
    float uprights = sd_capsule(m, vec3(0.14, 0.44, 0.14), vec3(0.15, 0.8, 0.2), 0.012);
    // The bentwood loop: the top half of a ring through both uprights.
    vec3 b = q - vec3(0.0, 0.8, 0.2);
    float loop = max(length(vec2(length(b.xy) - 0.15, b.z)) - 0.012, -b.y);
    float rail = sd_capsule(m, vec3(0.0, 0.63, 0.178), vec3(0.146, 0.63, 0.178), 0.009);
    float ring = sd_torus(q - vec3(0.0, 0.2, 0.01), 0.2, 0.008);
    return min(min(min(seat, legs), min(uprights, loop)), min(rail, ring));
}

vec2 sd_chairs(vec3 p) {
    vec3 q = p - vec3(0.0, FLOOR_Y, CHAIR_Z0);
    float half_cols = 0.5 * float(CHAIR_COLS - 1);
    float col = clamp(floor(q.x / CHAIR_DX + half_cols + 0.5), 0.0, float(CHAIR_COLS - 1));
    float row = clamp(floor(q.z / CHAIR_DZ + 0.5), 0.0, float(CHAIR_ROWS - 1));
    q.x -= (col - half_cols) * CHAIR_DX;
    q.z -= row * CHAIR_DZ;
    // Only this cell's chair is evaluated, so the distance may not reach past
    // the cell's edge: the next chair is waiting a few centimetres beyond it,
    // and a step that overshot into it drew the bentwood loops as dashes.
    float edge = min(0.5 * CHAIR_DX - abs(q.x), 0.5 * CHAIR_DZ - abs(q.z)) + 0.03;
    // Nobody lines chairs up with a ruler.
    float h = hash12(vec2(col, row) + 3.7);
    q.xz = rot2((h - 0.5) * 0.24) * (q.xz - vec2((h - 0.5) * 0.05, (fract(h * 7.3) - 0.5) * 0.08));
    return vec2(min(sd_chair(q), max(edge, 0.005)), float(M_CHAIR));
}

// ------------------------------------------------------------------ bar ---
float sd_bottles(vec3 p, float shelf_y, float seed) {
    vec3 q = p - vec3(BAR_X, shelf_y, REAR_Z - 0.13);
    float cell = clamp(floor(q.x / 0.11 + 0.5), -17.0, 17.0);
    q.x -= cell * 0.11;
    float h = hash12(vec2(cell, seed));
    float tall = 0.2 + 0.13 * fract(h * 3.1);
    float radius = 0.03 + 0.012 * fract(h * 5.7);
    float body = sd_cyl(q - vec3(0.0, tall * 0.33, 0.0), radius, tall * 0.33) - 0.004;
    float neck = sd_capsule(q, vec3(0.0, tall * 0.62, 0.0), vec3(0.0, tall, 0.0), 0.012);
    float bottle = smin(body, neck, 0.03);
    return h < 0.12 ? max(bottle, 0.05) : bottle;      // the odd gap on the shelf
}

vec2 sd_bar(vec3 p) {
    vec2 res = vec2(sd_rbox(p - vec3(BAR_X, FLOOR_Y + 0.52, BAR_Z), vec3(2.3, 0.52, 0.28), 0.01),
                    float(M_COUNTER));
    res = opU(res, vec2(sd_rbox(p - vec3(BAR_X, FLOOR_Y + 1.07, BAR_Z - 0.03),
                                vec3(2.38, 0.028, 0.34), 0.012), float(M_COUNTER_TOP)));
    res = opU(res, vec2(sd_capsule(p, vec3(BAR_X - 2.28, FLOOR_Y + 0.2, BAR_Z - 0.42),
                                   vec3(BAR_X + 2.28, FLOOR_Y + 0.2, BAR_Z - 0.42), 0.02),
                        float(M_BRASS)));
    // The back bar: two shelves of bottles against the rear wall, under a
    // pelmet that hides the strip light.
    res = opU(res, vec2(sd_box(p - vec3(BAR_X, FLOOR_Y + 2.26, REAR_Z - 0.13),
                               vec3(2.05, 0.05, 0.13)), float(M_COUNTER)));
    for (int i = 0; i < 2; ++i) {
        float shelf_y = FLOOR_Y + 1.42 + 0.42 * float(i);
        res = opU(res, vec2(sd_box(p - vec3(BAR_X, shelf_y - 0.015, REAR_Z - 0.14),
                                   vec3(2.0, 0.015, 0.14)), float(M_COUNTER_TOP)));
        if (abs(p.y - shelf_y - 0.18) < 0.3) {
            res = opU(res, vec2(sd_bottles(p, shelf_y, float(i) + 1.3), float(M_BOTTLE)));
        }
    }
    // Stools along the front of the counter.
    vec3 s = p - vec3(BAR_X, FLOOR_Y, BAR_Z - 0.64);
    s.x -= clamp(floor(s.x / 0.8 + 0.5), -2.0, 2.0) * 0.8;
    res = opU(res, vec2(sd_cyl(s - vec3(0.0, 0.76, 0.0), 0.17, 0.022) - 0.01, float(M_CHAIR)));
    float frame = min(sd_capsule(s, vec3(0.0, 0.02, 0.0), vec3(0.0, 0.74, 0.0), 0.02),
                      sd_torus(s - vec3(0.0, 0.3, 0.0), 0.16, 0.009));
    frame = min(frame, sd_cyl(s - vec3(0.0, 0.01, 0.0), 0.2, 0.01));
    res = opU(res, vec2(frame, float(M_BLACK)));
    // Three pendants over the counter.
    vec3 pq = p - vec3(BAR_X, PENDANT_Y, BAR_Z);
    pq.x -= clamp(floor(pq.x / PENDANT_DX + 0.5), -1.0, 1.0) * PENDANT_DX;
    float cord = sd_capsule(pq, vec3(0.0, 0.1, 0.0), vec3(0.0, CEIL_Y - PENDANT_Y, 0.0), 0.004);
    float radial = length(pq.xz);
    float shade = max(abs(radial - (0.035 + 0.7 * (0.1 - pq.y))) * 0.82 - 0.002,
                      abs(pq.y) - 0.1);
    shade = min(shade, sd_cyl(pq - vec3(0.0, 0.1, 0.0), 0.04, 0.02));
    res = opU(res, vec2(min(cord, shade), float(M_BLACK)));
    res = opU(res, vec2(length(pq - vec3(0.0, -0.02, 0.0)) - 0.045, float(M_BULB)));
    return res;
}

// ---------------------------------------------------------------- walls ---
vec2 sd_fittings(vec3 p) {
    vec3 m = vec3(abs(p.x), p.yz);
    float conduit = sd_capsule(m, vec3(HALF_W - 0.025, 0.25, -0.7),
                               vec3(HALF_W - 0.025, CEIL_Y, -0.7), 0.012);
    conduit = min(conduit, sd_rbox(m - vec3(HALF_W - 0.03, 0.2, -0.7),
                                   vec3(0.03, 0.06, 0.05), 0.005));
    return vec2(conduit, float(M_CONDUIT));
}

// ------------------------------------------------------ the whole field ---
// Props only: the room and the deck are intersected analytically, so they are
// not here. `arch_distance` adds them back where a query needs them (AO).
vec2 map(vec3 p) {
    vec2 res = vec2(sd_drape(p), float(M_CURTAIN));
    float b = sd_box(p - vec3(0.0, 0.8, -1.7), vec3(HALF_W, 0.85, 1.55));
    res = opU(res, b > B ? vec2(b, 0.0) : sd_backline(p));
    b = sd_box(p - vec3(0.0, 2.92, 0.5 * (BACK_Z + HOUSE_Z + 0.5)),
               vec3(HALF_W, 0.5, 0.5 * (HOUSE_Z + 0.5 - BACK_Z)));
    res = opU(res, b > B ? vec2(b, 0.0) : sd_rig(p));
    float last_row = CHAIR_Z0 + CHAIR_DZ * float(CHAIR_ROWS - 1);
    b = sd_box(p - vec3(0.0, FLOOR_Y + 0.5, 0.5 * (CHAIR_Z0 + last_row)),
               vec3(HALF_W, 0.5, 0.5 * (last_row - CHAIR_Z0) + 0.4));
    res = opU(res, b > B ? vec2(b, 0.0) : sd_chairs(p));
    b = sd_box(p - vec3(0.0, 0.5 * (FLOOR_Y + CEIL_Y), 0.5 * (BAR_Z - 1.0 + REAR_Z)),
               vec3(HALF_W, 0.5 * (CEIL_Y - FLOOR_Y), 0.5 * (REAR_Z - BAR_Z + 1.0)));
    res = opU(res, b > B ? vec2(b, 0.0) : sd_bar(p));
    b = HALF_W - 0.1 - abs(p.x);
    res = opU(res, b > B ? vec2(b, 0.0) : sd_fittings(p));
    return res;
}

float arch_distance(vec3 p) {
    float room = min(min(p.x + HALF_W, HALF_W - p.x),
                     min(min(p.y - FLOOR_Y, CEIL_Y - p.y), min(p.z - BACK_Z, REAR_Z - p.z)));
    float deck = sd_box(p - vec3(0.0, 0.5 * FLOOR_Y, 0.5 * BACK_Z),
                        vec3(HALF_W, -0.5 * FLOOR_Y, -0.5 * BACK_Z));
    return min(room, deck);
}

vec3 prop_normal(vec3 p, float t) {
    float e = 0.0004 + 0.00015 * t;
    const vec2 k = vec2(1.0, -1.0);
    return normalize(k.xyy * map(p + k.xyy * e).x + k.yyx * map(p + k.yyx * e).x
                   + k.yxy * map(p + k.yxy * e).x + k.xxx * map(p + k.xxx * e).x);
}

vec2 trace_props(vec3 origin, vec3 dir, float t_max) {
    float t = 0.01;
    for (int i = 0; i < TRACE_STEPS; ++i) {
        vec2 h = map(origin + dir * t);
        if (h.x < 0.0004 * t + 0.0002) { return vec2(t, h.y); }
        t += h.x;
        if (t > t_max) { break; }
    }
    return vec2(t_max, 0.0);
}

// The room is a box the camera is inside, and the deck a box it is outside:
// one slab exit, one slab entry, and whichever comes first is the surface.
float trace_arch(vec3 origin, vec3 dir, out int face, out vec3 normal) {
    vec3 safe = vec3(abs(dir.x) < 1e-6 ? 1e-6 : dir.x,
                     abs(dir.y) < 1e-6 ? 1e-6 : dir.y,
                     abs(dir.z) < 1e-6 ? 1e-6 : dir.z);
    vec3 inv = 1.0 / safe;
    vec3 lo = vec3(-HALF_W, FLOOR_Y, BACK_Z);
    vec3 hi = vec3(HALF_W, CEIL_Y, REAR_Z);
    vec3 far = max((lo - origin) * inv, (hi - origin) * inv);
    float t = min(min(far.x, far.y), far.z);
    if (t == far.x) {
        face = safe.x > 0.0 ? A_WALL_R : A_WALL_L;
        normal = vec3(safe.x > 0.0 ? -1.0 : 1.0, 0.0, 0.0);
    } else if (t == far.y) {
        face = safe.y > 0.0 ? A_CEILING : A_FLOOR;
        normal = vec3(0.0, safe.y > 0.0 ? -1.0 : 1.0, 0.0);
    } else {
        face = safe.z > 0.0 ? A_REAR : A_BACK;
        normal = vec3(0.0, 0.0, safe.z > 0.0 ? -1.0 : 1.0);
    }

    vec3 t0 = (vec3(-HALF_W, FLOOR_Y, BACK_Z) - origin) * inv;
    vec3 t1 = (vec3(HALF_W, 0.0, 0.0) - origin) * inv;
    vec3 near = min(t0, t1);
    vec3 exit = max(t0, t1);
    float enter = max(max(near.x, near.y), near.z);
    float leave = min(min(exit.x, exit.y), exit.z);
    if (enter > 0.0 && enter < leave && enter < t) {
        t = enter;
        if (enter == near.y) {
            face = A_DECK;
            normal = vec3(0.0, 1.0, 0.0);
        } else {
            face = A_FASCIA;
            normal = vec3(0.0, 0.0, 1.0);
        }
    }
    return t;
}

// ------------------------------------------------------------- surfaces ---
struct Surface {
    vec3 albedo;
    vec3 normal;
    vec3 emission;
    float rough;
    float metal;
    float sheen;
};

Surface plain(vec3 n, vec3 albedo, float rough, float metal) {
    Surface s;
    s.albedo = albedo;
    s.normal = n;
    s.emission = vec3(0.0);
    s.rough = rough;
    s.metal = metal;
    s.sheen = 0.0;
    return s;
}

// Running-bond brick. `uv` is in metres along the wall, v up.
// x: 1 on a brick face, 0 in the mortar; y: this brick's own random tone.
vec2 brick_cell(vec2 uv) {
    const vec2 PITCH = vec2(0.225, 0.075);
    float row = floor(uv.y / PITCH.y);
    float shifted = uv.x / PITCH.x + 0.5 * mod(row, 2.0);
    float col = floor(shifted);
    vec2 f = vec2(fract(shifted) * PITCH.x, fract(uv.y / PITCH.y) * PITCH.y);
    vec2 e = min(f, PITCH - f);
    return vec2(smoothstep(0.004, 0.009, min(e.x, e.y)), hash12(vec2(col, row)));
}

float brick_height(vec2 uv) {
    return brick_cell(uv).x * (0.006 + 0.002 * vnoise2(uv * 31.0))
         + 0.0012 * vnoise2(uv * 95.0);
}

Surface brick_wall(vec2 uv, vec3 n, vec3 tu, vec3 tv) {
    Surface s = plain(n, vec3(0.0), 0.9, 0.0);
    const float E = 0.0012;
    float h0 = brick_height(uv);
    float gu = (brick_height(uv + vec2(E, 0.0)) - h0) / E;
    float gv = (brick_height(uv + vec2(0.0, E)) - h0) / E;
    s.normal = normalize(n - (tu * gu + tv * gv) * 0.6);
    vec2 cell = brick_cell(uv);
    vec3 fired = mix(vec3(0.16, 0.085, 0.055), vec3(0.26, 0.15, 0.095), cell.y);
    fired *= 0.75 + 0.5 * vnoise2(uv * vec2(18.0, 40.0) + cell.y * 31.0);
    if (fract(cell.y * 7.13) > 0.86) { fired *= 0.55; }       // the odd overfired brick
    s.albedo = mix(vec3(0.12, 0.105, 0.088), fired, cell.x);
    // Decades of smoke: grimier toward the ceiling, and never even.
    s.albedo *= mix(1.0, 0.5, smoothstep(1.0, CEIL_Y, uv.y)) * (0.75 + 0.5 * fbm2(uv * 0.8));
    return s;
}

// Floorboards running along Z. Returns (distance to the board's edge, tone).
vec2 plank_cell(vec2 uv, float width, float seed) {
    float board = floor(uv.x / width);
    float hb = hash12(vec2(board, seed));
    float len = 1.6 + 1.6 * hb;
    float along = uv.y / len + hb * 3.7;
    float seg = floor(along);
    float fx = fract(uv.x / width) * width;
    float fz = fract(along) * len;
    float edge = min(min(fx, width - fx), min(fz, len - fz));
    return vec2(edge, hash12(vec2(board, seg) + seed));
}

float plank_height(vec2 uv, float width, float seed) {
    return 0.002 * smoothstep(0.0, 0.004, plank_cell(uv, width, seed).x);
}

Surface planks(vec2 uv, vec3 n, vec3 tu, vec3 tv, vec3 tint, float width, float seed) {
    Surface s = plain(n, vec3(0.0), 0.5, 0.0);
    const float E = 0.001;
    float h0 = plank_height(uv, width, seed);
    float gu = (plank_height(uv + vec2(E, 0.0), width, seed) - h0) / E;
    float gv = (plank_height(uv + vec2(0.0, E), width, seed) - h0) / E;
    s.normal = normalize(n - (tu * gu + tv * gv) * 0.8);
    vec2 cell = plank_cell(uv, width, seed);
    float grain = 0.6 * vnoise2(vec2(uv.x * 55.0 + cell.y * 40.0, uv.y * 1.3))
                + 0.4 * vnoise2(vec2(uv.x * 190.0, uv.y * 5.0));
    vec3 col = tint * (0.65 + 0.7 * cell.y) * (0.7 + 0.5 * grain);
    col *= mix(0.3, 1.0, smoothstep(0.0, 0.003, cell.x));
    float wear = fbm2(uv * 1.7 + seed);
    s.albedo = col * (0.85 + 0.35 * wear);
    s.rough = mix(0.5, 0.85, wear);
    return s;
}

Surface architecture(int face, vec3 p, vec3 n) {
    if (face == A_WALL_L || face == A_WALL_R) {
        return brick_wall(vec2(p.z, p.y), n, vec3(0.0, 0.0, 1.0), vec3(0.0, 1.0, 0.0));
    }
    if (face == A_REAR) {
        return brick_wall(vec2(p.x, p.y), n, vec3(1.0, 0.0, 0.0), vec3(0.0, 1.0, 0.0));
    }
    if (face == A_DECK) {
        return planks(vec2(p.x + HALF_W, p.z), n, vec3(1.0, 0.0, 0.0), vec3(0.0, 0.0, 1.0),
                      vec3(0.085, 0.064, 0.05), 0.14, 1.0);
    }
    if (face == A_FLOOR) {
        return planks(vec2(p.x + HALF_W, p.z), n, vec3(1.0, 0.0, 0.0), vec3(0.0, 0.0, 1.0),
                      vec3(0.045, 0.035, 0.028), 0.11, 7.0);
    }
    if (face == A_FASCIA) {
        // Black-painted riser front under a worn wooden nosing.
        if (p.y > -0.04) {
            return plain(n, vec3(0.09, 0.062, 0.045) * (0.7 + 0.5 * vnoise2(p.xy * vec2(3.0, 90.0))),
                         0.45, 0.0);
        }
        return plain(n, vec3(0.012) * (0.8 + 0.5 * fbm2(p.xy * 4.0)), 0.6, 0.0);
    }
    // Ceiling and the wall behind the drape: black, as a venue paints them.
    return plain(n, vec3(0.012) * (0.8 + 0.4 * fbm2(p.xz * 2.0 + p.y)), 0.8, 0.0);
}

Surface prop(int id, vec3 p, vec3 n) {
    if (id >= M_LENS) {
        Surface s = plain(n, vec3(0.02), 0.05, 0.0);
        s.emission = u_light_rgb[id - M_LENS] * LENS_GAIN;
        return s;
    }
    if (id == M_CURTAIN) {
        Surface s = plain(n, vec3(0.016, 0.014, 0.014)
                             * (0.8 + 0.4 * vnoise2(vec2(p.x * 90.0, p.y * 1.5))), 1.0, 0.0);
        s.sheen = 1.0;
        return s;
    }
    if (id == M_TOLEX) {
        vec3 grain = vec3(vnoise3(p * 180.0), vnoise3(p * 180.0 + 7.1), vnoise3(p * 180.0 + 3.3));
        return plain(normalize(n + (grain - 0.5) * 0.12), vec3(0.022), 0.55, 0.0);
    }
    if (id == M_GRILLE) {
        return plain(n, vec3(0.03, 0.03, 0.032) * (0.85 + 0.3 * vnoise2(p.xy * 60.0)), 0.95, 0.0);
    }
    if (id == M_GRILLE_LIGHT) {
        return plain(n, vec3(0.11, 0.1, 0.085) * (0.8 + 0.4 * vnoise2(p.xy * 140.0)), 0.95, 0.0);
    }
    if (id == M_PANEL) { return plain(n, vec3(0.6, 0.52, 0.38), 0.35, 0.9); }
    if (id == M_KNOB) { return plain(n, vec3(0.02), 0.3, 0.0); }
    if (id == M_JEWEL) {
        Surface s = plain(n, vec3(0.5, 0.02, 0.01), 0.1, 0.0);
        s.emission = vec3(4.0, 0.15, 0.05);
        return s;
    }
    if (id == M_CHROME) { return plain(n, vec3(0.62, 0.62, 0.64), 0.2, 1.0); }
    if (id == M_CYMBAL) {
        float lathe = vnoise2(vec2(length(p.xz) * 400.0, 0.0));
        return plain(n, vec3(0.74, 0.54, 0.3), 0.26 + 0.1 * lathe, 1.0);
    }
    if (id == M_DRUMHEAD) {
        // The kick's resonant head has a port hole, and nothing behind it.
        vec3 k = p - KICK;
        if (k.z > 0.19 && length(k.xy - vec2(0.09, -0.1)) < 0.055) {
            return plain(n, vec3(0.004), 0.9, 0.0);
        }
        return plain(n, vec3(0.5, 0.45, 0.36) * (0.85 + 0.3 * vnoise2(p.xy * 12.0)), 0.7, 0.0);
    }
    if (id == M_SHELL) { return plain(n, vec3(0.03, 0.017, 0.011), 0.22, 0.0); }
    if (id == M_FLOORTOM) {
        // Hammered: the dents are only in the normal.
        vec3 dents = vec3(vnoise3(p * 60.0), vnoise3(p * 60.0 + 5.2), vnoise3(p * 60.0 + 9.7));
        return plain(normalize(n + (dents - 0.5) * 0.22), vec3(0.4, 0.3, 0.18), 0.32, 0.8);
    }
    if (id == M_HOOP) { return plain(n, vec3(0.3, 0.16, 0.07), 0.3, 0.0); }
    if (id == M_GUITAR) {
        vec3 g = guitar_local(p);
        if (g.z < 0.018) { return plain(n, vec3(0.03, 0.012, 0.006), 0.15, 0.0); }
        float r = length((g.xy - vec2(0.0, 0.25)) * vec2(1.0, 0.75)) / 0.2;
        vec3 burst = mix(vec3(0.5, 0.28, 0.07), vec3(0.04, 0.014, 0.004), smoothstep(0.45, 1.05, r));
        return plain(n, burst, 0.12, 0.0);
    }
    if (id == M_NECK) {
        vec3 g = guitar_local(p);
        vec3 wood = g.z > 0.02 ? vec3(0.05, 0.03, 0.02) : vec3(0.4, 0.25, 0.11);
        return plain(n, wood, 0.4, 0.0);
    }
    if (id == M_BODY) { return plain(n, vec3(0.025), 0.45, 0.0); }
    if (id == M_PIPE) { return plain(n, vec3(0.03), 0.4, 0.3); }
    if (id == M_CHAIR) {
        float grain = vnoise2(vec2(p.x * 20.0 + p.z * 20.0, p.y * 150.0));
        return plain(n, vec3(0.075, 0.037, 0.018) * (0.8 + 0.4 * grain), 0.35, 0.0);
    }
    if (id == M_COUNTER) {
        // Panelled: a groove every 40 cm.
        float groove = smoothstep(0.004, 0.012, abs(fract(p.x / 0.4) - 0.5) * 0.4);
        return plain(n, vec3(0.09, 0.05, 0.028) * mix(0.4, 1.0, groove), 0.45, 0.0);
    }
    if (id == M_COUNTER_TOP) { return plain(n, vec3(0.16, 0.08, 0.04), 0.15, 0.0); }
    if (id == M_BRASS) { return plain(n, vec3(0.8, 0.6, 0.3), 0.3, 1.0); }
    if (id == M_BOTTLE) {
        float h = hash12(vec2(floor((p.x - BAR_X) / 0.11 + 0.5), floor(p.y * 2.4)));
        vec3 glass = h < 0.35 ? vec3(0.05, 0.16, 0.05) : h < 0.7 ? vec3(0.3, 0.14, 0.03)
                                                                 : vec3(0.3, 0.3, 0.28);
        Surface s = plain(n, glass, 0.06, 0.0);
        s.emission = glass * u_bulb_rgb * 0.45;   // lit from behind by the back bar strip
        return s;
    }
    if (id == M_MIC) { return plain(n, vec3(0.08), 0.35, 0.6); }
    if (id == M_CONDUIT) { return plain(n, vec3(0.3, 0.3, 0.32), 0.5, 0.8); }
    if (id == M_BULB) {
        Surface s = plain(n, vec3(0.8), 0.2, 0.0);
        s.emission = u_bulb_rgb * 24.0;
        return s;
    }
    if (id == M_LENS_DARK) { return plain(n, vec3(0.02), 0.05, 0.0); }
    return plain(n, vec3(0.018), 0.6, 0.0);       // M_BLACK: stands, handles, rubber
}

// ------------------------------------------------------------- lighting ---
// How much of light `i` reaches `p`, before surface orientation: the cone IS
// the fixture's optics, and the inverse square is the room being a room.
float spot(int i, vec3 p, out vec3 to_light, out float dist) {
    vec3 offset = u_light_pos[i] - p;
    dist = max(length(offset), 1e-4);
    to_light = offset / dist;
    float c = dot(-to_light, u_light_dir[i]);
    float cone = smoothstep(u_light_shape[i].x, u_light_shape[i].y, c);
    if (cone <= 0.0) { return 0.0; }
    // A real lens is hotter in the middle than a flat cone.
    cone *= 0.55 + 0.45 * smoothstep(u_light_shape[i].x, 1.0, c);
    return cone / (dist * dist + 0.05);
}

// Soft shadows from the distance field (after Inigo Quilez): the nearest miss
// along the ray, as an angle, against the angle the lens subtends from here.
//
// The classic estimate, not Quilez's "improved" one. That one triangulates the
// closest approach from two consecutive distances and assumes the field is
// continuous between them; the bounding boxes in `map` are not — a group's box
// distance jumps to the group's true distance on entry — and wherever it
// jumped by more than double, the triangulation collapsed to zero and printed
// every bounding sphere on the deck as a ring of shadow.
float soft_shadow(vec3 origin, vec3 dir, float t_max, float k) {
    float res = 1.0;
    float t = 0.015;
    for (int i = 0; i < SHADOW_STEPS; ++i) {
        float h = map(origin + dir * t).x;
        if (h < 0.0004) { return 0.0; }
        res = min(res, k * h / t);
        t += clamp(h, 0.01, 0.6);
        if (res < 0.003 || t > t_max) { break; }
    }
    res = clamp(res, 0.0, 1.0);
    return res * res * (3.0 - 2.0 * res);
}

float occlusion(vec3 p, vec3 n) {
    float occ = 0.0;
    float weight = 1.0;
    for (int i = 0; i < 5; ++i) {
        float h = 0.015 + 0.075 * float(i);
        float d = min(map(p + n * h).x, arch_distance(p + n * h));
        occ += (h - d) * weight;
        weight *= 0.75;
    }
    return clamp(1.0 - 2.2 * occ, 0.0, 1.0);
}

vec3 brdf(Surface s, vec3 n, vec3 v, vec3 l) {
    vec3 h = normalize(l + v);
    float nl = max(dot(n, l), 1e-3);
    float nv = max(dot(n, v), 1e-3);
    float nh = max(dot(n, h), 0.0);
    float vh = max(dot(v, h), 0.0);
    float a = max(s.rough * s.rough, 0.002);
    float a2 = a * a;
    float den = nh * nh * (a2 - 1.0) + 1.0;
    float ndf = a2 / (PI * den * den);
    float k = (s.rough + 1.0) * (s.rough + 1.0) / 8.0;
    float geo = (nl / (nl * (1.0 - k) + k)) * (nv / (nv * (1.0 - k) + k));
    vec3 f0 = mix(vec3(0.04), s.albedo, s.metal);
    vec3 fresnel = f0 + (1.0 - f0) * pow(1.0 - vh, 5.0);
    vec3 specular = ndf * geo * fresnel / (4.0 * nl * nv);
    vec3 diffuse = (1.0 - s.metal) * (1.0 - fresnel) * s.albedo / PI;
    // Velour: fibres standing on end catch light at the silhouette.
    diffuse += s.sheen * vec3(0.06, 0.055, 0.055) * pow(1.0 - nv, 4.0);
    return diffuse + specular;
}

vec3 shade(Surface s, vec3 p, vec3 v) {
    vec3 n = s.normal;
    float ao = occlusion(p, n);
    vec3 total = s.emission;
    for (int i = 0; i < MAX_LIGHTS; ++i) {
        if (i >= u_light_count) { break; }
        vec3 l;
        float dist;
        float fall = spot(i, p, l, dist);
        if (fall <= 0.0) { continue; }
        float nl = dot(n, l);
        if (nl <= 0.0) { continue; }
        // Worth a shadow ray only if the light would show here at all: the
        // far tail of a cone is most of its footprint and none of its light.
        vec3 lit = u_light_rgb[i] * (fall * nl) * brdf(s, n, v, l);
        if (max(lit.r, max(lit.g, lit.b)) < 2e-4) { continue; }
        float lens = max(u_light_shape[i].z, 0.01);
        total += lit * soft_shadow(p + n * 0.004, l, dist - lens - 0.06, dist / lens);
    }
    vec3 matte = s.albedo * (1.0 - s.metal) / PI;
    for (int j = 0; j < MAX_BOUNCES; ++j) {
        if (j >= u_bounce_count) { break; }
        vec3 d = u_bounce_pos[j] - p;
        float dist2 = dot(d, d);
        vec3 l = d * inversesqrt(max(dist2, 1e-6));
        float emit = max(dot(-l, u_bounce_nrm[j]), 0.0);
        float nl = max(dot(n, l), 0.0);
        total += u_bounce_rgb[j] * (emit * nl / (dist2 + 0.6)) * matte * ao;
    }
    // Metal has no diffuse to show the ambient with; what it shows is the room
    // it reflects — mostly dark, warmer and brighter toward the lit deck.
    vec3 r = reflect(-v, n);
    vec3 room = vec3(1.0, 0.72, 0.48) * (0.35 + 0.65 * smoothstep(0.2, -0.6, r.y));
    total += (matte * PI + s.albedo * s.metal * room * 3.0) * AMBIENT * ao;
    return total;
}

// ------------------------------------------------------------------ air ---
// Henyey-Greenstein, normalised to its broadside value, as in the draft.
float scatter_gain(float cosine, float g) {
    float denom = 1.0 + g * g - 2.0 * g * cosine;
    return pow(1.0 + g * g, 1.5) / pow(max(denom, 1e-4), 1.5);
}

// The union of every shaft-throwing cone along this ray — solved, not swept.
// See the draft: 40 samples spread over a 12 m room put none inside a beam.
bool air_segment(vec3 origin, vec3 dir, float t_end, out float t0, out float t1) {
    t0 = t_end;
    t1 = 0.0;
    bool found = false;
    for (int i = 0; i < MAX_LIGHTS; ++i) {
        if (i >= u_light_count) { break; }
        if (u_light_shape[i].w <= 0.25) { continue; }
        vec3 apex = u_light_pos[i];
        vec3 axis = u_light_dir[i];
        vec3 co = origin - apex;
        float along = -dot(co, dir);
        float perp = dot(co, co) - along * along;
        if (perp > BEAM_REACH * BEAM_REACH) { continue; }
        float chord = sqrt(BEAM_REACH * BEAM_REACH - perp);
        float enter = along - chord;
        float leave = along + chord;
        float kk = u_light_shape[i].x * u_light_shape[i].x;
        float dv = dot(dir, axis);
        float cv = dot(co, axis);
        float qa = dv * dv - kk;
        float qb = 2.0 * (dv * cv - kk * dot(dir, co));
        float qc = cv * cv - kk * dot(co, co);
        if (abs(qa) > 1e-5) {
            float disc = qb * qb - 4.0 * qa * qc;
            if (disc <= 0.0) { continue; }
            float root = sqrt(disc);
            float ta = (-qb - root) / (2.0 * qa);
            float tb = (-qb + root) / (2.0 * qa);
            enter = max(enter, min(ta, tb));
            leave = min(leave, max(ta, tb));
        }
        enter = max(enter, 0.0);
        leave = min(leave, t_end);
        if (leave <= enter) { continue; }
        vec3 middle = origin + dir * (0.5 * (enter + leave)) - apex;
        if (dot(middle, axis) <= 0.0) { continue; }
        t0 = min(t0, enter);
        t1 = max(t1, leave);
        found = true;
    }
    return found && t1 > t0;
}

vec3 march_air(vec3 origin, vec3 dir, float t_end) {
    if (u_haze <= 0.001) { return vec3(0.0); }
    float t0;
    float t1;
    if (!air_segment(origin, dir, t_end, t0, t1)) { return vec3(0.0); }
    float dt = (t1 - t0) / float(MARCH_STEPS);
    float jitter = hash12(gl_FragCoord.xy + vec2(u_seed * 17.31, u_seed * 3.7));
    vec3 total = vec3(0.0);
    for (int k = 0; k < MARCH_STEPS; ++k) {
        vec3 p = origin + dir * (t0 + (float(k) + jitter) * dt);
        float height = exp(-max(p.y - FLOOR_Y, 0.0) / 3.5);
        float texture = 0.55 + 0.9 * vnoise3(p * 0.7 + vec3(0.0, 0.0, u_time * 0.05));
        float density = u_haze * height * texture;
        vec3 radiance = vec3(0.0);
        for (int i = 0; i < MAX_LIGHTS; ++i) {
            if (i >= u_light_count) { break; }
            float air = u_light_shape[i].w;
            if (air <= 0.25) { continue; }
            vec3 l;
            float dist;
            float fall = spot(i, p, l, dist);
            if (fall <= 0.0) { continue; }
            radiance += u_light_rgb[i] * (fall * air * scatter_gain(dot(dir, -l), 0.45));
        }
        total += density * radiance * dt;
    }
    return total * AIR_GAIN;
}

// A lamp seen down its barrel blooms in the lens of whoever is looking.
vec3 lamp_glow(vec3 origin, vec3 dir, float t_hit) {
    vec3 total = vec3(0.0);
    for (int i = 0; i < MAX_LIGHTS; ++i) {
        if (i >= u_light_count) { break; }
        vec3 to = u_light_pos[i] - origin;
        float along = dot(to, dir);
        if (along <= 0.0 || along > t_hit + 0.06) { continue; }
        float across = sqrt(max(dot(to, to) - along * along, 0.0));
        float angle = across / along;
        float facing = max(dot(-dir, u_light_dir[i]), 0.0);
        float halo = 0.3 * exp(-angle * angle / 0.00008) + 0.012 * exp(-angle / 0.03);
        total += u_light_rgb[i] * halo * facing * facing * facing;
    }
    return total;
}

void main() {
    vec2 pixel = gl_FragCoord.xy + u_jitter;
    vec2 ndc = pixel / u_resolution * 2.0 - 1.0;
    float aspect = u_resolution.x / u_resolution.y;
    vec3 dir = normalize(u_cam_fwd + u_cam_right * (ndc.x * aspect * u_tan_half_fov)
                         + u_cam_up * (ndc.y * u_tan_half_fov));

    int face;
    vec3 arch_normal;
    float t_arch = trace_arch(u_cam_pos, dir, face, arch_normal);
    vec2 hit = trace_props(u_cam_pos, dir, t_arch);

    vec3 p = u_cam_pos + dir * hit.x;
    Surface s = hit.y > 0.5 ? prop(int(hit.y + 0.5), p, prop_normal(p, hit.x))
                            : architecture(face, p, arch_normal);
    // Normal maps may tip a normal past the silhouette; keep it facing us.
    if (dot(s.normal, -dir) < 0.0) { s.normal = normalize(s.normal - dir * dot(s.normal, -dir) * 1.01); }

    vec3 hdr = shade(s, p, -dir);
    hdr += march_air(u_cam_pos, dir, hit.x);
    hdr += lamp_glow(u_cam_pos, dir, hit.x);
    if (any(isnan(hdr)) || any(isinf(hdr))) { hdr = vec3(0.0); }
    fragColor = vec4(clamp(hdr, 0.0, 64.0), 1.0);
}
"""

RESOLVE_FRAGMENT = """
#version 330

out vec4 fragColor;

uniform sampler2D u_accum;
uniform float u_exposure;
uniform vec2  u_resolution;

// ACES filmic tonemap, Stephen Hill's fitted RRT+ODT (MIT, from BakingLab),
// verbatim from stage/gpu/shaders.py so this agrees with the app about what
// "brighter than white" looks like. The matrices are transposed on purpose.
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

float hash12(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

void main() {
    vec3 hdr = texelFetch(u_accum, ivec2(gl_FragCoord.xy), 0).rgb;
    // A real lens darkens toward its corners, and the photograph's does a lot.
    vec2 c = (gl_FragCoord.xy / u_resolution - 0.5) * vec2(u_resolution.x / u_resolution.y, 1.0);
    float vignette = 1.0 / (1.0 + 1.1 * dot(c, c));
    vec3 mapped = aces_fitted(hdr * u_exposure * vignette * vignette);
    vec3 encoded = pow(max(mapped, 0.0), vec3(1.0 / 2.2));
    // A little grain, so the darks read as film rather than as banding.
    encoded += (hash12(gl_FragCoord.xy) - 0.5) * 0.012;
    fragColor = vec4(encoded, 1.0);
}
"""


def scene_constants() -> str:
    """The venue's numbers, as GLSL — the shader reads the same values the rig
    is placed with, rather than a copy that can drift from them."""
    floats = {
        "HALF_W": HALF_WIDTH, "FLOOR_Y": -DECK_HEIGHT, "CEIL_Y": CEILING_Y,
        "BACK_Z": BACK_WALL_Z, "REAR_Z": REAR_WALL_Z, "CURTAIN_Z": CURTAIN_Z,
        "CURTAIN_TOP": CURTAIN_TOP,
        "FRONT_PIPE_Y": FRONT_PIPE[0], "FRONT_PIPE_Z": FRONT_PIPE[1],
        "BACK_PIPE_Y": BACK_PIPE[0], "BACK_PIPE_Z": BACK_PIPE[1],
        "FOH_PIPE_Y": FOH_PIPE[0], "FOH_PIPE_Z": FOH_PIPE[1],
        "CHAIR_Z0": CHAIR_Z0, "CHAIR_DZ": CHAIR_DZ, "CHAIR_DX": CHAIR_DX,
        "BAR_X": BAR_X, "BAR_Z": BAR_Z,
        "PENDANT_Y": PENDANT_Y, "PENDANT_DX": PENDANT_DX,
        "HOUSE_X": HOUSE_X, "HOUSE_Z": HOUSE_Z,
    }
    ints = {
        "MAX_LIGHTS": MAX_LIGHTS, "MAX_BOUNCES": MAX_BOUNCES,
        "MAX_CANS": MAX_CANS, "MAX_HEADS": MAX_HEADS,
        "CHAIR_ROWS": CHAIR_ROWS, "CHAIR_COLS": CHAIR_COLS,
    }
    lines = [f"const float {name} = {value:.5f};" for name, value in floats.items()]
    lines += [f"const int {name} = {value};" for name, value in ints.items()]
    return "\n".join(lines)


# ==========================================================================
# The renderer: a scene pass averaged into a float buffer, then a resolve.
# ==========================================================================

_GL_FRAMEBUFFER = 0x8D40
_GL_BLEND = 0x0BE2
_GL_DEPTH_TEST = 0x0B71
_GL_SCISSOR_TEST = 0x0C11
_GL_CONSTANT_ALPHA = 0x8003
_GL_ONE_MINUS_CONSTANT_ALPHA = 0x8004
_GL_TEXTURE_2D = 0x0DE1
_GL_TEXTURE0 = 0x84C0
_GL_FLOAT = 0x1406
_GL_TRIANGLE_STRIP = 0x0005
_GL_COLOR_BUFFER_BIT = 0x4000
_GL_RGBA32F = 0x8814


class BarRenderer:
    """Owns the GL objects. Knows nothing about widgets, so a script can drive
    it into an offscreen framebuffer exactly as the window does."""

    def __init__(self):
        self.gl = None
        self.scene = None
        self.resolve = None
        self.vao = None
        self.quad = None
        self.accum = None
        self.accum_size = (0, 0)
        self.samples = 0
        self._signature = None
        self._locations = {}

    def initialize(self, context) -> Optional[str]:
        """Compile and allocate; returns why it cannot render, or None."""
        from PyQt6.QtOpenGL import (
            QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram,
            QOpenGLVersionFunctionsFactory, QOpenGLVersionProfile,
            QOpenGLVertexArrayObject,
        )
        if context is None or context.isOpenGLES():
            return "no desktop OpenGL context"
        profile = QOpenGLVersionProfile()
        profile.setVersion(4, 1)
        profile.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        self.gl = QOpenGLVersionFunctionsFactory.get(profile, context)
        if self.gl is None:
            return "no OpenGL 4.1 core functions"
        self.gl.initializeOpenGLFunctions()

        fragment = SCENE_FRAGMENT.replace("//@CONSTANTS@", scene_constants())
        for name, source in (("scene", fragment), ("resolve", RESOLVE_FRAGMENT)):
            program = QOpenGLShaderProgram()
            if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, VERTEX):
                return f"{name} vertex shader: {program.log()}"
            if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, source):
                return f"{name} fragment shader: {program.log()}"
            if not program.link():
                return f"{name} link: {program.log()}"
            setattr(self, name, program)

        self.vao = QOpenGLVertexArrayObject()
        self.vao.create()
        corners = b"".join(struct.pack("ff", x, y)
                           for x, y in ((-1.0, -1.0), (1.0, -1.0), (-1.0, 1.0), (1.0, 1.0)))
        self.quad = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        self.quad.create()
        self.quad.bind()
        self.quad.allocate(corners, len(corners))
        self.quad.release()
        return None

    def _ensure_accum(self, width: int, height: int) -> None:
        from PyQt6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat
        if self.accum is not None and self.accum_size == (width, height):
            return
        fmt = QOpenGLFramebufferObjectFormat()
        # 32-bit float: the running average divides by the sample count, and a
        # half float would lose the last samples' contribution to rounding.
        fmt.setInternalTextureFormat(_GL_RGBA32F)
        self.accum = QOpenGLFramebufferObject(width, height, fmt)
        self.accum_size = (width, height)
        self.samples = 0

    def render(self, frame: Frame, width: int, height: int, target_fbo: int) -> None:
        gl = self.gl
        self._ensure_accum(width, height)
        signature = frame.signature()
        if signature != self._signature:
            self._signature = signature
            self.samples = 0

        # Every state both passes rely on, set here rather than assumed: a
        # QOpenGLWidget hands paintGL whatever Qt last left enabled, and a
        # resolve that inherited blending composited the finished image over
        # the widget's undefined framebuffer — noise, and a seam down the
        # quad's diagonal — on exactly the frames the scene pass was skipped.
        gl.glDisable(_GL_BLEND)
        gl.glDisable(_GL_DEPTH_TEST)
        gl.glDisable(_GL_SCISSOR_TEST)

        self.vao.bind()
        if self.samples < MAX_SAMPLES:
            self.samples += 1
            self.accum.bind()
            gl.glViewport(0, 0, width, height)
            # Running average: the n-th sample weighs 1/n against what is there.
            # The first is written, not blended: a new float buffer is whatever
            # memory it was given, and 0 x NaN is still NaN.
            if self.samples > 1:
                gl.glEnable(_GL_BLEND)
                gl.glBlendFunc(_GL_CONSTANT_ALPHA, _GL_ONE_MINUS_CONSTANT_ALPHA)
                gl.glBlendColor(0.0, 0.0, 0.0, 1.0 / self.samples)
            self.scene.bind()
            self._set_scene_uniforms(frame, width, height)
            self._draw_quad(self.scene)
            self.scene.release()
            gl.glDisable(_GL_BLEND)

        gl.glBindFramebuffer(_GL_FRAMEBUFFER, target_fbo)
        gl.glViewport(0, 0, width, height)
        self.resolve.bind()
        gl.glActiveTexture(_GL_TEXTURE0)
        gl.glBindTexture(_GL_TEXTURE_2D, self.accum.texture())
        self.resolve.setUniformValue("u_accum", 0)
        self.resolve.setUniformValue("u_exposure", float(frame.exposure))
        self.resolve.setUniformValue("u_resolution", float(width), float(height))
        self._draw_quad(self.resolve)
        self.resolve.release()
        self.vao.release()

    def _draw_quad(self, program) -> None:
        self.quad.bind()
        corner = program.attributeLocation("a_corner")
        program.enableAttributeArray(corner)
        program.setAttributeBuffer(corner, _GL_FLOAT, 0, 2, 0)
        self.quad.release()
        self.gl.glDrawArrays(_GL_TRIANGLE_STRIP, 0, 4)
        program.disableAttributeArray(corner)

    def _loc(self, name: str) -> int:
        """Uniform locations, looked up once: there are ~150 of them a frame."""
        location = self._locations.get(name)
        if location is None:
            location = self.scene.uniformLocation(name)
            self._locations[name] = location
        return location

    def _set_scene_uniforms(self, frame: Frame, width: int, height: int) -> None:
        from PyQt6.QtGui import QVector3D, QVector4D
        program = self.scene

        def vec3(name: str, v: Sequence[float]) -> None:
            program.setUniformValue(self._loc(name), QVector3D(*[float(c) for c in v]))

        def scalar(name: str, value) -> None:
            program.setUniformValue(self._loc(name), value)

        # Halton (2, 3) over the pixel: sample 1 is the pixel centre, so a view
        # that is moving every frame is still sampled where it always was.
        index = self.samples - 1
        jitter = ((halton(index, 2) - 0.5, halton(index, 3) - 0.5) if index else (0.0, 0.0))
        program.setUniformValue(self._loc("u_resolution"), float(width), float(height))
        program.setUniformValue(self._loc("u_jitter"), float(jitter[0]), float(jitter[1]))
        scalar("u_seed", float(self.samples))

        position, right, up, forward = frame.camera
        vec3("u_cam_pos", position)
        vec3("u_cam_right", right)
        vec3("u_cam_up", up)
        vec3("u_cam_fwd", forward)
        scalar("u_tan_half_fov", float(frame.tan_half_fov))
        scalar("u_haze", float(frame.haze))
        scalar("u_time", float(frame.time))
        vec3("u_bulb_rgb", frame.bulb_rgb)

        scalar("u_light_count", len(frame.emitters))
        for i, emitter in enumerate(frame.emitters):
            vec3(f"u_light_pos[{i}]", emitter.position)
            vec3(f"u_light_dir[{i}]", emitter.direction)
            vec3(f"u_light_rgb[{i}]", emitter.rgb)
            program.setUniformValue(
                self._loc(f"u_light_shape[{i}]"),
                QVector4D(float(emitter.cos_outer), float(emitter.cos_inner),
                          float(emitter.radius), float(emitter.air)))

        scalar("u_bounce_count", len(frame.bounces))
        for i, bounce in enumerate(frame.bounces):
            vec3(f"u_bounce_pos[{i}]", bounce.position)
            vec3(f"u_bounce_nrm[{i}]", bounce.normal)
            vec3(f"u_bounce_rgb[{i}]", bounce.rgb)

        scalar("u_can_count", len(frame.cans))
        for i, can in enumerate(frame.cans):
            for name in ("pos", "x", "y", "z", "size", "mount", "anchor"):
                value = can.position if name == "pos" else getattr(can, name)
                vec3(f"u_can_{name}[{i}]", value)
            scalar(f"u_can_light[{i}]", int(can.light))

        scalar("u_head_count", len(frame.heads))
        for i, head in enumerate(frame.heads):
            for name in ("pos", "x", "y", "z", "size", "mount"):
                value = head.position if name == "pos" else getattr(head, name)
                vec3(f"u_head_{name}[{i}]", value)
            vec3(f"u_yoke_x[{i}]", head.yoke_x)
            vec3(f"u_yoke_z[{i}]", head.yoke_z)
            scalar(f"u_head_light[{i}]", int(head.light))


def build_frame(rig: Rig, camera: OrbitCamera, haze: float, exposure: float,
                show_time: float) -> Frame:
    """What the renderer needs from the rig and the camera, this instant."""
    emitters, cans, heads, bulb = rig.build()
    return Frame(camera=camera.basis(),
                 tan_half_fov=math.tan(math.radians(FOV_VERTICAL_DEG) * 0.5),
                 emitters=emitters, bounces=bounce_lights(emitters),
                 cans=cans, heads=heads, bulb_rgb=bulb,
                 haze=haze, time=show_time, exposure=exposure)


# ==========================================================================
# The view
# ==========================================================================

class BarView(QOpenGLWidget):
    """The window: a camera, a rig, a renderer and a read-out."""

    def __init__(self, rig: Rig):
        super().__init__()
        self.rig = rig
        self.camera = OrbitCamera()
        self.renderer = BarRenderer()
        self.failed = False
        self._drag = None

        self.exposure = 1.0
        self.haze = 0.02
        self.show_time = 0.0
        self._sweep_started: Optional[float] = None

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(720, 460)
        self.hud = QLabel(self)
        self.hud.setStyleSheet(
            "color: #e8ecf4; background: rgba(0,0,0,150); padding: 8px 10px;"
            "font-family: monospace; font-size: 11px; border-radius: 4px;")
        self.hud.move(12, 12)

        self._last_paint: Optional[float] = None
        self.fps = 0.0

        # 60 Hz while anything moves; once a still view has converged the scene
        # pass is skipped and each tick costs one texture blit.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)

    def _tick(self) -> None:
        if self._sweep_started is not None:
            self.show_time = time.monotonic() - self._sweep_started
        self.rig.tick(self.show_time)
        self._update_hud()
        self.update()

    def _update_hud(self) -> None:
        head = self.rig.heads[0]
        converged = self.renderer.samples >= MAX_SAMPLES
        self.hud.setText(
            f"{self.fps:5.1f} fps   samples {self.renderer.samples:3d}/{MAX_SAMPLES}"
            f"{' (still)' if converged else ''}"
            f"   haze {self.haze:.2f}   exposure {self.exposure:.2f}\n"
            f"rig   5 x par-rgbw overhead, 2 on the walls, 2 front of house"
            f" [{self.rig.front[0].kind.value}]"
            f"   2 x {head.key} backlight [{head.kind.value}]"
            f"   sweep {'on' if self.rig.sweeping else 'off'}\n"
            f"drag orbit | wheel dolly | WASD walk | Q/E down/up | [ ] haze | - = exposure"
            f" | space sweep | R photo view")
        self.hud.adjustSize()

    def initializeGL(self) -> None:
        why = self.renderer.initialize(QOpenGLContext.currentContext())
        if why is not None:
            print(f"stage3d_bar: cannot render ({why})", file=sys.stderr)
            self.failed = True
            return
        version = QOpenGLContext.currentContext().format()
        print(f"stage3d_bar: OpenGL {version.majorVersion()}.{version.minorVersion()} ready")

    def paintGL(self) -> None:
        if self.failed:
            return
        started = time.perf_counter()
        if self._last_paint is not None:
            elapsed = started - self._last_paint
            if elapsed > 1e-6:
                self.fps = 1.0 / elapsed if self.fps <= 0.0 else self.fps * 0.9 + 0.1 / elapsed
        self._last_paint = started

        ratio = self.devicePixelRatioF()
        width = max(1, int(self.width() * ratio))
        height = max(1, int(self.height() * ratio))
        frame = build_frame(self.rig, self.camera, self.haze, self.exposure, self.show_time)
        self.renderer.render(frame, width, height, self.defaultFramebufferObject())

    # -- interaction ----------------------------------------------------
    def mousePressEvent(self, event) -> None:
        self._drag = event.position()

    def mouseMoveEvent(self, event) -> None:
        if self._drag is None:
            return
        delta = event.position() - self._drag
        self._drag = event.position()
        self.camera.yaw -= delta.x() * 0.006
        self.camera.pitch = max(-1.3, min(1.3, self.camera.pitch + delta.y() * 0.005))

    def mouseReleaseEvent(self, event) -> None:
        self._drag = None

    def wheelEvent(self, event) -> None:
        factor = 0.9 if event.angleDelta().y() > 0 else 1.1
        self.camera.radius = max(0.6, min(12.0, self.camera.radius * factor))

    def keyPressEvent(self, event) -> None:
        key = event.key()
        step = 0.3
        if key == Qt.Key.Key_Escape:
            self.window().close()
        elif key == Qt.Key.Key_Space:
            self.rig.sweeping = not self.rig.sweeping
            self._sweep_started = (time.monotonic() - self.show_time
                                   if self.rig.sweeping else None)
        elif key == Qt.Key.Key_R:
            self.camera.reset()
        elif key == Qt.Key.Key_BracketLeft:
            self.haze = max(0.0, self.haze - 0.03)
        elif key == Qt.Key.Key_BracketRight:
            self.haze = min(1.0, self.haze + 0.03)
        elif key == Qt.Key.Key_Minus:
            self.exposure = max(0.1, self.exposure - 0.1)
        elif key in (Qt.Key.Key_Equal, Qt.Key.Key_Plus):
            self.exposure = min(6.0, self.exposure + 0.1)
        elif key == Qt.Key.Key_W:
            self.camera.walk(step, 0.0, 0.0)
        elif key == Qt.Key.Key_S:
            self.camera.walk(-step, 0.0, 0.0)
        elif key == Qt.Key.Key_A:
            self.camera.walk(0.0, -step, 0.0)
        elif key == Qt.Key.Key_D:
            self.camera.walk(0.0, step, 0.0)
        elif key == Qt.Key.Key_Q:
            self.camera.walk(0.0, 0.0, -step)
        elif key == Qt.Key.Key_E:
            self.camera.walk(0.0, 0.0, step)
        else:
            super().keyPressEvent(event)


def main() -> int:
    # As stage/gpu/layer.py's configure_surface_format(): force desktop GL or a
    # Wayland/EGL session hands out GL ES, and do NOT ask for a version.
    fmt = QSurfaceFormat()
    fmt.setAlphaBufferSize(8)
    fmt.setDepthBufferSize(0)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication(sys.argv)
    rig = Rig()
    print(f"stage3d_bar: {rig.front[0].key} -> {rig.front[0].kind.value} "
          f"({rig.front[0].half_angle_deg() if rig.front[0].state else '?'}), "
          f"{rig.heads[0].key} ({rig.heads[0].fdef.mode}) -> {rig.heads[0].kind.value}")

    view = BarView(rig)
    view.setWindowTitle("TupperDMX — the bar, in 3D")
    view.resize(1280, 800)
    view.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
