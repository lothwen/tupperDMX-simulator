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
numbers cannot be checked against anything on disk. The one exception is an
INPUT: Advanced takes the move in beats, since a beat is a length and has no
position, and it names the absolute offset those beats are counted from.
"""

import html
import math
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QGridLayout, QLabel,
                             QVBoxLayout, QWidget)

from ui.widgets import icons

#: What the drag decided to do with everything that is not the dragged track.
#: AUDIO_ONLY keeps the offset and moves no clips; CANCEL keeps nothing at all.
CHOICE_CANCEL = "cancel"
CHOICE_AUDIO_ONLY = "audio_only"
CHOICE_MOVE_SHOW = "move_show"

#: Matches Config's own validation of `offset` / `guide_offset`.
OFFSET_LIMIT = 3600.0

#: The theme's error red, for the warning glyphs — the QSS colours the text
#: and the button to match (`state="blocked"` in dark-theme.qss).
BLOCKED_RED = "#e05252"


class OffsetRippleDialog(QDialog):
    """Ask what follows the track a drag moved from `old_offset` to
    `new_offset`.

    `show_start` is the earliest thing the show has on this song's timeline, or
    None when the song is empty — the difference between "the show may follow"
    and "there is no show to move", which no typed offset can change.
    `show_start_item` names what sits there (a clip, a song part, a marker), so
    a show that cannot follow can say what is in the way.
    `other_label`/`other_offset` name the track the drag did not move and where
    it sits now; `other_label` is None when the song has only one.
    `bpm` is the tempo the Advanced beats field counts in.
    """

    def __init__(self, parent, *, moved_label: str,
                 other_label: Optional[str], other_offset: float = 0.0,
                 old_offset: float, new_offset: float,
                 show_start: Optional[float], show_start_item: Optional[str],
                 bpm: int):
        super().__init__(parent)
        self.setWindowTitle("Move show with audio")
        self._old_offset = float(old_offset)
        self._dragged_offset = float(new_offset)
        self._show_start = show_start
        self._show_start_item = show_start_item
        self._other_label = other_label
        self._other_offset = float(other_offset)
        self._moved_label = moved_label
        self._bpm = int(bpm)
        self._beat_seconds = 60.0 / self._bpm
        self._choice = CHOICE_CANCEL   # closing the dialog keeps nothing

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self._headline = QLabel()
        font = self._headline.font()
        font.setBold(True)
        self._headline.setFont(font)
        layout.addWidget(self._headline)

        self._question = QLabel()
        self._question.setObjectName("OffsetQuestion")
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
            self._move_button.setObjectName("OffsetMoveShow")
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

        grid = QGridLayout()
        grid.addWidget(QLabel(f"Offset of the {moved_label}:"), 0, 0)
        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setDecimals(3)
        self.offset_spin.setRange(-OFFSET_LIMIT, OFFSET_LIMIT)
        self.offset_spin.setSingleStep(0.05)
        self.offset_spin.setSuffix(" s")
        self.offset_spin.setValue(self._dragged_offset)
        self.offset_spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self.offset_spin, 0, 1)

        # The same edit counted in beats from where the drag started. The two
        # fields write each other, and the offset stays the value in force —
        # so what gets stored is always the figure on screen.
        grid.addWidget(QLabel("Or move it by:"), 1, 0)
        self.beats_spin = QDoubleSpinBox()
        self.beats_spin.setDecimals(2)
        beats_limit = 2 * OFFSET_LIMIT / self._beat_seconds
        self.beats_spin.setRange(-beats_limit, beats_limit)
        self.beats_spin.setSingleStep(1.0)
        self.beats_spin.setSuffix(" beats")
        self.beats_spin.setValue(self._beats_to(self._dragged_offset))
        self.beats_spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self.beats_spin, 1, 1)
        grid.addWidget(QLabel(
            f"from {self._old_offset:+.3f} s, at {self._bpm} BPM"), 1, 2)
        grid.setColumnStretch(3, 1)
        body.addLayout(grid)

        self._was_label = QLabel(
            f"The drag landed on {self._dragged_offset:+.3f} s.")
        self._was_label.setWordWrap(True)
        body.addWidget(self._was_label)

        box.addWidget(self._advanced_body)
        self._advanced_body.setVisible(False)

        self.offset_spin.valueChanged.connect(self._on_offset_edited)
        self.beats_spin.valueChanged.connect(self._on_beats_edited)
        self.advanced.toggled.connect(self._on_advanced_toggled)
        return holder

    def _on_advanced_toggled(self, on: bool) -> None:
        """Collapsing throws the override away — the dialog must never keep a
        value the user can no longer see."""
        self._advanced_body.setVisible(on)
        if not on:
            self._set_quietly(self.offset_spin, self._dragged_offset)
            self._set_quietly(self.beats_spin,
                              self._beats_to(self._dragged_offset))
        self._refresh()
        self.adjustSize()

    def _on_offset_edited(self, offset: float) -> None:
        self._set_quietly(self.beats_spin, self._beats_to(offset))
        self._refresh()

    def _on_beats_edited(self, beats: float) -> None:
        self._set_quietly(self.offset_spin,
                          self._old_offset + beats * self._beat_seconds)
        self._refresh()

    def _beats_to(self, offset: float) -> float:
        """How many beats `offset` lies from where the drag started."""
        return (offset - self._old_offset) / self._beat_seconds

    @staticmethod
    def _set_quietly(spin: QDoubleSpinBox, value: float) -> None:
        """Set a field without it reporting back as an edit."""
        spin.blockSignals(True)
        spin.setValue(value)
        spin.blockSignals(False)

    @staticmethod
    def _set_state(widget: QWidget, state: str) -> None:
        """Restyle `widget` through the theme's `state` rules. Re-polished only
        on a change, since every keystroke in Advanced refreshes."""
        if widget.property("state") == state:
            return
        widget.setProperty("state", state)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

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

    def lowest_offset(self) -> Optional[float]:
        """The lowest offset the show can still follow to, rounded UP to the
        millisecond the offset field holds — rounded down, typing the quoted
        figure back would land the show a fraction before t=0 and be refused.
        None when there is no show."""
        if self._show_start is None:
            return None
        lowest = self._old_offset - self._show_start
        return math.ceil(round(lowest * 1000.0, 6)) / 1000.0

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
            f"{self._old_offset:+.3f} s → {self.offset():+.3f} s")
        if self._other_check is not None:
            self._other_check.setText(
                f"Also move the {self._other_label}: "
                f"{self._other_offset:+.3f} s → "
                f"{self._other_offset + delta:+.3f} s")
        blocked = self._show_start is not None and not self.can_move_show()
        if self._show_start is None:
            self._question.setVisible(False)
        elif not blocked:
            self._question.setText(
                "Move the show's clips, song parts and markers with it, so "
                "they stay where they sit in the music?")
            self._question.setVisible(True)
        else:
            # Red, and marked: colour alone is not a message.
            item = html.escape(self._show_start_item, quote=False)
            self._question.setText(
                f"{icons.img_tag('warning', BLOCKED_RED, 12)} "
                f"The show cannot follow. Its first item, the {item}, starts "
                f"at {self._show_start:+.3f} s and this offset would move it "
                f"to {self._show_start + delta:+.3f} s, before the beginning "
                f"of the timeline.<br><br>"
                f"The show can follow the {self._moved_label} down to an "
                f"offset of {self.lowest_offset():+.3f} s.")
            self._question.setVisible(True)
        self._set_state(self._question, "blocked" if blocked else "")
        if self._move_button is not None:
            # A disabled button alone reads as enabled under the theme, so a
            # blocked one also turns red, carries the warning glyph and — since
            # Qt still shows tooltips on disabled widgets — says why.
            self._move_button.setEnabled(not blocked)
            self._move_button.setIcon(
                icons.icon("warning", BLOCKED_RED) if blocked else QIcon())
            self._move_button.setToolTip(
                f"Blocked: the show would start before the timeline. The "
                f"lowest offset it can follow is {self.lowest_offset():+.3f} s."
                if blocked else "")
            self._set_state(self._move_button, "blocked" if blocked else "")

    def _finish(self, choice: str) -> None:
        self._choice = choice
        self.accept()
