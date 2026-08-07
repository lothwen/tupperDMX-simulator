# ABOUTME: The features every fixture has — how bright it is, what colour, and
# ABOUTME: whether its shutter is chopping the light up.
"""Level, colour and shutter.

The three layers that apply to essentially any fixture. Dimmer and colour claim
unconditionally: `resolve_state` has already folded intensity, colour mixing,
colour presets and colour temperature into one level and one RGB, and a fixture
with none of those resolves to "full, white", which is exactly right for a
fixture whose only channel is a strobe.
"""

import math
import random
from typing import Set

from stage.features.base import Feature, register_feature
from stage.frame import Frame


@register_feature
class DimmerFeature(Feature):
    """How bright the fixture is, before anything chops it up."""

    types = frozenset({"Intensity"})
    order = 10

    @classmethod
    def claims(cls, fdef, present: Set[str], body) -> bool:
        # Always: a fixture with no dimmer emits at full, and something has to
        # say so.
        return True

    def apply(self, frame: Frame) -> None:
        frame.beam.level = self.body.intensity_factor()


@register_feature
class ColorFeature(Feature):
    """What colour the fixture is making."""

    types = frozenset({"ColorIntensity", "ColorPreset", "ColorTemperature"})
    order = 20

    @classmethod
    def claims(cls, fdef, present: Set[str], body) -> bool:
        # Always: a fixture with no colour channels emits white, and the beam
        # still needs to be told that.
        return True

    def apply(self, frame: Frame) -> None:
        frame.beam.rgb = frame.state.color.rgb


@register_feature
class ShutterFeature(Feature):
    """The shutter, across all ten of OFL's shutter effects.

    Each is a waveform over the flash phase rather than an on/off flag, so a
    pulse breathes, a ramp fades and lightning is erratic — the things that make
    them recognisably different on stage. The phase comes from the shared clock,
    so every strobing fixture in the rig agrees.
    """

    types = frozenset({"ShutterStrobe", "StrobeSpeed", "StrobeDuration"})
    order = 30
    animates = True

    # Nothing useful is drawn below or above these; a 0.2 Hz strobe reads as
    # broken and a 40 Hz one as a solid dimmed lamp.
    MIN_HZ, MAX_HZ = 0.5, 25.0
    DEFAULT_HZ = 8.0

    def __init__(self, body):
        super().__init__(body)
        # Per-body, so two lightning fixtures don't flash in lockstep.
        self._seed = random.random() * 1000.0

    def apply(self, frame: Frame) -> None:
        gate = self._gate(frame.state.strobe, frame.t)
        frame.beam.gate = gate
        frame.beam.level *= gate

    def _gate(self, strobe, now: float) -> float:
        effect = strobe.effect
        if effect in ("Open", ""):
            return 1.0
        if effect == "Closed":
            return 0.0

        hz = strobe.hz if strobe.hz else self.DEFAULT_HZ
        hz = max(self.MIN_HZ, min(self.MAX_HZ, hz))
        phase = ((now + self._seed) * hz) % 1.0
        if strobe.random:
            # Same average rate, but the flashes don't land on a grid.
            phase = (phase + _jitter(self._seed, int((now + self._seed) * hz))) % 1.0

        if effect == "Strobe":
            return 1.0 if phase < 0.5 else 0.0
        if effect == "Pulse":
            return math.sin(math.pi * phase) ** 2
        if effect == "RampUp":
            return phase
        if effect == "RampDown":
            return 1.0 - phase
        if effect == "RampUpDown":
            return 1.0 - abs(2.0 * phase - 1.0)
        if effect == "Lightning":
            # Irregular hard hits with dark between them.
            return 1.0 if _jitter(self._seed, int((now + self._seed) * hz)) < 0.35 else 0.0
        if effect == "Spikes":
            # A brief stab each cycle rather than a square half-on.
            return 1.0 if phase < 0.12 else 0.0
        if effect == "Burst":
            # Three quick flashes, then a rest — the "machine gun" look.
            return 1.0 if (phase < 0.45 and int(phase * 20) % 2 == 0) else 0.0
        # An effect OFL has and we haven't: leave the light alone rather than
        # blacking the fixture out.
        return 1.0


def _jitter(seed: float, step: int) -> float:
    """A stable pseudo-random 0..1 for one flash of one fixture.

    Deterministic in `step`, so a frame drawn twice looks the same — important
    for a test that ticks the clock by hand.
    """
    value = math.sin(seed + step * 12.9898) * 43758.5453
    return value - math.floor(value)
