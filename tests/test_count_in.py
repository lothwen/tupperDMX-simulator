"""
Characterization test: the count-in (precuenta) behaviour.

MediaController with a fake engine + fake MIDI manager (offscreen Qt, no audio
device): count-in start offset, pause-during-count-in resets, seek cancels,
and the Metronome's beat numbering at negative timeline positions.

Run with:
    uv run python tests/test_count_in.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from audio.diagnostics import TimingMonitor
from core.click import KIND_ACCENT


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
        self.clicks = []
        self.voices = []
        # The Metronome reports every beat here; the real engine owns one.
        self.monitor = TimingMonitor()
        # The output's frame clock. Tests move it with advance() and then pump
        # the controller, which is exactly how the real engine drives it.
        self.frames = 0
        self.scheduled = []     # (at_frame, downbeat)

    RATE = 44100

    def rate(self):
        return self.RATE

    def frames_rendered(self):
        return self.frames

    def advance(self, seconds):
        """Render `seconds` of output, as the audio thread would."""
        self.frames += int(seconds * self.RATE)

    def schedule_click(self, kind, at_frame):
        downbeat = kind == KIND_ACCENT
        self.scheduled.append((at_frame, downbeat))
        self.clicks.append(downbeat)
        return max(0, self.frames - at_frame)

    def clear_clicks(self):
        self.scheduled = []

    def schedule_voice(self, samples, at_frame):
        self.voices.append(samples)
        return 0

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

    def trigger_click(self, kind):
        self.clicks.append(kind == KIND_ACCENT)

    def play_voice(self, samples):
        self.voices.append(samples)

    def stop_voices(self):
        pass


class FakeMidiManager:
    midi_data = None

    def set_midi_routing(self, routing): pass
    def is_running(self): return False
    def start_playback(self): return True
    def stop_worker(self): pass
    def seek(self, t): pass
    def play_until(self, t): pass
    def cleanup(self): pass


def _make_controller(tmp):
    from app.media_controller import MediaController
    engine = FakeEngine()
    controller = MediaController(FakeMidiManager(), engine)
    audio_path = os.path.join(tmp, "song.mp3")
    with open(audio_path, "wb") as f:
        f.write(b"fake")
    controller.set_files(audio_path)
    return controller, engine


def test_count_in_disabled_starts_at_zero():
    print("test_count_in_disabled_starts_at_zero")
    with tempfile.TemporaryDirectory() as tmp:
        controller, engine = _make_controller(tmp)
        controller.set_count_in(False, 2)
        _assert(controller.start_playback(), "playback starts")
        _assert(controller.get_current_position() >= 0,
                "no count-in → timeline starts at 0")
        _assert(engine.active, "backing plays immediately")
        controller.stop_playback()


def test_count_in_starts_negative():
    print("test_count_in_starts_negative")
    with tempfile.TemporaryDirectory() as tmp:
        controller, engine = _make_controller(tmp)
        controller.set_bpm(120)
        controller.set_count_in(True, 2)
        _assert(abs(controller.count_in_duration() - 4.0) < 1e-9,
                "2 bars at 120 BPM = 4 s count-in")
        controller.start_playback()
        _assert(controller.get_current_position() < -3.9,
                "count-in → timeline starts at -(bars × 4 beats)")
        _assert(controller.is_counting_in(), "is_counting_in() during pre-roll")
        _assert(not engine.active, "backing waits while counting in")

        controller.set_bpm(60)
        _assert(abs(controller.count_in_duration() - 8.0) < 1e-9,
                "count-in length follows BPM (2 bars at 60 = 8 s)")
        controller.stop_playback()


def test_pause_during_count_in_resets():
    print("test_pause_during_count_in_resets")
    with tempfile.TemporaryDirectory() as tmp:
        controller, engine = _make_controller(tmp)
        controller.set_bpm(120)
        controller.set_count_in(True, 2)
        controller.start_playback()
        _assert(controller.is_counting_in(), "counting in")
        controller.play_pause_playback()  # pause mid count-in
        _assert(not controller.get_playback_state(), "pause during count-in stops")
        _assert(controller.get_current_position() == 0,
                "…and resets the timeline to 0")


def test_seek_cancels_count_in():
    print("test_seek_cancels_count_in")
    with tempfile.TemporaryDirectory() as tmp:
        controller, engine = _make_controller(tmp)
        controller.set_bpm(120)
        controller.set_count_in(True, 2)
        controller.start_playback()
        controller.seek_to_position(10.0)
        _assert(not controller.is_counting_in(), "seek cancels the count-in")
        _assert(controller.get_current_position() >= 10.0,
                "timeline jumped to the seek target")
        _assert(engine.active, "backing plays after the seek")
        controller.stop_playback()


def test_metronome_beat_numbering_at_negative_positions():
    print("test_metronome_beat_numbering_at_negative_positions")
    from app.metronome import Metronome
    engine = FakeEngine()
    audio = Metronome(engine)
    audio.set_bpm(120)  # beat = 0.5 s

    audio.start_metronome(-4.0)  # exactly 8 beats before 0 → downbeat
    _assert(engine.clicks and engine.clicks[0] is True,
            "count-in starts on a downbeat click")
    _assert(engine.scheduled[0][0] == 0,
            "…placed at the playhead itself, so it sounds in the next block")
    audio.stop_metronome()

    engine.clicks.clear()
    # Bar boundaries fall at -4.0/-2.0/0.0; at -1.9 the next boundary (-1.5)
    # is beat 2 of the final bar, so beat 1 lands exactly at t=0.
    numbers = []
    audio.beat_scheduled.connect(lambda n, t, f: numbers.append(n))
    audio.start_metronome(-1.9)
    _assert(engine.scheduled[0][0] == int(0.4 * FakeEngine.RATE),
            "an off-boundary start queues the beat 0.4 s ahead, not now")
    _assert(numbers[0] == 2,
            "beat numbering at negative positions counts toward beat 1 at t=0")

    # Run out the count-in and into the song.
    for _ in range(8):
        engine.advance(0.25)
        audio._pump()
    _assert(numbers[:5] == [2, 3, 4, 1, 2],
            f"the 1-2-3-4 cycle crosses t=0 unbroken (got {numbers[:5]})")
    audio.stop_metronome()

    engine.clicks.clear()
    audio.start_metronome(0.0)  # song start
    _assert(engine.clicks and engine.clicks[0] is True, "t=0 is a downbeat")
    audio.stop_metronome()
    _assert(engine.scheduled == [],
            "stopping un-queues the beats placed ahead of the playhead")


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    test_count_in_disabled_starts_at_zero()
    test_count_in_starts_negative()
    test_pause_during_count_in_resets()
    test_seek_cancels_count_in()
    test_metronome_beat_numbering_at_negative_positions()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
