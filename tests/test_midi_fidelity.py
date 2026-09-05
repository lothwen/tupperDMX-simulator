"""
Characterization test: .mid authoring fidelity via tdmx1 sidecar metas
(midi/codec.py + MidiManager._apply_sidecar_meta).

Locks what plain MIDI loses: custom scene names survive the roundtrip, scene
group ids are STABLE across save/load, per-clip fades persist, and repeated
save/load cycles (including a BPM change) neither drift nor accumulate metas.
Also locks the interop export: no tdmx1 metas in the file, notes intact, and
the doc left unmodified.

Run with:
    uv run python tests/test_midi_fidelity.py
"""

import os
import sys
import tempfile
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mido

from core import note_layout
from midi.codec import SIDECAR_PREFIX
from midi.manager import MidiManager


@dataclass
class FakeFixture:
    type: str


PATCH = [FakeFixture("ledbar"), FakeFixture("blinder")]
LEDBAR_CH, BLINDER_CH = 0, 1
BLINDER_CLIP = 2   # the blinder's cool1 channel: a standalone clip that
                   # never scene-clusters (clustering needs 2+ at one time)
                   # and whose velocity is a LEVEL, so it can carry a fade

SCENE_CH = {"brightness": 255, "blue": 255, "green": 0, "red": 0,
            "animation": 0, "speed": 0, "direction": 0}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _make_manager():
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    m.set_song_duration(240.0)
    return m


def _roundtrip(m, path, bpm=None):
    assert m.export_to_file(path, bpm)
    m2 = _make_manager()
    assert m2.load_from_file(path)
    return m2


def _sidecar_metas(path):
    return [msg.text for msg in mido.MidiFile(path).merged_track
            if msg.is_meta and msg.type == 'text'
            and msg.text.startswith(SIDECAR_PREFIX)]


def test_custom_scene_name_survives():
    print("test_custom_scene_name_survives")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.mid")
        m = _make_manager()
        gid = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
        m.update_scene(gid, SCENE_CH, name="Mi rollo espacial")

        m2 = _roundtrip(m, path)
        names = {e["scene_name"] for e in m2.midi_events if e.get("group_id")}
        _assert(names == {"Mi rollo espacial"},
                "a custom scene name survives save+load (no 'Scene @ Xs' fallback)")


def test_group_id_stable_across_saves():
    print("test_group_id_stable_across_saves")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.mid")
        m = _make_manager()
        gid = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)

        m2 = _roundtrip(m, path)
        gids = {e["group_id"] for e in m2.midi_events if e.get("group_id")}
        _assert(gids == {gid}, "the scene keeps its group_id across save+load")

        m3 = _roundtrip(m2, path)
        gids3 = {e["group_id"] for e in m3.midi_events if e.get("group_id")}
        _assert(gids3 == {gid}, "…and across a second cycle (identity is stable)")


def test_clip_fades_persist():
    print("test_clip_fades_persist")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.mid")
        m = _make_manager()
        eid = m.add_event(BLINDER_CLIP, 5.0, 100, channel=BLINDER_CH)
        m.set_event_right_edge(eid, 12.0)
        assert m.set_event_fade_in(eid, 0.75)
        assert m.set_event_fade_out(eid, 1.25)

        m2 = _roundtrip(m, path)
        clip = next(e for e in m2.midi_events
                    if e["note"] == BLINDER_CLIP and not e.get("group_id"))
        _assert(abs(clip["fade_in"] - 0.75) < 1e-6
                and abs(clip["fade_out"] - 1.25) < 1e-6,
                "per-clip fades survive save+load exactly")


def test_scene_fades_still_roundtrip_with_metas():
    print("test_scene_fades_still_roundtrip_with_metas")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.mid")
        m = _make_manager()
        gid = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
        m.set_scene_right_edge(gid, 10.0)
        m.set_scene_fade_in(gid, 1.0)
        m.set_scene_fade_out(gid, 1.5)

        m2 = _roundtrip(m, path)
        member = next(e for e in m2.midi_events if e.get("group_id") == gid)
        _assert(abs(member["fade_in"] - 1.0) < 1e-6
                and abs(member["fade_out"] - 1.5) < 1e-6,
                "scene fades (ramp-derived) still roundtrip, now onto the stable gid")
        fade_notes = [n for n in m2.midi_notes
                      if n.get("is_fade") and n.get("parent_group_id") == gid]
        _assert(fade_notes, "fade ramp notes re-attach to the stable group id")


