"""
Characterization test: the toolbar's SETLIST + SONG FILES sections
(ui/toolbar_manager.py).

Covers the song-selector combobox (populate without firing selection, pick →
song_selected, edit + Enter → song_title_edited), the +/🗑 setlist actions,
the counter badge / song count, and the footer (BPM / offset / linked chip).

Run with:
    uv run python tests/test_toolbar_setlist_panel.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from core.config import Config
from ui.toolbar_manager import ToolbarManager


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _manager():
    manager = ToolbarManager(Config())
    manager.create_toolbar(None)
    return manager


def test_setlist_populate_is_silent():
    print("test_setlist_populate_is_silent")
    manager = _manager()
    selected = []
    manager.song_selected.connect(selected.append)

    manager.set_setlist(["Intro", "El Acantilado", "Marea"], 1)
    _assert([manager.song_combo.itemText(i) for i in range(manager.song_combo.count())]
            == ["Intro", "El Acantilado", "Marea"], "combobox holds every title")
    _assert(manager.song_combo.currentText() == "El Acantilado",
            "current index is selected")
    _assert(selected == [], "repopulating never fires song_selected")

    manager.set_setlist([], 0)
    _assert(manager.song_combo.count() == 0 and manager.song_combo.currentText() == "",
            "empty setlist clears the combobox")


def test_setlist_pick_and_rename():
    print("test_setlist_pick_and_rename")
    manager = _manager()
    manager.set_setlist(["Intro", "El Acantilado", "Marea"], 0)

    picked = []
    manager.song_selected.connect(picked.append)
    manager.song_combo.activated.emit(2)  # user picks the third song
    _assert(picked == [2], "a dropdown pick fires song_selected(index)")

    renamed = []
    manager.song_title_edited.connect(renamed.append)
    manager.song_combo.setEditText("Marea Alta")
    manager.song_combo.lineEdit().returnPressed.emit()
    _assert(renamed == ["Marea Alta"],
            "editing the text + Enter fires song_title_edited")


def test_new_and_remove_actions():
    print("test_new_and_remove_actions")
    manager = _manager()
    fired = []
    manager.new_song_requested.connect(lambda: fired.append("new"))
    manager.remove_song_requested.connect(lambda: fired.append("remove"))

    manager.new_song_button.click()
    _assert(fired == ["new"], "+ requests a new song")
    manager.remove_button.click()
    _assert(fired == ["new", "remove"], "🗑 requests the removal")

    manager.set_setlist([], 0)
    manager.set_navigation_enabled(False, False)
    _assert(manager.new_song_button.isEnabled(),
            "+ stays enabled on an empty setlist (it is how the first song "
            "can be made)")


def test_counter_and_footer():
    print("test_counter_and_footer")
    manager = _manager()

    manager.update_song_counter(5, 11)
    _assert(manager.setlist_counter_badge.text() == "05/11",
            "counter badge is zero-padded current/total")
    _assert(manager.setlist_count_label.text() == "11 songs", "header song count")
    manager.update_song_counter(1, 1)
    _assert(manager.setlist_count_label.text() == "1 song", "singular song count")

    manager.update_song_info("El Acantilado", "linked", bpm=142, offset=0.32)
    _assert(manager.bpm_label.text() == "142 BPM", "footer shows BPM")
    _assert(manager.offset_label.text() == "offset +0.32s", "footer shows offset")
    _assert("linked" in manager.linked_chip.text(), "footer shows linked chip")

    manager.set_song_bpm(150)
    _assert(manager.bpm_label.text() == "150 BPM", "live BPM update")

    manager.update_song_info("", "")
    _assert(manager.bpm_label.text() == "" and manager.offset_label.text() == "",
            "empty state clears BPM and offset")


def main():
    test_setlist_populate_is_silent()
    test_setlist_pick_and_rename()
    test_new_and_remove_actions()
    test_counter_and_footer()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
