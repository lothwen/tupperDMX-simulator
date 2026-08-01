"""
Characterization test: the data-driven note layout (note_layout.describe_note)
and per-fixture scene handling that replaced config.json's midi_note_map.

Covers:
  * describe_note resolving per-fixture verbs (channel names, scene/chase names)
    against a fixture def, and fixed labels for global verbs;
  * note_applies_to gating drops by fixture capability;
  * build_note_map ordering (BLACKOUT before BLACKOUT ALL);
  * a blinder scene round-trip through MidiManager — expansion to one
    channel-control note per channel (1..8), export/reload re-clustering, and
    scene-name recovery from the fixture's own library;
  * the chase-load verb (note 70) on the software controller.

Run with:
    uv run python tests/test_note_layout.py
"""

import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import note_layout
from core.fixture_library import load_fixture_defs
from midi.manager import MidiManager
from software_controller.main import DMXController
from software_controller.scene_loader import load_scenes


@dataclass
class FakeFixture:
    type: str


PATCH = [FakeFixture("ledbar"), FakeFixture("blinder"), FakeFixture("blinder")]


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_describe_note_per_fixture():
    print("test_describe_note_per_fixture")
    defs = load_fixture_defs()
    led, bli = defs["ledbar"], defs["blinder"]

    _assert(note_layout.describe_note(1, led).part == "Intensity",
            "note 1 -> what the fixture's first channel does")
    _assert(note_layout.describe_note(4, led).part == "Red",
            "note 4 -> ledbar Red channel, named by its OFL colour")
    _assert(note_layout.describe_note(4, led).color == "#aa3333",
            "Red channel gets a red swatch")
    _assert(note_layout.describe_note(7, led).part == "Direction",
            "note 7 -> the sidecar label, for a channel OFL only calls Generic")
    # Note 8 is the blinder's 8th channel; for the 7-channel ledbar it resolves
    # to a generic label (the applicability gate is note_applies_to, below).
    _assert(note_layout.describe_note(8, bli).part == "Speed",
            "note 8 -> blinder's 8th channel (Speed)")
    _assert(note_layout.describe_note(8, led).part == "CH 8",
            "note 8 -> generic label for the 7-channel ledbar")
    # Scene / chase names come from the fixture def.
    _assert(note_layout.describe_note(24, bli, 0).part
            == load_scenes("blinder")[0].name,
            "note 24 -> first scene name of the fixture's bank")
    _assert(note_layout.describe_note(70, bli, 0).part.startswith(
                ("Chase", bli.chases[0].get("name", "Chase"))),
            "note 70 -> a chase label")
    # Global verbs are fixture-agnostic and fixed.
    _assert(note_layout.describe_note(63).part == "CHORUS",
            "note 63 -> CHORUS (global)")
    _assert(note_layout.describe_note(72).part == "COLOR: WHITE",
            "note 72 -> colour macro label")


def test_note_applies_to():
    print("test_note_applies_to")
    defs = load_fixture_defs()
    led, bli = defs["ledbar"], defs["blinder"]
    _assert(note_layout.note_applies_to(8, bli) is True,
            "channel-8 note applies to the blinder")
    _assert(note_layout.note_applies_to(8, led) is False,
            "channel-8 note does not apply to the 7-channel ledbar")
    _assert(note_layout.note_applies_to(63, led) is True,
            "song parts apply to any lane")
    _assert(note_layout.note_applies_to(70, led) is bool(led.chases),
            "chase verb applies only when the fixture defines chases")


def test_build_note_map_order():
    print("test_build_note_map_order")
    m = note_layout.build_note_map()
    _assert("50" in m and "51" in m, "blackout verbs present in the note map")
    _assert(list(m).index("50") < list(m).index("51"),
            "BLACKOUT (50) precedes BLACKOUT ALL (51) for prefix-search")


def test_blinder_scene_roundtrip(tmp_path=None):
    print("test_blinder_scene_roundtrip")
    import tempfile
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    m.set_song_duration(60.0)

    scene = load_scenes("blinder")[0]
    gid = m.add_scene(2.0, dict(scene.values), scene.name, channel=1)
    _assert(gid is not None, "blinder scene added")
    members = [e for e in m.midi_events if e.get("group_id") == gid]
    ctrl = sorted(e["note"] for e in members
                  if e["note"] != note_layout.TAKE_MANUAL)
    _assert(ctrl == list(range(1, 9)),
            "blinder scene expands to one note per channel (1..8)")
    _assert(any(e["note"] == note_layout.TAKE_MANUAL for e in members),
            "take-manual (48) folded into the blinder scene group")
    _assert({e["channel"] for e in members} == {1},
            "all members route to the blinder's channel (1)")

    # Export + reload: the group_id is ephemeral, so reattach_groups must
    # re-cluster the 8 same-time, same-channel events and recover the name.
    path = os.path.join(tempfile.gettempdir(), "_blinder_rt.mid")
    try:
        _assert(m.export_to_file(path), "export blinder scene")
        m2 = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
        _assert(m2.load_from_file(path), "reload blinder scene")
        g2 = [e for e in m2.midi_events if e.get("group_id")]
        ctrl2 = sorted(e["note"] for e in g2
                       if e["note"] != note_layout.TAKE_MANUAL)
        _assert(ctrl2 == list(range(1, 9)),
                "all 8 channel notes survive the round-trip")
        _assert(any(e["note"] == note_layout.TAKE_MANUAL for e in g2),
                "take-manual survives the round-trip and regroups on reload")
        _assert(g2[0]["scene_name"] == scene.name,
                "scene name recovered from the blinder's library")
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_chase_load_verb():
    print("test_chase_load_verb")
    c = DMXController()
    # Blinder is patched at channel 1 by default and defines chases.
    c.handle_note_on(1, note_layout.CHASE_LOAD, 0)
    _assert(c.fixtures[1].is_chasing, "note 70 starts a chase on the fixture")
    # Out-of-range chase index is ignored without raising.
    c.handle_note_on(1, note_layout.CHASE_LOAD, 999)
    _assert(True, "out-of-range chase index is a no-op")


def main():
    test_describe_note_per_fixture()
    test_note_applies_to()
    test_build_note_map_order()
    test_blinder_scene_roundtrip()
    test_chase_load_verb()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
