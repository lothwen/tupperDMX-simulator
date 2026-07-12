"""
Characterization test: the toolbar's VERSIONS section (ui/toolbar_manager.py).

Covers the snapshot picker population (labels for autosave vs pinned, newest
selected), the autosave/pinned counts, and Restore enablement + restore signal.

Run with:
    uv run python tests/test_toolbar_versions_panel.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from core.config import Config
from core.show.versions import VersionInfo
from ui.toolbar_manager import ToolbarManager


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _manager():
    manager = ToolbarManager(Config())
    manager.create_toolbar(None)
    return manager


def test_versions_populate_and_counts():
    print("test_versions_populate_and_counts")
    manager = _manager()
    infos = [
        VersionInfo("2026-07-12T14-32-07", "2026-07-12T14-32-07", False, ""),
        VersionInfo("2026-07-12T14-31-00", "2026-07-12T14-31-00", False, ""),
        VersionInfo("2026-07-12T09-00-00", "2026-07-12T09-00-00", True, "before gig"),
    ]
    manager.set_versions(infos)

    _assert(manager.versions_combo.count() == 3, "every snapshot is listed")
    _assert(manager.versions_combo.currentIndex() == 0, "newest is selected")
    _assert(manager.versions_combo.itemText(0) == "● Autosave · 14:32:07",
            "autosave label is time-stamped")
    _assert(manager.versions_combo.itemText(2) == "📌 before gig · 09:00:00",
            "pinned label carries its name")
    _assert(manager.versions_combo.itemData(0) == "2026-07-12T14-32-07",
            "item data is the version_id")
    _assert(manager.versions_stats_label.text() == "2 autosaves · 1 pinned",
            "counts split autosaves and pinned")
    _assert(manager.restore_button.isEnabled(), "Restore enables with versions")


def test_versions_empty():
    print("test_versions_empty")
    manager = _manager()
    manager.set_versions([])
    _assert(manager.versions_combo.count() == 0, "empty list clears the picker")
    _assert(not manager.restore_button.isEnabled(),
            "Restore disabled with no versions")
    _assert(manager.versions_stats_label.text() == "0 autosaves · 0 pinned",
            "zero counts render")


def test_restore_signal():
    print("test_restore_signal")
    manager = _manager()
    manager.set_versions([
        VersionInfo("2026-07-12T14-32-07", "2026-07-12T14-32-07", False, ""),
        VersionInfo("2026-07-12T14-31-00", "2026-07-12T14-31-00", False, ""),
    ])
    restored = []
    manager.restore_version_requested.connect(restored.append)
    manager.versions_combo.setCurrentIndex(1)
    manager.restore_button.click()
    _assert(restored == ["2026-07-12T14-31-00"],
            "Restore emits the picked version_id")


def main():
    test_versions_populate_and_counts()
    test_versions_empty()
    test_restore_signal()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
