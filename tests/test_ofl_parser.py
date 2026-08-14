"""
Characterization test: the Open Fixture Library document parser (core/ofl.py).

Covers the parts of the OFL schema with real edge cases behind them — mode
resolution (null slots, fine channels, matrix inserts), capability range lookup,
pixel ordering and the entity/unit vocabulary. Nothing here touches the app: the
parser is pure and filesystem-free.

The matrix-order table is the fiddliest piece and the one most worth pinning:
for `eachPixelXYZ` the LAST axis letter is the primary sort key, so pixels come
out like reading a book — X varies fastest, Z slowest.

Run with:
    uv run python tests/test_ofl_parser.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.ofl import (
    Capability, parse_ofl, resolve_mode, parse_entity, capability_value, interp,
    slot_at, wheel_names, SPEED_MAX_HZ, CT_WARM_K, CT_DEFAULT_K, CT_COLD_K,
)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


SCHEMA = "https://raw.githubusercontent.com/OpenLightingProject/open-fixture-library/master/schemas/fixture.json"
META = {"authors": ["test"], "createDate": "2026-01-01", "lastModifyDate": "2026-01-01"}


def _fixture(**overrides):
    """A minimal valid OFL document, with overrides merged in."""
    doc = {
        "$schema": SCHEMA,
        "name": "Test Fixture",
        "categories": ["Color Changer"],
        "meta": META,
        "availableChannels": {
            "Intensity": {"capability": {"type": "Intensity"}},
            "Red": {"capability": {"type": "ColorIntensity", "color": "Red"}},
        },
        "modes": [{"name": "2ch", "channels": ["Intensity", "Red"]}],
    }
    doc.update(overrides)
    return doc


def _keys(mode):
    return [c.key for c in mode.channels]


# --- document level ----------------------------------------------------------

def test_rejects_non_ofl_documents():
    print("test_rejects_non_ofl_documents")
    # The legacy TupperDMX format, which must be refused now that OFL is the
    # only format — this is the hard cut's user-facing surface.
    legacy = {"name": "ledbar", "render": "bar",
              "channels": [{"field": "brightness", "role": "brightness"}]}
    _assert(parse_ofl(legacy) is None, "legacy fixture def rejected")
    _assert(parse_ofl({}) is None, "empty dict rejected")
    _assert(parse_ofl([]) is None, "non-dict rejected")
    _assert(parse_ofl(_fixture(categories=[])) is None, "missing categories rejected")
    no_channels = _fixture()
    del no_channels["availableChannels"]
    _assert(parse_ofl(no_channels) is None,
            "document without availableChannels or templateChannels rejected")


def test_parses_identity_and_physical():
    print("test_parses_identity_and_physical")
    fx = parse_ofl(_fixture(
        shortName="TF",
        categories=["Pixel Bar", "Strobe"],
        physical={"dimensions": [50, 1000, 60], "weight": 3.5, "power": 90,
                  "bulb": {"type": "LED", "colorTemperature": 6500},
                  "lens": {"name": "PC", "degreesMinMax": [10, 60]},
                  "matrixPixels": {"spacing": [440, 0, 0]}}))
    _assert(fx.name == "Test Fixture" and fx.short_name == "TF", "name + shortName")
    _assert(fx.categories == ("Pixel Bar", "Strobe"), "categories keep their order")
    _assert(fx.physical.dimensions_mm == (50.0, 1000.0, 60.0), "dimensions parsed")
    _assert(fx.physical.lens_degrees == (10.0, 60.0), "lens degreesMinMax parsed")
    _assert(fx.physical.bulb_color_temp_k == 6500, "bulb colour temperature parsed")
    _assert(fx.physical.pixel_spacing_mm == (440.0, 0.0, 0.0), "pixel spacing parsed")


def test_mode_lookup_falls_back_to_first():
    print("test_mode_lookup_falls_back_to_first")
    fx = parse_ofl(_fixture(modes=[
        {"name": "2ch", "shortName": "2", "channels": ["Intensity", "Red"]},
        {"name": "1ch", "channels": ["Red"]},
    ]))
    _assert(resolve_mode(fx, "").name == "2ch", "empty mode name -> first mode")
    _assert(resolve_mode(fx, "nope").name == "2ch", "unknown mode name -> first mode")
    _assert(resolve_mode(fx, "1ch").name == "1ch", "mode found by name")
    _assert(resolve_mode(fx, "2").name == "2ch", "mode found by shortName")
    _assert(resolve_mode(fx, "1CH").name == "1ch", "mode lookup is case-insensitive")


def test_mode_physical_overrides_fixture_physical():
    print("test_mode_physical_overrides_fixture_physical")
    fx = parse_ofl(_fixture(
        physical={"dimensions": [10, 10, 10], "power": 50},
        modes=[{"name": "2ch", "channels": ["Intensity", "Red"],
                "physical": {"power": 90}}]))
    mode = resolve_mode(fx)
    _assert(mode.physical.power_w == 90, "mode physical overrides its key")
    _assert(mode.physical.dimensions_mm == (10.0, 10.0, 10.0),
            "keys the mode doesn't set are inherited")


# --- mode channel resolution -------------------------------------------------

def test_null_and_unknown_slots_consume_addresses():
    print("test_null_and_unknown_slots_consume_addresses")
    fx = parse_ofl(_fixture(modes=[
        {"name": "4ch", "channels": ["Intensity", None, "Nonexistent", "Red"]}]))
    mode = resolve_mode(fx)
    _assert(mode.channel_count == 4, "the DMX footprint counts every slot")
    _assert(mode.channels[1].key == "" and mode.channels[1].channel is None,
            "a null slot is empty but present")
    _assert(mode.channels[2].channel is None and mode.channels[2].key == "Nonexistent",
            "an unresolvable key keeps its address rather than shifting the patch")
    _assert(mode.channels[3].key == "Red", "later channels keep their address")


def test_fine_channels():
    print("test_fine_channels")
    fx = parse_ofl(_fixture(
        availableChannels={
            "Pan": {"fineChannelAliases": ["Pan fine", "Pan ultra"],
                    "capability": {"type": "Pan", "angleStart": "0deg",
                                   "angleEnd": "540deg"}},
        },
        modes=[{"name": "3ch", "channels": ["Pan", "Pan fine", "Pan ultra"]}]))
    mode = resolve_mode(fx)
    coarse, fine1, fine2 = mode.channels
    _assert(coarse.channel.resolution_bytes == 3, "two fine aliases -> 24-bit")
    _assert(coarse.channel.max_value == 256 ** 3 - 1, "max value follows the resolution")
    _assert(fine1.fine_of == 0 and fine2.fine_of == 0,
            "both fine slots point at the coarse slot")
    # Coarse holds the top byte (shift 16), so the fine bytes shift 8 and 0.
    _assert(fine1.fine_shift == 8 and fine2.fine_shift == 0,
            "fine bytes carry their own shift, most significant first")


def test_dmx_value_resolution_overrides_alias_count():
    print("test_dmx_value_resolution_overrides_alias_count")
    fx = parse_ofl(_fixture(
        availableChannels={
            "Dimmer": {"fineChannelAliases": ["Dimmer fine"],
                       "dmxValueResolution": "16bit",
                       "capability": {"type": "Intensity"}}},
        modes=[{"name": "1ch", "channels": ["Dimmer"]}]))
    channel = resolve_mode(fx).channels[0].channel
    _assert(channel.resolution_bytes == 2, "dmxValueResolution honoured")


def test_default_and_highlight_values():
    print("test_default_and_highlight_values")
    fx = parse_ofl(_fixture(
        availableChannels={
            "A": {"defaultValue": 200, "highlightValue": "100%", "constant": True,
                  "precedence": "HTP", "capability": {"type": "Intensity"}}},
        modes=[{"name": "1ch", "channels": ["A"]}]))
    channel = resolve_mode(fx).channels[0].channel
    _assert(channel.default_value == 200, "integer defaultValue parsed")
    _assert(channel.highlight_value == 255, "percent highlightValue scaled to the range")
    _assert(channel.constant and channel.precedence == "HTP",
            "constant + precedence carried")


# --- capabilities ------------------------------------------------------------

def test_single_capability_spans_the_whole_range():
    print("test_single_capability_spans_the_whole_range")
    fx = parse_ofl(_fixture())
    channel = resolve_mode(fx).channels[0].channel
    _assert(len(channel.capabilities) == 1, "one capability")
    _assert(channel.capabilities[0].dmx_range == (0, 255),
            "a bare `capability` covers 0..255")
    _assert(channel.capability_at(0) is not None and channel.capability_at(255) is not None,
            "lookup works at both ends")


def test_capability_range_lookup_at_boundaries():
    print("test_capability_range_lookup_at_boundaries")
    fx = parse_ofl(_fixture(
        availableChannels={
            "Shutter": {"capabilities": [
                {"dmxRange": [0, 7], "type": "ShutterStrobe", "shutterEffect": "Open"},
                {"dmxRange": [8, 200], "type": "ShutterStrobe", "shutterEffect": "Strobe",
                 "speedStart": "0Hz", "speedEnd": "20Hz"},
            ]}},
        modes=[{"name": "1ch", "channels": ["Shutter"]}]))
    channel = resolve_mode(fx).channels[0].channel
    _assert(channel.capability_at(7).get("shutterEffect") == "Open", "upper bound inclusive")
    _assert(channel.capability_at(8).get("shutterEffect") == "Strobe", "lower bound inclusive")
    _assert(channel.capability_at(201) is None, "a gap above the last range resolves to None")

    strobe = channel.capability_at(200)
    _assert(capability_value(strobe, 8, "speed", "speed") == 0.0, "speedStart at the low end")
    _assert(capability_value(strobe, 200, "speed", "speed") == 20.0, "speedEnd at the high end")
    _assert(abs(capability_value(strobe, 104, "speed", "speed") - 10.0) < 0.1,
            "speed interpolates across the range")


def test_capability_value_falls_back_to_the_flat_form():
    print("test_capability_value_falls_back_to_the_flat_form")
    fx = parse_ofl(_fixture(
        availableChannels={
            "Colour": {"capability": {"type": "ColorTemperature",
                                      "colorTemperature": "6500K"}}},
        modes=[{"name": "1ch", "channels": ["Colour"]}]))
    cap = resolve_mode(fx).channels[0].channel.capability_at(128)
    _assert(capability_value(cap, 128, "colorTemperature", "colorTemperature") == 6500.0,
            "a flat value is used when there is no Start/End pair")


def test_unknown_capability_type_is_preserved():
    print("test_unknown_capability_type_is_preserved")
    fx = parse_ofl(_fixture(
        availableChannels={
            "X": {"capability": {"type": "SomethingNewInSchema13", "wat": 5}}},
        modes=[{"name": "1ch", "channels": ["X"]}]))
    cap = resolve_mode(fx).channels[0].channel.capabilities[0]
    _assert(cap.type == "SomethingNewInSchema13", "unknown type kept verbatim")
    _assert(cap.get("wat") == 5, "its properties survive — we consume, not validate")


# --- matrices ----------------------------------------------------------------

def _matrix_fixture(**overrides):
    """A 3x2x1 matrix with Red/Green template channels."""
    doc = _fixture(
        matrix={"pixelCount": [3, 2, 1]},
        templateChannels={
            "$pixelKey Red": {"capability": {"type": "ColorIntensity", "color": "Red"}},
            "$pixelKey Green": {"capability": {"type": "ColorIntensity", "color": "Green"}},
        })
    doc.update(overrides)
    return doc


def _insert(repeat_for, channel_order, templates=("$pixelKey Red", "$pixelKey Green")):
    return {"insert": "matrixChannels", "repeatFor": repeat_for,
            "channelOrder": channel_order, "templateChannels": list(templates)}


def test_matrix_pixel_keys_generated_from_pixel_count():
    print("test_matrix_pixel_keys_generated_from_pixel_count")
    fx = parse_ofl(_matrix_fixture())
    _assert(fx.matrix.pixel_count == (3, 2, 1), "pixelCount parsed")
    # Two axes > 1 -> coordinate-pair keys.
    _assert(fx.matrix.keys_in_order("eachPixelXYZ") ==
            ("(1, 1)", "(2, 1)", "(3, 1)", "(1, 2)", "(2, 2)", "(3, 2)"),
            "2D matrix gets (x, y) keys")

    one_d = parse_ofl(_fixture(matrix={"pixelCount": [4, 1, 1]},
                               templateChannels={"$pixelKey Red": {
                                   "capability": {"type": "Intensity"}}}))
    _assert(one_d.matrix.keys_in_order("eachPixelXYZ") == ("1", "2", "3", "4"),
            "1D matrix gets bare numbers")


def test_matrix_axis_orders():
    print("test_matrix_axis_orders")
    # 2x2x2 so every axis order produces a distinct sequence.
    fx = parse_ofl(_fixture(
        matrix={"pixelCount": [2, 2, 2]},
        templateChannels={"$pixelKey Red": {"capability": {"type": "Intensity"}}}))
    order = fx.matrix.keys_in_order

    # From the OFL docs: "like reading a book" — X fastest, then Y, then Z.
    _assert(order("eachPixelXYZ") == ("(1, 1, 1)", "(2, 1, 1)", "(1, 2, 1)", "(2, 2, 1)",
                                      "(1, 1, 2)", "(2, 1, 2)", "(1, 2, 2)", "(2, 2, 2)"),
            "eachPixelXYZ: X varies fastest, Z slowest")
    _assert(order("eachPixelZYX") == ("(1, 1, 1)", "(1, 1, 2)", "(1, 2, 1)", "(1, 2, 2)",
                                      "(2, 1, 1)", "(2, 1, 2)", "(2, 2, 1)", "(2, 2, 2)"),
            "eachPixelZYX: Z varies fastest, X slowest")
    _assert(order("eachPixelYXZ") == ("(1, 1, 1)", "(1, 2, 1)", "(2, 1, 1)", "(2, 2, 1)",
                                      "(1, 1, 2)", "(1, 2, 2)", "(2, 1, 2)", "(2, 2, 2)"),
            "eachPixelYXZ: Y fastest, then X, then Z")
    # Every order is a permutation of the same key set.
    for axes in ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"):
        _assert(sorted(order(f"eachPixel{axes}")) == sorted(order("eachPixelXYZ")),
                f"eachPixel{axes} is a permutation of the same pixels")
    _assert(order("eachPixelABC") == tuple(sorted(order("eachPixelXYZ"))),
            "eachPixelABC sorts by key name")
    _assert(order(["(2, 2, 2)", "(1, 1, 1)"]) == ("(2, 2, 2)", "(1, 1, 1)"),
            "an explicit list is used verbatim")


def test_matrix_insert_channel_order():
    print("test_matrix_insert_channel_order")
    per_pixel = parse_ofl(_matrix_fixture(modes=[
        {"name": "6ch", "channels": [_insert("eachPixelXYZ", "perPixel")]}]))
    _assert(_keys(resolve_mode(per_pixel))[:4] ==
            ["(1, 1) Red", "(1, 1) Green", "(2, 1) Red", "(2, 1) Green"],
            "perPixel groups every channel of a pixel together")

    per_channel = parse_ofl(_matrix_fixture(modes=[
        {"name": "6ch", "channels": [_insert("eachPixelXYZ", "perChannel")]}]))
    _assert(_keys(resolve_mode(per_channel))[:4] ==
            ["(1, 1) Red", "(2, 1) Red", "(3, 1) Red", "(1, 2) Red"],
            "perChannel groups every pixel of a channel together")

    _assert(resolve_mode(per_pixel).channel_count == 12, "3x2 pixels x 2 channels")


def test_matrix_insert_splices_inline():
    print("test_matrix_insert_splices_inline")
    fx = parse_ofl(_matrix_fixture(modes=[{
        "name": "mixed",
        "channels": ["Intensity",
                     _insert("eachPixelXYZ", "perPixel", ["$pixelKey Red"]),
                     None, "Red"]}]))
    _assert(_keys(resolve_mode(fx)) ==
            ["Intensity", "(1, 1) Red", "(2, 1) Red", "(3, 1) Red",
             "(1, 2) Red", "(2, 2) Red", "(3, 2) Red", "", "Red"],
            "expanded pixels are spliced in place, keeping surrounding slots")


def test_matrix_insert_null_template_leaves_a_gap():
    print("test_matrix_insert_null_template_leaves_a_gap")
    fx = parse_ofl(_matrix_fixture(modes=[{
        "name": "gapped",
        "channels": [_insert("eachPixelXYZ", "perPixel", ["$pixelKey Red", None])]}]))
    mode = resolve_mode(fx)
    _assert(mode.channel_count == 12, "6 pixels x 2 template slots")
    _assert([c.key for c in mode.channels[:2]] == ["(1, 1) Red", ""],
            "a null template entry becomes an unused slot per pixel")


def test_template_channel_expansion_records_its_pixel():
    print("test_template_channel_expansion_records_its_pixel")
    fx = parse_ofl(_matrix_fixture(modes=[
        {"name": "6ch", "channels": [_insert("eachPixelXYZ", "perPixel")]}]))
    first = resolve_mode(fx).channels[0].channel
    _assert(first.pixel_key == "(1, 1)", "expanded channels know which pixel they drive")
    _assert(first.capabilities[0].get("color") == "Red",
            "the template's capability survives expansion")


def test_pixel_groups():
    print("test_pixel_groups")
    fx = parse_ofl(_fixture(
        matrix={"pixelCount": [4, 1, 1],
                "pixelGroups": {"All": "all",
                                "Ends": ["1", "4"],
                                "Odd": {"x": ["odd"]}}},
        templateChannels={"$pixelKey Red": {"capability": {"type": "Intensity"}}}))
    groups = fx.matrix.pixel_groups
    _assert(groups["All"] == ("1", "2", "3", "4"), '"all" expands to every pixel')
    _assert(groups["Ends"] == ("1", "4"), "an explicit list is kept verbatim")
    _assert(groups["Odd"] == ("1", "3"), "a constraint object filters by position")
    _assert(fx.matrix.keys_in_order("eachPixelGroup") == ("All", "Ends", "Odd"),
            "eachPixelGroup yields group keys in declaration order")


def test_template_channels_expand_for_pixel_groups():
    print("test_template_channels_expand_for_pixel_groups")
    # A batten whose modes address groups of pixels rather than each pixel: the
    # real shape of Chroma-Q Color Force II, Astera tubes and most LED battens.
    fx = parse_ofl(_fixture(
        matrix={"pixelCount": [4, 1, 1],
                "pixelGroups": {"Master": "all", "1/2": ["1", "2"], "2/2": ["3", "4"]}},
        templateChannels={
            "Red $pixelKey": {"capability": {"type": "ColorIntensity", "color": "Red"},
                              "fineChannelAliases": ["Red $pixelKey fine"]},
            "Green $pixelKey": {"capability": {"type": "ColorIntensity", "color": "Green"}},
        },
        modes=[
            {"name": "master", "channels": [
                _insert(["Master"], "perPixel", ["Red $pixelKey", "Green $pixelKey"])]},
            {"name": "halves", "channels": [
                _insert(["1/2", "2/2"], "perPixel", ["Red $pixelKey"])]},
            {"name": "fine", "channels": ["Red Master", "Red Master fine"]},
        ]))

    master = fx.modes[0]
    _assert(_keys(master) == ["Red Master", "Green Master"],
            "a group key resolves like a pixel key")
    _assert(all(c.channel is not None for c in master.channels),
            "group channels resolve to real channels, not placeholder slots")
    _assert(master.channels[0].channel.capabilities[0].get("color") == "Red",
            "the template's capability survives group expansion")
    _assert(master.channels[0].channel.pixel_key == "",
            "a group drives several cells, so it claims no single pixel")
    _assert(master.channels[0].channel.pixel_group == "Master",
            "it names the group it drives, so the cells stay findable")
    _assert(_keys(fx.modes[1]) == ["Red 1/2", "Red 2/2"]
            and [c.channel.pixel_group for c in fx.modes[1].channels]
            == ["1/2", "2/2"],
            "each expanded group channel names its own group")

    _assert(_keys(fx.modes[1]) == ["Red 1/2", "Red 2/2"],
            "repeatFor over several groups expands each in order")

    fine = fx.modes[2]
    _assert(fine.channels[1].fine_of == 0 and fine.channels[1].fine_shift == 0,
            "a group channel's fine alias links back to its coarse slot")


def _switching_fixture(mode_channels, trigger_default=0):
    """A fixture whose "Speed / Sensitivity" slot means different things
    depending on where its "Programs" channel sits — the shape behind most of
    OFL's `X / Y` channel names."""
    return _fixture(
        availableChannels={
            "Programs": {
                "defaultValue": trigger_default,
                "capabilities": [
                    {"dmxRange": [0, 127], "type": "Effect", "effectName": "Auto",
                     "switchChannels": {"Speed / Sensitivity": "Program Speed"}},
                    {"dmxRange": [128, 255], "type": "Effect", "effectName": "Sound",
                     "switchChannels": {"Speed / Sensitivity": "Sound Sensitivity"}},
                ]},
            "Program Speed": {"capability": {"type": "EffectSpeed"},
                              "fineChannelAliases": ["Program Speed fine"]},
            "Sound Sensitivity": {"capability": {"type": "SoundSensitivity"}},
        },
        modes=[{"name": "test", "channels": mode_channels}])


