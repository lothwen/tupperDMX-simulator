"""
Characterization test: the show follows the audio when the offset moves.

The waveform drag edits `song.offset`, which slides the AUDIO alone — every
clip, song part and marker is anchored in timeline seconds. This locks the
ripple that keeps them together:

  * the waveform drag commits ONCE, on release, carrying where it started;
  * `MidiManager.shift_all` slides the whole sequence rigidly, as one undo;
  * `core.markers.shift_markers` does the same for the marker track;
  * both refuse (never clamp) a shift that would cross t=0, and MainWindow
    checks both halves before moving either, so they cannot desync;
  * backing and guide drags are both offered it, each offering to carry the
    other track by the DELTA (never by its offset);
  * Cancel puts the offset back and writes nothing;
  * a drag in progress touches nothing but the SETLIST chip;
  * Advanced overrides the offset the drag landed on, and everything that
    follows — headline, checkbox, the show shift — follows the typed value;
  * Advanced also takes the move in beats, and the two fields write each other;
  * a show that cannot follow names the item in the way and the lowest offset
    it can follow to;
  * every figure the dialog shows is an ABSOLUTE offset written `from -> to`,
    the same number that lands in show.json, never a relative nudge.

Run with:
    uv run python tests/test_offset_ripple.py
"""

import sys
import os
import tempfile
from dataclasses import dataclass

# Headless Qt for standalone runs (pytest sets this via conftest.py).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QWidget

from core import markers as core_markers
from core import note_layout
from core.config import ControlMarkerConfig, SongConfig, VoiceTTSConfig
from midi.manager import MidiManager


@dataclass
class FakeFixture:
    type: str
    dmx_start: int
    channel: int
    name: str = ""


PATCH = [FakeFixture("ledbar", 1, 0), FakeFixture("blinder", 33, 1)]
LEDBAR_CH, BLINDER_CH = 0, 1
LEDBAR_CLIP, BLINDER_CLIP = 1, 26
INTRO, CHORUS = 61, 63


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _close(a, b, tol=1e-6):
    return abs(a - b) <= tol


_APP = None  # module-level ref so the QApplication isn't garbage-collected


def _ensure_app():
    global _APP
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _make_midi():
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    m.set_song_duration(240.0)
    return m


def _times(m):
    """{(note, channel): time} for every non-BLACKOUT event."""
    return {(e["note"], e.get("channel")): e["time"]
            for e in m.midi_events if not m._doc.is_blackout(e)}


def _song():
    return SongConfig(
        audio="/tmp/x.mp3", id="s1",
        voice_tts=[VoiceTTSConfig(time=4.0, text="entra guitarra")],
        control_markers=[ControlMarkerConfig(time=8.0, kind=core_markers.KIND_TEMPO,
                                             bpm=140),
                         ControlMarkerConfig(time=30.0,
                                             kind=core_markers.KIND_CLICK_STOP)],
        segue=True, segue_time=100.0)


# ----------------------------------------------------------------------
# The sequence half
# ----------------------------------------------------------------------
def test_shift_all_is_rigid():
    print("test_shift_all_is_rigid")
    m = _make_midi()
    m.add_event(LEDBAR_CLIP, 2.0, 100, channel=LEDBAR_CH)
    m.add_event(BLINDER_CLIP, 5.0, 100, channel=BLINDER_CH)
    m.add_event(INTRO, 2.0, 100)
    before = _times(m)
    _assert(m.shift_all(1.5), "shift_all reports the move")
    after = _times(m)
    _assert(set(before) == set(after), "no event gained or lost")
    _assert(all(_close(after[k], before[k] + 1.5) for k in before),
            "every event moved by exactly the delta")
    _assert(_close(m.earliest_event_time(), 3.5), "earliest_event_time follows")


def test_shift_all_moves_the_mirrored_notes():
    print("test_shift_all_moves_the_mirrored_notes")
    m = _make_midi()
    m.add_event(LEDBAR_CLIP, 2.0, 100, channel=LEDBAR_CH)
    notes_before = sorted(n["time"] for n in m.midi_notes)
    m.shift_all(2.0)
    notes_after = sorted(n["time"] for n in m.midi_notes)
    _assert(len(notes_before) == len(notes_after), "note count unchanged")
    _assert(all(_close(a, b + 2.0) for a, b in zip(notes_after, notes_before)),
            "the notes the .mid is rebuilt from moved too")


def test_shift_all_leaves_the_setup_notes_at_zero():
    print("test_shift_all_leaves_the_setup_notes_at_zero")
    m = _make_midi()
    m.add_event(LEDBAR_CLIP, 3.0, 100, channel=LEDBAR_CH)
    m.set_fixture_auto(LEDBAR_CH, True)
    m.set_fixture_auto(BLINDER_CH, True)

    def _setup_times():
        return [n["time"] for n in m.midi_notes
                if n["note"] == note_layout.FIXTURE_SETUP]

    _assert(_setup_times() == [0.0, 0.0], "both setup notes start at t=0")
    _assert(m.shift_all(-1.0), "a leftward shift the clips can take is allowed")
    _assert(_setup_times() == [0.0, 0.0],
            "the setup notes stayed at t=0 (they are timeless persistence)")
    _assert(_close(m.earliest_event_time(), 2.0), "the clip still moved")
    _assert(m.shift_all(2.0), "a rightward shift too")
    _assert(_setup_times() == [0.0, 0.0], "and they stay put that way as well")


def test_a_leftward_shift_still_saves():
    """The bug the exclusion fixes: a setup note dragged below t=0 makes
    `rebuild` write a negative delta tick, which only blows up in
    `MidiFile.save` — so the export failed and the edit was lost from disk."""
    print("test_a_leftward_shift_still_saves")
    m = _make_midi()
    m.add_event(LEDBAR_CLIP, 3.0, 100, channel=LEDBAR_CH)
    m.set_fixture_auto(LEDBAR_CH, True)
    _assert(m.shift_all(-1.0), "the show follows the audio left")
    out = os.path.join(tempfile.gettempdir(), "tupper_shift_save.mid")
    try:
        _assert(m.export_to_file(out), "the .mid still exports")
        _assert(os.path.exists(out), "and lands on disk")
    finally:
        if os.path.exists(out):
            os.remove(out)


