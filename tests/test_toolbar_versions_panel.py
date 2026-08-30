"""
Characterization test: the ☰ menu's version entries (ui/toolbar_manager.py).

The VERSIONS card is gone — Snapshot and the Restore submenu live in the
hamburger menu and stay visible at every bar width. Covers the submenu's
population (labels for autosave vs pinned, newest first), its enablement, the
restore signal and the always-visible Snapshot action.

Run with:
    uv run python tests/test_toolbar_versions_panel.py
"""

import os
import sys
from datetime import datetime

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


def _menu_action(manager, text):
    for action in manager.menu_button.menu().actions():
        if action.text() == text:
            return action
    raise AssertionError(f"no '{text}' entry in the ☰ menu")


def test_versions_populate_the_submenu():
    print("test_versions_populate_the_submenu")
    manager = _manager()
    # Timestamps are dated "today" so the entries render bare clock times
    # (the toolbar prefixes MM-DD only for snapshots from other days).
    today = datetime.now().strftime("%Y-%m-%d")
    infos = [
        VersionInfo(f"{today}T14-32-07", f"{today}T14-32-07", False, ""),
        VersionInfo(f"{today}T14-31-00", f"{today}T14-31-00", False, ""),
        VersionInfo(f"{today}T09-00-00", f"{today}T09-00-00", True, "before gig"),
    ]
    manager.set_versions(infos)

    actions = manager.versions_menu.actions()
    _assert(len(actions) == 3, "every snapshot is listed")
    _assert(actions[0].text() == "Autosave · 14:32:07",
            "autosave label is time-stamped")
    _assert(actions[2].text() == "before gig · 09:00:00",
            "pinned label carries its name")
    _assert(not actions[0].icon().isNull() and not actions[2].icon().isNull(),
            "the pinned/autosave distinction rides the entry icons")
    _assert(manager.versions_menu.isEnabled(), "the submenu enables with versions")


def test_versions_empty():
    print("test_versions_empty")
    manager = _manager()
    manager.set_versions([])
    _assert(manager.versions_menu.actions() == [], "empty list clears the submenu")
    _assert(not manager.versions_menu.isEnabled(),
            "the submenu is disabled with no versions")


def test_restore_signal():
    print("test_restore_signal")
    manager = _manager()
    manager.set_versions([
        VersionInfo("2026-07-12T14-32-07", "2026-07-12T14-32-07", False, ""),
        VersionInfo("2026-07-12T14-31-00", "2026-07-12T14-31-00", False, ""),
    ])
    restored = []
    manager.restore_version_requested.connect(restored.append)
    manager.versions_menu.actions()[1].trigger()
    _assert(restored == ["2026-07-12T14-31-00"],
            "an entry emits its own version_id")


def test_version_entries_survive_folding():
    print("test_version_entries_survive_folding")
    manager = _manager()
    snapshot_action = _menu_action(manager, "Snapshot")
    restore_action = _menu_action(manager, "Restore version")
    fired = []
    manager.snapshot_requested.connect(lambda: fired.append("snapshot"))

    for compact in (False, True, False):
        manager.set_compact(compact)
        _assert(snapshot_action.isVisible() and restore_action.isVisible(),
                f"both stay visible with the bar {'folded' if compact else 'open'}")

    snapshot_action.trigger()
    _assert(fired == ["snapshot"], "Snapshot fires its request")


def main():
    test_versions_populate_the_submenu()
    test_versions_empty()
    test_restore_signal()
    test_version_entries_survive_folding()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
