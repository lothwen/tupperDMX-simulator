# ABOUTME: ClickPopup — the timeline toolbar's click panel, opening upward from
# ABOUTME: its ♩ button: tempo + TAP, meter, accent, count-in, level, free-run /
# ABOUTME: set song.
"""The click panel.

Tempo and meter here are a DRAFT: the panel opens on the song's own values,
and nothing reaches the song until "Set as song tempo". Free-run plays the
draft on its own, off the transport, and follows it live while it runs.
Accent and level are not part of the draft — they are the click's standing
settings, applied as they move, and the level IS the timeline header's CLICK
slider (the window keeps the two in step). Count-in is neither: it is the
current song's own flag, applied as it flips.

A widget only: every action leaves as a signal, and the window hands them to
AppCore.
"""

import time
from typing import Optional

from PyQt6.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QAbstractSpinBox, QButtonGroup, QFrame,
                             QGridLayout, QHBoxLayout, QLabel, QPushButton,
                             QSlider, QSpinBox, QVBoxLayout, QWidget)

from core.tempo_map import DEFAULT_METER, MAX_BPM, METERS, MIN_BPM, clamp_bpm
from timeline.master_header import format_db
from ui.widgets.toggle_switch import ToggleSwitch


class TapTempo:
    """A BPM from the rhythm of repeated taps: the mean interval of the last
    few, started over by a pause too long to be one beat of the slowest tempo
    the app plays (MIN_BPM is a beat every 1.5 s)."""

    RESET_S = 2.0
    WINDOW = 4   # intervals averaged

    def __init__(self):
        self._taps = []

    def tap(self, now: float) -> Optional[int]:
        """Record a tap at monotonic `now`; the BPM so far, from the second on."""
        if self._taps and now - self._taps[-1] > self.RESET_S:
            self._taps = []
        self._taps = (self._taps + [now])[-(self.WINDOW + 1):]
        if len(self._taps) < 2:
            return None
        interval = (self._taps[-1] - self._taps[0]) / (len(self._taps) - 1)
        return clamp_bpm(60.0 / interval)


