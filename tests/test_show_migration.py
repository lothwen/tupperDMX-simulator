"""
Characterization test: startup show resolution + one-time legacy migration
(core/show/migrate.py).

Covers the happy migration of a legacy userprefs playlist/patch/cue set into a
default show (owned .mid copies, referenced audio, slimmed prefs), idempotence
via last_show_path (second launch reopens the same show), the clean-start path
(a copy of the shipped demo show, an empty seeded show when the demo cannot be
copied, a read-only template still giving a writable copy), the demo using only
shipped fixture types, fallback through recent_shows when last_show_path is
stale, default-name collision handling, and the two ways a hand-edited
userprefs.json used to cost the operator the show it names (a UTF-8 BOM, and an
overwrite-on-parse-failure).

Run with:
    uv run python tests/test_show_migration.py
"""

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import config, fixture_library
from core.config import Config
from core.show import migrate, store
from core.show.migrate import (DEFAULT_SHOW_NAME, DEMO_SHOW_NAME,
                               open_or_create_startup_show)


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
        # The legacy prefs cue set does NOT come across: a cue that fired
        # several fixtures at once has no single-clip equivalent, and the
        # remote's buttons are now the show's starred library clips.
        _assert(show.favorites == [],
                "a migrated show starts with no favourites")

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


def _with_template(path):
    """Point the demo template at path; returns the restore callable."""
    orig = migrate.demo_template_dir
    migrate.demo_template_dir = lambda: Path(path)
    return lambda: setattr(migrate, "demo_template_dir", orig)


def test_clean_start_copies_the_demo_show():
    print("test_clean_start_copies_the_demo_show")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shows_dir = Path(tmp) / "shows"
        template = migrate.demo_template_dir()
        before = sorted(p.relative_to(template) for p in template.rglob("*"))

        cfg = Config.load()  # first launch: creates default prefs
        show = open_or_create_startup_show(cfg, shows_dir=shows_dir)
        _assert(show.root == shows_dir / f"{DEMO_SHOW_NAME}.show"
                and show.name == DEMO_SHOW_NAME,
                "clean start: the demo show is copied into the shows dir")
        _assert([s.title for s in show.playlist.songs] == ["The Final Stand"],
                "clean start: the demo's song")
        song = show.playlist.songs[0]
        root = show.root.resolve()
        _assert(all(os.path.isfile(p) and Path(p).resolve().is_relative_to(root)
                    for p in (song.midi, song.guide)),
                "its .mid and guide are files inside the copy")
        _assert(len(show.fixtures) == 11,
                "clean start: the demo's own patch, not the seed patch")
        _assert(show.favorites == [], "clean start: no favourites")
        _assert(not any(show.versions_dir.iterdir()), "no history shipped")
        _assert(sorted(p.relative_to(template) for p in template.rglob("*"))
                == before, "the template itself is left untouched")
        _assert(cfg.last_show_path == str(show.root),
                "the demo becomes last_show_path")

        cfg2 = Config.load()
        show2 = open_or_create_startup_show(cfg2, shows_dir=shows_dir)
        _assert(show2.root == show.root and len(list(shows_dir.iterdir())) == 1,
                "second launch reopens the demo, no second copy")


def test_missing_template_falls_back_to_empty_show():
    print("test_missing_template_falls_back_to_empty_show")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shows_dir = Path(tmp) / "shows"

        restore = _with_template(Path(tmp) / "nowhere.show")
        try:
            show = open_or_create_startup_show(Config.load(), shows_dir=shows_dir)
        finally:
            restore()
        _assert(show.root == shows_dir / f"{DEFAULT_SHOW_NAME}.show"
                and show.playlist.songs == [],
                "a missing demo template falls back to an empty default show")
        _assert(len(show.fixtures) == 2 and show.fixtures[0].type == "ledbar",
                "the fallback show is seeded with the shipped patch")

    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shows_dir = Path(tmp) / "shows"
        broken = Path(tmp) / "broken.show"
        broken.mkdir()
        (broken / "show.json").write_text('{"format_version": "x"}')

        restore = _with_template(broken)
        try:
            show = open_or_create_startup_show(Config.load(), shows_dir=shows_dir)
        finally:
            restore()
        _assert(show.root == shows_dir / f"{DEFAULT_SHOW_NAME}.show",
                "an unloadable demo template falls back too")
        _assert(not (shows_dir / f"{DEMO_SHOW_NAME}.show").exists(),
                "and the half-made demo copy is removed")


