"""
Characterization test: the OFL -> FixtureDef bridge and the .tdmx.json sidecar.

A channel's meaning is its OFL capabilities and nothing else — there is no
second, summarised description of it to keep in sync. This covers what the
bridge does with them:

  * `color_slots` addresses a colour channel by its OFL colour name, so every
    one of the 13 mixable colours is as addressable as Red;
  * `describe_channel` labels a channel from the capability covering the most
    DMX, ignoring NoFunction (a strobe channel that is NoFunction at the bottom
    is still a strobe channel);
  * `blend` is derived — every level-ish capability blends, every discrete one
    snaps — while the knob flags are not derivable and must be declared;
  * a sidecar can override the label and the flags, but nothing in it can change
    the DMX footprint.

The anchor test builds par_rgb both ways and demands the same result.

Run with:
    uv run python tests/test_ofl_bridge.py
"""

import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import ofl
from core.channel_families import describe_channel
from core.fixture_library import (
    all_capability_types, build_fixture_def, capability_types, color_slots,
    color_sources, dominant_capability, effect_asset, effects_dir_for,
    resolve_fixture,
)
from core.fixture_sidecar import parse_sidecar
import ofl_factory

BLEND, KNOB_LEVEL, KNOB_SPEED = 0x01, 0x02, 0x04
SCHEMA = "https://raw.githubusercontent.com/OpenLightingProject/open-fixture-library/master/schemas/fixture.json"
META = {"authors": ["test"], "createDate": "2026-01-01", "lastModifyDate": "2026-01-01"}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _build(doc, sidecar_data=None, mode="", effects_dir=""):
    fixture = ofl.parse_ofl(doc)
    assert fixture is not None, "test document must be valid OFL"
    sidecar = parse_sidecar(sidecar_data) if sidecar_data else None
    return build_fixture_def(doc["name"], fixture, sidecar, mode,
                            effects_dir=effects_dir)


def _pairs(fdef):
    return [(c.field, c.flags) for c in fdef.channels]


def _labels(fdef):
    return [describe_channel(c)[0] for c in fdef.channels]


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
LEGACY_PAR_RGB_PAIRS = [
    ("brightness", BLEND | KNOB_LEVEL),
    ("red", BLEND),
    ("green", BLEND),
    ("blue", BLEND),
]
LEGACY_PAR_RGB_COLOR_SLOTS = {"Red": 1, "Green": 2, "Blue": 3}

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
    _assert(_pairs(converted) == LEGACY_PAR_RGB_PAIRS,
            "same channel order and flags as the pre-OFL definition")
    _assert(converted.channel_count == 4, "same DMX footprint")
    _assert(color_slots(converted) == LEGACY_PAR_RGB_COLOR_SLOTS,
            "the same three colour channels, addressed by colour name")


def test_mode_and_categories_are_carried():
    print("test_mode_and_categories_are_carried")
    fdef = _build(OFL_PAR_RGB, PAR_RGB_SIDECAR)
    _assert(fdef.mode == "4ch", "the resolved mode name is recorded")
    _assert(fdef.categories == ("Color Changer", "Dimmer"),
            "categories keep their order — the renderer picks the first it knows")


# --- what a channel reads as -------------------------------------------------

def test_capability_type_to_family():
    print("test_capability_type_to_family")
    cases = [
        ({"type": "Intensity"}, "Intensity"),
        ({"type": "ShutterStrobe", "shutterEffect": "Open"}, "Shutter"),
        ({"type": "StrobeSpeed"}, "Shutter"),
        ({"type": "Effect", "effectName": "Rainbow"}, "Effect"),
        ({"type": "EffectSpeed"}, "Speed"),
        ({"type": "Speed"}, "Speed"),
        ({"type": "Maintenance"}, "Maintenance"),
        ({"type": "Zoom"}, "Zoom"),
        ({"type": "Pan"}, "Pan"),
        # No family table entry: still labelled, never blank.
        ({"type": "Generic"}, "Generic"),
    ]
    for capability, expected in cases:
        fdef = _one_channel({"capability": capability})
        _assert(describe_channel(fdef.channels[0])[0] == expected,
                f"{capability['type']} reads as '{expected}'")


def test_every_mixable_colour_is_addressable():
    print("test_every_mixable_colour_is_addressable")
    # All 13 of OFL's mixable colours, not the six a role enum had names for:
    # the colour IS the key, so an amber channel is as reachable as a red one.
    colours = ["Red", "Green", "Blue", "Cyan", "Magenta", "Yellow", "Amber",
               "White", "Warm White", "Cold White", "UV", "Lime", "Indigo"]
    for colour in colours:
        fdef = _one_channel({"capability": {"type": "ColorIntensity", "color": colour}})
        _assert(color_slots(fdef) == {colour: 0}, f"ColorIntensity {colour} -> slot 0")
        _assert(describe_channel(fdef.channels[0])[0] == colour,
                f"...and reads as '{colour}', not as a generic 'Color'")


