"""
Characterization test: the photometric model (core/fixture_state.py).

`resolve_state` answers "what is this fixture emitting?" from its DMX slice and
its OFL capabilities alone — no Qt, no per-fixture code. The stage renderers
consume the result, so these numbers are what ends up on screen.

The parity block is the important part: it reproduces, from capabilities alone,
the exact colours the three hand-written renderers produced. Everything else
covers capability kinds the old renderers had no concept of.

One thing deliberately NOT modelled here: the blinder's `multiply_factor`
overdrive. That is a per-instance render property, not something the fixture
emits, so it stays in the renderer.

Run with:
    uv run python tests/test_fixture_state.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import ofl
from core.fixture_library import build_fixture_def, resolve_fixture
from core.fixture_state import (
    REFERENCE_RGB, kelvin_to_rgb, resolve_state,
)

SCHEMA = "https://raw.githubusercontent.com/OpenLightingProject/open-fixture-library/master/schemas/fixture.json"
META = {"authors": ["test"], "createDate": "2026-01-01", "lastModifyDate": "2026-01-01"}
WILD_WASH = "Wild Wash Pro 648 RGBW, 4ch mode"


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

def test_parity_par_rgb():
    print("test_parity_par_rgb")
    fdef = resolve_fixture("par_rgb")
    state = resolve_state(fdef, [255, 10, 20, 30])
    _assert(state.rgb == (10, 20, 30) and state.intensity == 1.0,
            "RGB read from the right channels at full brightness")

    dim = resolve_state(fdef, [128, 200, 100, 50])
    _assert(dim.scaled_rgb() == (int(200 * 128 / 255), int(100 * 128 / 255),
                                 int(50 * 128 / 255)),
            "brightness scales the mix exactly as before")

    off = resolve_state(fdef, [0, 255, 255, 255])
    _assert(off.scaled_rgb() == (0, 0, 0), "zero brightness blacks out")


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


def main():
    test_parity_par_rgb()
    test_parity_ledbar_channel_order()
    test_parity_blinder_two_cells()
    test_parity_wild_wash_white_is_a_cool_tint()
    test_parity_ledbar_effect_names()
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
    test_fine_channels_fold_into_one_value()
    test_short_dmx_slice_uses_declared_defaults()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
