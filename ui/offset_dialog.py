# ABOUTME: OffsetRippleDialog — what should follow the audio track a waveform
# ABOUTME: drag just moved: the show, the other track, both or nothing.
"""The question a waveform drag asks when it lands.

An offset moves ONE audio track: the clips, song parts and markers are anchored
in timeline seconds and the other track carries its own offset, so nothing else
follows on its own. Right the first time a track is aligned, wrong every time
after — hence this dialog, which is the whole decision in one place: the show,
the other track, an exact offset typed by hand, or none of it.

Everything reacts to the offset, because the offset IS the delta: retype it and
the headline, the checkbox and whether the show can follow at all change with
it. That is why this is a real dialog rather than a QMessageBox carrying a
widget — a message box can hold a checkbox, but not a value the rest of the
text depends on.

**Every figure on it is an ABSOLUTE offset, written as `from -> to`** — the same
number that lands in `show.json`, never a relative nudge. The distance travelled
is real (it is what the show and the other track move by) but it is never the
thing shown: one dialog quoting both a delta and an absolute is a dialog whose
numbers cannot be checked against anything on disk.
"""

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QHBoxLayout, QLabel,
                             QVBoxLayout, QWidget)

#: What the drag decided to do with everything that is not the dragged track.
#: AUDIO_ONLY keeps the offset and moves no clips; CANCEL keeps nothing at all.
CHOICE_CANCEL = "cancel"
CHOICE_AUDIO_ONLY = "audio_only"
CHOICE_MOVE_SHOW = "move_show"

#: Matches Config's own validation of `offset` / `guide_offset`.
OFFSET_LIMIT = 3600.0


