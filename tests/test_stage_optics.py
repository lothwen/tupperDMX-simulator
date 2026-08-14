"""
Characterization test: what kind of light a fixture throws.

The engine used to treat "this document declares a lens angle" as "this fixture
throws a visible shaft". But OFL's `physical.lens.degreesMinMax` is the *beam
angle*, which every emitting fixture has — so a 120° flood panel grew a cone and
an identical panel whose document left the field blank did not. This covers the
replacement: a three-valued classification made from the fixture's own optics.

Every archetype below is a real shape found in the OFL catalogue, and several are
traps that a more obvious rule gets wrong — see the comments in the table.

Run with:
    uv run python tests/test_stage_optics.py
"""

import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ofl_factory
from stage.emission.cone import BeamCone, HardBeam, LampGlow, WashLamp
from stage.frame import BEAM_LENGTH_PX, BeamParams
from stage.optics import (
    _NOT_SHAPING_SLOTS, _SHAPING_CAPABILITIES, _SHAPING_SLOTS,
    BeamKind, DEFAULT_HARD_DEGREES, DEFAULT_WASH_DEGREES, MAX_HALF_ANGLE_DEG,
    WASH_MAX_DEGREES, beam_kind, resting_half_angle,
)
from stage.registry import renderer_for

SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "data", "ofl_schemas")

# Capability types that look like beam evidence but deliberately are not. Frost
# softens an edge rather than hardening it, and a bare wheel channel is whatever
# its slots are — a colour wheel declares `WheelSlot` exactly as a gobo wheel
# does. Getting these wrong is what made 32 wash movers draw as spots.
NOT_EVIDENCE = frozenset({"Frost", "FrostEffect", "WheelSlot", "WheelRotation",
                          "WheelSlotRotation", "WheelShake"})

MOVER_CHANNELS = {"p": ofl_factory.channel("Pan", angleStart="0deg",
                                           angleEnd="540deg"),
                  "t": ofl_factory.channel("Tilt", angleStart="0deg",
                                           angleEnd="270deg")}
GOBO_WHEEL = {"Gobo Wheel": {"slots": [{"type": "Open"},
                                       {"type": "Gobo",
                                        "resource": "gobos/dots"}]}}
COLOR_WHEEL = {"Color Wheel": {"slots": [{"type": "Open"},
                                         {"type": "Color", "name": "Red",
                                          "colors": ["#ff0000"]}]}}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _lens(low, high=None):
    return {"lens": {"degreesMinMax": [low, high if high is not None else low]}}


