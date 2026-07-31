"""
Characterization test: the shipped fixture definitions, as the rest of the app
sees them.

This is the safety net for the Open Fixture Library migration. It pins the
*resolved* shape of every shipped type — channel order, semantic roles, behaviour
flags, scene banks, chases, the note<->field map and the song-part program —
against golden data captured from the pre-OFL format. Converting the defs must
not move any of these numbers.

Two of them are load-bearing beyond "the def parses":

  * `field_note_map` is the note<->channel binding every saved .mid depends on
    (note n = the n-th channel of the fixture). The ledbar's BGR channel order is
    deliberate; reordering it would make every scene clip in every show play the
    wrong colours, silently.
  * the golden `flags` show that `blend` tracks the channel's kind (every
    intensity/colour channel blends, no effect/strobe/mode channel does) while
    `knob_level`/`knob_speed` do NOT — the ledbar's brightness carries
    knob_level and the blinder's doesn't. Only the former is derivable.

Run with:
    uv run python tests/test_ofl_parity.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.fixture_library import load_fixture_defs
from core import note_layout


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


# (field, role, flags) per channel, in DMX order; roles/flags are the ROLE_*/
# FLAG_NAMES ints. flags: 1=blend, 2=knob_level, 4=knob_speed.
GOLDEN_CHANNELS = {
    "ledbar": [
        ("brightness", 1, 3), ("blue", 4, 1), ("green", 3, 1), ("red", 2, 1),
        ("animation", 7, 0), ("speed", 8, 4), ("direction", 9, 0),
    ],
    "blinder": [
        ("brightness", 1, 1), ("cool1", 6, 1), ("cool2", 6, 1),
        ("warm1", 5, 1), ("warm2", 5, 1),
        ("strobe", 10, 0), ("mode", 11, 0), ("speed", 8, 0),
    ],
    "par_rgb": [
        ("brightness", 1, 3), ("red", 2, 1), ("green", 3, 1), ("blue", 4, 1),
    ],
    "Wild Wash Pro 648 RGBW, 4ch mode": [
        ("red", 2, 3), ("green", 3, 3), ("blue", 4, 3), ("white", 6, 3),
    ],
}

# (bank name, scene count) in declaration order — the order IS semantic: note
# 36+n selects the n-th bank and the first one is the fixture's boot bank.
GOLDEN_BANKS = {
    "ledbar": [("ANIMATIONS", 8), ("FILLS", 8), ("FILLS2", 8), ("MULTICOLOR", 8),
               ("STROBES", 8), ("TUBES", 8), ("RADARS", 8)],
    "blinder": [("BLINDER_SCENES", 8)],
    "par_rgb": [],
    "Wild Wash Pro 648 RGBW, 4ch mode": [],
}

GOLDEN_CHASES = {
    "ledbar": 15, "blinder": 2, "par_rgb": 0,
    "Wild Wash Pro 648 RGBW, 4ch mode": 0,
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
    "Wild Wash Pro 648 RGBW, 4ch mode": {},
}


def _channels(fdef):
    return [(c.field, c.role, c.flags) for c in fdef.channels]


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


def test_channel_order_roles_and_flags():
    print("test_channel_order_roles_and_flags")
    defs = load_fixture_defs()
    for name, golden in GOLDEN_CHANNELS.items():
        _assert(_channels(defs[name]) == golden,
                f"{name}: channel order + roles + flags unchanged")


def test_blend_flag_tracks_channel_kind():
    print("test_blend_flag_tracks_channel_kind")
    # Every intensity/colour channel blends; no effect/strobe/mode/direction
    # channel does. This is the invariant the OFL capability-type mapping must
    # reproduce with no per-channel overrides.
    defs = load_fixture_defs()
    BLEND = 0x01
    colour_roles = {1, 2, 3, 4, 5, 6}  # brightness + red/green/blue + warm/cool
    for name, fdef in defs.items():
        for ch in fdef.channels:
            if ch.role in colour_roles:
                _assert(ch.flags & BLEND, f"{name}.{ch.field} (colour/intensity) blends")
            else:
                _assert(not ch.flags & BLEND, f"{name}.{ch.field} (non-colour) does not blend")


def test_knob_flags_are_not_derivable():
    print("test_knob_flags_are_not_derivable")
    # Two brightness channels, same role, different knob_level — so knob flags
    # cannot come from the channel's kind and must stay author-declared.
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
        expected = tuple((field, i + 1) for i, (field, _r, _f) in enumerate(golden))
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
    test_channel_order_roles_and_flags()
    test_blend_flag_tracks_channel_kind()
    test_knob_flags_are_not_derivable()
    test_banks_and_chases()
    test_field_note_map()
    test_song_part_program()
    test_animation_capabilities_reproduce_the_gif_mapping()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
