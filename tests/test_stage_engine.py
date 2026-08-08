"""
Characterization test: the two-stage stage engine (stage/).

The engine resolves a fixture in two steps — its OFL *category* picks the body
and the kind of light it makes, then the *capabilities* its channels declare
pick the feature layers that shape that light. This covers the second step: that
features are discovered from the channels once at construction, that a fixture
declaring nothing gets nothing, and that a body releases everything it holds.

Run with:
    uv run python tests/test_stage_engine.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.fixture_library import resolve_fixture
from stage import CLOCK, renderer_for
from stage.emission.base import emission_reach
from stage.features.base import FEATURES, build_features
from stage.features.beamshape import (
    BladeFeature, IrisFeature, PanTiltFeature, PrismFeature, ZoomFeature,
)
from stage.features.level import ColorFeature, DimmerFeature, ShutterFeature
import ofl_factory


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _body(fdef, **kwargs):
    return renderer_for(fdef)(fdef, 0.0, 0.0, **kwargs)


def _names(body):
    return {type(f).__name__ for f in body.features}


def test_features_come_from_the_channels(qapp):
    print("test_features_come_from_the_channels")
    bare = _body(ofl_factory.fdef(channels={"d": ofl_factory.channel("Intensity")}))
    try:
        _assert(_names(bare) == {"DimmerFeature", "ColorFeature"},
                "a plain dimmer gets only the two layers every fixture has")
        _assert(ZoomFeature not in [type(f) for f in bare.features],
                "and nothing that would shape a beam it hasn't got")
    finally:
        bare.cleanup()

    full = _body(ofl_factory.fdef(
        categories=("Moving Head",),
        channels={
            "d": ofl_factory.channel("Intensity"),
            "p": ofl_factory.channel("Pan", angleStart="0deg", angleEnd="540deg"),
            "z": ofl_factory.channel("Zoom", angleStart="8deg", angleEnd="40deg"),
            "i": ofl_factory.channel("Iris", openPercentStart="closed",
                                     openPercentEnd="open"),
            "s": ofl_factory.channel("ShutterStrobe", shutterEffect="Strobe",
                                     speed="10Hz"),
        }))
    try:
        _assert({"DimmerFeature", "ColorFeature", "ShutterFeature", "ZoomFeature",
                 "IrisFeature", "PanTiltFeature"} <= _names(full),
                "every declared capability brings its layer")
        _assert("PrismFeature" not in _names(full),
                "and an undeclared one brings nothing")
    finally:
        full.cleanup()


def test_a_moving_head_without_gobos_degrades_to_a_wash(qapp):
    print("test_a_moving_head_without_gobos_degrades_to_a_wash")
    # The graceful-degradation case: categorised as a mover, but its channels
    # describe a colour wash with zoom. It must still aim, and must not grow a
    # gobo or prism layer out of nowhere.
    fdef = ofl_factory.fdef(
        categories=("Moving Head",),
        channels=dict(ofl_factory.RGB_CHANNELS,
                      d=ofl_factory.channel("Intensity"),
                      p=ofl_factory.channel("Pan", angleStart="0deg",
                                            angleEnd="540deg"),
                      t=ofl_factory.channel("Tilt", angleStart="0deg",
                                            angleEnd="270deg"),
                      z=ofl_factory.channel("Zoom", angleStart="10deg",
                                            angleEnd="60deg")))
    body = _body(fdef)
    try:
        _assert("PanTiltFeature" in _names(body), "it still aims")
        _assert(not ({"GoboFeature", "PrismFeature", "BladeFeature"} & _names(body)),
                "but grows no beam-shaping layers it has no channels for")
        body.update_dmx([255, 255, 255, 255, 255, 128, 255])
        _assert(body.state.motion.pan_deg is not None, "and resolves its position")
    finally:
        body.cleanup()


def test_features_are_discovered_once(qapp):
    print("test_features_are_discovered_once")
    fdef = resolve_fixture("ledbar")
    body = _body(fdef)
    try:
        before = list(body.features)
        for _ in range(3):
            body.update_dmx([255, 255, 255, 255, 0, 255, 0])
        _assert([f is g for f, g in zip(before, body.features)] == [True] * len(before),
                "rendering a frame never rebuilds the feature chain")
    finally:
        body.cleanup()


def test_only_animating_bodies_hold_the_clock(qapp):
    print("test_only_animating_bodies_hold_the_clock")
    still = _body(ofl_factory.fdef(channels={"d": ofl_factory.channel("Intensity")}))
    try:
        _assert(still._clock_token is None,
                "a fixture with nothing moving never subscribes")
    finally:
        still.cleanup()

    moving = [_body(resolve_fixture("ledbar"), body_index=i) for i in range(4)]
    try:
        _assert(all(b._clock_token is not None for b in moving),
                "an animating body does")
        _assert(len({id(CLOCK)}) == 1,
                "and they all share the one stage clock, not a timer each")
    finally:
        for body in moving:
            body.cleanup()
    _assert(all(b._clock_token is None for b in moving),
            "cleanup unsubscribes every one of them")


def test_cleanup_releases_the_animation(qapp):
    print("test_cleanup_releases_the_animation")
    body = _body(resolve_fixture("ledbar"))
    body.update_dmx([255, 255, 255, 255, 64, 255, 0])
    _assert(body.current_movie is not None, "an animation is running")
    body.cleanup()
    _assert(body.current_movie is None, "and stops when the body goes")


def test_emission_reach_answers_for_the_preview(qapp):
    print("test_emission_reach_answers_for_the_preview")
    _assert(emission_reach(resolve_fixture("ledbar")) == 0.0,
            "an LED bar declares no optics, so it reserves no room for a cone")
    lensed = ofl_factory.fdef(categories=("Color Changer",),
                              channels={"d": ofl_factory.channel("Intensity")},
                              physical={"lens": {"degreesMinMax": [10, 60]}})
    _assert(emission_reach(lensed) > 0.0, "a fixture with a lens does")
    zoomed = ofl_factory.fdef(
        categories=("Color Changer",),
        channels={"z": ofl_factory.channel("Zoom", angleStart="8deg",
                                           angleEnd="40deg")})
    _assert(emission_reach(zoomed) > 0.0, "and so does one with a zoom channel")

    # A declared lens angle is not the same claim as a visible shaft: every
    # emitting fixture has a beam angle, and a flood's is simply very wide.
    panel = ofl_factory.fdef(categories=("Dimmer", "Color Changer"),
                             channels={"d": ofl_factory.channel("Intensity")},
                             physical={"lens": {"degreesMinMax": [115, 115]}})
    _assert(emission_reach(panel) == 0.0,
            "a 115 deg panel lights a surface, so it reserves no room either")

    # And a head that declares nothing at all reserves nothing either. Its
    # profile falls back to WASH, but a fallback spread meets the same
    # `WASH_MAX_DEGREES` ceiling a declared one does and `DEFAULT_WASH_DEGREES`
    # sits above it, so what it actually resolves to is no shaft — the fixture
    # aims a soft source rather than a beam. Inventing a cone here was the
    # mirror image of the bug this module exists to undo: reading a blank field
    # as a claim, rather than as the absence of one.
    mover = ofl_factory.fdef(
        categories=("Moving Head",),
        channels={"p": ofl_factory.channel("Pan", angleStart="0deg",
                                           angleEnd="540deg")})
    _assert(emission_reach(mover) == 0.0,
            "a mover that declares no optics at all reserves no room either")


def test_the_shutter_gate_is_a_pure_function_of_time(qapp):
    print("test_the_shutter_gate_is_a_pure_function_of_time")
    body = _body(ofl_factory.fdef(channels={
        "d": ofl_factory.channel("Intensity"),
        "s": ofl_factory.channel("ShutterStrobe", shutterEffect="Strobe",
                                 speed="5Hz")}))
    try:
        shutter = next(f for f in body.features if isinstance(f, ShutterFeature))
        body.update_dmx([255, 255])
        # Half a flash cycle apart: on and off, and the same answer both times
        # a given moment is asked about.
        gates = [shutter._gate(body.state.strobe, t) for t in (0.0, 0.1, 0.0)]
        _assert(gates[0] == gates[2], "the same instant always gives the same gate")
        _assert(gates[0] != gates[1], "and half a cycle later it has flipped")
    finally:
        body.cleanup()


def test_every_registered_feature_declares_itself(qapp):
    print("test_every_registered_feature_declares_itself")
    for feature in FEATURES:
        _assert(isinstance(feature.order, int), f"{feature.__name__} has an order")
        _assert(isinstance(feature.types, frozenset),
                f"{feature.__name__} names the capability types it reads")
    orders = [f.order for f in sorted(FEATURES, key=lambda c: c.order)]
    _assert(orders == sorted(orders), "the chain has a defined order")
    _assert(DimmerFeature.order < ColorFeature.order < ShutterFeature.order
            < ZoomFeature.order < IrisFeature.order < PrismFeature.order
            < BladeFeature.order < PanTiltFeature.order,
            "level and colour run before the layers that shape them, aiming last")


def test_a_matrix_is_one_body_unless_its_pixels_are_separated(qapp):
    print("test_a_matrix_is_one_body_unless_its_pixels_are_separated")
    from stage.metrics import body_count

    _assert(body_count(ofl_factory.fdef(categories=("Pixel Bar",))) == 1,
            "a fixture with no matrix at all is one body")

    strip = ofl_factory.fdef(categories=("Pixel Bar",),
                             matrix={"pixelCount": [24, 1, 1]})
    _assert(body_count(strip) == 1, "a 24-pixel bar is one bar, not 24 fixtures")

    grid = ofl_factory.fdef(categories=("Flower",),
                            matrix={"pixelCount": [5, 5, 1]})
    _assert(body_count(grid) == 1, "a 5x5 pixel grid is one housing")

    # The LED bar's own numbers: 50mm-wide pixels sitting 440mm apart are not a
    # row of cells behind one lens, they are eight bars.
    apart = ofl_factory.fdef(
        categories=("Pixel Bar",), matrix={"pixelCount": [8, 40, 1]},
        physical={"matrixPixels": {"dimensions": [50, 25, 60],
                                   "spacing": [440, 0, 0]}})
    _assert(body_count(apart) == 8,
            "pixels spaced far wider than they are wide are separate bodies")

    hinted = ofl_factory.fdef(
        categories=("Pixel Bar",), matrix={"pixelCount": [6, 1, 1]},
        sidecar={"tdmx": 1, "modes": {"*": {"stage": {"bodies": 3}}}})
    _assert(body_count(hinted) == 3, "a sidecar stage hint overrides outright")


def test_matrix_cells_cover_the_whole_grid(qapp):
    print("test_matrix_cells_cover_the_whole_grid")
    grid = ofl_factory.fdef(categories=("Matrix",),
                            matrix={"pixelCount": [4, 3, 1]})
    body = _body(grid)
    cells = body.emission
    _assert((cells.columns, cells.rows) == (4, 3),
            "one body spans the matrix's own X by Y extent")
    _assert(len(cells.cells) == 12, "every pixel of the grid gets a cell")
    _assert({c.rect().width() for c in cells.cells} == {body.width / 4},
            "the cells divide the body's width between them")
    body.cleanup()

    # A bar-set shares the columns out instead, one each.
    apart = ofl_factory.fdef(
        categories=("Pixel Bar",), matrix={"pixelCount": [8, 40, 1]},
        physical={"matrixPixels": {"dimensions": [50, 25, 60],
                                   "spacing": [440, 0, 0]}})
    bodies = [_body(apart, body_index=i) for i in range(8)]
    _assert(all(b.emission.columns == 1 and b.emission.rows == 40
                for b in bodies), "each bar of a bar-set shows one column")
    _assert([b.emission.column for b in bodies] == list(range(8)),
            "and starts at its own column of the running animation")
    for b in bodies:
        b.cleanup()


def _segmented_wash(zones):
    """A Color Changer whose face is split into independently-driven zones.

    The shape of the Wild Wash Pro: a wash — not a pixel bar — that happens to
    address its face in segments, so its category picks a lamp body and its
    matrix decides how many lamps that body has.
    """
    return ofl_factory.fdef(
        categories=("Color Changer",),
        matrix={"pixelCount": [zones, 1, 1]},
        template_channels={
            "Red $pixelKey": ofl_factory.channel("ColorIntensity", color="Red"),
            "Green $pixelKey": ofl_factory.channel("ColorIntensity", color="Green"),
            "Blue $pixelKey": ofl_factory.channel("ColorIntensity", color="Blue"),
        },
        mode_channels=["d", {"insert": "matrixChannels",
                             "repeatFor": "eachPixelXYZ",
                             "channelOrder": "perPixel",
                             "templateChannels": ["Red $pixelKey",
                                                  "Green $pixelKey",
                                                  "Blue $pixelKey"]}],
        physical={"lens": {"degreesMinMax": [40, 40]}},
    )


def _rgb(color):
    return color.red(), color.green(), color.blue()


def test_a_segmented_wash_lights_every_zone(qapp):
    print("test_a_segmented_wash_lights_every_zone")
    body = _body(_segmented_wash(4))
    lamps = body.emission.lamps
    _assert(type(body).__name__ == "ColorChangerFixture",
            "a segmented wash is still a wash, not a pixel bar")
    _assert(len(lamps) == 4, "with one lamp per zone")
    _assert([round(lamp.rect().x()) for lamp in lamps]
            == sorted(round(lamp.rect().x()) for lamp in lamps)
            and len({round(lamp.rect().width(), 3) for lamp in lamps}) == 1,
            "laid out evenly across the face, the way the segments are")

    body.update_dmx([255, 255, 0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0])
    _assert([_rgb(lamp.glow_color) for lamp in lamps]
            == [(255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 0, 0)],
            "each zone draws its own channels, not the first zone's")

    stops = body.emission.cone.brush().gradient().stops()
    # Red, green and blue at full over a dark fourth: a quarter of each.
    _assert(_rgb(stops[0][1]) == (63, 63, 63),
            "and the cone shows the zones mixed, as they are once they land")

    body.update_dmx([128, 255, 0, 0, 255, 0, 0, 255, 0, 0, 255, 0, 0])
    _assert([round(lamp.glow_intensity, 2) for lamp in lamps] == [0.5] * 4,
            "the fixture's dimmer still scales every zone")
    body.cleanup()


def test_a_wash_with_one_zone_is_unchanged(qapp):
    print("test_a_wash_with_one_zone_is_unchanged")
    par = ofl_factory.fdef(categories=("Color Changer",),
                           channels=dict(ofl_factory.RGB_CHANNELS),
                           physical={"lens": {"degreesMinMax": [25, 25]}})
    body = _body(par)
    _assert(len(body.emission.lamps) == 1, "a par has a single lamp")
    _assert(body.emission.lamp is body.emission.lamps[0],
            "which is the one the profile and the preview reach for")
    _assert(round(body.emission.lamp.rect().x(), 3)
            == round((body.width - body.emission.lamp.rect().width()) / 2, 3),
            "centred in its body, as it has always been")

    body.update_dmx([255, 0, 0])
    _assert(_rgb(body.emission.lamp.glow_color) == (255, 0, 0),
            "and takes the fixture's own colour")
    body.cleanup()


def test_a_big_matrix_behind_a_lamp_is_sampled(qapp):
    print("test_a_big_matrix_behind_a_lamp_is_sampled")
    from stage.emission.cone import MAX_LAMPS

    body = _body(_segmented_wash(200))
    _assert(len(body.emission.lamps) == MAX_LAMPS,
            "a wash with hundreds of pixels doesn't put one item on stage each")
    _assert(body.emission.emitter_index[0] == 0
            and body.emission.emitter_index[-1] < 200,
            "the lamps it does draw are spread across the whole face")
    body.cleanup()


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_features_come_from_the_channels(app)
    test_a_moving_head_without_gobos_degrades_to_a_wash(app)
    test_features_are_discovered_once(app)
    test_only_animating_bodies_hold_the_clock(app)
    test_cleanup_releases_the_animation(app)
    test_emission_reach_answers_for_the_preview(app)
    test_the_shutter_gate_is_a_pure_function_of_time(app)
    test_every_registered_feature_declares_itself(app)
    test_a_matrix_is_one_body_unless_its_pixels_are_separated(app)
    test_matrix_cells_cover_the_whole_grid(app)
    test_a_segmented_wash_lights_every_zone(app)
    test_a_wash_with_one_zone_is_unchanged(app)
    test_a_big_matrix_behind_a_lamp_is_sampled(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