# label -> (fdef kwargs, expected kind, expected emission class)
#
# The emission column pins the profile wiring too: a kind is only useful if the
# body it lands on actually draws that kind of light.
ARCHETYPES = {
    # -- the headline case: a wide fixture makes no shaft -------------------
    "115 deg LED panel":
        (dict(categories=("Dimmer", "Color Changer"), physical=_lens(115)),
         BeamKind.NONE, LampGlow),
    "75 deg wash bar":
        (dict(categories=("Color Changer",), physical=_lens(75)),
         BeamKind.NONE, LampGlow),
    "65 deg par (exactly on the line, still a wash)":
        (dict(categories=("Color Changer",), physical=_lens(65)),
         BeamKind.WASH, WashLamp),

    # -- ordinary pars -----------------------------------------------------
    "25 deg par":
        (dict(categories=("Color Changer",), physical=_lens(25)),
         BeamKind.WASH, WashLamp),

    # -- narrow means hard, even with no wheels ----------------------------
    "4 deg beam mover, no wheels":
        (dict(categories=("Moving Head",), channels=MOVER_CHANNELS,
              physical=_lens(4)),
         BeamKind.HARD, HardBeam),
    # TRAP: 2-5 is a ratio of 2.5, so a pure zoom-ratio rule calls it a wash.
    # It is a beam mover wide open (american-dj/Inno Pocket Fusion).
    "2-5 deg narrow zoom, no wheels":
        (dict(categories=("Moving Head",), channels=MOVER_CHANNELS,
              physical=_lens(2, 5)),
         BeamKind.HARD, HardBeam),
    # TRAP: clay-paky/Sharpy really does declare a zero minimum.
    "0-3.8 deg lens (zero minimum)":
        (dict(categories=("Moving Head",), channels=MOVER_CHANNELS,
              physical=_lens(0, 3.8)),
         BeamKind.HARD, HardBeam),

    # -- a zoom range's tight end is the zoom, not a gate -------------------
    # TRAP: min(lens) <= 20 is what made these draw as spots.
    "8-30 deg zoom wash mover":
        (dict(categories=("Moving Head",), channels=MOVER_CHANNELS,
              physical=_lens(8, 30)),
         BeamKind.WASH, WashLamp),
    "7-58 deg zoom wash mover":
        (dict(categories=("Moving Head",), channels=MOVER_CHANNELS,
              physical=_lens(7, 58)),
         BeamKind.WASH, WashLamp),

    # -- what counts as evidence, and what does not ------------------------
    "gobo wheel makes a beam":
        (dict(categories=("Moving Head",), wheels=GOBO_WHEEL,
              channels=dict(MOVER_CHANNELS,
                            g=ofl_factory.channel("WheelSlot",
                                                  wheel="Gobo Wheel",
                                                  slotNumber=1))),
         BeamKind.HARD, HardBeam),
    # These two carry a lens on purpose. The question they ask is whether the
    # capability is read as a hard edge, so they have to reach the evidence
    # rules with a spread of their own — left bare they would fall through to
    # the profile default instead, which answers a different question (and is
    # pinned by `test_a_defaulted_spread_is_classified_like_a_declared_one`).
    # It is also what the real fixtures look like: 83 of the 102 shipped
    # definitions declaring Frost or a wheel declare a lens too.
    #
    # TRAP: a colour wheel declares WheelSlot exactly as a gobo wheel does.
    "colour wheel alone does not":
        (dict(categories=("Moving Head",), wheels=COLOR_WHEEL,
              physical=_lens(25),
              channels=dict(MOVER_CHANNELS,
                            c=ofl_factory.channel("WheelSlot",
                                                  wheel="Color Wheel",
                                                  slotNumber=1))),
         BeamKind.WASH, WashLamp),
    # TRAP: frost softens a beam. It rides on wash heads, and counting it is
    # what made dts/XR1200 WASH a spot.
    "frost alone does not":
        (dict(categories=("Moving Head",), physical=_lens(25),
              channels=dict(MOVER_CHANNELS,
                            f=ofl_factory.channel("Frost", frostIntensityStart="0%",
                                                  frostIntensityEnd="100%"))),
         BeamKind.WASH, WashLamp),
    "an iris does, at any width":
        (dict(categories=("Color Changer",), physical=_lens(120),
              channels={"i": ofl_factory.channel("Iris", openPercentStart="0%",
                                                 openPercentEnd="100%")}),
         BeamKind.HARD, HardBeam),

    # -- a zoom channel with no declared lens ------------------------------
    "zoom channel, no lens block":
        (dict(categories=("Color Changer",),
              channels={"z": ofl_factory.channel("Zoom", angleStart="8deg",
                                                 angleEnd="40deg")}),
         BeamKind.WASH, WashLamp),

    # -- no optics declared at all: the profile decides ---------------------
    "no data on a par":
        (dict(categories=("Color Changer",), channels=ofl_factory.RGB_CHANNELS),
         BeamKind.NONE, LampGlow),
    # TRAP: the profile says WASH, and the answer is still no shaft. A fallback
    # spread meets the same `WASH_MAX_DEGREES` ceiling a declared one does, and
    # `DEFAULT_WASH_DEGREES` sits above it on purpose — a head with no lens, no
    # zoom and no gate is a soft source, and giving it the widest cone on the
    # stage is the same mistake as reading a filled-in field as "throws a shaft".
    "no data on a moving head":
        (dict(categories=("Moving Head",), channels=MOVER_CHANNELS),
         BeamKind.NONE, LampGlow),
    # ...but only the wash side. A scanner is hard-edged because of its mirror,
    # so `DEFAULT_HARD_DEGREES` is never put through that rule.
    "no data on a scanner":
        (dict(categories=("Scanner",), channels=MOVER_CHANNELS),
         BeamKind.HARD, HardBeam),

    # -- the sidecar escape hatch, both directions -------------------------
    "hint forces a beam off a 4 deg fixture":
        (dict(categories=("Color Changer",), physical=_lens(4),
              sidecar={"tdmx": 1, "modes": {"*": {"stage": {"beam": "none"}}}}),
         BeamKind.NONE, LampGlow),
    "hint forces a beam onto a 60 deg fixture":
        (dict(categories=("Color Changer",), physical=_lens(60),
              sidecar={"tdmx": 1, "modes": {"*": {"stage": {"beam": "hard"}}}}),
         BeamKind.HARD, HardBeam),
}


