"""
Characterization test: importing songs from another show
(core/show/song_import.py).

Covers listing the other shows beside the open one, reading a show WITHOUT
opening it (the global fixture-defs layer and the source folder untouched),
the per-MIDI-channel patch comparison, and the import itself: fresh ids, every
song field carried, the .mid always copied into midis/, audio either copied
into audio/ (de-duplicated names, a shared file copied once) or referenced in
place, and missing source files reported rather than fatal.

Run with:
    uv run python tests/test_song_import.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mido
import pytest

from core import fixture_library
from core.config import FixtureInstance, SongConfig, VoiceTTSConfig
from core.show import song_import, store
from core.show.model import ShowError


@pytest.fixture(autouse=True)
def _reset_globals():
    yield
    fixture_library.set_show_fixtures_dir(None)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _write_midi(path, bpm=120):
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    track = mido.MidiTrack()
    track.append(mido.MetaMessage('set_tempo', tempo=mido.bpm2tempo(bpm), time=0))
    mid.tracks.append(track)
    mid.save(path)


def _write(path, data=b"audio"):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(data)
    return str(path)


def _source_show(tmp):
    """A show with a fully-featured song, a song sharing one file as backing
    AND guide, and a song whose files are all gone."""
    src = store.create_show(tmp, "Gira")
    external = _write(Path(tmp) / "music" / "marea.mp3", b"backing")
    in_show = _write(src.audio_dir / "marea-guia.mp3", b"guide")
    full = SongConfig(id="s1", title="Marea", audio=external, guide=in_show,
                      midi=str(src.midis_dir / "marea.mid"), offset=0.5,
                      guide_offset=-0.25, key="Am", precount=False,
                      meter="6/8", segue=True, segue_time=200.0,
                      enabled=False, cache={"bpm": 97},
                      voice_tts=[VoiceTTSConfig(time=4.0, text="coro")])
    _write_midi(full.midi, bpm=97)
    shared = _write(Path(tmp) / "music" / "faro.wav", b"both")
    twice = SongConfig(id="s2", title="Faro", audio=shared, guide=shared)
    gone = SongConfig(id="s3", title="Humo", audio=str(Path(tmp) / "nope.mp3"),
                      midi=str(src.midis_dir / "nope.mid"))
    src.playlist.songs.extend([full, twice, gone])
    src.fixtures.append(FixtureInstance(type="ledbar", dmx_start=1, channel=0))
    src.fixtures.append(FixtureInstance(type="blinder", dmx_start=20, channel=1))
    store.save_manifest(src)
    return src


def test_other_shows_and_peek():
    print("test_other_shows_and_peek")
    with tempfile.TemporaryDirectory() as tmp:
        _source_show(tmp)
        target = store.create_show(tmp, "Directo")
        (Path(tmp) / "not-a-show").mkdir()

        listed = song_import.other_shows(tmp, exclude_root=target.root)
        _assert([p.name for p in listed] == ["Gira.show"],
                "lists the other show folders, not the open one or plain dirs")

        before = fixture_library._show_fixtures_dir
        contents = sorted(p.name for p in listed[0].iterdir())
        show, raw = song_import.peek_show(listed[0])
        _assert(fixture_library._show_fixtures_dir == before,
                "peeking leaves the open show's fixture layer active")
        _assert(sorted(p.name for p in listed[0].iterdir()) == contents,
                "and creates nothing inside the other show")
        _assert([s.title for s in show.playlist.songs] == ["Marea", "Faro", "Humo"],
                "its songs are read, in setlist order")
        _assert(show.fixtures == [] and [f["type"] for f in raw]
                == ["ledbar", "blinder"], "its patch comes back raw")
        _assert(show.playlist.songs[0].audio == str(Path(tmp) / "music" / "marea.mp3")
                and show.playlist.songs[0].guide.startswith(str(listed[0])),
                "song paths resolve to absolute ones")

        (Path(tmp) / "Roto.show").mkdir()
        (Path(tmp) / "Roto.show" / "show.json").write_text("{nope")
        with pytest.raises(ShowError):
            song_import.peek_show(Path(tmp) / "Roto.show")
        print("  ok: an unreadable manifest raises ShowError")


def test_patch_differences():
    print("test_patch_differences")
    target = [FixtureInstance(type="ledbar", dmx_start=1, channel=0),
              FixtureInstance(type="blinder", dmx_start=20, channel=1, mode="4ch"),
              FixtureInstance(type="hazer", dmx_start=40, channel=5)]
    source = [{"type": "ledbar", "channel": 0, "mode": ""},
              {"type": "blinder", "channel": 1, "mode": ""},
              {"type": "wash", "channel": 3},
              {"type": "broken"}]
    _assert(song_import.patch_differences(source, target) == [1, 3],
            "a different mode and a missing fixture differ; a match, a "
            "target-only fixture and a channel-less entry do not")


def test_import_copying_audio():
    print("test_import_copying_audio")
    with tempfile.TemporaryDirectory() as tmp:
        _source_show(tmp)
        target = store.create_show(tmp, "Directo")
        _write(target.audio_dir / "marea.mp3", b"already here")
        source, _ = song_import.peek_show(Path(tmp) / "Gira.show")
        full, twice, gone = source.playlist.songs

        new, warnings = song_import.import_songs(
            target, source.playlist.songs, copy_audio=True)
        a, b, c = new

        _assert(len({a.id, b.id, c.id, full.id}) == 4, "every copy gets a fresh id")
        _assert((a.title, a.key, a.precount, a.meter, a.offset, a.guide_offset,
                 a.segue, a.segue_time, a.enabled, a.cache)
                == ("Marea", "Am", False, "6/8", 0.5, -0.25, True, 200.0,
                    False, {"bpm": 97}), "every song field is carried")
        _assert([v.text for v in a.voice_tts] == ["coro"]
                and a.voice_tts[0] is not full.voice_tts[0],
                "voice lines carried as copies")
        _assert(Path(a.midi).parent == target.midis_dir
                and Path(a.midi).name == "marea.mid"
                and Path(a.midi).read_bytes() == Path(full.midi).read_bytes(),
                "the .mid is copied into this show's midis/")
        _assert(Path(a.audio) == target.audio_dir / "marea-2.mp3"
                and Path(a.audio).read_bytes() == b"backing",
                "the backing is copied, renamed past an existing file")
        _assert(Path(a.guide) == target.audio_dir / "marea-guia.mp3",
                "the other show's own audio is copied too")
        _assert(b.audio == b.guide == str(target.audio_dir / "faro.wav"),
                "a file used as backing AND guide is copied once")
        _assert(c.midi is None and c.audio == gone.audio,
                "missing files: .mid dropped, audio path kept for relinking")
        _assert(len(warnings) == 2 and all("Humo" in w for w in warnings),
                "both missing files are reported")
        _assert(full.audio == str(Path(tmp) / "music" / "marea.mp3"),
                "the source songs are left untouched")


def test_import_referencing_audio():
    print("test_import_referencing_audio")
    with tempfile.TemporaryDirectory() as tmp:
        _source_show(tmp)
        target = store.create_show(tmp, "Directo")
        source, _ = song_import.peek_show(Path(tmp) / "Gira.show")
        full = source.playlist.songs[0]

        (a,), warnings = song_import.import_songs(target, [full], copy_audio=False)
        _assert(a.audio == full.audio and a.guide == full.guide,
                "audio keeps pointing at the original files")
        _assert(not any(target.audio_dir.iterdir()), "nothing copied to audio/")
        _assert(Path(a.midi).parent == target.midis_dir,
                "the .mid is still copied: the show owns its sequences")
        _assert(warnings == [], "no warnings")

        target.playlist.songs.append(a)
        store.save_manifest(target)
        saved = json.loads(target.manifest_path.read_text())["songs"][0]
        _assert(os.path.isabs(saved["audio"]) and saved["midi"] == "midis/marea.mid",
                "show.json stores the reference absolute, the owned .mid relative")


def main():
    try:
        test_other_shows_and_peek()
        test_patch_differences()
        test_import_copying_audio()
        test_import_referencing_audio()
    finally:
        fixture_library.set_show_fixtures_dir(None)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
