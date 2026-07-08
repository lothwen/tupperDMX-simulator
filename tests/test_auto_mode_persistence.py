"""
Characterization test: per-fixture auto-mode persistence in the .mid.

Auto mode (a fixture following the song-part program) used to be runtime-only
state on the software controller, lost on reload. It is now persisted as a
per-track "setup" note (note 71) whose velocity packs the flags. Because
describe_note(71) returns None, the codec keeps it in midi_notes (saved/loaded)
but excludes it from midi_events (never a timeline clip).

Covers:
  * note_layout pack/unpack helpers and the deliberate invisibility of note 71;
  * MidiDocument.set_fixture_auto_flag / auto_channels add/update/remove,
    operating on midi_notes only;
  * export -> load round-trip preserving auto channels with no phantom clip;
  * MidiManager.set_fixture_auto mirroring onto a live controller and
    seed_controller_auto restoring a fresh controller from the document.

Pure data logic — no GUI needed.

Run with:
    uv run python tests/test_auto_mode_persistence.py
"""

import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import note_layout
from core.config import FixtureInstance, FixturePosition
from midi.manager import MidiManager
from software_controller.main import DMXController


# ledbar @ ch0, blinders @ ch1/ch2 (channel == patch index via positional
# fallback, since these instances leave channel unassigned).
PATCH = [
    FixtureInstance(type="ledbar", dmx_start=1, bars=[FixturePosition(x=0, y=0)]),
    FixtureInstance(type="blinder", dmx_start=33),
    FixtureInstance(type="blinder", dmx_start=65),
]
LEDBAR_CH, B1_CH, B2_CH = 0, 1, 2

SETUP = note_layout.FIXTURE_SETUP


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _make_manager():
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    m.set_song_duration(240.0)
    return m


def _roundtrip(m):
    fd, path = tempfile.mkstemp(suffix=".mid")
    os.close(fd)
    try:
        _assert(m.export_to_file(path), "export_to_file succeeded")
        m2 = _make_manager()
        _assert(m2.load_from_file(path), "load_from_file succeeded")
        return m2
    finally:
        os.unlink(path)


def test_pack_unpack_and_invisibility():
    print("test_pack_unpack_and_invisibility")
    _assert(note_layout.pack_setup(auto=True) == note_layout.SETUP_AUTO,
            "pack_setup(auto=True) sets the auto bit")
    _assert(note_layout.pack_setup(auto=False) == 0,
            "pack_setup(auto=False) is 0 (all flags off)")
    _assert(note_layout.unpack_setup(note_layout.pack_setup(auto=True))["auto"] is True,
            "unpack round-trips auto=True")
    _assert(note_layout.unpack_setup(0)["auto"] is False,
            "unpack of 0 is auto=False")
    _assert(note_layout.describe_note(SETUP) is None,
            "describe_note(71) is None -> never renders as a clip")
    _assert(note_layout.is_fixture_setup(SETUP) and not note_layout.is_fixture_setup(70),
            "is_fixture_setup identifies note 71 only")


def test_document_add_update_remove():
    print("test_document_add_update_remove")
    m = _make_manager()
    doc = m._doc

    _assert(doc.set_fixture_auto_flag(LEDBAR_CH, True) is True,
            "enabling auto reports a change")
    _assert(doc.auto_channels() == {LEDBAR_CH}, "auto_channels reflects the flag")

    setup_notes = [n for n in m.midi_notes if n["note"] == SETUP]
    _assert(len(setup_notes) == 1, "exactly one setup note in midi_notes")
    _assert(setup_notes[0]["channel"] == LEDBAR_CH and setup_notes[0]["velocity"] > 0,
            "setup note carries the channel and a non-zero velocity")
    _assert(not any(e["note"] == SETUP for e in m.midi_events),
            "setup note never enters midi_events (no clip)")

    _assert(doc.set_fixture_auto_flag(LEDBAR_CH, True) is False,
            "re-enabling is a no-op (no change)")

    _assert(doc.set_fixture_auto_flag(LEDBAR_CH, False) is True,
            "disabling reports a change")
    _assert(doc.auto_channels() == set(), "auto_channels empty after disable")
    _assert(not any(n["note"] == SETUP for n in m.midi_notes),
            "setup note removed when all flags are off")
    _assert(doc.set_fixture_auto_flag(LEDBAR_CH, False) is False,
            "disabling when absent is a no-op")


