# ABOUTME: The per-frame contract between capability features and the emission
# ABOUTME: that draws them — features mutate BeamParams, emission reads it.
"""What one frame of light looks like, before anything is painted.

The engine resolves a fixture in two stages: its OFL *category* picks the body
and the kind of light it makes, then the *capabilities* it declares each modify
that light. `BeamParams` is where the second stage writes and the first stage
reads — a gobo feature doesn't know whether it is shaping a moving head's beam
or a scanner's, and the emission doesn't know which features ran.

Mutable, and reused frame to frame: at 20–100 Hz across a rig this is the one
object that would otherwise be reallocated constantly.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

from core import ofl
from core.fixture_state import FixtureState

# How far a beam reaches into the stage image, in scene units. Kept here rather
# than in the cone that draws it because the scene preview reserves room for it.
BEAM_LENGTH_PX = 1220

# 90 degrees is straight down the image — where every cone pointed before
# fixtures could aim, so a fixture with no pan/tilt draws exactly as it did.
STRAIGHT_DOWN_DEG = 90.0

#: How much of its length a beam keeps when it is aimed straight at the viewer.
#: A shaft coming at you is short on the picture and long in the room, and this
#: is the split: the drawn length is `length_px * (FLOOR + (1 - FLOOR) *
#: foreshorten)`. The fog march inverts it to recover the throw the fixture
#: actually has, so the two views of one beam agree about where it ends.
FORESHORTEN_FLOOR = 0.15

_EMPTY: Mapping = {}


@dataclass
class BeamParams:
    """The light one body is emitting this frame, in renderer-neutral terms."""

    rgb: Tuple[int, int, int] = (255, 255, 255)
    level: float = 0.0                  # 0..1 after dimmer, shutter and effects
    # The shutter's contribution on its own, so a per-cell emission (a matrix,
    # a blinder) can flash its cells without re-deriving it from `level`.
    gate: float = 1.0
    visible: bool = False

    # Shape. A half-angle of None means "this fixture declares no optics", which
    # is how a bare LED bar ends up with no cone at all.
    half_angle_deg: Optional[float] = None
    length_px: float = BEAM_LENGTH_PX
    aperture: float = 1.0               # iris: 1.0 wide open
    softness: float = 0.0               # frost: 0.0 hard-edged

    # Aim, in the plane of the stage image
    axis_deg: float = STRAIGHT_DOWN_DEG
    foreshorten: float = 1.0            # 1.0 across the picture, →0 toward the viewer

    # What is in the beam
    gobo: Optional[ofl.WheelSlot] = None
    gobo_next: Optional[ofl.WheelSlot] = None
    gobo_blend: float = 0.0
    gobo_spin_deg: float = 0.0
    copies: Tuple[float, ...] = (0.0,)  # prism: one angular offset per facet
    blades: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)

    def reset(self, half_angle_deg: Optional[float] = None) -> None:
        """Back to "nothing is shaping this beam", before the features run.

        `half_angle_deg` is the fixture's own resting spread, from its declared
        lens — a zoom channel overrides it, and a fixture with neither has none.
        """
        self.rgb = (255, 255, 255)
        self.level = 0.0
        self.gate = 1.0
        self.visible = False
        self.half_angle_deg = half_angle_deg
        self.length_px = BEAM_LENGTH_PX
        self.aperture = 1.0
        self.softness = 0.0
        self.axis_deg = STRAIGHT_DOWN_DEG
        self.foreshorten = 1.0
        self.gobo = None
        self.gobo_next = None
        self.gobo_blend = 0.0
        self.gobo_spin_deg = 0.0
        self.copies = (0.0,)
        self.blades = _EMPTY


@dataclass
class Frame:
    """One body's frame: what it resolved to, plus the world it is drawn into."""
    state: FixtureState
    body: Any
    haze: float = 0.0       # stage-wide, 0..1 — how visible any beam is
    t: float = 0.0          # the shared clock's `now()`
    beam: BeamParams = field(default_factory=BeamParams)
