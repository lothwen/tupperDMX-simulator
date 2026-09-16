"""
Characterization test: the per-fixture colour palette (note 60 / START).

START used to be a draggable clip whose velocity packed a hue and a palette
mode, editable anywhere on the timeline and any number of times per song. It is
per-fixture STATE now, shaped exactly like the note-71 setup flags: one note 60
per MIDI channel, pinned at t=0, living in ``midi_notes`` only — so it persists
and it replays, but nothing renders it as a clip and no drop can author one.

Covers:
  * note_layout pack/unpack and the deliberate invisibility of note 60
    (describe_note / note_applies_to / verb_choices / build_note_map / clips);
  * MidiDocument.set_fixture_palette add / update / remove, midi_notes only,
    collapsing a legacy file's several note-60s to one;
  * a rigid shift leaving the palette note at t=0;
  * export -> load round-trip with no phantom clip;
  * the Add/Edit fixture dialog's Palette tab: offered for a Color Changer
    only, loading the fixture's own value and writing it on OK.

Run with:
    uv run python tests/test_fixture_palette.py
"""

import sys
import os
import tempfile

# Headless Qt for standalone runs (pytest sets this via conftest.py).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import clips, note_layout
from core.config import Config, FixtureInstance, FixturePosition
from midi.manager import MidiManager

START = note_layout.START

# ledbar @ ch0 (a Color Changer), blinder @ ch1 (not one).
PATCH = [
    FixtureInstance(type="ledbar", dmx_start=1, channel=0,
                    bars=[FixturePosition(x=0, y=0)]),
    FixtureInstance(type="blinder", dmx_start=33, channel=1),
]
LEDBAR_CH, BLINDER_CH = 0, 1


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


_app = None


def _ensure_app():
    global _app
    from PyQt6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    return _app


def _make_manager():
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    m.set_song_duration(240.0)
    return m


# ---------------------------------------------------------------------------
# The note layout
# ---------------------------------------------------------------------------
def test_pack_unpack_and_invisibility():
    print("test_pack_unpack_and_invisibility")
    _assert(note_layout.pack_palette(7, 2) == 44,
            "pack_palette is hue * 6 + mode, the same byte handle_note_on splits")
    _assert(note_layout.unpack_palette(44) == (7, 2), "unpack round-trips")
    _assert(note_layout.pack_palette(0, 0) == 0,
            "the default palette packs to 0 — which is why absence means default")
    _assert(note_layout.pack_palette(99, 99)
            == note_layout.RANDOM_HUE_INDEX * note_layout.PALETTE_MODE_COUNT + 5,
            "out-of-range choices clamp rather than overflowing the velocity")
    _assert(note_layout.unpack_palette(126) == (0, 0),
            "a foreign velocity past the last hue falls back to hue 0, not a raise")

    _assert(note_layout.describe_note(START) is None,
            "describe_note(60) is None -> never renders as a clip")
    _assert(note_layout.is_fixture_palette(START)
            and not note_layout.is_fixture_palette(61),
            "is_fixture_palette identifies note 60 only")
    _assert(note_layout.note_applies_to(START, None) is False,
            "no lane accepts a note-60 drop")
    _assert(str(START) not in note_layout.build_note_map(),
            "the fixture-agnostic note map has no START entry")

    from core.fixture_library import resolve_fixture
    offered = {note for _label, entries in
               note_layout.verb_choices(resolve_fixture("ledbar"))
               for note, _n, _c in entries}
    _assert(START not in offered, "the clip editor's verb picker never offers it")


