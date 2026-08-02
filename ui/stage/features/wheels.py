# ABOUTME: Features driven by a fixture's wheels — colour gels, gobos, and the
# ABOUTME: shutter a "Closed" slot amounts to.
"""Wheels.

A wheel is a disc the fixture turns in front of its lamp. Which slot is in the
beam comes from a `WheelSlot` capability; what that slot *is* comes from the
document's `wheels` block, and OFL tells the two kinds apart only by what the
slots contain — so these features look at the slots, not at the wheel's name.

That matters for the common case of a fixture with two wheels on two channels:
the one whose slots are colours tints the beam, the one whose slots are gobos
puts a pattern in it, and neither needs to know which channel it came from.
"""

from typing import Optional, Set, Tuple

from core import ofl
from ui.stage import wheelart
from ui.stage.features.base import Feature, register_feature
from ui.stage.frame import Frame
from ui.stage.geometry import Spinner

_WHEEL_TYPES = frozenset({"WheelSlot", "WheelShake", "WheelSlotRotation",
                          "WheelRotation"})


@register_feature
class ColorWheelFeature(Feature):
    """A gel in the beam. It filters what the lamp already makes.

    Multiplicative, not a replacement: a colour wheel in front of a lamp that is
    already mixing colour gives you both, which is exactly what happens on the
    real fixture and is the reason a red gel over a green lamp goes black.
    """

    types = _WHEEL_TYPES
    order = 25

    def apply(self, frame: Frame) -> None:
        gel = _blended_gel(frame.state)
        if gel is None:
            return
        frame.beam.rgb = tuple(min(255, int(c * g / 255))
                               for c, g in zip(frame.beam.rgb, gel))


@register_feature
class GoboFeature(Feature):
    """A pattern cut into the beam, and the shutter a closed slot amounts to.

    The slot's own rotation spins the pattern; a wheel mid-change carries both
    slots, so the beam can show one dissolving into the next rather than
    snapping between them.
    """

    types = _WHEEL_TYPES
    order = 60
    animates = True

    def __init__(self, body):
        super().__init__(body)
        self._spinner = Spinner()

    def apply(self, frame: Frame) -> None:
        wheel = _gobo_wheel(frame.state)
        if wheel is None:
            return

        if wheelart.slot_blocks_light(wheel.slot):
            # A "Closed" slot is a physical blackout — the disc has no hole
            # there — whatever the dimmer is doing.
            frame.beam.level = 0.0
            return

        frame.beam.gobo = wheel.slot
        frame.beam.gobo_next = wheel.next_slot
        frame.beam.gobo_blend = wheel.blend
        if wheel.slot_angle_deg is not None:
            frame.beam.gobo_spin_deg = wheel.slot_angle_deg
        else:
            rate = wheel.slot_spin_hz or wheel.spin_hz
            frame.beam.gobo_spin_deg = self._spinner.advance(rate, frame.t)


def _blended_gel(state) -> Optional[Tuple[int, int, int]]:
    """The colour every colour wheel on the fixture puts in the beam, combined.

    Mid-change the two slots are mixed by how far the wheel has turned, so a
    colour change reads as a crossfade rather than a jump.
    """
    gel: Optional[Tuple[int, int, int]] = None
    for wheel in state.wheels.values():
        colour = wheelart.slot_rgb(wheel.slot)
        following = wheelart.slot_rgb(wheel.next_slot)
        if colour is None and following is None:
            continue
        if colour is None:
            colour = (255, 255, 255)
        if following is not None and wheel.blend:
            colour = tuple(int(a + (b - a) * wheel.blend)
                           for a, b in zip(colour, following))
        gel = colour if gel is None else tuple(
            min(255, int(g * c / 255)) for g, c in zip(gel, colour))
    return gel


def _gobo_wheel(state):
    """The first wheel with a pattern (or a closed slot) currently in the beam."""
    for wheel in state.wheels.values():
        if wheelart.slot_blocks_light(wheel.slot):
            return wheel
        if wheelart.pattern_for(wheel.slot) or wheelart.pattern_for(wheel.next_slot):
            return wheel
    return None
