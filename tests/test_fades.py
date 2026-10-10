"""
Characterization test: scene fade engine on MidiManager.

Covers the fade ramp math and the scene fade setters:
  * set_scene_fade_in / set_scene_fade_out clamp to the clip's visible duration
  * fade ramps generate is_fade notes in midi_notes (not midi_events) with a
    velocity floor of 1 and an exact endpoint
  * fades regenerate when the clip is resized
  * a ramp rides the fixture's OWN dimmer note on its OWN MIDI channel, and a
    fixture with no dimmer refuses to fade at all
  * a direct-channel clip ramps its OWN note's velocity, and a verb whose
    velocity is an index (a chase) refuses
  * a fade-out stops short of the right edge so it can't overwrite the clip
    that starts there
  * a fade-out follows its clip's right edge when a NEIGHBOUR moves it
  * bulk_edit() does the per-edit work once, at its end, and ends where the
    same edits one by one would
  * ramps round-trip through the .mid without leaking back in as clips

Locks current fade behavior before the collaborator-object refactor.

Run with:
    uv run python tests/test_fades.py
"""

import sys
import os
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import note_layout
from core.fixture_library import intensity_slot, resolve_fixture
from midi.manager import MidiManager


@dataclass
class FakeFixture:
    """Minimal patched-fixture stand-in: its index in the patch == the MIDI
    channel that selects it (the generalized note map routes by channel)."""
    type: str


# Patch: the shipped rig's first two fixtures plus two that make the fade's
# target explicit — a moving head, which dims on its THIRD channel, and an RGBW
# wash, which has no dimmer at all. Mirrors production: MidiManager gets the
# data-driven note map plus the patch, and every scene is stamped with its
# target fixture's channel.
PATCH = [FakeFixture("ledbar"), FakeFixture("blinder"),
         FakeFixture("moving-head"), FakeFixture("Wild Wash Pro 648 RGBW")]
LEDBAR_CH, BLINDER_CH, MOVER_CH, WASH_CH = 0, 1, 2, 3

# The note a fade rides is the fixture's OWN dimmer, so the expected note is
# read from the def rather than assumed: 1 on the ledbar, 3 on the moving head
# (Pan, Tilt, Dimmer), none at all on the RGBW wash.
LEDBAR_DIMMER_NOTE = intensity_slot(resolve_fixture("ledbar", "")) + 1

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
    _assert(all(n["note"] == LEDBAR_DIMMER_NOTE for n in fades),
            "fade notes are all on the ledbar's dimmer note (1)")
    _assert(all(n.get("channel") == LEDBAR_CH for n in fades),
            "fade notes carry the scene's MIDI channel (its fixture)")
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
                  if e.get("group_id") == g and e["note"] == LEDBAR_DIMMER_NOTE)
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
                  if e.get("group_id") == g and e["note"] == LEDBAR_DIMMER_NOTE)
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


def test_fade_rides_the_fixtures_own_dimmer():
    """A moving head dims on channel 3 — its first two channels are Pan and
    Tilt — so its ramp must ride note 3 on ITS MIDI channel, not note 1 on
    whichever fixture happens to be patched first."""
    print("test_fade_rides_the_fixtures_own_dimmer")
    m = _make_manager()
    fdef = resolve_fixture("moving-head", "")
    dimmer_note = intensity_slot(fdef) + 1
    _assert(dimmer_note == 3, f"moving head dims on note 3 (got {dimmer_note})")

    scene = {ch.field: (255 if ch.field == "Dimmer" else 40)
             for ch in fdef.channels}
    g = m.add_scene(4.0, scene, "MH", channel=MOVER_CH)
    m.set_scene_right_edge(g, 10.0)
    _assert(m.set_scene_fade_in(g, 1.0), "set_scene_fade_in returned True")

    fades = _fade_notes(m, g)
    _assert(fades, "fade notes generated")
    _assert({n["note"] for n in fades} == {dimmer_note},
            f"ramp rides the Dimmer note {dimmer_note}, not note 1 "
            f"(got {sorted({n['note'] for n in fades})})")
    _assert({n.get("channel") for n in fades} == {MOVER_CH},
            f"ramp carries the moving head's MIDI channel {MOVER_CH} "
            f"(got {sorted({n.get('channel') for n in fades}, key=str)})")

    # And the same must hold once it has been through the codec, which is what
    # actually plays: no ramp may leak onto another fixture's channel.
    m._codec.rebuild(m._doc)
    ramp = [(msg.channel, msg.note) for msg in m.midi_data
            if msg.type == "note_on" and msg.channel != MOVER_CH]
    _assert(not ramp, f"nothing addressed to another fixture (got {ramp})")