def test_the_clip_library_no_longer_offers_it():
    print("test_the_clip_library_no_longer_offers_it")
    config = Config()
    config.fixtures = list(PATCH)
    groups = clips.build_groups(config)
    every = [c for g in groups for c in g["clips"]]
    _assert(every, "the library still builds clips")
    _assert(not any(c["payload"].get("note") == START for c in every),
            "not one of them is a START clip")


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------
def test_document_add_update_remove():
    print("test_document_add_update_remove")
    m = _make_manager()
    doc = m._doc

    _assert(doc.fixture_palette(LEDBAR_CH) == 0,
            "a fixture with no palette note reads as the default palette")
    _assert(doc.set_fixture_palette(LEDBAR_CH, 44) is True,
            "setting a palette reports a change")
    _assert(doc.fixture_palette(LEDBAR_CH) == 44, "and reads back")

    notes = [n for n in m.midi_notes if n["note"] == START]
    _assert(len(notes) == 1, "exactly one palette note in midi_notes")
    _assert(notes[0]["time"] == 0.0 and notes[0]["channel"] == LEDBAR_CH,
            "pinned at t=0 on the fixture's own channel")
    _assert(not any(e["note"] == START for e in m.midi_events),
            "and never enters midi_events (no clip)")

    _assert(doc.set_fixture_palette(LEDBAR_CH, 44) is False,
            "rewriting the same palette is a no-op")
    _assert(doc.set_fixture_palette(LEDBAR_CH, 0) is True,
            "returning to the default reports a change")
    _assert(not any(n["note"] == START for n in m.midi_notes),
            "and removes the note — absence IS the default")
    _assert(doc.set_fixture_palette(LEDBAR_CH, 0) is False,
            "clearing an absent palette is a no-op")


def test_a_second_note_60_is_collapsed():
    """A legacy .mid could hold several START clips per channel (a re-palette
    mid-song). One fixture has one palette now, so a write keeps the FIRST in
    time and drops the rest."""
    print("test_a_second_note_60_is_collapsed")
    m = _make_manager()
    doc = m._doc
    for time, velocity in ((6.4, 45), (189.5, 127)):
        m.midi_notes.append({"id": f"legacy-{time}", "note": START,
                             "time": time, "duration": 0.1,
                             "velocity": velocity, "channel": LEDBAR_CH})
    _assert(doc.fixture_palette(LEDBAR_CH) == 45,
            "the earliest one is the palette the show actually hears")

    _assert(doc.set_fixture_palette(LEDBAR_CH, 44) is True, "a write reports a change")
    notes = [n for n in m.midi_notes if n["note"] == START]
    _assert(len(notes) == 1 and notes[0]["velocity"] == 44
            and notes[0]["time"] == 0.0,
            "one note survives, carrying the new palette, moved to t=0")


def test_a_rigid_shift_leaves_the_palette_at_zero():
    print("test_a_rigid_shift_leaves_the_palette_at_zero")
    m = _make_manager()
    m.add_event(1, 3.0, 100, channel=LEDBAR_CH)
    m.set_fixture_palette(LEDBAR_CH, 44)

    _assert(m.shift_all(-1.0), "a leftward shift the clips can take is allowed")
    palette = [n for n in m.midi_notes if n["note"] == START]
    _assert([n["time"] for n in palette] == [0.0],
            "the palette note stayed at t=0 (it is timeless persistence)")


def test_roundtrip_persists_without_phantom_clip():
    print("test_roundtrip_persists_without_phantom_clip")
    m = _make_manager()
    m.set_fixture_palette(LEDBAR_CH, 44)
    m.set_fixture_palette(BLINDER_CH, 7)

    fd, path = tempfile.mkstemp(suffix=".mid")
    os.close(fd)
    try:
        _assert(m.export_to_file(path), "export_to_file succeeded")
        m2 = _make_manager()
        _assert(m2.load_from_file(path), "load_from_file succeeded")
    finally:
        os.unlink(path)

    _assert(m2.fixture_palette(LEDBAR_CH) == 44
            and m2.fixture_palette(BLINDER_CH) == 7,
            "both palettes survive export -> load")
    _assert(not any(e["note"] == START for e in m2.midi_events),
            "no phantom START clip on the reloaded timeline")


