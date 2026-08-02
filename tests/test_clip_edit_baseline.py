"""
Characterization test: what timeline clip editing does TODAY, before the
generalized ClipEditDialog replaces it.

Every assertion here pins behaviour that is about to change, so the diff that
changes it has to say so out loud. Three defects are pinned as they currently
stand — each one is marked DEFECT and names the phase that flips it:

  * the ledbar-shaped scene dialog silently mangles any other fixture's scene:
    it reads seven hardcoded field names, so a blinder's six other channels
    come back as zeros and its `brightness`/`speed` are written from the wrong
    widgets;
  * `update_scene` can only touch channels the group ALREADY has a member for,
    so a scene can never grow onto a channel it was not created with;
  * `field_note_map` is uncapped, so a fixture with more than 23 channels emits
    notes 24+ — which `handle_note_on` dispatches as SCENE LOAD, not as a
    channel set.

Run with:
    uv run python tests/test_clip_edit_baseline.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dataclasses import dataclass

from core import note_layout
from core.fixture_library import resolve_fixture
from midi.manager import MidiManager, _channel_to_velocity
from tests import ofl_factory


@dataclass
class FakeFixture:
    """Minimal patched-fixture stand-in: its index in the patch == the MIDI
    channel that selects it (the generalized note map routes by channel)."""
    type: str


PATCH = [FakeFixture("ledbar"), FakeFixture("blinder")]
LEDBAR_CH, BLINDER_CH = 0, 1

# The blinder's own eight channels, at values that are all distinct and all
# recoverable through the velocity//2 round-trip (even numbers, >= 2).
BLINDER_SCENE = {"brightness": 200, "cool1": 180, "cool2": 160, "warm1": 140,
                 "warm2": 120, "strobe": 100, "mode": 80, "speed": 60}

# What today's SceneEditDialog.get_values() returns, whatever fixture it was
# opened on: seven literal ledbar field names.
LEDBAR_DIALOG_KEYS = ("brightness", "blue", "green", "red",
                      "animation", "speed", "direction")


def _make_manager():
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    m.set_song_duration(240.0)
    return m


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _members(m, gid):
    return [e for e in m.midi_events if e.get("group_id") == gid]


def _values_by_field(m, gid, channel):
    """Reconstruct {field: 0..255} from a group exactly as Timeline._edit_scene
    does — velocity*2, clamped, defaulting to 0 for a field with no member."""
    velocity_by_note = {e["note"]: e["velocity"] for e in _members(m, gid)}
    return {field: min(255, velocity_by_note.get(note, 0) * 2)
            for field, note in m.channel_note_map(channel)}


# ---------------------------------------------------------------------------
# DEFECT 1 — the scene dialog is ledbar-shaped. Phase 3 replaces it.
# ---------------------------------------------------------------------------
def test_scene_reconstruction_is_already_fixture_driven():
    """The INPUT side is fine: _edit_scene reads the blinder's own eight
    fields. Only the dialog throws them away, which is why the fix is a
    dialog rewrite and not a data-layer one."""
    print("test_scene_reconstruction_is_already_fixture_driven")
    m = _make_manager()
    g = m.add_scene(4.0, BLINDER_SCENE, "B", channel=BLINDER_CH)
    reconstructed = _values_by_field(m, g, BLINDER_CH)

    _assert(set(reconstructed) == set(BLINDER_SCENE),
            "reconstruction yields the blinder's own 8 fields")
    _assert(reconstructed == BLINDER_SCENE,
            f"all 8 values round-trip through velocity*2 ({reconstructed})")


def test_ledbar_shaped_dialog_mangles_a_blinder_scene():
    """DEFECT (Phase 3): feeding the blinder's group through the seven ledbar
    keys drops six channels and rewrites two from the wrong widgets."""
    print("test_ledbar_shaped_dialog_mangles_a_blinder_scene")
    m = _make_manager()
    g = m.add_scene(4.0, BLINDER_SCENE, "B", channel=BLINDER_CH)

    # What the dialog shows: .get(key, 0) against the blinder's dict.
    reconstructed = _values_by_field(m, g, BLINDER_CH)
    shown = {key: reconstructed.get(key, 0) for key in LEDBAR_DIALOG_KEYS}
    blank = [key for key, value in shown.items() if value == 0]
    _assert(sorted(blank) == ["animation", "blue", "direction", "green", "red"],
            f"5 of the 7 rows show a meaningless 0 ({sorted(blank)})")

    # What the dialog writes back: the same seven keys, untouched by the user.
    m.update_scene(g, dict(shown), name="B")
    after = _values_by_field(m, g, BLINDER_CH)

    _assert(after["brightness"] == BLINDER_SCENE["brightness"],
            "brightness survives (it happens to share the ledbar's name)")
    _assert(after["speed"] == BLINDER_SCENE["speed"],
            "speed survives (same name collision)")
    untouched = ["cool1", "cool2", "warm1", "warm2", "strobe", "mode"]
    _assert(all(after[key] == BLINDER_SCENE[key] for key in untouched),
            "the other 6 are left alone — update_scene skips unknown fields")
    _assert(after == BLINDER_SCENE,
            "so a no-op OK is lossless TODAY; the damage lands the moment the "
            "user moves a fader, since 5 rows sit at a bogus 0")


# ---------------------------------------------------------------------------
# DEFECT 2 — update_scene cannot grow a group. Phase 2 changes this.
# ---------------------------------------------------------------------------
def test_update_scene_grows_onto_a_missing_channel():
    """FIXED (Phase 2): a field with no member is INSERTED, so a partial scene
    can be completed from the editor. Was silently dropped before."""
    print("test_update_scene_grows_onto_a_missing_channel")
    m = _make_manager()
    partial = {"brightness": 200, "cool1": 180}
    g = m.add_scene(4.0, partial, "P", channel=BLINDER_CH)

    control = [e for e in _members(m, g) if 1 <= e["note"] <= 8]
    _assert(len(control) == 2, f"group starts with 2 channel members ({len(control)})")

    _assert(m.update_scene(g, {"warm1": 140}), "update_scene returns True")
    after = [e for e in _members(m, g) if 1 <= e["note"] <= 8]
    _assert(len(after) == 3, f"'warm1' joined the group ({len(after)} members)")
    _assert(_values_by_field(m, g, BLINDER_CH)["warm1"] == 140,
            "warm1 now reads the value that was written")

    grown = next(e for e in _members(m, g) if e["note"] == 4)
    _assert(abs(grown["time"] - 4.0) < 1e-6, "the new member sits at the group's time")
    _assert(grown["channel"] == BLINDER_CH, "and on the group's fixture")
    _assert(grown["scene_name"] == "P", "carrying the group's scene name")
    _assert(any(n["note"] == 4 and abs(n["time"] - 4.0) < 1e-6
                for n in m.midi_notes), "mirrored into midi_notes")


def test_update_scene_removes_only_when_asked():
    """Removal is opt-in, and never takes a group below two members — one
    member renders as an ungrouped clip and never re-clusters on load."""
    print("test_update_scene_removes_only_when_asked")
    m = _make_manager()
    g = m.add_scene(4.0, BLINDER_SCENE, "B", channel=BLINDER_CH)
    before = len(_members(m, g))

    m.update_scene(g, {"cool1": 20})
    _assert(len(_members(m, g)) == before,
            "a plain value edit never drops a member")

    m.update_scene(g, {}, remove={"cool1", "cool2"})
    notes = {e["note"] for e in _members(m, g)}
    _assert(2 not in notes and 3 not in notes, "the named fields are gone")
    _assert(len(_members(m, g)) == before - 2, "exactly two members removed")
    _assert(not any(n["note"] in (2, 3) and abs(n["time"] - 4.0) < 1e-6
                    for n in m.midi_notes), "their midi_notes twins too")

    # Strip it down to the floor, then try to go under.
    m.update_scene(g, {}, remove={"brightness", "warm1", "warm2", "strobe",
                                  "mode", "speed"})
    survivors = len(_members(m, g))
    _assert(survivors == 2, f"stops at 2 members ({survivors})")
    m.update_scene(g, {}, remove={"speed"})
    _assert(len(_members(m, g)) == 2, "and refuses to go below 2")


def test_update_scene_leaves_absent_fields_alone():
    """The flip side, and the reason auto-removal would be wrong: a field the
    caller omits keeps its member and its value."""
    print("test_update_scene_leaves_absent_fields_alone")
    m = _make_manager()
    g = m.add_scene(4.0, BLINDER_SCENE, "B", channel=BLINDER_CH)
    m.update_scene(g, {"cool1": 20})
    after = _values_by_field(m, g, BLINDER_CH)

    _assert(after["cool1"] == 20, "the named field is written")
    _assert(all(after[key] == BLINDER_SCENE[key]
                for key in BLINDER_SCENE if key != "cool1"),
            "every omitted field keeps its value")


def test_channel_value_of_zero_is_unauthorable():
    """`_channel_to_velocity` floors at 1 (velocity 0 would read as a note_off),
    so a fader at 0 comes back as DMX 2. Pinned, not fixed: it affects every
    saved show and needs its own migration."""
    print("test_channel_value_of_zero_is_unauthorable")
    _assert(_channel_to_velocity(0) == 1, "0 -> velocity 1")
    _assert(_channel_to_velocity(1) == 1, "1 -> velocity 1")
    m = _make_manager()
    g = m.add_scene(4.0, dict(BLINDER_SCENE, cool1=0), "Z", channel=BLINDER_CH)
    _assert(_values_by_field(m, g, BLINDER_CH)["cool1"] == 2,
            "a scene channel authored at 0 replays as 2")


# ---------------------------------------------------------------------------
# DEFECT 3 — field_note_map is uncapped. Phase 2 caps it at 23.
# ---------------------------------------------------------------------------
def _wide_fdef(count):
    """A synthetic fixture with `count` plain dimmer channels."""
    channels = {f"c{i:02d}": ofl_factory.channel("Intensity") for i in range(count)}
    return ofl_factory.fdef(name=f"wide{count}", channels=channels,
                            mode_channels=list(channels))


def test_field_note_map_stops_at_the_addressable_limit():
    """FIXED (Phase 2): notes past 23 are the scene-load verbs, so a wide
    fixture's slots 24+ used to make handle_note_on load a scene instead of
    setting a channel. They are now simply out of reach."""
    print("test_field_note_map_stops_at_the_addressable_limit")
    fdef = _wide_fdef(30)
    _assert(fdef.channel_count == 30, "synthetic fixture has 30 channels")

    mapping = note_layout.field_note_map(fdef)
    notes = [note for _field, note in mapping]
    _assert(len(mapping) == 23, f"capped at 23 entries ({len(mapping)})")
    _assert(max(notes) == note_layout.CHANNEL_OFFSET_RANGE[1],
            f"highest note is the channel-verb ceiling ({max(notes)})")
    _assert(not any(note_layout.SCENE_LOAD_RANGE[0] <= n
                    <= note_layout.SCENE_LOAD_RANGE[1] for n in notes),
            "nothing collides with SCENE_LOAD_RANGE any more")

    fields = [field for field, _note in mapping]
    _assert(fields == [f"c{i:02d}" for i in range(23)],
            "the first 23 channels, in DMX order")


def test_field_note_map_is_unaffected_below_the_cap():
    """The shipped fixtures are all well under 23, so the cap must be a no-op
    for them."""
    print("test_field_note_map_is_unaffected_below_the_cap")
    for type_name, expected in (("ledbar", 7), ("blinder", 8)):
        fdef = resolve_fixture(type_name)
        mapping = note_layout.field_note_map(fdef)
        _assert(len(mapping) == expected,
                f"{type_name}: {expected} entries")
        _assert([note for _f, note in mapping] == list(range(1, expected + 1)),
                f"{type_name}: notes 1..{expected}")


def main():
    test_scene_reconstruction_is_already_fixture_driven()
    test_ledbar_shaped_dialog_mangles_a_blinder_scene()
    test_update_scene_grows_onto_a_missing_channel()
    test_update_scene_removes_only_when_asked()
    test_update_scene_leaves_absent_fields_alone()
    test_channel_value_of_zero_is_unauthorable()
    test_field_note_map_stops_at_the_addressable_limit()
    test_field_note_map_is_unaffected_below_the_cap()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
