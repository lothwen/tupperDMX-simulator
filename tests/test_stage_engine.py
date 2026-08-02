"""
Characterization test: the two-stage stage engine (ui/stage/).

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
from ui.stage import CLOCK, renderer_for
from ui.stage.emission.base import emission_reach
from ui.stage.features.base import FEATURES, build_features
from ui.stage.features.beamshape import (
    BladeFeature, IrisFeature, PanTiltFeature, PrismFeature, ZoomFeature,
)
from ui.stage.features.level import ColorFeature, DimmerFeature, ShutterFeature
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
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
