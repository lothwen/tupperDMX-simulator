"""
Characterization test: aiming a light on a stage that has no depth (stage/).

The stage is a front-elevation photograph, so a fixture is aimed in a room it
does have — pan and tilt build a unit vector — and that vector is then
flattened into the picture as `(axis_deg, foreshorten)`. Home is straight down,
tilt swings the beam and pan turns the plane it swings in; the load-bearing
rule is the first one below: a fixture with no pan or tilt channel must come
out pointing straight down at full length, because that is what every beam did
before fixtures could aim at all.

Also covers that every one of the 17 OFL categories actually builds and renders,
which is the difference between "a profile is registered" and "a profile works".

Run with:
    uv run python tests/test_stage_geometry.py
"""

import math
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


def test_half_of_the_declared_travel_is_straight_down(qapp):
    print("test_half_of_the_declared_travel_is_straight_down")
    aim = Aim(ofl_factory.fdef(channels=MOVER_CHANNELS))
    _assert(aim.pan_range == (0.0, 540.0), "the fixture's own range is read")
    _assert(aim.tilt_mid == 135.0, "and half of a 270 deg tilt is its home")

    # DMX 127/128 on both axes: where a hung head rests.
    axis, length = aim.solve(MotionState(pan_deg=270.0, tilt_deg=135.0))
    _assert(axis == STRAIGHT_DOWN_DEG, "mid-travel points at the floor")
    _assert(length == 1.0, "at the full length of its throw")


def test_pan_alone_cannot_move_a_beam_that_points_at_the_floor(qapp):
    print("test_pan_alone_cannot_move_a_beam_that_points_at_the_floor")
    # The old model swung the beam +-80 deg across the picture on pan alone.
    # A head resting straight down turns *under* its own beam.
    aim = Aim(ofl_factory.fdef(channels=MOVER_CHANNELS))
    for pan in (0.0, 135.0, 270.0, 405.0, 540.0):
        axis, length = aim.solve(MotionState(pan_deg=pan, tilt_deg=135.0))
        _assert(abs(axis - STRAIGHT_DOWN_DEG) < 1e-6 and length == 1.0,
                f"pan {pan:.0f} deg leaves the beam where it was")


def test_tilt_at_home_pan_walks_the_beam_toward_the_viewer(qapp):
    print("test_tilt_at_home_pan_walks_the_beam_toward_the_viewer")
    # Pan facing the audience: the beam stays on the picture's vertical axis
    # and only its length changes, longest at half travel.
    aim = Aim(ofl_factory.fdef(channels=MOVER_CHANNELS))
    down, full = aim.solve(MotionState(pan_deg=270.0, tilt_deg=135.0))
    near, tipped = aim.solve(MotionState(pan_deg=270.0, tilt_deg=90.0))
    far, tipped_back = aim.solve(MotionState(pan_deg=270.0, tilt_deg=180.0))
    _assert(full == 1.0, "half travel is the longest the beam ever draws")
    _assert(abs(near - down) < 1e-6 and abs(far - down) < 1e-6,
            "tilting keeps it on the same axis of the picture")
    _assert(tipped < full and abs(tipped - tipped_back) < 1e-6,
            "and shortens it by the same amount either side of home")
    _assert(aim.solve(MotionState(pan_deg=270.0, tilt_deg=45.0))[1] >= 0.15,
            "a beam at the camera is a bloom, never nothing")


def test_a_quarter_turn_of_pan_sweeps_the_tilt_across_the_picture(qapp):
    print("test_a_quarter_turn_of_pan_sweeps_the_tilt_across_the_picture")
    # Turned side-on, the plane the head tilts in IS the picture: the same tilt
    # that only shortened the beam now carries it across the stage, full length.
    aim = Aim(ofl_factory.fdef(channels=MOVER_CHANNELS))
    left, out = aim.solve(MotionState(pan_deg=270.0 + 90.0, tilt_deg=45.0))
    right, back = aim.solve(MotionState(pan_deg=270.0 + 90.0, tilt_deg=225.0))
    _assert(abs(out - 1.0) < 1e-9 and abs(back - 1.0) < 1e-9,
            "nothing is aimed out of the picture, so nothing is shortened")
    _assert(abs(left - STRAIGHT_DOWN_DEG) > 45.0,
            "the beam has swung right across the stage")
    _assert(abs((left + right) / 2.0 - STRAIGHT_DOWN_DEG) < 1e-6,
            "symmetrically about the floor")


def test_the_projection_is_a_unit_vector(qapp):
    print("test_the_projection_is_a_unit_vector")
    # What the pair means, and why the fog march can rebuild a direction in
    # three dimensions from it: LIGHT_FRAG reads back
    # `(cos.f, sin.f, -sqrt(1 - f^2))` and expects a unit vector.
    aim = Aim(ofl_factory.fdef(channels=MOVER_CHANNELS))
    # A pan range this wide is a turn and a half either way; drawn 1:1 as a
    # screen swing it used to need clamping to keep the beam on stage. A
    # projection needs none — it cannot leave the unit sphere.
    for pan in (-1200.0, 0.0, 90.0, 270.0, 540.0, 1200.0):
        for tilt in (0.0, 60.0, 135.0, 200.0, 270.0):
            axis, flat = aim.solve(MotionState(pan_deg=pan, tilt_deg=tilt))
            radians = math.radians(axis)
            length = math.sqrt((math.cos(radians) * flat) ** 2
                               + (math.sin(radians) * flat) ** 2
                               + max(0.0, 1.0 - flat * flat))
            _assert(flat <= 1.0 and abs(length - 1.0) < 1e-9,
                    f"pan {pan:.0f} tilt {tilt:.0f} is one beam's worth of light")


