# ABOUTME: AboutDialog — modal showing build metadata (date, commit, tag).
# ABOUTME: Read-only display, opened from the toolbar's hamburger menu.

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QFormLayout, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout)

from core import buildinfo


class AboutDialog(QDialog):
    """Small modal showing the app name and build provenance."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About TupperDMX")
        self.setModal(True)
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("TupperDMX Simulator")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 4)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        form.addRow("Build date:", self._value_label(buildinfo.BUILD_DATE))
        form.addRow("Commit:", self._value_label(buildinfo.GIT_COMMIT))
        form.addRow("Tag:", self._value_label(buildinfo.GIT_TAG))
        layout.addLayout(form)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

    @staticmethod
    def _value_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label