def test_switching_channel_resolves_to_its_default_target():
    print("test_switching_channel_resolves_to_its_default_target")
    fx = parse_ofl(_switching_fixture(["Programs", "Speed / Sensitivity"]))
    slot = resolve_mode(fx).channels[1]
    _assert(slot.channel is not None,
            "a switching key resolves to a real channel, not a placeholder")
    _assert(slot.channel.name == "Program Speed",
            "the trigger's default value picks which target is the static one")
    _assert(slot.switch_of == 0, "the slot points at its trigger channel")
    _assert([o.trigger_range for o in slot.switch_options] == [(0, 127), (128, 255)],
            "every alternative is kept, ordered by the trigger's range")
    _assert([o.channel.name for o in slot.switch_options] ==
            ["Program Speed", "Sound Sensitivity"],
            "each alternative carries the channel it switches to")


def test_switching_channel_default_follows_the_trigger_default():
    print("test_switching_channel_default_follows_the_trigger_default")
    fx = parse_ofl(_switching_fixture(["Programs", "Speed / Sensitivity"],
                                      trigger_default=200))
    _assert(resolve_mode(fx).channels[1].channel.name == "Sound Sensitivity",
            "a trigger defaulting into the second range picks the second target")


def test_switching_channel_trigger_may_come_later():
    print("test_switching_channel_trigger_may_come_later")
    # Patch order is the fixture's business: 87 real OFL fixtures list the
    # switching slot before the channel that drives it.
    fx = parse_ofl(_switching_fixture(["Speed / Sensitivity", "Programs"]))
    slot = resolve_mode(fx).channels[0]
    _assert(slot.switch_of == 1, "a trigger patched after the slot still resolves")


