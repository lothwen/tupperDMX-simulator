from typing import Optional

from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QPushButton, QLabel, QSpinBox,
                             QComboBox, QSlider, QButtonGroup, QFrame, QSizePolicy)
from PyQt6.QtGui import QIcon
from PyQt6.QtCore import QObject, Qt, pyqtSignal

from tupperlogger import get_logger


class TimelineToolbar(QWidget):
    """Toolbar strip sitting directly above the timeline tracks.

    Three zones: left edit controls (placeholders for now), the center
    transport (moved out of the top toolbar — fully wired), and right view
    controls (Rows preset — S collapses all lanes, M/L set the row height —
    plus a wired zoom slider and Fit button).
    """

    # Transport signals (moved verbatim from ToolbarManager)
    play_pause_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    bpm_changed = pyqtSignal(int)  # new_bpm
    # View signals (wired to the timeline)
    zoom_changed = pyqtSignal(float)  # horizontal scale factor
    fit_requested = pyqtSignal()
    row_height_changed = pyqtSignal(int)  # fixture-track height in px (M/L, expands)
    collapse_all_requested = pyqtSignal()  # Rows "S": collapse every fixture lane
    ruler_mode_changed = pyqtSignal(str)  # "bars" | "time"
    beat_division_changed = pyqtSignal(int)  # ruler grid lines per bar
    snap_toggled = pyqtSignal(bool)  # snap-to-grid enabled

    # Zoom slider maps integer percent -> scale factor (slider/100.0).
    ZOOM_MIN = 20
    ZOOM_MAX = 400
    ZOOM_DEFAULT = 100

    # Rows segmented preset -> fixture-track height in px (M == timeline default).
    ROW_HEIGHT_PRESETS = {"S": 30, "M": 50, "L": 80}

    # Beat-division combo label -> ruler grid lines per bar ("1/4 beat" == default).
    BEAT_DIVISIONS = {"1 beat": 1, "1/4 beat": 4, "1/8 beat": 8}
    BEAT_DIVISION_DEFAULT = "1/4 beat"

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

        # TODO: Future implementation
        #add_track = QPushButton("+ Add track")
        #add_track.setObjectName("TTAddTrack")
        #row.addWidget(add_track)

        #group_btn = QPushButton("🖿 Group")
        #group_btn.setObjectName("TTToolButton")
        #row.addWidget(group_btn)

        #delete_btn = QPushButton("🗑")
        #delete_btn.setObjectName("TTToolButton")
        #row.addWidget(delete_btn)

        row.addWidget(self._separator())

        snap_btn = QPushButton("🧲 Snap")
        snap_btn.setObjectName("TTSnap")
        snap_btn.setCheckable(True)
        snap_btn.setChecked(True)
        snap_btn.toggled.connect(self.snap_toggled.emit)
        row.addWidget(snap_btn)

        self.beat_combo = QComboBox()
        self.beat_combo.setObjectName("TTBeatCombo")
        self.beat_combo.addItems(list(self.BEAT_DIVISIONS))
        self.beat_combo.setCurrentText(self.BEAT_DIVISION_DEFAULT)
        self.beat_combo.currentTextChanged.connect(
            lambda text: self.beat_division_changed.emit(self.BEAT_DIVISIONS[text]))
        row.addWidget(self.beat_combo)

        # Bars | Time segmented toggle
        bars_time = QButtonGroup(self)
        bars_time.setExclusive(True)
        bars_btn = QPushButton("Bars")
        bars_btn.setObjectName("TTSegment")
        bars_btn.setProperty("seg", "left")
        bars_btn.setCheckable(True)
        bars_btn.setChecked(True)
        time_btn = QPushButton("Time")
        time_btn.setObjectName("TTSegment")
        time_btn.setProperty("seg", "right")
        time_btn.setCheckable(True)
        bars_btn.clicked.connect(lambda: self.ruler_mode_changed.emit("bars"))
        time_btn.clicked.connect(lambda: self.ruler_mode_changed.emit("time"))
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

        # BPM cluster: decorative tempo glyph + spinbox. (Metronome mute now
        # lives solely on the timeline header's CLICK row, so this is a
        # non-interactive label.)
        tempo_glyph = QLabel("♩")
        tempo_glyph.setObjectName("MetronomeButton")
        tempo_glyph.setFixedSize(32, 32)
        tempo_glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(tempo_glyph)

        self.bpm_spinbox = QSpinBox()
        self.bpm_spinbox.setRange(40, 300)
        self.bpm_spinbox.setValue(120)
        self.bpm_spinbox.setFixedSize(50, 30)
        self.bpm_spinbox.valueChanged.connect(self.bpm_changed.emit)
        row.addWidget(self.bpm_spinbox)

        return zone

    def _build_right_zone(self) -> QWidget:
        """View controls — Rows preset (S collapses all, M/L set height) + wired
        zoom & Fit."""
        zone = QWidget()
        row = QHBoxLayout(zone)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        rows_label = QLabel("Rows")
        rows_label.setObjectName("TTRowsLabel")
        row.addWidget(rows_label)

        # S / M / L segmented preset for fixture-track height
        self.rows_group = QButtonGroup(self)
        self.rows_group.setExclusive(True)
        self._row_buttons = {}
        seg = QHBoxLayout()
        seg.setContentsMargins(0, 0, 0, 0)
        seg.setSpacing(0)
        labels = ("S", "M", "L")
        for i, label in enumerate(labels):
            btn = QPushButton(label)
            btn.setObjectName("TTSegment")
            # Round only the outer corners of the segmented control.
            if i == 0:
                btn.setProperty("seg", "left")
            elif i == len(labels) - 1:
                btn.setProperty("seg", "right")
            btn.setCheckable(True)
            btn.setFixedWidth(28)
            if label == "M":
                btn.setChecked(True)
            if label == "S":
                # "S" collapses every lane to a single header row (the smallest
                # possible row) rather than picking a height preset.
                btn.setToolTip("Collapse all tracks")
                btn.clicked.connect(lambda _checked: self.collapse_all_requested.emit())
            else:
                height = self.ROW_HEIGHT_PRESETS[label]
                btn.clicked.connect(
                    lambda _checked, h=height: self.row_height_changed.emit(h))
            self.rows_group.addButton(btn)
            self._row_buttons[label] = btn
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

    def set_zoom_value(self, factor: float) -> None:
        """Move the zoom slider to reflect a zoom change driven elsewhere
        (Fit / Ctrl+wheel). Signals are blocked so this doesn't loop back
        into the timeline's set_zoom."""
        value = max(self.ZOOM_MIN, min(self.ZOOM_MAX, round(factor * 100)))
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(value)
        self.zoom_slider.blockSignals(False)

    def set_row_height(self, height: int) -> None:
        """Check the M/L segment whose preset is closest to `height` (used on
        startup to reflect the persisted track height). 'S' is excluded — it is
        the collapse toggle, reflected separately via set_rows_collapsed().
        setChecked fires `toggled`, not `clicked`, so this does not re-emit."""
        presets = {k: v for k, v in self.ROW_HEIGHT_PRESETS.items() if k != "S"}
        label = min(presets, key=lambda k: abs(presets[k] - height))
        self._row_buttons[label].setChecked(True)

    def set_rows_collapsed(self) -> None:
        """Check the 'S' (collapse) segment to reflect a restored collapsed state
        on startup. setChecked fires `toggled`, not `clicked`, so this does not
        re-emit collapse_all_requested."""
        self._row_buttons["S"].setChecked(True)