def test_sidecar_label_names_what_capabilities_cannot():
    print("test_sidecar_label_names_what_capabilities_cannot")
    # OFL folds direction into the sign of a rotation speed; the ledbar spells
    # it as two commented `Generic` ranges, which no capability type names.
    generic = [{"dmxRange": [0, 127], "type": "Generic", "comment": "Forward"},
               {"dmxRange": [128, 255], "type": "Generic", "comment": "Reverse"}]
    fdef = _one_channel({"capabilities": generic})
    _assert(describe_channel(fdef.channels[0])[0] == "Generic",
            "nothing in the capabilities says 'direction'")

    declared = _one_channel(
        {"capabilities": generic},
        {"tdmx": 1, "modes": {"*": {"channels": {"ch": {"label": "Direction"}}}}})
    _assert(describe_channel(declared.channels[0])[0] == "Direction",
            "a sidecar label says it")
    _assert(declared.channels[0].label == "ch",
            "and leaves the OFL name alone — the browser still shows the document")


def test_dominant_capability_wins_by_dmx_span():
    print("test_dominant_capability_wins_by_dmx_span")
    # A strobe channel that does nothing at the bottom of its range is still a
    # strobe channel.
    strobe = _one_channel({"capabilities": [
        {"dmxRange": [0, 7], "type": "NoFunction"},
        {"dmxRange": [8, 255], "type": "ShutterStrobe", "shutterEffect": "Strobe"}]})
    _assert(dominant_capability(strobe.channels[0].capabilities).type == "ShutterStrobe",
            "NoFunction never counts toward the dominant capability")

    # Many Effect capabilities across the range still read as one effect channel.
    effects = _one_channel({"capabilities": (
        [{"dmxRange": [i * 4, i * 4 + 3], "type": "Effect", "effectName": f"E{i}"}
         for i in range(35)]
        + [{"dmxRange": [140, 255], "type": "NoFunction"}])})
    _assert(describe_channel(effects.channels[0])[0] == "Effect",
            "35 Effect capabilities sum to the widest type")

    # The narrower type loses even though it appears first.
    mixed = _one_channel({"capabilities": [
        {"dmxRange": [0, 9], "type": "Intensity"},
        {"dmxRange": [10, 255], "type": "Effect", "effectName": "Program"}]})
    _assert(describe_channel(mixed.channels[0])[0] == "Effect",
            "the widest type wins, not the first")

    # A tie between two types is broken by which describes the light itself.
    tied = _one_channel({"capabilities": [
        {"dmxRange": [0, 127], "type": "Effect", "effectName": "Program"},
        {"dmxRange": [128, 255], "type": "ColorIntensity", "color": "Red"}]})
    _assert(describe_channel(tied.channels[0])[0] == "Red",
            "an equal-span colour outranks an effect")


def test_two_wheels_on_one_channel_do_not_merge():
    print("test_two_wheels_on_one_channel_do_not_merge")
    # A channel that is mostly a dimmer, with a colour wheel and a gobo wheel
    # sharing the top of its range — 68 values each against the dimmer's 120.
    # Keyed by type alone the two wheels summed to 136 and stole the channel.
    split = _one_channel({"capabilities": [
        {"dmxRange": [0, 119], "type": "Intensity"},
        {"dmxRange": [120, 187], "type": "WheelSlot", "wheel": "Color Wheel",
         "slotNumber": 1},
        {"dmxRange": [188, 255], "type": "WheelSlot", "wheel": "Gobo Wheel",
         "slotNumber": 1}]})
    dominant = dominant_capability(split.channels[0].capabilities)
    _assert(dominant is not None and dominant.type == "Intensity",
            "two different wheels are two subjects, not one wide wheel channel")

    blades = _one_channel({"capabilities": [
        {"dmxRange": [0, 119], "type": "Intensity"},
        {"dmxRange": [120, 187], "type": "BladeInsertion", "blade": "Top",
         "insertionStart": "out", "insertionEnd": "in"},
        {"dmxRange": [188, 255], "type": "BladeInsertion", "blade": "Bottom",
         "insertionStart": "out", "insertionEnd": "in"}]})
    dominant = dominant_capability(blades.channels[0].capabilities)
    _assert(dominant is not None and dominant.type == "Intensity",
            "two blades on one channel are counted separately too")

    # Same shape, but one wheel across both ranges really is one subject.
    same = _one_channel({"capabilities": [
        {"dmxRange": [0, 119], "type": "Intensity"},
        {"dmxRange": [120, 187], "type": "WheelSlot", "wheel": "Gobo Wheel",
         "slotNumber": 1},
        {"dmxRange": [188, 255], "type": "WheelSlot", "wheel": "Gobo Wheel",
         "slotNumber": 2}]})
    dominant = dominant_capability(same.channels[0].capabilities)
    _assert(dominant is not None and dominant.type == "WheelSlot",
            "one wheel over two ranges does still add up")