def test_roundtrip_persists_without_phantom_clip():
    print("test_roundtrip_persists_without_phantom_clip")
    m = _make_manager()
    # Facade path: writes the flag + commits (rebuild) so midi_data is current.
    m.set_fixture_auto(LEDBAR_CH, True)
    m.set_fixture_auto(B2_CH, True)

    m2 = _roundtrip(m)
    _assert(m2.auto_channels() == {LEDBAR_CH, B2_CH},
            "auto channels survive export -> load")
    _assert(not any(e["note"] == SETUP for e in m2.midi_events),
            "no phantom setup clip on the reloaded timeline")


def test_controller_mirror_and_seed():
    print("test_controller_mirror_and_seed")
    m = _make_manager()
    ctrl = DMXController(PATCH)
    m.set_software_controller(ctrl)

    m.set_fixture_auto(B1_CH, True)
    _assert(ctrl.is_fixture_auto(B1_CH) is True,
            "facade mirrors the flag onto the live controller")
    _assert(m.auto_channels() == {B1_CH}, "document persists the flag")

    m2 = _roundtrip(m)
    ctrl2 = DMXController(PATCH)
    m2.set_software_controller(ctrl2)
    m2.seed_controller_auto(ctrl2)
    _assert(ctrl2.is_fixture_auto(B1_CH) is True,
            "a fresh controller is seeded on from the loaded document")
    _assert(ctrl2.is_fixture_auto(LEDBAR_CH) is False
            and ctrl2.is_fixture_auto(B2_CH) is False,
            "non-auto fixtures stay off after seeding")


def test_song_switch_follows_persisted_state():
    print("test_song_switch_follows_persisted_state")
    # Two song files with different per-track auto state.
    a = _make_manager()
    a.set_fixture_auto(LEDBAR_CH, True)
    b = _make_manager()
    b.set_fixture_auto(B1_CH, True)

    fd_a, path_a = tempfile.mkstemp(suffix=".mid")
    os.close(fd_a)
    fd_b, path_b = tempfile.mkstemp(suffix=".mid")
    os.close(fd_b)
    try:
        _assert(a.export_to_file(path_a) and b.export_to_file(path_b),
                "exported both song files")

        # One live manager + one persistent controller, as in the running app;
        # switching songs calls load_from_file on the same manager.
        live = _make_manager()
        ctrl = DMXController(PATCH)
        live.set_software_controller(ctrl)

        live.load_from_file(path_a)
        _assert(ctrl.is_fixture_auto(LEDBAR_CH) and not ctrl.is_fixture_auto(B1_CH),
                "song A loaded: ledbar auto on, blinder off")

        live.load_from_file(path_b)
        _assert(ctrl.is_fixture_auto(B1_CH) and not ctrl.is_fixture_auto(LEDBAR_CH),
                "switching to song B follows its persisted state (A's flag cleared)")

        live.create_empty_track()
        _assert(not ctrl.is_fixture_auto(B1_CH) and not ctrl.is_fixture_auto(LEDBAR_CH),
                "switching to a MIDI-less song clears all auto flags")
    finally:
        os.unlink(path_a)
        os.unlink(path_b)


def main():
    test_pack_unpack_and_invisibility()
    test_document_add_update_remove()
    test_roundtrip_persists_without_phantom_clip()
    test_controller_mirror_and_seed()
    test_song_switch_follows_persisted_state()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
