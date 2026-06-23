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
# Standalone single-note clips: scene-load verbs (24/25/26), resolved against a
# fixture's own def via describe_note — not channel-control notes, so they're
# never clustered into scene groups. A/B/C ride the ledbar (channel 0); the
# blinder clip is the same kind of verb on channel 1 (verb is shared, the
# channel selects the fixture).
LED_CLIP_A, LED_CLIP_B, LED_CLIP_C = 24, 25, 26
BLINDER_CLIP = 26

SCENE_CH = {"brightness": 255, "blue": 255, "green": 0, "red": 0,
            "animation": 0, "speed": 0, "direction": 0}


def _make_manager():
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    m.set_song_duration(240.0)
    return m


def _id_at(m, channel, time, tol=0.01):
    """Return the event id of the (non-BLACKOUT) clip nearest `time`."""
    for e in m.midi_events:
        if (m._event_fixture(e) == channel and not m.is_blackout(e)
                and abs(e["time"] - time) < tol):
            return e["id"]
    raise AssertionError(f"no channel-{channel} event near {time}s")


def _ledbar_blackout_times(m):
    return sorted(e["time"] for e in m.midi_events
                  if m._event_fixture(e) == LEDBAR_CH and m.is_blackout(e))


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_selection_move_bounds():
    print("test_selection_move_bounds")
    m = _make_manager()
    # ledbar: A@2, B@4 (selected, adjacent), C@8 (non-selected obstacle).
    m.add_event(LED_CLIP_A, 2.0, 100, channel=LEDBAR_CH)
    m.add_event(LED_CLIP_B, 4.0, 100, channel=LEDBAR_CH)
    m.add_event(LED_CLIP_C, 8.0, 100, channel=LEDBAR_CH)
    a = _id_at(m, LEDBAR_CH, 2.0)
    b = _id_at(m, LEDBAR_CH, 4.0)

    lo, hi = m.selection_move_bounds([a, b])
    # B sits gaplessly against C → cannot move right; A can slide left to t=0.
    _assert(abs(hi - 0.0) < 1e-6, f"max_delta blocked at 0 (got {hi:.3f})")
    _assert(abs(lo - (-2.0)) < 1e-6, f"min_delta is -2.0 (got {lo:.3f})")


def test_move_selection_preserves_timing_and_rebuilds_blackouts():
    print("test_move_selection_preserves_timing_and_rebuilds_blackouts")
    m = _make_manager()
    m.add_event(LED_CLIP_A, 2.0, 100, channel=LEDBAR_CH)   # A
    m.add_event(LED_CLIP_B, 4.0, 100, channel=LEDBAR_CH)   # B (visible width 4 → C@8)
    m.add_event(LED_CLIP_C, 8.0, 100, channel=LEDBAR_CH)   # C (non-selected)
    a = _id_at(m, LEDBAR_CH, 2.0)
    b = _id_at(m, LEDBAR_CH, 4.0)

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
    m.add_event(LED_CLIP_A, 2.0, 100, channel=LEDBAR_CH)   # ledbar A
    m.add_event(LED_CLIP_B, 5.0, 100, channel=LEDBAR_CH)   # ledbar B
    m.add_event(BLINDER_CLIP, 3.0, 100, channel=BLINDER_CH)  # blinder X
    a = _id_at(m, LEDBAR_CH, 2.0)
    x = _id_at(m, BLINDER_CH, 3.0)

    before_blinder = _id_at(m, BLINDER_CH, 3.0)  # sanity it exists pre-delete
    ok = m.delete_selection([a, x])
    _assert(ok, "delete_selection returned True")

    # The deleted clips are gone as regular events (replaced by BLACKOUTs).
    a_gone = all(e["id"] != a or m.is_blackout(e) for e in m.midi_events)
    _assert(a_gone, "deleted ledbar clip is now a BLACKOUT (or removed)")
    blinder_clips = [e for e in m.midi_events
                     if m._event_fixture(e) == BLINDER_CH and not m.is_blackout(e)]
    _assert(not blinder_clips, "blinder clip replaced by a BLACKOUT")
    # Untouched ledbar B still present.
    _id_at(m, LEDBAR_CH, 5.0)
    _assert_invariants(m)


