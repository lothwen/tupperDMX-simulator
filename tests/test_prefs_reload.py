# ABOUTME: Live reload of userprefs.json — Config.reload_prefs() adopting an
# ABOUTME: external edit, and PrefsWatcher surviving the save-by-rename that
# ABOUTME: both the app and an editor perform.
"""
Covers the two halves of live prefs reload:

  * Config.reload_prefs() — adopts only the machine-level sections, tells its
    own save() apart from an operator's edit by content, leaves the show
    pointers and the show-aliased playlist/fixtures alone, and refuses to
    quarantine a file it merely failed to parse.
  * PrefsWatcher — coalesces a burst of writes into one signal and keeps
    working after os.replace() swaps the inode out from under it.

Run with:
    uv run python tests/test_prefs_reload.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import config as config_mod
from core.config import Config


@pytest.fixture(autouse=True)
def _restore_config_paths():
    orig_shipped, orig_prefs = config_mod.shipped_config_path, config_mod.userprefs_path
    yield
    config_mod.shipped_config_path, config_mod.userprefs_path = orig_shipped, orig_prefs


def _patch_paths(tmp):
    """Point both path helpers into the temp dir (see test_config_roundtrip)."""
    prefs = Path(tmp) / "userprefs.json"
    config_mod.shipped_config_path = lambda: os.path.join(tmp, "config.json")
    config_mod.userprefs_path = lambda: prefs
    return prefs


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _in_subprocess(body: str) -> None:
    """Run one of this file's event-loop bodies in a fresh interpreter.

    They need a live Qt event loop for a second or two, and pumping one in a
    process where tests/test_headless_smoke.py has already booted and shut down
    a full AppCore segfaults inside Qt's posted-event delivery. That fault is
    NOT this module's: a clean tree reproduces it with nothing but a bare
    processEvents() loop in a test collected after the smoke test (the audio
    engine's QAudioSink/QAudioDecoder are the likely dangling half — the same
    stack PyQt6 is pinned to 6.8 for). Isolating these two keeps their coverage
    without pretending that is fixed.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (f"import sys; sys.path.insert(0, {repo!r});"
            f" import tests.test_prefs_reload as m; m.{body}()")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=120)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        raise AssertionError(
            f"{body} failed in its own interpreter (rc={proc.returncode}):\n"
            f"{proc.stdout}\n{proc.stderr}")


#: Held for the process's lifetime on purpose: a QApplication that only a local
#: variable references is garbage-collected out from under the event dispatcher,
#: and the file watcher's socket notifier then never fires.
_QAPP = None


def _qapp_for_body():
    """Every body runs standalone, so each brings its own QApplication."""
    global _QAPP
    from PyQt6.QtWidgets import QApplication
    _QAPP = QApplication.instance() or QApplication([])
    return _QAPP


def _pump(ms):
    from PyQt6.QtCore import QCoreApplication, QElapsedTimer
    timer = QElapsedTimer()
    timer.start()
    while timer.elapsed() < ms:
        QCoreApplication.processEvents()


def _edit(prefs: Path, **sections):
    """Rewrite the prefs file the way an operator would: read, change, save."""
    data = json.loads(prefs.read_text())
    for name, values in sections.items():
        data.setdefault(name, {}).update(values)
    prefs.write_text(json.dumps(data, indent=2))


# ----------------------------------------------------------------- reload_prefs

def test_own_save_is_not_an_external_edit():
    with tempfile.TemporaryDirectory() as tmp:
        _patch_paths(tmp)
        cfg = Config.load()          # first launch writes the file
        _assert(cfg.reload_prefs() == [], "a freshly written file reads back as no change")

        cfg.volume.audio = 0.25
        cfg.save()
        _assert(cfg.reload_prefs() == [], "the app's own save() is not reported as an edit")


