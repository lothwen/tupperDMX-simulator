"""
Characterization test: a song's meter, and the click that counts it.

The meter reaches everything through the tempo map — the ruler's bars and
grid, the click's numbering and accent, the count-in's length and its spoken
last bar, the markers dialog's bar.beat — and rides the song entry in
show.json. The BPM always counts quarter notes, so a /8 meter clicks twice to
every quarter. Also covers the click popup's two actions on AppCore
(free-run, "Set as song tempo") and the machine-level accent switch.

Run with:
    uv run python tests/test_meter.py
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from app.bpm import BpmState
from app.core import AppCore
from app.metronome import Metronome
from core import click
from core.config import Config, SongConfig
from core.show.manifest import show_from_manifest, show_to_manifest
from core.show.model import Show
from core.tempo_map import (DEFAULT_METER, GRID_BEATS, METERS, TempoMap,
                            valid_meter)
from test_count_in import FakeEngine, _make_controller


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


# ----------------------------------------------------------------------
# The tempo map
# ----------------------------------------------------------------------
def test_meter_names():
    print("test_meter_names")
    _assert(METERS == ("3/4", "4/4", "5/4", "7/4", "3/8", "6/8"),
            "the six meters the click popup offers")
    maps = [TempoMap(120, meter=m) for m in METERS]
    _assert([t.beats_per_bar for t in maps] == [3, 4, 5, 7, 3, 6],
            "a bar is the numerator in beats")
    _assert([t.beat_note for t in maps] == [4, 4, 4, 4, 8, 8],
            "and a beat is the denominator's note")
    _assert(valid_meter("4/3") == DEFAULT_METER and valid_meter(None) == "4/4",
            "anything else loads as 4/4")


def test_bars_follow_the_meter():
    print("test_bars_follow_the_meter")
    waltz = TempoMap(120, meter="3/4")
    _assert(waltz.bar_seconds_at(0.0) == 1.5, "a 3/4 bar at 120 is 1.5 s")
    _assert(waltz.bar_beat_at(1.5) == (2, 1) and waltz.bar_beat_at(1.0) == (1, 3),
            "bar.beat counts three to the bar")
    _assert(waltz.seconds_at_bar_beat(3, 2) == 3.5, "and inverts")
    _assert(waltz.shortest_bar_seconds() == 1.5, "the label-density bar is 3 beats")



def test_the_bpm_counts_quarters_in_eighth_meters():
    print("test_the_bpm_counts_quarters_in_eighth_meters")
    six_eight = TempoMap(120, meter="6/8")
    _assert(six_eight.beat_seconds_at(0.0) == 0.25,
            "6/8 at 120: a beat (an eighth) is half of a 0.5 s quarter")
    _assert(six_eight.bar_seconds_at(0.0) == 1.5, "so the bar is 1.5 s")
    _assert(six_eight.bpm_at(0.0) == 120 and six_eight.bpm_at(-2.0) == 120,
            "and the map still reports the quarter-note BPM")
    _assert(six_eight.bar_beat_at(1.0) == (1, 5), "bar.beat counts eighths")

    three_eight = TempoMap(90, meter="3/8")
    _assert(abs(three_eight.bar_seconds_at(0.0) - 1.0) < 1e-9,
            "3/8 at 90 is half as long as 3/4 at 90")

    changed = TempoMap(120, [(3.0, 60)], meter="6/8")
    _assert(changed.bpm_at(4.0) == 60 and changed.beat_seconds_at(4.0) == 0.5,
            "a tempo marker is a quarter-note BPM too")
    _assert(changed.changes() == [(3.0, 60)], "and is handed back as written")


def test_grid_keeps_bars_and_beats_in_any_meter():
    print("test_grid_keeps_bars_and_beats_in_any_meter")
    waltz = TempoMap(120, meter="3/4")
    bars = list(waltz.grid_lines(4.6, 1))
    _assert([(s, n) for s, n, _ in bars] == [(0.0, 1), (1.5, 2), (3.0, 3), (4.5, 4)],
            "divisions=1 draws one line per 3/4 bar")
    beats = list(waltz.grid_lines(3.0, GRID_BEATS))
    _assert([s for s, _, _ in beats] == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
            "the beat grid is a line per beat")
    _assert([b for _, _, b in beats] == [True, False, False, True, False, False, True],
            "with every third line a bar line")
    halves = list(waltz.grid_lines(1.5, 8))
    _assert(len(halves) == 7 and [b for _, _, b in halves].count(True) == 2,
            "half-beats: six lines to the bar")
    _assert(waltz.nearest_grid_seconds(2.1, 1) == 1.5
            and waltz.nearest_grid_seconds(2.4, 1) == 3.0,
            "snapping to bars snaps to 3/4 bars")


def test_four_four_grid_is_unchanged():
    print("test_four_four_grid_is_unchanged")
    common = TempoMap(120)
    for divisions in (1, 4, 8):
        step = 4 / divisions
        expected = [(i * step * 0.5, int(i * step // 4) + 1, i % divisions == 0)
                    for i in range(int(8 / step) + 1)]
        _assert(list(common.grid_lines(4.0, divisions)) == expected,
                f"4/4 at divisions={divisions} walks the grid it always did")


# ----------------------------------------------------------------------
# The click
# ----------------------------------------------------------------------
def test_click_counts_and_accents_the_meter():
    print("test_click_counts_and_accents_the_meter")
    six_eight = TempoMap(120, meter="6/8")
    events = [click.event_at(six_eight, i) for i in range(-6, 7)]
    _assert([e.number for e in events] == [1, 2, 3, 4, 5, 6] * 2 + [1],
            "6/8 numbers its beats 1-6, through the count-in too")
    _assert([e.kind for e in events].count(click.KIND_ACCENT) == 3,
            "only beat 1 is accented")

    plain = [click.event_at(six_eight, i, accent=False) for i in range(7)]
    _assert(all(e.kind == click.KIND_BEAT for e in plain),
            "with the accent off, beat 1 sounds like every other beat")
    _assert(plain[0].number == 1 and plain[6].number == 1,
            "but it is still counted as beat 1")


def test_metronome_accent_switch():
    print("test_metronome_accent_switch")
    engine = FakeEngine()
    metronome = Metronome(engine)
    metronome.set_tempo_map(TempoMap(120, meter="3/4"))

    def bar_of_clicks():
        engine.clicks = []
        metronome.start_metronome(0.0)
        for _ in range(10):                      # 2 s of output
            engine.advance(0.2)
            metronome._pump()
        metronome.stop_metronome()
        return engine.clicks[:4]

    metronome.set_accent(False)
    _assert(bar_of_clicks() == [False] * 4,
            "accent off: no beat is queued with the accent sound")
    metronome.set_accent(True)
    _assert(bar_of_clicks() == [True, False, False, True],
            "accent on: the 3/4 downbeat takes it again")


def test_free_run_counts_its_own_map_until_the_transport_takes_over():
    print("test_free_run_counts_its_own_map_until_the_transport_takes_over")
    engine = FakeEngine()
    metronome = Metronome(engine)
    metronome.set_tempo_map(TempoMap(120))       # the song: 4/4 at 120
    changes = []
    metronome.free_run_changed.connect(changes.append)

    metronome.start_free_run(TempoMap(60, meter="3/4"))
    _assert(metronome.is_free_running() and changes == [True],
            "free-run starts, and says so once")
    for _ in range(40):                          # 4 s of output
        engine.advance(0.1)
        metronome._pump()
    frames = [f for f, _ in engine.scheduled]
    _assert(frames[1] - frames[0] == FakeEngine.RATE,
            "it counts ITS tempo (60 BPM), not the song's")
    _assert([d for _, d in engine.scheduled][:4] == [True, False, False, True],
            "in its own meter (3/4)")

    metronome.start_free_run(TempoMap(90))
    _assert(changes == [True], "a retime is not a new start")
    _assert(metronome.bpm == 120, "and the song's map is untouched")

    metronome.follow(0.0)
    _assert(not metronome.is_free_running() and changes == [True, False],
            "following the transport ends free-run")
    _assert(metronome.is_metronome_running(), "and runs the song's click instead")
    metronome.stop_metronome()
    _assert(changes == [True, False], "stopping the song's click is not free-run's end")


def test_app_core_free_run():
    print("test_app_core_free_run")
    engine = FakeEngine()
    playing = [False]
    core = SimpleNamespace(
        metronome=Metronome(engine),
        media_controller=SimpleNamespace(get_playback_state=lambda: playing[0]))

    playing[0] = True
    AppCore.start_free_run(core, 100, "3/4")
    _assert(not core.metronome.is_free_running(),
            "refused while the song plays — that click is the song's")

    playing[0] = False
    AppCore.start_free_run(core, 60, "6/8")
    clock = core.metronome._clock_map()
    _assert(core.metronome.is_free_running() and clock.beats_per_bar == 6
            and clock.bpm_at(0.0) == 60,
            "starts at the popup's tempo and meter")
    for _ in range(30):                          # 3 s of output
        engine.advance(0.1)
        core.metronome._pump()
    frames = [f for f, _ in engine.scheduled]
    _assert(frames[1] - frames[0] == FakeEngine.RATE // 2,
            "6/8 at 60 clicks every eighth: twice to each 1 s quarter")
    _assert([d for _, d in engine.scheduled][:7] == [True] + [False] * 5 + [True],
            "six clicks to the bar, the first accented")
    AppCore.stop_free_run(core)
    _assert(not core.metronome.is_metronome_running(), "and stops")


def test_a_tempo_edit_leaves_free_run_alone():
    print("test_a_tempo_edit_leaves_free_run_alone")
    engine = FakeEngine()
    announced = []
    core = SimpleNamespace(
        metronome=Metronome(engine),
        media_controller=SimpleNamespace(
            set_tempo_map=lambda m: None, get_current_position=lambda: 0.0,
            monotonic_at=lambda t: None),
        tempo_map_changed=SimpleNamespace(emit=announced.append))
    core.metronome.start_free_run(TempoMap(60))
    AppCore._on_tempo_map_changed(core, TempoMap(140))
    _assert(core.metronome.is_free_running()
            and core.metronome._clock_map().bpm_at(0.0) == 60,
            "a song tempo change does not hijack a free-run")
    _assert(core.metronome.bpm == 140 and len(announced) == 1,
            "but the song's map is still installed and announced")
    core.metronome.stop_metronome()


# ----------------------------------------------------------------------
# The count-in
# ----------------------------------------------------------------------
def test_count_in_is_bars_of_the_meter():
    print("test_count_in_is_bars_of_the_meter")
    with tempfile.TemporaryDirectory() as tmp:
        controller, _engine = _make_controller(tmp)
        controller.set_tempo_map(TempoMap(120, meter="3/4"))
        controller.set_count_in(True, 2)
        _assert(abs(controller.count_in_duration() - 3.0) < 1e-9,
                "two bars of 3/4 at 120 are 3 s")
        controller.set_tempo_map(TempoMap(120, meter="6/8"))
        _assert(abs(controller.count_in_duration() - 3.0) < 1e-9,
                "two bars of 6/8 at 120 are twelve quarter-second eighths")


def test_spoken_count_in_is_the_last_bar_of_the_meter():
    print("test_spoken_count_in_is_the_last_bar_of_the_meter")
    spoken = []
    words = [f"word{n}" for n in range(1, 8)]
    core = SimpleNamespace(
        config=SimpleNamespace(audio=SimpleNamespace(count_in_voice=True)),
        _count_words=words,
        metronome=SimpleNamespace(tempo_map=TempoMap(120, meter="3/4")),
        audio_engine=SimpleNamespace(
            schedule_voice=lambda samples, frame: spoken.append(samples)))
    for beat in range(-6, 0):                  # two bars of 3/4 at 120
        AppCore._on_beat_scheduled(core, 0, beat * 0.5, 0)
    _assert(spoken == ["word1", "word2", "word3"],
            "3/4 speaks one, two, three on its last bar")

    spoken.clear()
    core.metronome.tempo_map = TempoMap(120, meter="6/8")
    for beat in range(-12, 0):                 # two bars of 6/8 at 120
        AppCore._on_beat_scheduled(core, 0, beat * 0.25, 0)
    _assert(spoken == words[:6], "6/8 speaks its last bar's six eighths")

    spoken.clear()
    core._count_words = words[:4]              # an older cache
    for beat in range(-12, 0):
        AppCore._on_beat_scheduled(core, 0, beat * 0.25, 0)
    _assert(spoken == [], "a meter longer than the cached words stays silent")


# ----------------------------------------------------------------------
# The song's tempo, from the popup
# ----------------------------------------------------------------------
def _song_core():
    config = Config()
    config.playlist.songs = [SongConfig(id="a", title="A")]
    config.playlist.current_song_index = 0
    saves = []
    return SimpleNamespace(config=config, bpm_state=BpmState(),
                           save_config=lambda: saves.append(True)), saves


def test_set_song_tempo():
    print("test_set_song_tempo")
    core, saves = _song_core()
    maps = []
    core.bpm_state.tempo_map_changed.connect(maps.append)

    AppCore.set_song_tempo(core, 96, "6/8")
    song = core.config.get_current_song()
    _assert(song.meter == "6/8" and saves == [True],
            "the meter is written onto the song and saved")
    _assert(core.bpm_state.get_bpm() == 96, "the BPM goes through bpm_state")
    _assert(maps[-1].meter == "6/8" and maps[-1].bpm_at(0.0) == 96,
            "the map the ruler re-grids from carries both")
    _assert(maps[-1].bar_seconds_at(0.0) == 1.875,
            "a 6/8 bar at quarter = 96 is six 0.3125 s eighths")

    AppCore.set_song_tempo(core, 96, "6/8")
    _assert(saves == [True], "setting what is already set writes nothing")

    AppCore.set_song_tempo(core, 110, "bogus")
    _assert(song.meter == "4/4", "an unknown meter lands as 4/4")

    core.config.playlist.songs = []
    AppCore.set_song_tempo(core, 150, "3/4")
    _assert(core.bpm_state.get_bpm() == 110, "no song, nothing to set")


def test_bpm_state_meter():
    print("test_bpm_state_meter")
    state = BpmState()
    maps = []
    state.tempo_map_changed.connect(maps.append)
    state.set_meter("4/4")
    _assert(maps == [], "an unchanged meter is a no-op")
    state.set_meter("6/8")
    _assert(len(maps) == 1 and maps[0].meter == "6/8", "a new meter rebuilds")
    state.set_bpm(100)
    _assert(maps[-1].meter == "6/8" and maps[-1].bpm_at(0.0) == 100,
            "and survives a BPM change")
    state.set_meter("nonsense")
    _assert(maps[-1].meter == "4/4", "an unknown meter is 4/4")


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------
def test_meter_rides_the_song_entry():
    print("test_meter_rides_the_song_entry")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Gira.show"
        root.mkdir()
        show = Show(root=root, name="Gira")
        show.playlist.songs = [SongConfig(id="a", meter="7/4"), SongConfig(id="b")]
        data = show_to_manifest(show)
        _assert([s["meter"] for s in data["songs"]] == ["7/4", "4/4"],
                "show.json writes each song's meter")

        loaded = show_from_manifest(root, data)
        _assert([s.meter for s in loaded.playlist.songs] == ["7/4", "4/4"],
                "and reads it back")

        del data["songs"][0]["meter"]
        data["songs"][1]["meter"] = "9/8"
        loaded = show_from_manifest(root, data)
        _assert([s.meter for s in loaded.playlist.songs] == ["4/4", "4/4"],
                "a song saved before meters, or with an unknown one, is 4/4")


def test_the_markers_dialog_steps_the_meter():
    print("test_the_markers_dialog_steps_the_meter")
    from ui.markers_dialog import MODE_BARBEAT, TimeSpin
    spin = TimeSpin()
    spin.set_context(MODE_BARBEAT, TempoMap(120, meter="3/4"))
    spin.set_seconds(1.0)                      # bar 1, beat 3
    _assert(abs(spin.value() - 1.3) < 1e-9, "3/4 shows bar 1 beat 3")
    spin.stepBy(1)
    _assert(abs(spin.value() - 2.1) < 1e-9 and spin.seconds() == 1.5,
            "the next beat is bar 2 beat 1, not 1.4")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
