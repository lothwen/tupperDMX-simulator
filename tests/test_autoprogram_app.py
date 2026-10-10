"""
The auto program inside the app (app/autoprogram.py, ui/autoprogram_dialog.py).

The rules being pinned:

  * generating a take runs on its own thread and writes nothing to the show —
    no .mid, no snapshot, no manifest;
  * a take that differs only in how it is programmed reuses the analysis, and
    a take asked for while one is being made replaces it, unannounced;
  * applying a take to the LOADED song flushes its unsaved edits into a
    pinned snapshot first, then reloads the document from the take, without
    arming the autosave (it would write the same file back);
  * applying a take to ANOTHER song replaces that song's .mid and nothing
    else: the loaded document is untouched;
  * the window asks for a take as it opens and whenever an input changes,
    shows only a take of what its inputs say, and applies it after asking.

Uses a real ShowManager and MidiManager on a temporary show, and the
synthetic song of tests/test_autoprogram.py written to a WAV.

Run with:
    uv run python tests/test_autoprogram_app.py
"""

import os
import sys
import tempfile
import wave
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np  # noqa: E402
from PyQt6.QtCore import QCoreApplication, QElapsedTimer, QObject, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QColor  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog  # noqa: E402

from app.autoprogram import AutoProgrammer, snapshot_name  # noqa: E402
from app.bpm import BpmState  # noqa: E402
from app.show_manager import ShowManager  # noqa: E402
from autoprogram import colours  # noqa: E402
from autoprogram.analysis import Tuning  # noqa: E402
from autoprogram.director import DEFAULT_FEEL, FEELS, PACE_STEPS, Skipped, Style  # noqa: E402
from autoprogram.take import (ANALYSE, BACKING, DECODE, GUIDE, PROGRAM, Analysis,  # noqa: E402
                              Options, Programmed, Take)
from core import note_layout  # noqa: E402
from core.config import Config, SongConfig  # noqa: E402
from core.show import store, versions  # noqa: E402
from midi.manager import MidiManager  # noqa: E402
from test_autoprogram import SR, _analysis, _patch, _song2  # noqa: E402
import ui.autoprogram_dialog as dialog_module  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _pump_until(done, ms=120_000):
    timer = QElapsedTimer()
    timer.start()
    while not done() and timer.elapsed() < ms:
        QCoreApplication.processEvents()


class _Setlist(QObject):
    """PlaylistCore's two members the programmer uses."""
    setlist_changed = pyqtSignal()

    def song_bpm(self, _song):
        return 120


class _Core(QObject):
    """AppCore's members the programmer reaches, the show half real."""

    def __init__(self, show):
        super().__init__()
        self.config = Config()
        self.show_manager = ShowManager(self.config, self, snapshot_on_open=False)
        self.show_manager.attach(show)
        self.midi_manager = MidiManager(note_layout.build_note_map(),
                                        fixtures=self.config.fixtures)
        self.midi_manager.create_empty_track()
        self.bpm_state = BpmState()
        self.playlist_controller = _Setlist()
        self.stopped = 0

    def stop_playback(self):
        self.stopped += 1


