# ABOUTME: MarkersDialog — per-song editor for the marker track: spoken voice
# ABOUTME: cues plus the control markers (tempo change, stop click) and segue.

from typing import List, Optional, Tuple

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (QColor, QIcon, QPainter, QPainterPath, QPen,
                         QPixmap)
from PyQt6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox,
                             QDialog, QDialogButtonBox, QDoubleSpinBox, QFrame,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QMenu, QPushButton, QSpinBox, QStackedWidget,
                             QTableWidget, QTableWidgetItem, QToolButton,
                             QVBoxLayout, QWidget)

from audio import tts
from core import markers as core_markers
from core.config import ControlMarkerConfig, VoiceTTSConfig
from core.tempo_map import BEATS_PER_BAR, TempoMap
from tupperlogger import get_logger
from ui.widgets import icons

MODE_SECONDS = "seconds"
MODE_BARBEAT = "barbeat"


def _format_clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _split_bar_beat(value: float):
    """Decode a `bar.beat` spinbox value (53.2 → bar 53, beat 2).

    One decimal digit IS the beat, which only reads correctly while the beat
    stays a single digit — true for the 4/4 this app counts everywhere."""
    bar = max(1, int(value))
    beat = int(round((value - bar) * 10))
    return bar, min(BEATS_PER_BAR, max(1, beat))


def _kind_swatch(kind: str, size: int = 10) -> QPixmap:
    """The little filled square that identifies a kind in the table."""
    pixmap = QPixmap(size + 6, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(core_markers.kind_color(kind)))
    painter.drawRoundedRect(QRectF(0, size * 0.15, size * 0.7, size * 0.7), 2, 2)
    painter.end()
    return pixmap


