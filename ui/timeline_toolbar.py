from typing import Optional

from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QPushButton, QLabel, QSpinBox,
                             QComboBox, QSlider, QFrame, QSizePolicy)
from PyQt6.QtGui import QIcon
from PyQt6.QtCore import QObject, Qt, pyqtSignal, QSize

from app.media_controller import MONITOR_BACKING, MONITOR_GUIDE
from tupperlogger import get_logger
from ui.click_popup import ClickPopup
from ui.widgets import icons
from ui.widgets.segmented import Segment, segmented


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
    markers_requested = pyqtSignal()  # open the per-song marker editor
    add_track_requested = pyqtSignal()  # enter stage edit mode + patch a fixture
    monitor_changed = pyqtSignal(str)  # editor monitor: MONITOR_BACKING | MONITOR_GUIDE

    # Zoom slider maps integer percent -> scale factor (slider/100.0).
    ZOOM_MIN = 20
    ZOOM_MAX = 400
    ZOOM_DEFAULT = 100

    # Rows segmented preset -> fixture-track height in px (M == timeline default).
    ROW_HEIGHT_PRESETS = {"S": 30, "M": 50, "L": 80}

    # Beat-division combo label -> ruler grid lines per bar ("1/4 beat" == default).
    BEAT_DIVISIONS = {"1 beat": 1, "1/4 beat": 4, "1/8 beat": 8}
    BEAT_DIVISION_DEFAULT = "1/4 beat"

    # Breathing room over the row's own minimum before the view controls come
    # back, so the full row is never restored into a width it barely fits.
    BREAKPOINT_SLACK = 24

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
        self.monitor_backing_btn: Optional[QPushButton] = None
        self.monitor_guide_btn: Optional[QPushButton] = None

        # Narrow-window state: the view controls fold into a popup below the
        # width the full row needs.
        self._compact = False
        self._full_min_width = 0
        self._compact_min_width = 0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(8)

        layout.addWidget(self._build_left_zone())
        layout.addStretch(1)
        layout.addWidget(self._build_center_zone())
        layout.addStretch(1)
        self._right_zone = self._build_right_zone()
        layout.addWidget(self._right_zone)

        self._layout = layout
        self._build_view_popup()
        self._measure_breakpoint()

        self.logger.info("Timeline toolbar created")

    # ------------------------------------------------------------------
    # Narrow windows: the view controls fold into a "View" popup
    # ------------------------------------------------------------------
    def _build_view_popup(self) -> None:
        """The stand-in for the right zone while the bar is folded. The zone
        widget itself moves in and out of this popup, so Rows / zoom / Fit are
        the same widgets holding the same state either way."""
        self._view_popup = QFrame(self, Qt.WindowType.Popup)
        self._view_popup.setObjectName("TTViewPopup")
        popup_layout = QHBoxLayout(self._view_popup)
        popup_layout.setContentsMargins(8, 6, 8, 6)

        self._view_button = QPushButton("View ▾")
        self._view_button.setObjectName("TTToolButton")
        self._view_button.setToolTip("Row height, zoom and Fit")
        self._view_button.clicked.connect(self._show_view_popup)
        self._view_button.hide()
        self._layout.addWidget(self._view_button)

    def _show_view_popup(self) -> None:
        below = self._view_button.mapToGlobal(
            self._view_button.rect().bottomRight())
        self._view_popup.adjustSize()
        self._view_popup.move(below.x() - self._view_popup.width(), below.y() + 2)
        self._view_popup.show()

    def _measure_breakpoint(self) -> None:
        """Record what the row needs with the view controls in place and with
        them folded, so the breakpoint follows the UI density instead of a
        hardcoded width.

        Derived from whichever state the row is in rather than measured by
        folding it both ways — see `ToolbarManager._measure_breakpoint` for why
        re-measuring a widget in the same call stack that moved it lies.
        """
        self.ensurePolished()
        self._layout.invalidate()
        self._layout.activate()

        # Folding swaps the view controls for the View button, so the two
        # widths differ by exactly what that trade costs.
        traded = (self._right_zone.minimumSizeHint().width()
                  - self._view_button.minimumSizeHint().width())
        current = self._layout.minimumSize().width()
        if self._compact:
            self._compact_min_width = current
            self._full_min_width = current + traded
        else:
            self._full_min_width = current
            self._compact_min_width = current - traded
        self.updateGeometry()

    def remeasure_breakpoint(self) -> None:
        """Re-measure after a UI density change and re-decide from the width
        the row actually has (a resize event may never come)."""
        self._measure_breakpoint()
        self._apply_breakpoint(self.width())

    def minimumSizeHint(self) -> QSize:
        """The folded width, always — see `_ResponsiveBar` in
        `ui/toolbar_manager.py` for why a bar must under-report to be able to
        fold at all."""
        hint = super().minimumSizeHint()
        if self._compact_min_width:
            return QSize(self._compact_min_width, hint.height())
        return hint

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_breakpoint(event.size().width())

    def _apply_breakpoint(self, width: int) -> None:
        """Fold the view controls away when the full row would not fit.

        Hysteresis is implicit: it folds only when the expanded row is too wide
        for `width` and unfolds only when it would fit again, so the two
        decisions can never chase each other.
        """
        if not self._full_min_width:
            return
        if self._compact:
            if width >= self._full_min_width + self.BREAKPOINT_SLACK:
                self._set_compact(False)
        elif width < self._full_min_width + self.BREAKPOINT_SLACK:
            self._set_compact(True)

    def _set_compact(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        if compact:
            self._layout.removeWidget(self._right_zone)
            self._view_popup.layout().addWidget(self._right_zone)
        else:
            self._view_popup.hide()
            self._view_popup.layout().removeWidget(self._right_zone)
            # Back in front of the View button, which is the last item.
            self._layout.insertWidget(self._layout.count() - 1, self._right_zone)
        self._right_zone.setVisible(True)
        self._view_button.setVisible(compact)

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
        add_track.setToolTip("Patch a new fixture (opens the stage designer)")
        add_track.clicked.connect(self.add_track_requested.emit)
        row.addWidget(add_track)

        # TODO: Future implementation
        #group_btn = QPushButton("🖿 Group")
        #group_btn.setObjectName("TTToolButton")
        #row.addWidget(group_btn)

        #delete_btn = QPushButton("🗑")
        #delete_btn.setObjectName("TTToolButton")
        #row.addWidget(delete_btn)

        row.addWidget(self._separator())

        snap_btn = QPushButton("Snap")
        snap_btn.setIcon(icons.icon("magnet", "#e0e0e0"))
        snap_btn.setIconSize(QSize(14, 14))
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
        seg_wrap, _buttons = segmented(
            [Segment("bars", "Bars"), Segment("time", "Time")],
            on_pick=self.ruler_mode_changed.emit, checked="bars", parent=self)
        row.addWidget(seg_wrap)

        row.addWidget(self._separator())

        markers_btn = QPushButton("Markers")
        markers_btn.setIcon(icons.icon("speech", "#e0e0e0"))
        markers_btn.setIconSize(QSize(14, 14))
        markers_btn.setObjectName("TTToolButton")
        markers_btn.setToolTip("The song's marker track: spoken voice-TTS "
                               "lines, tempo changes, stop-click and the segue "
                               "out of this song")
        markers_btn.clicked.connect(self.markers_requested.emit)
        row.addWidget(markers_btn)

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
        stop_btn.setObjectName("StopButton")
        stop_btn.clicked.connect(self.stop_requested.emit)
        row.addWidget(stop_btn)

        self.play_pause_button = QPushButton()
        self.play_pause_button.setIcon(self.play_icon)
        self.play_pause_button.setObjectName("PlayPauseButton")
        self.play_pause_button.clicked.connect(self.play_pause_requested.emit)
        row.addWidget(self.play_pause_button)

        self.time_display = QLabel("00:00.000")
        self.time_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_display.setObjectName("TimeDisplay")
        row.addWidget(self.time_display)

        # BPM cluster: the click button + spinbox. The button opens the click
        # panel upward (tempo/TAP, meter, accent, count-in, level, free-run);
        # metronome mute stays on the timeline header's CLICK row.
        self.click_button = QPushButton("▴")
        self.click_button.setIcon(icons.icon("note", "#cfcfcf"))
        self.click_button.setIconSize(QSize(18, 18))
        self.click_button.setObjectName("TTClickButton")
        self.click_button.setToolTip(
            "Click: tempo, meter, accent, count-in and level — free-run it, or "
            "set it as the song's tempo")
        self.click_popup = ClickPopup(self)
        self.click_button.clicked.connect(
            lambda: self.click_popup.open_above(self.click_button))
        self.click_popup.open_changed.connect(self._on_click_popup_open)
        row.addWidget(self.click_button)

        self.bpm_spinbox = QSpinBox()
        self.bpm_spinbox.setRange(40, 300)
        self.bpm_spinbox.setValue(120)
        self.bpm_spinbox.setObjectName("TTBpm")
        self.bpm_spinbox.valueChanged.connect(self.bpm_changed.emit)
        row.addWidget(self.bpm_spinbox)

        row.addWidget(self._separator())

        # Editor monitor: which track you HEAR (and see as the waveform).
        # BACKING = the live master; GUIDE = the song's full-mix reference.
        # Session-only — live surfaces always play the backing.
        seg_wrap, monitor_buttons = segmented(
            [Segment(MONITOR_BACKING, "BACKING",
                     "Monitor the backing track (the live master)"),
             Segment(MONITOR_GUIDE, "GUIDE",
                     "Monitor the guide track (full-mix design reference; "
                     "never played live)")],
            on_pick=self.monitor_changed.emit, checked=MONITOR_BACKING,
            parent=self)
        self.monitor_backing_btn = monitor_buttons[MONITOR_BACKING]
        self.monitor_guide_btn = monitor_buttons[MONITOR_GUIDE]
        self.monitor_guide_btn.setProperty("monitor", "guide")
        # Enabled by set_monitor_state once a song declares a guide.
        self.monitor_guide_btn.setEnabled(False)
        row.addWidget(seg_wrap)

        return zone

    def set_monitor_state(self, monitor: str, has_guide: bool) -> None:
        """Reflect the editor monitor + guide availability. When the monitor
        is GUIDE but the song has no guide, BACKING shows checked (that IS
        what's audible — the controller falls back)."""
        if self.monitor_backing_btn is None or self.monitor_guide_btn is None:
            return
        self.monitor_guide_btn.setEnabled(has_guide)
        guide_on = monitor == MONITOR_GUIDE and has_guide
        self.monitor_backing_btn.setChecked(not guide_on)
        self.monitor_guide_btn.setChecked(guide_on)

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
        seg_wrap, self._row_buttons = segmented(
            [Segment("S", "S", "Collapse all tracks"),
             Segment("M", "M"), Segment("L", "L")],
            on_pick=self._pick_row_preset, checked="M", compact=True,
            parent=self)
        row.addWidget(seg_wrap)

        row.addWidget(self._separator())

        # Zoom slider (wired -> timeline horizontal scale)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setObjectName("TTZoom")
        self.zoom_slider.setRange(self.ZOOM_MIN, self.ZOOM_MAX)
        self.zoom_slider.setValue(self.ZOOM_DEFAULT)
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

    def _on_click_popup_open(self, is_open: bool) -> None:
        """Press the click button in while its panel is open, the chevron
        pointing the way it will go."""
        self.click_button.setText("▾" if is_open else "▴")
        self.click_button.setProperty("open", bool(is_open))
        self.click_button.style().unpolish(self.click_button)
        self.click_button.style().polish(self.click_button)

    def set_click_free_running(self, running: bool) -> None:
        """Light the click button while the click free-runs — the panel may be
        closed, and this is the one sign left that the click is its own."""
        self.click_button.setProperty("running", bool(running))
        self.click_button.style().unpolish(self.click_button)
        self.click_button.style().polish(self.click_button)
        self.click_popup.set_free_running(running)

    def set_zoom_value(self, factor: float) -> None:
        """Move the zoom slider to reflect a zoom change driven elsewhere
        (Fit / Ctrl+wheel). Signals are blocked so this doesn't loop back
        into the timeline's set_zoom."""
        value = max(self.ZOOM_MIN, min(self.ZOOM_MAX, round(factor * 100)))
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(value)
        self.zoom_slider.blockSignals(False)

    def _pick_row_preset(self, label: str) -> None:
        """"S" collapses every lane to a single header row (the smallest row
        there is) rather than picking a height preset; M and L are heights."""
        if label == "S":
            self.collapse_all_requested.emit()
        else:
            self.row_height_changed.emit(self.ROW_HEIGHT_PRESETS[label])

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
