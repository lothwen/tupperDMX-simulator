"""
Characterization test: the toolbar's SHOW section (ui/toolbar_manager.py).

Covers the project buttons firing their signals (incl. the "…" Import menu
and the split Export button), the show-name display, the autosave-state chip
mapping and the setlist/patch stats footer.

Run with:
    uv run python tests/test_toolbar_show_panel.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QApplication, QPushButton, QToolButton

app = QApplication.instance() or QApplication([])

from core.config import Config, SongConfig
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
    manager.import_bundle_requested.connect(lambda: fired.append("import"))

    for text in ("New", "Open", "Save", "Save as…"):
        _button(toolbar, text).click()
    more = next(b for b in toolbar.findChildren(QToolButton)
                if b.toolTip() == "More show actions")
    import_action = next(a for a in more.menu().actions()
                         if a.text() == "Import bundle…")
    import_action.trigger()
    _assert(fired == ["new", "open", "save", "save_as", "import"],
            "the four SHOW buttons + the … Import action fire their signals")


def test_export_split_button():
    print("test_export_split_button")
    manager = ToolbarManager(Config())
    toolbar = manager.create_toolbar(None)
    fired = []
    manager.export_midi_requested.connect(lambda: fired.append("midi"))
    manager.export_bundle_requested.connect(lambda: fired.append("bundle"))

    export = next(b for b in toolbar.findChildren(QToolButton)
                  if b.objectName() == "ExportSplitButton")
    export.click()
    _assert(fired == ["bundle"], "main click exports the bundle")

    midi_action, bundle_action = export.menu().actions()
    _assert(midi_action.text().endswith("Export MIDI…")
            and not midi_action.isEnabled(),
            "menu MIDI export starts disabled")
    manager.set_export_midi_enabled(True)
    _assert(midi_action.isEnabled(),
            "set_export_midi_enabled(True) enables the action")
    midi_action.trigger()
    bundle_action.trigger()
    _assert(fired == ["bundle", "midi", "bundle"],
            "menu actions fire the export signals")


def test_show_name_and_autosave_chip():
    print("test_show_name_and_autosave_chip")
    manager = ToolbarManager(Config())
    manager.create_toolbar(None)

    manager.set_show_name("Directo Billares 2026")
    text = manager.show_name_label.text()
    _assert("Directo Billares 2026" in text and ".show" in text,
            "show name renders with the dimmed .show suffix")
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


def test_show_stats_footer():
    print("test_show_stats_footer")
    config = Config()
    config.playlist.songs = [
        SongConfig(title="A", cache={"duration": 61.0}),
        SongConfig(title="B", cache={"duration": 120.5}),
    ]
    manager = ToolbarManager(config)
    manager.create_toolbar(None)

    lines = manager.show_stats_label.text().splitlines()
    _assert(lines[0] == "2 songs · 3:01s",
            "stats show song count and summed cache duration")
    _assert(lines[1] == f"patch {len(config.fixtures)} fix",
            "stats show the fixture-patch size")

    config.playlist.songs = [SongConfig(title="A", cache={"duration": 30.0})]
    manager.update_show_stats()
    _assert(manager.show_stats_label.text().startswith("1 song · 0:30s"),
            "singular song count after an update")


def main():
    test_show_buttons_fire_signals()
    test_export_split_button()
    test_show_name_and_autosave_chip()
    test_show_stats_footer()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