def test_fixture_without_a_dimmer_cannot_fade():
    """An RGBW wash has no Intensity channel, so there is nothing for a ramp to
    ride: the setter refuses instead of silently ramping a colour."""
    print("test_fixture_without_a_dimmer_cannot_fade")
    m = _make_manager()
    fdef = resolve_fixture("Wild Wash Pro 648 RGBW", "")
    _assert(intensity_slot(fdef) < 0, "the wash declares no Intensity channel")

    g = m.add_scene(4.0, {ch.field: 255 for ch in fdef.channels}, "W",
                    channel=WASH_CH)
    m.set_scene_right_edge(g, 10.0)
    _assert(not m.can_fade_scene(g), "can_fade_scene is False without a dimmer")
    _assert(not m.set_scene_fade_in(g, 1.0), "set_scene_fade_in refuses")
    _assert(not m.set_scene_fade_out(g, 1.0), "set_scene_fade_out refuses")
    _assert(not _fade_notes(m, g), "no ramp notes generated")


def _ramp(m, event_id):
    return sorted((round(n["time"], 3), n["velocity"], n["note"],
                   n.get("channel"))
                  for n in m.midi_notes
                  if n.get("is_fade") and n.get("parent_event_id") == event_id)


def test_direct_channel_clip_ramps_its_own_note():
    """A direct-channel clip's velocity IS its level, so its fade keeps the
    note and moves the velocity — the moving head's Red on note 5, not the
    fixture's dimmer and not note 1."""
    print("test_direct_channel_clip_ramps_its_own_note")
    m = _make_manager()
    eid = m.add_event(5, 4.0, 100, channel=MOVER_CH)   # 5 = Red
    m.set_event_right_edge(eid, 10.0)
    _assert(m.can_fade_event(eid), "a channel-control clip can fade")
    _assert(m.set_event_fade_in(eid, 1.0), "set_event_fade_in returned True")
    _assert(m.set_event_fade_out(eid, 1.0), "set_event_fade_out returned True")

    ramp = _ramp(m, eid)
    _assert(ramp, "ramp notes generated in midi_notes")
    _assert({r[2] for r in ramp} == {5},
            f"ramp keeps the clip's own note 5 (got {sorted({r[2] for r in ramp})})")
    _assert({r[3] for r in ramp} == {MOVER_CH},
            f"ramp carries the clip's MIDI channel {MOVER_CH}")
    _assert(not any(e.get("is_fade") for e in m.midi_events),
            "no ramp note leaks into midi_events")

    fade_in = [r for r in ramp if r[0] <= 5.0]
    _assert(fade_in[0][:2] == (4.0, 1) and fade_in[-1][:2] == (5.0, 100),
            f"fade-in climbs 1 → the clip's velocity 100 "
            f"(got {fade_in[0][:2]} → {fade_in[-1][:2]})")
    fade_out = [r for r in ramp if r[0] >= 9.0]
    _assert(fade_out[0][1] == 100 and fade_out[-1][1] == 1,
            f"fade-out falls from 100 → 1 "
            f"(got {fade_out[0][1]} → {fade_out[-1][1]})")
    _assert(all(a[1] >= b[1] for a, b in zip(fade_out, fade_out[1:])),
            "fade-out velocities are monotonically decreasing")


def test_indexed_verb_never_fades():
    """A chase clip packs the chase INDEX into velocity, so ramping it would
    step through a hundred chases rather than dim anything."""
    print("test_indexed_verb_never_fades")
    m = _make_manager()
    eid = m.add_event(note_layout.CHASE_LOAD, 4.0, 3, channel=LEDBAR_CH)
    _assert(not m.can_fade_event(eid), "can_fade_event is False for a chase")
    _assert(not m.set_event_fade_in(eid, 1.0), "set_event_fade_in refuses")
    _assert(not m.set_event_fade_out(eid, 1.0), "set_event_fade_out refuses")
    _assert(not _ramp(m, eid), "no ramp notes generated")


def test_fade_out_does_not_overwrite_the_next_clip():
    """The codec sorts ramp notes after originals at the same timestamp, so a
    fade-out ending exactly on the boundary would blank the clip starting
    there. The last ramp note must land strictly before it."""
    print("test_fade_out_does_not_overwrite_the_next_clip")
    m = _make_manager()
    g1 = m.add_scene(2.0, SCENE_CH, "A", channel=LEDBAR_CH)
    second = dict(SCENE_CH, red=200)
    m.add_scene(6.0, second, "B", channel=LEDBAR_CH)
    m.set_scene_fade_out(g1, 1.0)

    m._codec.rebuild(m._doc)
    at_boundary = []
    now = 0.0
    for msg in m.midi_data:
        now += msg.time
        if msg.type == "note_on" and msg.note == LEDBAR_DIMMER_NOTE \
                and 5.5 <= now <= 6.5:
            at_boundary.append((round(now, 3), msg.velocity))
    _assert(at_boundary, "dimmer messages exist around the boundary")
    _assert(at_boundary[-1][0] >= 6.0 and at_boundary[-1][1] > 1,
            f"the next scene's level is the last word at the boundary "
            f"(got {at_boundary[-1]})")
    _assert(all(t < 6.0 for t, v in at_boundary if v <= 1),
            f"every near-zero ramp note lands before 6.0 (got {at_boundary})")