def test_shift_all_refuses_across_zero():
    print("test_shift_all_refuses_across_zero")
    m = _make_midi()
    m.add_event(LEDBAR_CLIP, 1.0, 100, channel=LEDBAR_CH)
    before = _times(m)
    _assert(not m.shift_all(-2.0), "a shift crossing t=0 is refused")
    _assert(_times(m) == before, "and nothing moved (no partial clamp)")
    _assert(m.shift_all(-1.0), "a shift landing exactly on t=0 is allowed")
    _assert(_close(m.earliest_event_time(), 0.0), "first clip sits at 0")


def test_a_blackout_never_blocks_the_shift():
    """A BLACKOUT is a gap marker — dark, not content — so one sitting before
    the first clip does not hold the show back, and one a leftward shift pushes
    before t=0 is dropped rather than refusing the move."""
    print("test_a_blackout_never_blocks_the_shift")
    m = _make_midi()
    m.add_event(LEDBAR_CLIP, 1.0, 100, channel=LEDBAR_CH)
    m.add_event(LEDBAR_CLIP, 3.0, 100, channel=LEDBAR_CH)
    m.add_event(BLINDER_CLIP, 2.0, 100, channel=BLINDER_CH)
    # Deleting the first clip leaves a BLACKOUT at 1.0 on the ledbar.
    m.replace_with_blackout(min(m.midi_events, key=lambda e: e["time"])["id"])

    def _blackouts():
        return [e["time"] for e in m.midi_events if m.is_blackout(e)]

    _assert(_blackouts() == [1.0], "the ledbar has a BLACKOUT at 1.0")
    _assert(_close(m.earliest_event_time(), 2.0),
            "the show starts at the first clip, not at the BLACKOUT")
    _assert(m.shift_all(-0.5), "a shift the BLACKOUT can take")
    _assert(_blackouts() == [0.5], "moves it with the rest")
    _assert(m.shift_all(-1.5), "a shift past the BLACKOUT is not refused")
    _assert(_times(m) == {(LEDBAR_CLIP, LEDBAR_CH): 1.0,
                          (BLINDER_CLIP, BLINDER_CH): 0.0},
            "every clip moved by the delta")
    _assert(_blackouts() == [], "the BLACKOUT pushed before t=0 was dropped")
    _assert(all(n["time"] >= 0.0 for n in m.midi_notes),
            "and so was its note, so the .mid never gets a negative time")


def test_shift_all_is_one_undo_step():
    print("test_shift_all_is_one_undo_step")
    m = _make_midi()
    m.add_event(LEDBAR_CLIP, 2.0, 100, channel=LEDBAR_CH)
    m.add_event(BLINDER_CLIP, 5.0, 100, channel=BLINDER_CH)
    before = _times(m)
    m.shift_all(3.0)
    _assert(m.can_undo(), "the shift is undoable")
    _assert(m.undo(), "undo runs")
    _assert(_times(m) == before, "one undo puts the whole show back")


def test_shift_all_empty_document():
    print("test_shift_all_empty_document")
    m = _make_midi()
    _assert(m.earliest_event_time() is None, "an empty document has no start")
    _assert(not m.shift_all(1.0), "and nothing to shift")


# ----------------------------------------------------------------------
# The marker half
# ----------------------------------------------------------------------
def test_earliest_marker_time():
    print("test_earliest_marker_time")
    song = _song()
    _assert(_close(core_markers.earliest_marker_time(song), 4.0),
            "the earliest of cues, control markers and segue")
    _assert(core_markers.earliest_marker_time(SongConfig()) is None,
            "a song with no markers has no start")
    # segue_time is kept while segue is off, but its 0.0 default is not a marker
    _assert(core_markers.earliest_marker_time(
        SongConfig(segue=False, segue_time=0.0)) is None,
        "an undialled segue does not count as a marker at 0")


def test_shift_markers_moves_every_kind():
    print("test_shift_markers_moves_every_kind")
    song = _song()
    _assert(core_markers.shift_markers(song, 2.5), "shift reports the move")
    _assert(_close(song.voice_tts[0].time, 6.5), "voice cue moved")
    _assert(_close(song.control_markers[0].time, 10.5), "tempo marker moved")
    _assert(song.control_markers[0].bpm == 140, "its tempo is untouched")
    _assert(_close(song.control_markers[1].time, 32.5), "stop-click moved")
    _assert(_close(song.segue_time, 102.5), "segue moved")


def test_shift_markers_refuses_across_zero():
    print("test_shift_markers_refuses_across_zero")
    song = _song()
    _assert(not core_markers.shift_markers(song, -10.0),
            "a shift crossing t=0 is refused")
    _assert(_close(song.voice_tts[0].time, 4.0), "and nothing moved")


def test_shift_markers_leaves_an_undialled_segue_alone():
    print("test_shift_markers_leaves_an_undialled_segue_alone")
    song = SongConfig(voice_tts=[VoiceTTSConfig(time=5.0, text="hola")])
    core_markers.shift_markers(song, 1.0)
    _assert(_close(song.voice_tts[0].time, 6.0), "the cue moved")
    _assert(_close(song.segue_time, 0.0),
            "a segue that was never dialled stays at 0")


# ----------------------------------------------------------------------
# The window half: what follows the track that moved
# ----------------------------------------------------------------------
# MainWindow's ripple methods are exercised on a stand-in carrying only the
# collaborators they touch — building a real window would drag in the stage
# engine, the audio device and the web remote. It IS a QWidget, so the real
# QMessageBox can be parented to it and its composition tested for real.
RIPPLE_METHODS = ("_show_start", "_describe_event",
                  "move_show_with_audio", "on_waveform_offset_committed",
                  "on_waveform_offset_changed", "_ask_offset_ripple",
                  "_monitoring_guide", "_other_track",
                  "_move_other_track")