def test_a_standing_fixture_throws_its_beam_at_the_ceiling(qapp):
    print("test_a_standing_fixture_throws_its_beam_at_the_ceiling")
    fdef = ofl_factory.fdef(channels=MOVER_CHANNELS)
    flown = Aim(fdef)
    standing = Aim(fdef, hanging=False)
    for pan, tilt in ((270.0, 135.0), (270.0, 90.0), (360.0, 45.0)):
        motion = MotionState(pan_deg=pan, tilt_deg=tilt)
        down, near = flown.solve(motion)
        up, far = standing.solve(motion)
        _assert(abs((up - down) % 360.0 - 180.0) < 1e-6,
                f"pan {pan:.0f} tilt {tilt:.0f}: turned over, the beam is too")
        _assert(abs(near - far) < 1e-9,
                "and the part of it aimed at the audience is unchanged")


def test_rigging_turns_the_chassis_and_never_the_light(qapp):
    print("test_rigging_turns_the_chassis_and_never_the_light")
    # The GPU offsets each body's primitives by its *position* and nothing
    # else, so the flip has to live in the aim and in the housing — never in a
    # transform on the body, which the shader would not know about.
    fdef = ofl_factory.fdef(categories=("Moving Head",), channels=MOVER_CHANNELS,
                            physical={"lens": {"degreesMinMax": [12, 12]}})
    flown = renderer_for(fdef)(fdef, 0.0, 0.0, hanging=True)
    standing = renderer_for(fdef)(fdef, 0.0, 0.0, hanging=False)
    try:
        _assert(flown.rotation() == 0.0 and standing.rotation() == 0.0,
                "the body item itself is never turned")
        _assert(flown.housing.rotation() == 180.0
                and standing.housing.rotation() == 0.0,
                "the chassis is — a flown fixture is drawn upside down")
        _assert(flown.emit_origin()[1] > standing.emit_origin()[1],
                "so its lens is at the other end of the body")
        for body in (flown, standing):
            body.update_dmx([255, 255, 128])
        _assert(abs((flown._frame.beam.axis_deg
                     - standing._frame.beam.axis_deg) % 360.0 - 180.0) < 1e-6,
                "and the light it describes is turned with it, not by Qt")
    finally:
        flown.cleanup()
        standing.cleanup()


def test_a_fog_machine_ignores_a_patch_that_flies_it(qapp):
    print("test_a_fog_machine_ignores_a_patch_that_flies_it")
    # A plume climbs. Nobody hangs a hazer upside down, and one drawn that way
    # would pour smoke out of the floor.
    fdef = ofl_factory.fdef(categories=("Hazer",), channels={
        "f": ofl_factory.channel("FogOutput", fogOutputStart="off",
                                 fogOutputEnd="strong")})
    body = renderer_for(fdef)(fdef, 0.0, 0.0, hanging=True)
    try:
        _assert(not body.hanging, "the profile refuses to be flown")
        _assert(body.housing.rotation() == 0.0, "so its case stays upright")
    finally:
        body.cleanup()


def test_a_scanner_mirror_deflects_twice_its_own_angle(qapp):
    print("test_a_scanner_mirror_deflects_twice_its_own_angle")
    fdef = ofl_factory.fdef(categories=("Scanner",), channels=MOVER_CHANNELS)
    body = renderer_for(fdef)(fdef, 0.0, 0.0)
    try:
        # Panned a quarter turn off home, so tilt deflects in the picture and
        # there is an angle to halve at all.
        body.update_dmx([255, 213, 200])
        swing = body._frame.beam.axis_deg - STRAIGHT_DOWN_DEG
        _assert(abs(swing) > 1.0, "the beam is deflected somewhere")
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
    test_half_of_the_declared_travel_is_straight_down(app)
    test_pan_alone_cannot_move_a_beam_that_points_at_the_floor(app)
    test_tilt_at_home_pan_walks_the_beam_toward_the_viewer(app)
    test_a_quarter_turn_of_pan_sweeps_the_tilt_across_the_picture(app)
    test_the_projection_is_a_unit_vector(app)
    test_a_standing_fixture_throws_its_beam_at_the_ceiling(app)
    test_rigging_turns_the_chassis_and_never_the_light(app)
    test_a_fog_machine_ignores_a_patch_that_flies_it(app)
    test_a_scanner_mirror_deflects_twice_its_own_angle(app)
    test_spinner_integrates_a_rate_into_an_angle(app)
    test_slew_chases_its_target(app)
    test_every_category_builds_and_renders(app)
    test_a_moving_head_with_a_gobo_wheel_gets_a_hard_beam(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
