"""
Characterization test: web-remote state building (Qt-free).

Covers compute_sections (span derivation from song-part markers),
build_setlist (titles/probe tolerance), build_state (schema shape) and
core.waveform.peak_envelope (bin maxima + normalization) as the remote's
/api/waveform uses it — all pure functions, no sockets, no Qt.

Run with:
    uv run python tests/test_webremote_state.py
"""

import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from core.paths import default_shows_dir
from webremote.state import (compute_sections, build_setlist, build_state,
                             build_shows, resolve_show, shows_dir,
                             probe_duration, probe_bpm, song_duration)
from core.waveform import peak_envelope
from webremote.peaks import compute_peaks


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


class FakeSong:
    def __init__(self, audio=None, midi=None, guide=None):
        self.audio = audio
        self.midi = midi
        self.guide = guide


def _wav(path, seconds, rate=8000):
    """A real (silent) WAV so mutagen has a header to read."""
    import wave
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return str(path)


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
    _assert(all(s["key"] is None and s["precount"] is True
                for s in setlist["songs"]),
            "songs without metadata expose null key and default precount")

    keyed = FakeSong(audio="/no/such/dir/Marea.mp3")
    keyed.key = "Am"
    keyed.precount = False
    entry = build_setlist([keyed], 0)["songs"][0]
    _assert(entry["key"] == "Am" and entry["precount"] is False,
            "per-song key and precount serialize into the setlist")


def test_probes_missing_files():
    print("test_probes_missing_files")
    _assert(probe_duration(None) is None, "duration of None path is None")
    _assert(probe_duration("/no/such.mp3") is None,
            "duration of a missing file is None")
    _assert(probe_bpm("/no/such.mid") is None,
            "bpm of a missing file is None")


def test_song_duration_is_the_longest_track():
    """A song is as long as its LONGEST track. Monitoring a 2-second backing
    under a full-length guide must not shorten the remote (the desktop
    timeline spans the same maximum)."""
    print("test_song_duration_is_the_longest_track")
    with tempfile.TemporaryDirectory() as tmp:
        short = _wav(Path(tmp) / "backing.wav", 2.0)
        long = _wav(Path(tmp) / "guide.wav", 8.0)

        _assert(round(song_duration(FakeSong(audio=short, guide=long))) == 8,
                "a long guide over a short backing gives the song its length")
        _assert(round(song_duration(FakeSong(audio=long, guide=short))) == 8,
                "and the other way round")
        _assert(round(song_duration(FakeSong(guide=long))) == 8,
                "a guide-only song still has a length")
        _assert(song_duration(FakeSong(audio="/no/such.mp3")) is None,
                "nothing readable probes to None")

        row = build_setlist([FakeSong(audio=short, guide=long)], 0)["songs"][0]
        _assert(round(row["duration"]) == 8,
                "the setlist row carries the same length")


def test_build_state_schema():
    print("test_build_state_schema")
    state = build_state(
        playing=True, position=12.5, duration=200.0, bpm=142, blackout=False,
        setlist={"current": 0, "songs": []},
        sections=[], cues=[{"index": 0, "label": "Warm", "color": "#fff"}])

    expected_keys = {"type", "show_name", "shows", "playing", "position",
                     "duration", "bpm", "beats_per_bar", "blackout", "countin",
                     "setlist", "sections", "cues"}
    _assert(set(state) == expected_keys, "state message has the frozen schema keys")
    _assert(state["type"] == "state", "type tag is 'state'")
    _assert(state["show_name"] is None, "show name is a Phase-3 placeholder (null)")
    _assert(state["shows"] == [], "the show list defaults to empty")
    _assert(state["beats_per_bar"] == 4, "beats_per_bar is constant 4 this phase")
    _assert(state["countin"] == {"enabled": False, "bars": 2},
            "countin defaults to disabled / 2 bars when not provided")
    _assert(state["bpm"] == 142 and state["position"] == 12.5,
            "values pass through")

    state = build_state(
        playing=True, position=-3.2, duration=200.0, bpm=142, blackout=False,
        setlist={"current": 0, "songs": []}, sections=[], cues=[],
        countin={"enabled": True, "bars": 4})
    _assert(state["countin"] == {"enabled": True, "bars": 4},
            "countin block passes through")
    _assert(state["position"] == -3.2,
            "negative position (count-in countdown) passes through")

    state = build_state(playing=False, position=0.0, duration=None, bpm=120,
                        blackout=True, setlist={"current": 0, "songs": []},
                        sections=[], cues=[])
    _assert(state["duration"] is None, "unknown duration serializes as null")
    _assert(state["blackout"] is True, "blackout flag passes through")


