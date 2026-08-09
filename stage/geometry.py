# ABOUTME: Pan/tilt onto a flat front-elevation stage, plus the two integrators
# ABOUTME: (spin and slew) every moving thing on the stage shares.
"""Aiming a light in a picture that has no depth.

The stage is a photograph of a stage seen from the audience: scene units are
image pixels and there is no floor, no depth and no camera. But a moving head
does point into a room, so it is aimed in one — a unit vector built from its
pan and tilt — and only then flattened into the picture. Pan and tilt are not
two independent screen quantities, which is the whole of what this module gets
right and what reading them as "swing" and "lift" got wrong.

**Home is straight down** — for a flown fixture. Half of a fixture's declared
travel (DMX 127/128, 135° on a 270° tilt) is where a head rests, so tilt is
measured from there and the beam leaves the lens pointing at the floor. A
fixture standing on the deck is that same fixture turned over: its home points
at the ceiling instead, which is half a turn of everything it draws in the
picture and no change at all to the part of it aimed at the audience. Which way
a fixture is rigged is the patch's to say (`FixtureInstance.hanging`), because
nothing in a DMX document knows.

**Tilt swings the beam; pan chooses the plane it swings in.** From home the
head tips by the tilt angle inside one vertical plane, and panning turns that
plane about the fixture's own vertical axis. So:

  * at pan home the plane faces the audience: tilting keeps the beam on the
    picture's vertical axis and walks it *toward the viewer*, which reads as
    the beam getting shorter and blooming — longest at 50%, where it points
    straight down. That is `foreshorten`;
  * a quarter turn of pan away, the plane lies in the picture: the same tilt
    now sweeps the beam across the stage at full length;
  * at tilt home there is nothing to swing. A head pointing straight down does
    not move its beam when you pan it, and drawing one that did is why panning
    used to carry a beam ±80° across a picture the fixture had never left.

What comes out is `(axis_deg, foreshorten)`: the direction of the beam **in**
the picture, and how much of the unit vector is left there once the part aimed
at the viewer is taken away. The two together *are* that unit vector — the fog
march reconstructs it as `(cos·f, sin·f, -sqrt(1-f²))` — so the flat drawing
and the volumetric one are two views of one beam and cannot disagree.

The one hard requirement, unchanged: a fixture with no pan or tilt channel must
come out pointing straight down at full length, because that is what every cone
did before fixtures could aim at all.
"""

import math
from typing import Optional, Tuple

from core.fixture_library import FixtureDef
from core.fixture_state import MotionState
from core import ofl
from stage.frame import STRAIGHT_DOWN_DEG

# Most movers are 540°/270°; a fixture that declares its own ranges overrides
# these, and one that declares nothing at least behaves like a normal head.
DEFAULT_PAN_RANGE = (0.0, 540.0)
DEFAULT_TILT_RANGE = (0.0, 270.0)

# A beam pointed at the audience is never drawn as nothing; it becomes a short,
# bright bloom instead.
MIN_FORESHORTEN = 0.15