def test_switching_channel_fine_alias():
    print("test_switching_channel_fine_alias")
    fx = parse_ofl(_switching_fixture(
        ["Programs", "Speed / Sensitivity", "Speed / Sensitivity fine"]))
    mode = resolve_mode(fx)
    _assert(mode.channel_count == 3, "the fine byte keeps its own address")
    _assert(mode.channels[2].fine_of == 1,
            "a switching channel's fine byte is named after the virtual key "
            "and folds into it")


def test_switching_channel_fine_alias_named_after_the_real_channel():
    print("test_switching_channel_fine_alias_named_after_the_real_channel")
    # Chauvet's Colorado 1 Solo does this: its mode lists "Red fine" while "Red"
    # itself only appears behind the switching key "Red / Fade Duration".
    fx = parse_ofl(_switching_fixture(
        ["Programs", "Speed / Sensitivity", "Program Speed fine"]))
    slot = resolve_mode(fx).channels[2]
    _assert(slot.fine_of == 1,
            "a fine byte named after the switched channel folds into the "
            "switching slot that holds it")
    _assert(slot.fine_shift == 0, "and carries the coarse channel's shift")


def test_switching_channel_without_its_trigger_keeps_the_default():
    print("test_switching_channel_without_its_trigger_keeps_the_default")
    fx = parse_ofl(_switching_fixture(["Speed / Sensitivity"]))
    slot = resolve_mode(fx).channels[0]
    _assert(slot.switch_of == -1 and slot.channel.name == "Program Speed",
            "no trigger in the mode -> the slot stays on its default resolution")


