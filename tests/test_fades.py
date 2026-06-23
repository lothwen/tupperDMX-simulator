"""
Characterization test: scene fade engine on MidiManager.

Covers the fade ramp math and the scene fade setters:
  * set_scene_fade_in / set_scene_fade_out clamp to the clip's visible duration
  * fade ramps generate is_fade notes in midi_notes (not midi_events) with a
    velocity floor of 1 and an exact endpoint
  * fades regenerate when the clip is resized

Locks current fade behavior before the collaborator-object refactor.

Run with:
    uv run python tests/test_fades.py
"""

import sys
import os
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import note_layout
from midi.manager import MidiManager, FADE_BRIGHTNESS_NOTE


@dataclass
class FakeFixture:
    """Minimal patched-fixture stand-in: its index in the patch == the MIDI
    channel that selects it (the generalized note map routes by channel)."""
    type: str


# Patch: ledbar @ channel 0, blinder @ channel 1 (the shipped patch's first two
# fixtures). Mirrors production: MidiManager gets the data-driven note map plus
# the patch, and every scene is stamped with its target fixture's channel.
PATCH = [FakeFixture("ledbar"), FakeFixture("blinder")]
LEDBAR_CH, BLINDER_CH = 0, 1

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


def _fade_notes(m, gid):
    return [n for n in m.midi_notes
            if n.get("is_fade") and n.get("parent_group_id") == gid]


def test_set_fades_generate_fade_notes():
    print("test_set_fades_generate_fade_notes")
    m = _make_manager()
    g = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
    m.set_scene_right_edge(g, 10.0)          # visible [4, 10)
    _assert(m.set_scene_fade_in(g, 1.0), "set_scene_fade_in returned True")
    _assert(m.set_scene_fade_out(g, 1.0), "set_scene_fade_out returned True")

    members = [e for e in m.midi_events if e.get("group_id") == g]
    _assert(all(abs(mm["fade_in"] - 1.0) < 1e-6 for mm in members), "fade_in=1.0 on members")
    _assert(all(abs(mm["fade_out"] - 1.0) < 1e-6 for mm in members), "fade_out=1.0 on members")

    fades = _fade_notes(m, g)
    _assert(fades, "fade notes generated in midi_notes")
    _assert(all(n["note"] == FADE_BRIGHTNESS_NOTE for n in fades),
            "fade notes are all on the brightness channel (note 1)")
    _assert(all(n["velocity"] >= 1 for n in fades),
            "fade velocities never drop below 1 (note_on vel 0 == note_off)")
    # No fade note leaks into midi_events (would corrupt clustering / rendering).
    _assert(not any(e.get("is_fade") for e in m.midi_events),
            "no fade notes in midi_events")


def test_fade_clamps_to_visible_duration():
    print("test_fade_clamps_to_visible_duration")
    m = _make_manager()
    g = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
    m.set_scene_right_edge(g, 7.0)           # visible span = 3.0s
    m.set_scene_fade_in(g, 5.0)              # request more than fits
    bright = next(e for e in m.midi_events
                  if e.get("group_id") == g and e["note"] == FADE_BRIGHTNESS_NOTE)
    _assert(bright["fade_in"] <= 3.0 + 1e-6,
            f"fade_in clamped to <= visible 3.0s (got {bright['fade_in']:.3f})")


def test_fade_in_plus_out_bounded_by_visible():
    print("test_fade_in_plus_out_bounded_by_visible")
    m = _make_manager()
    g = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
    m.set_scene_right_edge(g, 8.0)           # visible span = 4.0s
    m.set_scene_fade_in(g, 3.0)
    m.set_scene_fade_out(g, 3.0)             # in+out would be 6 > 4 → clamped
    bright = next(e for e in m.midi_events
                  if e.get("group_id") == g and e["note"] == FADE_BRIGHTNESS_NOTE)
    total = bright["fade_in"] + bright["fade_out"]
    _assert(total <= 4.0 + 1e-6,
            f"fade_in + fade_out clamped to <= visible 4.0s (got {total:.3f})")


def test_fades_regenerate_on_resize():
    print("test_fades_regenerate_on_resize")
    m = _make_manager()
    g = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
    m.set_scene_right_edge(g, 20.0)
    m.set_scene_fade_out(g, 2.0)
    before = sorted(round(n["time"], 3) for n in _fade_notes(m, g))
    _assert(before, "fade-out notes exist before resize")
    # Shrink the clip; the fade-out ramp must move with the new right edge.
    m.set_scene_right_edge(g, 12.0)
    after = sorted(round(n["time"], 3) for n in _fade_notes(m, g))
    _assert(after, "fade-out notes still exist after resize")
    _assert(max(after) <= 12.0 + 1e-6,
            f"fade-out ramp ends at or before new right edge 12.0 (got max {max(after):.3f})")


def main():
    test_set_fades_generate_fade_notes()
    test_fade_clamps_to_visible_duration()
    test_fade_in_plus_out_bounded_by_visible()
    test_fades_regenerate_on_resize()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