def test_all_capability_types_sees_every_range():
    print("test_all_capability_types_sees_every_range")
    fdef = _one_channel({"capabilities": [
        {"dmxRange": [0, 249], "type": "Effect", "effectName": "Program"},
        {"dmxRange": [250, 255], "type": "ShutterStrobe",
         "shutterEffect": "Strobe", "speed": "fast"}]})
    _assert(capability_types(fdef) == {"Effect"},
            "the dominant summary reports only what the channel is mostly for")
    _assert(all_capability_types(fdef) == {"Effect", "ShutterStrobe"},
            "the full set sees the strobe at the top of the range")
    _assert("NoFunction" not in all_capability_types(
        _one_channel({"capabilities": [
            {"dmxRange": [0, 127], "type": "NoFunction"},
            {"dmxRange": [128, 255], "type": "Intensity"}]})),
        "NoFunction is not a thing the fixture can do")


def test_color_sources_names_how_a_fixture_takes_colour():
    print("test_color_sources_names_how_a_fixture_takes_colour")
    _assert(color_sources(_build(OFL_PAR_RGB)) == {"mix"}, "an RGB par mixes")
    _assert(color_sources(_one_channel({"capability": {"type": "Intensity"}})) == set(),
            "a plain dimmer has no colour to set")

    wheeled = ofl_factory.fdef(
        wheels={"Color Wheel": {"slots": [
            {"type": "Open"},
            {"type": "Color", "name": "Red", "colors": ["#ff0000"]}]}},
        channels={"Color Wheel": ofl_factory.channel("WheelSlot", slotNumber=1)})
    _assert(color_sources(wheeled) == {"wheel"},
            "a fixture whose only colour is a physical wheel says so")
    _assert(wheeled.wheels["Color Wheel"].slots[1].colors == ((255, 0, 0),),
            "the wheel itself reaches the FixtureDef")


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
    _assert(fdef.channels[1].field == "__unused_1",
            "the placeholder keeps later channels on their own addresses")
    _assert(describe_channel(fdef.channels[1])[0] == "—",
            "and reads as unused rather than as a channel")


def test_fine_channels_do_not_shadow_the_coarse_channel():
    print("test_fine_channels_do_not_shadow_the_coarse_channel")
    fdef = _build({
        "$schema": SCHEMA, "name": "fine", "categories": ["Color Changer"], "meta": META,
        "availableChannels": {
            "red": {"fineChannelAliases": ["red fine"],
                    "capability": {"type": "ColorIntensity", "color": "Red"}}},
        "modes": [{"name": "2ch", "channels": ["red", "red fine"]}],
    })
    coarse, fine = fdef.channels
    _assert(color_slots(fdef) == {"Red": 0},
            "colour lookup resolves to the coarse slot, never the fine byte")
    _assert(describe_channel(coarse)[0] == "Red" and describe_channel(fine)[0] == "Red fine",
            "the fine byte says so instead of posing as a second red")
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
            "effects": {"Glow": {"asset": "01-ON.gif"}},
            "stage": {"size_px": [40, 40]}}}})
    _assert(list(fdef.banks) == ["FIRST", "SECOND"],
            "bank order is preserved — the first bank is the boot bank")
    _assert(len(fdef.chases) == 1, "chases carried")
    _assert(fdef.effects["Glow"]["asset"] == "01-ON.gif", "effect asset map carried")
    _assert(fdef.stage["size_px"] == [40, 40], "stage hints carried")