# --- entity / unit parsing ---------------------------------------------------

def test_parse_entity_units():
    print("test_parse_entity_units")
    cases = [
        ("50%", "brightness", 0.5), ("100%", "brightness", 1.0),
        ("20Hz", "speed", 20.0), ("120bpm", "speed", 2.0), ("60rpm", "speed", 1.0),
        ("50%", "speed", SPEED_MAX_HZ / 2),
        ("2s", "time", 2.0), ("500ms", "time", 0.5),
        ("6500K", "colorTemperature", 6500.0),
        ("25deg", "beamAngle", 25.0), ("90deg", "angle", 90.0),
        ("5m", "distance", 5.0),
    ]
    for text, kind, expected in cases:
        got = parse_entity(text, kind)
        _assert(got is not None and abs(got - expected) < 1e-6,
                f"{text!r} as {kind} -> {expected}")


def test_parse_entity_keywords():
    print("test_parse_entity_keywords")
    cases = [
        ("off", "brightness", 0.0), ("bright", "brightness", 1.0),
        ("stop", "speed", 0.0), ("fast", "speed", SPEED_MAX_HZ),
        ("fast reverse", "speed", -SPEED_MAX_HZ),
        ("fast CW", "rotationSpeed", SPEED_MAX_HZ),
        ("fast CCW", "rotationSpeed", -SPEED_MAX_HZ),
        ("instant", "time", 0.0), ("long", "time", 5.0),
        ("warm", "colorTemperature", CT_WARM_K), ("CTB", "colorTemperature", CT_COLD_K),
        ("closed", "beamAngle", 0.0), ("wide", "beamAngle", 60.0),
        ("near", "distance", 1.0),
    ]
    for text, kind, expected in cases:
        _assert(parse_entity(text, kind) == expected, f"{text!r} as {kind} -> {expected}")

    # "slow"/"fast" mean different things on speed vs rotationSpeed; the two
    # vocabularies must not bleed into each other.
    _assert(parse_entity("fast CW", "speed") is None,
            "rotationSpeed keywords are not valid speed keywords")
    _assert(parse_entity("fast", "rotationSpeed") is None,
            "speed keywords are not valid rotationSpeed keywords")


