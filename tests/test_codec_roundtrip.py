"""
Characterization test: MIDI file codec round-trip.

Builds a track containing single clips, BLACKOUT gap markers, scene groups, and
scene fades, then  export_to_file -> load_from_file  into a fresh manager and
asserts the observable state (events, scene groups, fade_in/out) reconstructs
equivalently. This locks the most delicate behavior in midi/manager.py:
_parse_midi_data / _rebuild_midi_data / _reattach_scene_groups /
_detect_fade_notes_on_load / _attach_fade_runs_to_groups.

These exercise pure data logic — no GUI / QApplication needed.

Run with:
    uv run python tests/test_codec_roundtrip.py
"""

import sys
import os
import tempfile
from dataclasses import dataclass

# Ensure the project root is on sys.path when run from tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from midi.manager import MidiManager


@dataclass
class FakeNoteInfo:
    part: str
    color: str
    fixture: str = "ledbar"


FAKE_NOTE_MAP = {
    "1": FakeNoteInfo(part="CH:BRIGHTNESS", color="#555555", fixture="ledbar"),
    "2": FakeNoteInfo(part="CH:BLUE", color="#3333aa", fixture="ledbar"),
    "3": FakeNoteInfo(part="CH:GREEN", color="#33aa33", fixture="ledbar"),
    "4": FakeNoteInfo(part="CH:RED", color="#aa3333", fixture="ledbar"),
    "5": FakeNoteInfo(part="CH:ANIM", color="#555555", fixture="ledbar"),
    "6": FakeNoteInfo(part="CH:SPEED", color="#555555", fixture="ledbar"),
    "7": FakeNoteInfo(part="CH:DIRECTION", color="#555555", fixture="ledbar"),
    "26": FakeNoteInfo(part="Blinder Flash", color="#ffffff", fixture="blinder"),
    "38": FakeNoteInfo(part="BLACKOUT", color="#000000", fixture="ledbar"),
    "39": FakeNoteInfo(part="BLACKOUT_BLINDERS", color="#000000", fixture="blinder"),
}

SCENE_CH = {"brightness": 255, "blue": 255, "green": 0, "red": 0,
            "animation": 0, "speed": 0, "direction": 0}


def _make_manager():
    m = MidiManager(midi_note_map=FAKE_NOTE_MAP)
    m.create_empty_track()
    m.set_song_duration(240.0)
    return m


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _non_fade_event_keys(m):
    """Stable multiset of (note, round(time,2), velocity) for all midi_events.

    midi_events never contains fade notes (those live only in midi_notes), so
    this captures every clip + BLACKOUT marker independent of uuid churn.
    """
    return sorted((e["note"], round(e["time"], 2), e["velocity"])
                  for e in m.midi_events)


def _scene_groups(m):
    """Map group start time (rounded) -> sorted member notes."""
    groups = {}
    for e in m.midi_events:
        gid = e.get("group_id")
        if not gid:
            continue
        groups.setdefault(gid, []).append(e)
    out = {}
    for gid, members in groups.items():
        t = round(members[0]["time"], 2)
        out[t] = sorted(mm["note"] for mm in members)
    return out


def _roundtrip(m):
    """Export m to a temp .mid, load into a fresh manager, return the fresh one."""
    fd, path = tempfile.mkstemp(suffix=".mid")
    os.close(fd)
    try:
        _assert(m.export_to_file(path), "export_to_file succeeded")
        m2 = MidiManager(midi_note_map=FAKE_NOTE_MAP)
        m2.set_song_duration(240.0)
        _assert(m2.load_from_file(path), "load_from_file succeeded")
        return m2
    finally:
        os.unlink(path)


def test_single_clips_and_blackouts_roundtrip():
    print("test_single_clips_and_blackouts_roundtrip")
    m = _make_manager()
    m.add_event(26, 1.0, 100)         # blinder clip
    m.add_event(26, 5.0, 64)          # blinder clip, different velocity
    # Carve a gap on the blinder lane (inserts a BLACKOUT_BLINDERS marker).
    bid = next(e["id"] for e in m.midi_events
               if e["note"] == 26 and abs(e["time"] - 1.0) < 0.01)
    m.set_event_right_edge(bid, 3.0)

    before = _non_fade_event_keys(m)
    m2 = _roundtrip(m)
    after = _non_fade_event_keys(m2)
    _assert(before == after,
            f"event keys identical across round-trip\n   before={before}\n   after ={after}")


def test_scene_group_roundtrip():
    print("test_scene_group_roundtrip")
    m = _make_manager()
    m.add_scene(4.0, SCENE_CH, "A")
    m.add_scene(12.0, SCENE_CH, "B")

    before = _scene_groups(m)
    _assert(len(before) == 2, f"two scene groups before (got {len(before)})")
    _assert(all(notes == [1, 2, 3, 4, 5, 6, 7] for notes in before.values()),
            "each group has all 7 channel notes before")

    m2 = _roundtrip(m)
    after = _scene_groups(m2)
    _assert(set(before.keys()) == set(after.keys()),
            f"group start times preserved (before={sorted(before)}, after={sorted(after)})")
    _assert(all(notes == [1, 2, 3, 4, 5, 6, 7] for notes in after.values()),
            "each reloaded group still has all 7 channel notes")


def test_scene_fade_roundtrip():
    print("test_scene_fade_roundtrip")
    m = _make_manager()
    g = m.add_scene(4.0, SCENE_CH, "A")
    m.set_scene_right_edge(g, 10.0)     # visible span [4, 10)
    m.set_scene_fade_in(g, 1.0)
    m.set_scene_fade_out(g, 1.5)

    members = [e for e in m.midi_events if e.get("group_id") == g]
    fin = members[0]["fade_in"]
    fout = members[0]["fade_out"]
    _assert(abs(fin - 1.0) < 1e-6, f"fade_in set to 1.0 (got {fin:.3f})")
    _assert(abs(fout - 1.5) < 1e-6, f"fade_out set to 1.5 (got {fout:.3f})")

    m2 = _roundtrip(m)
    g2_members = [e for e in m2.midi_events if e.get("group_id")]
    _assert(g2_members, "reloaded track has a scene group")
    gid2 = g2_members[0]["group_id"]
    rt_members = [e for e in m2.midi_events if e.get("group_id") == gid2]
    rfin = rt_members[0]["fade_in"]
    rfout = rt_members[0]["fade_out"]
    # Fades round-trip through a quantized ramp; allow one step of slack.
    _assert(abs(rfin - 1.0) < 0.11, f"fade_in survived round-trip (~1.0, got {rfin:.3f})")
    _assert(abs(rfout - 1.5) < 0.11, f"fade_out survived round-trip (~1.5, got {rfout:.3f})")


def main():
    test_single_clips_and_blackouts_roundtrip()
    test_scene_group_roundtrip()
    test_scene_fade_roundtrip()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