def test_fade_out_follows_a_neighbours_arrival():
    """A fade-out is anchored on its clip's RIGHT edge, and that edge belongs
    to the neighbour. Dropping a scene in front of a faded one has to re-derive
    the ramp behind it, or the old ramp stays stranded where the clip used to
    end — audible, and invisible on the timeline."""
    print("test_fade_out_follows_a_neighbours_arrival")
    m = _make_manager()
    g = m.add_scene(1.0, SCENE_CH, "A", channel=LEDBAR_CH)
    m.set_scene_fade_out(g, 0.5)             # no neighbour yet → ends at song end
    stranded = max(n["time"] for n in _fade_notes(m, g))
    _assert(stranded > 100.0,
            f"with no neighbour the ramp ends near the song end (got {stranded:.1f})")

    m.add_scene(9.0, dict(SCENE_CH, red=200), "B", channel=LEDBAR_CH)
    after = max(n["time"] for n in _fade_notes(m, g))
    _assert(after < 9.0,
            f"the ramp moved to the new right edge 9.0 (got {after:.3f})")
    _assert(after > 8.0,
            f"…and still sits in the last half second (got {after:.3f})")


def test_bulk_edit_does_the_work_once():
    """bulk_edit() defers what every edit repeats — the .mid rebuild, the
    notification, the lane's ramps — to the block's end, and ends where the
    same edits one by one end: a neighbour arriving inside the block still
    moves the fade-out behind it, and the whole block is one undo step."""
    print("test_bulk_edit_does_the_work_once")

    def edits(m):
        g = m.add_scene(1.0, SCENE_CH, "A", channel=LEDBAR_CH)
        m.set_scene_fade_out(g, 0.5)
        m.add_scene(9.0, dict(SCENE_CH, red=200), "B", channel=LEDBAR_CH)
        return g

    def ramps(m):
        return sorted((n["time"], n["velocity"]) for n in m.midi_notes if n.get("is_fade"))

    one_by_one = _make_manager()
    edits(one_by_one)

    m = _make_manager()
    changed = []
    m.data_changed.connect(lambda: changed.append(1))
    with m.bulk_edit():
        g = edits(m)
        _assert(not changed, "nothing is announced inside the block")
        _assert(len(m.midi_data.tracks[0]) == len(_make_manager().midi_data.tracks[0]),
                "the .mid is not rebuilt inside the block")
    _assert(len(changed) == 1, f"the block ends with one notification (got {len(changed)})")
    _assert(max(n["time"] for n in _fade_notes(m, g)) < 9.0,
            "the neighbour that arrived in the block moved the fade-out")
    _assert(ramps(m) == ramps(one_by_one), "the ramps are those of the same edits one by one")
    _assert(m.midi_data.length == one_by_one.midi_data.length,
            "the .mid is rebuilt at the end")
    m.undo()
    _assert(not m.midi_events and not m.midi_notes, "one undo takes the whole block back")


def test_ramps_roundtrip_without_leaking_back_as_clips():
    """Ramp notes are stripped from the timeline on load and re-derived, so a
    save/load cycle is a fixed point: no clip count growth, no doubled ramps."""
    print("test_ramps_roundtrip_without_leaking_back_as_clips")
    import tempfile
    m = _make_manager()
    for i in range(3):
        g = m.add_scene(1.0 + i * 4.0, SCENE_CH, f"S{i}", channel=LEDBAR_CH)
        m.set_scene_fade_in(g, 0.5)
        m.set_scene_fade_out(g, 0.5)
    eid = m.add_event(5, 40.0, 100, channel=MOVER_CH)
    m.set_event_right_edge(eid, 46.0)
    m.set_event_fade_in(eid, 0.5)
    m.set_event_fade_out(eid, 0.5)

    events = len(m.midi_events)
    ramps = len([n for n in m.midi_notes if n.get("is_fade")])
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.mid")
        _assert(m.export_to_file(path), "exported")
        m2 = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
        m2.load_from_file(path)
    _assert(len(m2.midi_events) == events,
            f"clip count is unchanged by a save+load "
            f"({events} → {len(m2.midi_events)})")
    reloaded = len([n for n in m2.midi_notes if n.get("is_fade")])
    _assert(reloaded == ramps,
            f"ramp count is unchanged too ({ramps} → {reloaded})")
    clip = next(e for e in m2.midi_events
                if e["note"] == 5 and not e.get("group_id"))
    _assert(abs(clip["fade_in"] - 0.5) < 1e-6
            and abs(clip["fade_out"] - 0.5) < 1e-6,
            "the direct-channel clip's fades came back exactly")


def main():
    test_set_fades_generate_fade_notes()
    test_fade_clamps_to_visible_duration()
    test_fade_in_plus_out_bounded_by_visible()
    test_fades_regenerate_on_resize()
    test_fade_rides_the_fixtures_own_dimmer()
    test_fixture_without_a_dimmer_cannot_fade()
    test_direct_channel_clip_ramps_its_own_note()
    test_indexed_verb_never_fades()
    test_fade_out_does_not_overwrite_the_next_clip()
    test_fade_out_follows_a_neighbours_arrival()
    test_bulk_edit_does_the_work_once()
    test_ramps_roundtrip_without_leaking_back_as_clips()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