def test_parse_entity_colour_temperature_percent_is_bipolar():
    print("test_parse_entity_colour_temperature_percent_is_bipolar")
    _assert(parse_entity("0%", "colorTemperature") == CT_DEFAULT_K, "0% is neutral")
    _assert(parse_entity("-100%", "colorTemperature") == CT_WARM_K, "-100% is warm")
    _assert(parse_entity("100%", "colorTemperature") == CT_COLD_K, "+100% is cold")


def test_parse_entity_rejects_unconvertible():
    print("test_parse_entity_rejects_unconvertible")
    _assert(parse_entity(None, "brightness") is None, "None -> None")
    _assert(parse_entity("1000lm", "brightness") is None,
            "lumens can't become a 0..1 brightness")
    _assert(parse_entity("nonsense", "speed") is None, "unparseable text -> None")
    _assert(parse_entity(True, "brightness") is None, "a bool is not a number")
    _assert(parse_entity(0.25, "parameter") == 0.25, "a plain number passes through")


def test_parse_entity_angle_kinds():
    print("test_parse_entity_angle_kinds")
    # rotationAngle backs nine capability types (Pan, Tilt, Rotation, Prism,
    # every wheel and blade rotation). Before it was in the tables these all
    # came back None, so a mover's position silently didn't exist.
    cases = [
        ("540deg", "rotationAngle", 540.0), ("50%", "rotationAngle", 180.0),
        ("-90deg", "rotationAngle", -90.0),
        ("45deg", "horizontalAngle", 45.0), ("50%", "horizontalAngle", 90.0),
        ("30deg", "verticalAngle", 30.0),
        ("20deg", "swingAngle", 20.0), ("100%", "swingAngle", 90.0),
    ]
    for text, kind, expected in cases:
        got = parse_entity(text, kind)
        _assert(got is not None and abs(got - expected) < 1e-6,
                f"{text!r} as {kind} -> {expected}")

    _assert(parse_entity("left", "horizontalAngle") == -90.0
            and parse_entity("right", "horizontalAngle") == 90.0,
            "the aim keywords are bipolar")
    _assert(parse_entity("top", "verticalAngle") == -90.0
            and parse_entity("wide", "swingAngle") == 45.0,
            "vertical and swing carry their own vocabularies")


