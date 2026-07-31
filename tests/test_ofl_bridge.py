"""
Characterization test: the OFL -> FixtureDef bridge and the .tdmx.json sidecar.

OFL describes a channel by what it does across its DMX range; the engine, the
note layout and the clip library all speak a single ROLE_* tag plus a flag
bitmask. This covers that translation:

  * a capability type maps to a role, and ColorIntensity maps by its colour;
  * a channel whose capabilities disagree resolves to the type covering the most
    DMX, ignoring NoFunction (a strobe channel that is NoFunction at the bottom
    is still a strobe channel);
  * `blend` is derived — every level-ish capability blends, every discrete one
    snaps — while the knob flags are not derivable and must be declared;
  * a sidecar can override role and flags, but nothing in it can change the DMX
    footprint.

The anchor test builds par_rgb both ways and demands the same result.

Run with:
    uv run python tests/test_ofl_bridge.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import ofl
from core.fixture_library import (
    build_fixture_def, role_index_map,
    ROLE_GENERIC, ROLE_BRIGHTNESS, ROLE_RED, ROLE_GREEN, ROLE_BLUE,
    ROLE_WHITE_WARM, ROLE_WHITE_COOL, ROLE_ANIMATION, ROLE_SPEED,
    ROLE_DIRECTION, ROLE_STROBE, ROLE_MODE,
)
from core.fixture_sidecar import parse_sidecar

BLEND, KNOB_LEVEL, KNOB_SPEED = 0x01, 0x02, 0x04
SCHEMA = "https://raw.githubusercontent.com/OpenLightingProject/open-fixture-library/master/schemas/fixture.json"
META = {"authors": ["test"], "createDate": "2026-01-01", "lastModifyDate": "2026-01-01"}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _build(doc, sidecar_data=None, mode=""):
    fixture = ofl.parse_ofl(doc)
    assert fixture is not None, "test document must be valid OFL"
    sidecar = parse_sidecar(sidecar_data) if sidecar_data else None
    return build_fixture_def(doc["name"], fixture, sidecar, mode)


def _triples(fdef):
    return [(c.field, c.role, c.flags) for c in fdef.channels]


def _one_channel(channel_data, sidecar_data=None):
    """A single-channel fixture, for exercising one channel's derivation."""
    return _build({
        "$schema": SCHEMA, "name": "probe", "categories": ["Other"], "meta": META,
        "availableChannels": {"ch": channel_data},
        "modes": [{"name": "1ch", "channels": ["ch"]}],
    }, sidecar_data)


# --- the anchor: OFL must reproduce the legacy result -------------------------

# What the pre-OFL format produced for par_rgb, captured before the cut. The
# legacy parser is gone, so this is the golden record it left behind.
LEGACY_PAR_RGB_TRIPLES = [
    ("brightness", ROLE_BRIGHTNESS, BLEND | KNOB_LEVEL),
    ("red", ROLE_RED, BLEND),
    ("green", ROLE_GREEN, BLEND),
    ("blue", ROLE_BLUE, BLEND),
]
LEGACY_PAR_RGB_ROLE_INDEX = {ROLE_BRIGHTNESS: 0, ROLE_RED: 1, ROLE_GREEN: 2, ROLE_BLUE: 3}

OFL_PAR_RGB = {
    "$schema": SCHEMA,
    "name": "par_rgb",
    "categories": ["Color Changer", "Dimmer"],
    "meta": META,
    "availableChannels": {
        "brightness": {"capability": {"type": "Intensity"}},
        "red": {"capability": {"type": "ColorIntensity", "color": "Red"}},
        "green": {"capability": {"type": "ColorIntensity", "color": "Green"}},
        "blue": {"capability": {"type": "ColorIntensity", "color": "Blue"}},
    },
    "modes": [{"name": "4ch",
               "channels": ["brightness", "red", "green", "blue"]}],
}

# Only the knob flag needs declaring; blend derives from the capability types.
PAR_RGB_SIDECAR = {
    "tdmx": 1,
    "modes": {"*": {"channels": {"brightness": {"flags": ["blend", "knob_level"]}}}},
}


def test_ofl_reproduces_the_legacy_definition():
    print("test_ofl_reproduces_the_legacy_definition")
    converted = _build(OFL_PAR_RGB, PAR_RGB_SIDECAR)
    _assert(_triples(converted) == LEGACY_PAR_RGB_TRIPLES,
            "same channel order, roles and flags as the pre-OFL definition")
    _assert(converted.channel_count == 4, "same DMX footprint")
    _assert(role_index_map(converted) == LEGACY_PAR_RGB_ROLE_INDEX,
            "same role -> slice index mapping")


