# ABOUTME: What kind of light a fixture throws — no shaft, a soft wash cone or a
# ABOUTME: hard-edged beam — decided from its own optics rather than its name.
"""Beam classification.

The engine used to ask one question — *does this fixture declare a lens angle?*
— and treat "yes" as "it throws a visible shaft". But OFL's
`physical.lens.degreesMinMax` is the *beam angle*: the angular spread of the
output. Every emitting fixture has one, from a 4° beam mover to a 120° flood
panel, so the answer was really "did whoever wrote this document fill that field
in". An ARRI SkyPanel grew a 57.5° cone; a Lupo Superpanel — the same class of
device, with the field left blank — grew none.

So the question is split in two, and both are answered from the fixture's optics:

  * `beam_kind()` — is there a shaft in the air at all, and is its edge defined?
  * `resting_half_angle()` — how wide is it when nothing is driving a zoom?

Conflating those two was the original bug, which is why they stay apart here: a
fixture can be HARD *and* wide (an iris-equipped 120° fixture is hard-edged by
evidence, but its cone is still as wide as its lens says).

Qt-free on purpose, and it may import only from `core`. The kind -> emission
mapping lives in `stage/emission/cone.py`; importing a profile or an emission
from here would close the `registry -> profiles -> registry` cycle that
`registry._CHANNEL_SHAPE_FALLBACKS()` exists to work around.
"""

import math
from enum import Enum
from typing import Optional

from core.fixture_library import FixtureDef, all_capability_types


class BeamKind(str, Enum):
    """What a fixture puts in the air.

    String-valued so a `stage.beam` sidecar hint parses straight into one.
    """

    NONE = "none"   # a surface light: a panel, a flood, a strip. No shaft.
    WASH = "wash"   # a soft-edged cone: a par, a wash head, a zoom wash.
    HARD = "hard"   # a defined edge you can see the end of: a spot, a beam.


# A lens this tight is a beam or a spot whatever else the fixture declares —
# nothing that narrow is sold as a wash.
HARD_MAX_DEGREES = 12.0
# Wider than this and the light lands on a surface without a shaft on the way.
WASH_MAX_DEGREES = 65.0
# Both thresholds sit on real data: 13 fixtures in a 631-strong library declare
# exactly 12.0 and 2 declare exactly 65.0, so `<=` against `<` moves 15 of them.

# `degreesMinMax` is one number for a fixed lens and a *range* for a zoom. Told
# apart by ratio: a spread this many times its own narrow end is a zoom, and its
# tight end then says nothing about how hard the edge is — a wash zoomed in is
# still a wash.
ZOOM_RANGE_RATIO = 2.5

# What a fixture that declares no angle at all falls back to, once its profile
# has said it throws something.
DEFAULT_WASH_DEGREES = 75.0
DEFAULT_HARD_DEGREES = 14.0

# The cone is drawn with `tan(half_angle)`, which passes 1.6e16 at 90°.
MAX_HALF_ANGLE_DEG = 89.0

# Capabilities that mean "there is a gate in this fixture's optical train".
_SHAPING_CAPABILITIES = frozenset({
    "Prism", "PrismRotation", "Iris", "IrisEffect",
    "BladeInsertion", "BladeRotation", "Focus",
})
# Capabilities that set the spread without saying anything about its edge.
_ANGLE_CAPABILITIES = frozenset({"Zoom", "BeamAngle"})

# Wheel slots that put something *in* the beam, so there is a beam to put it in.
_SHAPING_SLOTS = frozenset({"Gobo", "AnimationGoboStart", "AnimationGoboEnd",
                            "Prism", "Iris"})
# The rest of OFL's nine slot types, listed rather than implied: recording which
# slots are deliberately NOT evidence is the whole point. `Frost` softens an edge
# instead of hardening it and rides on wash heads; `Color` is a gel, not a gate.
# `tests/test_stage_optics.py` asserts the two sets cover all nine, so a new OFL
# slot type fails loudly instead of silently landing in the wrong one.
_NOT_SHAPING_SLOTS = frozenset({"Open", "Closed", "Color", "Frost"})


