"""
Regression: song-part markers (61-69) must not occupy a fixture's lane.

Song parts are GLOBAL verbs. The timeline draws them on the arrangement strip
and `MidiEventsTrack.matches_filter` keeps them off every fixture lane — but
the document scopes events by `_event_fixture`, which reads the MIDI channel.
A song part is authored with `channel=None`, yet the .mid can only carry it on
a real channel (the codec writes `channel or 0`), so after one save/reload
every INTRO/VERSE/CHORUS claims to belong to whichever fixture is patched at
MIDI channel 0.

The document then treats those markers as non-BLACKOUT obstacles on that
fixture's lane: a clip dragged there lands in a sliver between two sections
instead of where it was dropped, and takes the sliver's width. The view, which
filtered them out, offered the whole lane. This asserts the two agree.

Run with:
    uv run python tests/test_song_part_lane_scope.py
"""

import os
import sys
import tempfile
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import note_layout
from midi.manager import MidiManager


@dataclass
class FakeFixture:
    """Patched-fixture stand-in; its index in the patch == its MIDI channel."""
    type: str


PATCH = [FakeFixture("ledbar"), FakeFixture("blinder")]
LEDBAR_CH = 0
INTRO, VERSE, CHORUS = 61, 62, 63
# A ledbar scene-load verb: a standalone clip the codec never clusters.
LEDBAR_CLIP = 24


def _make_manager():
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    m.set_song_duration(240.0)
    return m


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _roundtrip(m):
    """Save + reload — the step that stamps channel 0 onto the song parts."""
    path = tempfile.mktemp(suffix=".mid")
    try:
        m.export_to_file(path)
        m2 = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
        m2.load_from_file(path)
        m2.set_song_duration(240.0)
        return m2
    finally:
        if os.path.exists(path):
            os.unlink(path)


def _lane(m, channel):
    """The events the document scopes to `channel`'s lane."""
    return [e for e in m.midi_events if m._event_fixture(e) == channel]


def _clip_extent(m, event_id, channel):
    """(start, end) of `event_id` as the lane DRAWS it: up to the next
    same-lane occupant at a later time (a scene group's members all share
    one start, so same-time siblings are skipped — they are one clip)."""
    lane = sorted(_lane(m, channel), key=lambda e: e["time"])
    me = next((e for e in lane if e["id"] == event_id), None)
    if me is None:
        raise AssertionError(f"event {event_id} not on lane {channel}")
    later = [e["time"] for e in lane if e["time"] > me["time"] + 1e-6]
    return me["time"], (min(later) if later else m.song_end_time())


def test_song_parts_are_not_lane_occupants():
    """After a round-trip, a song part still belongs to no fixture lane."""
    print("\n=== song parts stay global across a save/reload ===")
    m = _make_manager()
    m.add_event(INTRO, 10.0, 127, None)
    m.add_event(VERSE, 20.0, 127, None)
    m.add_event(LEDBAR_CLIP, 40.0, 0, LEDBAR_CH)

    parts = [e for e in m.midi_events if m.is_song_part(e)]
    _assert(all(m._event_fixture(e) is None for e in parts),
            "fresh song parts scope to no fixture")

    m2 = _roundtrip(m)
    parts = [e for e in m2.midi_events if m2.is_song_part(e)]
    _assert(len(parts) == 2, "both song parts survived the round-trip")
    _assert(all(m2._event_fixture(e) is None for e in parts),
            "reloaded song parts STILL scope to no fixture")
    _assert(not any(m2.is_song_part(e) for e in _lane(m2, LEDBAR_CH)),
            "no song part sits on the ledbar lane")


def test_clip_move_ignores_song_parts():
    """A clip drags to where it was dropped, whatever sections it crosses."""
    print("\n=== a clip is not blocked by song sections ===")
    m = _make_manager()
    # Sections every 10s across the whole song, as a real arrangement has.
    for i, note in enumerate([INTRO, VERSE, CHORUS, VERSE, CHORUS]):
        m.add_event(note, 10.0 * (i + 1), 127, None)
    m.add_event(LEDBAR_CLIP, 47.5, 0, LEDBAR_CH)
    # A trailing BLACKOUT caps the clip, so it has a real width to preserve
    # (the LAST clip on a lane stretches to song end and moves width-free).
    m.add_event(50, 51.0, 100, LEDBAR_CH)
    m = _roundtrip(m)

    clip = next(e["id"] for e in m.midi_events
                if e["note"] == LEDBAR_CLIP and e.get("channel") == LEDBAR_CH)
    start, end = _clip_extent(m, clip, LEDBAR_CH)
    width = end - start
    _assert(abs(start - 47.5) < 1e-3, f"clip loaded at {start:.3f}s")

    for target in (5.0, 15.0, 23.0, 34.0):
        m2 = _roundtrip(m)
        cid = next(e["id"] for e in m2.midi_events
                   if e["note"] == LEDBAR_CLIP
                   and e.get("channel") == LEDBAR_CH)
        m2.move_event(cid, target)
        landed, new_end = _clip_extent(m2, cid, LEDBAR_CH)
        _assert(abs(landed - target) < 1e-3,
                f"dropped at {target}s -> landed at {landed:.3f}s")
        _assert(abs((new_end - landed) - width) < 1e-3,
                f"dropped at {target}s -> kept its width "
                f"({new_end - landed:.3f}s, was {width:.3f}s)")