class ClickPopup(QFrame):
    """The panel itself — a Qt popup, so any click outside closes it."""

    free_run_requested = pyqtSignal(int, str)   # bpm, meter: start or retime
    free_run_stop_requested = pyqtSignal()
    apply_requested = pyqtSignal(int, str)      # bpm, meter: "Set as song tempo"
    accent_toggled = pyqtSignal(bool)
    precount_toggled = pyqtSignal(bool)         # the current song's count-in
    level_changed = pyqtSignal(int)             # 0-100, like the header's slider
    open_changed = pyqtSignal(bool)             # shown / hidden

    # A free-run retime restarts the bar, so it waits for the draft to settle:
    # scrolling through ten tempos should not fire ten downbeats.
    RETIME_DELAY_MS = 300

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("ClickPopup")

        self._song_bpm = 120
        self._song_meter = DEFAULT_METER
        self._has_song = False
        self._free_running = False
        self._playing = False
        self._tap = TapTempo()
        self._anchor = None

        self._retime = QTimer(self)
        self._retime.setSingleShot(True)
        self._retime.setInterval(self.RETIME_DELAY_MS)
        self._retime.timeout.connect(self._request_free_run)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(10)

        title = QLabel("CLICK")
        title.setObjectName("ClickPopupTitle")
        layout.addWidget(title)
        layout.addLayout(self._build_tempo_row())

        rows = QGridLayout()
        rows.setHorizontalSpacing(10)
        rows.setVerticalSpacing(10)
        meter_label = self._row_label("METER")
        meter_label.setProperty("top", True)   # beside the chips' first row
        rows.addWidget(meter_label, 0, 0,
                       Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        rows.addWidget(self._build_meter_chips(), 0, 1)
        rows.addWidget(self._row_label("ACCENT"), 1, 0)
        rows.addLayout(self._build_accent_row(), 1, 1)
        rows.addWidget(self._row_label("COUNT-IN"), 2, 0)
        rows.addLayout(self._build_precount_row(), 2, 1)
        rows.addWidget(self._row_label("LEVEL"), 3, 0)
        rows.addLayout(self._build_level_row(), 3, 1)
        layout.addLayout(rows)

        layout.addLayout(self._build_action_row())
        self._refresh_actions()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @staticmethod
    def _row_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("ClickRowLabel")
        return label

    def _build_tempo_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)

        minus = QPushButton("−")
        minus.setObjectName("ClickStepButton")
        minus.setToolTip("One BPM slower")
        minus.setAutoRepeat(True)
        row.addWidget(minus)

        box = QFrame()
        box.setObjectName("ClickTempoBox")
        box_row = QHBoxLayout(box)
        box_row.setContentsMargins(6, 0, 10, 0)
        box_row.setSpacing(4)
        box_row.addStretch(1)
        self.bpm_spin = QSpinBox()
        self.bpm_spin.setObjectName("ClickBpm")
        self.bpm_spin.setRange(MIN_BPM, MAX_BPM)
        self.bpm_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.bpm_spin.setAlignment(Qt.AlignmentFlag.AlignRight
                                   | Qt.AlignmentFlag.AlignVCenter)
        self.bpm_spin.valueChanged.connect(self._on_draft_changed)
        box_row.addWidget(self.bpm_spin)
        unit = QLabel("BPM")
        unit.setObjectName("ClickBpmUnit")
        box_row.addWidget(unit, 0, Qt.AlignmentFlag.AlignBottom)
        box_row.addStretch(1)
        row.addWidget(box, 1)

        plus = QPushButton("+")
        plus.setObjectName("ClickStepButton")
        plus.setToolTip("One BPM faster")
        plus.setAutoRepeat(True)
        row.addWidget(plus)

        tap = QPushButton("TAP")
        tap.setObjectName("ClickTapButton")
        tap.setToolTip("Tap the beat to set the tempo")
        row.addWidget(tap)

        minus.clicked.connect(lambda: self.bpm_spin.stepBy(-1))
        plus.clicked.connect(lambda: self.bpm_spin.stepBy(1))
        tap.pressed.connect(self._on_tap)   # on press: a tap is a moment
        return row

    def _build_meter_chips(self) -> QWidget:
        chips = QWidget()
        grid = QGridLayout(chips)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        self._meter_group = QButtonGroup(self)
        self._meter_group.setExclusive(True)
        self._meter_buttons = {}
        for index, meter in enumerate(METERS):
            chip = QPushButton(meter)
            chip.setObjectName("ClickMeterChip")
            chip.setCheckable(True)
            self._meter_group.addButton(chip)
            self._meter_buttons[meter] = chip
            grid.addWidget(chip, index // 3, index % 3)
        self._meter_buttons[DEFAULT_METER].setChecked(True)
        self._meter_group.buttonToggled.connect(
            lambda _button, checked: checked and self._on_draft_changed())
        return chips

    def _build_accent_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.accent_switch = ToggleSwitch()
        self.accent_switch.setChecked(True)
        self.accent_switch.setToolTip(
            "Give beat 1 its own, stronger sound (off: every beat alike)")
        self.accent_switch.toggled.connect(self.accent_toggled.emit)
        row.addWidget(self.accent_switch)
        hint = QLabel("Beat 1")
        hint.setObjectName("ClickAccentHint")
        row.addWidget(hint)
        row.addStretch(1)
        return row

    def _build_precount_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.precount_switch = ToggleSwitch()
        self.precount_switch.setToolTip(
            "Play a count-in before this song (off = segue from the previous one)")
        self.precount_switch.toggled.connect(self.precount_toggled.emit)
        row.addWidget(self.precount_switch)
        hint = QLabel("This song")
        hint.setObjectName("ClickAccentHint")
        row.addWidget(hint)
        row.addStretch(1)
        return row

    def _build_level_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.level_slider = QSlider(Qt.Orientation.Horizontal)
        self.level_slider.setObjectName("ClickLevelSlider")
        self.level_slider.setRange(0, 100)
        self.level_slider.setToolTip("Click volume — the timeline's CLICK slider")
        self._level_db = QLabel(format_db(self.level_slider.value()))
        self._level_db.setObjectName("ClickLevelDb")
        self._level_db.setAlignment(Qt.AlignmentFlag.AlignRight
                                    | Qt.AlignmentFlag.AlignVCenter)
        self.level_slider.valueChanged.connect(
            lambda value: self._level_db.setText(format_db(value)))
        self.level_slider.valueChanged.connect(self.level_changed.emit)
        row.addWidget(self.level_slider, 1)
        row.addWidget(self._level_db)
        return row

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.free_run_button = QPushButton()
        self.free_run_button.setObjectName("ClickFreeRunButton")
        self.free_run_button.clicked.connect(self._on_free_run_clicked)
        row.addWidget(self.free_run_button, 1)
        self.apply_button = QPushButton("Set as song tempo")
        self.apply_button.setObjectName("ClickApplyButton")
        self.apply_button.setToolTip(
            "Make this tempo and meter the current song's (the ruler follows)")
        self.apply_button.clicked.connect(
            lambda: self.apply_requested.emit(*self.draft()))
        row.addWidget(self.apply_button, 1)
        return row

    # ------------------------------------------------------------------
    # The draft
    # ------------------------------------------------------------------
    def draft(self):
        """(bpm, meter) as the panel shows them."""
        checked = self._meter_group.checkedButton()
        return self.bpm_spin.value(), checked.text() if checked else DEFAULT_METER

    def _set_draft(self, bpm: int, meter: str) -> None:
        blocked = self.bpm_spin.blockSignals(True)
        self.bpm_spin.setValue(bpm)
        self.bpm_spin.blockSignals(blocked)
        chip = self._meter_buttons.get(meter, self._meter_buttons[DEFAULT_METER])
        blocked = self._meter_group.blockSignals(True)
        chip.setChecked(True)
        self._meter_group.blockSignals(blocked)
        self._refresh_actions()

    def _on_draft_changed(self, *_):
        self._refresh_actions()
        if self._free_running:
            self._retime.start()

    def _on_tap(self) -> None:
        bpm = self._tap.tap(time.monotonic())
        if bpm is not None:
            self.bpm_spin.setValue(bpm)

    def _request_free_run(self) -> None:
        self.free_run_requested.emit(*self.draft())

    def _on_free_run_clicked(self) -> None:
        self._retime.stop()
        if self._free_running:
            self.free_run_stop_requested.emit()
        else:
            self._request_free_run()

    def _refresh_actions(self) -> None:
        self.free_run_button.setText("Stop" if self._free_running else "Free-run")
        self.free_run_button.setProperty("running", self._free_running)
        self.free_run_button.style().unpolish(self.free_run_button)
        self.free_run_button.style().polish(self.free_run_button)
        # The song's click owns the output while it plays.
        self.free_run_button.setEnabled(self._free_running or not self._playing)
        self.free_run_button.setToolTip(
            "Stop the click" if self._free_running
            else "The song is playing — its click is running" if self._playing
            else "Play the click on its own at this tempo and meter")
        self.apply_button.setEnabled(
            self._has_song
            and self.draft() != (self._song_bpm, self._song_meter))

    # ------------------------------------------------------------------
    # State from outside (signals blocked: these are not edits)
    # ------------------------------------------------------------------
    def set_song_tempo(self, bpm: int, meter: str, has_song: bool) -> None:
        """The current song's own tempo and meter — what the panel opens on,
        and what "Set as song tempo" would change."""
        self._song_bpm, self._song_meter = int(bpm), meter
        self._has_song = bool(has_song)
        self._refresh_actions()

    def set_free_running(self, running: bool) -> None:
        self._free_running = bool(running)
        if not running:
            self._retime.stop()
        self._refresh_actions()

    def set_transport_playing(self, playing: bool) -> None:
        self._playing = bool(playing)
        self._refresh_actions()

    def set_accent(self, accent: bool) -> None:
        blocked = self.accent_switch.blockSignals(True)
        self.accent_switch.setChecked(bool(accent))
        self.accent_switch.blockSignals(blocked)

    def set_precount(self, enabled: bool,
                     has_song: Optional[bool] = None) -> None:
        """The current song's count-in flag. Pass ``has_song`` to also
        enable/disable the switch (no song = greyed out)."""
        blocked = self.precount_switch.blockSignals(True)
        self.precount_switch.setChecked(bool(enabled))
        if has_song is not None:
            self.precount_switch.setEnabled(bool(has_song))
        self.precount_switch.blockSignals(blocked)

    def set_level(self, percent: int) -> None:
        blocked = self.level_slider.blockSignals(True)
        self.level_slider.setValue(int(percent))
        self.level_slider.blockSignals(blocked)
        self._level_db.setText(format_db(self.level_slider.value()))

    # ------------------------------------------------------------------
    # Showing
    # ------------------------------------------------------------------
    def open_above(self, anchor: QWidget) -> None:
        """Open over `anchor`, left edges aligned — below it only when the
        screen has no room above. A free-run keeps its draft; otherwise the
        panel opens on the song's own tempo and meter."""
        if not self._free_running:
            self._set_draft(self._song_bpm, self._song_meter)
        self._anchor = anchor
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, False)
        self.adjustSize()
        top_left = anchor.mapToGlobal(QPoint(0, 0))
        x = top_left.x()
        y = top_left.y() - self.height() - 4
        screen = anchor.screen()
        if screen is not None:
            area = screen.availableGeometry()
            if y < area.top():
                y = top_left.y() + anchor.height() + 4
            x = max(area.left(), min(x, area.right() - self.width()))
        self.move(x, y)
        self.show()

    def showEvent(self, event):
        super().showEvent(event)
        self.open_changed.emit(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.open_changed.emit(False)

    def mousePressEvent(self, event):
        """A press on the button that opened the panel just closes it. Qt
        replays a popup-closing press to whatever lies under it, which on the
        button would open the panel straight back up."""
        anchor = self._anchor
        if anchor is not None and anchor.rect().contains(
                anchor.mapFromGlobal(event.globalPosition().toPoint())):
            self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)
        super().mousePressEvent(event)