class FakeMediaController:
    """Monitor selection + the two tracks and their offsets."""

    def __init__(self, audio_file="/tmp/backing.wav", guide_file=None,
                 monitor=None):
        from app.media_controller import MONITOR_BACKING
        self.audio_file = audio_file
        self.guide_file = guide_file
        self.monitor = monitor or MONITOR_BACKING
        self.audio_offset_sec = 0.0
        self.guide_offset_sec = 0.0

    def set_audio_offset(self, offset):
        self.audio_offset_sec = offset

    def set_guide_offset(self, offset):
        self.guide_offset_sec = offset


class FakeToolbar:
    """Records what the SETLIST footer's offset chip was told to show."""

    def __init__(self):
        self.offsets = []

    def update_offset_display(self, offset, has_song=True):
        self.offsets.append(offset if has_song else None)


class FakeTimeline:
    """Records the revert Cancel drives through set_waveform_offset."""

    def __init__(self):
        self.offsets = []

    def set_waveform_offset(self, offset):
        self.offsets.append(offset)


class DialogDriver:
    """Answers the REAL OffsetRippleDialog by driving its widgets, recording
    what it offered — so the dialog's composition is under test too, not just
    the branch that follows it."""

    def __init__(self, pick, tick_checkbox=False, override=None, beats=None):
        self.pick = pick                 # button text, or None to close it
        self.tick_checkbox = tick_checkbox
        self.override = override         # an absolute offset typed in Advanced
        self.beats = beats               # or a move in beats typed there
        self.seen = []                   # as opened
        self.driven = []                 # after the override / tick

    def __enter__(self):
        from ui.offset_dialog import OffsetRippleDialog
        driver = self
        self._original = OffsetRippleDialog.exec

        def _snapshot(dialog):
            buttons = dialog.button_box.buttons()
            return {
                "headline": dialog._headline.text(),
                "question": (dialog._question.text()
                             if dialog._question.isVisibleTo(dialog) else ""),
                "buttons": [b.text().replace("&", "") for b in buttons],
                "enabled": {b.text().replace("&", ""): b.isEnabled()
                            for b in buttons},
                "checkbox": (dialog._other_check.text()
                             if dialog._other_check is not None else None),
                "checked": (dialog._other_check.isChecked()
                            if dialog._other_check is not None else None),
                "advanced_open": dialog.advanced.isChecked(),
                "spin": dialog.offset_spin.value(),
                "beats": dialog.beats_spin.value(),
            }

        def _exec(dialog):
            # Snapshot the untouched dialog first, so the defaults it opens
            # with stay assertable, then again after driving it.
            driver.seen.append(_snapshot(dialog))
            if driver.override is not None:
                dialog.advanced.setChecked(True)
                dialog.offset_spin.setValue(driver.override)
            if driver.beats is not None:
                dialog.advanced.setChecked(True)
                dialog.beats_spin.setValue(driver.beats)
            if driver.tick_checkbox and dialog._other_check is not None:
                dialog._other_check.setChecked(True)
            driver.driven.append(_snapshot(dialog))
            for button in dialog.button_box.buttons():
                if button.text().replace("&", "") == driver.pick:
                    button.click()
                    break
            return 0

        OffsetRippleDialog.exec = _exec
        return self

    def __exit__(self, *exc):
        from ui.offset_dialog import OffsetRippleDialog
        OffsetRippleDialog.exec = self._original
        return False

    @property
    def dialog(self):
        """The dialog as it opened, before the driver touched anything."""
        _assert(len(self.seen) == 1, f"exactly one dialog (got {len(self.seen)})")
        return self.seen[0]

    @property
    def after(self):
        """The dialog once the override and checkbox have been applied."""
        _assert(len(self.driven) == 1, f"exactly one dialog (got {len(self.driven)})")
        return self.driven[0]


class FakeWindow(QWidget):
    """The collaborators MainWindow's ripple methods actually touch, with the
    real methods bound on so they call each other for real."""

    def __init__(self, midi_manager, song, media_controller=None, bpm=120):
        import logging
        from app.bpm import BpmState
        from ui.main_window import MainWindow
        _ensure_app()
        super().__init__()
        self.midi_manager = midi_manager
        self.bpm_state = BpmState(bpm)
        self.fixtures = PATCH
        self.media_controller = media_controller or FakeMediaController()
        self.toolbar_manager = FakeToolbar()
        self.timeline = FakeTimeline()
        self._song = song
        self.config = self
        self.logger = logging.getLogger("test")
        self.saved = 0
        self.applied = []
        for name in RIPPLE_METHODS:
            setattr(self, name, getattr(MainWindow, name).__get__(self))

    # config half
    def get_current_song(self):
        return self._song

    def update_current_song(self, **kwargs):
        for field, value in kwargs.items():
            if value is not None:
                setattr(self._song, field, value)

    # window half
    def save_config(self):
        self.saved += 1

    def apply_marker_edits(self, song):
        self.applied.append(song)


def _window(song=None, media_controller=None):
    m = _make_midi()
    m.add_event(LEDBAR_CLIP, 2.0, 100, channel=LEDBAR_CH)
    m.add_event(INTRO, 2.0, 100)
    return FakeWindow(m, song if song is not None else _song(),
                      media_controller=media_controller), m


def _with_guide(monitor=None):
    from app.media_controller import MONITOR_GUIDE
    return FakeMediaController(guide_file="/tmp/guide.wav",
                               monitor=monitor or MONITOR_GUIDE)


def test_show_start_spans_clips_and_markers():
    print("test_show_start_spans_clips_and_markers")
    w, m = _window()
    # Clips start at 2.0, the earliest marker at 4.0.
    time, label = w._show_start()
    _assert(_close(time, 2.0), "the show starts at the earlier of the two halves")
    part = m.midi_events[0]["info"].part
    _assert(label == f"clip “{part}” on ledbar (MIDI channel 0)",
            f"and names the clip, its fixture and its channel ({label!r})")
    w._song = SongConfig(voice_tts=[VoiceTTSConfig(time=0.5, text="ya")])
    time, label = w._show_start()
    _assert(_close(time, 0.5), "a marker before the first clip wins")
    _assert(label == "Voice TTS marker “ya”", f"and is named ({label!r})")
    _assert(FakeWindow(_make_midi(), SongConfig())._show_start() is None,
            "an empty song has no start")


