"""
Characterization test: clip copy/paste on MidiManager.

Covers the clip clipboard: copy_selection (single clips, scene groups,
BLACKOUT exclusion, relative-offset capture), paste (placement at the target
time, width preservation, internal-gap preservation, velocity round-trip,
fade round-trip), the single-undo-step guarantee, repeatability, and the
empty-clipboard guard.

Run with:
    uv run python tests/test_copy_paste.py
"""

import sys
import os
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import note_layout
from midi.manager import MidiManager


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
# A standalone single-note blinder clip: note 26 is a scene-load verb (resolved
# against the blinder's own def via describe_note), not a channel-control note.
BLINDER_CLIP = 26

SCENE_CH = {"brightness": 255, "blue": 255, "green": 0, "red": 0,
            "animation": 0, "speed": 0, "direction": 0}


def _make_manager():
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    m.set_song_duration(240.0)
    return m


def _add_blinder(m, time, velocity=100):
    """Add a standalone blinder clip, stamped with the blinder's channel."""
    return m.add_event(BLINDER_CLIP, time, velocity, channel=BLINDER_CH)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _near(a, b, tol=0.02):
    return abs(a - b) <= tol


def _sig(m):
    return sorted(
        (e["note"], round(e["time"], 4), e["velocity"], m.is_blackout(e))
        for e in m.midi_events
    )


def _blinder_clip_times(m):
    return sorted(e["time"] for e in m.midi_events
                  if m._event_fixture(e) == BLINDER_CH and not m.is_blackout(e))


def test_copy_empty_selection():
    print("test_copy_empty_selection")
    m = _make_manager()
    _assert(m.copy_selection([]) == 0, "copy of empty selection returns 0")
    _assert(m.has_clipboard() is False, "clipboard stays empty")
    _assert(m.paste(10.0) is False, "paste with empty clipboard returns False")


def test_single_clip_paste_position_and_width():
    print("test_single_clip_paste_position_and_width")
    m = _make_manager()
    a = _add_blinder(m, 5.0)
    _add_blinder(m, 9.0)            # A's visible width = 4
    _assert(m.copy_selection([a]) == 1, "copied one unit")
    _assert(m.has_clipboard() is True, "clipboard populated")
    _assert(m.paste(30.0) is True, "paste succeeded")
    _assert(any(_near(t, 30.0) for t in _blinder_clip_times(m)),
            "clip pasted at the target time")
    after = sorted(e["time"] for e in m.midi_events
                   if m._event_fixture(e) == BLINDER_CH and e["time"] > 30.0 + 1e-6)
    _assert(bool(after) and _near(after[0], 34.0, 0.05),
            "pasted clip width (~4s) preserved via trailing BLACKOUT")


def test_paste_is_single_undo_step():
    print("test_paste_is_single_undo_step")
    m = _make_manager()
    a = _add_blinder(m, 5.0)
    _add_blinder(m, 9.0)
    base = _sig(m)
    m.copy_selection([a])
    m.paste(30.0)
    _assert(_sig(m) != base, "paste changed the state")
    m.undo()
    _assert(_sig(m) == base, "one undo reverses the whole paste")


def test_multi_clip_relative_offsets():
    print("test_multi_clip_relative_offsets")
    m = _make_manager()
    a = _add_blinder(m, 2.0)
    _add_blinder(m, 6.0)            # A: 2..6
    b = _add_blinder(m, 10.0)
    _add_blinder(m, 13.0)           # B: 10..13; gap 6..10
    _assert(m.copy_selection([a, b]) == 2, "copied two units")
    m.paste(50.0)
    starts = [t for t in _blinder_clip_times(m) if t >= 49.0]
    _assert(any(_near(t, 50.0) for t in starts), "first clip at the target time")
    _assert(any(_near(t, 58.0) for t in starts),
            "second clip's +8s relative offset preserved")


def test_copy_excludes_blackouts():
    print("test_copy_excludes_blackouts")
    m = _make_manager()
    a = _add_blinder(m, 5.0)
    _add_blinder(m, 10.0)
    m.set_event_right_edge(a, 7.0)       # inserts a BLACKOUT at 7 (gap 7..10)
    all_ids = [e["id"] for e in m.midi_events]   # includes the BLACKOUT id
    _assert(m.copy_selection(all_ids) == 2,
            "copy skips BLACKOUT markers (only 2 real clips captured)")


def test_scene_paste_roundtrip():
    print("test_scene_paste_roundtrip")
    m = _make_manager()
    g = m.add_scene(5.0, SCENE_CH, "MyScene", channel=LEDBAR_CH)
    m.add_scene(15.0, SCENE_CH, "B", channel=LEDBAR_CH)     # gives the first scene width 10
    ids = [mem["id"] for mem in m._doc._group_members(g)]
    _assert(m.copy_selection(ids) == 1, "scene counted as a single unit")
    m.paste(40.0)

    groups = {}
    for e in m.midi_events:
        gid = e.get("group_id")
        if gid and not m.is_blackout(e):
            groups.setdefault(gid, []).append(e)
    pasted = [gid for gid, evs in groups.items()
              if _near(min(x["time"] for x in evs), 40.0)]
    _assert(len(pasted) == 1, "one scene group pasted at the target time")
    members = m._doc._group_members(pasted[0])
    _assert(len(members) == 7, "pasted scene has all 7 channels")
    _assert(members[0].get("scene_name") == "MyScene", "scene name preserved")
    brightness = next(x for x in members if x["note"] == 1)
    _assert(brightness["velocity"] == 127,
            "brightness velocity round-tripped (255 -> 127)")
    after = sorted(e["time"] for e in m.midi_events
                   if m._event_fixture(e) == LEDBAR_CH and e["time"] > 40.0 + 1e-6)
    _assert(bool(after) and _near(after[0], 50.0, 0.05),
            "scene width (~10s) preserved")


def test_scene_fade_roundtrip():
    print("test_scene_fade_roundtrip")
    m = _make_manager()
    g = m.add_scene(5.0, SCENE_CH, "F", channel=LEDBAR_CH)
    m.add_scene(30.0, SCENE_CH, "B", channel=LEDBAR_CH)
    m.set_scene_fade_in(g, 1.5)
    ids = [mem["id"] for mem in m._doc._group_members(g)]
    m.copy_selection(ids)
    m.paste(60.0)
    target = None
    for e in m.midi_events:
        gid = e.get("group_id")
        if gid and not m.is_blackout(e) and _near(e["time"], 60.0):
            target = gid
            break
    _assert(target is not None, "scene pasted at 60s")
    fade_in = m._doc._group_members(target)[0].get("fade_in", 0.0)
    _assert(_near(fade_in, 1.5, 0.05),
            "scene fade_in round-tripped through copy/paste")


def test_paste_is_repeatable():
    print("test_paste_is_repeatable")
    m = _make_manager()
    a = _add_blinder(m, 5.0)
    _add_blinder(m, 9.0)
    m.copy_selection([a])
    m.paste(30.0)
    m.paste(50.0)                        # same clipboard, second target
    times = _blinder_clip_times(m)
    _assert(any(_near(t, 30.0) for t in times), "first paste present")
    _assert(any(_near(t, 50.0) for t in times), "second paste present")


def main():
    test_copy_empty_selection()
    test_single_clip_paste_position_and_width()
    test_paste_is_single_undo_step()
    test_multi_clip_relative_offsets()
    test_copy_excludes_blackouts()
    test_scene_paste_roundtrip()
    test_scene_fade_roundtrip()
    test_paste_is_repeatable()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
