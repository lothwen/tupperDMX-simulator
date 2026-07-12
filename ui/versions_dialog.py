# ABOUTME: History dialog for show version snapshots: list, pin state and
# ABOUTME: restore (the ShowManager performs the actual rollback + reload).
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QListWidget,
                             QListWidgetItem, QMessageBox, QPushButton,
                             QVBoxLayout)

from core.show import versions


class VersionsDialog(QDialog):
    """Snapshot history of the open show. Restoring first snapshots the
    current state (versions.restore does), so it is always undoable."""

    def __init__(self, show_manager, parent=None):
        super().__init__(parent)
        self.show_manager = show_manager
        self.setWindowTitle("Show history")
        self.setMinimumSize(420, 320)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Snapshots of the show (manifest + owned MIDI files). "
            "Named snapshots are pinned and never pruned."))
        self.version_list = QListWidget()
        layout.addWidget(self.version_list)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.restore_button = QPushButton("Restore")
        buttons.addButton(self.restore_button,
                          QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        self.restore_button.clicked.connect(self._restore_selected)
        layout.addWidget(buttons)

        self._reload_list()

    def _reload_list(self):
        self.version_list.clear()
        show = self.show_manager.show
        if show is None:
            return
        for info in versions.list_versions(show):
            label = info.timestamp.replace("T", " ")
            if info.pinned:
                label += f"  📌 {info.name}" if info.name else "  📌"
            elif info.name:
                label += f"  ({info.name})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, info.version_id)
            self.version_list.addItem(item)

    def _restore_selected(self):
        item = self.version_list.currentItem()
        if item is None:
            return
        version_id = item.data(Qt.ItemDataRole.UserRole)
        reply = QMessageBox.question(
            self, "Restore version",
            f"Restore the show to snapshot '{item.text()}'?\n\n"
            "The current state is snapshotted first, so this can be undone "
            "from this same history.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.show_manager.restore_version(version_id)
        except Exception as e:
            QMessageBox.critical(self, "Restore failed", str(e))
            return
        self._reload_list()