def test_every_kind_of_first_item_is_named():
    print("test_every_kind_of_first_item_is_named")
    m = _make_midi()
    m.add_event(INTRO, 1.0, 100)
    w = FakeWindow(m, SongConfig())
    _assert(w._show_start()[1] == "song part “INTRO”",
            f"a song part ({w._show_start()[1]!r})")
    m.add_scene(0.5, {"brightness": 255, "blue": 255, "green": 0, "red": 0,
                      "animation": 0, "speed": 0, "direction": 0},
                "Cold open", channel=LEDBAR_CH)
    _assert(w._show_start()[1] == "scene “Cold open” on ledbar (MIDI channel 0)",
            f"a scene, by its scene name ({w._show_start()[1]!r})")
    for song, expected in (
            (SongConfig(control_markers=[ControlMarkerConfig(
                time=0.2, kind=core_markers.KIND_TEMPO, bpm=96)]),
             "Tempo change marker (96 BPM)"),
            (SongConfig(control_markers=[ControlMarkerConfig(
                time=0.2, kind=core_markers.KIND_CLICK_STOP)]),
             "Stop click marker"),
            (SongConfig(segue=True, segue_time=0.2), "Segue marker")):
        w._song = song
        _assert(w._show_start()[1] == expected,
                f"a marker of each kind ({w._show_start()[1]!r})")


def test_move_show_moves_both_halves():
    print("test_move_show_moves_both_halves")
    w, m = _window()
    w.move_show_with_audio(1.25)
    _assert(_close(m.earliest_event_time(), 3.25), "the sequence moved")
    _assert(_close(w._song.voice_tts[0].time, 5.25), "the marker track moved")
    _assert(w.applied == [w._song],
            "the marker re-apply path ran (ruler, tempo map, TTS)")


def test_move_show_with_no_markers_still_saves():
    print("test_move_show_with_no_markers_still_saves")
    w, m = _window(song=SongConfig())
    w.move_show_with_audio(1.0)
    _assert(_close(m.earliest_event_time(), 3.0), "the sequence moved")
    _assert(not w.applied, "no marker re-apply for a song with no markers")
    _assert(w.saved == 1, "the show was saved once")


def test_a_backing_drag_offers_the_show_and_the_guide():
    print("test_a_backing_drag_offers_the_show_and_the_guide")
    from app.media_controller import MONITOR_BACKING
    w, m = _window(media_controller=_with_guide(monitor=MONITOR_BACKING))
    with DialogDriver("Move show", tick_checkbox=True) as driver:
        w.on_waveform_offset_committed(0.0, 1.75)
    dialog = driver.dialog
    _assert("Backing track" in dialog["headline"]
            and "+0.000" in dialog["headline"] and "+1.750" in dialog["headline"],
            f"the headline names the track and reads from -> to, both absolute "
            f"({dialog['headline']!r})")
    _assert(dialog["buttons"] == ["Move show", "Audio only", "Cancel"],
            f"all three choices are offered (got {dialog['buttons']})")
    _assert(dialog["checkbox"] and "guide track" in dialog["checkbox"],
            f"the checkbox offers the OTHER track ({dialog['checkbox']!r})")
    _assert("+0.000" in dialog["checkbox"] and "+1.750" in dialog["checkbox"],
            "and quotes where that track LANDS, not how far it shifts")
    _assert(dialog["checked"] is False,
            "unchecked by default — the two files have independent lead-ins")

    _assert(_close(w.media_controller.audio_offset_sec, 1.75)
            and _close(w._song.offset, 1.75), "the backing offset persisted")
    _assert(_close(m.earliest_event_time(), 3.75), "the show followed")
    _assert(_close(w.media_controller.guide_offset_sec, 1.75)
            and _close(w._song.guide_offset, 1.75), "and so did the guide")


def test_a_guide_drag_offers_the_show_too():
    print("test_a_guide_drag_offers_the_show_too")
    mc = _with_guide()
    w, m = _window(media_controller=mc)
    with DialogDriver("Move show") as driver:
        w.on_waveform_offset_committed(0.0, 2.0)
    _assert("Guide track" in driver.dialog["headline"],
            f"the dialog names the guide ({driver.dialog['headline']!r})")
    _assert(driver.dialog["checkbox"] and "backing track" in driver.dialog["checkbox"],
            "and offers the backing as the other track")
    _assert(_close(mc.guide_offset_sec, 2.0) and _close(w._song.guide_offset, 2.0),
            "the guide offset persisted")
    _assert(_close(m.earliest_event_time(), 4.0),
            "the show follows the guide too — it is the reference you design against")
    _assert(_close(mc.audio_offset_sec, 0.0) and _close(w._song.offset, 0.0),
            "the backing stayed put with the checkbox unticked")
    _assert(w.toolbar_manager.offsets == [],
            "the SETLIST chip shows the BACKING offset, so it is untouched")


def test_the_other_track_moves_by_the_correction_not_the_offset():
    print("test_the_other_track_moves_by_the_correction_not_the_offset")
    from app.media_controller import MONITOR_BACKING
    mc = _with_guide(monitor=MONITOR_BACKING)
    mc.guide_offset_sec = 5.0        # its own lead-in, nothing to do with ours
    w, m = _window(media_controller=mc)
    with DialogDriver("Audio only", tick_checkbox=True):
        w.on_waveform_offset_committed(1.0, 3.0)
    _assert(_close(mc.audio_offset_sec, 3.0), "the dragged track took the offset")
    _assert(_close(mc.guide_offset_sec, 7.0),
            "the other took the DELTA on top of its own offset (5.0 + 2.0)")


