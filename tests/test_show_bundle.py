"""
Characterization test: portable show bundles (core/show/bundle.py).

Covers the export roundtrip (referenced audio collected into audio/, owned
midis included, manifest rewritten to relative paths), non-shipped fixture
defs traveling with the bundle and resolving via the show-local layer on
import, missing-audio warnings, include_history, unsafe/foreign zip rejection,
and future-format rejection.

Run with:
    uv run python tests/test_show_bundle.py
"""

import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import fixture_library
from core.config import SongConfig, FixtureInstance
from core.show import bundle, store, versions
from core.show.model import FORMAT_VERSION, ShowError


@pytest.fixture(autouse=True)
def _reset_fixture_library():
    yield
    fixture_library.set_show_fixtures_dir(None)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


CUSTOM_DEF = {
    "$schema": ("https://raw.githubusercontent.com/OpenLightingProject/"
                "open-fixture-library/master/schemas/fixture.json"),
    "name": "laser_x",
    "categories": ["Laser"],
    "meta": {"authors": ["test"], "createDate": "2026-01-01",
             "lastModifyDate": "2026-01-01"},
    "availableChannels": {"brightness": {"capability": {"type": "Intensity"}}},
    "modes": [{"name": "1ch", "channels": ["brightness"]}],
}


# A definition is one or two files. This one ships a sidecar with scene banks;
# laser_x above ships none, so the bundle has to handle both.
PAIRED_DEF = {
    "$schema": ("https://raw.githubusercontent.com/OpenLightingProject/"
                "open-fixture-library/master/schemas/fixture.json"),
    "name": "wash_y",
    "categories": ["Color Changer"],
    "meta": {"authors": ["test"], "createDate": "2026-01-01",
             "lastModifyDate": "2026-01-01"},
    "availableChannels": {
        "red": {"capability": {"type": "ColorIntensity", "color": "Red"}}},
    "modes": [{"name": "1ch", "channels": ["red"]}],
}

PAIRED_SIDECAR = {
    "tdmx": 1,
    "modes": {"*": {"banks": {"WASH_SCENES": [{"red": 255}, {"red": 0}]}}},
}


def _rich_show(tmp):
    """A show with referenced audio, an owned midi and two show-local defs."""
    show = store.create_show(tmp, "Gira")
    (show.fixtures_dir / "laser_x.json").write_text(json.dumps(CUSTOM_DEF))
    (show.fixtures_dir / "wash_y.json").write_text(json.dumps(PAIRED_DEF))
    (show.fixtures_dir / "wash_y.tdmx.json").write_text(json.dumps(PAIRED_SIDECAR))
    fixture_library.clear_cache()  # the defs were written after the layer was set
    show.fixtures = [FixtureInstance(type="ledbar", dmx_start=1, channel=0),
                     FixtureInstance(type="laser_x", dmx_start=90, channel=1),
                     FixtureInstance(type="wash_y", dmx_start=95, channel=2)]

    external = Path(tmp) / "guias" / "marea.mp3"
    external.parent.mkdir()
    external.write_bytes(b"audio-bytes")
    midi = show.midis_dir / "marea.mid"
    midi.write_bytes(b"midi-bytes")
    show.playlist.songs.append(SongConfig(
        audio=str(external), midi=str(midi), id="s1", title="Marea", key="Am"))
    store.save_manifest(show)
    return show, external