def test_angle_kind_is_an_alias_of_rotation_angle():
    print("test_angle_kind_is_an_alias_of_rotation_angle")
    # "angle" predates the real OFL name and callers still ask for it.
    for text in ("540deg", "50%", "-90deg"):
        _assert(parse_entity(text, "angle") == parse_entity(text, "rotationAngle"),
                f"{text!r} reads the same under either name")


def test_parse_entity_fog_iris_and_insertion():
    print("test_parse_entity_fog_iris_and_insertion")
    cases = [
        ("50%", "fogOutput", 0.5), ("strong", "fogOutput", 1.0),
        ("off", "fogOutput", 0.0),
        ("5m^3/min", "fogOutput", 0.5),
        ("out", "insertion", 0.0), ("in", "insertion", 1.0),
        ("75%", "insertion", 0.75),
        ("closed", "irisPercent", 0.0), ("open", "irisPercent", 1.0),
        ("40%", "irisPercent", 0.4),
        (3, "slotNumber", 3.0),
    ]
    for text, kind, expected in cases:
        got = parse_entity(text, kind)
        _assert(got is not None and abs(got - expected) < 1e-6,
                f"{text!r} as {kind} -> {expected}")


# --- wheels ------------------------------------------------------------------

WHEELS = {
    "Color Wheel": {
        "slots": [
            {"type": "Open"},
            {"type": "Color", "name": "Red", "colors": ["#ff0000"]},
            {"type": "Color", "name": "CTB", "colors": ["#c8dcff"],
             "colorTemperature": "8000K"},
        ],
    },
    "Gobo Wheel": {
        "direction": "CCW",
        "slots": [
            {"type": "Open"},
            {"type": "Gobo", "resource": "gobos/biohazard", "name": "Biohazard"},
            {"type": "Prism", "name": "3-facet", "facets": 3},
            {"type": "Iris", "openPercent": "40%"},
            {"type": "Frost", "frostIntensity": "60%"},
            {"type": "AnimationGoboStart", "name": "Fire"},
            {"type": "AnimationGoboEnd"},
            {"type": "Closed"},
        ],
    },
}