def test_audio_only_keeps_the_offset_and_moves_nothing():
    print("test_audio_only_keeps_the_offset_and_moves_nothing")
    w, m = _window()
    with DialogDriver("Audio only"):
        w.on_waveform_offset_committed(0.0, 1.75)
    _assert(_close(w._song.offset, 1.75), "the offset is still persisted")
    _assert(w.toolbar_manager.offsets[-1] == 1.75, "and shown on the chip")
    _assert(_close(m.earliest_event_time(), 2.0), "the sequence stayed put")
    _assert(_close(w._song.voice_tts[0].time, 4.0), "so did the marker track")
    _assert(w.saved == 1, "the offset was saved")


def test_a_drag_in_progress_touches_nothing_but_the_chip():
    print("test_a_drag_in_progress_touches_nothing_but_the_chip")
    from app.media_controller import MONITOR_BACKING
    mc = _with_guide(monitor=MONITOR_BACKING)
    w, m = _window(media_controller=mc)
    for frame in (0.4, 0.9, 1.6):        # the preview, once per mouse-move
        w.on_waveform_offset_changed(frame)
    _assert(w.toolbar_manager.offsets == [0.4, 0.9, 1.6],
            "the chip tracks the drag frame by frame")
    _assert(_close(mc.audio_offset_sec, 0.0) and _close(mc.guide_offset_sec, 0.0),
            "but no controller offset moved — a drag is a proposal, and per-frame "
            "work must stay out of the audio path")
    _assert(_close(w._song.offset, 0.0) and w.saved == 0, "and nothing was written")


def test_a_guide_drag_in_progress_leaves_the_chip_alone():
    print("test_a_guide_drag_in_progress_leaves_the_chip_alone")
    w, m = _window(media_controller=_with_guide())
    w.on_waveform_offset_changed(1.5)
    _assert(w.toolbar_manager.offsets == [],
            "the chip shows the BACKING offset, so a guide drag does not touch it")


def test_cancel_puts_the_offset_back_and_writes_nothing():
    print("test_cancel_puts_the_offset_back_and_writes_nothing")
    w, m = _window(media_controller=_with_guide(monitor=None))
    w.media_controller.audio_offset_sec = 0.5   # where the drag started
    with DialogDriver("Cancel", tick_checkbox=True):
        w.on_waveform_offset_committed(0.5, 4.0)
    _assert(w.timeline.offsets == [0.5],
            "the waveform is put back where the drag started")
    _assert(_close(w._song.offset, 0.0),
            "nothing was persisted — the drag only moved it in memory")
    _assert(_close(m.earliest_event_time(), 2.0), "the show did not move")
    _assert(_close(w.media_controller.guide_offset_sec, 0.0),
            "and a ticked checkbox is discarded along with everything else")
    _assert(w.saved == 0, "no save at all")


def test_closing_the_dialog_is_a_cancel():
    print("test_closing_the_dialog_is_a_cancel")
    w, m = _window()
    with DialogDriver(None):     # no button clicked, as closing the window does
        w.on_waveform_offset_committed(0.0, 4.0)
    _assert(w.timeline.offsets == [0.0], "the offset was put back")
    _assert(w.saved == 0, "and nothing written")


def test_a_show_that_cannot_follow_disables_the_move_button():
    print("test_a_show_that_cannot_follow_disables_the_move_button")
    w, m = _window()
    # The show starts at 2.0, so -3.0 would push it before the timeline. The
    # button is disabled rather than absent: Advanced can retype the offset
    # into a legal one without the dialog changing shape underneath.
    with DialogDriver("Audio only") as driver:
        w.on_waveform_offset_committed(3.0, 0.0)
    dialog = driver.dialog
    _assert(dialog["enabled"]["Move show"] is False,
            "'Move show' is disabled")
    _assert("cannot follow" in dialog["question"]
            and "2.000" in dialog["question"],
            f"the reason replaces the question ({dialog['question']!r})")
    _assert(_close(w._song.offset, 0.0), "the offset is still kept")
    _assert(_close(m.earliest_event_time(), 2.0),
            "and the show was not partially shifted")


def test_every_figure_on_the_dialog_is_an_absolute_offset():
    print("test_every_figure_on_the_dialog_is_an_absolute_offset")
    from app.media_controller import MONITOR_BACKING
    mc = _with_guide(monitor=MONITOR_BACKING)
    mc.guide_offset_sec = 5.0        # the other track's own, unrelated lead-in
    w, m = _window(media_controller=mc)
    with DialogDriver("Audio only") as driver:
        w.on_waveform_offset_committed(1.0, 3.0)     # a +2.0 correction
    dialog = driver.dialog
    _assert("+1.000" in dialog["headline"] and "+3.000" in dialog["headline"],
            f"the headline is where the track was and where it goes "
            f"({dialog['headline']!r})")
    _assert("+2.000" not in dialog["headline"],
            "and never the distance travelled")
    _assert("+5.000" in dialog["checkbox"] and "+7.000" in dialog["checkbox"],
            f"the checkbox is where the OTHER track sits and lands "
            f"({dialog['checkbox']!r})")
    _assert("+2.000" not in dialog["checkbox"],
            "again not the distance — the two figures are what show.json stores")


def test_a_show_that_cannot_follow_is_told_in_absolutes_too():
    print("test_a_show_that_cannot_follow_is_told_in_absolutes_too")
    w, m = _window()
    with DialogDriver("Audio only") as driver:
        w.on_waveform_offset_committed(3.0, 0.0)     # -3.0 against a show at 2.0
    question = driver.dialog["question"]
    _assert("+2.000" in question,
            f"it says where the show starts now ({question!r})")
    _assert("-1.000" in question,
            "and where this offset would start it, not by how much it slips")