def test_export_import_roundtrip():
    print("test_export_import_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        show, external = _rich_show(tmp)
        pack = Path(tmp) / "gira.showpack"
        warnings = bundle.export_bundle(show, pack)
        _assert(warnings == [], "clean show exports without warnings")

        names = set(zipfile.ZipFile(pack).namelist())
        _assert("show.json" in names and "midis/marea.mid" in names,
                "bundle carries the manifest and the owned midis")
        _assert("audio/marea.mp3" in names,
                "referenced audio is COLLECTED into audio/ (collect & save)")
        _assert("fixtures/laser_x.json" in names,
                "the non-shipped fixture def travels with the bundle")
        _assert("fixtures/wash_y.json" in names
                and "fixtures/wash_y.tdmx.json" in names,
                "a definition's sidecar travels with it, or it arrives bankless")
        _assert("fixtures/laser_x.tdmx.json" not in names,
                "a definition with no sidecar contributes only its own file")

        # Import on a "different machine" (another folder).
        target = Path(tmp) / "otra-maquina"
        target.mkdir()
        root = bundle.import_bundle(pack, target)
        _assert(root == target / "Gira.show", "bundle unpacks to Name.show/")

        imported = store.load_show(root)
        song = imported.playlist.songs[0]
        _assert(song.audio == str(root / "audio" / "marea.mp3")
                and os.path.exists(song.audio),
                "imported audio is self-contained (lives inside the show)")

        fixture_library.clear_cache()
        reopened = fixture_library.load_fixture_defs(force_reload=True)
        _assert("wash_y" in reopened and "laser_x" in reopened,
                "both show-local definitions resolve in the reopened show")
        _assert(list(reopened["wash_y"].banks) == ["WASH_SCENES"],
                "the bundled sidecar's scene banks survived the round trip")
        _assert(Path(song.midi).read_bytes() == b"midi-bytes",
                "owned midi arrived intact")
        _assert(song.title == "Marea" and song.key == "Am", "song metadata arrived")
        _assert(any(f.type == "laser_x" for f in imported.fixtures),
                "the patch entry for the custom type parsed (show-local def wins)")


def test_guide_tracks_are_collected():
    print("test_guide_tracks_are_collected")
    with tempfile.TemporaryDirectory() as tmp:
        show, external = _rich_show(tmp)
        # A guide whose basename COLLIDES with the collected backing.
        guide = Path(tmp) / "estudio" / "marea.mp3"
        guide.parent.mkdir()
        guide.write_bytes(b"guide-bytes")
        show.playlist.songs[0].guide = str(guide)
        show.playlist.songs[0].guide_offset = 0.25
        store.save_manifest(show)

        pack = Path(tmp) / "gira.showpack"
        warnings = bundle.export_bundle(show, pack)
        _assert(warnings == [], "guide exports without warnings")
        names = set(zipfile.ZipFile(pack).namelist())
        _assert("audio/marea.mp3" in names and "audio/marea-2.mp3" in names,
                "backing AND guide are collected (collision suffixed)")
        manifest = json.loads(zipfile.ZipFile(pack).read("show.json"))
        _assert(manifest["songs"][0]["guide"] == "audio/marea-2.mp3",
                "the bundled manifest points at the collected guide")
        _assert(manifest["songs"][0]["guide_offset"] == 0.25,
                "guide_offset rides the manifest")

        # Round-trip: the imported show resolves the guide inside the folder.
        target = Path(tmp) / "otra-maquina"
        target.mkdir()
        root = bundle.import_bundle(pack, target)
        imported = store.load_show(root)
        song = imported.playlist.songs[0]
        _assert(song.guide == str(root / "audio" / "marea-2.mp3")
                and os.path.exists(song.guide),
                "imported guide is self-contained (lives inside the show)")
        _assert(Path(song.guide).read_bytes() == b"guide-bytes",
                "guide bytes arrived intact")


def test_inshow_audio_ships_and_reserves_names():
    print("test_inshow_audio_ships_and_reserves_names")
    with tempfile.TemporaryDirectory() as tmp:
        show, external = _rich_show(tmp)
        # In-show audio (as after a bundle import) with the SAME basename as
        # the external backing that will be collected.
        inshow = show.audio_dir / "marea.mp3"
        inshow.write_bytes(b"in-show-bytes")
        show.playlist.songs.append(SongConfig(
            audio=str(inshow), id="s2", title="Dentro"))
        store.save_manifest(show)

        pack = Path(tmp) / "gira.showpack"
        warnings = bundle.export_bundle(show, pack)
        _assert(warnings == [], "in-show audio exports without warnings")
        zf = zipfile.ZipFile(pack)
        names = set(zf.namelist())
        _assert("audio/marea.mp3" in names,
                "in-show audio ships wholesale at its own path")
        _assert("audio/marea-2.mp3" in names,
                "the colliding external backing is collected under a suffix")
        _assert(zf.read("audio/marea.mp3") == b"in-show-bytes"
                and zf.read("audio/marea-2.mp3") == b"audio-bytes",
                "each arcname carries the right bytes")
        manifest = json.loads(zf.read("show.json"))
        _assert(manifest["songs"][0]["audio"] == "audio/marea-2.mp3"
                and manifest["songs"][1]["audio"] == "audio/marea.mp3",
                "manifest entries point at their respective files")

        target = Path(tmp) / "otra-maquina"
        target.mkdir()
        root = bundle.import_bundle(pack, target)
        imported = store.load_show(root)
        _assert(all(os.path.exists(s.audio) for s in imported.playlist.songs),
                "both songs' audio resolves after import")


def test_missing_audio_warns_but_exports():
    print("test_missing_audio_warns_but_exports")
    with tempfile.TemporaryDirectory() as tmp:
        show, external = _rich_show(tmp)
        external.unlink()  # the guide moved away
        pack = Path(tmp) / "gira.showpack"
        warnings = bundle.export_bundle(show, pack)
        _assert(len(warnings) == 1 and "audio" in warnings[0],
                "missing audio produces a warning")
        names = set(zipfile.ZipFile(pack).namelist())
        _assert(not any(n.startswith("audio/") for n in names),
                "the missing file is simply not bundled")
        manifest = json.loads(zipfile.ZipFile(pack).read("show.json"))
        _assert(manifest["songs"][0]["audio"] == str(external),
                "the manifest keeps the original path (relink target)")


def test_include_history():
    print("test_include_history")
    with tempfile.TemporaryDirectory() as tmp:
        show, _ = _rich_show(tmp)
        versions.take_snapshot(show, pinned=True, name="v1")
        pack_without = Path(tmp) / "sin.showpack"
        pack_with = Path(tmp) / "con.showpack"
        bundle.export_bundle(show, pack_without)
        bundle.export_bundle(show, pack_with, include_history=True)
        _assert(not any(n.startswith("versions/")
                        for n in zipfile.ZipFile(pack_without).namelist()),
                "history is excluded by default")
        _assert(any(n.startswith("versions/")
                    for n in zipfile.ZipFile(pack_with).namelist()),
                "include_history bundles the snapshots")


def test_import_rejections():
    print("test_import_rejections")
    with tempfile.TemporaryDirectory() as tmp:
        # Not a bundle at all.
        foreign = Path(tmp) / "foreign.zip"
        with zipfile.ZipFile(foreign, "w") as zf:
            zf.writestr("readme.txt", "hola")
        with pytest.raises(ShowError):
            bundle.import_bundle(foreign, tmp)
        print("  ok: a zip without show.json is rejected")

        # Future format version.
        future = Path(tmp) / "future.showpack"
        with zipfile.ZipFile(future, "w") as zf:
            zf.writestr("show.json", json.dumps(
                {"format_version": FORMAT_VERSION + 1, "name": "X"}))
        with pytest.raises(ShowError):
            bundle.import_bundle(future, tmp)
        print("  ok: a future format_version is rejected cleanly")

        # Path traversal.
        evil = Path(tmp) / "evil.showpack"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("show.json", json.dumps({"format_version": 1, "name": "E"}))
            zf.writestr("../escape.txt", "nope")
        with pytest.raises(ShowError):
            bundle.import_bundle(evil, tmp)
        print("  ok: path traversal in a bundle is rejected")

        # Existing destination.
        show, _ = _rich_show(tmp)
        pack = Path(tmp) / "gira.showpack"
        bundle.export_bundle(show, pack)
        with pytest.raises(ShowError):
            bundle.import_bundle(pack, tmp)  # Gira.show already exists here
        print("  ok: unpacking over an existing show folder is rejected")


def main():
    try:
        test_export_import_roundtrip()
        test_guide_tracks_are_collected()
        test_inshow_audio_ships_and_reserves_names()
        test_missing_audio_warns_but_exports()
        test_include_history()
        test_import_rejections()
    finally:
        fixture_library.set_show_fixtures_dir(None)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