def beam_kind(fdef: FixtureDef,
              fallback: BeamKind = BeamKind.NONE) -> BeamKind:
    """What kind of light this fixture throws.

    `fallback` is what a fixture that declares no optics at all resolves to —
    the profile's answer, since the document has none. A moving head that throws
    nothing is nonsense, so an aiming profile passes WASH or HARD; a lamp body
    passes NONE, because with no evidence either way a panel is the safer guess.
    """
    hinted = _hinted_kind(fdef)
    if hinted is not None:
        return hinted

    if _has_shaping_optics(fdef):
        return BeamKind.HARD

    lens = _lens_range(fdef)
    if lens is not None:
        return _kind_from_lens(*lens)

    # No angle, but something drives one — a zoom channel is only fitted to a
    # fixture with a beam to zoom.
    if all_capability_types(fdef) & _ANGLE_CAPABILITIES:
        return BeamKind.WASH

    return fallback


def resting_half_angle(fdef: FixtureDef,
                       kind: BeamKind) -> Optional[float]:
    """The cone's half-angle before any channel touches it.

    Only what a fixture with no zoom channel is pinned to: `ZoomFeature` runs
    after `BeamParams.reset()` seeds this and overwrites it every frame, so a
    live zoom always wins.

    `None` when the fixture throws no shaft, which keeps the `half_angle_deg is
    None` guard in `BeamCone.apply_beam` working as a second net.
    """
    if kind is BeamKind.NONE:
        return None

    lens = _lens_range(fdef)
    if lens is None:
        degrees = (DEFAULT_HARD_DEGREES if kind is BeamKind.HARD
                   else DEFAULT_WASH_DEGREES)
    else:
        low, high = lens
        if high / low < ZOOM_RANGE_RATIO:
            degrees = low
        else:
            # The geometric mean, not the arithmetic one: a zoom range is a
            # ratio, and averaging it straight drags the resting spread to the
            # wide end — a 19-180° head would sit at 100° rather than 58°.
            degrees = math.sqrt(low * high)

    return min(MAX_HALF_ANGLE_DEG, max(1.0, degrees / 2.0))


# -- the evidence --------------------------------------------------------

def _hinted_kind(fdef: FixtureDef) -> Optional[BeamKind]:
    """A `stage.beam` sidecar hint, when the definition carries one.

    The escape hatch for a fixture whose gate isn't a DMX channel and so can't
    be seen in the data — an ETC Source Four's lens tube, say.
    """
    try:
        return BeamKind(fdef.stage.get("beam"))
    except (ValueError, TypeError):   # absent, misspelled, or not even a string
        return None


def _has_shaping_optics(fdef: FixtureDef) -> bool:
    """Whether this fixture has something that defines the beam's edge.

    Deliberately narrow. `Frost` is not evidence — it softens an edge, and lives
    on wash heads — and neither is a bare `WheelSlot`, which a colour wheel
    declares just as a gobo wheel does. Asking what the wheel's *slots* are is
    the only way to tell those apart; `fixture_library.color_sources()` reads
    them the same way for the same reason.
    """
    if all_capability_types(fdef) & _SHAPING_CAPABILITIES:
        return True
    return any(slot.type in _SHAPING_SLOTS
               for wheel in fdef.wheels.values() for slot in wheel.slots)


def _lens_range(fdef: FixtureDef) -> Optional[tuple]:
    """The fixture's declared spread as an ordered, usable `(low, high)`.

    `None` when it declares none, or when what it declares can't be divided by
    (a Clay Paky Sharpy states `[0, 3.8]`, so the narrow end is taken from the
    wide one rather than thrown away).
    """
    lens = fdef.physical.lens_degrees
    if lens is None:
        return None
    low, high = min(lens), max(lens)
    if low <= 0:
        low = high
    return (low, high) if low > 0 else None


def _kind_from_lens(low: float, high: float) -> BeamKind:
    # Narrow even wide open: a 2-5° head is a beam, whatever its zoom ratio.
    if high <= HARD_MAX_DEGREES:
        return BeamKind.HARD

    if high / low < ZOOM_RANGE_RATIO:
        # A fixed lens, so its one angle says everything.
        if low <= HARD_MAX_DEGREES:
            return BeamKind.HARD
        return BeamKind.NONE if low > WASH_MAX_DEGREES else BeamKind.WASH

    # A zoom range. The tight end is the zoom working, not a gate, so it is no
    # evidence of a hard edge — only of how wide the fixture can get.
    return BeamKind.NONE if low > WASH_MAX_DEGREES else BeamKind.WASH