def test_scene_move_ignores_song_parts():
    """Same for a scene group — the case the show-test ledbar hits."""
    print("\n=== a scene group is not blocked by song sections ===")
    m = _make_manager()
    for i, note in enumerate([INTRO, VERSE, CHORUS, VERSE, CHORUS]):
        m.add_event(note, 10.0 * (i + 1), 127, None)
    gid = m.add_scene(47.5, {"brightness": 255, "blue": 255, "green": 255,
                             "red": 255, "animation": 8, "speed": 255,
                             "direction": 0},
                      name="Fade In/Out fast", channel=LEDBAR_CH)
    _assert(gid is not None, "scene created")
    # A trailing BLACKOUT caps the scene, exactly as the real file does.
    m.add_event(50, 51.0, 100, LEDBAR_CH)
    m = _roundtrip(m)

    gid = next(e["group_id"] for e in m.midi_events if e.get("group_id"))
    members = [e for e in m.midi_events if e.get("group_id") == gid]
    start = members[0]["time"]
    _, end = _clip_extent(m, members[0]["id"], LEDBAR_CH)
    width = end - start
    _assert(abs(width - 3.5) < 0.05, f"scene is {width:.3f}s wide on load")

    for target in (5.0, 15.0, 23.0, 34.0):
        m2 = _roundtrip(m)
        g = next(e["group_id"] for e in m2.midi_events if e.get("group_id"))
        m2.move_scene(g, target)
        mem = [e for e in m2.midi_events if e.get("group_id") == g]
        landed = mem[0]["time"]
        _, new_end = _clip_extent(m2, mem[0]["id"], LEDBAR_CH)
        _assert(abs(landed - target) < 1e-3,
                f"dropped at {target}s -> landed at {landed:.3f}s")
        _assert(abs((new_end - landed) - width) < 0.05,
                f"dropped at {target}s -> kept its width "
                f"({new_end - landed:.3f}s, was {width:.3f}s)")


def test_blackout_not_dropped_by_a_section_beside_it():
    """A section landing on a BLACKOUT must not delete the gap marker.

    reconcile_blackouts drops a BLACKOUT whose next SAME-LANE event is within
    BLACKOUT_EPSILON. With song parts on the lane, a section boundary dropped
    next to a gap marker silently removed it — and the clip before the gap
    stretched over it.
    """
    print("\n=== a section beside a BLACKOUT leaves it alone ===")
    m = _make_manager()
    m.add_event(LEDBAR_CLIP, 10.0, 0, LEDBAR_CH)
    m.add_event(50, 20.0, 100, LEDBAR_CH)        # gap marker: clip ends at 20s
    m.add_event(LEDBAR_CLIP, 30.0, 1, LEDBAR_CH)
    m.add_event(CHORUS, 20.0, 127, None)         # a section exactly on the gap
    m = _roundtrip(m)                            # stamps the section's channel
    m._doc.reconcile_blackouts()

    gap = [e for e in m.midi_events if m.is_blackout(e)]
    _assert(len(gap) == 1, "the BLACKOUT survived the section landing on it")
    first = next(e for e in m.midi_events
                 if e["note"] == LEDBAR_CLIP and abs(e["time"] - 10.0) < 1e-6)
    _, end = _clip_extent(m, first["id"], LEDBAR_CH)
    _assert(abs(end - 20.0) < 1e-3,
            f"the first clip still ends at the gap ({end:.3f}s)")


def test_blackout_all_stays_on_its_lane():
    """BLACKOUT ALL is global at runtime but NOT in the timeline.

    Unlike a song part it is dropped on a lane and stamped with that lane's
    channel, so it keeps its lane — and keeps acting as that lane's gap
    marker. The scoping fix must not sweep it up with the song parts.
    """
    print("\n=== BLACKOUT ALL keeps the lane it was dropped on ===")
    m = _make_manager()
    m.add_event(LEDBAR_CLIP, 10.0, 0, LEDBAR_CH)
    m.add_event(51, 20.0, 127, LEDBAR_CH)   # BLACKOUT ALL on the ledbar lane
    m = _roundtrip(m)

    ba = next(e for e in m.midi_events if e["note"] == 51)
    _assert(m._event_fixture(ba) == LEDBAR_CH,
            "BLACKOUT ALL still scopes to the lane it was dropped on")
    _assert(m.is_blackout(ba), "BLACKOUT ALL still reads as a gap marker")
    clip = next(e for e in m.midi_events if e["note"] == LEDBAR_CLIP)
    _, end = _clip_extent(m, clip["id"], LEDBAR_CH)
    _assert(abs(end - 20.0) < 1e-3,
            f"it still caps the clip before it ({end:.3f}s)")


def main():
    test_song_parts_are_not_lane_occupants()
    test_clip_move_ignores_song_parts()
    test_scene_move_ignores_song_parts()
    test_blackout_not_dropped_by_a_section_beside_it()
    test_blackout_all_stays_on_its_lane()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
