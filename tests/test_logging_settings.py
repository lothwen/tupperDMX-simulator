# ABOUTME: The configurable log folder and the Settings ▸ Logging page — where
# ABOUTME: an unset folder resolves to, and the live tail pane over it.
"""
Covers the three halves of "the log folder is a preference now":

  * tupperlogger.resolve_log_dir — a blank or absent setting is not a folder
    named "", it is the ./logs the app has always written to, and setup_logging
    puts the file exactly there.
    Re-installing them is also how a level change reaches a running app.
  * Config — log_dir defaults to blank, round-trips through userprefs.json, and
    a prefs file written before the setting existed still loads.
  * The Logging page — the folder field's readouts, accept persisting it,
    accept refusing a folder that cannot be created, and LogTailView streaming
    a growing file across a rotation.

Offscreen Qt + a shared QApplication come from conftest.py (the `qapp` fixture).

Run with:
    uv run python tests/test_logging_settings.py
"""

import contextlib
import datetime
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import config as config_mod
from core.config import Config
from tupperlogger import DEFAULT_LOG_DIR, resolve_log_dir
import tupperlogger.logger as tupperlogger_logger


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


@contextlib.contextmanager
def _restored_logging():
    """Put the root logger back: setup_logging() replaces its handlers, and
    pytest (and every later test in the process) is still using them."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    instance = tupperlogger_logger._logger_instance
    log_dir, log_file = instance.log_dir, instance.log_file
    try:
        yield
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)
        instance.log_dir, instance.log_file = log_dir, log_file


@pytest.fixture
def restore_logging():
    with _restored_logging():
        yield


def _log_line(name, level, message, clock="21:18:14"):
    """One line in the shape the file formatter writes."""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return f"{today} {clock} | {name:<20} | {level:<8} | {name} | {message}\n"


# --- the setting itself -----------------------------------------------------

def test_blank_log_dir_is_the_default_folder():
    print("test_blank_log_dir_is_the_default_folder")
    for blank in ("", "   ", None):
        _assert(resolve_log_dir(blank) == DEFAULT_LOG_DIR,
                f"{blank!r} resolves to the default folder ({DEFAULT_LOG_DIR})")
    _assert(resolve_log_dir("/var/log/tupper") == Path("/var/log/tupper"),
            "a configured folder is used as given")
    _assert(resolve_log_dir("~/logs") == Path("~/logs").expanduser(),
            "~ is expanded")


def test_setup_logging_writes_into_the_configured_folder(restore_logging):
    print("test_setup_logging_writes_into_the_configured_folder")
    from tupperlogger import current_log_path, setup_logging

    with tempfile.TemporaryDirectory() as tmp:
        # A folder two levels below an existing one: a preference may point
        # anywhere, so the parents have to be created too.
        target = Path(tmp) / "deep" / "logs"
        setup_logging(level="INFO", console_output=False, file_output=True,
                      log_dir=str(target))
        active = current_log_path()
        _assert(active is not None and active.parent == target,
                f"the log file lands in the configured folder ({active})")
        _assert(active.exists(), "and the file is actually created")

        logging.getLogger("test.folder").info("hello from the configured folder")
        for handler in logging.getLogger().handlers:
            handler.flush()
        _assert("hello from the configured folder" in active.read_text(),
                "records reach it")


def test_blank_setting_keeps_logging_where_it_always_went(restore_logging):
    print("test_blank_setting_keeps_logging_where_it_always_went")
    from tupperlogger import current_log_dir, setup_logging

    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)  # DEFAULT_LOG_DIR is relative — don't litter the repo
        try:
            setup_logging(level="INFO", console_output=False, file_output=True,
                          log_dir="")
            _assert(current_log_dir() == DEFAULT_LOG_DIR,
                    "a blank setting logs into ./logs, as before")
            _assert((Path(tmp) / "logs").is_dir(), "and that folder is created")
        finally:
            os.chdir(cwd)


def test_level_applies_without_a_relaunch(restore_logging):
    print("test_level_applies_without_a_relaunch")
    from tupperlogger import setup_logging

    with tempfile.TemporaryDirectory() as tmp:
        def install(level):
            setup_logging(level=level, console_output=False, file_output=True,
                          log_dir=tmp)

        install("DEBUG")
        _assert(logging.getLogger().level == logging.DEBUG, "starts on DEBUG")
        logging.getLogger("test.level").debug("chatter at DEBUG")

        # What Settings ▸ Logging does on OK: re-install, same process.
        install("WARNING")
        _assert(logging.getLogger().level == logging.WARNING,
                "the new level is in force straight away, with no relaunch")
        logging.getLogger("test.level").debug("chatter after the raise")

        for handler in logging.getLogger().handlers:
            handler.flush()
        written = Path(tmp).glob("tupperDMX_*.log")
        text = "".join(path.read_text() for path in written)
        _assert("chatter at DEBUG" in text, "what was logged before is kept")
        _assert("chatter after the raise" not in text,
                "and the raised level filters the very next record")


def test_config_round_trips_the_folder(tmp_path, monkeypatch):
    print("test_config_round_trips_the_folder")
    prefs = tmp_path / "userprefs.json"
    monkeypatch.setattr(config_mod, "userprefs_path", lambda: prefs)

    _assert(Config().logging.log_dir == "",
            "a fresh config leaves the folder unset")

    cfg = Config()
    cfg.logging.log_dir = "/var/log/tupper"
    _assert(cfg.save(), "prefs saved")
    _assert(json.loads(prefs.read_text())["logging"]["log_dir"] == "/var/log/tupper",
            "the folder is written to userprefs.json")
    _assert(Config.load().logging.log_dir == "/var/log/tupper",
            "and read back")

    # A prefs file written before the folder was settable.
    data = json.loads(prefs.read_text())
    del data["logging"]["log_dir"]
    prefs.write_text(json.dumps(data))
    _assert(Config.load().logging.log_dir == "",
            "a prefs file with no log_dir loads as unset (= the old folder)")


# --- the Logging page -------------------------------------------------------

def _dialog(cfg=None):
    from ui.settings_dialog import SettingsDialog
    return SettingsDialog(cfg or Config())


def test_folder_field_shows_the_setting_and_the_default(qapp, tmp_path):
    print("test_folder_field_shows_the_setting_and_the_default")
    cfg = Config()
    cfg.logging.log_dir = str(tmp_path)
    (tmp_path / "tupperDMX_20260101.log").write_bytes(b"x" * 2048)

    dlg = _dialog(cfg)
    _assert(dlg.log_dir_edit.text() == str(tmp_path),
            "the field shows the configured folder")
    _assert(dlg.log_dir_edit.placeholderText() == str(DEFAULT_LOG_DIR.resolve()),
            "an empty field reads as the default folder, spelled out")
    _assert(dlg.log_dir_stats.text() == "2.0 KB · 1 file",
            f"the folder's size and file count are shown ({dlg.log_dir_stats.text()})")

    dlg.log_dir_edit.setText(str(tmp_path / "nope"))
    _assert(dlg.log_dir_stats.text() == "not created yet",
            "a folder that isn't there says so")


def test_accept_persists_the_folder(qapp, tmp_path):
    print("test_accept_persists_the_folder")
    cfg = Config()
    dlg = _dialog(cfg)
    target = tmp_path / "shows" / "logs"
    dlg.log_dir_edit.setText(str(target))
    dlg._on_accept()
    _assert(cfg.logging.log_dir == str(target), "the folder reaches the config")
    _assert(target.is_dir(), "accept creates it, so the next launch has it")

    dlg = _dialog(cfg)
    dlg.log_dir_edit.setText("  ")
    dlg._on_accept()
    _assert(cfg.logging.log_dir == "",
            "clearing the field goes back to the default folder")


def test_accept_refuses_an_impossible_folder(qapp, tmp_path):
    print("test_accept_refuses_an_impossible_folder")
    from ui import settings_dialog

    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")

    cfg = Config()
    dlg = _dialog(cfg)
    dlg.log_dir_edit.setText(str(blocker / "logs"))

    warned = []
    original = settings_dialog.QMessageBox.warning
    settings_dialog.QMessageBox.warning = staticmethod(
        lambda *a, **k: warned.append(a))
    try:
        dlg._on_accept()
    finally:
        settings_dialog.QMessageBox.warning = original

    _assert(len(warned) == 1, "a folder that cannot be created pops a warning")
    _assert(dlg.result() == 0, "and the dialog is not accepted")
    _assert(cfg.logging.log_dir == "", "nothing is written to the config")


# --- the live pane ----------------------------------------------------------

def test_tail_streams_and_survives_rotation(qapp, tmp_path):
    print("test_tail_streams_and_survives_rotation")
    from ui.widgets.log_tail import LogTailView

    log = tmp_path / "tupperDMX_20260826.log"
    log.write_text(_log_line("webremote", "INFO", "client connected"))

    pane = LogTailView()
    pane.set_path(log)
    pane._poll()
    _assert("client connected" in pane.view.toPlainText(),
            "the file's existing tail is shown on open")
    _assert(pane.view.blockCount() == 1, "one line in, one line shown")

    with open(log, "a") as handle:
        handle.write(_log_line("dmx.output", "WARNING", "buffer late"))
    pane._poll()
    text = pane.view.toPlainText()
    _assert("buffer late" in text and "client connected" in text,
            "an appended line is added, not re-read from the top")
    _assert(pane.view.blockCount() == 2, "and only once")

    # A half-written line is held back until its newline arrives.
    with open(log, "a") as handle:
        handle.write("2026-08-26 21:18:20 | midi.worker | INFO | MIDI | half")
    pane._poll()
    _assert("half" not in pane.view.toPlainText(),
            "a line without its newline yet is not shown")
    with open(log, "a") as handle:
        handle.write(" a line\n")
    pane._poll()
    _assert("half a line" in pane.view.toPlainText(),
            "and appears whole once it is finished")

    # Rotation: the handler renames ours away and opens a smaller one.
    log.unlink()
    log.write_text(_log_line("system.startup", "INFO", "fresh file"))
    pane._poll()
    _assert("fresh file" in pane.view.toPlainText(),
            "the replacement file is picked up from its top")


def test_tail_polls_only_while_visible_and_unpaused(qapp, tmp_path):
    print("test_tail_polls_only_while_visible_and_unpaused")
    from ui.widgets.log_tail import LogTailView

    log = tmp_path / "tupperDMX_20260826.log"
    log.write_text(_log_line("patch", "INFO", "fixture 12 -> ch 121"))

    pane = LogTailView()
    pane.set_path(log)
    _assert(not pane._timer.isActive(), "a hidden pane doesn't poll")

    pane.show()
    _assert(pane._timer.isActive(), "showing it starts the poll")
    _assert("fixture 12" in pane.view.toPlainText(), "and reads straight away")

    pane.pause_button.setChecked(True)
    _assert(not pane._timer.isActive(), "Pause stops the poll")
    _assert(pane.pause_button.text() == "Resume", "and the button says so")
    with open(log, "a") as handle:
        handle.write(_log_line("cue.engine", "INFO", "while paused"))
    _assert("while paused" not in pane.view.toPlainText(),
            "nothing arrives while paused")

    pane.pause_button.setChecked(False)
    _assert(pane._timer.isActive(), "Resume starts it again")
    _assert("while paused" in pane.view.toPlainText(),
            "and catches up on what it missed")

    pane.clear_button.click()
    _assert(pane.view.toPlainText() == "", "Clear view empties the pane")
    _assert("while paused" in log.read_text(), "and leaves the file alone")

    pane.hide()
    _assert(not pane._timer.isActive(), "hiding it stops the poll")


def test_line_rendering(qapp):
    print("test_line_rendering")
    from ui.widgets import log_tail

    rendered = log_tail.format_line(
        "2026-08-26 21:18:20 | dmx.output           | WARNING  | DMX      | "
        "universe 3 dropped 1 frame")
    _assert(log_tail.LEVEL_COLORS["WARNING"] in rendered,
            "the level is coloured by severity")
    _assert("21:18:20" in rendered and "2026-08-26" not in rendered,
            "the clock is shown, the file's own date is not")
    _assert("dmx.output" in rendered and "universe&nbsp;3" in rendered,
            "logger and message survive, with the columns kept apart")

    plain = log_tail.format_line('  File "main.py", line 12, in <module>')
    _assert("&lt;module&gt;" in plain,
            "a line that isn't a log record is escaped and passed through")


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        test_blank_log_dir_is_the_default_folder()
        for fn in (test_setup_logging_writes_into_the_configured_folder,
                   test_blank_setting_keeps_logging_where_it_always_went,
                   test_level_applies_without_a_relaunch):
            with _restored_logging():
                fn(None)

        monkeypatch = pytest.MonkeyPatch()
        try:
            (tmp_path / "cfg").mkdir()
            test_config_round_trips_the_folder(tmp_path / "cfg", monkeypatch)
        finally:
            monkeypatch.undo()

        for index, fn in enumerate((test_folder_field_shows_the_setting_and_the_default,
                                    test_accept_persists_the_folder,
                                    test_accept_refuses_an_impossible_folder,
                                    test_tail_streams_and_survives_rotation,
                                    test_tail_polls_only_while_visible_and_unpaused)):
            case = tmp_path / f"case{index}"
            case.mkdir()
            fn(app, case)
        test_line_rendering(app)

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
