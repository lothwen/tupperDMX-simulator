"""
Characterization test: show.json (de)serialization (core/show/manifest.py).

Covers format_version validation (missing/invalid/future -> ShowError), song
path portability (owned .mid relative, referenced audio absolute, in-show audio
relative), the deliberate preservation of missing-media paths (unlike
Config._validate_file_path), song metadata defaults + uuid assignment, reuse of
the Config fixture/cue parsers, and the song_status() derived states.

Run with:
    uv run python tests/test_show_manifest.py
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.config import SongConfig
from core.show import model
from core.show.manifest import show_from_manifest, show_to_manifest
from core.show.model import Show, ShowError, song_status


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _empty_show(root: Path) -> Show:
    return Show(root=root, name="Test Show")


def test_format_version_validation():
    print("test_format_version_validation")
    root = Path("/tmp/does-not-matter.show")
    for bad in [{}, {"format_version": "1"}, {"format_version": 0}, []]:
        with pytest.raises(ShowError):
            show_from_manifest(root, bad)
    print("  ok: missing/invalid format_version raises ShowError")
    with pytest.raises(ShowError):
        show_from_manifest(root, {"format_version": model.FORMAT_VERSION + 1})
    print("  ok: future format_version raises ShowError")
    show = show_from_manifest(root, {"format_version": 1})
    _assert(show.format_version == 1, "current format_version accepted")


def test_song_path_portability():
    print("test_song_path_portability")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Gira.show"
        (root / model.MIDIS_DIR).mkdir(parents=True)
        (root / model.AUDIO_DIR).mkdir()
        owned_midi = root / model.MIDIS_DIR / "cancion.mid"
        owned_midi.write_bytes(b"m")
        inside_audio = root / model.AUDIO_DIR / "guia.mp3"
        inside_audio.write_bytes(b"a")
        outside_audio = Path(tmp) / "guias" / "externa.mp3"
        outside_audio.parent.mkdir()
        outside_audio.write_bytes(b"a")

        show = _empty_show(root)
        show.playlist.songs = [
            SongConfig(midi=str(owned_midi), audio=str(outside_audio), id="s1"),
            SongConfig(audio=str(inside_audio), id="s2"),
        ]
        data = show_to_manifest(show)

        _assert(data["songs"][0]["midi"] == "midis/cancion.mid",
                "owned .mid serializes relative to the show root (POSIX)")
        _assert(data["songs"][0]["audio"] == str(outside_audio),
                "referenced-in-place audio serializes absolute")
        _assert(data["songs"][1]["audio"] == "audio/guia.mp3",
                "in-show audio serializes relative")

        loaded = show_from_manifest(root, data)
        _assert(loaded.playlist.songs[0].midi == str(owned_midi),
                "relative midi resolves to absolute against the root")
        _assert(loaded.playlist.songs[0].audio == str(outside_audio),
                "absolute audio survives the roundtrip")
        _assert(loaded.playlist.songs[1].audio == str(inside_audio),
                "relative audio resolves to absolute against the root")


def test_missing_media_paths_are_preserved():
    print("test_missing_media_paths_are_preserved")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Gira.show"
        root.mkdir()
        gone = str(Path(tmp) / "moved-away" / "guia.mp3")

        show = _empty_show(root)
        show.playlist.songs = [SongConfig(audio=gone, id="s1")]
        loaded = show_from_manifest(root, show_to_manifest(show))
        song = loaded.playlist.songs[0]
        _assert(song.audio == gone,
                "a missing audio path is preserved (not nulled) for relinking")
        _assert(song_status(song) == model.STATUS_MISSING_AUDIO,
                "song_status flags the missing audio")


def test_song_metadata_defaults_and_id():
    print("test_song_metadata_defaults_and_id")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Gira.show"
        root.mkdir()
        data = {
            "format_version": 1,
            "name": "Gira",
            "songs": [
                {"title": "El Acantilado", "key": "Am", "precount": False,
                 "offset": 0.32, "cache": {"duration": 245.2, "bpm": 142}},
                {},  # everything defaulted
                "not-a-dict",  # skipped
            ],
            "current_song_index": 99,
        }
        show = show_from_manifest(root, data)
        _assert(len(show.playlist.songs) == 2, "malformed song entries are skipped")
        s0, s1 = show.playlist.songs
        _assert(s0.title == "El Acantilado" and s0.key == "Am" and s0.precount is False,
                "song metadata parsed")
        _assert(s0.cache.get("bpm") == 142, "song cache parsed")
        _assert(abs(s0.offset - 0.32) < 1e-9, "song offset parsed")
        _assert(s1.title == "" and s1.key == "" and s1.precount is True and s1.cache == {},
                "song metadata defaults are innocuous")
        _assert(s0.id and s1.id and s0.id != s1.id,
                "songs without an id get a fresh uuid each")
        _assert(show.playlist.current_song_index == 1,
                "current_song_index is clamped to the setlist")


def test_guide_track_roundtrip():
    print("test_guide_track_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Gira.show"
        (root / model.AUDIO_DIR).mkdir(parents=True)
        inside_guide = root / model.AUDIO_DIR / "mezcla.mp3"
        inside_guide.write_bytes(b"g")
        outside_guide = Path(tmp) / "estudio" / "mezcla.mp3"
        outside_guide.parent.mkdir()
        outside_guide.write_bytes(b"g")

        show = _empty_show(root)
        show.playlist.songs = [
            SongConfig(audio="/x/backing.mp3", guide=str(outside_guide),
                       guide_offset=0.75, id="s1"),
            SongConfig(audio="/x/backing2.mp3", guide=str(inside_guide), id="s2"),
        ]
        data = show_to_manifest(show)
        _assert(data["songs"][0]["guide"] == str(outside_guide),
                "referenced-in-place guide serializes absolute")
        _assert(data["songs"][1]["guide"] == "audio/mezcla.mp3",
                "in-show guide serializes relative")

        loaded = show_from_manifest(root, data)
        s0, s1 = loaded.playlist.songs
        _assert(s0.guide == str(outside_guide) and abs(s0.guide_offset - 0.75) < 1e-9,
                "guide path + guide_offset roundtrip")
        _assert(s1.guide == str(inside_guide),
                "relative guide resolves to absolute against the root")

        legacy = show_from_manifest(root, {"format_version": 1, "songs": [{}]})
        song = legacy.playlist.songs[0]
        _assert(song.guide is None and song.guide_offset == 0.0,
                "manifests without guide keys default to None/0.0")


def test_enabled_flag_roundtrip():
    print("test_enabled_flag_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Gira.show"
        root.mkdir()
        show = show_from_manifest(root, {
            "format_version": 1,
            "songs": [{"title": "legacy"}, {"title": "off", "enabled": False}],
        })
        legacy, off = show.playlist.songs
        _assert(legacy.enabled is True,
                "a manifest written before the flag loads every song enabled")
        _assert(off.enabled is False, "enabled: false is read")
        again = show_from_manifest(root, show_to_manifest(show))
        _assert([s.enabled for s in again.playlist.songs] == [True, False],
                "the flag survives a save/load round trip")


def test_show_name_fallback():
    print("test_show_name_fallback")
    root = Path("/tmp/Directo Billares 2026.show")
    show = show_from_manifest(root, {"format_version": 1})
    _assert(show.name == "Directo Billares 2026",
            "missing name falls back to the folder name without .show")


def test_fixtures_and_favorites_reuse_the_shared_parsers():
    print("test_fixtures_and_favorites_reuse_the_shared_parsers")
    root = Path("/tmp/x.show")
    data = {
        "format_version": 1,
        "fixtures": [
            {"type": "ledbar", "dmx_start": 1},
            {"type": "bogus", "dmx_start": 5},
            {"type": "par-rgbw", "dmx_start": 9, "hanging": False},
        ],
        "favorites": [
            {"kind": "note", "fixture": "ledbar", "note": 73, "velocity": 127},
            {"kind": "scene", "fixture": "ledbar", "bank": "FILLS",
             "name": "Fuego", "index": 3},
            {"kind": "bogus"},                       # unknown kind -> dropped
            {"kind": "note", "fixture": "ledbar"},   # incomplete -> dropped
        ],
        # What every show written before favourites still carries. It is read
        # by nobody: a cue that fired several fixtures at once has no
        # single-clip equivalent, so nothing is migrated.
        "webremote_cues": [
            {"label": "Warm", "color": "#ffb86b", "messages": [[0, 73, 127]]},
        ],
    }
    show = show_from_manifest(root, data)
    _assert(len(show.fixtures) == 2 and show.fixtures[0].type == "ledbar",
            "fixtures parse via Config._parse_fixtures (unknown types skipped)")
    _assert(show.fixtures[0].channel == 0, "stable channel auto-assignment applies")
    _assert(len(show.favorites) == 2,
            "favourites parse via clips.parse_favorites (malformed dropped)")
    _assert(show.favorites[0]["mode"] == "",
            "a ref without a mode gets the patch's empty default")
    # A patch written before fixtures could say which way up they are rigged
    # keeps throwing its light at the floor, which is where it already was.
    _assert(show.fixtures[0].hanging and not show.fixtures[1].hanging,
            "how a fixture is rigged is read, and defaults to flown")

    written = show_to_manifest(show)
    _assert("webremote_cues" not in written,
            "the legacy cue set is not written back")
    roundtrip = show_from_manifest(root, written)
    _assert(roundtrip.fixtures[0].dmx_start == 1
            and roundtrip.favorites == show.favorites,
            "fixtures and favourites roundtrip")
    _assert(not roundtrip.fixtures[1].hanging, "and so does the rigging")


def test_song_status_matrix():
    print("test_song_status_matrix")
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "a.mp3"
        audio.write_bytes(b"a")
        midi = Path(tmp) / "m.mid"
        midi.write_bytes(b"m")
        gone = str(Path(tmp) / "gone.file")
        cases = [
            (SongConfig(audio=str(audio), midi=str(midi)), model.STATUS_LINKED),
            (SongConfig(audio=gone, midi=str(midi)), model.STATUS_MISSING_AUDIO),
            (SongConfig(audio=str(audio), midi=gone), model.STATUS_MISSING_MIDI),
            (SongConfig(audio=str(audio)), model.STATUS_AUDIO_ONLY),
            (SongConfig(), model.STATUS_NO_AUDIO),
        ]
        for song, expected in cases:
            _assert(song_status(song) == expected, f"song_status -> {expected}")


def main():
    test_format_version_validation()
    test_song_path_portability()
    test_missing_media_paths_are_preserved()
    test_song_metadata_defaults_and_id()
    test_guide_track_roundtrip()
    test_enabled_flag_roundtrip()
    test_show_name_fallback()
    test_fixtures_and_favorites_reuse_the_shared_parsers()
    test_song_status_matrix()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
