"""
Characterization test: single-clip edit ops on MidiManager.

Covers add_event / delete_event, move_event (free placement + jump-over-gap +
neighbor clamp), set_event_left_edge / set_event_right_edge (BLACKOUT
insertion), and replace_with_blackout. Locks current clip-editing behavior
before the collaborator-object refactor.

Run with:
    uv run python tests/test_clip_ops.py
"""

import sys
import os
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from midi.manager import MidiManager, MIN_EVENT_DURATION


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


def _make_manager():
    m = MidiManager(midi_note_map=FAKE_NOTE_MAP)
    m.create_empty_track()
    m.set_song_duration(240.0)
    return m


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _id_at(m, fixture, time, tol=0.05):
    for e in m.midi_events:
        if (m._event_fixture(e) == fixture and not m.is_blackout(e)
                and abs(e["time"] - time) < tol):
            return e["id"]
    raise AssertionError(f"no {fixture} clip near {time}s")


def _time_of(m, event_id):
    return next(e["time"] for e in m.midi_events if e["id"] == event_id)


def _blinder_blackouts(m):
    return sorted(e["time"] for e in m.midi_events
                  if m._event_fixture(e) == "blinder" and m.is_blackout(e))


def test_add_and_delete_event():
    print("test_add_and_delete_event")
    m = _make_manager()
    eid = m.add_event(26, 3.0, 100)
    _assert(eid != "", "add_event returned an id")
    _assert(any(e["id"] == eid for e in m.midi_events), "event present after add")
    _assert(m.delete_event(eid), "delete_event returned True")
    _assert(not any(e["id"] == eid for e in m.midi_events), "event gone after delete")


def test_move_event_free_placement():
    print("test_move_event_free_placement")
    m = _make_manager()
    m.add_event(26, 2.0, 100)            # lone blinder clip
    a = _id_at(m, "blinder", 2.0)
    _assert(m.move_event(a, 7.0), "move_event returned True")
    _assert(abs(_time_of(m, a) - 7.0) < 1e-6, f"clip moved to 7.0 (got {_time_of(m, a):.3f})")


def test_move_event_clamps_against_neighbor():
    print("test_move_event_clamps_against_neighbor")
    m = _make_manager()
    m.add_event(26, 2.0, 100)            # A (first → width spans to B)
    m.add_event(26, 10.0, 100)           # B
    a = _id_at(m, "blinder", 2.0)
    # A's preserved width is 8s (2→10); the only fit is exactly at its origin,
    # so a rightward move past B is rejected (no slot can hold the 8s clip).
    _assert(not m.move_event(a, 50.0), "rightward move past neighbor rejected")
    _assert(abs(_time_of(m, a) - 2.0) < 1e-6, "A stayed put")


def test_move_event_jump_over_gap():
    print("test_move_event_jump_over_gap")
    m = _make_manager()
    m.add_event(26, 2.0, 100)            # A
    m.add_event(26, 10.0, 100)           # B
    m.add_event(26, 20.0, 100)           # C
    a = _id_at(m, "blinder", 2.0)
    b = _id_at(m, "blinder", 10.0)
    # Carve A narrow and open a gap after B so a jump target exists.
    m.set_event_right_edge(a, 4.0)       # A width ~2, BLACKOUT at 4
    m.set_event_right_edge(b, 12.0)      # gap [12, 20) on the blinder lane
    _assert(m.move_event(a, 15.0), "jump move returned True")
    at = _time_of(m, a)
    _assert(9.0 < at < 18.1, f"A jumped past B into the [12,20) gap (got {at:.3f})")


def test_set_event_right_edge_inserts_blackout():
    print("test_set_event_right_edge_inserts_blackout")
    m = _make_manager()
    m.add_event(26, 2.0, 100)
    a = _id_at(m, "blinder", 2.0)
    _assert(m.set_event_right_edge(a, 5.0), "set_event_right_edge returned True")
    _assert(abs(_time_of(m, a) - 2.0) < 1e-6, "clip start unchanged")
    bos = _blinder_blackouts(m)
    _assert(any(abs(t - 5.0) < 1e-6 for t in bos),
            f"BLACKOUT cap inserted at 5.0 (got {bos})")


def test_set_event_left_edge_moves_start():
    print("test_set_event_left_edge_moves_start")
    m = _make_manager()
    m.add_event(26, 2.0, 100)            # A
    m.add_event(26, 12.0, 100)           # B (so A has a finite right edge)
    a = _id_at(m, "blinder", 2.0)
    _assert(m.set_event_left_edge(a, 6.0), "set_event_left_edge returned True")
    _assert(abs(_time_of(m, a) - 6.0) < 1e-6, f"A start moved to 6.0 (got {_time_of(m, a):.3f})")
    # Shrinking from the left with no prior clip exposes [0,6); no marker is
    # required there, but invariants must still hold.
    _assert(_time_of(m, a) >= MIN_EVENT_DURATION, "A start non-negative")


def test_replace_with_blackout():
    print("test_replace_with_blackout")
    m = _make_manager()
    m.add_event(26, 3.0, 100)
    a = _id_at(m, "blinder", 3.0)
    _assert(m.replace_with_blackout(a), "replace_with_blackout returned True")
    # The slot at 3.0 is now a BLACKOUT_BLINDERS marker, not a live clip.
    live = [e for e in m.midi_events
            if m._event_fixture(e) == "blinder" and not m.is_blackout(e)]
    _assert(not live, "no live blinder clip remains")
    _assert(any(abs(t - 3.0) < 1e-6 for t in _blinder_blackouts(m)),
            "BLACKOUT marker sits at the old clip time")


def main():
    test_add_and_delete_event()
    test_move_event_free_placement()
    test_move_event_clamps_against_neighbor()
    test_move_event_jump_over_gap()
    test_set_event_right_edge_inserts_blackout()
    test_set_event_left_edge_moves_start()
    test_replace_with_blackout()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