def test_read_only_template_yields_a_writable_show():
    print("test_read_only_template_yields_a_writable_show")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shows_dir = Path(tmp) / "shows"
        # What a packaged install looks like: read-only files in read-only dirs.
        ro = Path(tmp) / "bundle" / "demo.show"
        shutil.copytree(migrate.demo_template_dir(), ro)
        for p in sorted(ro.rglob("*"), reverse=True) + [ro]:
            p.chmod(0o555 if p.is_dir() else 0o444)

        restore = _with_template(ro)
        try:
            show = open_or_create_startup_show(Config.load(), shows_dir=shows_dir)
        finally:
            restore()
            for p in [ro] + sorted(ro.rglob("*")):
                p.chmod(stat.S_IRWXU if p.is_dir() else 0o644)
        song = show.playlist.songs[0]
        _assert(all(os.access(p, os.W_OK) for p in
                    (show.manifest_path, song.midi, song.guide, show.midis_dir)),
                "a read-only template still gives a writable show")


def test_demo_template_uses_only_shipped_fixtures():
    print("test_demo_template_uses_only_shipped_fixtures")
    template = migrate.demo_template_dir()
    data = json.loads((template / "show.json").read_text(encoding="utf-8"))
    shipped_dir = Path(fixture_library.fixtures_dir_shipped())
    unshipped = [f["type"] for f in data["fixtures"]
                 if not (shipped_dir / f"{f['type']}.json").is_file()]
    _assert(unshipped == [], "every demo patch type ships in fixtures/")
    bad_modes = [(f["type"], f["mode"]) for f in data["fixtures"]
                 if f["mode"] and f["mode"] not in fixture_library.fixture_modes(f["type"])]
    _assert(bad_modes == [], "every demo patch mode exists on its type")
    missing = [p for s in data["songs"] for p in (s["midi"], s["guide"])
               if p and not (template / p).is_file()]
    _assert(missing == [], "every file the demo manifest names is shipped")


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
        _assert(show2.root == shows_dir / f"{DEMO_SHOW_NAME}.show",
                "no loadable candidate -> a fresh copy of the demo show")


def test_hand_edited_prefs_with_a_bom_still_open_the_show():
    print("test_hand_edited_prefs_with_a_bom_still_open_the_show")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shows_dir = Path(tmp) / "shows"
        mine = store.create_show(tmp, "Billares")

        # Exactly what a Windows editor leaves behind on the rack box's SMB
        # share: valid JSON, UTF-8 BOM in front.
        prefs.write_bytes(b"\xef\xbb\xbf" + json.dumps(
            {"last_show_path": str(mine.root)}).encode("utf-8"))

        cfg = Config.load()
        _assert(cfg.last_show_path == str(mine.root),
                "a BOM does not hide last_show_path")
        show = open_or_create_startup_show(cfg, shows_dir=shows_dir)
        _assert(show.root == mine.root,
                "the show named in the prefs opens, not a new default one")
        _assert(not shows_dir.exists(), "no default show created alongside it")


def test_unparseable_prefs_are_kept_not_overwritten():
    print("test_unparseable_prefs_are_kept_not_overwritten")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shows_dir = Path(tmp) / "shows"
        mine = store.create_show(tmp, "Billares")

        # A hand-edit that broke the syntax: the pointer cannot be recovered,
        # but it must survive on disk for the operator to read back.
        broken = '{"last_show_path": "%s",,}' % mine.root
        prefs.write_text(broken)

        cfg = Config.load()
        show = open_or_create_startup_show(cfg, shows_dir=shows_dir)
        _assert(show.root.parent == shows_dir,
                "unparseable prefs still boot into a default show")
        kept = prefs.with_name(prefs.name + ".bad")
        _assert(kept.read_text() == broken,
                "the unparseable prefs were kept aside verbatim")
        _assert(json.loads(prefs.read_text())["last_show_path"] == str(show.root),
                "the rewritten prefs point at the show that actually opened")