def test_a_show_that_cannot_follow_names_what_is_in_the_way():
    print("test_a_show_that_cannot_follow_names_what_is_in_the_way")
    w, m = _window()
    with DialogDriver("Audio only") as driver:
        w.on_waveform_offset_committed(3.0, 0.0)     # -3.0 against a show at 2.0
    question = driver.dialog["question"]
    part = m.midi_events[0]["info"].part
    _assert(f"clip “{part}” on ledbar (MIDI channel 0)" in question,
            f"it names the clip in the way ({question!r})")
    _assert("+2.000" in question and "-1.000" in question,
            "where that clip starts and where it would land")
    _assert("down to an offset of +1.000 s" in question,
            "and the lowest offset the show can still follow to")


def test_a_blackout_is_never_in_the_way():
    print("test_a_blackout_is_never_in_the_way")
    w, m = _window(song=SongConfig())                 # clips at 2.0
    m.add_event(BLINDER_CLIP, 0.5, 100, channel=BLINDER_CH)
    m.replace_with_blackout(min(m.midi_events, key=lambda e: e["time"])["id"])
    _assert(_close(w._show_start()[0], 2.0),
            "the show's start skips the BLACKOUT at 0.5")
    with DialogDriver("Move show") as driver:
        w.on_waveform_offset_committed(3.0, 1.5)     # -1.5: the BLACKOUT goes <0
    _assert(driver.dialog["enabled"]["Move show"] is True,
            f"'Move show' is offered ({driver.dialog['question']!r})")
    _assert(_close(m.earliest_event_time(), 0.5), "the show moved by -1.5")
    _assert(not any(m.is_blackout(e) for e in m.midi_events),
            "and the BLACKOUT it pushed before t=0 is gone")


def test_a_marker_in_the_way_is_named_too():
    print("test_a_marker_in_the_way_is_named_too")
    song = SongConfig(voice_tts=[VoiceTTSConfig(time=0.5, text="ya")])
    w, m = _window(song=song)
    with DialogDriver("Audio only") as driver:
        w.on_waveform_offset_committed(1.0, 0.0)     # -1.0 against a cue at 0.5
    question = driver.dialog["question"]
    _assert("Voice TTS marker “ya”" in question,
            f"the voice cue is what is in the way, not the clip ({question!r})")
    _assert("+0.500" in question and "-0.500" in question
            and "down to an offset of +0.500 s" in question,
            "with its times and the lowest offset")


def test_the_lowest_offset_quoted_can_be_typed_back():
    print("test_the_lowest_offset_quoted_can_be_typed_back")
    _ensure_app()
    from ui.offset_dialog import OffsetRippleDialog
    # An offset from an earlier drag carries more than millisecond precision.
    dialog = OffsetRippleDialog(None, moved_label="backing track",
                                other_label=None, old_offset=0.1234567,
                                new_offset=-3.0, show_start=2.0,
                                show_start_item="clip “X”", bpm=120)
    _assert(_close(dialog.lowest_offset(), -1.876),
            f"rounded UP to the millisecond ({dialog.lowest_offset()})")
    dialog.advanced.setChecked(True)
    dialog.offset_spin.setValue(dialog.lowest_offset())
    _assert(dialog.can_move_show(), "typing the quoted figure is accepted")
    dialog.offset_spin.setValue(dialog.lowest_offset() - 0.001)
    _assert(not dialog.can_move_show(), "a millisecond lower is refused")


def test_a_blocked_move_is_marked_not_just_disabled():
    """Under the theme a disabled dialog button looks enabled (its colour rule
    wins), so a blocked move carries the `state` the QSS turns red, a warning
    glyph and a tooltip — and drops all three once the offset is legal again."""
    print("test_a_blocked_move_is_marked_not_just_disabled")
    _ensure_app()
    from ui.offset_dialog import OffsetRippleDialog
    dialog = OffsetRippleDialog(None, moved_label="backing track",
                                other_label=None, old_offset=3.0,
                                new_offset=0.0, show_start=2.0,
                                show_start_item="clip “X” & <co>", bpm=120)
    button, question = dialog._move_button, dialog._question
    _assert(not button.isEnabled(), "the move is disabled")
    _assert(button.property("state") == "blocked"
            and question.property("state") == "blocked",
            "the button and the reason are both marked blocked")
    _assert(not button.icon().isNull(), "the button carries the warning glyph")
    _assert("+1.000 s" in button.toolTip(),
            f"and its tooltip says how far it can go ({button.toolTip()!r})")
    _assert("<img" in question.text() and "clip “X” &amp; &lt;co&gt;" in question.text(),
            "the reason is marked too, with the item name escaped for rich text")

    dialog.advanced.setChecked(True)
    dialog.offset_spin.setValue(2.5)
    _assert(button.isEnabled(), "a legal offset re-enables it")
    _assert(button.property("state") == "" and question.property("state") == "",
            "and clears the blocked state")
    _assert(button.icon().isNull() and button.toolTip() == "",
            "the glyph and the tooltip go with it")


def test_advanced_overrides_the_offset_the_drag_landed_on():
    print("test_advanced_overrides_the_offset_the_drag_landed_on")
    from app.media_controller import MONITOR_BACKING
    mc = _with_guide(monitor=MONITOR_BACKING)
    w, m = _window(media_controller=mc)
    # Dragged to 1.75, but the user knows the real figure is 0.500.
    with DialogDriver("Move show", tick_checkbox=True, override=0.5) as driver:
        w.on_waveform_offset_committed(0.0, 1.75)
    _assert(driver.dialog["advanced_open"] is False,
            "Advanced starts collapsed — three decimals is the exception")
    _assert(_close(driver.dialog["spin"], 1.75),
            "and starts on the offset the drag landed on")
    _assert("+0.500" in driver.after["headline"],
            f"the headline follows the typed value ({driver.after['headline']!r})")
    _assert("+0.500" in driver.after["checkbox"],
            f"and so does the checkbox — the guide lands at its own 0.000 plus "
            f"the 0.500 correction ({driver.after['checkbox']!r})")

    _assert(_close(w._song.offset, 0.5) and _close(mc.audio_offset_sec, 0.5),
            "the typed offset is what gets persisted, not the dragged one")
    _assert(w.timeline.offsets == [0.5],
            "and the waveform is moved to agree with it")
    _assert(_close(m.earliest_event_time(), 2.5),
            "the show followed the typed delta (+0.5), not the drag (+1.75)")
    _assert(_close(mc.guide_offset_sec, 0.5), "and so did the other track")


