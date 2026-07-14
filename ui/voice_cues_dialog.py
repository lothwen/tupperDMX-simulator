# ABOUTME: VoiceCuesDialog — per-song editor for spoken guide cues (time +
# ABOUTME: text). Pure editor: cues() returns the result; the caller persists.

from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QTableWidget, QVBoxLayout, QWidget)

from audio import tts
from core.config import VoiceCueConfig
from tupperlogger import get_logger


class VoiceCuesDialog(QDialog):
    """Editor for a song's voice cues — spoken messages ("entra batería")
    fired at a timeline position and mixed onto the voice bus (the musician's
    click side). Rows: time in seconds + free text. Preview renders through
    the TTS cache and plays on the shared engine."""

    def __init__(self, cues: List[VoiceCueConfig], parent=None,
                 audio_engine=None, tts_voice: str = tts.DEFAULT_VOICE):
        super().__init__(parent)
        self.logger = get_logger("ui.voice_cues")
        self.audio_engine = audio_engine
        self.tts_voice = tts_voice

        self.setWindowTitle("Voice cues")
        self.setModal(True)
        self.setMinimumSize(460, 340)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel("Spoken guide messages for this song. They play on the "
                       "VOICE bus (same side as the click in split routing).")
        intro.setWordWrap(True)
        intro.setObjectName("SettingsDimLabel")
        layout.addWidget(intro)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Time (s)", "Text"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(0, 100)
        layout.addWidget(self.table, 1)

        buttons_row = QHBoxLayout()
        add_button = QPushButton("＋ Add")
        add_button.clicked.connect(lambda: self._add_row())
        buttons_row.addWidget(add_button)
        remove_button = QPushButton("－ Remove")
        remove_button.clicked.connect(self._remove_selected)
        buttons_row.addWidget(remove_button)
        self.preview_button = QPushButton("▶ Preview")
        self.preview_button.setToolTip("Render the selected cue and play it "
                                       "on the voice bus")
        self.preview_button.clicked.connect(self._preview_selected)
        self.preview_button.setEnabled(audio_engine is not None)
        buttons_row.addWidget(self.preview_button)
        buttons_row.addStretch(1)
        self.status_label = QLabel()
        self.status_label.setObjectName("SettingsDimLabel")
        buttons_row.addWidget(self.status_label)
        layout.addLayout(buttons_row)

        if not tts.is_available() or not tts.is_voice_installed(tts_voice):
            self.status_label.setText(
                "⚠ TTS voice not ready (Settings ▸ AUDIO) — cues stay silent.")

        dialog_buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        dialog_buttons.accepted.connect(self.accept)
        dialog_buttons.rejected.connect(self.reject)
        layout.addWidget(dialog_buttons)

        for cue in cues:
            self._add_row(cue.time, cue.text)

    # ------------------------------------------------------------------
    def _add_row(self, time_sec: float = 0.0, text: str = "") -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)

        spin = QDoubleSpinBox()
        spin.setRange(0.0, 36000.0)
        spin.setDecimals(2)
        spin.setSingleStep(0.5)
        spin.setValue(time_sec)
        self.table.setCellWidget(row, 0, spin)

        edit = QLineEdit(text)
        edit.setPlaceholderText("e.g. entra batería")
        self.table.setCellWidget(row, 1, edit)

        if not text:
            self.table.setCurrentCell(row, 1)
            edit.setFocus()

    def _remove_selected(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _row_cue(self, row: int) -> Optional[VoiceCueConfig]:
        spin = self.table.cellWidget(row, 0)
        edit = self.table.cellWidget(row, 1)
        if spin is None or edit is None:
            return None
        text = edit.text().strip()
        if not text:
            return None
        return VoiceCueConfig(time=spin.value(), text=text)

    def _preview_selected(self) -> None:
        row = self.table.currentRow()
        cue = self._row_cue(row) if row >= 0 else None
        if cue is None:
            self.status_label.setText("Select a cue with text to preview.")
            return
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            samples = tts.render(cue.text, self.tts_voice)
        finally:
            self.unsetCursor()
        if samples is None:
            self.status_label.setText(
                "⚠ Could not render — voice not ready (Settings ▸ AUDIO).")
            return
        self.status_label.setText("")
        if self.audio_engine is not None:
            self.audio_engine.play_voice(samples)

    def cues(self) -> List[VoiceCueConfig]:
        """The edited cue list, sorted by time; blank rows are dropped."""
        result = []
        for row in range(self.table.rowCount()):
            cue = self._row_cue(row)
            if cue is not None:
                result.append(cue)
        result.sort(key=lambda c: c.time)
        return result