def _wheel_fixture():
    return parse_ofl(_fixture(
        wheels=WHEELS,
        availableChannels={
            "Color Wheel": {"capability": {"type": "WheelSlot", "slotNumber": 2}},
            "Gobo Wheel": {"capability": {"type": "WheelSlot", "slotNumber": 2}},
            "Gobo Spin": {"capability": {"type": "WheelRotation",
                                         "wheel": ["Gobo Wheel", "Color Wheel"],
                                         "speedStart": "slow CW", "speedEnd": "fast CW"}},
        },
        modes=[{"name": "test",
                "channels": ["Color Wheel", "Gobo Wheel", "Gobo Spin"]}]))


def test_wheels_parse_every_slot_type():
    print("test_wheels_parse_every_slot_type")
    fx = _wheel_fixture()
    _assert(set(fx.wheels) == {"Color Wheel", "Gobo Wheel"}, "both wheels parsed")

    colour = fx.wheels["Color Wheel"]
    _assert([s.type for s in colour.slots] == ["Open", "Color", "Color"],
            "slot types survive in order")
    _assert(colour.slots[1].colors == ((255, 0, 0),), "a gel colour becomes RGB")
    _assert(colour.slots[1].name == "Red", "a slot keeps its name")
    _assert(colour.slots[2].get("colorTemperature") == "8000K",
            "unmodelled slot properties pass through raw")

    gobo = fx.wheels["Gobo Wheel"]
    _assert(gobo.direction == "CCW", "the wheel's spin direction is kept")
    _assert([s.type for s in gobo.slots] ==
            ["Open", "Gobo", "Prism", "Iris", "Frost",
             "AnimationGoboStart", "AnimationGoboEnd", "Closed"],
            "all nine OFL slot types round-trip")
    _assert(gobo.slots[1].get("resource") == "gobos/biohazard",
            "a gobo names the pattern to draw")
    _assert(gobo.slots[2].get("facets") == 3, "a prism declares its facet count")


def test_a_fixture_without_wheels_has_none():
    print("test_a_fixture_without_wheels_has_none")
    fx = parse_ofl(_fixture())
    _assert(fx.wheels == {}, "no wheels block -> an empty mapping, not None")


