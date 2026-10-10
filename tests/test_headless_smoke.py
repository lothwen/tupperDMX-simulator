"""
Headless smoke test: AppCore boots the whole core without any widgets.

Builds a real temp show (store.create_show), boots AppCore over it with the
default "all software" I/O routing and the web remote disabled (no sockets in
CI), and asserts the headless contract: the software controller runs and the
MIDI stream points at it, the web-remote snapshot carries the frozen schema
keys (incl. Fase-4 countin), preview_clip drives the blackout verb (the
remote's cue/blackout/panic path), playlist navigation moves the index, the
count-in switch applies, the remote's free-run click runs on the real
metronome until the song's play takes it over, and shutdown is idempotent.

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
from core.config import Config, FixtureInstance, SongConfig
from core.show import store, versions


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


def _boot(tmp, fixtures=()):
    """A real show + AppCore, exactly like app/headless.py boots it (minus
    the web-remote socket: config leaves it disabled)."""
    from app.core import AppCore

    show = store.create_show(tmp, "Rack", fixtures=fixtures)
    show.playlist.songs.append(SongConfig(id="s1", title="Uno"))
    show.playlist.songs.append(SongConfig(id="s2", title="Dos"))
    show.playlist.current_song_index = 0
    store.save_manifest(show)

    config = Config()
    config.webremote.enabled = False  # no sockets in CI
    # snapshot_on_open=False exactly as app.headless.run_headless builds it.
    core = AppCore(config, show, snapshot_on_open=False)
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

            # No editor exists headless, so booting must not checkpoint the
            # show: on a rack box that turned every power-on into another
            # identical copy of midis/ under versions/.
            _assert(versions.list_versions(show) == [],
                    "headless takes no opening snapshot")

            # The frozen web-remote snapshot works without a window.
            state = core.webremote_manager._build_state()
            for key in ("type", "show_name", "playing", "position", "duration",
                        "bpm", "beats_per_bar", "beat_note", "blackout",
                        "countin", "free_run",
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

            # Count-in (the click panel's switch; the remote reads it back).
            core.on_song_precount_toggled(False)
            _assert(config.get_current_song().precount is False,
                    "the count-in switch persists on the current song")
            state = core.webremote_manager._build_state()
            _assert(state["countin"] == {"enabled": False, "bars": 2},
                    "the snapshot reflects the new count-in state")

            # The remote's free-run click, on the real metronome.
            remote = core.webremote_manager
            remote._execute({"action": "free_run", "bpm": 96, "meter": "6/8"})
            _assert(core.metronome.is_free_running()
                    and remote._build_state()["free_run"]
                    == {"bpm": 96, "meter": "6/8"},
                    "free_run starts the click on its own and publishes it")
            core.play_pause_playback()
            _assert(core.media_controller.get_playback_state(), "the song plays")
            _assert(not core.metronome.is_free_running()
                    and remote._build_state()["free_run"] is None,
                    "and its play took the click over from the free-run")
            remote._execute({"action": "free_run", "bpm": 96, "meter": "6/8"})
            _assert(not core.metronome.is_free_running(),
                    "a free-run is refused while the song plays")
            core.stop_playback()
        finally:
            core.shutdown()

        core.shutdown()
        _assert(core._shutdown_done, "shutdown twice is a safe no-op")
        _assert(not core.dmx_manager.controller_running()
                if hasattr(core.dmx_manager, "controller_running") else True,
                "controller stopped on shutdown")


def test_repatch_keeps_the_rig_in_auto():
    """A patch edit must not drop every fixture out of the song-part program.

    `repatch` builds each Fixture fresh, so the auto flag — a runtime *mirror*
    of what the .mid persists as its note-71 setup — comes back at its default
    of off. Nothing else re-seeds it (that only happens when the controller
    *instance* changes), so editing a fixture in the designer left the whole rig
    ignoring every song-part marker while the timeline's 'A' buttons still said
    it was in auto: the notes went on arriving and stopped doing anything.
    """
    print("test_repatch_keeps_the_rig_in_auto")
    patch = [FixtureInstance(type="ledbar", dmx_start=1),
             FixtureInstance(type="blinder", dmx_start=33)]
    with tempfile.TemporaryDirectory() as tmp:
        config_module.userprefs_path = lambda: Path(tmp) / "userprefs.json"
        core, config, _ = _boot(tmp, fixtures=patch)
        try:
            controller = core.software_controller
            for channel in (0, 1):
                core.midi_manager.set_fixture_auto(channel, True)
            _assert(core.midi_manager.auto_channels() == {0, 1},
                    "the document has both lanes in auto")
            _assert(all(controller.is_fixture_auto(c) for c in (0, 1)),
                    "and the controller mirrors it")

            # What a designer edit does: the address moved, so the patch is
            # hot-swapped onto the running controller.
            config.fixtures[1].dmx_start = 40
            core.repatch_controller()

            _assert(core.midi_manager.auto_channels() == {0, 1},
                    "the document still has both lanes in auto")
            _assert(all(controller.is_fixture_auto(c) for c in (0, 1)),
                    "and the repatched controller still agrees with it")
        finally:
            core.shutdown()


def main():
    orig_prefs = config_module.userprefs_path
    try:
        test_headless_core_contract()
        test_repatch_keeps_the_rig_in_auto()
    finally:
        config_module.userprefs_path = orig_prefs
        fixture_library.set_show_fixtures_dir(None)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
