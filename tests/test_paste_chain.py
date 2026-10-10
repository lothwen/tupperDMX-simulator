"""
Repeat-paste chaining: consecutive Ctrl+V with the playhead left alone stacks
each pasted block after the previous one, so a copied section loops.

Covers MidiManager.clipboard_span (the step size) and Timeline's chain state
(chained pastes, playhead move breaks the chain, a fresh copy resets it).

Run with:
    uv run python tests/test_paste_chain.py
"""

import sys
import os
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from core import note_layout
from midi.manager import MidiManager
from timeline.manager import Timeline


@dataclass
class FakeFixture:
    """Minimal patched-fixture stand-in: its index in the patch == the MIDI
    channel that selects it (the generalized note map routes by channel)."""
    type: str
    dmx_start: int = 1


PATCH = [FakeFixture("ledbar", 1), FakeFixture("blinder", 33)]
BLINDER_CH = 1
BLINDER_CLIP = 26          # a scene-load verb, never clustered into a group

_APP = None


def _ensure_app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication(sys.argv)
    return _APP


def _make():
    _ensure_app()
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    m.set_song_duration(240.0)
    tl = Timeline(midi_manager=m, fixtures=PATCH)
    return m, tl


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _clip_times(m):
    return sorted(e["time"] for e in m.midi_events
                  if m._event_fixture(e) == BLINDER_CH and not m.is_blackout(e))


def _select(tl, event_id):
    """Select the drawn clip for `event_id`, the way a click would."""
    for track in tl.midi_events_tracks:
        for item in track.items:
            if getattr(item, "event_id", None) == event_id:
                item.setSelected(True)
                return
    raise AssertionError(f"no drawn clip for {event_id}")


def _id_at(m, time, tol=0.02):
    """Event id of the (non-BLACKOUT) blinder clip at `time`."""
    for e in m.midi_events:
        if (m._event_fixture(e) == BLINDER_CH and not m.is_blackout(e)
                and abs(e["time"] - time) <= tol):
            return e["id"]
    raise AssertionError(f"no blinder clip near {time}s")


def _has(times, wanted, tol=0.02):
    return all(any(abs(t - w) <= tol for t in times) for w in wanted)


def _block(m):
    """Three blinder clips 4 s apart; copy the first two — a 8 s block
    (two 4 s-wide clips), the third only serving as the second one's cap."""
    ids = [m.add_event(BLINDER_CLIP, t, 100, channel=BLINDER_CH)
           for t in (5.0, 9.0, 13.0)]
    m.copy_selection(ids[:2])


def test_clipboard_span():
    print("test_clipboard_span")
    m, _tl = _make()
    _assert(m.clipboard_span() == 0.0, "empty clipboard spans 0")
    _block(m)
    _assert(abs(m.clipboard_span() - 8.0) < 0.02,
            "block spans first clip's start to last clip's end (8s)")


def test_open_ended_block_has_no_span():
    print("test_open_ended_block_has_no_span")
    m, _tl = _make()
    # A lone clip with nothing after it runs to the song end: no end to
    # measure, so nothing to step a chained paste by.
    only = m.add_event(BLINDER_CLIP, 5.0, 100, channel=BLINDER_CH)
    m.copy_selection([only])
    _assert(m.clipboard_span() == 0.0, "open-ended block spans 0")


def test_consecutive_pastes_stack():
    print("test_consecutive_pastes_stack")
    m, tl = _make()
    _block(m)
    tl.update_playhead(30.0)
    tl.paste_at_playhead()
    tl.paste_at_playhead()
    tl.paste_at_playhead()
    times = _clip_times(m)
    _assert(_has(times, [30.0, 34.0]), "first paste at the playhead")
    _assert(_has(times, [38.0, 42.0]), "second paste one block later")
    _assert(_has(times, [46.0, 50.0]), "third paste two blocks later")


def test_moving_the_playhead_breaks_the_chain():
    print("test_moving_the_playhead_breaks_the_chain")
    m, tl = _make()
    _block(m)
    tl.update_playhead(30.0)
    tl.paste_at_playhead()
    tl.paste_at_playhead()
    tl.update_playhead(100.0)
    tl.paste_at_playhead()
    tl.paste_at_playhead()
    times = _clip_times(m)
    _assert(_has(times, [30.0, 38.0]), "the first chain stayed where it was")
    _assert(_has(times, [100.0, 108.0]),
            "moving the playhead restarts at the playhead, then chains again")


def test_copy_resets_the_chain():
    print("test_copy_resets_the_chain")
    m, tl = _make()
    _block(m)
    tl.update_playhead(30.0)
    tl.paste_at_playhead()
    _assert(abs(tl._paste_chain_next - 38.0) < 1e-4, "chain armed at 38s")
    # A fresh copy of a different (open-ended) block: the chain drops, so the
    # next paste lands at the playhead rather than after the previous paste.
    new_id = m.add_event(BLINDER_CLIP, 200.0, 100, channel=BLINDER_CH)
    tl.refresh_from_manager()
    _select(tl, new_id)
    tl.copy_selected()
    _assert(tl._paste_chain_from is None, "a fresh copy drops the chain")
    tl.paste_at_playhead()
    _assert(abs(tl._paste_chain_next - 30.0) < 1e-4,
            "the new clipboard pasted at the playhead, not at 38s")


def test_delete_breaks_the_chain():
    print("test_delete_breaks_the_chain")
    m, tl = _make()
    _block(m)
    tl.update_playhead(30.0)
    tl.paste_at_playhead()
    tl.paste_at_playhead()
    _assert(abs(tl._paste_chain_next - 46.0) < 1e-4, "chain armed at 46s")
    # Delete one of the pasted clips: the next paste refills from the playhead
    # rather than carrying on to the next slot.
    tl.refresh_from_manager()
    _select(tl, _id_at(m, 42.0))
    tl.delete_selected()
    _assert(tl._paste_chain_from is None, "deleting a clip drops the chain")
    tl.paste_at_playhead()
    _assert(abs(tl._paste_chain_next - 38.0) < 1e-4,
            "the paste after a delete starts over at the playhead")
    # The single-clip (right-click) delete path drops it too.
    tl.refresh_from_manager()
    tl.on_midi_event_delete_by_id(_id_at(m, 30.0))
    _assert(tl._paste_chain_from is None, "right-click delete drops the chain")


def test_open_ended_block_keeps_pasting_at_the_playhead():
    print("test_open_ended_block_keeps_pasting_at_the_playhead")
    m, tl = _make()
    only = m.add_event(BLINDER_CLIP, 5.0, 100, channel=BLINDER_CH)
    m.copy_selection([only])
    tl.update_playhead(30.0)
    tl.paste_at_playhead()
    tl.paste_at_playhead()
    _assert(abs(tl._paste_chain_next - 30.0) < 1e-4,
            "with no measurable block length the chain cannot advance")


def main():
    _ensure_app()
    test_clipboard_span()
    test_open_ended_block_has_no_span()
    test_consecutive_pastes_stack()
    test_moving_the_playhead_breaks_the_chain()
    test_copy_resets_the_chain()
    test_delete_breaks_the_chain()
    test_open_ended_block_keeps_pasting_at_the_playhead()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