def test_slot_at_wraps_and_blends():
    print("test_slot_at_wraps_and_blends")
    wheel = _wheel_fixture().wheels["Color Wheel"]

    slot, nxt, blend = slot_at(wheel, 1)
    _assert(slot.type == "Open" and nxt is None and blend == 0.0,
            "slot 1 is the first slot, with nothing to blend toward")

    slot, nxt, blend = slot_at(wheel, 2.5)
    _assert(slot.name == "Red" and nxt.name == "CTB" and abs(blend - 0.5) < 1e-6,
            "2.5 sits halfway between slots 2 and 3 — the wheel is mid-turn")

    slot, nxt, _ = slot_at(wheel, 3.5)
    _assert(slot.name == "CTB" and nxt.type == "Open",
            "past the last slot it wraps back to the first")

    _assert(slot_at(wheel, 4)[0].type == "Open", "a whole turn comes back around")


def test_wheel_names_from_a_capability():
    print("test_wheel_names_from_a_capability")
    fx = _wheel_fixture()
    mode = fx.modes[0]

    plain = mode.channels[0].channel.capabilities[0]
    _assert(wheel_names(plain, "Color Wheel") == ("Color Wheel",),
            "no `wheel` property means the wheel named after the channel")

    several = mode.channels[2].channel.capabilities[0]
    _assert(wheel_names(several, "Gobo Spin") == ("Gobo Wheel", "Color Wheel"),
            "WheelRotation may drive several wheels at once")

    named = Capability(type="WheelSlot", dmx_range=(0, 255),
                       props={"wheel": "Gobo Wheel"})
    _assert(wheel_names(named, "Whatever") == ("Gobo Wheel",),
            "an explicit name wins over the channel's")


def test_slot_number_interpolates_across_the_range():
    print("test_slot_number_interpolates_across_the_range")
    # The between-slots case needs no new code: slotNumberStart/End go through
    # the same interpolation as every other entity pair.
    cap = Capability(type="WheelSlot", dmx_range=(0, 100),
                     props={"slotNumberStart": 1, "slotNumberEnd": 3})
    _assert(capability_value(cap, 0, "slotNumber", "slotNumber") == 1.0,
            "the bottom of the range is the first slot")
    _assert(capability_value(cap, 50, "slotNumber", "slotNumber") == 2.0,
            "halfway is the middle slot")
    _assert(capability_value(cap, 100, "slotNumber", "slotNumber") == 3.0,
            "the top is the last")


def test_interp():
    print("test_interp")
    _assert(interp(0.0, 10.0, 0.5) == 5.0, "both endpoints lerp")
    _assert(interp(3.0, None, 0.9) == 3.0, "start alone is constant")
    _assert(interp(None, 7.0, 0.1) == 7.0, "end alone is constant")
    _assert(interp(None, None, 0.5) is None, "neither -> None")
    _assert(interp(0.0, 10.0, 5.0) == 10.0, "t is clamped")


def main():
    test_rejects_non_ofl_documents()
    test_parses_identity_and_physical()
    test_mode_lookup_falls_back_to_first()
    test_mode_physical_overrides_fixture_physical()
    test_null_and_unknown_slots_consume_addresses()
    test_fine_channels()
    test_dmx_value_resolution_overrides_alias_count()
    test_default_and_highlight_values()
    test_single_capability_spans_the_whole_range()
    test_capability_range_lookup_at_boundaries()
    test_capability_value_falls_back_to_the_flat_form()
    test_unknown_capability_type_is_preserved()
    test_matrix_pixel_keys_generated_from_pixel_count()
    test_matrix_axis_orders()
    test_matrix_insert_channel_order()
    test_matrix_insert_splices_inline()
    test_matrix_insert_null_template_leaves_a_gap()
    test_template_channel_expansion_records_its_pixel()
    test_pixel_groups()
    test_template_channels_expand_for_pixel_groups()
    test_switching_channel_resolves_to_its_default_target()
    test_switching_channel_default_follows_the_trigger_default()
    test_switching_channel_trigger_may_come_later()
    test_switching_channel_fine_alias()
    test_switching_channel_fine_alias_named_after_the_real_channel()
    test_switching_channel_without_its_trigger_keeps_the_default()
    test_parse_entity_units()
    test_parse_entity_keywords()
    test_parse_entity_colour_temperature_percent_is_bipolar()
    test_parse_entity_rejects_unconvertible()
    test_parse_entity_angle_kinds()
    test_angle_kind_is_an_alias_of_rotation_angle()
    test_parse_entity_fog_iris_and_insertion()
    test_wheels_parse_every_slot_type()
    test_a_fixture_without_wheels_has_none()
    test_slot_at_wraps_and_blends()
    test_wheel_names_from_a_capability()
    test_slot_number_interpolates_across_the_range()
    test_interp()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
