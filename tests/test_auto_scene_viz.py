"""
Characterization test: the auto-mode song-part visualization helpers.

Covers the pure pieces behind the read-only auto clips drawn on a fixture lane
when its 'A' (auto) toggle is on:
  * note_layout.song_part_refs       — index the "song_parts" tags on a def.
  * note_layout.song_part_clip_info  — per-part label/colour (single vs many
    candidates vs none).
  * midi_events.subtract_intervals   — split a song-part section around the
    manual clips that take priority over the program.

Run with:
    uv run python tests/test_auto_scene_viz.py
"""

import sys
import os
from dataclasses import dataclass

# Headless Qt for standalone runs (pytest sets this via conftest.py).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import note_layout
from core.fixture_library import load_fixture_defs
from timeline.tracks.midi_events import AutoSceneClip


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _ledbar():
    return load_fixture_defs()["ledbar"]


def test_song_part_refs_index():
    print("test_song_part_refs_index")
    refs = note_layout.song_part_refs(_ledbar())
    # INTRO/VERSE are single scenes; CHORUS is the 6-chase set.
    _assert(refs["INTRO"] == [note_layout.SongPartRef(False, "FILLS2", 2)],
            "INTRO -> one FILLS2 scene")
    _assert(len(refs["CHORUS"]) == 6 and all(r.is_chase for r in refs["CHORUS"]),
            "CHORUS -> six chases")
    _assert("BREAK" not in refs, "ledbar isn't tagged for BREAK")


def test_clip_info_single_candidate():
    print("test_clip_info_single_candidate")
    info = note_layout.song_part_clip_info(_ledbar(), "INTRO")
    _assert(info is not None, "INTRO resolves")
    # Single candidate -> the scene's own name, not the part name nor a suffix.
    _assert("+" not in info.part and info.part != "INTRO",
            f"INTRO shows the scene name ({info.part!r})")
    _assert(info.color == note_layout._SONG_PART_COLORS["INTRO"],
            f"INTRO uses the song-part palette colour ({info.color!r})")


def test_clip_info_multiple_candidates():
    print("test_clip_info_multiple_candidates")
    info = note_layout.song_part_clip_info(_ledbar(), "CHORUS")
    _assert(info is not None, "CHORUS resolves")
    # First candidate's name + ' +N' flagging the other alternatives — NOT the
    # part name. Colour is the song part's palette colour (shared by all CHORUS).
    first = note_layout.song_part_refs(_ledbar())["CHORUS"][0]
    expected = f"{note_layout._chase_name(_ledbar(), first.index)} +5"
    _assert(info.part == expected,
            f"multiple candidates show 'first +N' ({info.part!r})")
    _assert(info.color == note_layout._SONG_PART_COLORS["CHORUS"],
            f"multiple candidates use the song-part palette colour ({info.color!r})")


def test_clip_info_zero_and_none():
    print("test_clip_info_zero_and_none")
    _assert(note_layout.song_part_clip_info(_ledbar(), "BREAK") is None,
            "untagged part -> None (no clip drawn on that lane)")
    _assert(note_layout.song_part_clip_info(None, "INTRO") is None,
            "no fixture def -> None")


# --- Integration: the clips actually appear on the right lane --------------
@dataclass
class FakeFixture:
    type: str
    dmx_start: int
    channel: int
    name: str = ""


_APP = None  # keep the QApplication alive for the module's lifetime


def _build_timeline():
    global _APP
    from PyQt6.QtWidgets import QApplication
    from midi.manager import MidiManager
    from timeline.manager import Timeline
    from software_controller.main import DMXController
    _APP = QApplication.instance() or QApplication([])
    patch = [FakeFixture("ledbar", 1, 0),
             FakeFixture("blinder", 33, 1),
             FakeFixture("blinder", 65, 2)]
    m = MidiManager(note_layout.build_note_map(), fixtures=patch)
    m.create_empty_track()
    m.set_song_duration(240.0)
    tl = Timeline(midi_manager=m, fixtures=patch)
    ctrl = DMXController()  # __init__ only builds buffers; no threads/Art-Net
    m.set_software_controller(ctrl)
    return m, tl, ctrl


def _auto_clips(track):
    return [it for it in track.items if isinstance(it, AutoSceneClip)]


def test_auto_clips_only_on_auto_lane():
    print("test_auto_clips_only_on_auto_lane")
    m, tl, ctrl = _build_timeline()
    m.add_event(61, 1.0, 100)    # INTRO marker
    m.add_event(63, 10.0, 100)   # CHORUS marker  -> two sections
    ctrl.set_fixture_auto(0, True)   # ledbar (channel 0) opts in
    tl.refresh_from_manager()
    led, bl1, bl2 = tl.midi_events_tracks
    _assert(len(_auto_clips(led)) == 2, "ledbar (auto on) gets one clip per section")
    _assert(_auto_clips(bl1) == [] and _auto_clips(bl2) == [],
            "auto-off lanes get no auto clips")


