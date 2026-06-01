"""
Headless tests for the multi-selection batch data ops on MidiManager:

    * selection_move_bounds  — rigid-block clamp range
    * move_selection         — shift a block, preserve inter-clip timing,
                               rebuild BLACKOUT gap markers
    * delete_selection       — replace a mixed (ledbar + blinder) selection
                               with BLACKOUTs, one commit, invariants intact

These exercise pure data logic — no GUI / QApplication needed.

Run with:
    uv run python tests/test_multi_select_move.py
"""

import sys
import os
from dataclasses import dataclass

# Ensure the project root is on sys.path when run from tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from midi.manager import MidiManager, BLACKOUT_EPSILON


@dataclass
class FakeNoteInfo:
    part: str
    color: str
    fixture: str = "ledbar"


FAKE_NOTE_MAP = {
    "12": FakeNoteInfo(part="Scene Select 1", color="#ff0000", fixture="ledbar"),
    "13": FakeNoteInfo(part="Scene Select 2", color="#00ff00", fixture="ledbar"),
    "14": FakeNoteInfo(part="Scene Select 3", color="#0000ff", fixture="ledbar"),
    "26": FakeNoteInfo(part="Blinder Flash", color="#ffffff", fixture="blinder"),
    "38": FakeNoteInfo(part="BLACKOUT", color="#000000", fixture="ledbar"),
    "39": FakeNoteInfo(part="BLACKOUT_BLINDERS", color="#000000", fixture="blinder"),
}


def _make_manager():
    m = MidiManager(midi_note_map=FAKE_NOTE_MAP)
    m.create_empty_track()
    m.set_song_duration(240.0)
    return m


def _id_at(m, fixture, time, tol=0.01):
    """Return the event id of the (non-BLACKOUT) clip nearest `time`."""
    for e in m.midi_events:
        if (m._event_fixture(e) == fixture and not m.is_blackout(e)
                and abs(e["time"] - time) < tol):
            return e["id"]
    raise AssertionError(f"no {fixture} event near {time}s")


def _ledbar_blackout_times(m):
    return sorted(e["time"] for e in m.midi_events
                  if m._event_fixture(e) == "ledbar" and m.is_blackout(e))


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_selection_move_bounds():
    print("test_selection_move_bounds")
    m = _make_manager()
    # ledbar: A@2, B@4 (selected, adjacent), C@8 (non-selected obstacle).
    m.add_event(12, 2.0, 100)
    m.add_event(13, 4.0, 100)
    m.add_event(14, 8.0, 100)
    a = _id_at(m, "ledbar", 2.0)
    b = _id_at(m, "ledbar", 4.0)

    lo, hi = m.selection_move_bounds([a, b])
    # B sits gaplessly against C → cannot move right; A can slide left to t=0.
    _assert(abs(hi - 0.0) < 1e-6, f"max_delta blocked at 0 (got {hi:.3f})")
    _assert(abs(lo - (-2.0)) < 1e-6, f"min_delta is -2.0 (got {lo:.3f})")


def test_move_selection_preserves_timing_and_rebuilds_blackouts():
    print("test_move_selection_preserves_timing_and_rebuilds_blackouts")
    m = _make_manager()
    m.add_event(12, 2.0, 100)   # A
    m.add_event(13, 4.0, 100)   # B  (B's visible width = 4 → C@8)
    m.add_event(14, 8.0, 100)   # C (non-selected)
    a = _id_at(m, "ledbar", 2.0)
    b = _id_at(m, "ledbar", 4.0)

    ok = m.move_selection([a, b], -1.0)
    _assert(ok, "move_selection returned True")

    a_t = next(e["time"] for e in m.midi_events if e["id"] == a)
    b_t = next(e["time"] for e in m.midi_events if e["id"] == b)
    _assert(abs(a_t - 1.0) < 1e-6, f"A moved to 1.0s (got {a_t:.3f})")
    _assert(abs(b_t - 3.0) < 1e-6, f"B moved to 3.0s (got {b_t:.3f})")
    _assert(abs((b_t - a_t) - 2.0) < 1e-6, "inter-clip delta preserved (2.0s)")

    # B kept its width (4s) → a BLACKOUT cap rebuilt at 7.0 before C@8.
    bos = _ledbar_blackout_times(m)
    _assert(any(abs(t - 7.0) < 1e-6 for t in bos),
            f"BLACKOUT cap rebuilt at 7.0 (got {bos})")
    _assert_invariants(m)


def test_delete_selection_mixed():
    print("test_delete_selection_mixed")
    m = _make_manager()
    m.add_event(12, 2.0, 100)   # ledbar A
    m.add_event(13, 5.0, 100)   # ledbar B
    m.add_event(26, 3.0, 100)   # blinder X
    a = _id_at(m, "ledbar", 2.0)
    x = _id_at(m, "blinder", 3.0)

    before_blinder = _id_at(m, "blinder", 3.0)  # sanity it exists pre-delete
    ok = m.delete_selection([a, x])
    _assert(ok, "delete_selection returned True")

    # The deleted clips are gone as regular events (replaced by BLACKOUTs).
    a_gone = all(e["id"] != a or m.is_blackout(e) for e in m.midi_events)
    _assert(a_gone, "deleted ledbar clip is now a BLACKOUT (or removed)")
    blinder_clips = [e for e in m.midi_events
                     if m._event_fixture(e) == "blinder" and not m.is_blackout(e)]
    _assert(not blinder_clips, "blinder clip replaced by a BLACKOUT")
    # Untouched ledbar B still present.
    _id_at(m, "ledbar", 5.0)
    _assert_invariants(m)


def _assert_invariants(m):
    """reconcile_blackouts invariants: no zero-dur / no adjacent BLACKOUTs."""
    for fixture in ("ledbar", "blinder"):
        ev = [e for e in m.midi_events if m._event_fixture(e) == fixture]
        ev.sort(key=lambda e: e["time"])
        for i, e in enumerate(ev):
            if not m.is_blackout(e):
                continue
            nxt = ev[i + 1]["time"] if i + 1 < len(ev) else float("inf")
            _assert(nxt - e["time"] > BLACKOUT_EPSILON,
                    f"{fixture} BLACKOUT at {e['time']:.3f} has positive duration")
            if i + 1 < len(ev):
                _assert(not m.is_blackout(ev[i + 1]),
                        f"{fixture} has no adjacent BLACKOUTs at {e['time']:.3f}")


def main():
    test_selection_move_bounds()
    test_move_selection_preserves_timing_and_rebuilds_blackouts()
    test_delete_selection_mixed()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