# ---------------------------------------------------------------------------
# The dialog
# ---------------------------------------------------------------------------
def _dialog(midi_manager, instance=None):
    from stage.controller import StageController
    from ui.fixture.browser import AddFixtureDialog
    config = Config()
    config.fixtures = list(PATCH)
    return AddFixtureDialog(StageController(config), instance=instance,
                            midi_manager=midi_manager)


def test_the_tab_is_offered_to_colour_changers_only():
    print("test_the_tab_is_offered_to_colour_changers_only")
    _ensure_app()
    dialog = _dialog(_make_manager())
    try:
        index = dialog._palette_index
        _assert(dialog.tabs.tabText(index) == "Palette",
                "the Palette tab joins the detail panel's tabs")

        dialog.select_type("ledbar")
        _assert(dialog.tabs.isTabVisible(index),
                "a Color Changer gets it")
        dialog.select_type("hazer")
        _assert(not dialog.tabs.isTabVisible(index),
                "a hazer does not — it has no colour to cycle")
        dialog.select_type("blinder")
        _assert(not dialog.tabs.isTabVisible(index),
                "nor does a plain blinder")
    finally:
        dialog.deleteLater()


def test_no_song_no_tab():
    print("test_no_song_no_tab")
    _ensure_app()
    dialog = _dialog(None)
    try:
        dialog.select_type("ledbar")
        _assert(not dialog.tabs.isTabVisible(dialog._palette_index),
                "with no .mid to write into, the tab is not offered at all")
        _assert(dialog.apply_palette(LEDBAR_CH) is False,
                "and applying is a no-op rather than a crash")
    finally:
        dialog.deleteLater()


def test_edit_mode_loads_and_writes_the_fixtures_palette():
    print("test_edit_mode_loads_and_writes_the_fixtures_palette")
    _ensure_app()
    m = _make_manager()
    m.set_fixture_palette(LEDBAR_CH, 44)

    dialog = _dialog(m, instance=PATCH[0])
    try:
        _assert(dialog.palette_tab.velocity() == 44,
                "the tab opens on the fixture's own palette")
        _assert((dialog.palette_tab.hue_index,
                 dialog.palette_tab.palette_mode_index) == (7, 2),
                "unpacked into the two controls")

        dialog.palette_tab._on_color_changed(3)
        dialog.palette_tab._on_mode_changed(1)
        _assert(m.fixture_palette(LEDBAR_CH) == 44,
                "nothing is written while the dialog is open")

        dialog.apply_edits()
        _assert(m.fixture_palette(LEDBAR_CH) == note_layout.pack_palette(3, 1),
                "OK writes the new palette")
    finally:
        dialog.deleteLater()


def test_a_hidden_tab_writes_nothing():
    """The blinder is not a Color Changer, so editing it must not stamp a
    palette note onto its channel."""
    print("test_a_hidden_tab_writes_nothing")
    _ensure_app()
    m = _make_manager()
    dialog = _dialog(m, instance=PATCH[1])
    try:
        _assert(not dialog.tabs.isTabVisible(dialog._palette_index),
                "the blinder's dialog has no Palette tab")
        dialog.apply_edits()
        _assert(not any(n["note"] == START for n in m.midi_notes),
                "and OK leaves the .mid without a palette note")
    finally:
        dialog.deleteLater()


def main():
    _ensure_app()
    test_pack_unpack_and_invisibility()
    test_the_clip_library_no_longer_offers_it()
    test_document_add_update_remove()
    test_a_second_note_60_is_collapsed()
    test_a_rigid_shift_leaves_the_palette_at_zero()
    test_roundtrip_persists_without_phantom_clip()
    test_the_tab_is_offered_to_colour_changers_only()
    test_no_song_no_tab()
    test_edit_mode_loads_and_writes_the_fixtures_palette()
    test_a_hidden_tab_writes_nothing()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