def test_advanced_can_rescue_a_show_that_could_not_follow():
    print("test_advanced_can_rescue_a_show_that_could_not_follow")
    w, m = _window()
    # Dragging to 0.0 from 3.0 is -3.0, which the show (starting at 2.0) cannot
    # take. Retyping 2.5 makes it -0.5, which it can.
    with DialogDriver("Move show", override=2.5) as driver:
        w.on_waveform_offset_committed(3.0, 0.0)
    _assert(driver.dialog["enabled"]["Move show"] is False,
            "'Move show' starts disabled")
    _assert(driver.after["enabled"]["Move show"] is True,
            "and the typed offset re-enables it")
    _assert(_close(m.earliest_event_time(), 1.5), "the show moved by -0.5")
    _assert(_close(w._song.offset, 2.5), "at the typed offset")


def test_collapsing_advanced_throws_the_override_away():
    print("test_collapsing_advanced_throws_the_override_away")
    _ensure_app()
    from ui.offset_dialog import OffsetRippleDialog
    dialog = OffsetRippleDialog(None, moved_label="backing track",
                                other_label=None, old_offset=0.0,
                                new_offset=1.75, show_start=2.0,
                                show_start_item="clip “X”", bpm=120)
    dialog.advanced.setChecked(True)
    dialog.offset_spin.setValue(9.0)
    _assert(_close(dialog.offset(), 9.0), "an open Advanced supplies the offset")
    dialog.advanced.setChecked(False)
    _assert(_close(dialog.offset(), 1.75),
            "collapsing it goes back to where the drag landed")
    _assert(_close(dialog.offset_spin.value(), 1.75),
            "and the field is reset, never left showing a value in force")
    _assert(_close(dialog.beats_spin.value(), 3.5),
            "the beats field too")


def test_beats_and_seconds_write_each_other():
    print("test_beats_and_seconds_write_each_other")
    _ensure_app()
    from ui.offset_dialog import OffsetRippleDialog
    dialog = OffsetRippleDialog(None, moved_label="backing track",
                                other_label=None, old_offset=1.0,
                                new_offset=2.75, show_start=2.0,
                                show_start_item="clip “X”", bpm=120)
    _assert(_close(dialog.beats_spin.value(), 3.5),
            "beats open on the drag's move, counted from where it started")
    dialog.advanced.setChecked(True)
    dialog.beats_spin.setValue(-4.0)
    _assert(_close(dialog.offset_spin.value(), -1.0)
            and _close(dialog.offset(), -1.0),
            "typing beats writes the offset, which is the value in force")
    _assert("-1.000" in dialog._headline.text(),
            f"and the headline follows ({dialog._headline.text()!r})")
    _assert(dialog.can_move_show(),
            "a move landing the show exactly on t=0 is one it can follow")
    dialog.offset_spin.setValue(2.0)
    _assert(_close(dialog.beats_spin.value(), 2.0),
            "typing the offset writes the beats")


def test_a_move_in_beats_moves_the_show_by_those_beats():
    print("test_a_move_in_beats_moves_the_show_by_those_beats")
    from app.media_controller import MONITOR_BACKING
    mc = _with_guide(monitor=MONITOR_BACKING)
    w, m = _window(media_controller=mc)
    # Base 120, but 90 BPM from 0.5 s: the drag started at 1.0 s, so a beat
    # there is 0.667 s and 3 beats is exactly 2 s.
    w.bpm_state.set_tempo_changes([(0.5, 90)])
    with DialogDriver("Move show", tick_checkbox=True, beats=3.0):
        w.on_waveform_offset_committed(1.0, 1.3)
    _assert(_close(w._song.offset, 3.0, tol=5e-4),
            f"the offset moved by 3 beats at 90 BPM ({w._song.offset})")
    _assert(_close(m.earliest_event_time(), 4.0, tol=5e-4),
            "the show followed by the same 2 s")
    _assert(_close(mc.guide_offset_sec, 2.0, tol=5e-4),
            "and so did the other track")


def test_the_checkbox_is_absent_with_only_one_track():
    print("test_the_checkbox_is_absent_with_only_one_track")
    w, m = _window()
    with DialogDriver("Audio only") as driver:
        w.on_waveform_offset_committed(0.0, 1.0)
    _assert(driver.dialog["checkbox"] is None,
            "a song with no guide is offered no other track")


def test_an_empty_song_with_one_track_is_never_asked():
    print("test_an_empty_song_with_one_track_is_never_asked")
    w = FakeWindow(_make_midi(), SongConfig())
    with DialogDriver("Cancel") as driver:
        w.on_waveform_offset_committed(0.0, 2.0)
    _assert(driver.seen == [],
            "aligning a lone track before programming it does not nag")
    _assert(_close(w._song.offset, 2.0), "the offset is simply kept")


def test_an_empty_song_with_two_tracks_is_still_asked_about_the_other():
    print("test_an_empty_song_with_two_tracks_is_still_asked_about_the_other")
    from app.media_controller import MONITOR_BACKING
    mc = _with_guide(monitor=MONITOR_BACKING)
    w = FakeWindow(_make_midi(), SongConfig(), media_controller=mc)
    with DialogDriver("Keep the new offset", tick_checkbox=True) as driver:
        w.on_waveform_offset_committed(0.0, 2.0)
    dialog = driver.dialog
    _assert(dialog["buttons"] == ["Keep the new offset", "Cancel"],
            f"no show to move, so no 'Move show' (got {dialog['buttons']})")
    _assert(dialog["question"] == "",
            "and no question about a show that does not exist")
    _assert(dialog["checkbox"] is not None, "but the other track is offered")
    _assert(_close(mc.guide_offset_sec, 2.0), "and it moved")