class TimeSpin(QDoubleSpinBox):
    """Time entry in either clock seconds or musical bar.beat.

    Bar.beat is not a scaled version of seconds — the conversion runs through
    the song's tempo map, so the same box means different instants before and
    after a tempo marker. Stepping walks BEATS, which is why `stepBy` is
    overridden rather than left to the 0.1 single-step: 53.4 must roll to 54.1,
    not to 53.5, or a bar would appear to have ten beats.

    The exact seconds are held here rather than read back off the widget: bar
    .beat resolves only to the beat, so a value merely LOOKED at in bar.beat
    would come back quantized. The widget shows the position; this holds it.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = MODE_SECONDS
        self._tempo_map = TempoMap(120)
        self._seconds = 0.0
        self._apply_mode()
        # Connected first, so the dialog's own valueChanged slot always reads
        # a _seconds that already reflects the edit.
        self.valueChanged.connect(self._adopt_widget_value)

    def _adopt_widget_value(self, _value) -> None:
        self._seconds = self._widget_seconds()

    def set_context(self, mode: str, tempo_map: TempoMap) -> None:
        """Switch units and/or tempo map, holding the instant being shown."""
        seconds = self._seconds
        self._mode = mode
        self._tempo_map = tempo_map
        self._apply_mode()
        self.set_seconds(seconds)

    def _apply_mode(self) -> None:
        blocked = self.blockSignals(True)
        if self._mode == MODE_SECONDS:
            self.setDecimals(2)
            self.setSingleStep(0.5)
            self.setRange(0.0, 36000.0)
        else:
            self.setDecimals(1)
            self.setSingleStep(0.1)
            self.setRange(1.1, 99999.4)
        self.blockSignals(blocked)

    def _widget_seconds(self) -> float:
        """The instant the box is currently SHOWING."""
        if self._mode == MODE_SECONDS:
            return float(self.value())
        return max(0.0, self._tempo_map.seconds_at_bar_beat(
            *_split_bar_beat(self.value())))

    def seconds(self) -> float:
        return self._seconds

    def set_seconds(self, seconds: float) -> None:
        self._seconds = max(0.0, float(seconds))
        blocked = self.blockSignals(True)
        if self._mode == MODE_SECONDS:
            self.setValue(self._seconds)
        else:
            bar, beat = self._tempo_map.bar_beat_at(self._seconds)
            self.setValue(bar + beat / 10.0)
        self.blockSignals(blocked)

    def stepBy(self, steps: int) -> None:
        if self._mode == MODE_SECONDS:
            super().stepBy(steps)
            return
        bar, beat = _split_bar_beat(self.value())
        index = max(0, (bar - 1) * BEATS_PER_BAR + (beat - 1) + steps)
        self.setValue(index // BEATS_PER_BAR + 1
                      + (index % BEATS_PER_BAR + 1) / 10.0)


class MarkerStrip(QWidget):
    """The song as one horizontal rail with its markers on it.

    A table sorted by time says WHEN each marker is; only a strip says how they
    sit relative to each other and to the end of the song — which is the
    question being asked when a cue is dropped "somewhere in the last chorus".
    Dragging a marker here is the same edit as typing in the inspector.
    """

    marker_selected = pyqtSignal(int)      # row uid
    marker_moved = pyqtSignal(int, float)  # row uid, new time
    segue_moved = pyqtSignal(float)

    HIT_PX = 8
    SEGUE_UID = -2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MarkerStrip")
        self._rows = []          # [(uid, time, kind)]
        self._segue_time = None  # float | None
        self._duration = 60.0
        self._selected_uid = None
        self._drag_uid = None
        self.setMouseTracking(True)

    # -- data ----------------------------------------------------------
    def set_rows(self, rows, selected_uid=None) -> None:
        self._rows = list(rows)
        self._selected_uid = selected_uid
        self.update()

    def set_segue(self, time_sec: Optional[float]) -> None:
        self._segue_time = time_sec
        self.update()

    def set_duration(self, duration: float) -> None:
        self._duration = max(1.0, float(duration or 0.0))
        self.update()

    # -- geometry ------------------------------------------------------
    def _margin(self) -> float:
        return 10.0

    def _x_for(self, seconds: float) -> float:
        span = max(1.0, self.width() - 2 * self._margin())
        return self._margin() + (max(0.0, seconds) / self._duration) * span

    def _time_for(self, x: float) -> float:
        span = max(1.0, self.width() - 2 * self._margin())
        ratio = (x - self._margin()) / span
        return max(0.0, min(self._duration, ratio * self._duration))

    def _rail_y(self) -> float:
        return self.height() * 0.72

    def _marker_size(self) -> float:
        # Derived from the widget's own height so the theme's ui_scale, which
        # only ever rewrites the stylesheet, still reaches this painting.
        return max(7.0, min(13.0, self.height() * 0.30))

    # -- painting ------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.fillRect(self.rect(), QColor("#232323"))

        rail_y = self._rail_y()
        painter.setPen(QPen(QColor("#4a4a4a"), 1))
        painter.drawLine(int(self._margin()), int(rail_y),
                         int(self.width() - self._margin()), int(rail_y))

        # Quarter ticks: enough of a scale to read a position off the rail.
        for fraction in (0.25, 0.5, 0.75):
            x = self._x_for(self._duration * fraction)
            painter.setPen(QPen(QColor("#3a3a3a"), 1))
            painter.drawLine(int(x), int(rail_y - 4), int(x), int(rail_y + 4))

        painter.setPen(QColor("#8a8a8a"))
        font = painter.font()
        font.setPointSizeF(max(6.5, font.pointSizeF() - 1))
        painter.setFont(font)
        painter.drawText(int(self._margin()), int(rail_y - 8), "0:00")
        end_text = _format_clock(self._duration)
        width = painter.fontMetrics().horizontalAdvance(end_text)
        painter.drawText(int(self.width() - self._margin() - width),
                         int(rail_y - 8), end_text)

        size = self._marker_size()
        for uid, seconds, kind in self._rows:
            self._paint_marker(painter, self._x_for(seconds), rail_y, size,
                               core_markers.kind_color(kind),
                               uid == self._selected_uid)

        if self._segue_time is not None:
            self._paint_segue(painter, self._x_for(self._segue_time), rail_y,
                              size)
        painter.end()

    def _paint_marker(self, painter, x, rail_y, size, color, selected):
        rect = QRectF(x - size / 2, rail_y - size / 2, size, size)
        painter.setBrush(QColor(color))
        painter.setPen(QPen(QColor("#f0f0f0"), 1.5) if selected
                       else Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 2, 2)
        if selected:
            painter.setPen(QPen(QColor(color), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(int(x), int(rail_y + size), int(x),
                             int(self.height() - 2))

    def _paint_segue(self, painter, x, rail_y, size):
        """The segue is drawn as an arrow, not a square: it is not a moment in
        this song so much as the door out of it."""
        half = size / 2
        path = QPainterPath()
        path.moveTo(x - half, rail_y - half)
        path.lineTo(x + half, rail_y)
        path.lineTo(x - half, rail_y + half)
        path.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(core_markers.kind_color(core_markers.KIND_SEGUE)))
        painter.drawPath(path)

    # -- interaction ---------------------------------------------------
    def _hit(self, x: float) -> Optional[int]:
        best, best_distance = None, self.HIT_PX
        for uid, seconds, _kind in self._rows:
            distance = abs(self._x_for(seconds) - x)
            if distance <= best_distance:
                best, best_distance = uid, distance
        if self._segue_time is not None:
            distance = abs(self._x_for(self._segue_time) - x)
            if distance <= best_distance:
                best = self.SEGUE_UID
        return best

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        uid = self._hit(event.position().x())
        if uid is None:
            return
        self._drag_uid = uid
        if uid != self.SEGUE_UID:
            self._selected_uid = uid
            self.marker_selected.emit(uid)
        self.update()

    def mouseMoveEvent(self, event):
        x = event.position().x()
        if self._drag_uid is None:
            self.setCursor(Qt.CursorShape.PointingHandCursor if self._hit(x)
                           is not None else Qt.CursorShape.ArrowCursor)
            return
        seconds = self._time_for(x)
        if self._drag_uid == self.SEGUE_UID:
            self.segue_moved.emit(seconds)
        else:
            self.marker_moved.emit(self._drag_uid, seconds)

    def mouseReleaseEvent(self, event):
        self._drag_uid = None


class MarkersDialog(QDialog):
    """Editor for a song's marker track.

    Four things ride a song's timeline that are neither notes nor audio: a
    spoken voice-TTS line, a tempo change, a stop-click, and the segue out of
    the song. They are one editing job — "what happens, and when" — so they share
    one table, one strip and one time-unit toggle here, and are split back into
    their storage shapes only on the way out (voice TTS pre-renders to PCM,
    control markers act on the transport, the segue is one flag per song).

    A pure editor: the accessors return the result and the caller persists.
    """

    def __init__(self, voice_tts: List[VoiceTTSConfig],
                 control_markers: List[ControlMarkerConfig], parent=None,
                 audio_engine=None, tts_voice: str = tts.DEFAULT_VOICE,
                 segue: bool = False, segue_time: float = 0.0,
                 base_bpm: int = 120, duration: float = 0.0,
                 song_title: str = "", has_next_song: bool = True,
                 select: Optional[Tuple[str, float]] = None):
        super().__init__(parent)
        self.logger = get_logger("ui.markers")
        self.audio_engine = audio_engine
        self.tts_voice = tts_voice
        self.base_bpm = int(base_bpm)
        self.duration = float(duration or 0.0)
        self.has_next_song = bool(has_next_song)

        self._next_uid = 1
        self._rows = []           # dicts: uid, time, kind, text, bpm
        self._selected_uid = None
        self._time_mode = MODE_SECONDS
        self._tempo_map = TempoMap(self.base_bpm)
        self._loading = False

        for cue in voice_tts:
            self._new_row(cue.time, core_markers.KIND_VOICE, text=cue.text)
        for marker in control_markers:
            self._new_row(marker.time, marker.kind, bpm=marker.bpm)
        self._rows.sort(key=lambda r: r["time"])

        self.setObjectName("MarkersDialog")
        self.setWindowTitle("Song markers")
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setObjectName("MarkersLayout")
        layout.addLayout(self._build_header(song_title))
        layout.addWidget(self._build_strip())
        layout.addLayout(self._build_body(), 1)
        layout.addWidget(self._build_segue_bar(segue, segue_time))
        layout.addLayout(self._build_footer())

        if not tts.is_available() or not tts.is_voice_installed(tts_voice):
            self.hint_label.setText(
                f'{icons.img_tag("warning", "#e2a04a", 11)} '
                "TTS voice not ready (Settings ▸ AUDIO) — voice TTS stays silent.")

        self._rebuild_tempo_map()
        opening = self._uid_for(*select) if select else None
        if opening is None and self._rows:
            opening = self._rows[0]["uid"]
        self._refresh(select_uid=opening)
        if select and select[0] == core_markers.KIND_SEGUE:
            # The segue is no row, so there is nothing to select for it — the
            # field it does have gets the caret instead, or a double-click on
            # its flag would look like it had opened the dialog at random.
            self.segue_spin.setFocus()
            self.segue_spin.selectAll()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_header(self, song_title):
        row = QHBoxLayout()
        title = QLabel(song_title or "Untitled song")
        title.setObjectName("MarkersSongTitle")
        row.addWidget(title)

        meta = [_format_clock(self.duration)] if self.duration > 0 else []
        meta += [f"{self.base_bpm} BPM", "4/4"]
        meta_label = QLabel("  ·  ".join(meta))
        meta_label.setObjectName("SettingsDimLabel")
        row.addWidget(meta_label)
        row.addStretch(1)

        self.hint_label = QLabel("Voice plays on the VOICE bus — "
                                 "same side as the click.")
        self.hint_label.setObjectName("SettingsDimLabel")
        row.addWidget(self.hint_label)
        return row

    def _build_strip(self):
        self.strip = MarkerStrip(self)
        self.strip.set_duration(self._strip_duration())
        self.strip.marker_selected.connect(self._select_uid)
        self.strip.marker_moved.connect(self._on_strip_moved)
        self.strip.segue_moved.connect(self._on_strip_segue_moved)
        return self.strip

    def _build_body(self):
        body = QHBoxLayout()
        body.setObjectName("MarkersBody")

        left = QVBoxLayout()
        self.table = QTableWidget(0, 3)
        self.table.setObjectName("MarkersTable")
        self.table.setHorizontalHeaderLabels(["Time", "Type", "Text / value"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        left.addWidget(self.table, 1)
        left.addLayout(self._build_row_buttons())
        body.addLayout(left, 1)

        body.addWidget(self._build_inspector())
        return body

    def _build_row_buttons(self):
        row = QHBoxLayout()
        row.setObjectName("MarkersButtonRow")

        self.add_button = QToolButton()
        self.add_button.setObjectName("MarkersAddButton")
        self.add_button.setText("Add")
        self.add_button.setIcon(icons.icon("plus"))
        self.add_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.add_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        menu = QMenu(self.add_button)
        for kind in (core_markers.KIND_VOICE, core_markers.KIND_TEMPO,
                     core_markers.KIND_CLICK_STOP):
            action = menu.addAction(core_markers.kind_label(kind))
            action.triggered.connect(lambda _checked, k=kind: self._add(k))
        self.add_button.setMenu(menu)
        # The button half adds the kind the dialog is mostly used for; the
        # arrow half offers the rest.
        self.add_button.clicked.connect(
            lambda: self._add(core_markers.KIND_VOICE))
        row.addWidget(self.add_button)

        self.remove_button = QPushButton("Remove")
        self.remove_button.setIcon(icons.icon("minus"))
        self.remove_button.clicked.connect(self._remove_selected)
        row.addWidget(self.remove_button)

        self.preview_button = QPushButton("Preview")
        self.preview_button.setIcon(icons.icon("play"))
        self.preview_button.setToolTip("Render the selected voice-TTS line and play "
                                       "it on the voice bus")
        self.preview_button.clicked.connect(self._preview_selected)
        row.addWidget(self.preview_button)
        row.addStretch(1)
        return row

    def _build_inspector(self):
        panel = QFrame()
        panel.setObjectName("MarkersInspector")
        column = QVBoxLayout(panel)

        self.inspector_title = QLabel("NO MARKER")
        self.inspector_title.setObjectName("MarkersInspectorTitle")
        column.addWidget(self.inspector_title)

        self.time_label = QLabel("Time (s)")
        self.time_label.setObjectName("SettingsDimLabel")
        column.addWidget(self.time_label)
        self.time_spin = TimeSpin()
        self.time_spin.setObjectName("MarkersTimeSpin")
        self.time_spin.valueChanged.connect(self._on_time_edited)
        column.addWidget(self.time_spin)

        self.pages = QStackedWidget()
        self.pages.setObjectName("MarkersPages")

        # Voice TTS: the spoken text.
        voice_page = QWidget()
        voice_layout = QVBoxLayout(voice_page)
        voice_layout.setContentsMargins(0, 0, 0, 0)
        text_label = QLabel("Spoken text")
        text_label.setObjectName("SettingsDimLabel")
        voice_layout.addWidget(text_label)
        self.text_edit = QLineEdit()
        self.text_edit.setObjectName("MarkersTextEdit")
        self.text_edit.setPlaceholderText("e.g. entra batería")
        self.text_edit.textChanged.connect(self._on_text_edited)
        voice_layout.addWidget(self.text_edit)
        voice_layout.addStretch(1)
        self.pages.addWidget(voice_page)

        # Tempo change: the new click tempo.
        tempo_page = QWidget()
        tempo_layout = QVBoxLayout(tempo_page)
        tempo_layout.setContentsMargins(0, 0, 0, 0)
        bpm_label = QLabel("Tempo (BPM)")
        bpm_label.setObjectName("SettingsDimLabel")
        tempo_layout.addWidget(bpm_label)
        self.bpm_spin = QSpinBox()
        self.bpm_spin.setObjectName("MarkersBpmSpin")
        self.bpm_spin.setRange(40, 300)
        self.bpm_spin.valueChanged.connect(self._on_bpm_edited)
        tempo_layout.addWidget(self.bpm_spin)
        tempo_note = QLabel("The click and the bar grid follow from here. The "
                            "song's base tempo is unchanged.")
        tempo_note.setObjectName("SettingsDimLabel")
        tempo_note.setWordWrap(True)
        tempo_layout.addWidget(tempo_note)
        tempo_layout.addStretch(1)
        self.pages.addWidget(tempo_page)

        # Stop click: nothing to configure but the time.
        stop_page = QWidget()
        stop_layout = QVBoxLayout(stop_page)
        stop_layout.setContentsMargins(0, 0, 0, 0)
        stop_note = QLabel("The click stops here and stays off for the rest of "
                           "the song. Seeking back before this marker brings "
                           "it back.")
        stop_note.setObjectName("SettingsDimLabel")
        stop_note.setWordWrap(True)
        stop_layout.addWidget(stop_note)
        stop_layout.addStretch(1)
        self.pages.addWidget(stop_page)

        # Nothing selected.
        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.setContentsMargins(0, 0, 0, 0)
        empty_note = QLabel("Select a marker to edit it, or add one below.")
        empty_note.setObjectName("SettingsDimLabel")
        empty_note.setWordWrap(True)
        empty_layout.addWidget(empty_note)
        empty_layout.addStretch(1)
        self.pages.addWidget(empty_page)

        column.addWidget(self.pages, 1)
        self._pages_by_kind = {
            core_markers.KIND_VOICE: 0,
            core_markers.KIND_TEMPO: 1,
            core_markers.KIND_CLICK_STOP: 2,
        }
        self._empty_page = 3
        return panel

    def _build_segue_bar(self, segue, segue_time):
        bar = QFrame()
        bar.setObjectName("MarkersSegueBar")
        row = QHBoxLayout(bar)

        self.segue_check = QCheckBox("Segue to next song")
        self.segue_check.setChecked(bool(segue) and self.has_next_song)
        self.segue_check.toggled.connect(self._on_segue_toggled)
        row.addWidget(self.segue_check)

        self.segue_hint = QLabel("no count-in → next in setlist")
        self.segue_hint.setObjectName("SettingsDimLabel")
        row.addWidget(self.segue_hint)
        row.addStretch(1)

        if not self.has_next_song:
            self.segue_check.setEnabled(False)
            self.segue_hint.setText("last song in the setlist — nothing to segue to")

        # Time-unit toggle. It governs EVERY time in the dialog (the table
        # column, the inspector and this field), so the table header renames
        # with it — otherwise a bar.beat value under a "Time (s)" heading reads
        # as a wrong number rather than a different unit.
        self.unit_group = QButtonGroup(self)
        seconds_btn = QPushButton("Time")
        barbeat_btn = QPushButton("Bar.beat")
        self._unit_buttons = {MODE_SECONDS: seconds_btn,
                              MODE_BARBEAT: barbeat_btn}
        for mode, button in self._unit_buttons.items():
            button.setObjectName("MarkersUnitButton")
            button.setCheckable(True)
            button.clicked.connect(lambda _c, m=mode: self._set_time_mode(m))
            self.unit_group.addButton(button)
        seconds_btn.setChecked(True)
        segment = QHBoxLayout()
        segment.setContentsMargins(0, 0, 0, 0)
        segment.setSpacing(0)
        segment.addWidget(seconds_btn)
        segment.addWidget(barbeat_btn)
        wrap = QWidget()
        wrap.setLayout(segment)
        row.addWidget(wrap)

        self.segue_spin = TimeSpin()
        self.segue_spin.setObjectName("MarkersSegueSpin")
        self.segue_spin.set_seconds(segue_time or self._default_segue_time())
        self.segue_spin.valueChanged.connect(self._on_segue_time_edited)
        row.addWidget(self.segue_spin)

        self.segue_unit_label = QLabel("seconds")
        self.segue_unit_label.setObjectName("SettingsDimLabel")
        row.addWidget(self.segue_unit_label)
        return bar

    def _build_footer(self):
        row = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setObjectName("SettingsDimLabel")
        row.addWidget(self.status_label)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        return row

    # ------------------------------------------------------------------
    # Row model
    # ------------------------------------------------------------------
    def _new_row(self, time_sec, kind, text="", bpm=0):
        row = {"uid": self._next_uid, "time": float(time_sec), "kind": kind,
               "text": text, "bpm": int(bpm) or self.base_bpm}
        self._next_uid += 1
        self._rows.append(row)
        return row

    def _uid_for(self, kind: str, time_sec: float) -> Optional[int]:
        """The row a caller pointed at, by (kind, time).

        That pair is how the timeline names a marker — its ruler flags are
        drawn from tuples, not from these rows — so opening the dialog "on"
        one means finding it the same way `core.markers.marker_at` does."""
        candidates = [r for r in self._rows if r["kind"] == kind]
        if not candidates:
            return None
        return min(candidates,
                   key=lambda r: abs(r["time"] - float(time_sec)))["uid"]

    def _row(self, uid):
        for row in self._rows:
            if row["uid"] == uid:
                return row
        return None

    def _selected_row(self):
        return self._row(self._selected_uid) if self._selected_uid else None

    def _strip_duration(self):
        """What the strip spans.

        A song with a known length spans exactly that, so the end label is the
        song's own end — widened only if a marker sits past it, which would
        otherwise fall off the rail. Only a song with no audio yet gets the
        padded fallback."""
        latest = max([r["time"] for r in self._rows] + [0.0])
        # The strip is built before the segue bar, so this runs once with no
        # segue widgets yet.
        if getattr(self, "segue_check", None) is not None \
                and self.segue_check.isChecked():
            latest = max(latest, self.segue_spin.seconds())
        if self.duration > 0:
            return max(self.duration, latest)
        return max(latest * 1.15, 30.0)

    def _default_segue_time(self):
        return self.duration if self.duration > 0 else 0.0

    def _rebuild_tempo_map(self):
        """The dialog's own map, rebuilt after every tempo edit — the bar.beat
        columns and fields are read through it, so it has to track the markers
        being edited rather than the ones the song was opened with."""
        changes = [(r["time"], r["bpm"]) for r in self._rows
                   if r["kind"] == core_markers.KIND_TEMPO]
        self._tempo_map = TempoMap(self.base_bpm, changes)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------
    def _refresh(self, select_uid=None):
        """Re-sort, redraw the table/strip/inspector/status from the rows."""
        if select_uid is not None:
            self._selected_uid = select_uid
        self._rows.sort(key=lambda r: r["time"])
        self._rebuild_tempo_map()

        self._loading = True
        try:
            self.table.setRowCount(0)
            for index, row in enumerate(self._rows):
                self.table.insertRow(index)
                time_item = QTableWidgetItem(self._format_time(row["time"]))
                self.table.setItem(index, 0, time_item)

                type_item = QTableWidgetItem(
                    core_markers.kind_label(row["kind"]))
                type_item.setIcon(QIcon(_kind_swatch(row["kind"])))
                self.table.setItem(index, 1, type_item)

                self.table.setItem(index, 2,
                                   QTableWidgetItem(self._row_value(row)))
                if row["uid"] == self._selected_uid:
                    self.table.selectRow(index)
        finally:
            self._loading = False

        self.strip.set_duration(self._strip_duration())
        self.strip.set_rows([(r["uid"], r["time"], r["kind"])
                             for r in self._rows], self._selected_uid)
        self.strip.set_segue(self.segue_spin.seconds()
                             if self.segue_check.isChecked() else None)
        self._refresh_inspector()
        self._refresh_status()

    def _format_time(self, seconds):
        if self._time_mode == MODE_SECONDS:
            return f"{seconds:.1f}"
        bar, beat = self._tempo_map.bar_beat_at(seconds)
        return f"{bar}.{beat}"

    def _row_value(self, row):
        if row["kind"] == core_markers.KIND_VOICE:
            return row["text"]
        if row["kind"] == core_markers.KIND_TEMPO:
            return f"{row['bpm']} BPM"
        return "click off"

    def _refresh_inspector(self):
        row = self._selected_row()
        self.time_spin.set_context(self._time_mode, self._tempo_map)
        if row is None:
            self.inspector_title.setText("NO MARKER")
            self.pages.setCurrentIndex(self._empty_page)
            self.time_spin.setEnabled(False)
            self.remove_button.setEnabled(False)
            self.preview_button.setEnabled(False)
            return

        self.inspector_title.setText(
            core_markers.kind_label(row["kind"]).upper())
        self.pages.setCurrentIndex(self._pages_by_kind[row["kind"]])
        self.time_spin.setEnabled(True)
        self.remove_button.setEnabled(True)
        self.preview_button.setEnabled(
            row["kind"] == core_markers.KIND_VOICE
            and self.audio_engine is not None)

        self._loading = True
        try:
            self.time_spin.set_seconds(row["time"])
            if row["kind"] == core_markers.KIND_VOICE:
                self.text_edit.setText(row["text"])
            elif row["kind"] == core_markers.KIND_TEMPO:
                self.bpm_spin.setValue(row["bpm"])
        finally:
            self._loading = False

    def _refresh_status(self):
        counts = {}
        for row in self._rows:
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
        parts = []
        for kind, word in ((core_markers.KIND_VOICE, "voice"),
                           (core_markers.KIND_TEMPO, "tempo"),
                           (core_markers.KIND_CLICK_STOP, "click stop")):
            if counts.get(kind):
                parts.append(f"{counts[kind]} {word}")
        if self.segue_check.isChecked():
            parts.append(f"segue at {self._format_segue_status()}")
        self.status_label.setText(" · ".join(parts) or "no markers")

    def _format_segue_status(self):
        seconds = self.segue_spin.seconds()
        if self._time_mode == MODE_SECONDS:
            return f"{seconds:.1f} s"
        bar, beat = self._tempo_map.bar_beat_at(seconds)
        return f"bar {bar}.{beat}"

    # ------------------------------------------------------------------
    # Editing
    # ------------------------------------------------------------------
    def _set_time_mode(self, mode):
        if mode == self._time_mode:
            return
        self._time_mode = mode
        seconds = mode == MODE_SECONDS
        # The toggle is the mode's view, not its owner: a mode set from
        # anywhere else has to move the buttons too.
        self._unit_buttons[mode].setChecked(True)
        self.table.setHorizontalHeaderLabels(
            ["Time", "Type", "Text / value"] if seconds
            else ["Bar.beat", "Type", "Text / value"])
        self.time_label.setText("Time (s)" if seconds else "Time (bar.beat)")
        self.segue_unit_label.setText("seconds" if seconds else "bar.beat")
        self.segue_spin.set_context(mode, self._tempo_map)
        self._refresh()

    def _add(self, kind):
        """Add a marker after the selected one (or at the end)."""
        selected = self._selected_row()
        base = selected["time"] if selected else max(
            [r["time"] for r in self._rows] + [0.0])
        row = self._new_row(base, kind)
        self._refresh(select_uid=row["uid"])
        if kind == core_markers.KIND_VOICE:
            self.text_edit.setFocus()
            self.text_edit.selectAll()

    def _remove_selected(self):
        row = self._selected_row()
        if row is None:
            return
        index = self._rows.index(row)
        self._rows.remove(row)
        following = self._rows[min(index, len(self._rows) - 1)] \
            if self._rows else None
        self._refresh(select_uid=following["uid"] if following else None)
        if not self._rows:
            self._selected_uid = None
            self._refresh_inspector()

    def _select_uid(self, uid):
        self._selected_uid = uid
        self._refresh_inspector()
        self.strip.set_rows([(r["uid"], r["time"], r["kind"])
                             for r in self._rows], self._selected_uid)
        for index, row in enumerate(self._rows):
            if row["uid"] == uid:
                self._loading = True
                try:
                    self.table.selectRow(index)
                finally:
                    self._loading = False
                break

    def _on_table_selection(self):
        if self._loading:
            return
        index = self.table.currentRow()
        if 0 <= index < len(self._rows):
            self._selected_uid = self._rows[index]["uid"]
            self._refresh_inspector()
            self.strip.set_rows([(r["uid"], r["time"], r["kind"])
                                 for r in self._rows], self._selected_uid)

    def _on_time_edited(self, _value):
        if self._loading:
            return
        row = self._selected_row()
        if row is None:
            return
        row["time"] = self.time_spin.seconds()
        self._refresh()

    def _on_text_edited(self, text):
        if self._loading:
            return
        row = self._selected_row()
        if row is None or row["kind"] != core_markers.KIND_VOICE:
            return
        row["text"] = text
        self._refresh_table_value(row)

    def _on_bpm_edited(self, value):
        if self._loading:
            return
        row = self._selected_row()
        if row is None or row["kind"] != core_markers.KIND_TEMPO:
            return
        row["bpm"] = int(value)
        # A tempo edit moves every bar line after it, so the whole dialog is
        # re-read rather than just this row's value cell.
        self._refresh()

    def _refresh_table_value(self, row):
        """Update one row's value cell in place — a text edit must not rebuild
        the table, or the QLineEdit would lose focus on every keystroke."""
        for index, candidate in enumerate(self._rows):
            if candidate is row:
                item = self.table.item(index, 2)
                if item is not None:
                    item.setText(self._row_value(row))
                return

    def _on_strip_moved(self, uid, seconds):
        row = self._row(uid)
        if row is None:
            return
        row["time"] = seconds
        self._refresh(select_uid=uid)

    def _on_strip_segue_moved(self, seconds):
        self._loading = True
        try:
            self.segue_spin.set_seconds(seconds)
        finally:
            self._loading = False
        self._refresh()

    def _on_segue_toggled(self, checked):
        if checked and self.segue_spin.seconds() <= 0:
            self.segue_spin.set_seconds(self._default_segue_time())
        self._refresh()

    def _on_segue_time_edited(self, _value):
        if self._loading:
            return
        self._refresh()

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------
    def _preview_selected(self):
        row = self._selected_row()
        if row is None or row["kind"] != core_markers.KIND_VOICE \
                or not row["text"].strip():
            self.status_label.setText("Select a voice-TTS marker with text to preview.")
            return
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            samples = tts.render(row["text"].strip(), self.tts_voice)
        finally:
            self.unsetCursor()
        if samples is None:
            self.status_label.setText(
                f'{icons.img_tag("warning", "#e2a04a", 11)} '
                "Could not render — voice not ready (Settings ▸ AUDIO).")
            return
        self._refresh_status()
        if self.audio_engine is not None:
            self.audio_engine.play_voice(samples)

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------
    def voice_tts(self) -> List[VoiceTTSConfig]:
        """The edited voice-TTS markers, sorted by time (blank rows dropped).

        Still `VoiceTTSConfig` under the `voice_tts` key: the label changed,
        the show format did not."""
        return sorted(
            (VoiceTTSConfig(time=r["time"], text=r["text"].strip())
             for r in self._rows
             if r["kind"] == core_markers.KIND_VOICE and r["text"].strip()),
            key=lambda c: c.time)

    def control_markers(self) -> List[ControlMarkerConfig]:
        """The edited control markers, sorted by time."""
        return sorted(
            (ControlMarkerConfig(time=r["time"], kind=r["kind"],
                                 bpm=r["bpm"] if r["kind"] ==
                                 core_markers.KIND_TEMPO else 0)
             for r in self._rows if r["kind"] in core_markers.CONTROL_KINDS),
            key=lambda m: m.time)

    def segue_enabled(self) -> bool:
        return bool(self.segue_check.isChecked() and self.has_next_song)

    def segue_time(self) -> float:
        return self.segue_spin.seconds()