def test_mode_and_categories_are_carried():
    print("test_mode_and_categories_are_carried")
    fdef = _build(OFL_PAR_RGB, PAR_RGB_SIDECAR)
    _assert(fdef.mode == "4ch", "the resolved mode name is recorded")
    _assert(fdef.categories == ("Color Changer", "Dimmer"),
            "categories keep their order — the renderer picks the first it knows")


# --- role derivation ---------------------------------------------------------

def test_capability_type_to_role():
    print("test_capability_type_to_role")
    cases = [
        ({"type": "Intensity"}, ROLE_BRIGHTNESS),
        ({"type": "ShutterStrobe", "shutterEffect": "Open"}, ROLE_STROBE),
        ({"type": "StrobeSpeed"}, ROLE_STROBE),
        ({"type": "Effect", "effectName": "Rainbow"}, ROLE_ANIMATION),
        ({"type": "EffectSpeed"}, ROLE_SPEED),
        ({"type": "Speed"}, ROLE_SPEED),
        ({"type": "Maintenance"}, ROLE_MODE),
        ({"type": "Generic"}, ROLE_GENERIC),
        ({"type": "NoFunction"}, ROLE_GENERIC),
        ({"type": "Zoom"}, ROLE_GENERIC),
    ]
    for capability, expected in cases:
        fdef = _one_channel({"capability": capability})
        _assert(fdef.channels[0].role == expected,
                f"{capability['type']} -> role {expected}")


def test_colour_intensity_maps_by_colour():
    print("test_colour_intensity_maps_by_colour")
    cases = [("Red", ROLE_RED), ("Green", ROLE_GREEN), ("Blue", ROLE_BLUE),
             ("Warm White", ROLE_WHITE_WARM), ("Cold White", ROLE_WHITE_COOL),
             ("White", ROLE_WHITE_COOL),
             # No ROLE_* exists for these; the renderer mixes them from the
             # capability directly.
             ("Amber", ROLE_GENERIC), ("UV", ROLE_GENERIC), ("Lime", ROLE_GENERIC)]
    for colour, expected in cases:
        fdef = _one_channel({"capability": {"type": "ColorIntensity", "color": colour}})
        _assert(fdef.channels[0].role == expected, f"ColorIntensity {colour} -> {expected}")


def test_direction_has_no_derivation():
    print("test_direction_has_no_derivation")
    # OFL folds direction into the sign of a rotation speed, so guessing would
    # misfire; a sidecar has to say so.
    fdef = _one_channel({"capabilities": [
        {"dmxRange": [0, 127], "type": "Generic", "comment": "Forward"},
        {"dmxRange": [128, 255], "type": "Generic", "comment": "Reverse"}]})
    _assert(fdef.channels[0].role == ROLE_GENERIC, "no capability produces ROLE_DIRECTION")

    declared = _one_channel(
        {"capabilities": [
            {"dmxRange": [0, 127], "type": "Generic"},
            {"dmxRange": [128, 255], "type": "Generic"}]},
        {"tdmx": 1, "modes": {"*": {"channels": {"ch": {"role": "direction"}}}}})
    _assert(declared.channels[0].role == ROLE_DIRECTION, "a sidecar can declare it")


def test_dominant_capability_wins_by_dmx_span():
    print("test_dominant_capability_wins_by_dmx_span")
    # A strobe channel that does nothing at the bottom of its range is still a
    # strobe channel.
    strobe = _one_channel({"capabilities": [
        {"dmxRange": [0, 7], "type": "NoFunction"},
        {"dmxRange": [8, 255], "type": "ShutterStrobe", "shutterEffect": "Strobe"}]})
    _assert(strobe.channels[0].role == ROLE_STROBE, "NoFunction never counts toward the role")

    # Many Effect capabilities across the range still read as one effect channel.
    effects = _one_channel({"capabilities": (
        [{"dmxRange": [i * 4, i * 4 + 3], "type": "Effect", "effectName": f"E{i}"}
         for i in range(35)]
        + [{"dmxRange": [140, 255], "type": "NoFunction"}])})
    _assert(effects.channels[0].role == ROLE_ANIMATION,
            "35 Effect capabilities sum to the widest type")

    # The narrower type loses even though it appears first.
    mixed = _one_channel({"capabilities": [
        {"dmxRange": [0, 9], "type": "Intensity"},
        {"dmxRange": [10, 255], "type": "Effect", "effectName": "Program"}]})
    _assert(mixed.channels[0].role == ROLE_ANIMATION, "the widest type wins, not the first")