def test_every_archetype_resolves_to_the_right_kind_of_light():
    print("test_every_archetype_resolves_to_the_right_kind_of_light")
    for label, (kwargs, expected, emission) in ARCHETYPES.items():
        fdef = ofl_factory.fdef(**kwargs)
        renderer = renderer_for(fdef)
        kind = renderer.beam_kind(fdef)
        _assert(kind is expected,
                f"{label} -> {expected.name} (got {kind.name})")
        _assert(renderer.emission_for(fdef) is emission,
                f"{label} -> {emission.__name__}")


def test_a_fixture_with_a_gobo_wheel_always_lands_on_hardbeam():
    print("test_a_fixture_with_a_gobo_wheel_always_lands_on_hardbeam")
    # Only HardBeam owns a GoboPlane, so a gobo on any other emission is a
    # pattern nothing draws. The classifier is what guarantees it, on a static
    # body as much as on a mover.
    for categories in (("Moving Head",), ("Color Changer",), ("Dimmer",),
                       ("Scanner",), ("Other",)):
        fdef = ofl_factory.fdef(
            categories=categories, wheels=GOBO_WHEEL,
            channels={"g": ofl_factory.channel("WheelSlot", wheel="Gobo Wheel",
                                               slotNumber=1),
                      "d": ofl_factory.channel("Intensity")})
        renderer = renderer_for(fdef)
        _assert(renderer.emission_for(fdef) is HardBeam,
                f"{categories[0]} + a gobo wheel -> HardBeam")


def test_the_slot_table_covers_every_ofl_wheel_slot_type():
    print("test_the_slot_table_covers_every_ofl_wheel_slot_type")
    declared = _schema_slot_types()
    _assert(len(declared) == 9, "OFL 12.5.1 defines 9 wheel-slot types")
    classified = _SHAPING_SLOTS | _NOT_SHAPING_SLOTS
    _assert(classified == declared,
            "every slot type is either beam-shaping evidence or explicitly not")
    _assert(not (_SHAPING_SLOTS & _NOT_SHAPING_SLOTS),
            "and no slot type is in both sets")


def test_the_non_evidence_set_stays_out_of_the_evidence_set():
    print("test_the_non_evidence_set_stays_out_of_the_evidence_set")
    # The negative decisions are the ones worth pinning: each of these looks
    # like beam evidence and is not, and every one of them was a live bug.
    overlap = _SHAPING_CAPABILITIES & NOT_EVIDENCE
    _assert(not overlap, f"none of {sorted(NOT_EVIDENCE)} counts as a hard edge")


def test_the_resting_angle_is_the_fixtures_own_spread():
    print("test_the_resting_angle_is_the_fixtures_own_spread")
    fixed = ofl_factory.fdef(physical=_lens(25))
    _assert(resting_half_angle(fixed, BeamKind.WASH) == 12.5,
            "a fixed 25 deg lens rests at a 12.5 deg half-angle")

    zoom = ofl_factory.fdef(physical=_lens(8, 30))
    # The geometric mean: averaging a zoom range straight drags it wide.
    _assert(abs(resting_half_angle(zoom, BeamKind.WASH)
                - math.sqrt(8 * 30) / 2) < 1e-9,
            "a zoom range rests at the geometric mean of its ends")
    _assert(resting_half_angle(zoom, BeamKind.WASH) < (8 + 30) / 4,
            "which is tighter than the arithmetic mean would be")

    bare = ofl_factory.fdef()
    _assert(resting_half_angle(bare, BeamKind.WASH)
            == DEFAULT_WASH_DEGREES / 2,
            "a fixture with no lens data falls back to a wash default")
    _assert(resting_half_angle(bare, BeamKind.HARD)
            == DEFAULT_HARD_DEGREES / 2,
            "or a tighter one when its profile says it is a beam")
    _assert(resting_half_angle(bare, BeamKind.NONE) is None,
            "and to nothing at all when it throws no shaft")


