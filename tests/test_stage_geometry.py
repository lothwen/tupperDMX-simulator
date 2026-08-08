"""
Characterization test: aiming a light on a stage that has no depth (stage/).

The stage is a front-elevation photograph, so pan and tilt can't point into a
world — they are mapped into the plane of the picture. The load-bearing rule is
the first one below: a fixture with no pan or tilt channel must come out
pointing straight down at full length, because that is what every beam did
before fixtures could aim at all.

Also covers that every one of the 17 OFL categories actually builds and renders,
which is the difference between "a profile is registered" and "a profile works".

Run with:
    uv run python tests/test_stage_geometry.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.fixture_state import MotionState
from stage import renderer_for
from stage.frame import STRAIGHT_DOWN_DEG
from stage.geometry import Aim, Slew, Spinner
import ofl_factory


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


MOVER_CHANNELS = {
    "d": ofl_factory.channel("Intensity"),
    "p": ofl_factory.channel("Pan", angleStart="0deg", angleEnd="540deg"),
    "t": ofl_factory.channel("Tilt", angleStart="0deg", angleEnd="270deg"),
}


def test_a_fixture_that_cannot_aim_points_straight_down(qapp):
    print("test_a_fixture_that_cannot_aim_points_straight_down")
    aim = Aim(ofl_factory.fdef(channels={"d": ofl_factory.channel("Intensity")}))
    axis, foreshorten = aim.solve(MotionState())
    _assert(axis == STRAIGHT_DOWN_DEG,
            "no pan, no tilt -> the cone the engine always drew")
    _assert(foreshorten == 1.0, "and at its full length")


def test_pan_swings_the_beam_across_the_picture(qapp):
    print("test_pan_swings_the_beam_across_the_picture")
    aim = Aim(ofl_factory.fdef(channels=MOVER_CHANNELS))
    _assert(aim.pan_range == (0.0, 540.0), "the fixture's own range is read")

    centre, _ = aim.solve(MotionState(pan_deg=270.0))
    left, _ = aim.solve(MotionState(pan_deg=0.0))
    right, _ = aim.solve(MotionState(pan_deg=540.0))
    _assert(centre == STRAIGHT_DOWN_DEG, "mid-pan is straight down")
    _assert(left < centre < right, "either end swings to its own side")
    _assert(abs(left - centre) == abs(right - centre), "and symmetrically")


def test_tilt_lifts_the_beam_and_foreshortens_it(qapp):
    print("test_tilt_lifts_the_beam_and_foreshortens_it")
    aim = Aim(ofl_factory.fdef(channels=MOVER_CHANNELS))
    _, flat = aim.solve(MotionState(tilt_deg=135.0))
    axis, tipped = aim.solve(MotionState(tilt_deg=45.0))
    _assert(flat == 1.0, "pointing across the stage, the beam is full length")
    _assert(tipped < flat, "tipped toward the audience, it shortens")
    _assert(axis != STRAIGHT_DOWN_DEG, "and its axis has moved")
    _assert(tipped >= 0.15, "a beam at the camera is a bloom, never nothing")


def test_the_swing_is_clamped_to_the_picture(qapp):
    print("test_the_swing_is_clamped_to_the_picture")
    # Real pan is up to a turn and a half; drawn 1:1 the beam would sweep back
    # through the fixture's own body.
    aim = Aim(ofl_factory.fdef(channels={
        "p": ofl_factory.channel("Pan", angleStart="0deg", angleEnd="1200deg")}))
    axis, _ = aim.solve(MotionState(pan_deg=1200.0))
    _assert(abs(axis - STRAIGHT_DOWN_DEG) <= 80.0,
            "however far the head turns, the beam stays on stage")


def test_a_scanner_mirror_deflects_twice_its_own_angle(qapp):
    print("test_a_scanner_mirror_deflects_twice_its_own_angle")
    fdef = ofl_factory.fdef(categories=("Scanner",), channels=MOVER_CHANNELS)
    body = renderer_for(fdef)(fdef, 0.0, 0.0)
    try:
        body.update_dmx([255, 255, 128])
        swing = body.aim.pan_swing(body.state.motion)
        _assert(abs(body.mirror.rotation() - swing / 2.0) < 1e-6,
                "the mirror turns half as far as the beam it throws")
    finally:
        body.cleanup()


def test_spinner_integrates_a_rate_into_an_angle(qapp):
    print("test_spinner_integrates_a_rate_into_an_angle")
    spinner = Spinner()
    spinner.advance(1.0, 0.0)
    _assert(abs(spinner.advance(1.0, 0.25) - 90.0) < 1e-6,
            "a quarter second at 1 Hz is a quarter turn")
    _assert(abs(spinner.advance(1.0, 0.5) - 180.0) < 1e-6, "then half a turn")
    _assert(abs(spinner.advance(1.0, 0.75) - 270.0) < 1e-6, "then three quarters")
    _assert(abs(spinner.advance(1.0, 1.0)) < 1e-6, "and a full turn wraps to zero")
    _assert(spinner.advance(-1.0, 1.25) > 180.0, "a negative rate turns back")

    # A frame the app missed must not teleport a spinning gobo through several
    # turns, so an over-long gap is capped rather than integrated.
    jumped = Spinner()
    jumped.advance(1.0, 0.0)
    _assert(jumped.advance(1.0, 10.0) <= 90.0 + 1e-6,
            "a long stall advances by the cap, not by ten seconds")


def test_slew_chases_its_target(qapp):
    print("test_slew_chases_its_target")
    slew = Slew()
    _assert(slew.advance(100.0, 0.0) == 100.0,
            "the first value is adopted outright — nothing to move from")
    first = slew.advance(0.0, 0.05, tau=1.0)
    _assert(0.0 < first < 100.0, "then it moves part of the way each frame")
    second = slew.advance(0.0, 0.10, tau=1.0)
    _assert(second < first, "closing on the target rather than overshooting")


# One entry per OFL category, with channels that give its profile something to
# do. Registration is not the same thing as working: this builds and renders
# each one.
CATEGORY_PROBES = {
    "Barrel Scanner": MOVER_CHANNELS,
    "Blinder": {"d": ofl_factory.channel("Intensity"),
                "w": ofl_factory.channel("ColorIntensity", color="Warm White")},
    "Color Changer": ofl_factory.RGB_CHANNELS,
    "Dimmer": {"d": ofl_factory.channel("Intensity")},
    "Effect": {"d": ofl_factory.channel("Intensity"),
               "r": ofl_factory.channel("Rotation", speedStart="slow CW",
                                        speedEnd="fast CW")},
    "Fan": {"s": ofl_factory.channel("Speed", speedStart="0Hz", speedEnd="5Hz")},
    "Flower": {"d": ofl_factory.channel("Intensity")},
    "Hazer": {"f": ofl_factory.channel("FogOutput", fogOutputStart="off",
                                       fogOutputEnd="strong")},
    "Laser": dict(ofl_factory.RGB_CHANNELS,
                  e=ofl_factory.channel("Effect", effectName="Fan")),
    "Matrix": {"d": ofl_factory.channel("Intensity")},
    "Moving Head": MOVER_CHANNELS,
    "Other": {"d": ofl_factory.channel("Intensity")},
    "Pixel Bar": {"d": ofl_factory.channel("Intensity")},
    "Scanner": MOVER_CHANNELS,
    "Smoke": {"f": ofl_factory.channel("Fog", fogType="Fog")},
    "Stand": {"x": ofl_factory.channel("Maintenance")},
    "Strobe": {"d": ofl_factory.channel("Intensity"),
               "s": ofl_factory.channel("ShutterStrobe", shutterEffect="Strobe",
                                        speed="10Hz")},
}


def test_every_category_builds_and_renders(qapp):
    print("test_every_category_builds_and_renders")
    for category, channels in CATEGORY_PROBES.items():
        fdef = ofl_factory.fdef(categories=(category,), channels=channels)
        body = renderer_for(fdef)(fdef, 0.0, 0.0)
        try:
            for value in (0, 128, 255):
                body.update_dmx([value] * len(fdef.channels))
            _assert(body.childItems() != [] or body.emission is not None,
                    f"{category} draws as {type(body).__name__}")
        finally:
            body.cleanup()


def test_a_moving_head_with_a_gobo_wheel_gets_a_hard_beam(qapp):
    print("test_a_moving_head_with_a_gobo_wheel_gets_a_hard_beam")
    from stage.emission.cone import HardBeam, WashLamp

    # Both heads declare a lens, because the question here is what the GOBO
    # WHEEL does to the beam. A head with no lens, no zoom and no gate falls
    # through to the profile default, which resolves to no shaft at all — a
    # different question, pinned in `test_stage_optics.py`.
    wash = ofl_factory.fdef(categories=("Moving Head",),
                            channels=dict(MOVER_CHANNELS, **ofl_factory.RGB_CHANNELS),
                            physical={"lens": {"degreesMinMax": [25, 25]}})
    _assert(renderer_for(wash).emission_for(wash) is WashLamp,
            "a mover with only colour channels is a wash")

    spot = ofl_factory.fdef(
        categories=("Moving Head",),
        wheels={"Gobo Wheel": {"slots": [{"type": "Open"},
                                         {"type": "Gobo", "resource": "gobos/dots"}]}},
        channels=dict(MOVER_CHANNELS,
                      g=ofl_factory.channel("WheelSlot", wheel="Gobo Wheel",
                                            slotNumber=1)),
        physical={"lens": {"degreesMinMax": [25, 25]}})
    _assert(renderer_for(spot).emission_for(spot) is HardBeam,
            "the same body with a gobo wheel throws a beam you can see the edge of")

    # A zoom range's tight end is the zoom working, not a gate. Reading
    # min(lens) as evidence of a hard edge is what drew wash heads as spots.
    zoom_wash = ofl_factory.fdef(
        categories=("Moving Head",),
        channels=dict(MOVER_CHANNELS, **ofl_factory.RGB_CHANNELS),
        physical={"lens": {"degreesMinMax": [8, 30]}})
    _assert(renderer_for(zoom_wash).emission_for(zoom_wash) is WashLamp,
            "a wash head that zooms down to 8 deg is still a wash")


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_a_fixture_that_cannot_aim_points_straight_down(app)
    test_pan_swings_the_beam_across_the_picture(app)
    test_tilt_lifts_the_beam_and_foreshortens_it(app)
    test_the_swing_is_clamped_to_the_picture(app)
    test_a_scanner_mirror_deflects_twice_its_own_angle(app)
    test_spinner_integrates_a_rate_into_an_angle(app)
    test_slew_chases_its_target(app)
    test_every_category_builds_and_renders(app)
    test_a_moving_head_with_a_gobo_wheel_gets_a_hard_beam(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
