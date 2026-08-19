"""
Characterization test: the guide-track monitor (backing vs guide).

MediaController with a fake engine + fake MIDI manager (offscreen Qt, no audio
device): the monitor selects which file feeds the engine, active_offset()
drives the sync, a missing guide falls back to the backing, and song switches
re-resolve the active file without touching the monitor — unless auto-monitor
(the desktop editor's) re-picks it per song. Backing and guide are equal: a
song may carry either, both or neither, and one with no monitored track plays
its click/MIDI alone, bounded by the sequence.

Run with:
    uv run python tests/test_guide_monitor.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import time

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from app.media_controller import MONITOR_BACKING, MONITOR_GUIDE


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


class FakeEngine(QObject):
    """Stands in for audio.engine.AudioEngine: records calls, no device."""
    backing_loaded = pyqtSignal(str)
    backing_failed = pyqtSignal(str, str)
    finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.loaded = None
        self.position = 0.0
        self.active = False

    def set_bus_volume(self, bus, volume): pass
    def set_bus_muted(self, bus, muted): pass

    def load_backing(self, path):
        self.loaded = path
        self.backing_loaded.emit(path)

    def unload_backing(self):
        self.loaded = None

    def is_backing_loaded(self):
        return self.loaded is not None

    def set_backing_position(self, seconds):
        self.position = seconds

    def backing_position(self):
        return self.position

    def play(self):
        self.active = True

    def pause(self):
        self.active = False

    def is_active(self):
        return self.active

    def play_voice(self, samples): pass
    def stop_voices(self): pass


class FakeMidiManager:
    midi_data = None
    duration = 0.0

    def get_duration(self): return self.duration
    def set_midi_routing(self, routing): pass
    def is_running(self): return False
    def start_playback(self): return True
    def stop_worker(self): pass
    def seek(self, t): pass
    def play_until(self, t): pass
    def cleanup(self): pass


def _touch(tmp, name):
    path = os.path.join(tmp, name)
    with open(path, "wb") as f:
        f.write(b"fake")
    return path


def _make_controller(tmp):
    from app.media_controller import MediaController
    engine = FakeEngine()
    controller = MediaController(FakeMidiManager(), engine)
    return controller, engine


def _advance(controller, seconds):
    """Move the transport's master clock to `seconds` and tick it once."""
    controller.playback_start_time = time.time() - seconds
    controller._on_playback_tick()


def test_default_monitor_plays_backing():
    print("test_default_monitor_plays_backing")
    with tempfile.TemporaryDirectory() as tmp:
        controller, engine = _make_controller(tmp)
        backing = _touch(tmp, "backing.mp3")
        controller.set_files(backing)
        _assert(controller.monitor == MONITOR_BACKING,
                "monitor defaults to backing")
        _assert(engine.loaded == backing, "backing decoded by default")
        _assert(controller.active_file() == backing,
                "active_file() is the backing")


def test_switch_to_guide_loads_guide():
    print("test_switch_to_guide_loads_guide")
    with tempfile.TemporaryDirectory() as tmp:
        controller, engine = _make_controller(tmp)
        backing = _touch(tmp, "backing.mp3")
        guide = _touch(tmp, "mezcla.mp3")
        controller.set_files(backing)
        controller.set_guide(guide, 0.5)
        _assert(engine.loaded == backing,
                "setting a guide does not reload while monitoring backing")

        seen = []
        controller.monitor_changed.connect(seen.append)
        controller.set_monitor(MONITOR_GUIDE)
        _assert(engine.loaded == guide, "GUIDE monitor decodes the guide")
        _assert(seen == [MONITOR_GUIDE], "monitor_changed emitted once")
        _assert(controller.active_offset() == 0.5,
                "active_offset() is the guide offset under GUIDE")

        controller.set_monitor(MONITOR_GUIDE)
        _assert(seen == [MONITOR_GUIDE], "same-monitor switch is a no-op")
        controller.set_monitor("bogus")
        _assert(controller.monitor == MONITOR_GUIDE,
                "invalid monitor value is ignored")

        controller.set_monitor(MONITOR_BACKING)
        _assert(engine.loaded == backing, "switching back reloads the backing")
        _assert(controller.active_offset() == 0.0,
                "active_offset() is the backing offset under BACKING")


def test_guide_monitor_without_guide_falls_back():
    print("test_guide_monitor_without_guide_falls_back")
    with tempfile.TemporaryDirectory() as tmp:
        controller, engine = _make_controller(tmp)
        backing = _touch(tmp, "backing.mp3")
        controller.set_files(backing)
        controller.set_monitor(MONITOR_GUIDE)
        _assert(controller.active_file() == backing,
                "no guide -> active_file() falls back to the backing")
        _assert(engine.loaded == backing, "engine keeps the backing")
        _assert(controller.start_playback(), "playback still starts")
        controller.stop_playback()


def test_guide_offset_drives_sync():
    print("test_guide_offset_drives_sync")
    with tempfile.TemporaryDirectory() as tmp:
        controller, engine = _make_controller(tmp)
        backing = _touch(tmp, "backing.mp3")
        guide = _touch(tmp, "mezcla.mp3")
        controller.set_files(backing)
        controller.set_guide(guide, 0.5)
        controller.set_monitor(MONITOR_GUIDE)
        controller.set_count_in(False, 2)

        controller.start_playback()
        controller.seek_to_position(2.0)
        _assert(abs(engine.position - 1.5) < 0.1,
                "guide plays at timeline - guide_offset")
        controller.stop_playback()

        controller.set_monitor(MONITOR_BACKING)
        controller.start_playback()
        controller.seek_to_position(2.0)
        _assert(abs(engine.position - 2.0) < 0.1,
                "backing plays at timeline - offset (0.0)")
        controller.stop_playback()


