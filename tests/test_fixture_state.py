"""
Characterization test: the photometric model (core/fixture_state.py).

`resolve_state` answers "what is this fixture emitting?" from its DMX slice and
its OFL capabilities alone — no Qt, no per-fixture code. The stage renderers
consume the result, so these numbers are what ends up on screen.

The parity block is the important part: it reproduces, from capabilities alone,
the exact colours the three hand-written renderers produced. Everything else
covers capability kinds the old renderers had no concept of.

Run with:
    uv run python tests/test_fixture_state.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import ofl
from core.fixture_library import build_fixture_def, resolve_fixture
from core.fixture_state import (
    REFERENCE_RGB, kelvin_to_rgb, resolve_state, solve_color,
)
import ofl_factory

SCHEMA = "https://raw.githubusercontent.com/OpenLightingProject/open-fixture-library/master/schemas/fixture.json"
META = {"authors": ["test"], "createDate": "2026-01-01", "lastModifyDate": "2026-01-01"}
WILD_WASH = "Wild Wash Pro 648 RGBW"


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _probe(channels, mode_channels=None, sidecar=None, name="probe",
           categories=("Other",), physical=None):
    """Build a FixtureDef from a channel dict, for resolving DMX against."""
    doc = {
        "$schema": SCHEMA, "name": name, "categories": list(categories),
        "meta": META, "availableChannels": channels,
        "modes": [{"name": "test", "channels": mode_channels or list(channels)}],
    }
    if physical:
        doc["physical"] = physical
    from core.fixture_sidecar import parse_sidecar
    return build_fixture_def(name, ofl.parse_ofl(doc),
                             parse_sidecar(sidecar) if sidecar else None)


# --- parity with the pre-OFL renderers ---------------------------------------

def test_parity_par_rgbw():
    print("test_parity_par_rgbw")
    fdef = resolve_fixture("par-rgbw")
    state = resolve_state(fdef, [255, 10, 20, 30, 0, 0])
    _assert(state.rgb == (10, 20, 30) and state.intensity == 1.0,
            "RGB read from the right channels at full brightness")

    dim = resolve_state(fdef, [128, 200, 100, 50, 0, 0])
    _assert(dim.scaled_rgb() == (int(200 * 128 / 255), int(100 * 128 / 255),
                                 int(50 * 128 / 255)),
            "brightness scales the mix exactly as before")

    off = resolve_state(fdef, [0, 255, 255, 255, 0, 0])
    _assert(off.scaled_rgb() == (0, 0, 0), "zero brightness blacks out")

    white = resolve_state(fdef, [255, 0, 0, 0, 255, 0])
    _assert(white.scaled_rgb() == (255, 255, 255),
            "the white emitter lights on its own")


def test_parity_ledbar_channel_order():
    print("test_parity_ledbar_channel_order")
    # The ledbar is wired B, G, R — the order every saved scene clip assumes.
    state = resolve_state(resolve_fixture("ledbar"), [255, 10, 20, 30, 200, 255, 0])
    _assert(state.rgb == (30, 20, 10),
            "channels 1..3 resolve as blue, green, red")


def test_parity_blinder_two_cells():
    print("test_parity_blinder_two_cells")
    fdef = resolve_fixture("blinder")

    warm = resolve_state(fdef, [255, 0, 0, 255, 255, 0, 0, 0])
    _assert(len(warm.emitters) == 2, "the blinder resolves to two cells")
    _assert(all(e.rgb == REFERENCE_RGB["Warm White"] for e in warm.emitters),
            "both bulbs warm — derived from repeated colour channels, not hardcoded")

    split = resolve_state(fdef, [128, 255, 0, 0, 255, 0, 0, 0])
    _assert(split.emitters[0].rgb == REFERENCE_RGB["Cold White"],
            "cool1 drives the first cell")
    _assert(split.emitters[1].rgb == REFERENCE_RGB["Warm White"],
            "warm2 drives the second — the n-th channel of a colour is the n-th cell")

    both = resolve_state(fdef, [255, 255, 255, 255, 255, 0, 0, 0])
    _assert(both.emitters[0].rgb == (255, 255, 255),
            "warm + cold together saturate to white")

    dark = resolve_state(fdef, [0, 255, 255, 255, 255, 0, 0, 0])
    _assert(all(e.scaled_rgb() == (0, 0, 0) for e in dark.emitters),
            "master brightness at zero blacks both cells out")


def test_parity_wild_wash_white_is_a_cool_tint():
    print("test_parity_wild_wash_white_is_a_cool_tint")
    fdef = resolve_fixture(WILD_WASH)
    state = resolve_state(fdef, [0, 0, 0, 255])
    _assert(state.rgb == REFERENCE_RGB["Cold White"],
            "its white channel is a cold-white emitter, as the fixture declares")
    _assert(resolve_state(fdef, [10, 20, 30, 0]).intensity == 1.0,
            "a fixture with no Intensity channel emits at full")


def test_parity_ledbar_effect_names():
    print("test_parity_ledbar_effect_names")
    fdef = resolve_fixture("ledbar")
    first = resolve_state(fdef, [255, 255, 255, 255, 0, 255, 0])
    last = resolve_state(fdef, [255, 255, 255, 255, 136, 255, 0])
    none = resolve_state(fdef, [255, 255, 255, 255, 200, 255, 0])
    _assert(fdef.effects.get(first.effect, {}).get("gif") == "01-ON.gif",
            "DMX 0 selects the first animation")
    _assert(fdef.effects.get(last.effect, {}).get("gif") == "35-strobo+rain.gif",
            "DMX 136 selects the last one")
    _assert(none.effect == "", "past the last animation, no effect runs")


# The resolve-side wall. Every one of these numbers is DMX the app already
# writes: `solve_color` inverts this same mix to drive real fixtures, and
# `note_layout.scene_rgb` paints timeline clips from it. Extending
# `resolve_state` to cover more capability types must leave all of them exactly
# where they are — a fixture that declares none of the new types can't notice
# the new code exists.
GOLDENS = [
    # (type, mode, dmx slice, intensity, rgb, scaled_rgb, per-emitter scaled rgb)
    ("par-rgbw", "", [255, 255, 0, 0, 0, 0],
     1.0, (255, 0, 0), (255, 0, 0), [(255, 0, 0)]),
    ("par-rgbw", "", [128, 200, 100, 50, 0, 0],
     128 / 255, (200, 100, 50), (100, 50, 25), [(100, 50, 25)]),
    ("par-rgbw", "", [0, 255, 255, 255, 0, 0],
     0.0, (255, 255, 255), (0, 0, 0), [(0, 0, 0)]),
    ("par-rgbw", "", [255, 0, 0, 0, 0, 0],
     1.0, (0, 0, 0), (0, 0, 0), [(0, 0, 0)]),
    ("par-rgbw", "", [255, 0, 0, 0, 255, 0],
     1.0, (255, 255, 255), (255, 255, 255), [(255, 255, 255)]),

    # The ledbar is wired B, G, R.
    ("ledbar", "", [255, 10, 20, 30, 0, 0, 0],
     1.0, (30, 20, 10), (30, 20, 10), [(30, 20, 10)]),
    ("ledbar", "", [128, 255, 255, 255, 0, 255, 0],
     128 / 255, (255, 255, 255), (128, 128, 128), [(128, 128, 128)]),
    ("ledbar", "", [0, 255, 0, 0, 0, 0, 0],
     0.0, (0, 0, 255), (0, 0, 0), [(0, 0, 0)]),

    # Two cells, cool white on the first pair of channels and warm on the second.
    ("blinder", "", [255, 255, 255, 0, 0, 0],
     1.0, (200, 220, 255), (200, 220, 255), [(200, 220, 255), (200, 220, 255)]),
    ("blinder", "", [128, 255, 0, 0, 0, 0],
     128 / 255, (200, 220, 255), (100, 110, 128), [(100, 110, 128), (0, 0, 0)]),
    ("blinder", "", [255, 0, 0, 255, 255, 0],
     1.0, (255, 200, 100), (255, 200, 100), [(255, 200, 100), (255, 200, 100)]),
    ("blinder", "", [64, 128, 64, 32, 16, 0],
     64 / 255, (132, 135, 140), (33, 33, 35), [(33, 33, 35), (16, 16, 17)]),

    # No intensity channel at all: the colour channels are the only level control.
    (WILD_WASH, "", [255, 0, 0, 0], 1.0, (255, 0, 0), (255, 0, 0), [(255, 0, 0)]),
    (WILD_WASH, "", [0, 0, 0, 255],
     1.0, (200, 220, 255), (200, 220, 255), [(200, 220, 255)]),
    (WILD_WASH, "", [128, 64, 32, 16],
     1.0, (140, 77, 48), (140, 77, 48), [(140, 77, 48)]),
]


def test_shipped_fixtures_resolve_goldens():
    print("test_shipped_fixtures_resolve_goldens")
    for name, mode, dmx, intensity, rgb, scaled, emitters in GOLDENS:
        state = resolve_state(resolve_fixture(name, mode), dmx)
        label = f"{name} {dmx}"
        _assert(abs(state.intensity - intensity) < 1e-6, f"{label}: intensity")
        _assert(state.rgb == rgb, f"{label}: rgb {state.rgb} == {rgb}")
        _assert(state.scaled_rgb() == scaled, f"{label}: scaled {scaled}")
        _assert([e.scaled_rgb() for e in state.emitters] == emitters,
                f"{label}: {len(emitters)} emitter(s)")


# --- colour mixing -----------------------------------------------------------

def test_all_reference_colours_mix():
    print("test_all_reference_colours_mix")
    for colour, expected in REFERENCE_RGB.items():
        fdef = _probe({"c": {"capability": {"type": "ColorIntensity", "color": colour}}})
        state = resolve_state(fdef, [255])
        _assert(state.rgb == expected, f"ColorIntensity {colour} emits {expected}")
        _assert(resolve_state(fdef, [0]).rgb == (0, 0, 0), f"{colour} at 0 emits nothing")


def test_colours_mix_additively_and_clamp():
    print("test_colours_mix_additively_and_clamp")
    fdef = _probe({
        "r": {"capability": {"type": "ColorIntensity", "color": "Red"}},
        "w": {"capability": {"type": "ColorIntensity", "color": "White"}},
    })
    _assert(resolve_state(fdef, [255, 255]).rgb == (255, 255, 255),
            "red + white saturates rather than overflowing")
    _assert(resolve_state(fdef, [128, 0]).rgb == (128, 0, 0),
            "a half-open channel contributes half its colour")


def test_pure_dimmer_emits_white():
    print("test_pure_dimmer_emits_white")
    fdef = _probe({"d": {"capability": {"type": "Intensity"}}})
    state = resolve_state(fdef, [128])
    _assert(not state.has_color, "a fixture with no colour channel has no colour")
    _assert(state.rgb == (255, 255, 255), "so it emits white")
    _assert(abs(state.intensity - 128 / 255) < 1e-6, "at its dimmer's level")


def test_declared_brightness_beats_dmx_position():
    print("test_declared_brightness_beats_dmx_position")
    # A capability that states its own brightness range is authoritative; the
    # raw DMX position is only the fallback.
    fdef = _probe({"d": {"capabilities": [
        {"dmxRange": [0, 255], "type": "Intensity",
         "brightnessStart": "0%", "brightnessEnd": "50%"}]}})
    _assert(abs(resolve_state(fdef, [255]).intensity - 0.5) < 1e-6,
            "full DMX on a half-brightness capability emits half")


def test_colour_preset_and_temperature():
    print("test_colour_preset_and_temperature")
    preset = _probe({"c": {"capabilities": [
        {"dmxRange": [0, 127], "type": "ColorPreset", "colors": ["#ff0000"]},
        {"dmxRange": [128, 255], "type": "ColorPreset", "colors": ["#0000ff"]}]}})
    _assert(resolve_state(preset, [0]).rgb == (255, 0, 0), "a preset sets its colour")
    _assert(resolve_state(preset, [200]).rgb == (0, 0, 255), "and switches by range")

    ramp = _probe({"c": {"capabilities": [
        {"dmxRange": [0, 255], "type": "ColorPreset",
         "colorsStart": ["#000000"], "colorsEnd": ["#ffffff"]}]}})
    _assert(resolve_state(ramp, [128]).rgb == (128, 128, 128),
            "a start/end pair interpolates across the range")

    warm = _probe({"c": {"capability": {"type": "ColorTemperature",
                                        "colorTemperature": "2800K"}}})
    state = resolve_state(warm, [128])
    _assert(state.color_temp_k == 2800, "colour temperature is resolved")
    _assert(state.rgb == kelvin_to_rgb(2800),
            "and tints the output when there is no colour mix")
    _assert(state.rgb[0] > state.rgb[2], "2800K is warm — more red than blue")


def test_kelvin_to_rgb_direction():
    print("test_kelvin_to_rgb_direction")
    warm, cold = kelvin_to_rgb(2000), kelvin_to_rgb(9000)
    _assert(warm[0] >= warm[2], "a low temperature is red-dominant")
    _assert(cold[2] >= cold[0], "a high temperature is blue-dominant")


# --- shutter, effect, beam ---------------------------------------------------

def test_shutter_and_strobe():
    print("test_shutter_and_strobe")
    fdef = _probe({
        "d": {"capability": {"type": "Intensity"}},
        "s": {"capabilities": [
            {"dmxRange": [0, 7], "type": "ShutterStrobe", "shutterEffect": "Open"},
            {"dmxRange": [8, 250], "type": "ShutterStrobe", "shutterEffect": "Strobe",
             "speedStart": "0Hz", "speedEnd": "20Hz"},
            {"dmxRange": [251, 255], "type": "ShutterStrobe", "shutterEffect": "Closed"}]},
    })
    open_state = resolve_state(fdef, [255, 0])
    _assert(open_state.shutter == "Open" and open_state.intensity == 1.0,
            "an open shutter passes the dimmer through")

    strobing = resolve_state(fdef, [255, 250])
    _assert(strobing.shutter == "Strobe" and abs(strobing.strobe_hz - 20.0) < 0.1,
            "a strobe reports its rate in Hz")

    closed = resolve_state(fdef, [255, 255])
    _assert(closed.intensity == 0.0,
            "a closed shutter emits nothing however open the dimmer is")


def test_effect_and_speed():
    print("test_effect_and_speed")
    fdef = _probe({
        "e": {"capabilities": [
            {"dmxRange": [0, 127], "type": "Effect", "effectName": "Rainbow",
             "speedStart": "slow", "speedEnd": "fast"},
            {"dmxRange": [128, 255], "type": "NoFunction"}]},
        "s": {"capability": {"type": "EffectSpeed", "speedStart": "0Hz",
                             "speedEnd": "10Hz"}},
    })
    running = resolve_state(fdef, [0, 255])
    _assert(running.effect == "Rainbow", "the running effect is named")
    _assert(running.effect_dmx == 0, "its raw DMX value is kept for the renderer")
    _assert(abs(running.effect_speed_hz - 10.0) < 1e-6,
            "a dedicated speed channel overrides the effect's own speed")
    _assert(resolve_state(fdef, [200, 0]).effect == "",
            "a NoFunction range runs no effect")


def test_beam_and_zoom():
    print("test_beam_and_zoom")
    fdef = _probe({"z": {"capability": {"type": "Zoom", "angleStart": "10deg",
                                        "angleEnd": "60deg"}}},
                  physical={"lens": {"degreesMinMax": [10, 60]}})
    _assert(resolve_state(fdef, [0]).beam_deg == (10.0, 60.0),
            "the lens' angle range is carried for the beam cone")
    _assert(abs(resolve_state(fdef, [255]).zoom_deg - 60.0) < 1e-6,
            "a zoom channel reports the current angle")
    _assert(resolve_state(_probe({"d": {"capability": {"type": "Intensity"}}}),
                          [255]).beam_deg is None,
            "a fixture with no lens declares no beam")


# --- emitters ----------------------------------------------------------------

def test_matrix_pixels_become_emitters():
    print("test_matrix_pixels_become_emitters")
    doc = {
        "$schema": SCHEMA, "name": "grid", "categories": ["Matrix"], "meta": META,
        "matrix": {"pixelCount": [3, 1, 1]},
        "availableChannels": {"d": {"capability": {"type": "Intensity"}}},
        "templateChannels": {
            "$pixelKey Red": {"capability": {"type": "ColorIntensity", "color": "Red"}}},
        "modes": [{"name": "3ch", "channels": [
            {"insert": "matrixChannels", "repeatFor": "eachPixelXYZ",
             "channelOrder": "perPixel", "templateChannels": ["$pixelKey Red"]}]}],
    }
    fdef = build_fixture_def("grid", ofl.parse_ofl(doc))
    state = resolve_state(fdef, [255, 128, 0])
    _assert(len(state.emitters) == 3, "one emitter per matrix pixel")
    _assert([e.key for e in state.emitters] == ["1", "2", "3"],
            "each emitter knows its pixel")
    _assert([e.rgb for e in state.emitters] == [(255, 0, 0), (128, 0, 0), (0, 0, 0)],
            "each pixel takes its own channel's value")


def test_declared_cell_count_pads_emitters():
    print("test_declared_cell_count_pads_emitters")
    fdef = _probe({"w": {"capability": {"type": "ColorIntensity", "color": "White"}}},
                  sidecar={"tdmx": 1, "modes": {"*": {"stage": {"cells": 3}}}})
    state = resolve_state(fdef, [255])
    _assert(len(state.emitters) == 3,
            "a declared cell count wins when the channels imply fewer")
    _assert(state.emitters[0].rgb == (255, 255, 255)
            and state.emitters[1].rgb == (0, 0, 0),
            "only the driven cell lights")


def test_per_pixel_intensity():
    print("test_per_pixel_intensity")
    # A matrix whose template gives every pixel its own dimmer. Before, the last
    # such channel became the fixture's one global master and the others were
    # simply lost.
    fdef = ofl_factory.fdef(
        matrix={"pixelCount": [3, 1, 1]},
        channels={},
        template_channels={"$pixelKey dim": ofl_factory.channel("Intensity")},
        modes=[{"name": "test", "channels": [
            {"insert": "matrixChannels", "repeatFor": "eachPixelXYZ",
             "channelOrder": "perPixel",
             "templateChannels": ["$pixelKey dim"]}]}])

    state = resolve_state(fdef, [255, 128, 0])
    levels = [round(e.intensity, 3) for e in state.emitters]
    _assert(levels == [1.0, round(128 / 255, 3), 0.0],
            "each pixel keeps its own level instead of the last one winning")
    _assert(state.intensity == 1.0,
            "the fixture-wide level is the brightest cell")


def test_repeated_dimmers_are_repeated_cells():
    print("test_repeated_dimmers_are_repeated_cells")
    # No matrix, two plain dimmers: the same rule the repeated colour channels
    # follow, which is how a two-cell fixture resolves with no per-fixture code.
    fdef = ofl_factory.fdef(channels={
        "a": ofl_factory.channel("Intensity"),
        "b": ofl_factory.channel("Intensity")})
    state = resolve_state(fdef, [255, 0])
    _assert([e.intensity for e in state.emitters] == [1.0, 0.0],
            "the n-th dimmer is the n-th cell")

    # But one dimmer is still the whole fixture's master — the path every
    # shipped fixture takes, and the one the goldens pin.
    single = resolve_state(ofl_factory.fdef(
        channels={"a": ofl_factory.channel("Intensity")}), [128])
    _assert(len(single.emitters) == 1 and abs(single.intensity - 128 / 255) < 1e-6,
            "one dimmer is a master, not a cell")


def test_pixel_group_master_dims_its_members():
    print("test_pixel_group_master_dims_its_members")
    fdef = ofl_factory.fdef(
        matrix={"pixelCount": [4, 1, 1],
                "pixelGroups": {"Left": ["1", "2"], "Right": ["3", "4"]}},
        channels={},
        template_channels={"$pixelKey dim": ofl_factory.channel("Intensity")},
        modes=[{"name": "test", "channels": [
            {"insert": "matrixChannels", "repeatFor": "eachPixelXYZ",
             "channelOrder": "perPixel", "templateChannels": ["$pixelKey dim"]},
            "Left dim", "Right dim"]}])

    state = resolve_state(fdef, [255, 255, 255, 255, 128, 0])
    levels = [round(e.intensity, 3) for e in state.emitters]
    _assert(levels[:2] == [round(128 / 255, 3)] * 2,
            "the Left master scales both of its pixels")
    _assert(levels[2:] == [0.0, 0.0], "and the Right master blacks its own out")


def test_switching_channel_reaches_the_new_handlers():
    print("test_switching_channel_reaches_the_new_handlers")
    # One address that is a pan channel in one program and a gobo wheel in
    # another. The dispatch keys off the LIVE capability, so the same slot has
    # to route to two different handlers depending on the trigger.
    fdef = ofl_factory.fdef(
        wheels={"Gobo Wheel": {"slots": [
            {"type": "Open"}, {"type": "Gobo", "resource": "gobos/dots",
                               "name": "Dots"}]}},
        channels={
            "mode": ofl_factory.ranged(
                ofl_factory.cap("Effect", [0, 127], effectName="Position",
                                switchChannels={"dual": "pan"}),
                ofl_factory.cap("Effect", [128, 255], effectName="Gobo",
                                switchChannels={"dual": "gobo"})),
            "pan": ofl_factory.channel("Pan", angleStart="0deg", angleEnd="540deg"),
            "gobo": ofl_factory.channel("WheelSlot", wheel="Gobo Wheel",
                                        slotNumber=2),
        },
        mode_channels=["mode", "dual"])

    moving = resolve_state(fdef, [0, 255])
    _assert(abs(moving.motion.pan_deg - 540.0) < 1e-6,
            "under the first trigger the slot is a pan channel")
    _assert(not moving.wheels, "and no wheel is in the beam")

    gobos = resolve_state(fdef, [255, 255])
    _assert(gobos.motion.pan_deg is None, "under the second it stops being pan")
    _assert(gobos.wheels["Gobo Wheel"].slot.name == "Dots",
            "and becomes a gobo wheel at the same address")


def test_legacy_fields_are_views_of_the_groups():
    print("test_legacy_fields_are_views_of_the_groups")
    # The flat names predate the grouped records and are what the timeline, the
    # clip library and solve_color read. They have to keep agreeing.
    fdef = ofl_factory.fdef(channels={
        "d": ofl_factory.channel("Intensity"),
        "r": ofl_factory.channel("ColorIntensity", color="Red"),
        "s": ofl_factory.channel("ShutterStrobe", shutterEffect="Strobe",
                                 speed="9Hz"),
        "e": ofl_factory.channel("Effect", effectName="Rainbow"),
        "z": ofl_factory.channel("Zoom", angleStart="10deg", angleEnd="40deg"),
    }, physical={"lens": {"degreesMinMax": [10, 40]}})
    state = resolve_state(fdef, [255, 255, 255, 255, 255])

    pairs = [
        ("rgb", state.color.rgb), ("has_color", state.color.has_color),
        ("color_temp_k", state.color.color_temp_k),
        ("emitters", state.color.emitters),
        ("shutter", state.strobe.effect), ("strobe_hz", state.strobe.hz),
        ("effect", state.program.name), ("effect_dmx", state.program.dmx),
        ("effect_speed_hz", state.program.speed_hz),
        ("zoom_deg", state.optics.zoom_deg), ("beam_deg", state.optics.lens_deg),
    ]
    for name, grouped in pairs:
        _assert(getattr(state, name) == grouped, f"state.{name} is its group's view")

    _assert(state.shutter == "Strobe" and state.effect == "Rainbow"
            and state.beam_deg == (10.0, 40.0),
            "and the views carry the values a renderer expects")


# --- value folding -----------------------------------------------------------

def test_fine_channels_fold_into_one_value():
    print("test_fine_channels_fold_into_one_value")
    fdef = _probe({"d": {"fineChannelAliases": ["d fine"],
                         "capability": {"type": "Intensity"}}},
                  mode_channels=["d", "d fine"])
    coarse_only = resolve_state(fdef, [255, 0])
    full = resolve_state(fdef, [255, 255])
    _assert(full.intensity == 1.0, "both bytes at max is full intensity")
    _assert(coarse_only.intensity < full.intensity,
            "the fine byte adds resolution below the coarse step")
    _assert(len(full.emitters) == 1, "a fine byte is not a separate emitter")


def test_short_dmx_slice_uses_declared_defaults():
    print("test_short_dmx_slice_uses_declared_defaults")
    fdef = _probe({
        "d": {"defaultValue": 255, "capability": {"type": "Intensity"}},
        "r": {"defaultValue": 255,
              "capability": {"type": "ColorIntensity", "color": "Red"}},
    })
    state = resolve_state(fdef, [128])   # only the first channel arrived
    _assert(state.rgb == (255, 0, 0),
            "a channel the frame doesn't reach falls back to its declared default")


def test_switching_channel_changes_what_a_slot_means():
    print("test_switching_channel_changes_what_a_slot_means")
    # Address 3 is a strobe while "Mode" is low and an effect speed while it is
    # high — the real shape of Chauvet/Eurolite "Strobe / Speed" channels. Read
    # with the wrong capability list, the fixture strobes when it should fade.
    fdef = _probe({
        "Dimmer": {"capability": {"type": "Intensity"}},
        "Mode": {"capabilities": [
            {"dmxRange": [0, 127], "type": "Effect", "effectName": "Strobing",
             "switchChannels": {"Strobe / Speed": "Strobe"}},
            {"dmxRange": [128, 255], "type": "Effect", "effectName": "Chase",
             "switchChannels": {"Strobe / Speed": "Effect Speed"}}]},
        "Strobe": {"capability": {"type": "ShutterStrobe", "shutterEffect": "Strobe",
                                  "speedStart": "0Hz", "speedEnd": "20Hz"}},
        "Effect Speed": {"capability": {"type": "EffectSpeed",
                                        "speedStart": "0Hz", "speedEnd": "10Hz"}},
    }, mode_channels=["Dimmer", "Mode", "Strobe / Speed"])

    strobing = resolve_state(fdef, [255, 0, 255])
    _assert(strobing.strobe_hz == 20.0,
            "trigger low -> the shared address reads as a 20Hz strobe")

    chasing = resolve_state(fdef, [255, 200, 255])
    _assert(chasing.strobe_hz is None,
            "trigger high -> the same address is no longer a strobe at all")
    _assert(chasing.effect_speed_hz == 10.0,
            "trigger high -> it reads as effect speed instead")


# --- the write direction: solve_color ----------------------------------------

def _colour_probe(*colours):
    return _probe({c: {"capability": {"type": "ColorIntensity", "color": c}}
                   for c in colours}, mode_channels=list(colours))


def test_solve_color_writes_rgb_straight_through():
    print("test_solve_color_writes_rgb_straight_through")
    fdef = _colour_probe("Red", "Green", "Blue")
    _assert(solve_color(fdef, (255, 120, 0)) == {0: 255, 1: 120, 2: 0},
            "an RGB fixture gets the components on their own channels")

    # The ledbar's channels are ordered B, G, R — the components follow the
    # colours, never the positions.
    ledbar = resolve_fixture("ledbar")
    _assert(solve_color(ledbar, (10, 20, 30)) == {1: 30, 2: 20, 3: 10},
            "...whatever order the fixture declares them in")


def test_solve_color_zeroes_the_other_emitters():
    print("test_solve_color_zeroes_the_other_emitters")
    # An RGBAW fixture must not keep the previous scene's amber lit under the
    # new colour — the role-based code only ever touched R/G/B and left it.
    fdef = _colour_probe("Red", "Green", "Blue", "Amber", "White")
    _assert(solve_color(fdef, (255, 0, 0)) == {0: 255, 1: 0, 2: 0, 3: 0, 4: 0},
            "every colour the fixture has is driven, not just the three")


def test_solve_color_fits_a_fixture_without_rgb():
    print("test_solve_color_fits_a_fixture_without_rgb")
    # 27 of the installed fixtures mix CMY and have no red channel at all; the
    # role enum had no name for any of them, so they never lit.
    fdef = _colour_probe("Cyan", "Magenta", "Yellow")
    solved = solve_color(fdef, (255, 0, 0))
    _assert(set(solved) == {0, 1, 2} and any(solved.values()),
            "a CMY fixture is driven rather than skipped")

    lit = resolve_state(fdef, [solved[i] for i in range(3)]).scaled_rgb()
    _assert(lit[0] > lit[1] and lit[0] > lit[2],
            f"...and lands on a red-dominant mix, not on nothing ({lit})")


def test_solve_color_leaves_a_white_only_fixture_alone():
    print("test_solve_color_leaves_a_white_only_fixture_alone")
    # A palette is a hue, and a blinder has none to set. Driving its whites from
    # a colour would hand the fixture to the colour cycler mid-song.
    _assert(solve_color(resolve_fixture("blinder"), (255, 0, 0)) == {},
            "a warm/cold white blinder is not a colour mixer")
    _assert(solve_color(_probe({"Dimmer": {"capability": {"type": "Intensity"}}}),
                        (255, 0, 0)) == {},
            "neither is a fixture with no colour channels at all")


def test_solve_color_round_trips_through_resolve_state():
    print("test_solve_color_round_trips_through_resolve_state")
    fdef = _colour_probe("Red", "Green", "Blue", "White")
    for target in ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255),
                   (128, 64, 200)):
        solved = solve_color(fdef, target)
        lit = resolve_state(fdef, [solved[i] for i in range(4)]).scaled_rgb()
        _assert(lit == target, f"{target} survives the round trip")


def main():
    test_parity_par_rgbw()
    test_parity_ledbar_channel_order()
    test_parity_blinder_two_cells()
    test_parity_wild_wash_white_is_a_cool_tint()
    test_parity_ledbar_effect_names()
    test_shipped_fixtures_resolve_goldens()
    test_all_reference_colours_mix()
    test_colours_mix_additively_and_clamp()
    test_pure_dimmer_emits_white()
    test_declared_brightness_beats_dmx_position()
    test_colour_preset_and_temperature()
    test_kelvin_to_rgb_direction()
    test_shutter_and_strobe()
    test_effect_and_speed()
    test_beam_and_zoom()
    test_matrix_pixels_become_emitters()
    test_declared_cell_count_pads_emitters()
    test_per_pixel_intensity()
    test_repeated_dimmers_are_repeated_cells()
    test_pixel_group_master_dims_its_members()
    test_switching_channel_reaches_the_new_handlers()
    test_legacy_fields_are_views_of_the_groups()
    test_fine_channels_fold_into_one_value()
    test_short_dmx_slice_uses_declared_defaults()
    test_switching_channel_changes_what_a_slot_means()
    test_solve_color_writes_rgb_straight_through()
    test_solve_color_zeroes_the_other_emitters()
    test_solve_color_fits_a_fixture_without_rgb()
    test_solve_color_leaves_a_white_only_fixture_alone()
    test_solve_color_round_trips_through_resolve_state()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