# --- flags -------------------------------------------------------------------

def test_blend_is_derived_from_the_capability_kind():
    print("test_blend_is_derived_from_the_capability_kind")
    blending = ["Intensity", "ColorIntensity", "ColorTemperature", "Zoom", "Pan"]
    snapping = ["Effect", "ShutterStrobe", "Maintenance", "Generic", "EffectSpeed"]
    for cap_type in blending:
        fdef = _one_channel({"capability": {"type": cap_type, "color": "Red"}})
        _assert(fdef.channels[0].flags & BLEND, f"{cap_type} blends")
    for cap_type in snapping:
        fdef = _one_channel({"capability": {"type": cap_type, "effectName": "x"}})
        _assert(not fdef.channels[0].flags & BLEND, f"{cap_type} snaps")


def test_knob_flags_require_a_sidecar():
    print("test_knob_flags_require_a_sidecar")
    bare = _one_channel({"capability": {"type": "Intensity"}})
    _assert(bare.channels[0].flags == BLEND, "nothing derives the knob flags")

    declared = _one_channel(
        {"capability": {"type": "Intensity"}},
        {"tdmx": 1, "modes": {"*": {"channels": {"ch": {"flags": ["blend", "knob_level"]}}}}})
    _assert(declared.channels[0].flags == BLEND | KNOB_LEVEL, "a sidecar declares them")


def test_declared_flags_replace_the_derived_set():
    print("test_declared_flags_replace_the_derived_set")
    # Intensity would derive `blend`; an explicit empty list means none.
    fdef = _one_channel(
        {"capability": {"type": "Intensity"}},
        {"tdmx": 1, "modes": {"*": {"channels": {"ch": {"flags": []}}}}})
    _assert(fdef.channels[0].flags == 0, "an explicit flag list is the whole truth")


# --- slots, fine channels, footprint -----------------------------------------

def test_unused_slots_get_placeholder_fields():
    print("test_unused_slots_get_placeholder_fields")
    fdef = _build({
        "$schema": SCHEMA, "name": "gapped", "categories": ["Other"], "meta": META,
        "availableChannels": {"a": {"capability": {"type": "Intensity"}}},
        "modes": [{"name": "3ch", "channels": ["a", None, "a"]}],
    })
    _assert(fdef.channel_count == 3, "an unused slot still consumes its address")
    _assert(fdef.channels[1].field == "__unused_1" and fdef.channels[1].role == ROLE_GENERIC,
            "the placeholder keeps later channels on their own addresses")


def test_fine_channels_do_not_shadow_the_coarse_role():
    print("test_fine_channels_do_not_shadow_the_coarse_role")
    fdef = _build({
        "$schema": SCHEMA, "name": "fine", "categories": ["Dimmer"], "meta": META,
        "availableChannels": {
            "dimmer": {"fineChannelAliases": ["dimmer fine"],
                       "capability": {"type": "Intensity"}}},
        "modes": [{"name": "2ch", "channels": ["dimmer", "dimmer fine"]}],
    })
    coarse, fine = fdef.channels
    _assert(coarse.role == ROLE_BRIGHTNESS, "the coarse slot carries the role")
    _assert(fine.role == ROLE_GENERIC,
            "the fine byte is part of a value, not a channel of its own")
    _assert(role_index_map(fdef)[ROLE_BRIGHTNESS] == 0,
            "role lookup still resolves to the coarse slot")
    _assert(fine.fine_of == 0 and fine.resolution_bytes == 2,
            "the fine slot points back at its coarse slot")


# --- the sidecar itself ------------------------------------------------------

def test_sidecar_supplies_banks_and_chases():
    print("test_sidecar_supplies_banks_and_chases")
    fdef = _build(OFL_PAR_RGB, {
        "tdmx": 1,
        "modes": {"*": {
            "banks": {"FIRST": [{"brightness": 255}], "SECOND": [{"brightness": 0}]},
            "chases": [{"banks": ["FIRST"], "scenes": [0]}],
            "effects": {"Glow": {"gif": "01-ON.gif"}},
            "stage": {"size_px": [40, 40]}}}})
    _assert(list(fdef.banks) == ["FIRST", "SECOND"],
            "bank order is preserved — the first bank is the boot bank")
    _assert(len(fdef.chases) == 1, "chases carried")
    _assert(fdef.effects["Glow"]["gif"] == "01-ON.gif", "effect asset map carried")
    _assert(fdef.stage["size_px"] == [40, 40], "stage hints carried")