def test_guide_only_song():
    print("test_guide_only_song")
    with tempfile.TemporaryDirectory() as tmp:
        controller, engine = _make_controller(tmp)
        guide = _touch(tmp, "mezcla.mp3")
        controller.set_guide(guide, 0.0)
        controller.set_files(None)
        _assert(engine.loaded is None,
                "no backing + BACKING monitor -> nothing decoded")
        _assert(controller.start_playback(),
                "guide-only song still plays under BACKING (click only)")
        _assert(not engine.active, "...with the backing bus left silent")
        controller.stop_playback()

        controller.set_monitor(MONITOR_GUIDE)
        _assert(engine.loaded == guide, "GUIDE monitor decodes the guide")
        _assert(controller.start_playback(),
                "guide-only song plays under GUIDE monitor")
        controller.stop_playback()


def test_click_only_song_runs_to_the_sequence_end():
    print("test_click_only_song_runs_to_the_sequence_end")
    with tempfile.TemporaryDirectory() as tmp:
        controller, engine = _make_controller(tmp)
        controller.midi_manager.duration = 5.0
        controller.set_files(None)
        _assert(controller.start_playback(),
                "a song with no track at all starts (click + MIDI + lights)")
        _advance(controller, 4.0)
        _assert(controller.is_playing, "still playing inside the sequence")
        _advance(controller, 5.5)
        _assert(not controller.is_playing, "stopped at the end of the sequence")


def test_click_only_song_without_a_sequence_runs_on():
    print("test_click_only_song_without_a_sequence_runs_on")
    with tempfile.TemporaryDirectory() as tmp:
        controller, engine = _make_controller(tmp)
        controller.set_files(None)
        controller.start_playback()
        _advance(controller, 600.0)
        _assert(controller.is_playing,
                "nothing bounds the click, so it runs until stopped")
        controller.stop_playback()


def test_click_only_song_pauses_and_resumes():
    print("test_click_only_song_pauses_and_resumes")
    with tempfile.TemporaryDirectory() as tmp:
        controller, engine = _make_controller(tmp)
        controller.set_files(None)
        controller.start_playback()
        _advance(controller, 3.0)
        controller.play_pause_playback()
        _assert(not controller.is_playing, "click-only song pauses")
        controller.play_pause_playback()
        _assert(controller.is_playing, "click-only song resumes")
        _assert(controller.get_current_position() >= 3.0,
                "...from where it was paused, not from the top")
        controller.stop_playback()


def test_auto_monitor_picks_the_track_the_song_has():
    print("test_auto_monitor_picks_the_track_the_song_has")
    with tempfile.TemporaryDirectory() as tmp:
        controller, engine = _make_controller(tmp)
        backing = _touch(tmp, "backing.mp3")
        guide = _touch(tmp, "guide.mp3")

        # Off by default (headless/live): the monitor never moves on its own.
        controller.set_guide(guide, 0.0)
        controller.auto_select_monitor()
        _assert(controller.monitor == MONITOR_BACKING,
                "auto-monitor off -> a guide does not steal the monitor")

        # On (the desktop editor): every song load re-picks.
        controller.set_auto_monitor(True)
        controller.auto_select_monitor()
        controller.set_files(backing)
        _assert(controller.monitor == MONITOR_GUIDE,
                "both tracks -> the guide is monitored")
        _assert(engine.loaded == guide, "...and the guide is what decodes")

        # Next song: backing only.
        controller.set_guide(None, 0.0)
        controller.auto_select_monitor()
        controller.set_files(backing)
        _assert(controller.monitor == MONITOR_BACKING,
                "no guide -> back to the backing")
        _assert(engine.loaded == backing, "...and the backing is what decodes")


def test_song_switch_reresolves_active():
    print("test_song_switch_reresolves_active")
    with tempfile.TemporaryDirectory() as tmp:
        controller, engine = _make_controller(tmp)
        backing1 = _touch(tmp, "b1.mp3")
        guide1 = _touch(tmp, "g1.mp3")
        controller.set_files(backing1)
        controller.set_guide(guide1, 0.0)
        controller.set_monitor(MONITOR_GUIDE)
        _assert(engine.loaded == guide1, "song 1's guide is monitored")

        # Song switch (same order as PlaylistCore._load_song): guide first,
        # then the backing. Song 2 has no guide.
        backing2 = _touch(tmp, "b2.mp3")
        controller.set_guide(None, 0.0)
        controller.set_files(backing2)
        _assert(controller.monitor == MONITOR_GUIDE,
                "the monitor survives the song switch")
        _assert(engine.loaded == backing2,
                "song without a guide falls back to its backing")

        # Song 3 has a guide again.
        backing3 = _touch(tmp, "b3.mp3")
        guide3 = _touch(tmp, "g3.mp3")
        controller.set_guide(guide3, 0.0)
        controller.set_files(backing3)
        _assert(engine.loaded == guide3,
                "song with a guide resolves back to the guide")


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    test_default_monitor_plays_backing()
    test_switch_to_guide_loads_guide()
    test_guide_monitor_without_guide_falls_back()
    test_guide_offset_drives_sync()
    test_guide_only_song()
    test_click_only_song_runs_to_the_sequence_end()
    test_click_only_song_without_a_sequence_runs_on()
    test_click_only_song_pauses_and_resumes()
    test_auto_monitor_picks_the_track_the_song_has()
    test_song_switch_reresolves_active()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