def test_show_already_on_disk_is_adopted_not_replaced():
    print("test_show_already_on_disk_is_adopted_not_replaced")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shows_dir = Path(tmp) / "shows"
        shows_dir.mkdir()
        # What an operator copying a show onto the rack box leaves behind:
        # a show folder in the shows dir and prefs that name nothing.
        old = store.create_show(shows_dir, "Vieja")
        mine = store.create_show(shows_dir, "Billares")
        os.utime(mine.root, (2_000_000_000, 2_000_000_000))  # the newer one
        prefs.write_text(json.dumps({}))

        cfg = Config.load()
        show = open_or_create_startup_show(cfg, shows_dir=shows_dir)
        _assert(show.root == mine.root,
                "the newest show already on disk is adopted")
        _assert(not (shows_dir / f"{DEFAULT_SHOW_NAME}.show").exists(),
                "no empty default show invented on top of it")
        _assert(cfg.last_show_path == str(mine.root),
                "the adopted show becomes last_show_path")
        _assert(old.root.exists(), "the other show is left alone")


def test_explicit_show_path_wins_and_is_remembered():
    print("test_explicit_show_path_wins_and_is_remembered")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shows_dir = Path(tmp) / "shows"
        remembered = store.create_show(tmp, "Recordada")
        wanted = store.create_show(tmp, "Pedida")
        prefs.write_text(json.dumps({"last_show_path": str(remembered.root)}))

        cfg = Config.load()
        show = open_or_create_startup_show(cfg, shows_dir=shows_dir,
                                           explicit_path=str(wanted.root))
        _assert(show.root == wanted.root, "--show overrides last_show_path")
        _assert(cfg.last_show_path == str(wanted.root),
                "the requested show becomes last_show_path")

        # An unloadable --show must not strand the box: fall back, don't die.
        # (Rewrite the pointer the first half just persisted.)
        prefs.write_text(json.dumps({"last_show_path": str(remembered.root)}))
        cfg2 = Config.load()
        show2 = open_or_create_startup_show(cfg2, shows_dir=shows_dir,
                                            explicit_path=str(Path(tmp) / "nope.show"))
        _assert(show2.root == remembered.root,
                "a bad --show falls back to last_show_path")

        # A relative --show must not be remembered relative: the next run's
        # working directory is whatever the init script or operator had.
        rel = os.path.relpath(wanted.root, tmp)
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            cfg3 = Config.load()
            show3 = open_or_create_startup_show(cfg3, shows_dir=shows_dir,
                                                explicit_path=rel)
            _assert(show3.root == Path(rel), "a relative --show still opens")
            _assert(os.path.isabs(cfg3.last_show_path),
                    "last_show_path is stored absolute")
            _assert(cfg3.last_show_path == str(wanted.root),
                    "and it names the show that was opened")
        finally:
            os.chdir(cwd)


def test_default_name_collision_uniquified():
    print("test_default_name_collision_uniquified")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shows_dir = Path(tmp) / "shows"
        (shows_dir / f"{DEMO_SHOW_NAME}.show").mkdir(parents=True)  # squatter

        cfg = Config.load()
        show = open_or_create_startup_show(cfg, shows_dir=shows_dir)
        _assert(show.root == shows_dir / f"{DEMO_SHOW_NAME} 2.show"
                and show.name == f"{DEMO_SHOW_NAME} 2",
                "existing demo folder bumps the show name")


def main():
    orig_shipped, orig_prefs = config.shipped_config_path, config.userprefs_path
    try:
        for test in (test_migrates_legacy_playlist_and_patch,
                     test_second_launch_reopens_same_show,
                     test_clean_start_copies_the_demo_show,
                     test_missing_template_falls_back_to_empty_show,
                     test_read_only_template_yields_a_writable_show,
                     test_demo_template_uses_only_shipped_fixtures,
                     test_stale_pointers_fall_back,
                     test_hand_edited_prefs_with_a_bom_still_open_the_show,
                     test_unparseable_prefs_are_kept_not_overwritten,
                     test_show_already_on_disk_is_adopted_not_replaced,
                     test_explicit_show_path_wins_and_is_remembered,
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