def test_manual_clip_breaks_auto_clip():
    print("test_manual_clip_breaks_auto_clip")
    m, tl, ctrl = _build_timeline()
    m.add_event(61, 1.0, 100)    # INTRO at 1s; section runs to the song end
    ctrl.set_fixture_auto(0, True)
    tl.refresh_from_manager()
    _assert(len(_auto_clips(tl.midi_events_tracks[0])) == 1, "one section -> one clip")

    # A manual ledbar clip at 3s takes priority: with no later clip it holds to
    # the song end, so the auto-viz is truncated to [1s, 3s] (it never resumes).
    m.add_event(1, 3.0, 100, channel=0)
    tl.refresh_from_manager()
    led = tl.midi_events_tracks[0]
    clips = _auto_clips(led)
    pps = tl.PIXELS_PER_SECOND
    _assert(len(clips) == 1, "still one auto clip (broken, not removed)")
    c = clips[0]
    _assert(abs(c.pos().x() - 1.0 * pps) < 1e-6,
            "auto clip starts at the INTRO marker (1s)")
    _assert(abs(c.boundingRect().width() - 2.0 * pps) < 1e-6,
            "auto clip ends where the manual clip begins (3s)")


def test_song_part_marker_truncates_manual_clip():
    print("test_song_part_marker_truncates_manual_clip")
    from timeline.tracks.midi_events import DraggableMidiEvent
    m, tl, ctrl = _build_timeline()
    # A manual ledbar clip at 2s (extends to the song end), then a programmed
    # INTRO marker at 5s: the marker's auto scene takes over, so the manual clip
    # must end at 5s and the auto clip start there.
    m.add_event(1, 2.0, 100, channel=0)
    m.add_event(61, 5.0, 100)
    ctrl.set_fixture_auto(0, True)
    tl.refresh_from_manager()
    pps = tl.PIXELS_PER_SECOND
    led = tl.midi_events_tracks[0]
    manual = [it for it in led.items
              if isinstance(it, DraggableMidiEvent) and not it.is_blackout_clip]
    auto = _auto_clips(led)
    _assert(len(manual) == 1 and len(auto) == 1, "one manual + one auto clip")
    mc = manual[0]
    _assert(abs(mc.pos().x() - 2.0 * pps) < 1e-6, "manual clip starts at 2s")
    _assert(abs(mc.boundingRect().width() - 3.0 * pps) < 1e-6,
            "manual clip is truncated at the 5s marker (width 3s)")
    _assert(abs(auto[0].pos().x() - 5.0 * pps) < 1e-6,
            "auto clip starts at the marker (5s)")


def test_unprogrammed_marker_also_truncates_manual_clip():
    print("test_unprogrammed_marker_also_truncates_manual_clip")
    from timeline.tracks.midi_events import DraggableMidiEvent
    m, tl, ctrl = _build_timeline()
    # The ledbar is managed (has a program) but is NOT programmed for BREAK.
    # At the BREAK marker auto mode still blacks it out, so the manual clip is
    # cut there too — but no auto clip is drawn (the section is dark).
    m.add_event(1, 2.0, 100, channel=0)
    m.add_event(64, 5.0, 100)   # BREAK marker; ledbar has no scene for it
    ctrl.set_fixture_auto(0, True)
    tl.refresh_from_manager()
    pps = tl.PIXELS_PER_SECOND
    led = tl.midi_events_tracks[0]
    manual = [it for it in led.items
              if isinstance(it, DraggableMidiEvent) and not it.is_blackout_clip]
    _assert(len(manual) == 1 and abs(manual[0].boundingRect().width() - 3.0 * pps) < 1e-6,
            "manual clip is cut at the BREAK marker even though it's unprogrammed")
    _assert(_auto_clips(led) == [], "no auto clip for the unprogrammed BREAK")


def test_only_programmed_parts_draw():
    print("test_only_programmed_parts_draw")
    m, tl, ctrl = _build_timeline()
    m.add_event(61, 1.0, 100)    # INTRO  (blinder NOT programmed for it)
    m.add_event(64, 10.0, 100)   # BREAK  (blinder IS programmed for it)
    ctrl.set_fixture_auto(1, True)   # the first blinder (channel 1) opts in
    tl.refresh_from_manager()
    blinder = tl.midi_events_tracks[1]
    clips = _auto_clips(blinder)
    _assert(len(clips) == 1, "only the BREAK section draws (INTRO isn't programmed)")
    _assert(abs(clips[0].pos().x() - 10.0 * tl.PIXELS_PER_SECOND) < 1e-6,
            "the lone clip sits in the BREAK section, not the INTRO one")


def test_auto_off_clears_clips():
    print("test_auto_off_clears_clips")
    m, tl, ctrl = _build_timeline()
    m.add_event(61, 1.0, 100)
    ctrl.set_fixture_auto(0, True)
    tl.refresh_from_manager()
    _assert(len(_auto_clips(tl.midi_events_tracks[0])) == 1, "clip present while auto on")
    ctrl.set_fixture_auto(0, False)
    tl.refresh_from_manager()
    _assert(_auto_clips(tl.midi_events_tracks[0]) == [], "clips cleared when auto off")


def main():
    test_song_part_refs_index()
    test_clip_info_single_candidate()
    test_clip_info_multiple_candidates()
    test_clip_info_zero_and_none()
    test_auto_clips_only_on_auto_lane()
    test_manual_clip_breaks_auto_clip()
    test_song_part_marker_truncates_manual_clip()
    test_unprogrammed_marker_also_truncates_manual_clip()
    test_only_programmed_parts_draw()
    test_auto_off_clears_clips()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
