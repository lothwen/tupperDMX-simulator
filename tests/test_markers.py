"""
Characterization test: the song marker track (voice cues + control markers).

Covers the TempoMap in both directions, the ControlMarkerConfig model and its
show-manifest round-trip, core.markers' resolution helpers, MediaController's
marker/segue firing, and AudioController's beat scheduling across a tempo
change — all with fakes: no audio device, no TTS, no GL.

Run with:
    uv run python tests/test_markers.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from core import markers as core_markers
from core.config import Config, ControlMarkerConfig, PlaylistConfig, SongConfig
from core.show.manifest import show_from_manifest, show_to_manifest
from core.show.model import Show
from core.tempo_map import TempoMap

from test_count_in import FakeEngine, FakeMidiManager


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _close(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ----------------------------------------------------------------------
# TempoMap
# ----------------------------------------------------------------------
def test_tempo_map_constant():
    print("test_tempo_map_constant")
    m = TempoMap(120)
    _assert(m.is_constant(), "a map with no changes is constant")
    _assert(_close(m.beat_at(2.0), 4.0), "120 BPM: 2s is beat 4")
    _assert(_close(m.seconds_at_beat(4), 2.0), "and beat 4 is 2s (inverse)")
    _assert(m.bar_beat_at(2.0) == (2, 1), "2s is bar 2 beat 1")
    _assert(_close(m.seconds_at_bar_beat(2, 1), 2.0), "bar 2.1 is 2s")
    _assert(_close(m.shortest_bar_seconds(), 2.0), "one bar is 2s at 120 BPM")


def test_tempo_map_change():
    print("test_tempo_map_change")
    # 120 BPM until 8s (beat 16), then 60 BPM (one beat per second).
    m = TempoMap(120, [(8.0, 60)])
    _assert(not m.is_constant(), "a change makes the map non-constant")
    _assert(m.bpm_at(4.0) == 120 and m.bpm_at(10.0) == 60,
            "bpm_at reads the segment the position falls in")
    _assert(_close(m.beat_at(8.0), 16.0), "the change lands on beat 16")
    _assert(_close(m.beat_at(10.0), 18.0),
            "beats after the change are counted at the NEW tempo")
    _assert(_close(m.seconds_at_beat(18), 10.0), "and invert back exactly")
    _assert(m.bar_beat_at(10.0) == (5, 3),
            "bar numbering continues across the change, it does not restart")
    _assert(_close(m.shortest_bar_seconds(), 2.0),
            "the shortest bar is the fastest segment's, not the last one's")


def test_tempo_map_edges():
    print("test_tempo_map_edges")
    m = TempoMap(120, [(8.0, 60)])
    _assert(_close(m.seconds_at_beat(-8), -4.0),
            "negative beats extrapolate backwards at the FIRST tempo "
            "(what sizes the count-in)")

    at_zero = TempoMap(120, [(0.0, 90)])
    _assert(at_zero.bpm_at(0.0) == 90 and at_zero.is_constant(),
            "a change at t=0 means the song simply starts at that tempo")
    _assert(_close(at_zero.seconds_at_beat(-4), -4 * 60.0 / 90),
            "and the count-in counts at it too")

    unsorted_map = TempoMap(120, [(20.0, 100), (10.0, 80)])
    _assert(unsorted_map.changes() == [(10.0, 80), (20.0, 100)],
            "changes are ordered however they arrive")

    _assert(TempoMap(9999).bpm_at(0.0) == 300, "BPM clamps to the musical range")


def test_tempo_map_grid():
    print("test_tempo_map_grid")
    m = TempoMap(120, [(8.0, 60)])
    lines = list(m.grid_lines(12.0, divisions=4))
    seconds = [round(s, 3) for s, _bar, _is_bar in lines]
    _assert(seconds[:5] == [0.0, 0.5, 1.0, 1.5, 2.0],
            "beat lines are half a second apart at 120 BPM")
    _assert(seconds[16:19] == [8.0, 9.0, 10.0],
            "and a second apart after the 60 BPM marker")
    bars = [bar for _s, bar, is_bar in lines if is_bar]
    _assert(bars[:3] == [1, 2, 3], "every fourth line starts a bar")
    _assert(all(s <= 12.0 for s in seconds), "the walk stops at the requested end")

    _assert(_close(m.nearest_grid_seconds(9.4, 4), 9.0),
            "snapping past the change rounds on the NEW spacing")
    _assert(_close(m.nearest_grid_seconds(1.6, 4), 1.5),
            "and on the old spacing before it")

    bar_only = [s for s, _b, _i in m.grid_lines(12.0, divisions=1)]
    _assert(bar_only[:3] == [0.0, 2.0, 4.0], "divisions=1 draws bar lines only")


# ----------------------------------------------------------------------
# Model + persistence
# ----------------------------------------------------------------------
def test_parse_control_markers():
    print("test_parse_control_markers")
    markers = Config._parse_control_markers([
        {"time": 96.0, "kind": "tempo", "bpm": 132},
        {"time": 10.0, "kind": "click_stop"},
        {"time": 5.0, "kind": "tempo", "bpm": 0},     # no usable tempo: dropped
        {"time": 5.0, "kind": "reticulate_splines"},   # unknown kind: dropped
        "not a dict",                                   # malformed: dropped
        {"time": -3.0, "kind": "click_stop"},          # negative time clamps
    ])
    _assert([(m.time, m.kind) for m in markers]
            == [(0.0, "click_stop"), (10.0, "click_stop"), (96.0, "tempo")],
            "markers parse, drop the unusable and sort by time")
    _assert(markers[2].bpm == 132, "a tempo marker keeps its BPM")
    _assert(Config._parse_control_markers(None) == [], "non-list parses to empty")


def test_manifest_roundtrip():
    print("test_manifest_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Demo.show"
        root.mkdir()
        song = SongConfig(
            id="abc", title="Marea", segue=True, segue_time=210.5,
            control_markers=[ControlMarkerConfig(96.0, "tempo", 132),
                             ControlMarkerConfig(188.0, "click_stop")])
        show = Show(root=root, name="Demo", playlist=PlaylistConfig(songs=[song]))

        data = show_to_manifest(show)
        _assert(data["songs"][0]["control_markers"] == [
            {"time": 96.0, "kind": "tempo", "bpm": 132},
            {"time": 188.0, "kind": "click_stop", "bpm": 0}],
            "control markers serialize as plain dicts")
        _assert(data["songs"][0]["segue"] is True
                and data["songs"][0]["segue_time"] == 210.5,
                "the segue rides the song entry")

        back = show_from_manifest(root, data).playlist.songs[0]
        _assert([(m.time, m.kind, m.bpm) for m in back.control_markers]
                == [(96.0, "tempo", 132), (188.0, "click_stop", 0)],
                "and come back identical")
        _assert(back.segue is True and back.segue_time == 210.5,
                "so does the segue")

        # A manifest written before control markers existed still loads.
        del data["songs"][0]["control_markers"]
        del data["songs"][0]["segue"]
        del data["songs"][0]["segue_time"]
        older = show_from_manifest(root, data).playlist.songs[0]
        _assert(older.control_markers == [] and older.segue is False,
                "a pre-markers manifest loads with none, not an error")


def test_marker_helpers():
    print("test_marker_helpers")
    markers = [ControlMarkerConfig(96.0, "tempo", 132),
               ControlMarkerConfig(188.0, "click_stop"),
               ControlMarkerConfig(200.0, "click_stop"),
               ControlMarkerConfig(10.0, "tempo", 90)]
    _assert(core_markers.tempo_changes(markers) == [(10.0, 90), (96.0, 132)],
            "tempo_changes is what a TempoMap is built from")
    _assert(core_markers.click_stop_time(markers) == 188.0,
            "the EARLIEST stop-click wins (the marker is one-way)")
    _assert(not core_markers.click_stopped_at(markers, 187.9),
            "the click runs before the marker")
    _assert(core_markers.click_stopped_at(markers, 188.0),
            "and is stopped from the marker on")
    _assert(core_markers.click_stop_time([]) is None,
            "no marker means the click never stops")

    song = SongConfig(control_markers=markers, segue=True, segue_time=210.5)
    rows = core_markers.ruler_rows(song)
    _assert(rows[0] == (10.0, "tempo", "90 BPM"), "ruler rows label a tempo")
    _assert((210.5, "segue", "next song, no count-in") in rows,
            "and carry the segue, which has no other way onto the ruler")


# ----------------------------------------------------------------------
# Runtime
# ----------------------------------------------------------------------
def test_marker_firing():
    print("test_marker_firing")
    from app.media_controller import MediaController
    controller = MediaController(FakeMidiManager(), FakeEngine())

    fired = []
    controller.control_marker_fired.connect(
        lambda kind, bpm: fired.append((kind, bpm)))
    controller.set_control_markers([(20.0, "click_stop", 0),
                                    (10.0, "tempo", 132)])  # unsorted on purpose

    controller.current_timeline_time = 9.9
    controller._reset_marker_pointer(0.0)
    controller._fire_control_markers()
    _assert(fired == [], "no marker before its time")

    controller.current_timeline_time = 10.05
    controller._fire_control_markers()
    _assert(fired == [("tempo", 132)], "a marker fires on the crossing")

    controller._fire_control_markers()
    _assert(len(fired) == 1, "a marker never fires twice")

    controller.current_timeline_time = 25.0
    controller._fire_control_markers()
    _assert(fired[-1] == ("click_stop", 0), "later markers fire in order")

    fired.clear()
    controller._reset_marker_pointer(15.0)
    controller.current_timeline_time = 25.0
    controller._fire_control_markers()
    _assert(fired == [("click_stop", 0)],
            "seeking forward skips the markers behind the target")


def test_segue_arming():
    print("test_segue_arming")
    from app.media_controller import MediaController
    controller = MediaController(FakeMidiManager(), FakeEngine())

    reached = []
    controller.segue_reached.connect(lambda: reached.append(True))

    controller.set_segue(True, 100.0)
    controller.current_timeline_time = 99.0
    _assert(not controller._fire_segue() and not reached, "not yet")

    controller.current_timeline_time = 100.5
    _assert(controller._fire_segue() and len(reached) == 1, "fires on crossing")
    _assert(not controller._fire_segue() and len(reached) == 1,
            "and only once per pass")

    controller.seek_to_position(50.0)
    controller.current_timeline_time = 100.5
    _assert(controller._fire_segue(), "seeking back before it re-arms the segue")

    controller.set_segue(False, 100.0)
    controller.current_timeline_time = 200.0
    _assert(not controller._fire_segue(), "a disarmed segue never fires")

    # A song whose length is not known yet defaults its segue to 0.
    controller.set_segue(True, 0.0)
    controller.current_timeline_time = 5.0
    _assert(not controller._fire_segue(),
            "a segue at t=0 never fires — it would mean 'never play this song'")


def test_count_in_uses_the_tempo_map():
    print("test_count_in_uses_the_tempo_map")
    from app.media_controller import MediaController
    controller = MediaController(FakeMidiManager(), FakeEngine())
    controller.set_count_in(True, 2)

    controller.set_bpm(120)
    _assert(_close(controller.count_in_duration(), 4.0),
            "2 bars at 120 BPM is 4s")

    # A song that simply starts at another tempo counts in at THAT tempo.
    controller.set_tempo_map(TempoMap(120, [(0.0, 60)]))
    _assert(_close(controller.count_in_duration(), 8.0),
            "a tempo marker at t=0 sizes the count-in with it")

    # A tempo change later in the song must not touch the count-in.
    controller.set_tempo_map(TempoMap(120, [(30.0, 60)]))
    _assert(_close(controller.count_in_duration(), 4.0),
            "a mid-song tempo change leaves the count-in at the base tempo")


def test_metronome_across_a_tempo_change():
    print("test_metronome_across_a_tempo_change")
    from app.audio_controller import AudioController
    engine = FakeEngine()
    controller = AudioController(engine)
    controller.set_tempo_map(TempoMap(120, [(8.0, 60)]))

    # Start on the first count-in beat of a 2-bar count-in at 120 BPM.
    controller.start_metronome(-4.0)
    _assert(engine.clicks == [True],
            "a count-in starts ON the downbeat, not mid-bar")

    beats = [controller.beat_count]
    for _ in range(7):
        controller._play_metronome_beep()
        beats.append(controller.beat_count)
    _assert(beats == [2, 3, 4, 1, 2, 3, 4, 1],
            "the 1-2-3-4 cycle runs unbroken from negative time into the song")

    tempo_map = controller.tempo_map
    before = tempo_map.seconds_at_beat(16) - tempo_map.seconds_at_beat(15)
    after = tempo_map.seconds_at_beat(17) - tempo_map.seconds_at_beat(16)
    _assert(_close(before, 0.5) and _close(after, 1.0),
            "the beat interval the scheduler adds changes at the marker")

    # The scheduler needs no nudging at the marker: the map it reads already
    # carries the change, which is why nothing restarts it there.
    engine.clicks.clear()
    controller.start_metronome(0.0)
    landed = [0.0]
    for _ in range(20):
        controller._play_metronome_beep()
        landed.append(controller.tempo_map.seconds_at_beat(controller._beat_index))
    intervals = [round(landed[i + 1] - landed[i], 3) for i in range(14, 19)]
    _assert(intervals == [0.5, 1.0, 1.0, 1.0, 1.0],
            "beats retime themselves at the marker with no restart")
    _assert(len(engine.clicks) == 21,
            "and no beat is played twice on the way through it")

    controller.stop_metronome()
    _assert(controller.beat_count == 0, "stopping resets the beat")


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    test_tempo_map_constant()
    test_tempo_map_change()
    test_tempo_map_edges()
    test_tempo_map_grid()
    test_parse_control_markers()
    test_manifest_roundtrip()
    test_marker_helpers()
    test_marker_firing()
    test_segue_arming()
    test_count_in_uses_the_tempo_map()
    test_metronome_across_a_tempo_change()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
