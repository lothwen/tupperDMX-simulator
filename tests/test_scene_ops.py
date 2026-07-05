"""
Characterization test: scene-clip group ops on MidiManager.

A scene is 7 simultaneous channel-control note_on events (notes 1..7) plus a
take-manual verb (note 48), all sharing a group_id (8 members). Covers
add_scene / move_scene / update_scene / delete_scene / set_scene_left_edge /
set_scene_right_edge, asserting the group stays atomic (8 members, one
timestamp) and gap markers behave. Locks current behavior before the
collaborator-object refactor.

Run with:
    uv run python tests/test_scene_ops.py
"""

import sys
import os
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import note_layout
from midi.manager import MidiManager, _channel_to_velocity


@dataclass
class FakeFixture:
    """Minimal patched-fixture stand-in: its index in the patch == the MIDI
    channel that selects it (the generalized note map routes by channel)."""
    type: str


# Patch: ledbar @ channel 0, blinder @ channel 1 (the shipped patch's first two
# fixtures). Mirrors production: MidiManager gets the data-driven note map plus
# the patch, and every clip is stamped with the channel of its target fixture.
PATCH = [FakeFixture("ledbar"), FakeFixture("blinder")]
LEDBAR_CH, BLINDER_CH = 0, 1
# A standalone single-note ledbar clip: note 24 is a scene-load verb (resolved
# against the ledbar's own def via describe_note), not a channel-control note,
# so it never gets clustered into a scene group.
LEDBAR_CLIP = 24

SCENE_CH = {"brightness": 255, "blue": 255, "green": 0, "red": 0,
            "animation": 0, "speed": 0, "direction": 0}


def _make_manager():
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    m.set_song_duration(240.0)
    return m


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _members(m, gid):
    return [e for e in m.midi_events if e.get("group_id") == gid]


def _ledbar_blackouts(m):
    return sorted(e["time"] for e in m.midi_events
                  if m._event_fixture(e) == LEDBAR_CH and m.is_blackout(e))


def test_add_scene_atomic():
    print("test_add_scene_atomic")
    m = _make_manager()
    g = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
    _assert(g is not None, "add_scene returned a group_id")
    members = _members(m, g)
    ctrl = [mm for mm in members if 1 <= mm["note"] <= 7]
    _assert(len(ctrl) == 7, f"7 channel-control members (got {len(ctrl)})")
    _assert(any(mm["note"] == note_layout.TAKE_MANUAL for mm in members),
            "take-manual (48) folded into the scene group")
    _assert(len(members) == 8, f"group has 8 members (got {len(members)})")
    _assert(len({mm["time"] for mm in members}) == 1, "all members share one timestamp")
    _assert(all(abs(mm["time"] - 4.0) < 1e-6 for mm in members), "members at 4.0s")
    bright = next(mm for mm in members if mm["note"] == 1)
    _assert(bright["velocity"] == _channel_to_velocity(255), "brightness velocity = 255//2")


def test_move_scene():
    print("test_move_scene")
    m = _make_manager()
    g = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
    _assert(m.move_scene(g, 9.0), "move_scene returned True")
    members = _members(m, g)
    _assert(len(members) == 8, "still 8 members after move")
    _assert(all(abs(mm["time"] - 9.0) < 1e-6 for mm in members), "all members at 9.0s")


def test_update_scene():
    print("test_update_scene")
    m = _make_manager()
    g = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
    _assert(m.update_scene(g, {"red": 200}, name="A2"), "update_scene returned True")
    red = next(mm for mm in _members(m, g) if mm["note"] == 4)
    _assert(red["velocity"] == _channel_to_velocity(200), "red velocity updated to 200//2")
    _assert(all(mm["scene_name"] == "A2" for mm in _members(m, g)), "scene renamed to A2")


def test_set_scene_right_edge_caps_with_blackout():
    print("test_set_scene_right_edge_caps_with_blackout")
    m = _make_manager()
    g = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
    _assert(m.set_scene_right_edge(g, 9.0), "set_scene_right_edge returned True")
    _assert(any(abs(t - 9.0) < 1e-6 for t in _ledbar_blackouts(m)),
            f"BLACKOUT cap at 9.0 (got {_ledbar_blackouts(m)})")
    _assert(all(abs(mm["time"] - 4.0) < 1e-6 for mm in _members(m, g)),
            "group start unchanged at 4.0s")


def test_set_scene_left_edge_moves_start():
    print("test_set_scene_left_edge_moves_start")
    m = _make_manager()
    m.add_event(LEDBAR_CLIP, 1.0, 100, channel=LEDBAR_CH)  # predecessor clip
    g = m.add_scene(5.0, SCENE_CH, "A", channel=LEDBAR_CH)
    m.add_scene(20.0, SCENE_CH, "B", channel=LEDBAR_CH)    # finite right edge
    _assert(m.set_scene_left_edge(g, 9.0), "set_scene_left_edge returned True")
    _assert(all(abs(mm["time"] - 9.0) < 1e-6 for mm in _members(m, g)),
            "group A start moved to 9.0s")
    # Shrinking A's left edge away from its predecessor exposes [5,9); since a
    # real clip precedes A, that gap is capped with a BLACKOUT at A's old start.
    _assert(any(abs(t - 5.0) < 1e-6 for t in _ledbar_blackouts(m)),
            f"exposed gap capped with BLACKOUT at old start 5.0 (got {_ledbar_blackouts(m)})")


def test_delete_scene_replaces_with_blackout():
    print("test_delete_scene_replaces_with_blackout")
    m = _make_manager()
    g = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
    _assert(m.delete_scene(g), "delete_scene returned True")
    _assert(not _members(m, g), "no group members remain")
    _assert(any(abs(t - 4.0) < 1e-6 for t in _ledbar_blackouts(m)),
            "BLACKOUT inserted at the deleted group's start")


def main():
    test_add_scene_atomic()
    test_move_scene()
    test_update_scene()
    test_set_scene_right_edge_caps_with_blackout()
    test_set_scene_left_edge_moves_start()
    test_delete_scene_replaces_with_blackout()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
