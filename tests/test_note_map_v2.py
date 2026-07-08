"""
Characterization test: the generalized (channel = fixture) MIDI note map.

Subtask 4 makes handle_note_on route by MIDI channel: the note is a universal
verb and the channel selects which fixture in the patch it targets. This guards
the routing + safeguards on the default shipped patch
([ledbar@1 (7ch), blinder@33 (8ch), blinder@65 (8ch)]).

No Art-Net / threads are started; the controller is driven by handle_note_on.

Run with:
    uv run python tests/test_note_map_v2.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from software_controller.main import DMXController, COLOR32_WHITE
from core.config import FixtureInstance


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


# DMX bases (0-indexed) for the shipped patch.
LEDBAR_BASE, BLINDER1_BASE, BLINDER2_BASE = 0, 32, 64


def test_channel_selects_fixture():
    print("test_channel_selects_fixture")
    ctrl = DMXController()

    # Note 1 = channel offset 1 (the fixture's first DMX channel). The MIDI
    # channel picks which fixture: 0 = ledbar, 1 = blinder1, 2 = blinder2.
    ctrl.handle_note_on(0, 1, 100)
    ctrl.handle_note_on(1, 1, 90)
    ctrl.handle_note_on(2, 1, 80)
    _assert(ctrl.present[LEDBAR_BASE] == 200, "ch0 note1 -> ledbar ch1 = 200")
    _assert(ctrl.present[BLINDER1_BASE] == 180, "ch1 note1 -> blinder1 ch1 = 180")
    _assert(ctrl.present[BLINDER2_BASE] == 160, "ch2 note1 -> blinder2 ch1 = 160")


def test_offset_and_channel_bounds_are_safe():
    print("test_offset_and_channel_bounds_are_safe")
    ctrl = DMXController()

    # Offset past the fixture's channel count is ignored (no bleed into the
    # next fixture's DMX range). ledbar has 7 channels; offset 8 -> present[7].
    ctrl.handle_note_on(0, 8, 127)
    _assert(ctrl.present[7] == 0, "offset > channel_count is ignored")

    # A channel with no patched fixture is ignored (only 3 fixtures: 0,1,2).
    before = list(ctrl.present)
    ctrl.handle_note_on(3, 1, 127)
    _assert(ctrl.present == before, "note on an unpatched channel is a no-op")


def test_note5_sets_channel_not_manual_control():
    print("test_note5_sets_channel_not_manual_control")
    ctrl = DMXController()
    # Note 5 is now channel offset 5 (ledbar animation), NOT the manual-control
    # trigger (that moved to note 48). It must not blackout other fixtures.
    ctrl.handle_note_on(0, 5, 64)
    _assert(ctrl.present[LEDBAR_BASE + 4] == 128, "note5 -> ledbar ch5 (animation) = 128")


def test_bank_and_scene_verbs():
    print("test_bank_and_scene_verbs")
    ctrl = DMXController()
    led = ctrl.fixtures[0]
    banks = list(led.type.banks.keys())

    # Bank select: note 36 + idx -> the idx-th bank.
    ctrl.handle_note_on(0, 36 + 2, 0)
    _assert(led.bank == banks[2], "note 38 selects the 3rd ledbar bank")

    # Scene loader: note 24 + scene -> load that scene of the current bank.
    ctrl.handle_note_on(0, 24 + 1, 100)
    _assert((led.scene, led.is_blackout) == (1, False),
            "note 25 loads scene 1 of the current bank")

    # Next/previous scene (notes 58/57) step within the bank.
    ctrl.handle_note_on(0, 58, 100)
    _assert(led.scene == 2, "note 58 -> next scene")
    ctrl.handle_note_on(0, 57, 100)
    _assert(led.scene == 1, "note 57 -> previous scene")


def test_blackout_verbs_and_global_song_parts():
    print("test_blackout_verbs_and_global_song_parts")
    ctrl = DMXController()
    # Light the ledbar via a direct channel set, then blackout just it.
    ctrl.handle_note_on(0, 1, 100)
    _assert(ctrl.present[LEDBAR_BASE] == 200, "ledbar lit via channel set")
    ctrl.handle_note_on(0, 50, 0)
    _assert(not any(ctrl.present[LEDBAR_BASE:LEDBAR_BASE + 7]),
            "note 50 ch0 blacks out the ledbar")

    # Song parts (61-69) are global: they run even on an unpatched channel.
    # (Auto-mode fixtures follow the program; opt them all in for this check.)
    ctrl = DMXController()
    for fx in ctrl.fixtures:
        fx.auto = True
    ctrl.handle_note_on(5, 64, 64)  # BREAK on a bogus channel
    b1 = ctrl.fixtures[1]
    _assert((b1.bank, b1.scene, b1.is_blackout) == ("BLINDER_SCENES", 1, False),
            "BREAK song part is global (runs on an out-of-range channel)")

    # blackout-all toggle (note 51) is global too.
    ctrl.handle_note_on(7, 51, 0)
    _assert(ctrl.blackout_all_state is True, "note 51 toggles blackout-all globally")


def test_repatch_hot_swaps_routing():
    print("test_repatch_hot_swaps_routing")
    ctrl = DMXController()
    ctrl.handle_note_on(0, 1, 100)
    _assert(ctrl.present[LEDBAR_BASE] == 200, "pre-repatch: ch0 lights the ledbar")

    # Swap to a patch where channel 0 is a blinder at DMX 33, and add a new
    # fixture on channel 5. repatch clears the universe and rebuilds routing.
    ctrl.repatch([
        FixtureInstance(type="blinder", dmx_start=33, channel=0),
        FixtureInstance(type="ledbar", dmx_start=1, channel=5),
    ])
    _assert(ctrl.present[LEDBAR_BASE] == 0, "repatch cleared the old universe values")
    _assert(ctrl.num_fixtures == 2, "fixture count reflects the new patch")

    ctrl.handle_note_on(0, 1, 50)   # ch0 is now the blinder @33 (base 32)
    _assert(ctrl.present[32] == 100, "ch0 now routes to the blinder at DMX 33")
    ctrl.handle_note_on(5, 1, 60)   # the added fixture on channel 5
    _assert(ctrl.present[0] == 120, "ch5 routes to the ledbar at DMX 1")
    ctrl.handle_note_on(1, 1, 70)   # channel 1 is no longer patched
    _assert(ctrl.present[32] == 100, "note on a now-unpatched channel is a no-op")


def test_repatch_to_empty_is_safe():
    print("test_repatch_to_empty_is_safe")
    ctrl = DMXController()
    ctrl.repatch([])
    _assert(ctrl.num_fixtures == 0, "empty patch -> no fixtures")
    before = list(ctrl.present)
    ctrl.handle_note_on(0, 1, 127)
    _assert(ctrl.present == before, "any note is a no-op on an empty patch (no crash)")


def main():
    test_channel_selects_fixture()
    test_offset_and_channel_bounds_are_safe()
    test_note5_sets_channel_not_manual_control()
    test_bank_and_scene_verbs()
    test_blackout_verbs_and_global_song_parts()
    test_repatch_hot_swaps_routing()
    test_repatch_to_empty_is_safe()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
