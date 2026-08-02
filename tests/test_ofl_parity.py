"""
Characterization test: the shipped fixture definitions, as the rest of the app
sees them.

This is the safety net for the Open Fixture Library migration. It pins the
*resolved* shape of every shipped type — channel order, behaviour flags, scene
banks, chases, the note<->field map and the song-part program — against golden
data captured from the pre-OFL format. Converting the defs must not move any of
these numbers.

Two of them are load-bearing beyond "the def parses":

  * `field_note_map` is the note<->channel binding every saved .mid depends on
    (note n = the n-th channel of the fixture). The ledbar's BGR channel order is
    deliberate; reordering it would make every scene clip in every show play the
    wrong colours, silently.
  * the golden `flags` show that `blend` tracks the channel's kind (every
    level-ish capability blends, every discrete one snaps) while
    `knob_level`/`knob_speed` do NOT — the ledbar's brightness carries
    knob_level and the blinder's doesn't. Only the former is derivable.

What a channel *means* is not pinned here: that is its OFL capabilities, read
live by everything that needs it, and covered by test_ofl_bridge.

Run with:
    uv run python tests/test_ofl_parity.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.fixture_library import dominant_capability, load_fixture_defs
from core import note_layout

# The capability kinds whose value is a level worth fading between, stated here
# independently of the bridge's own table so this asserts the rule rather than
# restating the implementation. Adding a kind means changing both, deliberately.
_BLENDS = frozenset({
    "Intensity", "ColorIntensity", "ColorTemperature",
    "Zoom", "Focus", "Iris", "Frost", "BeamAngle", "BeamPosition", "Pan", "Tilt",
    "PanContinuous", "TiltContinuous", "PanTiltSpeed", "Rotation",
    "PrismRotation", "WheelRotation", "WheelSlotRotation",
    "BladeInsertion", "BladeRotation", "BladeSystemRotation",
    "Fog", "FogOutput", "SoundSensitivity", "EffectParameter",
})

# Channels whose shipped sidecar declares `flags` outright, which replaces the
# derived set wholesale — so they are not evidence about the derivation.
_OVERRIDDEN_FLAGS = {
    "ledbar": {"brightness", "speed"},
    "par_rgb": {"brightness"},
    "Wild Wash Pro 648 RGBW": {"red", "green", "blue", "white"},
}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


# (field, flags) per channel, in DMX order. flags are the FLAG_NAMES ints:
# 1=blend, 2=knob_level, 4=knob_speed. What each channel *does* isn't pinned
# here — that is its OFL capabilities, covered by test_ofl_bridge.
GOLDEN_CHANNELS = {
    "ledbar": [
        ("brightness", 3), ("blue", 1), ("green", 1), ("red", 1),
        ("animation", 0), ("speed", 4), ("direction", 0),
    ],
    "blinder": [
        ("brightness", 1), ("cool1", 1), ("cool2", 1),
        ("warm1", 1), ("warm2", 1),
        ("strobe", 0), ("mode", 0), ("speed", 0),
    ],
    "par_rgb": [
        ("brightness", 3), ("red", 1), ("green", 1), ("blue", 1),
    ],
    "Wild Wash Pro 648 RGBW": [
        ("red", 3), ("green", 3), ("blue", 3), ("white", 3),
    ],
}

# (bank name, scene count) in declaration order — the order IS semantic: note
# 36+n selects the n-th bank and the first one is the fixture's boot bank.
GOLDEN_BANKS = {
    "ledbar": [("ANIMATIONS", 8), ("FILLS", 8), ("FILLS2", 8), ("MULTICOLOR", 8),
               ("STROBES", 8), ("TUBES", 8), ("RADARS", 8)],
    "blinder": [("BLINDER_SCENES", 8)],
    "par_rgb": [],
    "Wild Wash Pro 648 RGBW": [],
}

GOLDEN_CHASES = {
    "ledbar": 15, "blinder": 2, "par_rgb": 0,
    "Wild Wash Pro 648 RGBW": 0,
}

# song part -> [(is_chase, bank, index)], from the "song_parts" tags on scenes
# and chases. Drives the timeline's auto-mode visualization and the controller's
# per-part scene picking.
GOLDEN_SONG_PARTS = {
    "ledbar": {
        "INTRO": [(False, "FILLS2", 2)],
        "VERSE": [(False, "FILLS2", 1)],
        "CHORUS": [(True, "", 9), (True, "", 10), (True, "", 11),
                   (True, "", 12), (True, "", 13), (True, "", 14)],
        "BRIDGE": [(False, "ANIMATIONS", 6)],
        "INST": [(False, "ANIMATIONS", 5), (False, "ANIMATIONS", 6),
                 (False, "ANIMATIONS", 7)],
        "SOLO": [(False, "ANIMATIONS", 5), (False, "ANIMATIONS", 6),
                 (False, "ANIMATIONS", 7)],
        "OUTRO": [(True, "", 0), (True, "", 1), (True, "", 2)],
    },
    "blinder": {
        "BREAK": [(False, "BLINDER_SCENES", 1)],
        "BRIDGE": [(True, "", 1)],
        "INST": [(True, "", 1)],
        "SOLO": [(True, "", 1)],
        "OUTRO": [(False, "BLINDER_SCENES", 3)],
        "END": [(True, "", 0)],
    },
    "par_rgb": {},
    "Wild Wash Pro 648 RGBW": {},
}


def _channels(fdef):
    return [(c.field, c.flags) for c in fdef.channels]


def _banks(fdef):
    return [(name, len(scenes)) for name, scenes in fdef.banks.items()]


def _song_parts(fdef):
    return {part: [(r.is_chase, r.bank, r.index) for r in refs]
            for part, refs in note_layout.song_part_refs(fdef).items()}


def test_all_shipped_types_resolve():
    print("test_all_shipped_types_resolve")
    defs = load_fixture_defs()
    for name in GOLDEN_CHANNELS:
        _assert(name in defs, f"shipped type '{name}' resolves")


def test_channel_order_and_flags():
    print("test_channel_order_and_flags")
    defs = load_fixture_defs()
    for name, golden in GOLDEN_CHANNELS.items():
        _assert(_channels(defs[name]) == golden,
                f"{name}: channel order + flags unchanged")


def test_blend_flag_tracks_channel_kind():
    print("test_blend_flag_tracks_channel_kind")
    # A channel blends iff its dominant capability is a level worth fading
    # between — colour, intensity, zoom, a mover's pan/tilt — and snaps
    # otherwise, because interpolating an effect or a mode selector would sweep
    # through every program in between. Checked across every fixture the machine
    # has installed, not just the shipped four, and with no per-channel override
    # in sight: this is what the capability types alone must produce.
    BLEND = 0x01
    for name, fdef in load_fixture_defs().items():
        for spec in fdef.channels:
            if spec.field in _OVERRIDDEN_FLAGS.get(name, ()):
                continue
            cap = dominant_capability(spec.capabilities)
            blends = cap is not None and cap.type in _BLENDS
            _assert(bool(spec.flags & BLEND) == blends,
                    f"{name}.{spec.field} ({cap.type if cap else 'unused'}) "
                    f"{'blends' if blends else 'snaps'}")


def test_knob_flags_are_not_derivable():
    print("test_knob_flags_are_not_derivable")
    # Two brightness channels, same capability, different knob_level — so knob
    # flags cannot come from the channel's kind and must stay author-declared.
    defs = load_fixture_defs()
    KNOB_LEVEL = 0x02
    ledbar = next(c for c in defs["ledbar"].channels if c.field == "brightness")
    blinder = next(c for c in defs["blinder"].channels if c.field == "brightness")
    _assert(ledbar.flags & KNOB_LEVEL, "ledbar brightness has knob_level")
    _assert(not blinder.flags & KNOB_LEVEL, "blinder brightness does not")


def test_banks_and_chases():
    print("test_banks_and_chases")
    defs = load_fixture_defs()
    for name, golden in GOLDEN_BANKS.items():
        _assert(_banks(defs[name]) == golden,
                f"{name}: bank names, order and scene counts unchanged")
        _assert(len(defs[name].chases) == GOLDEN_CHASES[name],
                f"{name}: {GOLDEN_CHASES[name]} chases")


def test_field_note_map():
    print("test_field_note_map")
    # The note<->channel binding every saved .mid depends on.
    defs = load_fixture_defs()
    for name, golden in GOLDEN_CHANNELS.items():
        expected = tuple((field, i + 1) for i, (field, _flags) in enumerate(golden))
        _assert(note_layout.field_note_map(defs[name]) == expected,
                f"{name}: note n binds to the same channel as before")


def test_song_part_program():
    print("test_song_part_program")
    defs = load_fixture_defs()
    for name, golden in GOLDEN_SONG_PARTS.items():
        _assert(_song_parts(defs[name]) == golden,
                f"{name}: song-part program unchanged")


def test_animation_capabilities_reproduce_the_gif_mapping():
    print("test_animation_capabilities_reproduce_the_gif_mapping")
    # The ledbar used to pick its animation arithmetically: `dmx // 4 + 1`
    # indexed the shipped GIFs. That arithmetic is now spelled out as one Effect
    # capability per animation, with the effect name resolving to a file through
    # the sidecar. Walk all 256 values and demand the same asset either way.
    import glob
    from core.paths import asset_path

    fdef = load_fixture_defs()["ledbar"]
    animation = next(c for c in fdef.channels if c.field == "animation")

    def legacy_gif(value):
        found = glob.glob(os.path.join(asset_path("animations"),
                                       f"{value // 4 + 1:02d}-*.gif"))
        return os.path.basename(found[0]) if found else None

    def ofl_gif(value):
        cap = next((c for c in animation.capabilities
                    if c.dmx_range[0] <= value <= c.dmx_range[1]), None)
        if cap is None or cap.type != "Effect":
            return None
        return fdef.effects.get(str(cap.get("effectName", "")), {}).get("gif")

    mismatches = [v for v in range(256) if ofl_gif(v) != legacy_gif(v)]
    _assert(not mismatches,
            f"all 256 animation values select the same GIF as before "
            f"(first mismatch: {mismatches[:1]})")
    _assert(len({ofl_gif(v) for v in range(256)} - {None}) == 35,
            "all 35 animations stay reachable")
    _assert(ofl_gif(140) is None and ofl_gif(255) is None,
            "values past the last animation still fall through to a flat colour")


def main():
    test_all_shipped_types_resolve()
    test_channel_order_and_flags()
    test_blend_flag_tracks_channel_kind()
    test_knob_flags_are_not_derivable()
    test_banks_and_chases()
    test_field_note_map()
    test_song_part_program()
    test_animation_capabilities_reproduce_the_gif_mapping()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
