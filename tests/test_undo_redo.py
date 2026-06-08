"""
Characterization test: undo/redo snapshot history on MidiManager.

Covers the snapshot-history machinery added to MidiManager: baseline seeding,
add/move round-trip fidelity, multi-step walk-back, redo-tail truncation on a
new edit, the MAX_HISTORY cap, history reset on create_empty_track, the
history_batch() grouping (including the empty-batch no-op guard), and scene
group undo.

Run with:
    uv run python tests/test_undo_redo.py
"""

import sys
import os
from dataclasses import dataclass

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


def _sig(m):
    """Order-independent signature of the editable event state."""
    return sorted(
        (e["note"], round(e["time"], 4), e["velocity"], m.is_blackout(e))
        for e in m.midi_events
    )


def test_baseline_no_history():
    print("test_baseline_no_history")
    m = _make_manager()
    _assert(m.can_undo() is False, "fresh track cannot undo")
    _assert(m.can_redo() is False, "fresh track cannot redo")
    _assert(m.undo() is False, "undo on baseline is a no-op")
    _assert(m.redo() is False, "redo at tip is a no-op")


def test_add_undo_redo_roundtrip():
    print("test_add_undo_redo_roundtrip")
    m = _make_manager()
    base = _sig(m)
    m.add_event(26, 5.0, 100)
    after = _sig(m)
    _assert(after != base, "add changed the state")
    _assert(m.can_undo() is True, "can undo after an edit")
    _assert(m.undo() is True, "undo reported success")
    _assert(_sig(m) == base, "undo restored the baseline exactly")
    _assert(m.can_redo() is True, "can redo after an undo")
    _assert(m.redo() is True, "redo reported success")
    _assert(_sig(m) == after, "redo restored the post-add state exactly")


def test_resize_undo_fidelity():
    print("test_resize_undo_fidelity")
    m = _make_manager()
    a = m.add_event(26, 5.0, 100)
    m.add_event(26, 20.0, 100)          # gives A a finite right edge at 20
    before = _sig(m)
    # Shrink A's right edge to 10 — inserts a derived BLACKOUT at 10.
    _assert(m.set_event_right_edge(a, 10.0) is True, "resize applied")
    resized = _sig(m)
    _assert(resized != before, "resize changed the layout (added a BLACKOUT)")
    m.undo()
    _assert(_sig(m) == before, "undo restored the pre-resize layout exactly")
    m.redo()
    _assert(_sig(m) == resized, "redo restored the resized layout exactly")


def test_multi_step_walk_back():
    print("test_multi_step_walk_back")
    m = _make_manager()
    sigs = [_sig(m)]
    for t in (3.0, 8.0, 15.0):
        m.add_event(26, t, 100)
        sigs.append(_sig(m))
    m.undo(); _assert(_sig(m) == sigs[2], "undo 1 -> state after 2 edits")
    m.undo(); _assert(_sig(m) == sigs[1], "undo 2 -> state after 1 edit")
    m.undo(); _assert(_sig(m) == sigs[0], "undo 3 -> baseline")
    _assert(m.can_undo() is False, "back at baseline, nothing to undo")
    m.redo(); _assert(_sig(m) == sigs[1], "redo 1 -> state after 1 edit")


def test_new_edit_truncates_redo():
    print("test_new_edit_truncates_redo")
    m = _make_manager()
    m.add_event(26, 3.0, 100)
    m.add_event(26, 8.0, 100)
    m.undo()
    _assert(m.can_redo() is True, "redo available after an undo")
    m.add_event(26, 12.0, 100)          # diverge from the redo branch
    _assert(m.can_redo() is False, "a new edit dropped the redo tail")


def test_history_capped():
    print("test_history_capped")
    m = _make_manager()
    for i in range(MidiManager.MAX_HISTORY + 25):
        m.add_event(26, float(i) + 0.5, 100)
    _assert(len(m._history) == MidiManager.MAX_HISTORY,
            f"history capped at MAX_HISTORY ({MidiManager.MAX_HISTORY})")
    _assert(m.can_undo() is True, "capped history is still undoable")


def test_create_track_resets_history():
    print("test_create_track_resets_history")
    m = _make_manager()
    m.add_event(26, 3.0, 100)
    _assert(m.can_undo() is True, "can undo after an edit")
    m.create_empty_track()
    _assert(m.can_undo() is False, "create_empty_track reseeds the baseline")
    _assert(m.can_redo() is False, "create_empty_track clears the redo tail")


def test_history_batch_single_step():
    print("test_history_batch_single_step")
    m = _make_manager()
    base = _sig(m)
    with m.history_batch():
        m.add_event(26, 3.0, 100)
        m.add_event(26, 8.0, 100)
        m.add_event(26, 15.0, 100)
    batched = _sig(m)
    _assert(batched != base, "batch mutated the state")
    m.undo()
    _assert(_sig(m) == base, "one undo reverses the whole batch")
    m.redo()
    _assert(_sig(m) == batched, "one redo reapplies the whole batch")


def test_empty_batch_no_step():
    print("test_empty_batch_no_step")
    m = _make_manager()
    m.add_event(26, 3.0, 100)
    idx = m._history_index
    with m.history_batch():
        pass                             # no mutations inside
    _assert(m._history_index == idx, "an empty batch adds no undo step")


def test_scene_group_undo():
    print("test_scene_group_undo")
    m = _make_manager()
    base = len(m.midi_events)
    g = m.add_scene(5.0, SCENE_CH, "S")
    _assert(g is not None, "scene added")
    full = len(m.midi_events)
    _assert(full > base, "scene added channel events")
    m.undo()
    _assert(len(m.midi_events) == base, "undo removed the whole scene group")
    m.redo()
    _assert(len(m.midi_events) == full, "redo restored the whole scene group")


def main():
    test_baseline_no_history()
    test_add_undo_redo_roundtrip()
    test_resize_undo_fidelity()
    test_multi_step_walk_back()
    test_new_edit_truncates_redo()
    test_history_capped()
    test_create_track_resets_history()
    test_history_batch_single_step()
    test_empty_batch_no_step()
    test_scene_group_undo()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