def test_no_sidecar_means_no_banks():
    print("test_no_sidecar_means_no_banks")
    # A fixture taken straight from the OFL project, with nothing added.
    fdef = _build(OFL_PAR_RGB)
    _assert(fdef is not None, "an OFL document alone is a usable fixture type")
    _assert(fdef.banks == {} and fdef.chases == [], "it simply has no scene banks")
    _assert(_triples(fdef)[1:] == [("red", ROLE_RED, BLEND),
                                   ("green", ROLE_GREEN, BLEND),
                                   ("blue", ROLE_BLUE, BLEND)],
            "roles and blend still derive with no sidecar at all")


def test_sidecar_merges_shared_and_named_mode_blocks():
    print("test_sidecar_merges_shared_and_named_mode_blocks")
    two_modes = dict(OFL_PAR_RGB)
    two_modes["modes"] = [
        {"name": "4ch", "channels": ["brightness", "red", "green", "blue"]},
        {"name": "3ch", "channels": ["red", "green", "blue"]},
    ]
    sidecar = {
        "tdmx": 1,
        "modes": {
            "*": {"channels": {"red": {"flags": ["blend", "knob_level"]}},
                  "banks": {"SHARED": [{"red": 255}]}},
            "3ch": {"channels": {"green": {"flags": []}},
                    "banks": {"NARROW": [{"red": 0}]}},
        },
    }
    wide = _build(two_modes, sidecar, "4ch")
    narrow = _build(two_modes, sidecar, "3ch")

    _assert(list(wide.banks) == ["SHARED"], "the shared block applies to every mode")
    _assert(list(narrow.banks) == ["NARROW"], "a named block replaces banks wholesale")

    red_narrow = next(c for c in narrow.channels if c.field == "red")
    green_narrow = next(c for c in narrow.channels if c.field == "green")
    _assert(red_narrow.flags == BLEND | KNOB_LEVEL,
            "channel overrides merge per key, so the shared one still applies")
    _assert(green_narrow.flags == 0, "the named block overrides just its own channel")


def test_sidecar_can_override_the_registry_key():
    print("test_sidecar_can_override_the_registry_key")
    # Lets an OFL doc use a proper product name while existing patches keep
    # resolving under the name they were saved with.
    fixture = ofl.parse_ofl(OFL_PAR_RGB)
    sidecar = parse_sidecar({"tdmx": 1, "key": "legacy_name"})
    fdef = build_fixture_def(sidecar.key or fixture.name, fixture, sidecar)
    _assert(fdef.name == "legacy_name", "the registry key comes from the sidecar")


def test_sidecar_version_gate():
    print("test_sidecar_version_gate")
    _assert(parse_sidecar({}) is None, "a sidecar without a version is rejected")
    _assert(parse_sidecar({"tdmx": 99}) is None, "a newer format version is ignored")
    _assert(parse_sidecar({"tdmx": 1}) is not None, "the current version parses")


def test_sidecar_cannot_change_the_footprint():
    print("test_sidecar_cannot_change_the_footprint")
    # Channel count and order come only from the OFL document — the invariant
    # that keeps saved .mid note bindings correct.
    noisy = {"tdmx": 1, "modes": {"*": {"channels": {
        "nonexistent": {"role": "red"}, "brightness": {"role": "red"}}}}}
    fdef = _build(OFL_PAR_RGB, noisy)
    _assert(fdef.channel_count == 4, "an override for an unknown channel adds nothing")
    _assert([c.field for c in fdef.channels] == ["brightness", "red", "green", "blue"],
            "channel order is untouched by the sidecar")


def main():
    test_ofl_reproduces_the_legacy_definition()
    test_mode_and_categories_are_carried()
    test_capability_type_to_role()
    test_colour_intensity_maps_by_colour()
    test_direction_has_no_derivation()
    test_dominant_capability_wins_by_dmx_span()
    test_blend_is_derived_from_the_capability_kind()
    test_knob_flags_require_a_sidecar()
    test_declared_flags_replace_the_derived_set()
    test_unused_slots_get_placeholder_fields()
    test_fine_channels_do_not_shadow_the_coarse_role()
    test_sidecar_supplies_banks_and_chases()
    test_no_sidecar_means_no_banks()
    test_sidecar_merges_shared_and_named_mode_blocks()
    test_sidecar_can_override_the_registry_key()
    test_sidecar_version_gate()
    test_sidecar_cannot_change_the_footprint()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