def test_external_edit_is_adopted():
    with tempfile.TemporaryDirectory() as tmp:
        prefs = _patch_paths(tmp)
        cfg = Config.load()
        cfg.save()

        _edit(prefs, volume={"audio": 0.9}, logging={"level": "DEBUG"})
        adopted = cfg.reload_prefs()

        _assert(sorted(adopted) == ["logging", "volume"],
                f"only the edited sections are reported (got {adopted})")
        _assert(cfg.volume.audio == 0.9, "the edited value is live in memory")
        _assert(cfg.logging.level == "DEBUG", "a second edited section is live too")
        _assert(cfg.reload_prefs() == [], "re-reading an already-adopted file is a no-op")


def test_edited_values_are_validated_like_a_startup_read():
    with tempfile.TemporaryDirectory() as tmp:
        prefs = _patch_paths(tmp)
        cfg = Config.load()
        cfg.save()

        _edit(prefs, volume={"audio": 7.5}, window={"stage_haze": -3.0})
        cfg.reload_prefs()

        _assert(cfg.volume.audio == 1.0, "an out-of-range volume is clamped, not adopted raw")
        _assert(cfg.window.stage_haze == 0.0, "an out-of-range haze is clamped too")


def test_show_pointers_and_patch_are_never_adopted():
    """The show layer aliases config.playlist/fixtures onto the open show's own
    objects; adopting the legacy keys would detach them. last_show_path would
    mean swapping the show out from under a running session."""
    with tempfile.TemporaryDirectory() as tmp:
        prefs = _patch_paths(tmp)
        cfg = Config.load()
        cfg.last_show_path = "/shows/Real.show"
        cfg.save()

        show_playlist = cfg.playlist
        show_fixtures = cfg.fixtures

        data = json.loads(prefs.read_text())
        data["last_show_path"] = "/shows/Hijacked.show"
        data["recent_shows"] = ["/shows/Hijacked.show"]
        data["fixtures"] = [{"type": "ledbar", "dmx_start": 1}]
        data["playlist"] = {"songs": [{"audio": "/x.mp3"}], "current_song_index": 0}
        data["volume"] = {"audio": 0.33}
        prefs.write_text(json.dumps(data, indent=2))

        adopted = cfg.reload_prefs()

        _assert(adopted == ["volume"], f"only the machine section is adopted (got {adopted})")
        _assert(cfg.last_show_path == "/shows/Real.show", "last_show_path is left alone")
        _assert(cfg.recent_shows != ["/shows/Hijacked.show"], "recent_shows is left alone")
        _assert(cfg.playlist is show_playlist, "the show's playlist object is still the one in use")
        _assert(cfg.fixtures is show_fixtures, "the show's fixture list is still the one in use")


def test_unparseable_file_raises_and_is_left_where_it_is():
    """load() quarantines a broken file; reload_prefs() must not — it is far
    more likely to have caught an editor mid-write."""
    with tempfile.TemporaryDirectory() as tmp:
        prefs = _patch_paths(tmp)
        cfg = Config.load()
        cfg.save()

        prefs.write_text('{"volume": {"audio": 0.4')   # truncated mid-write
        with pytest.raises(ValueError):                # JSONDecodeError
            cfg.reload_prefs()

        _assert(prefs.exists(), "the half-written file is still where the editor left it")
        _assert(not (prefs.parent / "userprefs.json.bad").exists(),
                "reload_prefs did not quarantine it")


def test_save_after_an_adopted_edit_keeps_it():
    """The whole point: the operator's edit survives the next save instead of
    being overwritten by stale in-memory state."""
    with tempfile.TemporaryDirectory() as tmp:
        prefs = _patch_paths(tmp)
        cfg = Config.load()
        cfg.save()

        _edit(prefs, artnet={"universe": 7})
        cfg.reload_prefs()
        cfg.window.width = 1600      # something the app changes afterwards
        cfg.save()

        on_disk = json.loads(prefs.read_text())
        _assert(on_disk["artnet"]["universe"] == 7, "the adopted edit is still on disk")
        _assert(on_disk["window"]["width"] == 1600, "the app's own later change is there too")


