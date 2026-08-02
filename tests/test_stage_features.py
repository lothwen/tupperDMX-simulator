"""
Characterization test: the capability features that shape a beam (ui/stage/).

Covers the layers that need a fixture's wheels to mean anything — colour gels,
gobo patterns, a closed slot — plus the prism, the shutter waveforms and the
stage-wide haze that decides whether any of it is visible at all.

Run with:
    uv run python tests/test_stage_features.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.fixture_state import resolve_state
from ui.stage import renderer_for, wheelart
from ui.stage.atmosphere import Atmosphere
from ui.stage.features.level import ShutterFeature
import ofl_factory


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


WHEELS = {
    "Color Wheel": {"slots": [
        {"type": "Open"},
        {"type": "Color", "name": "Red", "colors": ["#ff0000"]},
        {"type": "Color", "name": "Blue", "colors": ["#0000ff"]},
    ]},
    "Gobo Wheel": {"slots": [
        {"type": "Open"},
        {"type": "Gobo", "resource": "gobos/breakup-foliage", "name": "Foliage"},
        {"type": "Gobo", "resource": "gobos/dots-small", "name": "Dots"},
        {"type": "Prism", "name": "3-facet", "facets": 3},
        {"type": "Closed"},
    ]},
}

# Channel keys spelled out: "g" would be both green and the gobo wheel, and one
# would silently replace the other in the document.
MOVER = {
    "dim": ofl_factory.channel("Intensity"),
    "red": ofl_factory.channel("ColorIntensity", color="Red"),
    "green": ofl_factory.channel("ColorIntensity", color="Green"),
    "blue": ofl_factory.channel("ColorIntensity", color="Blue"),
    "pan": ofl_factory.channel("Pan", angleStart="0deg", angleEnd="540deg"),
    "cwheel": ofl_factory.ranged(
        ofl_factory.cap("WheelSlot", [0, 84], wheel="Color Wheel", slotNumber=1),
        ofl_factory.cap("WheelSlot", [85, 169], wheel="Color Wheel", slotNumber=2),
        ofl_factory.cap("WheelSlot", [170, 255], wheel="Color Wheel", slotNumber=3)),
    "gwheel": ofl_factory.ranged(
        ofl_factory.cap("WheelSlot", [0, 50], wheel="Gobo Wheel", slotNumber=1),
        ofl_factory.cap("WheelSlot", [51, 101], wheel="Gobo Wheel", slotNumber=2),
        ofl_factory.cap("WheelSlot", [102, 152], wheel="Gobo Wheel", slotNumber=3),
        ofl_factory.cap("WheelSlot", [153, 203], wheel="Gobo Wheel", slotNumber=4),
        ofl_factory.cap("WheelSlot", [204, 255], wheel="Gobo Wheel", slotNumber=5)),
}


def _mover(**values):
    """A moving head, and a DMX slice with the named channels set."""
    fdef = ofl_factory.fdef(categories=("Moving Head",), channels=MOVER,
                            wheels=WHEELS)
    order = [spec.field for spec in fdef.channels]
    unknown = set(values) - set(order)
    assert not unknown, f"no such channel: {sorted(unknown)}"
    return fdef, [values.get(field, 0) for field in order]


def _beam(fdef, dmx):
    """Build the body, feed it a frame and hand back the shaped beam."""
    body = renderer_for(fdef)(fdef, 0.0, 0.0)
    try:
        body.update_dmx(dmx)
        return body._frame.beam, body
    finally:
        pass


def test_a_colour_wheel_filters_the_beam(qapp):
    print("test_a_colour_wheel_filters_the_beam")
    white = _mover(dim=255, red=255, green=255, blue=255)
    beam, body = _beam(*white)
    try:
        _assert(beam.rgb == (255, 255, 255), "the open slot passes the lamp through")
    finally:
        body.cleanup()

    beam, body = _beam(*_mover(dim=255, red=255, green=255, blue=255, cwheel=100))
    try:
        _assert(beam.rgb == (255, 0, 0),
                "a red gel over a white lamp passes only red")
    finally:
        body.cleanup()

    # The gel multiplies rather than replacing: red gel, green lamp, no light.
    beam, body = _beam(*_mover(dim=255, green=255, cwheel=100))
    try:
        _assert(beam.rgb == (0, 0, 0),
                "and a red gel over a green lamp passes nothing, as it would "
                "on the real fixture")
    finally:
        body.cleanup()


def test_a_gobo_slot_puts_a_pattern_in_the_beam(qapp):
    print("test_a_gobo_slot_puts_a_pattern_in_the_beam")
    beam, body = _beam(*_mover(dim=255, red=255, green=255, blue=255))
    try:
        _assert(beam.gobo is None or wheelart.pattern_for(beam.gobo) == "",
                "the open slot puts nothing in the beam")
    finally:
        body.cleanup()

    beam, body = _beam(*_mover(dim=255, red=255, green=255, blue=255, gwheel=75))
    try:
        _assert(beam.gobo is not None and beam.gobo.name == "Foliage",
                "the gobo slot reaches the beam")
        _assert(wheelart.gobo_path(beam.gobo, 40.0) is not None,
                "and it has a pattern to draw")
    finally:
        body.cleanup()


def test_a_closed_slot_blacks_the_fixture_out(qapp):
    print("test_a_closed_slot_blacks_the_fixture_out")
    beam, body = _beam(*_mover(dim=255, red=255, green=255, blue=255, gwheel=255))
    try:
        _assert(beam.level == 0.0,
                "a closed slot is a hole the disc hasn't got — no light, "
                "whatever the dimmer says")
    finally:
        body.cleanup()


def test_different_gobos_draw_different_patterns(qapp):
    print("test_different_gobos_draw_different_patterns")
    from core import ofl
    foliage = ofl.WheelSlot("Gobo", {"resource": "gobos/breakup-foliage",
                                     "name": "Foliage"})
    dots = ofl.WheelSlot("Gobo", {"resource": "gobos/dots-small", "name": "Dots"})
    lines = ofl.WheelSlot("Gobo", {"resource": "gobos/lines-thin", "name": "Lines"})
    _assert(wheelart.pattern_for(dots) == "dots", "a dotted gobo draws dots")
    _assert(wheelart.pattern_for(lines) == "lines", "a lined one draws lines")
    _assert(wheelart.pattern_for(foliage) == "breakup", "foliage is a breakup")
    paths = [wheelart.gobo_path(s, 40.0) for s in (foliage, dots, lines)]
    _assert(all(p is not None for p in paths), "each has a path")
    _assert(len({p.elementCount() for p in paths}) > 1,
            "and they are visibly different shapes, not one disc three times")

    _assert(wheelart.gobo_path(dots, 40.0) is wheelart.gobo_path(dots, 40.0),
            "the same gobo at the same size is cached, not rebuilt per frame")


def test_a_prism_splits_the_beam(qapp):
    print("test_a_prism_splits_the_beam")
    fdef = ofl_factory.fdef(
        categories=("Moving Head",),
        wheels=WHEELS,
        channels={"d": ofl_factory.channel("Intensity"),
                  "pr": ofl_factory.channel("Prism", wheel="Gobo Wheel")})
    beam, body = _beam(fdef, [255, 255])
    try:
        _assert(len(beam.copies) == 3,
                "a 3-facet prism makes three copies of the beam")
        _assert(len(body.emission.cones) >= 3, "and there are cones to draw them")
    finally:
        body.cleanup()


def test_the_shutter_waveforms_differ(qapp):
    print("test_the_shutter_waveforms_differ")
    series = {}
    for effect in ("Open", "Closed", "Strobe", "Pulse", "RampUp", "RampDown",
                   "RampUpDown", "Lightning", "Spikes", "Burst"):
        fdef = ofl_factory.fdef(channels={
            "d": ofl_factory.channel("Intensity"),
            "s": ofl_factory.channel("ShutterStrobe", shutterEffect=effect,
                                     speed="4Hz")})
        body = renderer_for(fdef)(fdef, 0.0, 0.0)
        try:
            body.update_dmx([255, 255])
            shutter = next(f for f in body.features
                           if isinstance(f, ShutterFeature))
            shutter._seed = 0.0    # pin the jitter so the run is repeatable
            series[effect] = tuple(round(shutter._gate(body.state.strobe, t / 20.0), 3)
                                   for t in range(20))
        finally:
            body.cleanup()

    _assert(set(series["Open"]) == {1.0}, "Open passes everything")
    _assert(set(series["Closed"]) == {0.0}, "Closed passes nothing")
    for effect in ("Strobe", "Pulse", "RampUp", "RampDown", "RampUpDown",
                   "Lightning", "Spikes", "Burst"):
        _assert(len(set(series[effect])) > 1, f"{effect} actually varies")
    distinct = {series[e] for e in ("Strobe", "Pulse", "RampUp", "RampDown",
                                    "RampUpDown", "Spikes")}
    _assert(len(distinct) == 6, "and no two of them are the same waveform")


def test_haze_builds_and_hangs_around():
    print("test_haze_builds_and_hangs_around")
    air = Atmosphere(baseline=0.0)
    _assert(air.level == 0.0, "clean air to start")

    air.contribute(1, 1.0, "Haze")
    for step in range(40):
        air.advance(step * 0.1)
    _assert(air.level > 0.8, "a hazer at full fills the room in a few seconds")

    air.release(1)
    before = air.level
    for step in range(40, 60):
        air.advance(step * 0.1)
    _assert(air.level < before, "and it starts to clear when the hazer stops")
    _assert(air.level > 0.5,
            "but slowly — haze hangs in a room, and beams that vanished with "
            "the DMX would look wrong")


def test_two_hazers_combine_without_exceeding_full():
    print("test_two_hazers_combine_without_exceeding_full")
    air = Atmosphere(baseline=0.2)
    air.contribute(1, 0.8, "Haze")
    air.contribute(2, 0.8, "Haze")
    _assert(air.target <= 1.0, "two hazers never make more than a full room")
    _assert(air.target > 0.9, "but they do add up")

    fog = Atmosphere(baseline=0.0)
    fog.contribute(1, 1.0, "Fog")
    haze = Atmosphere(baseline=0.0)
    haze.contribute(1, 1.0, "Haze")
    _assert(fog.target < haze.target,
            "fog is denser but stays low, so it helps the beams less than haze")


def test_a_hazer_lights_up_the_whole_rig(qapp):
    print("test_a_hazer_lights_up_the_whole_rig")
    air = Atmosphere(baseline=0.0)
    fdef = ofl_factory.fdef(
        categories=("Hazer",),
        channels={"f": ofl_factory.channel("FogOutput", fogOutputStart="off",
                                           fogOutputEnd="strong")})
    hazer = renderer_for(fdef)(fdef, 0.0, 0.0)
    hazer.set_atmosphere(air)
    try:
        hazer.update_dmx([255])
        _assert(air.target > 0.9,
                "a hazer at full asks for a full room, whatever else is patched")
        hazer.update_dmx([0])
        _assert(air.target == 0.0, "and asks for nothing when it stops")
    finally:
        hazer.cleanup()
    _assert(air.target == 0.0, "a hazer that leaves the stage stops contributing")


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_a_colour_wheel_filters_the_beam(app)
    test_a_gobo_slot_puts_a_pattern_in_the_beam(app)
    test_a_closed_slot_blacks_the_fixture_out(app)
    test_different_gobos_draw_different_patterns(app)
    test_a_prism_splits_the_beam(app)
    test_the_shutter_waveforms_differ(app)
    test_haze_builds_and_hangs_around()
    test_two_hazers_combine_without_exceeding_full()
    test_a_hazer_lights_up_the_whole_rig(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