def test_move_selection_scene_groups_no_spurious_blackout():
    print("test_move_selection_scene_groups_no_spurious_blackout")
    # Regression: moving multiple scene groups used to spray a BLACKOUT at
    # each group's start time (LED bar never turned on), because the rebuild
    # treated the 7 same-time members as zero-width clips.
    m = _make_manager()
    g1 = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
    g2 = m.add_scene(10.0, SCENE_CH, "B", channel=LEDBAR_CH)
    ids = [e["id"] for e in m.midi_events if e.get("group_id") in (g1, g2)]

    m.move_selection(ids, 2.0)

    group_starts = {round(e["time"], 3) for e in m.midi_events
                    if e.get("group_id") in (g1, g2)}
    for e in m.midi_events:
        if m.is_blackout(e):
            _assert(round(e["time"], 3) not in group_starts,
                    f"no BLACKOUT lands on a group start ({e['time']:.2f}s)")
    a_t = min(e["time"] for e in m.midi_events if e.get("group_id") == g1)
    b_t = min(e["time"] for e in m.midi_events if e.get("group_id") == g2)
    _assert(abs(a_t - 6.0) < 1e-6 and abs(b_t - 12.0) < 1e-6,
            f"groups shifted to 6.0s & 12.0s (got {a_t:.2f}, {b_t:.2f})")
    _assert_invariants(m)


def test_move_selection_scene_group_keeps_trailing_gap():
    print("test_move_selection_scene_group_keeps_trailing_gap")
    # A group with a real trailing gap keeps that gap (its BLACKOUT cap is
    # rebuilt at the shifted position), not at the group start.
    m = _make_manager()
    g1 = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
    g2 = m.add_scene(20.0, SCENE_CH, "B", channel=LEDBAR_CH)
    m.set_scene_right_edge(g1, 8.0)   # carve A's visible end → gap [8, 20)
    ids = [e["id"] for e in m.midi_events if e.get("group_id") in (g1, g2)]

    m.move_selection(ids, 3.0)

    a_t = min(e["time"] for e in m.midi_events if e.get("group_id") == g1)
    _assert(abs(a_t - 7.0) < 1e-6, f"group A shifted to 7.0s (got {a_t:.2f})")
    bos = _ledbar_blackout_times(m)
    _assert(any(abs(t - 11.0) < 1e-6 for t in bos),
            f"trailing-gap cap rebuilt at 11.0s (got {bos})")
    _assert(all(abs(t - 7.0) > 1e-6 for t in bos),
            f"no cap at group A's start 7.0s (got {bos})")
    _assert_invariants(m)


def _assert_invariants(m):
    """reconcile_blackouts invariants: no zero-dur / no adjacent BLACKOUTs."""
    for name, channel in (("ledbar", LEDBAR_CH), ("blinder", BLINDER_CH)):
        ev = [e for e in m.midi_events if m._event_fixture(e) == channel]
        ev.sort(key=lambda e: e["time"])
        for i, e in enumerate(ev):
            if not m.is_blackout(e):
                continue
            nxt = ev[i + 1]["time"] if i + 1 < len(ev) else float("inf")
            _assert(nxt - e["time"] > BLACKOUT_EPSILON,
                    f"{name} BLACKOUT at {e['time']:.3f} has positive duration")
            if i + 1 < len(ev):
                _assert(not m.is_blackout(ev[i + 1]),
                        f"{name} has no adjacent BLACKOUTs at {e['time']:.3f}")


def main():
    test_selection_move_bounds()
    test_move_selection_preserves_timing_and_rebuilds_blackouts()
    test_move_selection_scene_groups_no_spurious_blackout()
    test_move_selection_scene_group_keeps_trailing_gap()
    test_delete_selection_mixed()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
