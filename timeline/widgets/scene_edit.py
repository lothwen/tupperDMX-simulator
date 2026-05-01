# ABOUTME: Edit dialog for LED scene clips — 7 channel sliders + scene name.
# ABOUTME: Returns the modified channels dict and name on accept.

from typing import Dict, Optional

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QLineEdit, QSpinBox, QDialogButtonBox,
                             QFormLayout, QFrame)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPalette


CHANNEL_FIELDS = (
    ("brightness", "Brightness"),
    ("blue", "Blue"),
    ("green", "Green"),
    ("red", "Red"),
    ("animation", "Animation"),
    ("speed", "Speed"),
    ("direction", "Direction"),
)


class SceneEditDialog(QDialog):
    """Edit the 7 channel values of a scene group (and its display name)."""

    def __init__(self, name: str, channels: Dict[str, int], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit Scene — {name}")
        self.setModal(True)
        self.resize(360, 360)

        self._spinboxes: Dict[str, QSpinBox] = {}
        self._setup_ui(name, channels)

    def _setup_ui(self, name: str, channels: Dict[str, int]):
        layout = QVBoxLayout(self)

        # Name
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit(name)
        name_row.addWidget(self.name_edit)
        layout.addLayout(name_row)

        # Channel values (0..255 each, matching the controller's range)
        form = QFormLayout()
        for key, label in CHANNEL_FIELDS:
            spin = QSpinBox()
            spin.setMinimum(0)
            spin.setMaximum(255)
            spin.setValue(int(channels.get(key, 0)))
            spin.valueChanged.connect(self._refresh_preview)
            form.addRow(label + ":", spin)
            self._spinboxes[key] = spin
        layout.addLayout(form)

        # Color preview swatch
        self.preview = QFrame()
        self.preview.setFrameShape(QFrame.Shape.Box)
        self.preview.setMinimumHeight(40)
        self.preview.setAutoFillBackground(True)
        layout.addWidget(QLabel("Preview:"))
        layout.addWidget(self.preview)
        self._refresh_preview()

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _refresh_preview(self):
        """Update the preview swatch to reflect the current RGB + brightness."""
        red = self._spinboxes["red"].value()
        green = self._spinboxes["green"].value()
        blue = self._spinboxes["blue"].value()
        brightness = self._spinboxes["brightness"].value()
        scale = max(brightness / 255.0, 0.18)
        color = QColor(int(red * scale), int(green * scale), int(blue * scale))
        palette = self.preview.palette()
        palette.setColor(QPalette.ColorRole.Window, color)
        self.preview.setPalette(palette)

    def get_values(self) -> Optional[Dict]:
        """Return {'name': str, 'channels': {...}} as currently entered."""
        return {
            "name": self.name_edit.text().strip() or "Scene",
            "channels": {k: spin.value() for k, spin in self._spinboxes.items()},
        }