def test_a_defaulted_spread_is_classified_like_a_declared_one():
    """The wash default is over the ceiling, and that is what it is for.

    `resting_half_angle` answers "how wide" for a kind it is *handed*, so on its
    own it will happily quote 37.5 degrees for a WASH. What decides there is no
    WASH to quote for is `beam_kind`, which puts the fallback spread through the
    same `WASH_MAX_DEGREES` ceiling a declared one meets. Both halves are
    load-bearing: drop the routing and `DEFAULT_WASH_DEGREES = 75` silently
    becomes the widest cone on the stage instead of no cone at all, which is
    exactly what it did.
    """
    print("test_a_defaulted_spread_is_classified_like_a_declared_one")
    _assert(DEFAULT_WASH_DEGREES > WASH_MAX_DEGREES,
            f"the wash default ({DEFAULT_WASH_DEGREES} deg) is over the "
            f"{WASH_MAX_DEGREES} deg ceiling, which is how it means 'no shaft'")

    bare = ofl_factory.fdef(categories=("Moving Head",), channels=MOVER_CHANNELS)
    _assert(beam_kind(bare, BeamKind.WASH) is BeamKind.NONE,
            "so a profile falling back to WASH with nothing to go on throws "
            "no shaft at all")
    _assert(beam_kind(bare, BeamKind.HARD) is BeamKind.HARD,
            "while a HARD fallback is left alone — a scanner is hard-edged "
            "because of its mirror, not because of its lens")
    _assert(beam_kind(bare, BeamKind.NONE) is BeamKind.NONE,
            "and NONE was already nothing")

    # The evidence still outranks the fallback, or the routing above would have
    # turned every wash head with a real lens into a panel too.
    lensed = ofl_factory.fdef(categories=("Moving Head",),
                              channels=MOVER_CHANNELS, physical=_lens(25))
    _assert(beam_kind(lensed, BeamKind.WASH) is BeamKind.WASH,
            "a declared 25 deg lens still beats the default")
    zoomed = ofl_factory.fdef(
        categories=("Moving Head",),
        channels=dict(MOVER_CHANNELS,
                      z=ofl_factory.channel("Zoom", angleStart="8deg",
                                            angleEnd="40deg")))
    _assert(beam_kind(zoomed, BeamKind.WASH) is BeamKind.WASH,
            "and so does a zoom channel, which is only fitted to a fixture "
            "with a beam to zoom")


def test_the_resting_angle_never_reaches_the_tangent_asymptote():
    print("test_the_resting_angle_never_reaches_the_tangent_asymptote")
    for degrees in (90, 120, 180, 270, 360):
        fdef = ofl_factory.fdef(physical=_lens(degrees))
        for kind in (BeamKind.WASH, BeamKind.HARD):
            angle = resting_half_angle(fdef, kind)
            _assert(angle is not None and angle <= MAX_HALF_ANGLE_DEG,
                    f"{degrees} deg rests at {angle:.1f}, under the 90 deg wall")


def test_a_cone_stays_on_the_stage_however_wide_it_is_asked_to_be(qapp):
    print("test_a_cone_stays_on_the_stage_however_wide_it_is_asked_to_be")
    # tan(90 deg) is 1.6e16. Nothing declares an angle that wide once the
    # classifier runs, but a Zoom capability can resolve past 180 degrees and a
    # `stage.beam` hint can force a cone onto a fixture that has no business
    # with one, so the clamp is the thing that keeps the scene finite.
    from PyQt6.QtWidgets import QGraphicsRectItem

    parent = QGraphicsRectItem(0, 0, 10, 10)
    cone = BeamCone(parent, 5.0, 10.0)
    beam = BeamParams()
    beam.level = 1.0
    for half_angle in (89.0, 90.0, 135.0, 200.0):
        beam.half_angle_deg = half_angle
        cone.apply_beam(beam, haze=1.0)
        reach = max(max(abs(p.x()), abs(p.y())) for p in cone.polygon())
        _assert(reach < BEAM_LENGTH_PX * 10,
                f"a {half_angle} deg half-angle stays within the stage "
                f"({reach:.0f} px)")


def _schema_slot_types():
    """The wheel-slot types OFL itself declares, from the vendored schema."""
    with open(os.path.join(SCHEMA_DIR, "wheel-slot.json")) as handle:
        schema = json.load(handle)
    return frozenset(variant["properties"]["type"]["const"]
                     for variant in schema["oneOf"])


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_every_archetype_resolves_to_the_right_kind_of_light()
    test_a_fixture_with_a_gobo_wheel_always_lands_on_hardbeam()
    test_the_slot_table_covers_every_ofl_wheel_slot_type()
    test_the_non_evidence_set_stays_out_of_the_evidence_set()
    test_the_resting_angle_is_the_fixtures_own_spread()
    test_a_defaulted_spread_is_classified_like_a_declared_one()
    test_the_resting_angle_never_reaches_the_tangent_asymptote()
    test_a_cone_stays_on_the_stage_however_wide_it_is_asked_to_be(app)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    main()