def test_effect_artwork_resolves_beside_the_definition():
    print("test_effect_artwork_resolves_beside_the_definition")
    # Artwork belongs to the fixture that names it, so it is resolved against
    # that definition's own `<stem>.effects/` folder rather than one global
    # bucket keyed by effect name.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        art = Path(tmp) / "par_rgb.effects"
        art.mkdir()
        (art / "01-ON.gif").write_bytes(b"GIF89a")

        def build(effects):
            return _build(OFL_PAR_RGB,
                          {"tdmx": 1, "modes": {"*": {"effects": effects}}},
                          effects_dir=str(art))

        fdef = build({"Glow": {"asset": "01-ON.gif"}})
        _assert(effect_asset(fdef, "Glow") == str(art / "01-ON.gif"),
                "the effects map resolves against the fixture's own folder")
        _assert(effect_asset(fdef, "Nope") is None,
                "an effect the fixture never declares has no artwork")

        # `gif` is what the key was called before the folder moved beside the
        # definition; a sidecar forked into the user layer back then still says
        # it, and must keep its artwork.
        legacy = build({"Glow": {"gif": "01-ON.gif"}})
        _assert(effect_asset(legacy, "Glow") == str(art / "01-ON.gif"),
                "the pre-rename 'gif' key still resolves")

        missing = build({"Glow": {"asset": "99-nope.gif"}})
        _assert(effect_asset(missing, "Glow") is None,
                "a named file that isn't there degrades to no artwork")

    unplaced = _build(OFL_PAR_RGB,
                      {"tdmx": 1, "modes": {"*": {
                          "effects": {"Glow": {"asset": "01-ON.gif"}}}}})
    _assert(effect_asset(unplaced, "Glow") is None,
            "a def with no folder behind it resolves nothing")


def test_effects_dir_pairs_with_the_ofl_document():
    print("test_effects_dir_pairs_with_the_ofl_document")
    # Stem-plus-suffix, the same convention the .tdmx.json sidecar uses — and
    # deliberately NOT a bare `fixtures/ledbar/`, which a fixtures dir reads as
    # a manufacturer namespace.
    _assert(effects_dir_for(Path("/fx/ledbar.json")) == Path("/fx/ledbar.effects"),
            "ledbar.json -> ledbar.effects/")
    _assert(effects_dir_for(Path("/fx/ADJ/focus-spot-4z.json"))
            == Path("/fx/ADJ/focus-spot-4z.effects"),
            "artwork stays inside the manufacturer folder")

    led = resolve_fixture("ledbar")
    _assert(Path(led.effects_dir).name == "ledbar.effects",
            f"the shipped ledbar points at its own folder ({led.effects_dir})")
    _assert(Path(led.effects_dir).is_dir(), "which is really there")
    _assert(effect_asset(led, "Radar") == str(Path(led.effects_dir) / "17-radar.gif"),
            "and its shipped artwork resolves through the effects map")


def test_no_sidecar_means_no_banks():
    print("test_no_sidecar_means_no_banks")
    # A fixture taken straight from the OFL project, with nothing added.
    fdef = _build(OFL_PAR_RGB)
    _assert(fdef is not None, "an OFL document alone is a usable fixture type")
    _assert(fdef.banks == {} and fdef.chases == [], "it simply has no scene banks")
    _assert(_pairs(fdef)[1:] == [("red", BLEND), ("green", BLEND), ("blue", BLEND)],
            "blend still derives with no sidecar at all")
    _assert(_labels(fdef) == ["Intensity", "Red", "Green", "Blue"],
            "and so does every channel's name — the capabilities carry it")


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
        "nonexistent": {"label": "Nope"}, "brightness": {"label": "Level"}}}}}
    fdef = _build(OFL_PAR_RGB, noisy)
    _assert(fdef.channel_count == 4, "an override for an unknown channel adds nothing")
    _assert([c.field for c in fdef.channels] == ["brightness", "red", "green", "blue"],
            "channel order is untouched by the sidecar")


def main():
    test_ofl_reproduces_the_legacy_definition()
    test_mode_and_categories_are_carried()
    test_capability_type_to_family()
    test_every_mixable_colour_is_addressable()
    test_sidecar_label_names_what_capabilities_cannot()
    test_dominant_capability_wins_by_dmx_span()
    test_two_wheels_on_one_channel_do_not_merge()
    test_all_capability_types_sees_every_range()
    test_color_sources_names_how_a_fixture_takes_colour()
    test_blend_is_derived_from_the_capability_kind()
    test_knob_flags_require_a_sidecar()
    test_declared_flags_replace_the_derived_set()
    test_unused_slots_get_placeholder_fields()
    test_fine_channels_do_not_shadow_the_coarse_channel()
    test_sidecar_supplies_banks_and_chases()
    test_effect_artwork_resolves_beside_the_definition()
    test_effects_dir_pairs_with_the_ofl_document()
    test_no_sidecar_means_no_banks()
    test_sidecar_merges_shared_and_named_mode_blocks()
    test_sidecar_can_override_the_registry_key()
    test_sidecar_version_gate()
    test_sidecar_cannot_change_the_footprint()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
