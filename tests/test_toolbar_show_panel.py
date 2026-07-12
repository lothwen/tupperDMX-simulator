"""
Characterization test: the toolbar's SHOW section (ui/toolbar_manager.py).

Covers the four project buttons firing their signals, the show-name display
and the autosave-state chip mapping.

Run with:
    uv run python tests/test_toolbar_show_panel.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QApplication, QPushButton

app = QApplication.instance() or QApplication([])

from core.config import Config
from ui.toolbar_manager import ToolbarManager


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _button(toolbar, text):
    for b in toolbar.findChildren(QPushButton):
        if b.text() == text:
            return b
    raise AssertionError(f"no '{text}' button on the toolbar")


def test_show_buttons_fire_signals():
    print("test_show_buttons_fire_signals")
    manager = ToolbarManager(Config())
    toolbar = manager.create_toolbar(None)
    fired = []
    manager.new_show_requested.connect(lambda: fired.append("new"))
    manager.open_show_requested.connect(lambda: fired.append("open"))
    manager.save_show_requested.connect(lambda: fired.append("save"))
    manager.save_show_as_requested.connect(lambda: fired.append("save_as"))

    for text in ("New", "Open", "Save", "Save as…"):
        _button(toolbar, text).click()
    _assert(fired == ["new", "open", "save", "save_as"],
            "the four SHOW buttons fire their signals")


def test_show_name_and_autosave_chip():
    print("test_show_name_and_autosave_chip")
    manager = ToolbarManager(Config())
    manager.create_toolbar(None)

    manager.set_show_name("Directo Billares 2026")
    _assert(manager.show_name_label.text() == "Directo Billares 2026.show",
            "show name renders with the .show suffix")
    manager.set_show_name("")
    _assert(manager.show_name_label.text() == "No show", "empty name -> No show")

    manager.set_autosave_state("saved")
    _assert("saved" in manager.autosave_status_label.text(),
            "saved state renders")
    manager.set_autosave_state("pending")
    _assert("autosave" in manager.autosave_status_label.text(),
            "pending state renders")
    manager.set_autosave_state("error")
    _assert("error" in manager.autosave_status_label.text().lower(),
            "error state renders")
    manager.set_autosave_state("bogus")
    _assert(manager.autosave_status_label.text() == "",
            "unknown state clears the chip")


def main():
    test_show_buttons_fire_signals()
    test_show_name_and_autosave_chip()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