class Aim:
    """Where a fixture is pointing, and what that looks like in the picture.

    Built once per body: the ranges come from the fixture's own capabilities and
    can't change, so the per-frame work is two sines and a cosine.

    It also *remembers* the last direction it was pointed, because the housing
    is drawn after the beam and has to agree with it — a yoke that has turned
    while its beam has not is the same lie in the other direction.
    """

    def __init__(self, fdef: FixtureDef, hanging: bool = True):
        self.pan_range = _declared_range(fdef, "Pan") or DEFAULT_PAN_RANGE
        self.tilt_range = _declared_range(fdef, "Tilt") or DEFAULT_TILT_RANGE
        self.pan_mid = sum(self.pan_range) / 2.0
        self.tilt_mid = sum(self.tilt_range) / 2.0
        self.hanging = hanging
        self._pan = 0.0
        self._tilt = 0.0
        self._swung = 0.0

    def offsets(self, motion: MotionState,
                offset: Tuple[float, float] = (0.0, 0.0)) -> Tuple[float, float]:
        """`(pan, tilt)` in degrees from home, before anything is projected.

        Kept apart from `point` so the mechanics can be slewed as mechanics: a
        head takes time to reach a position, and it is the yoke that is slow,
        not the picture of it.

        `offset` is a BeamPosition nudge in degrees, applied on top.
        """
        pan, tilt = offset
        if motion.pan_deg is not None:
            pan += motion.pan_deg - self.pan_mid
        if motion.tilt_deg is not None:
            tilt += motion.tilt_deg - self.tilt_mid
        return pan, tilt

    def point(self, pan_deg: float, tilt_deg: float) -> Tuple[float, float]:
        """Aim the fixture there; answer with `(axis_deg, foreshorten)`."""
        self._pan, self._tilt = pan_deg, tilt_deg
        pan = math.radians(pan_deg)
        tilt = math.radians(tilt_deg)

        # The unit vector, in the picture's own axes: x across, y down, and a
        # third component `sin(tilt) * cos(pan)` out of the page that nothing
        # draws — `foreshorten` is what is left after it.
        across = math.sin(tilt) * math.sin(pan)
        down = math.cos(tilt)

        flat = math.hypot(across, down)
        # Aimed exactly down the lens at the viewer there is no direction left
        # in the picture to point. What is drawn there is a round bloom, so any
        # answer looks the same; straight ahead is the one that cannot surprise.
        self._swung = math.degrees(math.atan2(across, down)) if flat > 1e-9 else 0.0

        # A fixture standing on the deck is the same fixture turned over, so
        # everything it does in the picture turns with it — half a turn, which
        # leaves the part aimed at the audience alone and so leaves
        # `foreshorten` (and the unit vector the fog march rebuilds) intact.
        axis = STRAIGHT_DOWN_DEG - self._swung
        if not self.hanging:
            axis += 180.0
        return axis, max(MIN_FORESHORTEN, flat)

    def solve(self, motion: MotionState,
              offset: Tuple[float, float] = (0.0, 0.0)) -> Tuple[float, float]:
        """`(axis_deg, foreshorten)` for a motion state, in one step."""
        return self.point(*self.offsets(motion, offset))

    def turn(self) -> float:
        """How far the beam has turned off the fixture's own straight-ahead, as
        degrees of item rotation — what the head or mirror throwing it turns by.

        Housing-local on purpose. The chassis of a flown fixture is *already*
        drawn turned over, so a part that took `beam.axis_deg` instead would
        count the rigging twice and point its lens back into its own base.
        """
        return -self._swung

    def pan_offset(self) -> float:
        """How far the yoke has turned from home, in degrees. 0 faces front."""
        return self._pan

    def tilt_offset(self) -> float:
        """How far the head has tipped from home, in degrees. 0 is straight down."""
        return self._tilt


def _declared_range(fdef: FixtureDef, cap_type: str) -> Optional[Tuple[float, float]]:
    """The widest angle range the fixture's capabilities of `cap_type` declare."""
    angles = []
    for spec in fdef.channels:
        if spec.fine_of >= 0:
            continue
        for cap in spec.capabilities:
            if cap.type != cap_type:
                continue
            for key in ("angleStart", "angleEnd", "angle"):
                angle = ofl.parse_entity(cap.get(key), "rotationAngle")
                if angle is not None:
                    angles.append(angle)
    return (min(angles), max(angles)) if len(angles) >= 2 else None


class Spinner:
    """Integrates a rate into an angle.

    Everything on a stage that turns — a gobo, a prism, a colour wheel, a fan, a
    head in continuous rotation — is declared as a signed speed rather than a
    position, so all of them need the same "where has it got to by now".
    """

    def __init__(self):
        self._angle = 0.0
        self._last: Optional[float] = None

    def advance(self, hz: float, now: float) -> float:
        if self._last is None:
            self._last = now
        elapsed = max(0.0, min(0.25, now - self._last))
        self._last = now
        self._angle = (self._angle + 360.0 * hz * elapsed) % 360.0
        return self._angle

    @property
    def angle(self) -> float:
        return self._angle


class Slew:
    """Chases a target at a limited rate — a fixture's pan/tilt speed setting.

    Also does something the fixtures don't: it smooths the 20 Hz DMX feed into
    continuous motion, so a head crossing the stage glides instead of stepping.
    """

    #  How long a move takes when the fixture says nothing about its speed. Short
    #  enough to feel immediate, long enough to hide the frame rate.
    DEFAULT_TAU = 0.08

    def __init__(self):
        self._value: Optional[float] = None
        self._last: Optional[float] = None

    def advance(self, target: float, now: float,
                tau: Optional[float] = None) -> float:
        if self._value is None or self._last is None:
            self._value = target
            self._last = now
            return target
        elapsed = max(0.0, min(0.25, now - self._last))
        self._last = now
        span = max(1e-3, tau if tau else self.DEFAULT_TAU)
        self._value += (target - self._value) * min(1.0, elapsed / span)
        return self._value
