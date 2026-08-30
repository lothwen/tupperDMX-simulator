"""
Characterization test: ShowManager (app/show_manager.py) — the Qt-side
coordinator of the open show.

Covers attach() aliasing (config.playlist/config.fixtures ARE the show's
objects), the compare-based manifest dirty tracking (in-place mutations reach
show.json; prefs-only saves don't rewrite it), autosave_state_changed
emissions, and the favourites toggle.

Run with:
    uv run python tests/test_show_manager.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import pytest

from core import config as config_module, fixture_library
from core import clips
from core.config import Config, FixtureInstance, SongConfig
from core.show import store
from core.show.model import ShowError
from app.show_manager import ShowManager


@pytest.fixture(autouse=True)
def _reset_globals():
    orig_prefs = config_module.userprefs_path
    yield
    config_module.userprefs_path = orig_prefs
    fixture_library.set_show_fixtures_dir(None)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_attach_aliases_config():
    print("test_attach_aliases_config")
    with tempfile.TemporaryDirectory() as tmp:
        show = store.create_show(tmp, "Gira")
        config = Config()
        manager = ShowManager(config)
        changed = []
        manager.show_changed.connect(lambda: changed.append(True))

        manager.attach(show)
        _assert(config.playlist is show.playlist,
                "config.playlist IS the show's playlist (aliased)")
        _assert(config.fixtures is show.fixtures,
                "config.fixtures IS the show's patch (aliased)")
        _assert(changed == [True], "attach emits show_changed")


def test_manifest_saved_only_on_change():
    print("test_manifest_saved_only_on_change")
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "guia.mp3"
        audio.write_bytes(b"a")
        show = store.create_show(tmp, "Gira")
        config = Config()
        manager = ShowManager(config)
        states = []
        manager.autosave_state_changed.connect(states.append)
        manager.attach(show)

        _assert(manager.save_manifest_if_changed() is False,
                "freshly attached show is clean (no spurious write)")

        # Mutate through the alias, exactly like the app's call sites do.
        config.playlist.songs.append(SongConfig(audio=str(audio), id="s1"))
        config.playlist.current_song_index = 0
        _assert(manager.save_manifest_if_changed() is True,
                "in-place playlist mutation marks the manifest dirty")
        data = json.loads((show.root / "show.json").read_text())
        _assert(data["songs"][0]["audio"] == str(audio),
                "the mutation reached show.json")

        before = (show.root / "show.json").stat().st_mtime_ns
        _assert(manager.save_manifest_if_changed() is False,
                "an unchanged manifest is not rewritten")
        _assert((show.root / "show.json").stat().st_mtime_ns == before,
                "file untouched on the no-op save")
        _assert(states == [], "manifest-only funnel saves emit no chip churn")

        # notify_changed is the save_config() funnel — same semantics.
        config.fixtures.append(FixtureInstance(type="blinder", dmx_start=65))
        manager.notify_changed()
        data = json.loads((show.root / "show.json").read_text())
        _assert(len(data["fixtures"]) == 1,
                "patch mutations reach show.json through notify_changed")


class FakeReloadWindow:
    """Just enough window for open_show(): records reloads."""

    def __init__(self):
        self.reloads = 0

    def reload_from_show(self):
        self.reloads += 1


def test_open_show_switches_and_reloads():
    print("test_open_show_switches_and_reloads")
    with tempfile.TemporaryDirectory() as tmp:
        config_module.userprefs_path = lambda: Path(tmp) / "userprefs.json"
        show_a = store.create_show(tmp, "A")
        show_b = store.create_show(tmp, "B")
        show_b.playlist.songs.append(SongConfig(id="s1", title="Tema B"))
        store.save_manifest(show_b)

        config = Config()
        window = FakeReloadWindow()
        manager = ShowManager(config, window)
        manager.attach(show_a)

        opened = manager.open_show(show_b.root)
        _assert(opened.name == "B" and manager.show is opened,
                "open_show adopts the loaded show")
        _assert(config.playlist is opened.playlist,
                "open_show re-establishes the aliasing")
        _assert(config.playlist.songs[0].title == "Tema B",
                "the new show's setlist is live")
        _assert(window.reloads == 1, "open_show rebuilds the window once")
        _assert(config.last_show_path == str(show_b.root),
                "open_show records the machine pointer")

        with pytest.raises(ShowError):
            manager.open_show(Path(tmp) / "nope.show")
        print("  ok: opening a non-show raises ShowError")
        _assert(manager.show is opened and config.playlist is opened.playlist,
                "a failed open keeps the current show attached")


def test_favorites_toggle():
    print("test_favorites_toggle")
    with tempfile.TemporaryDirectory() as tmp:
        show = store.create_show(tmp, "Gira")
        config = Config()
        manager = ShowManager(config, snapshot_on_open=False)
        ref = clips.note_ref("ledbar", "", 73, 127)

        _assert(manager.favorites() == [] and manager.toggle_favorite(ref) is False,
                "without a show there is nothing to star — favourites are "
                "show content, so there is no machine-level fallback")

        manager.attach(show)
        emitted = []
        manager.favorites_changed.connect(lambda: emitted.append(True))

        _assert(manager.toggle_favorite(ref) is True and manager.is_favorite(ref),
                "starring a clip adds it to the show's set")
        _assert(json.loads(show.manifest_path.read_text())["favorites"] == [ref],
                "and reaches show.json through the normal save funnel")

        manager.toggle_favorite(clips.show_ref(51))
        _assert([f["kind"] for f in manager.favorites()] == ["note", "show"],
                "starring appends, so the grid keeps the order they were "
                "starred in")

        _assert(manager.toggle_favorite(ref) is False
                and not manager.is_favorite(ref),
                "starring again unstars")
        _assert(len(emitted) == 3, "every toggle announces itself")


def main():
    orig_prefs = config_module.userprefs_path
    try:
        test_attach_aliases_config()
        test_manifest_saved_only_on_change()
        test_open_show_switches_and_reloads()
        test_favorites_toggle()
    finally:
        config_module.userprefs_path = orig_prefs
        fixture_library.set_show_fixtures_dir(None)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