def _write_wav(path):
    pcm = (np.clip(_song2() * 0.5, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SR)
        out.writeframes(pcm.tobytes())


def _show(tmp):
    """A show of two songs on the synthetic WAV, the first one loaded."""
    audio = os.path.join(tmp, "song.wav")
    _write_wav(audio)
    show = store.create_show(tmp, "Auto", fixtures=_patch())
    show.playlist.songs = [SongConfig(id="a", title="Alpha", guide=audio),
                           SongConfig(id="b", title="Beta", guide=audio)]
    show.playlist.current_song_index = 0
    store.save_manifest(show)
    return show


def _generate(programmer, song, options=None, stages=None):
    takes, errors = [], []
    stages = [] if stages is None else stages
    programmer.generated.connect(takes.append)
    programmer.failed.connect(lambda step, message: errors.append(message))
    programmer.stage.connect(stages.append)
    programmer.generate(song, options or Options())
    running = programmer.busy()
    _pump_until(lambda: takes or errors)
    _pump_until(lambda: not programmer.busy(), 10_000)
    programmer.generated.disconnect()
    programmer.failed.disconnect()
    programmer.stage.disconnect()
    if errors:
        raise AssertionError(f"generating failed: {errors[0]}")
    return takes[0], running


def _listing(root):
    return sorted(os.path.relpath(os.path.join(d, f), root)
                  for d, _dirs, files in os.walk(root) for f in files)


def test_a_take_is_generated_on_its_own_thread_and_writes_nothing():
    print("test_a_take_is_generated_on_its_own_thread_and_writes_nothing")
    with tempfile.TemporaryDirectory() as tmp:
        show = _show(tmp)
        core = _Core(show)
        before = _listing(show.root)
        programmer = AutoProgrammer(core)
        stages = []
        take, running = _generate(programmer, show.playlist.songs[0], stages=stages)
        _assert(running, "generate() returns while the take is still being made")
        _assert(stages == [DECODE, ANALYSE, PROGRAM], f"each step is told as it starts {stages}")
        _assert(take.song is show.playlist.songs[0] and take.track == GUIDE,
                "the take is of the song asked for, from its guide (the default)")
        _assert(os.path.exists(take.midi_path) and not take.midi_path.startswith(str(show.root)),
                "its .mid waits outside the show")
        _assert(take.analysis.map.sections and take.analysis.bpm is None
                and take.bpm == round(take.analysis.map.bpm),
                "it carries its analysis — a song with no tempo yet takes the one found")
        _assert({lane.channel: lane.role for lane in take.lanes}
                == {0: "pixel", 1: "blinder", 2: "fog", 3: "wash", 4: "wash", 5: "mover"}
                and not take.skipped, "and what each lane got")
        _assert(any("CLIMAX" in line for line in take.summary), "and the plan it made")
        _assert(_listing(show.root) == before, "the show folder is exactly as it was")
        programmer.cleanup()
        _assert(not os.path.exists(take.midi_path), "cleanup() drops the takes' files")


def test_programming_again_reuses_the_analysis():
    print("test_programming_again_reuses_the_analysis")
    with tempfile.TemporaryDirectory() as tmp:
        show = _show(tmp)
        core = _Core(show)
        alpha = show.playlist.songs[0]
        programmer = AutoProgrammer(core)
        _assert(programmer.analysis_for(alpha, Options()) is None, "nothing analysed yet")
        first, _running = _generate(programmer, alpha)
        _assert(programmer.analysis_for(alpha, Options()) is first.analysis,
                "the analysis is kept")
        stages = []
        options = Options(palette="ocean", pace=6, style=Style(cycle=2))
        again, _running = _generate(programmer, alpha, options, stages)
        _assert(stages == [PROGRAM], f"another palette and style only program again {stages}")
        _assert(again.analysis is first.analysis and again.palette == "ocean",
                "from the same analysis")
        stages = []
        _generate(programmer, alpha, Options(tuning=Tuning(quiet_max=0.4)), stages)
        _assert(stages == [DECODE, ANALYSE, PROGRAM], "another tuning analyses again")
        programmer.cleanup()


def test_a_newer_take_replaces_the_one_being_made():
    print("test_a_newer_take_replaces_the_one_being_made")
    with tempfile.TemporaryDirectory() as tmp:
        show = _show(tmp)
        core = _Core(show)
        alpha, beta = show.playlist.songs
        programmer = AutoProgrammer(core)
        takes = []
        programmer.generated.connect(takes.append)
        programmer.generate(alpha, Options())
        programmer.generate(beta, Options(palette="neon"))
        _pump_until(lambda: takes)
        _pump_until(lambda: not programmer.busy(), 10_000)
        _assert(len(takes) == 1 and takes[0].song is beta and takes[0].palette == "neon",
                "only the newer take is announced")
        programmer.generate(alpha, Options())
        programmer.cancel()
        _pump_until(lambda: not programmer.busy(), 30_000)
        QCoreApplication.processEvents()
        _assert(len(takes) == 1, "a cancelled take is never announced")
        programmer.cleanup()


def test_applying_to_the_loaded_song_snapshots_then_reloads():
    print("test_applying_to_the_loaded_song_snapshots_then_reloads")
    with tempfile.TemporaryDirectory() as tmp:
        show = _show(tmp)
        core = _Core(show)
        alpha = show.playlist.songs[0]
        programmer = AutoProgrammer(core)
        take, _running = _generate(programmer, alpha)

        # a hand edit, not saved yet
        core.midi_manager.add_event(note_layout.BLACKOUT_ALL, 1.0, 100, None)
        core.show_manager.on_midi_data_changed()
        applied = []
        programmer.applied.connect(lambda song, current: applied.append((song, current)))
        programmer.apply(take)

        snapshots = [v for v in versions.list_versions(show)
                     if v.name == snapshot_name(alpha) == "before auto-program — Alpha"]
        _assert(len(snapshots) == 1 and snapshots[0].pinned,
                "a pinned snapshot named after the song is taken")
        saved = show.versions_dir / snapshots[0].version_id / "midis" / os.path.basename(alpha.midi)
        old = MidiManager(note_layout.build_note_map(), fixtures=show.fixtures)
        _assert(saved.exists() and old.load_from_file(str(saved)) and len(old.midi_events) == 1,
                "it holds the hand edit, flushed into the song's .mid first")
        generated = MidiManager(note_layout.build_note_map(), fixtures=show.fixtures)
        generated.load_from_file(take.midi_path)
        _assert(len(core.midi_manager.midi_events) == len(generated.midi_events) > 50,
                f"the loaded document is the take ({len(core.midi_manager.midi_events)} events)")
        _assert(str(alpha.midi).startswith(str(show.midis_dir)),
                "the song's .mid is its own, in the show")
        _assert(not core.show_manager._midi_dirty, "reloading did not arm the autosave")
        _assert(core.stopped == 1, "playback of the loaded song was stopped first")
        _assert(applied == [(alpha, True)], "applied() names the loaded song")
        programmer.cleanup()


def test_applying_to_another_song_replaces_only_its_file():
    print("test_applying_to_another_song_replaces_only_its_file")
    with tempfile.TemporaryDirectory() as tmp:
        show = _show(tmp)
        core = _Core(show)
        beta = show.playlist.songs[1]
        programmer = AutoProgrammer(core)
        take, _running = _generate(programmer, beta, Options(pace=2, palette="ocean"))
        core.midi_manager.add_event(note_layout.BLACKOUT_ALL, 1.0, 100, None)
        changed = []
        core.playlist_controller.setlist_changed.connect(lambda: changed.append(1))
        programmer.apply(take)

        _assert(beta.midi and os.path.exists(beta.midi)
                and str(beta.midi).startswith(str(show.midis_dir)),
                "the other song got its own .mid in the show")
        _assert(len(core.midi_manager.midi_events) == 1, "the loaded document is untouched")
        _assert(core.stopped == 0, "playback was left alone")
        _assert(store.load_show(show.root).playlist.songs[1].midi == beta.midi,
                "the manifest names the new file")
        _assert(changed, "the setlist is told (its cached tempo)")
        programmer.cleanup()


# --------------------------------------------------------------------- window
class _Programmer(QObject):
    stage = pyqtSignal(str)
    generated = pyqtSignal(object)
    failed = pyqtSignal(str, str)
    applied = pyqtSignal(object, bool)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.cached = None            # what analysis_for() answers

    def busy(self):
        return False

    def fixture_count(self):
        return 4

    def song_tempo(self, _song):
        return 120

    def analysis_for(self, _song, _options):
        return self.cached

    def generate(self, song, options):
        self.calls.append(("generate", song, options))

    def cancel(self):
        self.calls.append(("cancel",))

    def apply(self, take):
        self.calls.append(("apply", take))


_MAPPED = None


def _mapped():
    global _MAPPED
    if _MAPPED is None:
        _MAPPED = Analysis(BACKING, "/song.wav", 120, "4/4", Tuning(), _analysis())
    return _MAPPED


def _take(song, options):
    lanes = [Programmed(0, "ledbar", "pixel", [], 3, 4),
             Programmed(5, "moving-head", "mover", [6], 2, 1, mirror=7)]
    return Take(song, options, options.track, "ember", 1, 120, "/nowhere.mid",
                ["a plan"], ["no steady beat found"], 10, 2, _mapped(), lanes,
                [Skipped(3, "relay-switch", "nothing to program")])


def _generates(programmer):
    return [call for call in programmer.calls if call[0] == "generate"]


def test_the_window_shows_and_applies_only_what_its_inputs_say():
    print("test_the_window_shows_and_applies_only_what_its_inputs_say")
    audio = os.path.abspath(__file__)            # any file that exists will do
    songs = [SongConfig(id="a", title="Alpha", guide=audio),
             SongConfig(id="s", title="Silent"),
             SongConfig(id="b", title="Beta", audio=audio)]
    programmer = _Programmer()
    dialog = dialog_module.AutoProgramDialog(programmer, songs, songs[2])
    shown = lambda widget: widget.isVisibleTo(dialog)  # noqa: E731
    _assert(dialog.song_combo.count() == 3 and dialog.song() is songs[2],
            "every song is offered, the loaded one picked first")
    _assert(not dialog.track_buttons[GUIDE].isEnabled()
            and dialog.track_buttons[BACKING].isChecked(), "only the tracks the song has")
    (_kind, song, options), = _generates(programmer)
    _assert(song is songs[2] and options == Options(track=BACKING),
            "the analysis starts as the window opens, at the defaults")
    _assert(dialog.middle.currentIndex() == 1 and not dialog.apply_button.isEnabled()
            and not dialog.export_button.isEnabled(), "its progress in place of the map")
    programmer.stage.emit(ANALYSE)
    _assert(dialog._step_rows[DECODE][0].state == "done"
            and dialog._step_rows[ANALYSE][0].state == "now", "the steps follow the work")

    programmer.generated.emit(_take(songs[0], options))
    _assert(not dialog.apply_button.isEnabled(), "a take of another song is ignored")
    programmer.generated.emit(_take(song, options))
    _assert(dialog.apply_button.isEnabled() and dialog.export_button.isEnabled(),
            "the take asked for can be applied")
    _assert(dialog.middle.currentIndex() == 0 and dialog.song_map.song_map is _mapped().map,
            "its song map is drawn")
    table = dialog.sections_table
    marked = [part for part, _s in _mapped().map.parts() if part != "END"] + ["END"]
    _assert([table.item(r, 2).text() for r in range(table.rowCount())] == marked
            and marked[-2:] == ["OUTRO", "END"],
            "a row per section the band marks, the climax the song ends on as its OUTRO, "
            "then the END")
    roles = [dialog.fixtures_table.item(r, 2).data(dialog_module.CHIP_ROLE)
             for r in range(dialog.fixtures_table.rowCount())]
    _assert(roles == ["pixel", None, "mover"], f"lanes and the skipped by channel {roles}")
    notes = [dialog.fixtures_table.item(r, 1).data(dialog_module.NOTE_ROLE)
             for r in range(dialog.fixtures_table.rowCount())]
    _assert(notes == ["", "— nothing to program", "also drives ch 6 · mirrors ch 7"],
            f"a lane's twins and the lane it mirrors across the stage {notes}")
    notices = [dialog.notices.item(r).text() for r in range(dialog.notices.count())]
    _assert(notices == ["No steady beat found", "Skipped ch 3 relay-switch: nothing to program"],
            f"its warnings, Apply still enabled {notices}")
    _assert(dialog.palette_combo.picked == "Ember", "Auto says what the mood picked")

    programmer.cached = _mapped()
    dialog.palette_combo.setCurrentIndex(dialog.palette_combo.findData("ocean"))
    _assert(_generates(programmer)[-1][2].palette == "ocean"
            and not dialog.apply_button.isEnabled(), "another palette asks for another take")
    _assert(dialog.middle.currentIndex() == 0, "programmed from the analysis on show")
    asked = len(_generates(programmer))
    dialog.palette_combo.setCurrentIndex(dialog.palette_combo.findData("ocean"))
    dialog.track_buttons[BACKING].click()
    _assert(len(_generates(programmer)) == asked, "the same inputs ask for nothing more")

    dialog.advanced_button.setChecked(True)
    _assert(shown(dialog.advanced_panel), "Advanced opens its panel")
    dialog._fields["quiet_max"][1].setValue(0.4)
    dialog._fields["cycle"][1].setValue(2)
    _assert(dialog._fields["quiet_max"][2].text() == "Quiet below •"
            and dialog._advanced_count.text() == "2 changed", "changed values are marked")
    _assert(len(_generates(programmer)) == asked, "spin box edits wait a moment")
    _pump_until(lambda: len(_generates(programmer)) > asked, 3000)
    options = _generates(programmer)[-1][2]
    _assert(options.tuning == Tuning(quiet_max=0.4) and options.style == Style(cycle=2),
            "then ask for a take at the values typed")
    dialog.reset_button.click()
    options = _generates(programmer)[-1][2]
    _assert(options.tuning == Tuning() and options.style == Style()
            and not shown(dialog._advanced_count), "Reset to defaults puts them all back")

    dialog.song_combo.setCurrentIndex(1)
    _assert(shown(dialog.error_panel) and not shown(dialog.tables)
            and programmer.calls[-1] == ("cancel",) and not dialog.apply_button.isEnabled(),
            "a song with no audio says so, and asks for nothing")
    dialog.song_combo.setCurrentIndex(2)
    (_kind, song, options) = _generates(programmer)[-1]
    programmer.generated.emit(_take(song, options))

    dialog._confirm = lambda take: True
    dialog.apply_button.click()
    _assert(programmer.calls[-2][0] == "apply" and programmer.calls[-2][1].options == options,
            "Apply asks first, then applies the take on show")
    _assert(dialog.result() == QDialog.DialogCode.Accepted and programmer.calls[-1] == ("cancel",),
            "and closes, dropping anything still being made")
    programmer.generated.emit(_take(song, options))   # the window let go of the signals
    dialog.deleteLater()


def test_the_palette_offers_presets_modes_and_picks():
    print("test_the_palette_offers_presets_modes_and_picks")
    songs = [SongConfig(id="b", title="Beta", audio=os.path.abspath(__file__))]
    programmer = _Programmer()
    programmer.cached = _mapped()
    dialog = dialog_module.AutoProgramDialog(programmer, songs, songs[0])
    shown = lambda widget: widget.isVisibleTo(dialog)  # noqa: E731
    combo, hue = dialog.palette_combo, dialog.hue_combo
    _assert(all(combo.findData(name) > 0 for name in colours.PALETTES)
            and all(combo.findData(mode) > 0 for mode in colours.MODES)
            and combo.findData(colours.CUSTOM) > 0,
            "every preset, every mode of the fixture palette and Custom are offered")
    _assert(not shown(hue) and not any(shown(b) for b in dialog.custom_buttons),
            "Auto needs nothing beside it")

    combo.setCurrentIndex(combo.findData("triadic"))
    options = _generates(programmer)[-1][2]
    _assert(shown(hue) and options.palette == "triadic:random",
            "a mode shows the hue it walks from, Random to start with")
    programmer.generated.emit(replace(_take(songs[0], options), palette="triadic:azure"))
    _assert("Azure" in hue.toolTip(), "and says which hue the take drew")
    hue.setCurrentIndex(hue.findData("azure"))
    _assert(_generates(programmer)[-1][2].palette == "triadic:azure",
            "picking that hue keeps it")

    combo.setCurrentIndex(combo.findData(colours.CUSTOM))
    options = _generates(programmer)[-1][2]
    _assert(not shown(hue) and all(shown(b) for b in dialog.custom_buttons)
            and colours.palette(options.palette)[1] == colours.palette("triadic:azure")[1],
            "Custom shows its three colours, starting from those on show")
    dialog._set_custom(2, QColor("#ffffff"))
    _assert(_generates(programmer)[-1][2].palette.endswith(",#ffffff"),
            "a colour picked asks for a take in it")
    combo.setCurrentIndex(combo.findData("ember"))
    _assert(not shown(hue) and not any(shown(b) for b in dialog.custom_buttons)
            and _generates(programmer)[-1][2].palette == "ember", "a preset is just its name")


def test_the_feel_sets_the_generation_values_and_edits_go_on_top():
    print("test_the_feel_sets_the_generation_values_and_edits_go_on_top")
    songs = [SongConfig(id="b", title="Beta", audio=os.path.abspath(__file__))]
    programmer = _Programmer()
    programmer.cached = _mapped()
    dialog = dialog_module.AutoProgramDialog(programmer, songs, songs[0])
    shown = lambda widget: widget.isVisibleTo(dialog)  # noqa: E731
    names = [feel.name for feel in FEELS]
    _assert(dialog.feel_slider.value() == DEFAULT_FEEL
            and dialog.feel_ticks.active_name() == "Steady", "a window opens Steady")

    busy = FEELS[names.index("Busy")]
    dialog.feel_slider.setValue(names.index("Busy"))
    _assert(dialog.feel_ticks.active_name() == "Busy" and dialog._shown("pace") == busy.pace
            and dialog._shown("cycle") == busy.style.cycle
            and not shown(dialog._advanced_count),
            "a feel sets its values, and they are not edits")
    _pump_until(lambda: _generates(programmer)[-1][2].pace == busy.pace, 3000)
    options = _generates(programmer)[-1][2]
    _assert(options.style == busy.style, "a take is asked for at the feel's values")

    dialog._fields["glow_scale"][1].setValue(80)
    _assert(dialog._advanced_count.text() == "1 changed"
            and dialog._fields["glow_scale"][2].text() == "Glow level •",
            "an edit is marked against the feel")
    dialog.reset_button.click()
    _assert(dialog._shown("glow_scale") == round(busy.style.glow_scale * 100)
            and dialog._shown("pace") == busy.pace and not shown(dialog._advanced_count),
            "Reset to defaults puts back the feel's values")

    dialog._fields["pace"][1].setValue(1)
    dialog.feel_slider.setValue(names.index("Still"))
    _assert(dialog._shown("pace") == 0 and dialog._shown("cycle") == 1
            and not shown(dialog._advanced_count),
            "moving the slider puts the feel's values over any edit")
    dialog.deleteLater()


def test_the_pace_says_what_it_does_in_the_songs_own_meter():
    print("test_the_pace_says_what_it_does_in_the_songs_own_meter")
    _assert(dialog_module.pace_readout(0, "4/4") == "one look per section",
            "at 0 a section holds one look")
    _assert(dialog_module.pace_readout(PACE_STEPS, "4/4")
            == "chorus every beat · climax every half beat",
            "at the top a chorus changes every beat and the climax every half beat")
    audio = os.path.abspath(__file__)
    songs = [SongConfig(id="a", title="Alpha", audio=audio),
             SongConfig(id="w", title="Waltz", audio=audio, meter="3/4")]
    programmer = _Programmer()
    dialog = dialog_module.AutoProgramDialog(programmer, songs, songs[0])
    _assert(dialog.pace_readout.text() == "Pace 4: chorus every 2 bars · climax every bar",
            "the default pace, in bars")
    dialog._fields["pace"][1].setValue(5)
    _assert(dialog.pace_readout.text() == "Pace 5: chorus every bar · climax every 2 beats",
            "the readout follows the field")
    dialog.song_combo.setCurrentIndex(1)
    _assert(dialog.pace_readout.text() == "Pace 5: chorus every bar · climax every 1.5 beats",
            "and counts in the song's own meter")
    _assert(_generates(programmer)[-1][2].pace == 5, "a take is asked for at that pace")
    dialog.deleteLater()


def test_the_window_when_the_analysis_cannot_finish():
    print("test_the_window_when_the_analysis_cannot_finish")
    audio = os.path.abspath(__file__)
    songs = [SongConfig(id="a", title="Alpha", guide=audio)]
    programmer = _Programmer()
    dialog = dialog_module.AutoProgramDialog(programmer, songs, songs[0])
    shown = lambda widget: widget.isVisibleTo(dialog)  # noqa: E731
    _assert(not shown(dialog.advanced_panel), "the Advanced panel opens closed")
    dialog.cancel_button.click()
    _assert(programmer.calls[-1] == ("cancel",) and shown(dialog.error_panel)
            and dialog.retry_button.text() == "Run again", "a cancelled analysis can run again")
    dialog.retry_button.click()
    _assert(len(_generates(programmer)) == 2, "and does")
    programmer.failed.emit(DECODE, "cannot decode song.wav: invalid data")
    _assert(dialog._error_title.text() == f"Could not decode {os.path.basename(audio)}"
            and dialog._error_detail.text() == "cannot decode song.wav: invalid data"
            and dialog.retry_button.text() == "Retry", "a file that cannot be read says so")
    dialog.retry_button.click()
    (_kind, song, options) = _generates(programmer)[-1]
    take = _take(song, options)
    take.lanes, take.skipped = [], [Skipped(0, "relay-switch", "nothing to program")]
    programmer.generated.emit(take)
    _assert(not dialog.apply_button.isEnabled()
            and dialog.notices.item(dialog.notices.count() - 1).text() == dialog_module.NO_FIXTURES,
            "a patch with nothing to program has nothing to apply")

    confirm = dialog_module.ReplaceDialog(_take(song, options), dialog)
    texts = [label.text() for label in confirm.findChildren(dialog_module.QLabel)]
    _assert("Every clip, scene and fade on all 4 lanes" in texts
            and "The song’s own 120 BPM, not the detected 120.00" not in texts
            and any(t.startswith("The song’s own 120 BPM") for t in texts),
            "the confirmation counts every lane the song loses, and keeps its tempo")
    _assert(confirm.cancel_button.isDefault(), "Cancel is its default")
    confirm.deleteLater()
    dialog.reject()
    dialog.deleteLater()


def main():
    test_a_take_is_generated_on_its_own_thread_and_writes_nothing()
    test_programming_again_reuses_the_analysis()
    test_a_newer_take_replaces_the_one_being_made()
    test_applying_to_the_loaded_song_snapshots_then_reloads()
    test_applying_to_another_song_replaces_only_its_file()
    test_the_window_shows_and_applies_only_what_its_inputs_say()
    test_the_palette_offers_presets_modes_and_picks()
    test_the_feel_sets_the_generation_values_and_edits_go_on_top()
    test_the_pace_says_what_it_does_in_the_songs_own_meter()
    test_the_window_when_the_analysis_cannot_finish()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
