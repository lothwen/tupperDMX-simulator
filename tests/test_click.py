"""
Characterization test: the click's three layers.

core.click (what each beat is, and whether the click is on), audio.click (what
each kind sounds like) and Metronome.follow (the one place that asks the
stop-click rule on start/seek/retime). Fakes only: no audio device.

Run with:
    uv run python tests/test_click.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from core import click
from core.config import ControlMarkerConfig
from core.tempo_map import TempoMap
from test_count_in import FakeEngine


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_events_carry_number_kind_and_time():
    print("test_events_carry_number_kind_and_time")
    tempo_map = TempoMap(120)
    events = [click.event_at(tempo_map, i) for i in range(-4, 5)]
    _assert([e.number for e in events] == [1, 2, 3, 4, 1, 2, 3, 4, 1],
            "negative indices land on the same 1-4 cycle the song continues")
    _assert([e.kind for e in events if e.number == 1]
            == [click.KIND_ACCENT] * 3,
            "beat 1 is the accent")
    _assert(all(e.kind == click.KIND_BEAT for e in events if e.number != 1),
            "every other beat is a plain beat")
    _assert(events[0].seconds == -2.0 and events[4].seconds == 0.0,
            "each event's time is the tempo map's")


def test_first_index_does_not_sound_a_passed_beat():
    print("test_first_index_does_not_sound_a_passed_beat")
    tempo_map = TempoMap(120)
    _assert(click.first_index_at(tempo_map, 1.0) == 2, "a beat ON the position is due")
    _assert(click.first_index_at(tempo_map, 1.01) == 3,
            "a beat just behind it is not")


def test_every_kind_has_a_sound_at_every_rate():
    print("test_every_kind_has_a_sound_at_every_rate")
    from audio.click import ClickSounds
    sounds = ClickSounds(44100)
    at_44 = {kind: len(sounds.sample(kind)) for kind in click.KINDS}
    sounds.set_rate(48000)
    at_48 = {kind: len(sounds.sample(kind)) for kind in click.KINDS}
    _assert(all(at_44[k] == 4410 and at_48[k] == 4800 for k in click.KINDS),
            "every kind is rebuilt at the output's rate (100 ms either way)")
    _assert(not (sounds.sample(click.KIND_ACCENT)
                 == sounds.sample(click.KIND_BEAT)).all(),
            "the accent sounds different from a beat")


def test_follow_honours_the_stop_click():
    print("test_follow_honours_the_stop_click")
    from app.metronome import Metronome
    engine = FakeEngine()
    metronome = Metronome(engine)
    metronome.set_bpm(120)
    metronome.set_control_markers([ControlMarkerConfig(60.0, "click_stop")])

    metronome.follow(10.0)
    _assert(metronome.is_metronome_running(), "before the marker the click runs")

    metronome.follow(70.0)
    _assert(not metronome.is_metronome_running() and engine.scheduled == [],
            "past it, follow() silences the click and un-queues its beats")

    metronome.follow(59.0)
    _assert(metronome.is_metronome_running(),
            "seeking back before the marker brings it back")

    metronome.set_control_markers([])
    metronome.follow(70.0)
    _assert(metronome.is_metronome_running(),
            "a song with no stop-click runs everywhere")
    metronome.stop_metronome()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