def test_build_shows():
    print("test_build_shows")
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)
        for name in ("Zaragoza.show", "billares.show", "Gira 2026.show"):
            (parent / name).mkdir()
            (parent / name / "show.json").write_text("{}")
        (parent / "not-a-show").mkdir()   # no manifest: not a show folder

        current = parent / "Gira 2026.show"
        _assert(shows_dir(current) == parent,
                "the menu lists the folder holding the open show")
        _assert(shows_dir(None) == default_shows_dir(),
                "with no show open it falls back to the default shows dir")

        shows = build_shows(current)
        _assert([s["name"] for s in shows] == ["billares", "Gira 2026", "Zaragoza"],
                "every sibling show folder is listed, by name, .show stripped")
        _assert([s["current"] for s in shows] == [False, True, False],
                "the open show is the marked entry")
        _assert(all(set(s) == {"name", "current"} for s in shows),
                "no filesystem path ever reaches a client")

        _assert(resolve_show("Zaragoza", current) == parent / "Zaragoza.show",
                "a display name resolves back to its folder")
        _assert(resolve_show("nope", current) is None,
                "an unknown name resolves to nothing")
        _assert(resolve_show("../../etc", current) is None,
                "a path-shaped name matches nothing (names are never joined)")

        _assert(build_shows(parent / "not-a-show" / "x.show") == [],
                "a folder holding no shows lists empty")
        _assert(build_shows(parent / "missing" / "x.show") == [],
                "a folder that doesn't exist lists empty instead of raising")


def testpeak_envelope():
    print("test_downsample")
    ramp = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
    bins = peak_envelope(ramp, 4)
    _assert(bins == [0.25, 0.5, 0.75, 1.0],
            "abs-max per bin, normalized so the peak is 1.0")

    bins = peak_envelope(np.array([-4.0, 1.0, 2.0, 1.0]), 2)
    _assert(bins == [1.0, 0.5], "negative samples count by absolute value")

    bins = peak_envelope(np.array([]), 4)
    _assert(bins == [0.0, 0.0, 0.0, 0.0], "empty input yields silent bins")

    bins = peak_envelope(np.zeros(16), 4)
    _assert(bins == [0.0] * 4, "all-zero input does not divide by zero")


def test_downsample_engine_pcm():
    print("test_downsample_engine_pcm")
    # What the engine hands over is not mono floats: int16 (n, 2) frames.
    pcm = np.zeros((8, 2), dtype=np.int16)
    pcm[1, 0] = 1000
    pcm[5, 1] = -2000
    _assert(peak_envelope(pcm, 4) == [0.5, 0.0, 1.0, 0.0],
            "stereo frames bin along time and take the louder channel")

    # abs(-32768) is -32768 in int16: the peak has to come off min/max instead,
    # and a clipped master is exactly where it would have shown up.
    clipped = np.array([[-32768, 0], [100, 100]], dtype=np.int16)
    _assert(peak_envelope(clipped, 2) == [1.0, 100 / 32768.0],
            "a full-scale negative sample is the peak, not a negative one")


def test_compute_peaks_never_reads_the_file():
    print("test_compute_peaks_never_reads_the_file")
    pcm = np.zeros((16, 2), dtype=np.int16)
    pcm[3] = 500
    # A path that does not exist: it is the cache key, never opened. Decoding
    # here is what dragged librosa (and scipy, and a BLAS) into the remote.
    bins = compute_peaks("/no/such/song.mp3", pcm, bins=4)
    _assert(len(bins) == 4 and max(bins) == 1.0,
            "peaks come from the passed PCM, not from the path")

    # None would blow up in the binning, so being served proves the cache hit.
    _assert(compute_peaks("/no/such/song.mp3", None, bins=4) == bins,
            "a repeat is served from the cache without touching the PCM")


def main():
    test_compute_sections()
    test_build_setlist()
    test_probes_missing_files()
    test_song_duration_is_the_longest_track()
    test_build_state_schema()
    test_build_shows()
    testpeak_envelope()
    test_downsample_engine_pcm()
    test_compute_peaks_never_reads_the_file()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
