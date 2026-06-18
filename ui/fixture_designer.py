# ABOUTME: Stage scenario designer UI — a floating panel to add / address / delete
# ABOUTME: fixtures on the virtual stage. Placement itself is done by dragging fixtures.
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QDialog, QVBoxLayout, QFormLayout, QHBoxLayout,
    QPushButton, QComboBox, QSpinBox, QLabel, QGroupBox, QDialogButtonBox,
)

from tupperlogger import get_logger
from config import known_fixture_types


def _suggest_free_dmx_start(stage_controller) -> int:
    """First DMX address past the currently patched channels (1 if empty)."""
    used = stage_controller.config.get_used_dmx_channels()
    highest = 0
    for channels in used.values():
        if channels:
            highest = max(highest, max(channels))
    return min(512, highest + 1) if highest else 1


class AddFixtureDialog(QDialog):
    """Pick a fixture type from the library + a DMX start address."""

    def __init__(self, stage_controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add fixture")

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.type_combo = QComboBox()
        for name in sorted(known_fixture_types()):
            self.type_combo.addItem(name)
        form.addRow("Type", self.type_combo)

        self.dmx_spin = QSpinBox()
        self.dmx_spin.setRange(1, 512)
        self.dmx_spin.setValue(_suggest_free_dmx_start(stage_controller))
        form.addRow("DMX start", self.dmx_spin)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        """Return (type_name, dmx_start) for the chosen fixture."""
        return self.type_combo.currentText(), self.dmx_spin.value()


class FixtureDesignerPanel(QWidget):
    """Floating designer panel: add a fixture, then drag it on the stage to place
    it; the inspector re-addresses or deletes the current selection.

    All edits funnel through ``on_patch_changed`` so the caller marks the patch
    dirty and persists it.
    """

    def __init__(self, stage_controller, on_patch_changed, parent=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("Stage Designer")
        self._stage = stage_controller
        self._notify = on_patch_changed
        self._current = None  # currently selected FixtureInstance
        self.logger = get_logger("ui.fixture_designer")

        layout = QVBoxLayout(self)

        add_button = QPushButton("Add fixture…")
        add_button.clicked.connect(self._on_add)
        layout.addWidget(add_button)

        # Inspector for the current selection.
        self._inspector = QGroupBox("Selected fixture")
        form = QFormLayout(self._inspector)

        self._type_label = QLabel("—")
        form.addRow("Type", self._type_label)

        self._dmx_spin = QSpinBox()
        self._dmx_spin.setRange(1, 512)
        self._dmx_spin.valueChanged.connect(self._on_dmx_changed)
        form.addRow("DMX start", self._dmx_spin)

        self._pos_label = QLabel("—")
        form.addRow("Position", self._pos_label)

        self._delete_button = QPushButton("Delete")
        self._delete_button.clicked.connect(self._on_delete)
        form.addRow(self._delete_button)

        layout.addWidget(self._inspector)
        layout.addStretch(1)

        hint = QLabel("Drag a fixture on the stage to move it.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        stage_controller.fixture_selected.connect(self._on_selected)
        stage_controller.fixture_moved.connect(self._on_moved)

        self._set_current(None)

    # ------------------------------------------------------------------
    # Selection / inspector
    # ------------------------------------------------------------------
    def _set_current(self, instance) -> None:
        self._current = instance
        enabled = instance is not None
        self._inspector.setEnabled(enabled)
        if not enabled:
            self._type_label.setText("—")
            self._pos_label.setText("—")
            return
        self._type_label.setText(instance.type)
        # Block the signal so populating the spin doesn't fire _on_dmx_changed.
        self._dmx_spin.blockSignals(True)
        self._dmx_spin.setValue(instance.dmx_start)
        self._dmx_spin.blockSignals(False)
        self._update_pos_label(instance)

    def _update_pos_label(self, instance) -> None:
        if instance.type == "ledbar" and instance.bars:
            self._pos_label.setText(f"{len(instance.bars)} bars")
        else:
            self._pos_label.setText(f"({instance.x}, {instance.y})")

    def _on_selected(self, instance) -> None:
        self._set_current(instance)

    def _on_moved(self, instance) -> None:
        if instance is self._current:
            self._update_pos_label(instance)
        self._notify()

    # ------------------------------------------------------------------
    # Edits
    # ------------------------------------------------------------------
    def _on_add(self) -> None:
        dialog = AddFixtureDialog(self._stage, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        type_name, dmx_start = dialog.values()
        # Drop new fixtures at the stage centre; the user drags from there.
        rect = self._stage.scene.sceneRect()
        instance = self._stage.add_fixture(
            type_name, dmx_start, int(rect.width() / 2), int(rect.height() / 2)
        )
        self._notify()
        self._set_current(instance)

    def _on_dmx_changed(self, value: int) -> None:
        if self._current is None:
            return
        self._current.dmx_start = int(value)
        self._notify()

    def _on_delete(self) -> None:
        if self._current is None:
            return
        removed = self._stage.remove_selected()
        if removed:
            self._notify()
            self._set_current(None)
