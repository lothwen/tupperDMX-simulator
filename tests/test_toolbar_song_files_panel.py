"""
Characterization test: the toolbar's SONG FILES cards (ui/toolbar_manager.py).

Covers the three per-file cards (MIDI / BACKING TRACK / GUIDE): the 🗁 load
buttons and dashed empty-state actions firing load_file_requested, the ▷
new-track and ✕ guide-clear header actions, the path-well/empty-action swap
on the update_*_display methods, and the right-anchored path well (overflow
clips on the left so the filename end stays visible) with its full-path
tooltip.

Run with:
    uv run python tests/test_toolbar_song_files_panel.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QFrame, QToolButton

app = QApplication.instance() or QApplication([])

from core.config import Config
from ui.toolbar_manager import ToolbarManager


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _card(toolbar, kind):
    for frame in toolbar.findChildren(QFrame):
        if frame.objectName() == "FileCard" and frame.property("kind") == kind:
            return frame
    raise AssertionError(f"no '{kind}' file card on the toolbar")


def _card_button(card, text):
    for button in card.findChildren(QToolButton):
        if button.text() == text:
            return button
    raise AssertionError(f"no '{text}' button on the card")


def test_load_buttons_fire_signals():
    print("test_load_buttons_fire_signals")
    manager = ToolbarManager(Config())
    toolbar = manager.create_toolbar(None)
    fired = []
    manager.load_file_requested.connect(fired.append)
    manager.new_midi_track_requested.connect(lambda: fired.append("new_track"))
    manager.guide_clear_requested.connect(lambda: fired.append("clear_guide"))

    for kind, file_type in (("midi", "midi"), ("backing", "audio"),
                            ("guide", "guide")):
        _card_button(_card(toolbar, kind), "🗁").click()
        _assert(fired[-1] == file_type,
                f"the {kind} card's 🗁 button requests a '{file_type}' load")

    _card_button(_card(toolbar, "midi"), "▷").click()
    _assert(fired[-1] == "new_track", "▷ requests a new MIDI track")
    _card_button(_card(toolbar, "guide"), "✕").click()
    _assert(fired[-1] == "clear_guide", "✕ requests the guide clear")


def test_empty_state_actions_fire_signals():
    print("test_empty_state_actions_fire_signals")
    manager = ToolbarManager(Config())
    manager.create_toolbar(None)
    fired = []
    manager.load_file_requested.connect(fired.append)

    for button, file_type in ((manager.midi_empty_button, "midi"),
                              (manager.audio_empty_button, "audio"),
                              (manager.guide_empty_button, "guide")):
        button.click()
        _assert(fired[-1] == file_type,
                f"'{button.text()}' requests a '{file_type}' load")


def test_display_swaps_well_and_empty_action():
    print("test_display_swaps_well_and_empty_action")
    manager = ToolbarManager(Config())
    manager.create_toolbar(None)

    _assert(manager.audio_file_label.isHidden()
            and not manager.audio_empty_button.isHidden(),
            "cards start empty: dashed action shown, path well hidden")
    _assert(manager.guide_clear_button.isHidden(),
            "✕ guide clear starts hidden")

    manager.update_audio_file_display("/tmp/song.mp3")
    _assert(not manager.audio_file_label.isHidden()
            and manager.audio_empty_button.isHidden(),
            "a loaded backing shows the path well and hides the action")

    manager.update_guide_file_display("/tmp/guide.mp3")
    _assert(not manager.guide_clear_button.isHidden(),
            "a loaded guide shows the ✕ clear action")
    manager.update_guide_file_display("")
    _assert(manager.guide_clear_button.isHidden()
            and not manager.guide_empty_button.isHidden(),
            "clearing the guide hides ✕ and restores the dashed action")


def test_path_well_and_tooltip():
    print("test_path_well_and_tooltip")
    manager = ToolbarManager(Config())
    manager.create_toolbar(None)

    long_path = "/very/long/path/" + "sub/" * 60 + "el-acantilado-v1.mid"
    manager.update_midi_file_display(long_path)
    _assert(manager.midi_file_label.text() == long_path,
            "the well keeps the full path text")
    _assert(manager.midi_file_label.toolTip() == long_path,
            "the tooltip keeps the full path")
    _assert(manager.midi_file_label.alignment()
            & Qt.AlignmentFlag.AlignRight,
            "right-aligned so an overflow keeps the filename end visible")


def main():
    test_load_buttons_fire_signals()
    test_empty_state_actions_fire_signals()
    test_display_swaps_well_and_empty_action()
    test_path_well_and_tooltip()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
