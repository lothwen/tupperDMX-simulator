"""
Characterization test: web-remote state building (Qt-free).

Covers compute_sections (span derivation from song-part markers),
build_setlist (titles/probe tolerance), build_state (schema shape) and
peaks._downsample (bin maxima + normalization) — all pure functions, no
sockets, no Qt.

Run with:
    uv run python tests/test_webremote_state.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from webremote.state import (compute_sections, build_setlist, build_state,
                             probe_duration, probe_bpm)
from webremote.peaks import _downsample


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


class FakeSong:
    def __init__(self, audio=None, midi=None):
        self.audio = audio
        self.midi = midi


def test_compute_sections():
    print("test_compute_sections")
    _assert(compute_sections([], 100.0) == [], "no markers → no sections")

    sections = compute_sections([(10.0, "INTRO", "#c77f37")], 100.0)
    _assert(sections == [{"name": "INTRO", "color": "#c77f37",
                          "start": 10.0, "end": 100.0}],
            "single marker spans to song end")

    sections = compute_sections(
        [(50.0, "CHORUS", "#37c737"), (0.0, "INTRO", "#c77f37"),
         (20.0, "VERSE", "#c6c737")], 90.0)
    _assert([s["name"] for s in sections] == ["INTRO", "VERSE", "CHORUS"],
            "markers are sorted by time")
    _assert(sections[0]["end"] == 20.0 and sections[1]["end"] == 50.0,
            "each section ends where the next starts")
    _assert(sections[2]["end"] == 90.0, "last section ends at song end")

    sections = compute_sections(
        [(0.0, "INTRO", "#c77f37"), (120.0, "OUTRO", "#c79337")], 100.0)
    _assert([s["name"] for s in sections] == ["INTRO"],
            "markers past song end are dropped")

    sections = compute_sections([(-5.0, "INTRO", "#c77f37")], 100.0)
    _assert(sections[0]["start"] == 0.0, "negative marker time clamps to 0")


def test_build_setlist():
    print("test_build_setlist")
    songs = [FakeSong(audio="/no/such/dir/Marea.mp3"),
             FakeSong(midi="/no/such/dir/Cenizas.mid"),
             FakeSong()]
    setlist = build_setlist(songs, 1)

    _assert(setlist["current"] == 1, "current index is passed through")
    _assert(setlist["songs"][0]["title"] == "Marea",
            "title is the audio basename without extension")
    _assert(setlist["songs"][1]["title"] == "Cenizas",
            "title falls back to the MIDI basename")
    _assert(setlist["songs"][2]["title"] == "Untitled",
            "pathless song gets a placeholder title")

    titled = FakeSong(audio="/no/such/dir/5-el-acantilado.mp3")
    titled.title = "El Acantilado"
    _assert(build_setlist([titled], 0)["songs"][0]["title"] == "El Acantilado",
            "an explicit show-manifest title wins over the basename")
    _assert(all(s["duration"] is None and s["bpm"] is None
                for s in setlist["songs"]),
            "unreadable files probe to None (client renders '—')")
    _assert([s["index"] for s in setlist["songs"]] == [0, 1, 2],
            "songs carry their setlist index")


def test_probes_missing_files():
    print("test_probes_missing_files")
    _assert(probe_duration(None) is None, "duration of None path is None")
    _assert(probe_duration("/no/such.mp3") is None,
            "duration of a missing file is None")
    _assert(probe_bpm("/no/such.mid") is None,
            "bpm of a missing file is None")


def test_build_state_schema():
    print("test_build_state_schema")
    state = build_state(
        playing=True, position=12.5, duration=200.0, bpm=142, blackout=False,
        setlist={"current": 0, "songs": []},
        sections=[], cues=[{"index": 0, "label": "Warm", "color": "#fff"}])

    expected_keys = {"type", "show_name", "playing", "position", "duration",
                     "bpm", "beats_per_bar", "blackout", "setlist",
                     "sections", "cues"}
    _assert(set(state) == expected_keys, "state message has the frozen schema keys")
    _assert(state["type"] == "state", "type tag is 'state'")
    _assert(state["show_name"] is None, "show name is a Phase-3 placeholder (null)")
    _assert(state["beats_per_bar"] == 4, "beats_per_bar is constant 4 this phase")
    _assert(state["bpm"] == 142 and state["position"] == 12.5,
            "values pass through")

    state = build_state(playing=False, position=0.0, duration=None, bpm=120,
                        blackout=True, setlist={"current": 0, "songs": []},
                        sections=[], cues=[])
    _assert(state["duration"] is None, "unknown duration serializes as null")
    _assert(state["blackout"] is True, "blackout flag passes through")


def test_downsample():
    print("test_downsample")
    ramp = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
    bins = _downsample(ramp, 4)
    _assert(bins == [0.25, 0.5, 0.75, 1.0],
            "abs-max per bin, normalized so the peak is 1.0")

    bins = _downsample(np.array([-4.0, 1.0, 2.0, 1.0]), 2)
    _assert(bins == [1.0, 0.5], "negative samples count by absolute value")

    bins = _downsample(np.array([]), 4)
    _assert(bins == [0.0, 0.0, 0.0, 0.0], "empty input yields silent bins")

    bins = _downsample(np.zeros(16), 4)
    _assert(bins == [0.0] * 4, "all-zero input does not divide by zero")


def main():
    test_compute_sections()
    test_build_setlist()
    test_probes_missing_files()
    test_build_state_schema()
    test_downsample()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
