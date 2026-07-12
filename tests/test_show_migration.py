"""
Characterization test: startup show resolution + one-time legacy migration
(core/show/migrate.py).

Covers the happy migration of a legacy userprefs playlist/patch/cue set into a
default show (owned .mid copies, referenced audio, slimmed prefs), idempotence
via last_show_path (second launch reopens the same show), the clean-start path
(empty default show with the seed patch + default cues), fallback through
recent_shows when last_show_path is stale, and default-name collision handling.

Run with:
    uv run python tests/test_show_migration.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import config, fixture_library
from core.config import Config
from core.show import store
from core.show.migrate import DEFAULT_SHOW_NAME, open_or_create_startup_show


@pytest.fixture(autouse=True)
def _restore_globals():
    """Restore the config path helpers and clear the show-local fixture-defs
    layer that store.load_show/create_show activate as a side effect."""
    orig_shipped, orig_prefs = config.shipped_config_path, config.userprefs_path
    yield
    config.shipped_config_path, config.userprefs_path = orig_shipped, orig_prefs
    fixture_library.set_show_fixtures_dir(None)


SHIPPED = {
    "fixtures": [
        {"type": "ledbar", "dmx_start": 1, "bars": [{"x": 10, "y": 20}]},
        {"type": "blinder", "dmx_start": 33, "x": 100, "y": 200},
    ],
}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _patch_paths(tmp):
    shipped = Path(tmp) / "config.json"
    prefs = Path(tmp) / "userprefs.json"
    config.shipped_config_path = lambda: str(shipped)
    config.userprefs_path = lambda: prefs
    shipped.write_text(json.dumps(SHIPPED))
    return shipped, prefs


def _legacy_prefs(tmp):
    """Legacy userprefs with a 2-song playlist (real files) + an owned patch."""
    guias = Path(tmp) / "guias"
    guias.mkdir()
    files = {}
    for base in ("marea", "acantilado"):
        (guias / f"{base}.mp3").write_bytes(b"audio-" + base.encode())
        (guias / f"{base}.mid").write_bytes(b"midi-" + base.encode())
        files[base] = (str(guias / f"{base}.mp3"), str(guias / f"{base}.mid"))
    return {
        "playlist": {
            "songs": [
                {"audio": files["marea"][0], "midi": files["marea"][1], "offset": 0.25},
                {"audio": files["acantilado"][0], "midi": files["acantilado"][1]},
            ],
            "current_song_index": 1,
        },
        "fixtures": [
            {"type": "blinder", "dmx_start": 65, "name": "Solo", "channel": 3},
        ],
    }, files


def test_migrates_legacy_playlist_and_patch():
    print("test_migrates_legacy_playlist_and_patch")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        legacy, files = _legacy_prefs(tmp)
        prefs.write_text(json.dumps(legacy))
        shows_dir = Path(tmp) / "shows"

        cfg = Config.load()
        show = open_or_create_startup_show(cfg, shows_dir=shows_dir)

        _assert(show.root == shows_dir / f"{DEFAULT_SHOW_NAME}.show",
                "default show created under the shows dir")
        _assert(show.playlist is cfg.playlist,
                "the show adopts the parsed playlist object (aliasing-ready)")
        _assert(show.playlist.current_song_index == 1, "current index preserved")

        s0, s1 = show.playlist.songs
        _assert(s0.title == "marea" and s1.title == "acantilado",
                "titles derived from the audio basenames")
        _assert(s0.id and s1.id and s0.id != s1.id, "stable uuids assigned")
        _assert(s0.audio == files["marea"][0],
                "audio stays referenced in place (absolute path)")
        _assert(s0.midi == str(show.midis_dir / "marea.mid"),
                "midi is now owned by the show (midis/)")
        _assert(Path(s0.midi).read_bytes() == b"midi-marea",
                "midi copied byte-identical")
        _assert(Path(files["marea"][1]).exists() and Path(files["acantilado"][1]).exists(),
                "the user's original .mid files are untouched")
        _assert(abs(s0.offset - 0.25) < 1e-9, "song offset preserved")

        _assert(len(show.fixtures) == 1 and show.fixtures[0].name == "Solo"
                and show.fixtures[0].channel == 3,
                "the prefs-owned patch moved into the show")
        _assert(len(show.cues) == 6, "the cue set moved into the show")
        show.cues[0].label = "EDITED"
        _assert(cfg.webremote.cues[0].label != "EDITED",
                "show cues are a deep copy (prefs keep the new-show defaults)")

        _assert(cfg.last_show_path == str(show.root), "last_show_path recorded")
        _assert(cfg.recent_shows[0] == str(show.root), "recent_shows updated")
        data = json.loads(prefs.read_text())
        _assert("playlist" not in data and "fixtures" not in data,
                "userprefs slimmed: playlist/fixtures dropped after migration")
        _assert(data["last_show_path"] == str(show.root),
                "slimmed userprefs points at the migrated show")

        manifest = json.loads((show.root / "show.json").read_text())
        _assert(manifest["songs"][0]["midi"] == "midis/marea.mid",
                "manifest stores owned midi relative")


def test_second_launch_reopens_same_show():
    print("test_second_launch_reopens_same_show")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        legacy, _ = _legacy_prefs(tmp)
        prefs.write_text(json.dumps(legacy))
        shows_dir = Path(tmp) / "shows"

        show = open_or_create_startup_show(Config.load(), shows_dir=shows_dir)

        # Second launch: prefs now carry last_show_path and no playlist.
        cfg2 = Config.load()
        _assert(cfg2.playlist.songs == [], "slimmed prefs carry no playlist")
        show2 = open_or_create_startup_show(cfg2, shows_dir=shows_dir)
        _assert(show2.root == show.root, "second launch reopens the same show")
        _assert(len(list(shows_dir.iterdir())) == 1,
                "no second show folder created (idempotent)")
        _assert(len(show2.playlist.songs) == 2
                and show2.playlist.songs[0].midi == str(show2.midis_dir / "marea.mid"),
                "songs load back from the manifest with owned midi paths")


def test_clean_start_creates_empty_default_show():
    print("test_clean_start_creates_empty_default_show")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shows_dir = Path(tmp) / "shows"

        cfg = Config.load()  # first launch: creates default prefs
        show = open_or_create_startup_show(cfg, shows_dir=shows_dir)
        _assert(show.playlist.songs == [], "clean start: empty setlist")
        _assert(len(show.fixtures) == 2 and show.fixtures[0].type == "ledbar",
                "clean start: show seeded with the shipped patch")
        _assert(len(show.cues) == 6, "clean start: show seeded with default cues")


def test_stale_pointers_fall_back():
    print("test_stale_pointers_fall_back")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shows_dir = Path(tmp) / "shows"
        valid = store.create_show(tmp, "Valida")

        prefs.write_text(json.dumps({
            "last_show_path": str(Path(tmp) / "gone.show"),
            "recent_shows": [str(Path(tmp) / "gone2.show"), str(valid.root)],
        }))
        cfg = Config.load()
        show = open_or_create_startup_show(cfg, shows_dir=shows_dir)
        _assert(show.root == valid.root,
                "stale last_show_path falls back through recent_shows")
        _assert(cfg.last_show_path == str(valid.root),
                "the recovered show becomes the new last_show_path")

        # Nothing valid at all -> a fresh default show, no crash.
        prefs.write_text(json.dumps({
            "last_show_path": str(Path(tmp) / "gone.show"),
        }))
        cfg2 = Config.load()
        show2 = open_or_create_startup_show(cfg2, shows_dir=shows_dir)
        _assert(show2.root.parent == shows_dir and show2.playlist.songs == [],
                "no loadable candidate -> fresh default show")


def test_default_name_collision_uniquified():
    print("test_default_name_collision_uniquified")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shows_dir = Path(tmp) / "shows"
        (shows_dir / f"{DEFAULT_SHOW_NAME}.show").mkdir(parents=True)  # squatter

        cfg = Config.load()
        show = open_or_create_startup_show(cfg, shows_dir=shows_dir)
        _assert(show.root == shows_dir / f"{DEFAULT_SHOW_NAME} 2.show",
                "existing default folder bumps the show name")


def main():
    orig_shipped, orig_prefs = config.shipped_config_path, config.userprefs_path
    try:
        for test in (test_migrates_legacy_playlist_and_patch,
                     test_second_launch_reopens_same_show,
                     test_clean_start_creates_empty_default_show,
                     test_stale_pointers_fall_back,
                     test_default_name_collision_uniquified):
            test()
            config.shipped_config_path, config.userprefs_path = orig_shipped, orig_prefs
            fixture_library.set_show_fixtures_dir(None)
    finally:
        config.shipped_config_path, config.userprefs_path = orig_shipped, orig_prefs
        fixture_library.set_show_fixtures_dir(None)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
