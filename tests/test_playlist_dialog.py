"""
Characterization test: the playlist manager (ui/playlist_dialog.py) and its
import picker (ui/import_songs_dialog.py).

Drives the real dialogs offscreen against a recording fake controller (the
playlist manager's only collaborator) and real show folders (the import
picker's): rows mirror the setlist, the filter hides rows and switches
reordering off, the move/delete/new buttons reach the controller and the table
follows, every per-song control writes through, the BPM field commits once per
burst of steps, and the picker keeps checks per show, warns on a differing
patch and asks how the audio comes in.

Run with:
    uv run python tests/test_playlist_dialog.py
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
                             QMessageBox, QPushButton, QSpinBox, QToolButton)

app = QApplication.instance() or QApplication([])

import pytest

from core import fixture_library
from core.config import FixtureInstance, SongConfig
from core.show import store
from ui import playlist_dialog
from ui.import_songs_dialog import ImportSongsDialog
from ui.playlist_dialog import (COL_BACKING, COL_BPM, COL_GRIP, COL_GUIDE,
                                COL_KEY, COL_NAME, COL_ON, COL_PRECOUNT,
                                PlaylistDialog)
from ui.widgets.toggle_switch import ToggleSwitch


@pytest.fixture(autouse=True)
def _reset_globals():
    yield
    fixture_library.set_show_fixtures_dir(None)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


class FakeController:
    """What PlaylistDialog asks of ui.playlist_controller.PlaylistController:
    the setlist as plain data, every edit recorded (and applied, so the
    rebuilt table can be checked)."""

    def __init__(self, songs):
        self._songs = songs
        self.calls = []
        self.owned = {}          # song id -> files deleting it would remove
        self.failed = []         # what remove_song reports as undeletable

    def songs(self):
        return self._songs

    def current_song(self):
        return self._songs[0] if self._songs else None

    def show_name(self):
        return "Gira"

    def other_shows(self):
        return []

    def patch(self):
        return []

    def song_bpm(self, song):
        return song.cache.get("bpm", 120)

    def new_song(self):
        self._songs.append(SongConfig(id=f"new{len(self._songs)}"))
        self.calls.append(("new_song",))
        return len(self._songs) - 1

    def move_song(self, src, dst):
        self._songs.insert(dst, self._songs.pop(src))
        self.calls.append(("move_song", src, dst))

    def song_owned_files(self, song):
        return self.owned.get(song.id, [])

    def remove_song(self, index, delete_files=False):
        self._songs.pop(index)
        self.calls.append(("remove_song", index, delete_files))
        return self.failed

    def set_song_enabled(self, song, enabled):
        song.enabled = enabled
        self.calls.append(("enabled", song.id, enabled))

    def set_song_title(self, song, title):
        song.title = title
        self.calls.append(("title", song.id, title))

    def set_song_key(self, song, key):
        song.key = key
        self.calls.append(("key", song.id, key))

    def set_song_precount(self, song, enabled):
        song.precount = enabled
        self.calls.append(("precount", song.id, enabled))

    def set_song_bpm(self, song, bpm):
        song.cache["bpm"] = bpm
        self.calls.append(("bpm", song.id, bpm))

    def set_song_track(self, song, kind, path):
        setattr(song, kind, path)
        self.calls.append(("track", song.id, kind, path))

    def import_songs(self, songs, copy_audio):
        self.calls.append(("import", [s.title for s in songs], copy_audio))
        return [], []


def _songs():
    return [SongConfig(id="a", title="Marea", key="Am", audio="/x/marea.mp3",
                       cache={"bpm": 100}),
            SongConfig(id="b", title="Faro", guide="/x/faro.mp3"),
            SongConfig(id="c", title="", enabled=False)]


class _Answer:
    """Answer the next hand-built QMessageBox with the button labelled `text`
    — after running `before`, if given — and record what it offered."""

    def __init__(self, text, before=None):
        self.text, self.before = text, before
        self.offered, self.informative = [], ""

    def __enter__(self):
        self._original = QMessageBox.exec, QMessageBox.clickedButton
        answer = self

        def exec_(box):
            answer.offered = [b.text().replace("&", "") for b in box.buttons()]
            answer.informative = box.informativeText()
            if answer.before:
                answer.before()
            return 0

        QMessageBox.exec = exec_
        QMessageBox.clickedButton = lambda box: next(
            b for b in box.buttons() if b.text().replace("&", "") == answer.text)
        return self

    def __exit__(self, *_exc):
        QMessageBox.exec, QMessageBox.clickedButton = self._original


def _cell(dialog, row, col, kind):
    return dialog.table.cellWidget(row, col).findChild(kind)


def _names(dialog):
    return [dialog.table.item(r, COL_NAME).text()
            for r in range(dialog.table.rowCount())]


def test_rows_mirror_the_setlist():
    print("test_rows_mirror_the_setlist")
    controller = FakeController(_songs())
    dialog = PlaylistDialog(controller)

    _assert(dialog.windowTitle() == "Playlist — Gira", "titled after the show")
    _assert(_names(dialog) == ["Marea", "Faro", "Song 3"],
            "one row per song; an untitled one shows its placeholder")
    _assert(dialog.table.item(2, COL_NAME).font().strikeOut(),
            "a disabled song is struck through")
    _assert(not _cell(dialog, 2, COL_ON, QCheckBox).isChecked(),
            "and its ON box is clear")
    _assert(_cell(dialog, 0, COL_BPM, QSpinBox).value() == 100,
            "BPM read through the controller")
    _assert(_cell(dialog, 0, COL_KEY, QComboBox).currentText() == "Am", "key shown")
    _assert(dialog.count_label.text() == "3 songs · 2 enabled · 1 disabled",
            "footer counts")
    _assert(_cell(dialog, 0, COL_GUIDE, QPushButton).text() == "+ Pick file…",
            "no guide: the pick button")
    _assert(_cell(dialog, 0, COL_BACKING, QToolButton) is not None,
            "a backing: the chip with its ✕")


def _hover(table, col, row=0):
    """A plain mouse move (no button) over the middle of a cell."""
    rect = table.visualRect(table.model().index(row, col))
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(rect.center()),
                        QPointF(table.viewport().mapToGlobal(rect.center())),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(table.viewport(), event)
    return table.viewport().cursor().shape()


def test_grip_shows_a_hand():
    print("test_grip_shows_a_hand")
    dialog = PlaylistDialog(FakeController(_songs()))
    dialog.resize(1260, 600)
    dialog.show()
    table = dialog.table
    _assert(_hover(table, COL_GRIP) == Qt.CursorShape.OpenHandCursor,
            "an open hand over the grip dots")
    _assert(_hover(table, COL_NAME) == Qt.CursorShape.ArrowCursor,
            "back to the arrow off the grip")
    _assert(table.cellWidget(0, COL_ON).cursor().shape()
            == Qt.CursorShape.ArrowCursor,
            "cell widgets keep their own arrow, never the grip's hand")
    dialog.filter_edit.setText("mar")
    _assert(_hover(table, COL_GRIP) == Qt.CursorShape.ArrowCursor,
            "no hand while filtering: rows cannot be dragged then")


def test_filter_hides_rows_and_stops_reordering():
    print("test_filter_hides_rows_and_stops_reordering")
    controller = FakeController(_songs())
    dialog = PlaylistDialog(controller)
    dialog.table.selectRow(0)
    _assert(dialog.down_button.isEnabled() and not dialog.up_button.isEnabled(),
            "the first row can only move down")

    dialog.filter_edit.setText("FAR")
    _assert([dialog.table.isRowHidden(r) for r in range(3)] == [True, False, True],
            "case-insensitive title match")
    dialog.table.selectRow(1)
    _assert(not dialog.up_button.isEnabled() and not dialog.down_button.isEnabled()
            and not dialog.table.dragEnabled(),
            "reordering is off while filtering")
    dialog._step(-1)
    _assert(controller.calls == [], "Ctrl+↑ does nothing while filtering")

    dialog.filter_edit.clear()
    _assert(not any(dialog.table.isRowHidden(r) for r in range(3))
            and dialog.table.dragEnabled(), "clearing the filter restores all")


def test_move_delete_and_new():
    print("test_move_delete_and_new")
    controller = FakeController(_songs())
    dialog = PlaylistDialog(controller)
    dialog.show()                        # as exec() does

    dialog.table.selectRow(1)
    dialog.up_button.click()
    _assert(controller.calls[-1] == ("move_song", 1, 0), "▲ moves the row up")
    _assert(_names(dialog)[:2] == ["Faro", "Marea"]
            and dialog._selected_row() == 0,
            "the table follows and the moved song stays selected")

    dialog.table.move_requested.emit(0, 2)
    _assert(controller.calls[-1] == ("move_song", 0, 2), "a drop moves too")

    controller.owned = {"a": ["/show/midis/marea.mid"],
                        "b": ["/show/midis/faro.mid", "/show/audio/faro.wav"]}
    dialog.table.selectRow(0)            # Marea: a song with files of its own
    with _Answer("Cancel") as answer:
        dialog.delete_button.click()
    _assert(controller.calls[-1][0] != "remove_song", "Cancel keeps the song")
    _assert("Remove, keep files" in answer.offered
            and "Remove and delete files" in answer.offered
            and "marea.mid" in answer.informative,
            "files of its own: keep or delete them, and which they are")
    with _Answer("Remove, keep files"):
        dialog.delete_button.click()
    _assert(controller.calls[-1] == ("remove_song", 0, False),
            "removed, its files kept")
    _assert(dialog.table.rowCount() == 2 and dialog._selected_row() == 0,
            "the row below takes the selection")

    with _Answer("Remove", before=dialog.reject) as answer:
        dialog.delete_button.click()     # the untitled song: no files
    _assert(answer.offered == ["Remove", "Cancel"],
            "no files of its own: nothing to choose")
    _assert(controller.calls[-1] == ("remove_song", 0, False)
            and len(controller.calls) == 3,
            "a show opened while the question was up: nothing is removed")
    dialog.show()

    warnings = []
    original = QMessageBox.warning
    controller.failed = ["/show/audio/faro.wav"]
    try:
        QMessageBox.warning = staticmethod(lambda *a, **k: warnings.append(a[2]))
        dialog.table.selectRow(1)        # Faro
        with _Answer("Remove and delete files"):
            dialog.delete_button.click()
    finally:
        QMessageBox.warning = original
    _assert(controller.calls[-1] == ("remove_song", 1, True),
            "removed with its files")
    _assert(warnings and "faro.wav" in warnings[0],
            "a file that could not be deleted is reported")

    dialog.filter_edit.setText("zzz")
    dialog.new_button.click()
    _assert(controller.calls[-1] == ("new_song",), "New song asks the controller")
    _assert(dialog.filter_edit.text() == "" and dialog._selected_row() == 1,
            "the filter clears and the new row is selected")
    _assert(dialog.table.state() == dialog.table.State.EditingState,
            "its name opens for typing")


def test_per_song_controls_write_through():
    print("test_per_song_controls_write_through")
    controller = FakeController(_songs())
    dialog = PlaylistDialog(controller)
    marea, faro, untitled = controller.songs()

    _cell(dialog, 0, COL_ON, QCheckBox).setChecked(False)
    _assert(controller.calls[-1] == ("enabled", "a", False)
            and dialog.table.item(0, COL_NAME).font().strikeOut()
            and dialog.count_label.text().endswith("2 disabled"),
            "ON off: written, struck through, counted")

    dialog.table.item(1, COL_NAME).setText("  Faro Norte ")
    _assert(controller.calls[-1] == ("title", "b", "Faro Norte"), "rename, trimmed")
    count = len(controller.calls)
    dialog.table.item(2, COL_NAME).setText("Song 3")
    _assert(len(controller.calls) == count and untitled.title == "",
            "leaving an untitled song's placeholder as it was writes nothing")

    key = _cell(dialog, 1, COL_KEY, QComboBox)
    key.setCurrentText("F#m")
    key.activated.emit(key.currentIndex())
    _assert(controller.calls[-1] == ("key", "b", "F#m"), "key picked")

    _cell(dialog, 0, COL_PRECOUNT, ToggleSwitch).setChecked(False)
    _assert(controller.calls[-1] == ("precount", "a", False), "precount toggled")

    spin = _cell(dialog, 0, COL_BPM, QSpinBox)
    count = len(controller.calls)
    for value in (101, 102, 103):
        spin.setValue(value)
    _assert(len(controller.calls) == count, "arrow steps wait for the debounce")
    dialog._bpm_timer.timeout.emit()
    _assert(controller.calls[count:] == [("bpm", "a", 103)],
            "one write, the last value")
    spin.setValue(104)
    dialog.done(0)
    _assert(controller.calls[-1] == ("bpm", "a", 104),
            "closing writes a BPM still waiting")


def test_track_pick_and_clear():
    print("test_track_pick_and_clear")
    controller = FakeController(_songs())
    dialog = PlaylistDialog(controller)
    original = QFileDialog.getOpenFileName
    try:
        QFileDialog.getOpenFileName = staticmethod(
            lambda *a, **k: ("/music/guia.wav", ""))
        _cell(dialog, 0, COL_GUIDE, QPushButton).click()
    finally:
        QFileDialog.getOpenFileName = original
    _assert(controller.calls[-1] == ("track", "a", "guide", "/music/guia.wav"),
            "the picked file becomes the guide")
    _assert(_cell(dialog, 0, COL_GUIDE, QToolButton) is not None,
            "the row now shows its chip")
    _assert(dialog._last_dir == "/music", "the next pick opens there")

    _cell(dialog, 0, COL_BACKING, QToolButton).click()
    _assert(controller.calls[-1] == ("track", "a", "audio", None),
            "✕ clears the backing")
    _assert(_cell(dialog, 0, COL_BACKING, QPushButton).text() == "+ Pick file…",
            "and the pick button returns")


def _two_shows(tmp):
    root = Path(tmp)
    first = store.create_show(root, "Verano")
    first.playlist.songs.extend([
        SongConfig(id="v1", title="Norte", audio=str(root / "norte.mp3")),
        SongConfig(id="v2", title="Puerto")])
    first.fixtures.append(FixtureInstance(type="ledbar", dmx_start=1, channel=4))
    store.save_manifest(first)
    second = store.create_show(root, "Acustico")
    second.playlist.songs.append(SongConfig(id="a1", title="Cenizas"))
    store.save_manifest(second)
    (root / "Roto.show").mkdir()
    (root / "Roto.show" / "show.json").write_text("{nope")
    return [first.root, second.root, root / "Roto.show"]


def test_import_picker():
    print("test_import_picker")
    with tempfile.TemporaryDirectory() as tmp:
        roots = _two_shows(tmp)
        picker = ImportSongsDialog(roots, [])

        _assert(picker.projects.count() == 3, "every show listed")
        _assert(picker.projects.item(2).flags() == Qt.ItemFlag.NoItemFlags,
                "an unreadable show is listed but cannot be picked")
        _assert(picker.song_list.topLevelItemCount() == 2
                and not picker.patch_warning.isHidden(),
                "the first show opens, warned about its patch")
        _assert("channel 4" in picker.patch_warning.text(),
                "the warning names the MIDI channel")
        _assert(not picker.import_button.isEnabled()
                and picker.status_label.text() == "Select songs to import",
                "nothing checked: Import is off")

        picker.select_all.click()
        _assert(picker.select_all.checkState() == Qt.CheckState.Checked
                and len(picker.selected_songs()) == 2, "Select all checks the show")
        picker.select_all.click()
        _assert(picker.selected_songs() == [], "and again clears it")

        picker.song_list.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
        _assert(picker.select_all.checkState() == Qt.CheckState.PartiallyChecked,
                "some checked: Select all shows partial")
        picker.projects.setCurrentRow(1)
        _assert(picker.patch_warning.isHidden(), "a matching patch: no warning")
        picker.song_list.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
        picker.projects.setCurrentRow(0)
        _assert(picker.song_list.topLevelItem(0).checkState(0)
                == Qt.CheckState.Checked, "checks survive switching shows")
        _assert([s.title for s in picker.selected_songs()] == ["Norte", "Cenizas"]
                and picker.status_label.text() == "2 songs selected from 2 projects",
                "one import can draw on several shows")

        original_exec, original_clicked = QMessageBox.exec, QMessageBox.clickedButton
        try:
            QMessageBox.exec = lambda self: 0
            QMessageBox.clickedButton = lambda self: next(
                b for b in self.buttons() if b.text() == "Link to original files")
            picker.import_button.click()
        finally:
            QMessageBox.exec, QMessageBox.clickedButton = original_exec, original_clicked
        _assert(picker.result() == picker.DialogCode.Accepted
                and picker.copy_audio() is False,
                "songs with audio ask how it comes in (here: link)")


def test_import_flows_into_the_controller():
    print("test_import_flows_into_the_controller")
    with tempfile.TemporaryDirectory() as tmp:
        roots = _two_shows(tmp)
        controller = FakeController(_songs())
        controller.other_shows = lambda: roots[1:2]
        dialog = PlaylistDialog(controller)
        dialog.show()                    # as exec() does

        class Picked(ImportSongsDialog):
            def exec(self):
                self.song_list.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
                return self.DialogCode.Accepted

        original = playlist_dialog.ImportSongsDialog
        try:
            playlist_dialog.ImportSongsDialog = Picked
            dialog.import_button.click()
        finally:
            playlist_dialog.ImportSongsDialog = original
        _assert(controller.calls[-1] == ("import", ["Cenizas"], True),
                "the checked songs reach the controller, audio copied by default")


def main():
    try:
        test_rows_mirror_the_setlist()
        test_grip_shows_a_hand()
        test_filter_hides_rows_and_stops_reordering()
        test_move_delete_and_new()
        test_per_song_controls_write_through()
        test_track_pick_and_clear()
        test_import_picker()
        test_import_flows_into_the_controller()
    finally:
        fixture_library.set_show_fixtures_dir(None)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