# ----------------------------------------------------------------------
# The drag: one commit, on release, carrying where it started
# ----------------------------------------------------------------------
def _drag_rig():
    """A real view over a real waveform item — the drag is driven with actual
    mouse events, because commit-on-release is Qt's move machinery plus the
    item's press/release bookkeeping, and a hand-rolled call proves neither."""
    from PyQt6.QtGui import QPixmap
    from PyQt6.QtWidgets import QGraphicsScene, QGraphicsView
    from timeline.tracks.waveform import DraggableWaveform
    scene = QGraphicsScene(0, 0, 500, 200)
    view = QGraphicsView(scene)
    view.resize(400, 120)
    item = DraggableWaveform(QPixmap(200, 40))
    scene.addItem(item)
    item.setX(10.0)
    view.show()
    return view, item


def _drag(view, points):
    """Press on the first point, move through the rest, release on the last."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    viewport = view.viewport()
    positions = [view.mapFromScene(x, y) for x, y in points]
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, positions[0])
    for pos in positions[1:]:
        QTest.mouseMove(viewport, pos)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, positions[-1])


def test_drag_commits_once_on_release():
    print("test_drag_commits_once_on_release")
    _ensure_app()
    view, item = _drag_rig()
    live, committed = [], []
    item.position_changed.connect(lambda p: live.append(p.x()))
    item.offset_committed.connect(lambda o, n: committed.append((o, n)))

    _drag(view, [(60, 20), (90, 20), (110, 20)])

    _assert(len(live) > 1, f"every drag frame previewed (got {live})")
    _assert(len(committed) == 1, f"exactly one commit (got {committed})")
    old, new = committed[0]
    _assert(_close(old, 10.0), "the commit carries where the drag STARTED")
    _assert(_close(new, item.x()), "and where it ended")
    _assert(not _close(old, new), "which is not where it started")


def test_a_click_that_moves_nothing_commits_nothing():
    print("test_a_click_that_moves_nothing_commits_nothing")
    _ensure_app()
    view, item = _drag_rig()
    committed = []
    item.offset_committed.connect(lambda o, n: committed.append((o, n)))
    _drag(view, [(60, 20)])
    _assert(item.x() == 10.0, "the item did not move")
    _assert(committed == [],
            "a press and release with no movement is not an edit")


def test_a_programmatic_offset_previews_but_never_commits():
    print("test_a_programmatic_offset_previews_but_never_commits")
    _ensure_app()
    view, item = _drag_rig()
    live, committed = [], []
    item.position_changed.connect(lambda p: live.append(p.x()))
    item.offset_committed.connect(lambda o, n: committed.append((o, n)))
    item.setX(75.0)   # what set_waveform_offset does on a song load
    _assert(live == [75.0], "loading a song previews the stored offset")
    _assert(committed == [],
            "and never asks to move the show it just loaded")


def test_the_track_commits_in_seconds():
    print("test_the_track_commits_in_seconds")
    _ensure_app()
    from timeline.manager import Timeline
    timeline = Timeline(midi_manager=_make_midi(), fixtures=PATCH)
    seen = []
    timeline.waveform_offset_committed.connect(
        lambda o, n: seen.append((o, n)))
    pps = timeline.PIXELS_PER_SECOND
    timeline.waveform_track._on_waveform_committed(1.5 * pps, 4.25 * pps)
    _assert(len(seen) == 1, "the track forwards one commit")
    _assert(_close(seen[0][0], 1.5) and _close(seen[0][1], 4.25),
            "scene pixels converted to timeline seconds")


def main():
    _ensure_app()
    test_shift_all_is_rigid()
    test_shift_all_moves_the_mirrored_notes()
    test_shift_all_leaves_the_setup_notes_at_zero()
    test_a_leftward_shift_still_saves()
    test_shift_all_refuses_across_zero()
    test_a_blackout_never_blocks_the_shift()
    test_shift_all_is_one_undo_step()
    test_shift_all_empty_document()
    test_earliest_marker_time()
    test_shift_markers_moves_every_kind()
    test_shift_markers_refuses_across_zero()
    test_shift_markers_leaves_an_undialled_segue_alone()
    test_show_start_spans_clips_and_markers()
    test_every_kind_of_first_item_is_named()
    test_move_show_moves_both_halves()
    test_move_show_with_no_markers_still_saves()
    test_a_backing_drag_offers_the_show_and_the_guide()
    test_a_guide_drag_offers_the_show_too()
    test_the_other_track_moves_by_the_correction_not_the_offset()
    test_audio_only_keeps_the_offset_and_moves_nothing()
    test_a_drag_in_progress_touches_nothing_but_the_chip()
    test_a_guide_drag_in_progress_leaves_the_chip_alone()
    test_cancel_puts_the_offset_back_and_writes_nothing()
    test_closing_the_dialog_is_a_cancel()
    test_a_show_that_cannot_follow_disables_the_move_button()
    test_every_figure_on_the_dialog_is_an_absolute_offset()
    test_a_show_that_cannot_follow_is_told_in_absolutes_too()
    test_a_show_that_cannot_follow_names_what_is_in_the_way()
    test_a_blackout_is_never_in_the_way()
    test_a_marker_in_the_way_is_named_too()
    test_the_lowest_offset_quoted_can_be_typed_back()
    test_a_blocked_move_is_marked_not_just_disabled()
    test_advanced_overrides_the_offset_the_drag_landed_on()
    test_advanced_can_rescue_a_show_that_could_not_follow()
    test_collapsing_advanced_throws_the_override_away()
    test_beats_and_seconds_write_each_other()
    test_a_move_in_beats_moves_the_show_by_those_beats()
    test_the_checkbox_is_absent_with_only_one_track()
    test_an_empty_song_with_one_track_is_never_asked()
    test_an_empty_song_with_two_tracks_is_still_asked_about_the_other()
    test_drag_commits_once_on_release()
    test_a_click_that_moves_nothing_commits_nothing()
    test_a_programmatic_offset_previews_but_never_commits()
    test_the_track_commits_in_seconds()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
