"""
Characterization test: the show-project disk store (core/show/store.py).

Covers create_show folder layout, load/save roundtrip through show.json,
atomic_write_json (valid content, no leftover temp files), ShowError on
missing/corrupt manifests, duplicate_show (versions/ history excluded, copy
retitled, relative paths re-resolve under the new root), allocate_midi_path
uniqueness, and is_show_dir.

Run with:
    uv run python tests/test_show_store.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.config import FixtureInstance, SongConfig, WebCueConfig
from core.show import model
from core.show.model import ShowError
from core.show.store import (
    allocate_midi_path,
    atomic_write_json,
    create_show,
    duplicate_show,
    is_show_dir,
    load_show,
    save_manifest,
)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_create_show_layout():
    print("test_create_show_layout")
    with tempfile.TemporaryDirectory() as tmp:
        show = create_show(tmp, "Directo Billares 2026")
        root = Path(tmp) / "Directo Billares 2026.show"
        _assert(show.root == root, "root is Name.show under the parent")
        _assert(show.name == "Directo Billares 2026", "display name has no suffix")
        for sub in (model.MIDIS_DIR, model.AUDIO_DIR, model.FIXTURES_DIR, model.VERSIONS_DIR):
            _assert((root / sub).is_dir(), f"{sub}/ created")
        _assert((root / model.MANIFEST_NAME).is_file(), "show.json written eagerly")
        _assert(is_show_dir(root), "is_show_dir recognizes the new folder")
        _assert(not is_show_dir(tmp), "is_show_dir rejects a plain folder")

        # A name that already carries the suffix doesn't get it twice.
        show2 = create_show(tmp, "Otra Gira.show")
        _assert(show2.root.name == "Otra Gira.show" and show2.name == "Otra Gira",
                "explicit .show suffix is not doubled")

        with pytest.raises(ShowError):
            create_show(tmp, "Directo Billares 2026")
        print("  ok: creating over an existing folder raises ShowError")
        with pytest.raises(ShowError):
            create_show(tmp, "   ")
        print("  ok: empty show name raises ShowError")


def test_save_load_roundtrip():
    print("test_save_load_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        external_audio = Path(tmp) / "guias" / "cancion.mp3"
        external_audio.parent.mkdir()
        external_audio.write_bytes(b"a")

        show = create_show(tmp, "Gira",
                           fixtures=[FixtureInstance(type="ledbar", dmx_start=1, channel=0)],
                           cues=[WebCueConfig("Warm", "#ffb86b", [[0, 73, 127]])])
        midi_path = allocate_midi_path(show, "El Acantilado")
        Path(midi_path).write_bytes(b"m")
        show.playlist.songs.append(SongConfig(
            midi=midi_path, audio=str(external_audio), offset=0.32,
            id="s1", title="El Acantilado", key="Am", precount=False,
            cache={"duration": 245.2, "bpm": 142}))
        show.playlist.current_song_index = 0
        save_manifest(show)

        data = json.loads(show.manifest_path.read_text())
        _assert(data["songs"][0]["midi"] == "midis/el-acantilado.mid",
                "owned midi stored relative in show.json")

        loaded = load_show(show.root)
        _assert(loaded.name == "Gira", "name roundtrips")
        song = loaded.playlist.songs[0]
        _assert(song.midi == midi_path and song.audio == str(external_audio),
                "song paths resolve back to absolute")
        _assert(song.title == "El Acantilado" and song.key == "Am"
                and song.precount is False and song.cache["bpm"] == 142
                and abs(song.offset - 0.32) < 1e-9,
                "song metadata roundtrips")
        _assert(loaded.fixtures[0].type == "ledbar" and loaded.cues[0].label == "Warm",
                "fixtures and cues roundtrip")


def test_load_errors():
    print("test_load_errors")
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ShowError):
            load_show(Path(tmp) / "nope.show")
        print("  ok: missing show folder raises ShowError")

        broken = Path(tmp) / "broken.show"
        broken.mkdir()
        (broken / model.MANIFEST_NAME).write_text("{ not json ")
        with pytest.raises(ShowError):
            load_show(broken)
        print("  ok: corrupt show.json raises ShowError")

        future = Path(tmp) / "future.show"
        future.mkdir()
        (future / model.MANIFEST_NAME).write_text(
            json.dumps({"format_version": model.FORMAT_VERSION + 1}))
        with pytest.raises(ShowError):
            load_show(future)
        print("  ok: future format_version raises ShowError")


def test_load_self_heals_subfolders():
    print("test_load_self_heals_subfolders")
    with tempfile.TemporaryDirectory() as tmp:
        show = create_show(tmp, "Gira")
        (show.root / model.MIDIS_DIR).rmdir()
        loaded = load_show(show.root)
        _assert(loaded.midis_dir.is_dir(), "load_show recreates pruned subfolders")


def test_atomic_write_json():
    print("test_atomic_write_json")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "deep" / "nested" / "file.json"
        atomic_write_json(target, {"a": 1})
        _assert(json.loads(target.read_text()) == {"a": 1},
                "creates parent dirs and writes valid JSON")
        atomic_write_json(target, {"a": 2})
        _assert(json.loads(target.read_text()) == {"a": 2}, "overwrites in place")
        leftovers = [p for p in target.parent.iterdir() if p.suffix == ".tmp"]
        _assert(leftovers == [], "no temp files left behind")

        with pytest.raises(TypeError):
            atomic_write_json(target, {"bad": object()})
        _assert(json.loads(target.read_text()) == {"a": 2},
                "a failed write leaves the previous version intact")
        leftovers = [p for p in target.parent.iterdir() if p.suffix == ".tmp"]
        _assert(leftovers == [], "a failed write cleans up its temp file")


def test_duplicate_show():
    print("test_duplicate_show")
    with tempfile.TemporaryDirectory() as tmp:
        external_audio = Path(tmp) / "guia.mp3"
        external_audio.write_bytes(b"a")

        show = create_show(tmp, "Original")
        midi_path = allocate_midi_path(show, "Tema")
        Path(midi_path).write_bytes(b"midi-bytes")
        show.playlist.songs.append(
            SongConfig(midi=midi_path, audio=str(external_audio), id="s1"))
        save_manifest(show)
        (show.versions_dir / "2026-01-01T00-00-00").mkdir()

        dest = duplicate_show(show, tmp, "Copia")
        _assert(dest == Path(tmp) / "Copia.show", "copy lands at Copia.show")
        copy = load_show(dest)
        _assert(copy.name == "Copia", "copy is retitled")
        _assert(copy.playlist.songs[0].midi == str(dest / "midis" / "tema.mid"),
                "relative midi re-resolves under the NEW root")
        _assert(Path(copy.playlist.songs[0].midi).read_bytes() == b"midi-bytes",
                "owned midi content copied")
        _assert(copy.playlist.songs[0].audio == str(external_audio),
                "referenced audio still points at the external file")
        _assert(list(copy.versions_dir.iterdir()) == [],
                "versions/ history is not copied")

        with pytest.raises(ShowError):
            duplicate_show(show, tmp, "Copia")
        print("  ok: duplicating onto an existing folder raises ShowError")


def test_allocate_midi_path():
    print("test_allocate_midi_path")
    with tempfile.TemporaryDirectory() as tmp:
        show = create_show(tmp, "Gira")
        first = allocate_midi_path(show, "El Acantilado (v2)!")
        _assert(first == str(show.midis_dir / "el-acantilado-v2.mid"),
                "title is slugged to a safe filename")

        Path(first).write_bytes(b"m")
        second = allocate_midi_path(show, "El Acantilado (v2)!")
        _assert(second == str(show.midis_dir / "el-acantilado-v2-2.mid"),
                "existing files bump the numeric suffix")

        # A path claimed by the playlist but not yet written also counts.
        show.playlist.songs.append(SongConfig(midi=second, id="s1"))
        third = allocate_midi_path(show, "El Acantilado (v2)!")
        _assert(third == str(show.midis_dir / "el-acantilado-v2-3.mid"),
                "allocated-but-unwritten paths are not reused")

        _assert(allocate_midi_path(show, "¡¡¡") == str(show.midis_dir / "song.mid"),
                "unsluggable titles fall back to 'song'")


def main():
    test_create_show_layout()
    test_save_load_roundtrip()
    test_load_errors()
    test_load_self_heals_subfolders()
    test_atomic_write_json()
    test_duplicate_show()
    test_allocate_midi_path()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