# ------------------------------------------------------------------- the watcher

def test_watcher_coalesces_and_survives_replace():
    _in_subprocess("_watcher_body")


def _watcher_body():
    """One signal per burst, and it keeps firing after os.replace() — which is
    how atomic_write_json and most editors save."""
    from app.prefs_watcher import PrefsWatcher
    from core.show.store import atomic_write_json

    _qapp_for_body()
    pump = _pump

    with tempfile.TemporaryDirectory() as tmp:
        prefs = Path(tmp) / "userprefs.json"
        prefs.write_text("{}")

        watcher = PrefsWatcher(prefs)
        fired = []
        watcher.changed.connect(lambda: fired.append(1))

        for value in (1, 2, 3):                      # one logical save, in pieces
            prefs.write_text(json.dumps({"n": value}))
            pump(30)
        pump(watcher.SETTLE_MS + 400)
        _assert(len(fired) == 1, f"a burst of writes coalesces into one signal (got {len(fired)})")

        fired.clear()
        atomic_write_json(prefs, {"n": 4})           # replaces the inode
        pump(watcher.SETTLE_MS + 400)
        _assert(len(fired) == 1,
                f"a rename-over-the-top is still noticed (got {len(fired)})")

        fired.clear()
        atomic_write_json(prefs, {"n": 5})           # ...and again, not just once
        pump(watcher.SETTLE_MS + 400)
        _assert(len(fired) == 1, f"the watch survives repeated replaces (got {len(fired)})")

        watcher.stop()
        fired.clear()
        prefs.write_text('{"n": 6}')
        pump(watcher.SETTLE_MS + 300)
        _assert(not fired, "stop() really stops it")


# ------------------------------------------------------------ the whole path

def test_appcore_adopts_an_external_edit_end_to_end():
    _in_subprocess("_appcore_body")


def _appcore_body():
    """AppCore boots, an operator edits the file, and the change reaches the
    live buses without a restart.

    The watcher has to resolve userprefs_path() the same way Config does, or it
    watches one file while the config reads another — which is exactly what a
    module-level `from core.config import userprefs_path` did.
    """
    from app.core import AppCore
    from audio.mixer import BUS_VOICE
    from core.show import store

    _qapp_for_body()
    pump = _pump

    with tempfile.TemporaryDirectory() as tmp:
        prefs = _patch_paths(tmp)
        show = store.create_show(tmp, "Rack")
        store.save_manifest(show)

        cfg = Config()
        cfg.webremote.enabled = False        # no sockets in CI
        cfg.save()

        core = AppCore(cfg, show, snapshot_on_open=False)
        adopted = []
        core.prefs_reloaded.connect(lambda names: adopted.extend(names))
        core.start()
        pump(200)
        try:
            _assert(core._prefs_watcher is not None, "start() brought a watcher up")

            _edit(prefs, volume={"voice": 0.11}, artnet={"universe": 5})
            pump(core._prefs_watcher.SETTLE_MS + 900)

            _assert(sorted(adopted) == ["artnet", "volume"],
                    f"the edit reached AppCore (got {adopted})")
            _assert(cfg.volume.voice == 0.11, "the config adopted the edited value")
            _assert(core.audio_engine.mixer._volume[BUS_VOICE] == 0.11,
                    "and it reached the live voice bus")

            # The app's own save must not come back around as an edit.
            adopted.clear()
            cfg.window.width = 1600
            core.save_config()
            pump(core._prefs_watcher.SETTLE_MS + 700)
            _assert(not adopted, f"our own save() is not reported as an edit (got {adopted})")
        finally:
            core.shutdown()
            pump(100)


def main():
    _qapp_for_body()
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        print(f"\n{name}:")
        fn()
    print("\nAll prefs-reload tests passed.")


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    main()