def test_repeated_cycles_do_not_accumulate_metas():
    print("test_repeated_cycles_do_not_accumulate_metas")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.mid")
        m = _make_manager()
        m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
        eid = m.add_event(BLINDER_CLIP, 20.0, 100, channel=BLINDER_CH)
        m.set_event_fade_in(eid, 0.5)

        m2 = _roundtrip(m, path)
        count1 = len(_sidecar_metas(path))
        m3 = _roundtrip(m2, path, bpm=142)   # save again, with a tempo change
        count2 = len(_sidecar_metas(path))
        _assert(count1 == count2 == 2,
                f"one meta per unit, regenerated not accumulated (got {count1}/{count2})")

        clip = next(e for e in m3.midi_events
                    if e["note"] == BLINDER_CLIP and not e.get("group_id"))
        _assert(abs(clip["fade_in"] - 0.5) < 1e-6,
                "clip fade still matches after a BPM change re-save")
        _assert(abs(clip["time"] - 20.0) < 0.02,
                "event wall-clock timing preserved through the tempo change")


def test_interop_export_is_clean_and_nondestructive():
    print("test_interop_export_is_clean_and_nondestructive")
    with tempfile.TemporaryDirectory() as tmp:
        owned = os.path.join(tmp, "owned.mid")
        clean = os.path.join(tmp, "interop.mid")
        m = _make_manager()
        gid = m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
        m.update_scene(gid, SCENE_CH, name="Custom")
        eid = m.add_event(BLINDER_CLIP, 20.0, 100, channel=BLINDER_CH)
        m.set_event_fade_in(eid, 0.5)

        assert m.export_to_file(owned, 120)
        assert m.export_to_file(clean, 120, interop=True)
        _assert(len(_sidecar_metas(owned)) == 2, "owned export carries the metas")
        _assert(_sidecar_metas(clean) == [], "interop export carries NO tdmx1 metas")

        def _notes(p):
            return [(msg.type, msg.note, msg.velocity, msg.time)
                    for msg in mido.MidiFile(p).merged_track
                    if msg.type in ("note_on", "note_off")]
        _assert(_notes(owned) == _notes(clean),
                "interop stripping leaves the note stream untouched")

        # The clean file re-imports (heuristics take over: generic name, fresh gid).
        m2 = _make_manager()
        assert m2.load_from_file(clean)
        names = {e["scene_name"] for e in m2.midi_events if e.get("group_id")}
        _assert(names == {"Scene @ 4.00s"},
                "interop re-import falls back to the heuristic name")

        # Exporting interop did not strip the in-memory doc.
        assert m.export_to_file(owned, 120)
        _assert(len(_sidecar_metas(owned)) == 2,
                "the interop export left the document's own data intact")


def test_a_chase_clip_on_the_first_chase_survives(qapp=None):
    print("test_a_chase_clip_on_the_first_chase_survives")
    from core.note_layout import CHASE_LOAD

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.mid")
        # velocity IS the chase index, so the FIRST chase is velocity 0 — and a
        # fixture with only one chase can address it no other way.
        for index in (0, 1):
            m = _make_manager()
            m.add_event(CHASE_LOAD, 4.0, index, channel=BLINDER_CH)
            m2 = _roundtrip(m, path)
            kept = [e for e in m2.midi_events if e["note"] == CHASE_LOAD]
            _assert(len(kept) == 1 and kept[0]["velocity"] == index,
                    f"a clip loading chase {index} survives save+load — "
                    f"velocity {index} is a chase index, not a note_off")
            # and again, because rebuild() writes from midi_notes: an event
            # that never paired with its note_off is dropped on the NEXT save.
            m3 = _roundtrip(m2, path)
            _assert(len([e for e in m3.midi_events if e["note"] == CHASE_LOAD]) == 1,
                    f"…and survives a second cycle (chase {index} keeps its "
                    f"duration entry, which is what rebuild writes from)")


def test_legacy_file_without_metas_unaffected():
    print("test_legacy_file_without_metas_unaffected")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "legacy.mid")
        m = _make_manager()
        m.add_scene(4.0, SCENE_CH, "A", channel=LEDBAR_CH)
        assert m.export_to_file(path, 120, interop=True)  # = a legacy file

        m2 = _make_manager()
        assert m2.load_from_file(path)
        groups = {e["group_id"] for e in m2.midi_events if e.get("group_id")}
        _assert(len(groups) == 1, "legacy files still cluster into scenes")


def main():
    test_custom_scene_name_survives()
    test_group_id_stable_across_saves()
    test_clip_fades_persist()
    test_scene_fades_still_roundtrip_with_metas()
    test_repeated_cycles_do_not_accumulate_metas()
    test_interop_export_is_clean_and_nondestructive()
    test_a_chase_clip_on_the_first_chase_survives()
    test_legacy_file_without_metas_unaffected()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
