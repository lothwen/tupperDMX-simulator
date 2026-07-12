"""
Characterization test: show version snapshots (core/show/versions.py).

Covers snapshot contents (show.json + midis/, NO audio), pinned/named metas,
newest-first listing, retention pruning that never touches pinned versions,
exact restore with the pre-restore safety snapshot, and same-second name
collisions.

Run with:
    uv run python tests/test_show_versions.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import fixture_library
from core.config import SongConfig
from core.show import store, versions
from core.show.model import ShowError


@pytest.fixture(autouse=True)
def _reset_fixture_library():
    yield
    fixture_library.set_show_fixtures_dir(None)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _show_with_content(tmp):
    show = store.create_show(tmp, "Gira")
    (show.audio_dir / "big.mp3").write_bytes(b"x" * 64)
    midi = show.midis_dir / "tema.mid"
    midi.write_bytes(b"midi-v1")
    show.playlist.songs.append(SongConfig(midi=str(midi), id="s1", title="Tema"))
    store.save_manifest(show)
    return show


def test_snapshot_contents_and_meta():
    print("test_snapshot_contents_and_meta")
    with tempfile.TemporaryDirectory() as tmp:
        show = _show_with_content(tmp)
        info = versions.take_snapshot(show, pinned=True, name="antes bolo")
        root = show.versions_dir / info.version_id
        _assert((root / "show.json").is_file(), "snapshot carries the manifest")
        _assert((root / "midis" / "tema.mid").read_bytes() == b"midi-v1",
                "snapshot carries the owned midis")
        _assert(not (root / "audio").exists(),
                "snapshot does NOT copy audio (referenced, heavy)")
        _assert(info.pinned and info.name == "antes bolo", "pin + name recorded")
        _assert("antes-bolo" in info.version_id, "named snapshots slug the id")

        listed = versions.list_versions(show)
        _assert(len(listed) == 1 and listed[0] == info,
                "list_versions returns the snapshot")


def test_listing_is_newest_first_and_collisions_bump():
    print("test_listing_is_newest_first_and_collisions_bump")
    with tempfile.TemporaryDirectory() as tmp:
        show = _show_with_content(tmp)
        a = versions.take_snapshot(show)
        b = versions.take_snapshot(show)  # same second -> id must be bumped
        _assert(a.version_id != b.version_id,
                "same-second snapshots get distinct ids")
        listed = versions.list_versions(show)
        _assert(listed[0].version_id == b.version_id,
                "list_versions is newest first")


def test_prune_respects_pinned():
    print("test_prune_respects_pinned")
    with tempfile.TemporaryDirectory() as tmp:
        show = _show_with_content(tmp)
        pinned = versions.take_snapshot(show, pinned=True, name="keep")
        autos = [versions.take_snapshot(show) for _ in range(5)]

        removed = versions.prune(show, retain=2)
        _assert(removed == 3, "prune removed the excess unpinned snapshots")
        remaining = {v.version_id for v in versions.list_versions(show)}
        _assert(pinned.version_id in remaining, "pinned snapshot survives pruning")
        _assert({a.version_id for a in autos[-2:]} <= remaining,
                "the newest unpinned snapshots survive")
        _assert(autos[0].version_id not in remaining,
                "the oldest unpinned snapshot is gone")


def test_restore_recovers_exact_state():
    print("test_restore_recovers_exact_state")
    with tempfile.TemporaryDirectory() as tmp:
        show = _show_with_content(tmp)
        checkpoint = versions.take_snapshot(show, pinned=True, name="v1")

        # Diverge: edit the midi, add a song, save the manifest.
        (show.midis_dir / "tema.mid").write_bytes(b"midi-v2")
        (show.midis_dir / "extra.mid").write_bytes(b"extra")
        show.playlist.songs.append(SongConfig(id="s2", title="Extra"))
        store.save_manifest(show)

        versions.restore(show, checkpoint.version_id)
        _assert((show.midis_dir / "tema.mid").read_bytes() == b"midi-v1",
                "restore brings the midi content back")
        _assert(not (show.midis_dir / "extra.mid").exists(),
                "files created after the snapshot are gone")
        manifest = json.loads(show.manifest_path.read_text())
        _assert(len(manifest["songs"]) == 1, "manifest rolled back too")

        listed = versions.list_versions(show)
        _assert(any(v.name == "pre-restore" for v in listed),
                "the pre-restore state was snapshotted (restore is undoable)")

        reloaded = store.load_show(show.root)
        _assert(len(reloaded.playlist.songs) == 1
                and reloaded.playlist.songs[0].title == "Tema",
                "the restored show loads cleanly")

        with pytest.raises(ShowError):
            versions.restore(show, "no-such-version")
        print("  ok: restoring a missing version raises ShowError")


def main():
    try:
        test_snapshot_contents_and_meta()
        test_listing_is_newest_first_and_collisions_bump()
        test_prune_respects_pinned()
        test_restore_recovers_exact_state()
    finally:
        fixture_library.set_show_fixtures_dir(None)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
