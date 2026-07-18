"""
Headless smoke test: AppCore boots the whole core without any widgets.

Builds a real temp show (store.create_show), boots AppCore over it with the
default "all software" I/O routing and the web remote disabled (no sockets in
CI), and asserts the headless contract: the software controller runs and the
MIDI stream points at it, the web-remote snapshot carries the frozen schema
keys (incl. Fase-4 countin), preview_clip drives the blackout verb (the
remote's cue/blackout/panic path), playlist navigation moves the index, the
count-in commands apply, and shutdown is idempotent.

Run with:
    uv run python tests/test_headless_smoke.py
"""

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
from core.config import Config, SongConfig
from core.show import store


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


def _boot(tmp):
    """A real show + AppCore, exactly like app/headless.py boots it (minus
    the web-remote socket: config leaves it disabled)."""
    from app.core import AppCore

    show = store.create_show(tmp, "Rack")
    show.playlist.songs.append(SongConfig(id="s1", title="Uno"))
    show.playlist.songs.append(SongConfig(id="s2", title="Dos"))
    show.playlist.current_song_index = 0
    store.save_manifest(show)

    config = Config()
    config.webremote.enabled = False  # no sockets in CI
    core = AppCore(config, show)
    core.start()
    return core, config, show


def test_headless_core_contract():
    print("test_headless_core_contract")
    with tempfile.TemporaryDirectory() as tmp:
        config_module.userprefs_path = lambda: Path(tmp) / "userprefs.json"
        core, config, show = _boot(tmp)
        try:
            # Default "all software" routing spawned the controller and the
            # core pointed the MIDI stream at it.
            _assert(core.software_controller is not None,
                    "software controller runs headless")
            _assert(core.midi_manager.software_controller
                    is core.software_controller,
                    "MIDI stream points at the controller")

            # The frozen web-remote snapshot works without a window.
            state = core.webremote_manager._build_state()
            for key in ("type", "show_name", "playing", "position", "duration",
                        "bpm", "beats_per_bar", "blackout", "countin",
                        "setlist", "sections", "cues"):
                _assert(key in state, f"snapshot carries '{key}'")
            _assert(state["show_name"] == "Rack", "snapshot names the show")
            _assert(len(state["setlist"]["songs"]) == 2, "setlist is live")

            # Blackout rides preview_clip (the remote's cue/blackout path).
            _assert(core.software_controller.blackout_all_state is False,
                    "blackout starts off")
            core.preview_clip([(0, 51, 0)])
            _assert(core.software_controller.blackout_all_state is True,
                    "preview_clip toggles blackout-all on the controller")
            core.preview_clip([(0, 51, 0)])

            # Navigation (the remote's next/prev/select_song path).
            core.playlist_controller.go_to_song(1)
            _assert(config.playlist.current_song_index == 1,
                    "go_to_song moves the index")

            # Count-in commands (the remote's set_countin_bars/set_precount).
            core.set_count_in_bars(4)
            _assert(config.audio.count_in_bars == 4
                    and core.media_controller.count_in_bars == 4,
                    "set_count_in_bars reaches config + media controller")
            core.on_song_precount_toggled(False)
            _assert(config.get_current_song().precount is False,
                    "set_precount persists on the current song")
            state = core.webremote_manager._build_state()
            _assert(state["countin"] == {"enabled": False, "bars": 4},
                    "the snapshot reflects the new count-in state")
        finally:
            core.shutdown()

        core.shutdown()
        _assert(core._shutdown_done, "shutdown twice is a safe no-op")
        _assert(not core.dmx_manager.controller_running()
                if hasattr(core.dmx_manager, "controller_running") else True,
                "controller stopped on shutdown")


def main():
    orig_prefs = config_module.userprefs_path
    try:
        test_headless_core_contract()
    finally:
        config_module.userprefs_path = orig_prefs
        fixture_library.set_show_fixtures_dir(None)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
