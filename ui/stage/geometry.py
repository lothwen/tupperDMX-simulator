# ABOUTME: Pan/tilt onto a flat front-elevation stage, plus the two integrators
# ABOUTME: (spin and slew) every moving thing on the stage shares.
"""Aiming a light in a picture that has no depth.

The stage is a photograph of a stage seen from the audience: scene units are
image pixels and there is no floor, no depth and no camera. A moving head still
has to *look* like it is pointing somewhere, so pan and tilt are mapped into the
plane of that picture rather than into a world that doesn't exist:

  * **pan** is the only axis that can carry a beam across the image, so it
    becomes the cone's swing left and right;
  * **tilt** rotates the cone up and down and, past the vertical, turns it
    toward the viewer — which reads as the beam getting shorter and blooming
    rather than sweeping anywhere. That is `foreshorten`.

The one hard requirement: a fixture with no pan or tilt channel must come out
pointing straight down at full length, because that is what every cone did
before fixtures could aim at all.
"""

import math
from typing import Optional, Tuple

from core.fixture_library import FixtureDef
from core.fixture_state import MotionState
from core import ofl
from ui.stage.frame import STRAIGHT_DOWN_DEG

# Most movers are 540°/270°; a fixture that declares its own ranges overrides
# these, and one that declares nothing at least behaves like a normal head.
DEFAULT_PAN_RANGE = (0.0, 540.0)
DEFAULT_TILT_RANGE = (0.0, 270.0)

# Real pan is up to a turn and a half. Mapping that onto a flat picture 1:1
# would swing the beam back through the fixture's own body, so it is scaled and
# clamped to the part of the swing an audience would see as "across the stage".
PAN_TO_SCREEN = 0.5
MAX_SWING_DEG = 80.0
MAX_TILT_DEG = 80.0

# A beam pointed at the audience is never drawn as nothing; it becomes a short,
# bright bloom instead.
MIN_FORESHORTEN = 0.15


class Aim:
    """Maps a fixture's declared pan/tilt onto the flat stage image.

    Built once per body: the ranges come from the fixture's own capabilities and
    can't change, so the per-frame work is two multiplications.
    """

    def __init__(self, fdef: FixtureDef):
        self.pan_range = _declared_range(fdef, "Pan") or DEFAULT_PAN_RANGE
        self.tilt_range = _declared_range(fdef, "Tilt") or DEFAULT_TILT_RANGE
        self.pan_mid = sum(self.pan_range) / 2.0
        self.tilt_mid = sum(self.tilt_range) / 2.0

    def solve(self, motion: MotionState,
              offset: Tuple[float, float] = (0.0, 0.0)) -> Tuple[float, float]:
        """`(axis_deg, foreshorten)` for a motion state.

        `offset` is a BeamPosition nudge in degrees, applied on top.
        """
        axis = STRAIGHT_DOWN_DEG
        foreshorten = 1.0

        swing = offset[0]
        if motion.pan_deg is not None:
            swing += (motion.pan_deg - self.pan_mid) * PAN_TO_SCREEN
        axis += _clamp(swing, MAX_SWING_DEG)

        lift = offset[1]
        if motion.tilt_deg is not None:
            lift += motion.tilt_deg - self.tilt_mid
        if lift:
            axis -= _clamp(lift, MAX_TILT_DEG)
            foreshorten = max(MIN_FORESHORTEN, abs(math.cos(math.radians(lift))))
        return axis, foreshorten

    def pan_swing(self, motion: MotionState) -> float:
        """How far the yoke has turned on screen, in degrees. 0 is facing front."""
        if motion.pan_deg is None:
            return 0.0
        return _clamp((motion.pan_deg - self.pan_mid) * PAN_TO_SCREEN, MAX_SWING_DEG)

    def tilt_offset(self, motion: MotionState) -> float:
        """How far the head has tipped from centre, in degrees."""
        if motion.tilt_deg is None:
            return 0.0
        return _clamp(motion.tilt_deg - self.tilt_mid, MAX_TILT_DEG)


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


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


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
