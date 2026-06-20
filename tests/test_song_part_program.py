"""
Characterization test: the data-driven song-part program.

pick_scene_from_song_part used to be a hardcoded if/elif chain mapping each song
part to specific scenes/chases. It is now driven by "song_parts" tags on the
scenes/chases in the fixture defs, indexed into FixtureType.song_part_program.
This guards that the tags reproduce the old per-part targets.

No Art-Net / threads are started; DMXController.__init__ only builds buffers, and
the program is driven by calling handle_note_on directly.

Run with:
    uv run python tests/test_song_part_program.py
"""

import sys
import os
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from software_controller.main import (
    DMXController,
    FIXTURE_TYPES,
    SongPartEntry,
)

# MIDI notes 41-49 -> song parts 1-9 (see handle_note_on).
INTRO, VERSE, CHORUS, BREAK, BRIDGE, INST, SOLO, OUTRO, END = range(41, 50)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_program_index_matches_legacy_targets():
    print("test_program_index_matches_legacy_targets")
    led = FIXTURE_TYPES["ledbar"].song_part_program
    blinder = FIXTURE_TYPES["blinder"].song_part_program

    _assert(led["INTRO"] == [SongPartEntry(False, "FILLS2", 2)],
            "INTRO -> ledbar FILLS2 scene 2")
    _assert(led["VERSE"] == [SongPartEntry(False, "FILLS2", 1)],
            "VERSE -> ledbar FILLS2 scene 1")
    _assert(led["CHORUS"] == [SongPartEntry(True, "", i) for i in range(9, 15)],
            "CHORUS -> ledbar chases 9..14")
    _assert(sorted(e.index for e in led["INST"]) == [5, 6, 7] and
            all(e.bank == "ANIMATIONS" and not e.is_chase for e in led["INST"]),
            "INST -> ledbar ANIMATIONS scenes 5,6,7")
    _assert(led["INST"] == led["SOLO"], "SOLO mirrors INST on the ledbar")
    _assert(led["BRIDGE"] == [SongPartEntry(False, "ANIMATIONS", 6)],
            "BRIDGE -> ledbar ANIMATIONS scene 6")
    _assert(led["OUTRO"] == [SongPartEntry(True, "", i) for i in range(0, 3)],
            "OUTRO -> ledbar chases 0..2")
    _assert("BREAK" not in led and "END" not in led,
            "ledbar has nothing for BREAK / END (stays blacked out)")

    _assert(blinder["BREAK"] == [SongPartEntry(False, "BLINDER_SCENES", 1)],
            "BREAK -> blinder BLINDER_SCENES scene 1")
    _assert(blinder["OUTRO"] == [SongPartEntry(False, "BLINDER_SCENES", 3)],
            "OUTRO -> blinder BLINDER_SCENES scene 3")
    _assert(blinder["BRIDGE"] == blinder["INST"] == blinder["SOLO"] ==
            [SongPartEntry(True, "", 1)],
            "BRIDGE/INST/SOLO -> blinder chase 1")
    _assert(blinder["END"] == [SongPartEntry(True, "", 0)],
            "END -> blinder chase 0")
    _assert("INTRO" not in blinder and "VERSE" not in blinder and
            "CHORUS" not in blinder,
            "blinder has nothing for INTRO / VERSE / CHORUS (stays blacked out)")


def _fx(ctrl):
    return (ctrl.fixtures[ctrl.led],
            ctrl.fixtures[ctrl.blinder1],
            ctrl.fixtures[ctrl.blinder2])


def test_deterministic_parts_load_expected_scenes():
    print("test_deterministic_parts_load_expected_scenes")

    ctrl = DMXController()
    ctrl.handle_note_on(0, INTRO, 64)
    led, b1, b2 = _fx(ctrl)
    _assert((led.bank, led.scene, led.is_blackout) == ("FILLS2", 2, False),
            "INTRO loads FILLS2/2 on the LED")
    _assert(b1.is_blackout and b2.is_blackout, "INTRO leaves blinders blacked out")
    _assert(b1.song_part_triggered and b2.song_part_triggered,
            "INTRO flags blinders as song-part-owned")
    _assert(not led.song_part_triggered, "LED is not flagged song-part-owned")

    ctrl = DMXController()
    ctrl.handle_note_on(0, VERSE, 64)
    led, b1, b2 = _fx(ctrl)
    _assert((led.bank, led.scene, led.is_blackout) == ("FILLS2", 1, False),
            "VERSE loads FILLS2/1 on the LED")
    _assert(b1.is_blackout and b2.is_blackout, "VERSE leaves blinders blacked out")

    ctrl = DMXController()
    ctrl.handle_note_on(0, BREAK, 64)
    led, b1, b2 = _fx(ctrl)
    _assert(led.is_blackout, "BREAK leaves the LED blacked out")
    _assert((b1.bank, b1.scene, b1.is_blackout) == ("BLINDER_SCENES", 1, False) and
            (b2.bank, b2.scene, b2.is_blackout) == ("BLINDER_SCENES", 1, False),
            "BREAK loads BLINDER_SCENES/1 on both blinders")


def test_random_parts_stay_in_range():
    print("test_random_parts_stay_in_range")
    random.seed(0)
    for _ in range(50):
        ctrl = DMXController()
        ctrl.handle_note_on(0, CHORUS, 64)
        led = ctrl.fixtures[ctrl.led]
        _assert(led.is_chasing and 9 <= led.chase <= 14,
                "CHORUS runs a ledbar chase in 9..14")

        ctrl = DMXController()
        ctrl.handle_note_on(0, OUTRO, 64)
        led = ctrl.fixtures[ctrl.led]
        b1 = ctrl.fixtures[ctrl.blinder1]
        _assert(led.is_chasing and 0 <= led.chase <= 2,
                "OUTRO runs a ledbar chase in 0..2")
        _assert((b1.bank, b1.scene, b1.is_blackout) == ("BLINDER_SCENES", 3, False),
                "OUTRO loads BLINDER_SCENES/3 on the blinders")


def main():
    test_program_index_matches_legacy_targets()
    test_deterministic_parts_load_expected_scenes()
    test_random_parts_stay_in_range()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
