import os
import sys
from typing import Optional

from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QPushButton, QLabel, QSpinBox,
                             QComboBox, QSlider, QButtonGroup, QFrame, QSizePolicy)
from PyQt6.QtGui import QIcon
from PyQt6.QtCore import QObject, Qt, QSize, pyqtSignal

from tupperlogger import get_logger


def _assets_base() -> str:
    """Base directory holding the bundled ``assets/`` tree (frozen-aware)."""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TimelineToolbar(QWidget):
    """Toolbar strip sitting directly above the timeline tracks.

    Three zones: left edit controls (placeholders for now), the center
    transport (moved out of the top toolbar — fully wired), and right view
    controls (Rows preset placeholder + a wired zoom slider and Fit button).
    """

    # Transport signals (moved verbatim from ToolbarManager)
    play_pause_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    bpm_changed = pyqtSignal(int)  # new_bpm
    metronome_mute_toggled = pyqtSignal(bool)  # mute_state
    # View signals (wired to the timeline)
    zoom_changed = pyqtSignal(float)  # horizontal scale factor
    fit_requested = pyqtSignal()

    # Zoom slider maps integer percent -> scale factor (slider/100.0).
    ZOOM_MIN = 20
    ZOOM_MAX = 400
    ZOOM_DEFAULT = 100

    def __init__(self, play_icon: QIcon, pause_icon: QIcon, stop_icon: QIcon, parent=None):
        super().__init__(parent)
        self.setObjectName("TimelineToolbar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.logger = get_logger("ui.timeline_toolbar")

        self.play_icon = play_icon
        self.pause_icon = pause_icon
        self.stop_icon = stop_icon
        self.is_playing = False

        # Widget handles needed after construction
        self.play_pause_button: Optional[QPushButton] = None
        self.time_display: Optional[QLabel] = None
        self.metronome_button: Optional[QPushButton] = None
        self.bpm_spinbox: Optional[QSpinBox] = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(8)

        layout.addWidget(self._build_left_zone())
        layout.addStretch(1)
        layout.addWidget(self._build_center_zone())
        layout.addStretch(1)
        layout.addWidget(self._build_right_zone())

        self.logger.info("Timeline toolbar created")

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _separator(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setObjectName("TTSeparator")
        return line

    def _build_left_zone(self) -> QWidget:
        """Edit controls — all placeholders (no logic) for now."""
        zone = QWidget()
        row = QHBoxLayout(zone)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        add_track = QPushButton("+ Add track")
        add_track.setObjectName("TTAddTrack")
        row.addWidget(add_track)

        group_btn = QPushButton("🖿 Group")
        group_btn.setObjectName("TTToolButton")
        row.addWidget(group_btn)

        delete_btn = QPushButton("🗑")
        delete_btn.setObjectName("TTToolButton")
        row.addWidget(delete_btn)

        row.addWidget(self._separator())

        snap_btn = QPushButton("🧲 Snap")
        snap_btn.setObjectName("TTSnap")
        snap_btn.setCheckable(True)
        snap_btn.setChecked(True)
        row.addWidget(snap_btn)

        beat_combo = QComboBox()
        beat_combo.setObjectName("TTBeatCombo")
        beat_combo.addItems(["1/4 beat", "1/8 beat", "1/16 beat", "1/32 beat"])
        beat_combo.setCurrentText("1/8 beat")
        row.addWidget(beat_combo)

        # Bars | Time segmented toggle
        bars_time = QButtonGroup(self)
        bars_time.setExclusive(True)
        bars_btn = QPushButton("Bars")
        bars_btn.setObjectName("TTSegment")
        bars_btn.setCheckable(True)
        bars_btn.setChecked(True)
        time_btn = QPushButton("Time")
        time_btn.setObjectName("TTSegment")
        time_btn.setCheckable(True)
        bars_time.addButton(bars_btn)
        bars_time.addButton(time_btn)
        seg = QHBoxLayout()
        seg.setContentsMargins(0, 0, 0, 0)
        seg.setSpacing(0)
        seg.addWidget(bars_btn)
        seg.addWidget(time_btn)
        seg_wrap = QWidget()
        seg_wrap.setLayout(seg)
        row.addWidget(seg_wrap)

        return zone

    def _build_center_zone(self) -> QWidget:
        """The transport, moved here from the top toolbar (object names kept
        so existing QSS applies unchanged)."""
        zone = QWidget()
        row = QHBoxLayout(zone)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.setAlignment(Qt.AlignmentFlag.AlignCenter)

        stop_btn = QPushButton()
        stop_btn.setIcon(self.stop_icon)
        stop_btn.setFixedSize(35, 35)
        stop_btn.setObjectName("StopButton")
        stop_btn.clicked.connect(self.stop_requested.emit)
        row.addWidget(stop_btn)

        self.play_pause_button = QPushButton()
        self.play_pause_button.setIcon(self.play_icon)
        self.play_pause_button.setFixedSize(35, 35)
        self.play_pause_button.setObjectName("PlayPauseButton")
        self.play_pause_button.clicked.connect(self.play_pause_requested.emit)
        row.addWidget(self.play_pause_button)

        self.time_display = QLabel("00:00.000")
        self.time_display.setFixedSize(120, 35)
        self.time_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_display.setObjectName("TimeDisplay")
        row.addWidget(self.time_display)

        # BPM cluster: metronome mute toggle + spinbox
        self.metronome_button = QPushButton("♩")
        metronome_icon = QIcon(os.path.join(_assets_base(), "assets", "icons", "metronome_icon.png"))
        self.metronome_button.setIcon(metronome_icon)
        self.metronome_button.setIconSize(QSize(28, 28))
        self.metronome_button.setFixedSize(32, 32)
        self.metronome_button.setCheckable(True)
        self.metronome_button.setObjectName("MetronomeButton")
        self.metronome_button.toggled.connect(self.metronome_mute_toggled.emit)
        row.addWidget(self.metronome_button)

        self.bpm_spinbox = QSpinBox()
        self.bpm_spinbox.setRange(40, 300)
        self.bpm_spinbox.setValue(120)
        self.bpm_spinbox.setFixedSize(50, 30)
        self.bpm_spinbox.valueChanged.connect(self.bpm_changed.emit)
        row.addWidget(self.bpm_spinbox)

        return zone

    def _build_right_zone(self) -> QWidget:
        """View controls — Rows preset (placeholder) + wired zoom & Fit."""
        zone = QWidget()
        row = QHBoxLayout(zone)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        rows_label = QLabel("Rows")
        rows_label.setObjectName("TTRowsLabel")
        row.addWidget(rows_label)

        # S / M / L segmented preset (placeholder)
        rows_group = QButtonGroup(self)
        rows_group.setExclusive(True)
        seg = QHBoxLayout()
        seg.setContentsMargins(0, 0, 0, 0)
        seg.setSpacing(0)
        for label in ("S", "M", "L"):
            btn = QPushButton(label)
            btn.setObjectName("TTSegment")
            btn.setCheckable(True)
            btn.setFixedWidth(28)
            if label == "M":
                btn.setChecked(True)
            rows_group.addButton(btn)
            seg.addWidget(btn)
        seg_wrap = QWidget()
        seg_wrap.setLayout(seg)
        row.addWidget(seg_wrap)

        row.addWidget(self._separator())

        # Zoom slider (wired -> timeline horizontal scale)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setObjectName("TTZoom")
        self.zoom_slider.setRange(self.ZOOM_MIN, self.ZOOM_MAX)
        self.zoom_slider.setValue(self.ZOOM_DEFAULT)
        self.zoom_slider.setFixedWidth(120)
        self.zoom_slider.setToolTip("Zoom")
        self.zoom_slider.valueChanged.connect(
            lambda v: self.zoom_changed.emit(v / 100.0))
        row.addWidget(self.zoom_slider)

        fit_btn = QPushButton("Fit")
        fit_btn.setObjectName("TTFit")
        fit_btn.clicked.connect(self.fit_requested.emit)
        row.addWidget(fit_btn)

        return zone

    # ------------------------------------------------------------------
    # Transport API (moved verbatim from ToolbarManager)
    # ------------------------------------------------------------------
    def update_time_display(self, time_str: str) -> None:
        """Update the time display."""
        if self.time_display:
            self.time_display.setText(time_str)

    def update_play_pause_icon(self, is_playing: bool) -> None:
        """Update the play/pause button icon based on current state."""
        self.is_playing = is_playing
        if self.play_pause_button:
            self.play_pause_button.setIcon(self.pause_icon if is_playing else self.play_icon)

    def set_bpm(self, bpm: int) -> None:
        """Set the BPM value in the spinbox."""
        if self.bpm_spinbox:
            self.bpm_spinbox.setValue(bpm)

    def get_bpm(self) -> int:
        """Get current BPM value from spinbox."""
        if self.bpm_spinbox:
            return self.bpm_spinbox.value()
        return 120

    def set_metronome_mute(self, is_muted: bool) -> None:
        """Reflect the metronome mute state on the toggle button."""
        if self.metronome_button:
            self.metronome_button.setChecked(is_muted)

    def set_zoom_value(self, factor: float) -> None:
        """Move the zoom slider to reflect a zoom change driven elsewhere
        (Fit / Ctrl+wheel). Signals are blocked so this doesn't loop back
        into the timeline's set_zoom."""
        value = max(self.ZOOM_MIN, min(self.ZOOM_MAX, round(factor * 100)))
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(value)
        self.zoom_slider.blockSignals(False)