class OffsetRippleDialog(QDialog):
    """Ask what follows the track a drag moved from `old_offset` to
    `new_offset`.

    `show_start` is the earliest thing the show has on this song's timeline, or
    None when the song is empty — the difference between "the show may follow"
    and "there is no show to move", which no typed offset can change.
    `other_label`/`other_offset` name the track the drag did not move and where
    it sits now; `other_label` is None when the song has only one.
    """

    def __init__(self, parent, *, moved_label: str,
                 other_label: Optional[str], other_offset: float = 0.0,
                 old_offset: float, new_offset: float,
                 show_start: Optional[float]):
        super().__init__(parent)
        self.setWindowTitle("Move show with audio")
        self._old_offset = float(old_offset)
        self._dragged_offset = float(new_offset)
        self._show_start = show_start
        self._other_label = other_label
        self._other_offset = float(other_offset)
        self._moved_label = moved_label
        self._choice = CHOICE_CANCEL   # closing the dialog keeps nothing

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self._headline = QLabel()
        font = self._headline.font()
        font.setBold(True)
        self._headline.setFont(font)
        layout.addWidget(self._headline)

        self._question = QLabel()
        self._question.setWordWrap(True)
        layout.addWidget(self._question)

        self._other_check = None
        if other_label:
            # Unchecked by default: the two files have independent lead-ins, so
            # a correction to one is not usually a correction to the other.
            self._other_check = QCheckBox()
            layout.addWidget(self._other_check)

        layout.addWidget(self._build_advanced(moved_label))

        self.button_box = QDialogButtonBox(self)
        self._move_button = None
        if show_start is not None:
            self._move_button = self.button_box.addButton(
                "Move show", QDialogButtonBox.ButtonRole.AcceptRole)
            self._move_button.clicked.connect(
                lambda: self._finish(CHOICE_MOVE_SHOW))
        self._keep_button = self.button_box.addButton(
            "Audio only" if show_start is not None else "Keep the new offset",
            QDialogButtonBox.ButtonRole.AcceptRole)
        self._keep_button.clicked.connect(
            lambda: self._finish(CHOICE_AUDIO_ONLY))
        cancel = self.button_box.addButton(
            "Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        cancel.clicked.connect(self.reject)
        (self._move_button or self._keep_button).setDefault(True)
        layout.addWidget(self.button_box)

        self._refresh()

    # --- construction -----------------------------------------------------

    def _build_advanced(self, moved_label: str) -> QWidget:
        """The offset the drag landed on, editable. Collapsed by default: a
        drag that needs three decimal places is the exception, not the rule.

        A bare checkbox rather than a checkable QGroupBox — a group box cannot
        shrink past its own frame, so collapsed it leaves an empty panel."""
        holder = QWidget()
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        self.advanced = QCheckBox("Advanced")
        box.addWidget(self.advanced)

        self._advanced_body = QWidget()
        body = QVBoxLayout(self._advanced_body)
        body.setContentsMargins(18, 0, 0, 0)   # indented under the disclosure

        row = QHBoxLayout()
        row.addWidget(QLabel(f"Offset of the {moved_label}:"))
        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setDecimals(3)
        self.offset_spin.setRange(-OFFSET_LIMIT, OFFSET_LIMIT)
        self.offset_spin.setSingleStep(0.05)
        self.offset_spin.setSuffix(" s")
        self.offset_spin.setValue(self._dragged_offset)
        self.offset_spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(self.offset_spin)
        row.addStretch(1)
        body.addLayout(row)

        self._was_label = QLabel(
            f"The drag landed on {self._dragged_offset:+.3f} s.")
        self._was_label.setWordWrap(True)
        body.addWidget(self._was_label)

        box.addWidget(self._advanced_body)
        self._advanced_body.setVisible(False)

        self.offset_spin.valueChanged.connect(self._refresh)
        self.advanced.toggled.connect(self._on_advanced_toggled)
        return holder

    def _on_advanced_toggled(self, on: bool) -> None:
        """Collapsing throws the override away — the dialog must never keep a
        value the user can no longer see."""
        self._advanced_body.setVisible(on)
        if not on:
            self.offset_spin.blockSignals(True)
            self.offset_spin.setValue(self._dragged_offset)
            self.offset_spin.blockSignals(False)
        self._refresh()
        self.adjustSize()

    # --- live state -------------------------------------------------------

    def offset(self) -> float:
        """The offset the dragged track should end up at."""
        if self.advanced.isChecked():
            return self.offset_spin.value()
        return self._dragged_offset

    def delta(self) -> float:
        """How far everything that follows would travel."""
        return self.offset() - self._old_offset

    def can_move_show(self) -> bool:
        """Whether the show can follow this delta. Refused rather than clamped
        when it would cross t=0: a partial shift leaves the show looking
        aligned while sitting the remainder out from the music."""
        return (self._show_start is not None
                and self._show_start + self.delta() >= 0.0)

    def choice(self) -> str:
        return self._choice

    def move_other(self) -> bool:
        return bool(self._other_check is not None
                    and self._other_check.isChecked())

    def _refresh(self) -> None:
        """Re-read every figure off the current offset. All absolute, all
        `from -> to`, all in the units `show.json` stores."""
        delta = self.delta()
        self._headline.setText(
            f"{self._moved_label.capitalize()} offset: "
            f"{self._old_offset:+.3f} s \u2192 {self.offset():+.3f} s")
        if self._other_check is not None:
            self._other_check.setText(
                f"Also move the {self._other_label}: "
                f"{self._other_offset:+.3f} s \u2192 "
                f"{self._other_offset + delta:+.3f} s")
        if self._show_start is None:
            self._question.setVisible(False)
        elif self.can_move_show():
            self._question.setText(
                "Move the show's clips, song parts and markers with it, so "
                "they stay where they sit in the music?")
            self._question.setVisible(True)
        else:
            self._question.setText(
                f"The show cannot follow: it starts at {self._show_start:+.3f} s "
                f"and this offset would start it at "
                f"{self._show_start + delta:+.3f} s, before the beginning of "
                "the timeline.")
            self._question.setVisible(True)
        if self._move_button is not None:
            self._move_button.setEnabled(self.can_move_show())

    def _finish(self, choice: str) -> None:
        self._choice = choice
        self.accept()
