"""
Characterization test: the toolbar's SETLIST + SONG FILES sections
(ui/toolbar_manager.py).

Covers the song-selector combobox (populate without firing selection, pick →
song_selected by playlist index, disabled songs left out, not editable, list
drops below), the + / playlist-manager setlist actions,
the counter badge / song count, and the footer (BPM / offset / linked chip).

Run with:
    uv run python tests/test_toolbar_setlist_panel.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QApplication, QStyle, QStyleOptionComboBox

app = QApplication.instance() or QApplication([])

from core.config import Config
from ui import theme
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


def test_setlist_pick():
    print("test_setlist_pick")
    manager = _manager()
    manager.set_setlist(["Intro", "El Acantilado", "Marea"], 0)

    picked = []
    manager.song_selected.connect(picked.append)
    manager.song_combo.activated.emit(2)  # user picks the third song
    _assert(picked == [2], "a dropdown pick fires song_selected(index)")
    _assert(not manager.song_combo.isEditable(),
            "the combobox only picks: renaming is the playlist manager's")


def test_setlist_leaves_out_disabled_songs():
    print("test_setlist_leaves_out_disabled_songs")
    manager = _manager()
    combo = manager.song_combo
    titles = ["Intro", "Descanso", "Marea", "Bis"]
    manager.set_setlist(titles, 0, [True, False, True, False])

    _assert([combo.itemText(i) for i in range(combo.count())] == ["Intro", "Marea"],
            "disabled songs are left out")
    picked = []
    manager.song_selected.connect(picked.append)
    combo.activated.emit(1)
    _assert(picked == [2], "a pick reports the song's playlist index, not its row")

    manager.set_setlist(titles, 3, [True, False, True, False])
    _assert([combo.itemText(i) for i in range(combo.count())]
            == ["Intro", "Marea", "Bis"] and combo.currentText() == "Bis",
            "a disabled CURRENT song is still shown, selected")


def test_setlist_opens_below():
    print("test_setlist_opens_below")
    manager = ToolbarManager(Config())
    bar = manager.create_toolbar(None)
    # The theme on this widget only: the app-wide stylesheet is shared by
    # every test in the run.
    bar.setStyleSheet(theme.load_qss())
    bar.resize(1500, 120)
    bar.move(50, 50)
    bar.show()
    combo = manager.song_combo
    manager.set_setlist([f"Tema {i}" for i in range(12)], 9)

    option = QStyleOptionComboBox()
    combo.initStyleOption(option)
    _assert(combo.style().styleHint(QStyle.StyleHint.SH_ComboBox_Popup,
                                    option, combo) == 0,
            "a plain drop-down list, not the menu laid over the selector")
    combo.showPopup()
    top = combo.view().window().geometry().top()
    combo.hidePopup()
    _assert(top >= combo.mapToGlobal(combo.rect().bottomLeft()).y(),
            "a late song still opens the list below the selector")
    bar.close()


def test_new_and_playlist_actions():
    print("test_new_and_playlist_actions")
    manager = _manager()
    fired = []
    manager.new_song_requested.connect(lambda: fired.append("new"))
    manager.playlist_requested.connect(lambda: fired.append("playlist"))

    manager.new_song_button.click()
    _assert(fired == ["new"], "+ requests a new song")
    manager.playlist_button.click()
    _assert(fired == ["new", "playlist"], "the playlist button opens the manager")

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
    test_setlist_pick()
    test_setlist_leaves_out_disabled_songs()
    test_setlist_opens_below()
    test_new_and_playlist_actions()
    test_counter_and_footer()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
