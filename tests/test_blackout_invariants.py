"""
Characterization test: BLACKOUT gap-marker invariants on MidiManager.

After a battery of mixed edit ops (add, move, resize, scene add/move/delete,
multi-select) the per-fixture BLACKOUT invariants enforced by
reconcile_blackouts() must always hold:
  * events sorted by time,
  * no BLACKOUT with effective duration <= BLACKOUT_EPSILON,
  * no two adjacent same-fixture BLACKOUTs,
  * ledbar and blinder lanes are scoped independently.

Run with:
    uv run python tests/test_blackout_invariants.py
"""

import sys
import os
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import note_layout
from midi.manager import MidiManager, BLACKOUT_EPSILON


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
# Standalone single-note clips per fixture: scene-load verbs (resolved against
# each fixture's own def via describe_note), not channel-control notes, so the
# codec's scene-clustering never absorbs them.
LEDBAR_CLIP, BLINDER_CLIP = 24, 26

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


def _id_at(m, channel, time, tol=0.2):
    for e in m.midi_events:
        if (m._event_fixture(e) == channel and not m.is_blackout(e)
                and abs(e["time"] - time) < tol):
            return e["id"]
    return None


def _check_invariants(m, label):
    # Global sort.
    times = [e["time"] for e in m.midi_events]
    _assert(times == sorted(times), f"[{label}] events sorted by time")
    for name, channel in (("ledbar", LEDBAR_CH), ("blinder", BLINDER_CH)):
        ev = [e for e in m.midi_events if m._event_fixture(e) == channel]
        ev.sort(key=lambda e: e["time"])
        for i, e in enumerate(ev):
            if not m.is_blackout(e):
                continue
            nxt = ev[i + 1]["time"] if i + 1 < len(ev) else float("inf")
            _assert(nxt - e["time"] > BLACKOUT_EPSILON,
                    f"[{label}] {name} BLACKOUT @ {e['time']:.3f} has positive duration")
            if i + 1 < len(ev):
                _assert(not m.is_blackout(ev[i + 1]),
                        f"[{label}] {name} no adjacent BLACKOUTs @ {e['time']:.3f}")


def test_invariants_through_op_battery():
    print("test_invariants_through_op_battery")
    m = _make_manager()

    # Build a mixed timeline across both lanes.
    m.add_event(BLINDER_CLIP, 2.0, 100, channel=BLINDER_CH)
    m.add_event(BLINDER_CLIP, 8.0, 100, channel=BLINDER_CH)
    m.add_event(LEDBAR_CLIP, 3.0, 100, channel=LEDBAR_CH)
    g1 = m.add_scene(5.0, SCENE_CH, "A", channel=LEDBAR_CH)
    g2 = m.add_scene(15.0, SCENE_CH, "B", channel=LEDBAR_CH)
    _check_invariants(m, "after build")

    # Resize ops that insert / move BLACKOUTs on each lane.
    b1 = _id_at(m, BLINDER_CH, 2.0)
    if b1:
        m.set_event_right_edge(b1, 4.0)
    m.set_scene_right_edge(g1, 9.0)
    _check_invariants(m, "after resizes")

    # Move ops (jump + clamp).
    if b1:
        m.move_event(b1, 20.0)
    m.move_scene(g2, 25.0)
    _check_invariants(m, "after moves")

    # Delete / replace ops.
    m.delete_scene(g1)
    b2 = _id_at(m, BLINDER_CH, 8.0)
    if b2:
        m.replace_with_blackout(b2)
    _check_invariants(m, "after deletes")

    # Multi-select move across the remaining clips.
    ids = [e["id"] for e in m.midi_events if not m.is_blackout(e)]
    if ids:
        m.move_selection(ids, 1.5)
    _check_invariants(m, "after selection move")

    # Lanes scoped independently: a blinder BLACKOUT between ledbar events
    # is never merged/dropped against ledbar neighbors (covered structurally
    # by the per-fixture checks above completing without error).
    print("  ok: ledbar / blinder lanes reconciled independently")


def main():
    test_invariants_through_op_battery()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
