"""DMX channel monitor panel (right-sidebar "DMX Channels" tab).

Two view modes toggled by a segmented control:
- USED: fixture sections (swatch + name + DMX range) with a wrapping grid of
  channel cells; each cell shows its value as a vertical fill (height ∝ value/255)
  coloured by the fixture's type colour.
- FULL 512: one dense grid of the whole universe; patched channels tinted in their
  fixture colour, unpatched channels dark with a dimmed 000.

The grid is drawn by a single custom-painted canvas (one paintEvent for every
cell) rather than per-cell widgets, so live updates at ~10 Hz are cheap. Fixture
colours come from ``core.fixture_library.color_for_type`` so the panel reads as one
system with the clip library and the timeline track headers.
"""

import math
from dataclasses import dataclass
from typing import List, Optional

from PyQt6.QtWidgets import (QWidget, QScrollArea, QFrame, QPushButton,
                             QButtonGroup, QLabel, QVBoxLayout, QHBoxLayout)
from PyQt6.QtGui import QPainter, QColor, QFont, QPainterPath
from PyQt6.QtCore import Qt, QRectF, pyqtSignal

from tupperlogger import get_logger
from core.config import Config, fixture_channel_count
from core.fixture_library import color_for_type


MODE_USED = "used"
MODE_FULL = "full"

# Geometry
PAD = 8
GAP = 6
SEC_GAP = 12
HDR_H = 22
HDR_GRID_GAP = 6
SWATCH = 10

CELL_MIN_W = 50
MAX_COLS_USED = 8
CELL_H_USED = 38
R_USED = 2
VAL_PT_USED = 10

FULL_MIN_W = 30
FULL_COLS = 12
CELL_H_FULL = 34
R_FULL = 2
VAL_PT_FULL = 9

CHAN_PT = 7
FILL_ALPHA = 210

# Vertical drag sensitivity: DMX units changed per pixel of mouse movement.
DRAG_SENSITIVITY = 1.5

# Colours (custom paint doesn't inherit QSS; blend with the dark theme)
COL_PANEL_BG = QColor("#2b2b2b")
COL_CELL_BG = QColor("#333333")
COL_CELL_BG_EMPTY = QColor("#242424")
COL_CELL_BORDER = QColor("#4a4a4a")
COL_CHAN_LABEL = QColor("#9a9a9a")
COL_VALUE = QColor("#ffffff")
COL_VALUE_DIM = QColor("#5a5a5a")
COL_RANGE = QColor("#8a8a8a")
COL_HEADER_NAME = QColor("#e0e0e0")


@dataclass
class _Section:
    """One fixture's section in the USED view."""
    name: str
    color: QColor
    channels: List[int]
    dmx_range: str


class _GridCanvas(QWidget):
    """Custom-painted grid of channel cells for the active view mode.

    Reads its model and DMX buffer from the owning ``DMXDisplayWidget`` and
    draws everything in a single ``paintEvent``. ``relayout`` recomputes the
    cached cell/header rectangles (on resize, mode change or config change);
    live value updates only trigger a repaint.
    """

    def __init__(self, owner: "DMXDisplayWidget"):
        super().__init__(owner)
        # Track motion without a pressed button so the cursor can react to
        # hovering an editable cell (see mouseMoveEvent).
        self.setMouseTracking(True)
        self._owner = owner
        self._cells = []      # (QRectF, channel, QColor|None)
        self._headers = []    # (QRectF, _Section)

        # Live-edit drag state (see mousePressEvent).
        self._drag_channel: Optional[int] = None
        self._drag_start_y = 0.0
        self._drag_start_value = 0

        self._chan_font = QFont()
        self._chan_font.setPointSize(CHAN_PT)
        self._val_font_used = QFont()
        self._val_font_used.setPointSize(VAL_PT_USED)
        self._val_font_used.setBold(True)
        self._val_font_full = QFont()
        self._val_font_full.setPointSize(VAL_PT_FULL)
        self._val_font_full.setBold(True)
        self._hdr_name_font = QFont()
        self._hdr_name_font.setPointSize(10)
        self._hdr_name_font.setBold(True)
        self._hdr_range_font = QFont()
        self._hdr_range_font.setPointSize(9)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _columns_for(self, avail: float, min_w: int, max_cols: int) -> int:
        cols = int((avail + GAP) // (min_w + GAP))
        return max(1, min(cols, max_cols))

    def relayout(self) -> None:
        self._cells = []
        self._headers = []
        avail = self.width() - 2 * PAD
        if avail <= 0:
            self.update()
            return
        if self._owner._mode == MODE_USED:
            total = self._layout_used(avail)
        else:
            total = self._layout_full(avail)
        total = max(int(round(total)), 0)
        if self.minimumHeight() != total:
            self.setMinimumHeight(total)
        self.update()

    def _layout_used(self, avail: float) -> float:
        cols = self._columns_for(avail, CELL_MIN_W, MAX_COLS_USED)
        cell_w = (avail - (cols - 1) * GAP) / cols
        y = PAD
        for i, sec in enumerate(self._owner._sections):
            if i > 0:
                y += SEC_GAP
            self._headers.append((QRectF(PAD, y, avail, HDR_H), sec))
            y += HDR_H + HDR_GRID_GAP
            rows = math.ceil(len(sec.channels) / cols)
            for idx, ch in enumerate(sec.channels):
                r, c = divmod(idx, cols)
                x = PAD + c * (cell_w + GAP)
                cy = y + r * (CELL_H_USED + GAP)
                self._cells.append(
                    (QRectF(x, cy, cell_w, CELL_H_USED), ch, sec.color))
            y += rows * CELL_H_USED + (rows - 1) * GAP
        return y + PAD

    def _layout_full(self, avail: float) -> float:
        cols = self._columns_for(avail, FULL_MIN_W, FULL_COLS)
        cell_w = (avail - (cols - 1) * GAP) / cols
        rows = math.ceil(512 / cols)
        for idx in range(512):
            r, c = divmod(idx, cols)
            x = PAD + c * (cell_w + GAP)
            y = PAD + r * (CELL_H_FULL + GAP)
            self._cells.append((QRectF(x, y, cell_w, CELL_H_FULL),
                                idx + 1, self._owner._full_colors[idx]))
        return PAD + rows * CELL_H_FULL + (rows - 1) * GAP + PAD

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.relayout()

    # ------------------------------------------------------------------
    # Live editing: left-drag a cell vertically to fade its value; right-click
    # toggles it between full (255) and off (0).
    # ------------------------------------------------------------------
    def _cell_at(self, pos) -> Optional[int]:
        for rect, channel, _color in self._cells:
            if rect.contains(pos):
                return channel
        return None

    def _editable_channel_at(self, pos) -> Optional[int]:
        """Channel under `pos` if it can be edited (editing on, within cap)."""
        if not self._owner._editable:
            return None
        channel = self._cell_at(pos)
        if channel is not None and channel <= self._owner._max_edit_channel:
            return channel
        return None

    def mousePressEvent(self, event):
        pos = event.position()
        if event.button() == Qt.MouseButton.RightButton:
            channel = self._editable_channel_at(pos)
            if channel is not None:
                value = 0 if self._owner.get_channel_value(channel) == 255 else 255
                self._owner._set_manual_value(channel, value)
                event.accept()
                return
        elif event.button() == Qt.MouseButton.LeftButton:
            channel = self._editable_channel_at(pos)
            if channel is not None:
                self._drag_channel = channel
                self._drag_start_y = pos.y()
                self._drag_start_value = self._owner.get_channel_value(channel)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_channel is not None:
            dy = self._drag_start_y - event.position().y()
            value = int(round(self._drag_start_value + dy * DRAG_SENSITIVITY))
            value = max(0, min(255, value))
            self._owner._set_manual_value(self._drag_channel, value)
            event.accept()
            return
        # Not dragging: show the fader cursor only while over an editable cell.
        over_cell = self._editable_channel_at(event.position()) is not None
        self.setCursor(Qt.CursorShape.SizeVerCursor if over_cell
                       else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if (self._drag_channel is not None
                and event.button() == Qt.MouseButton.LeftButton):
            self._drag_channel = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), COL_PANEL_BG)

        if self._owner._mode == MODE_USED and not self._owner._sections:
            p.setPen(COL_RANGE)
            font = p.font()
            font.setPointSize(11)
            p.setFont(font)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "No fixtures patched.")
            p.end()
            return

        for rect, sec in self._headers:
            self._draw_header(p, rect, sec)
        compact = self._owner._mode == MODE_FULL
        for rect, channel, color in self._cells:
            self._draw_cell(p, rect, channel, color, compact)
        p.end()

    def _draw_header(self, p: QPainter, rect: QRectF, sec: _Section) -> None:
        sy = rect.y() + (rect.height() - SWATCH) / 2
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect.x(), sy, SWATCH, SWATCH), 2, 2)
        p.fillPath(path, sec.color)

        p.setFont(self._hdr_name_font)
        p.setPen(COL_HEADER_NAME)
        name_rect = QRectF(rect.x() + SWATCH + 8, rect.y(),
                           rect.width() - SWATCH - 8, rect.height())
        p.drawText(name_rect,
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   sec.name)

        p.setFont(self._hdr_range_font)
        p.setPen(COL_RANGE)
        p.drawText(rect,
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sec.dmx_range)

    def _draw_cell(self, p: QPainter, rect: QRectF, channel: int,
                   color: Optional[QColor], compact: bool) -> None:
        radius = R_FULL if compact else R_USED
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        p.fillPath(path, COL_CELL_BG_EMPTY if color is None else COL_CELL_BG)

        value = self._owner._dmx[channel - 1] if 1 <= channel <= 512 else 0
        if color is not None and value > 0:
            fill_h = rect.height() * value / 255.0
            p.save()
            p.setClipPath(path)
            fc = QColor(color)
            fc.setAlpha(FILL_ALPHA)
            p.fillRect(QRectF(rect.x(), rect.y() + rect.height() - fill_h,
                              rect.width(), fill_h), fc)
            p.restore()

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(COL_CELL_BORDER)
        p.drawPath(path)

        p.setFont(self._chan_font)
        p.setPen(COL_CHAN_LABEL if color is not None else COL_VALUE_DIM)
        p.drawText(rect.adjusted(5, 2, -3, 0),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                   f"{channel:03d}")

        if color is None:
            txt, pen = "000", COL_VALUE_DIM
        else:
            txt, pen = f"{value:03d}", COL_VALUE
        p.setFont(self._val_font_full if compact else self._val_font_used)
        p.setPen(pen)
        p.drawText(rect.adjusted(0, 0, -3, -2),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                   txt)


class DMXDisplayWidget(QWidget):
    """Right-sidebar panel that monitors live DMX channel values."""

    # Emitted when the user drags a cell to a new value (channel 1-indexed,
    # value 0-255). MainWindow forwards it to the live software controller.
    channel_value_edited = pyqtSignal(int, int)

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.setObjectName("DmxDisplay")
        self.config = config
        self.logger = get_logger("ui.dmx_display")

        self._dmx = bytearray(512)
        self._mode = MODE_USED
        self._sections: List[_Section] = []
        self._full_colors: List[Optional[QColor]] = [None] * 512
        self._used_total = 0
        self._editable = False
        self._max_edit_channel = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)
        root.addWidget(self._build_toolbar())

        self._scroll = QScrollArea()
        self._scroll.setObjectName("DmxGridScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._canvas = _GridCanvas(self)
        self._scroll.setWidget(self._canvas)
        root.addWidget(self._scroll, 1)

        # The splitter (and the slide animation) drive the actual width.
        self.setMinimumWidth(0)

        self._rebuild_model()
        self.logger.info("DMX display widget initialized")

    # ------------------------------------------------------------------
    # Chrome
    # ------------------------------------------------------------------
    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("DmxToolbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._seg_group = QButtonGroup(self)
        self._seg_group.setExclusive(True)

        self._seg_used = QPushButton("USED · 0")
        self._seg_used.setObjectName("DmxSeg")
        self._seg_used.setProperty("pos", "left")
        self._seg_used.setCheckable(True)
        self._seg_used.setChecked(True)
        self._seg_used.setCursor(Qt.CursorShape.PointingHandCursor)

        self._seg_full = QPushButton("FULL 512")
        self._seg_full.setObjectName("DmxSeg")
        self._seg_full.setProperty("pos", "right")
        self._seg_full.setCheckable(True)
        self._seg_full.setCursor(Qt.CursorShape.PointingHandCursor)

        self._seg_group.addButton(self._seg_used)
        self._seg_group.addButton(self._seg_full)
        self._seg_used.toggled.connect(self._on_mode_toggled)

        layout.addWidget(self._seg_used)
        layout.addWidget(self._seg_full)
        layout.addStretch(1)

        self._universe = QLabel(
            "<span style='color:#4caf50;'>●</span>&nbsp;U1")
        self._universe.setObjectName("DmxUniverse")
        layout.addWidget(self._universe)
        return bar

    def _on_mode_toggled(self, checked: bool) -> None:
        self._mode = MODE_USED if checked else MODE_FULL
        self._canvas.relayout()

    # ------------------------------------------------------------------
    # Model
    # ------------------------------------------------------------------
    def _rebuild_model(self) -> None:
        """Build the section list and channel→colour map from the patch.

        Mirrors ``Config.get_used_dmx_channels`` naming/numbering so section
        titles read the same ("Ledbar 1"), but keeps each fixture's type so the
        section colour matches the clip library and timeline (by type)."""
        sections: List[_Section] = []
        full_colors: List[Optional[QColor]] = [None] * 512
        type_counts = {}
        for fx in self.config.fixtures:
            type_counts[fx.type] = type_counts.get(fx.type, 0) + 1
            name = f"{fx.type.capitalize()} {type_counts[fx.type]}"
            channels = list(range(fx.dmx_start,
                                  fx.dmx_start + fixture_channel_count(fx.type)))
            if not channels:
                continue
            color = QColor(color_for_type(self.config.fixtures, fx.type))
            sections.append(_Section(
                name=name, color=color, channels=channels,
                dmx_range=f"{channels[0]:03d}-{channels[-1]:03d}"))
            for ch in channels:
                if 1 <= ch <= 512:
                    full_colors[ch - 1] = color

        self._sections = sections
        self._full_colors = full_colors
        self._used_total = sum(len(s.channels) for s in sections)
        self._seg_used.setText(f"USED · {self._used_total}")
        self._canvas.relayout()

    # ------------------------------------------------------------------
    # Public API (kept stable for main.py)
    # ------------------------------------------------------------------
    def update_dmx_data(self, dmx_data: bytearray) -> None:
        """Copy a live DMX frame and schedule a repaint."""
        n = min(len(dmx_data), 512)
        self._dmx[:n] = dmx_data[:n]
        self._canvas.update()

    def get_channel_value(self, channel: int) -> int:
        """Current value for a 1-indexed DMX channel (0 if out of range)."""
        if 1 <= channel <= 512:
            return self._dmx[channel - 1]
        return 0

    def set_editing_enabled(self, enabled: bool, max_channel: int = 0) -> None:
        """Allow editing of channel values up to ``max_channel``.

        Only meaningful while a software controller is active to receive the
        edits — MainWindow wires this to ``software_controller_changed``. The
        fader cursor is applied per-cell on hover (see the canvas); here we just
        reset it when editing turns off."""
        self._editable = bool(enabled)
        self._max_edit_channel = int(max_channel) if enabled else 0
        if not self._editable:
            self._canvas.setCursor(Qt.CursorShape.ArrowCursor)

    def _set_manual_value(self, channel: int, value: int) -> None:
        """Show a dragged value immediately and emit it for the live source."""
        if 1 <= channel <= 512:
            self._dmx[channel - 1] = value
            self._canvas.update()
        self.channel_value_edited.emit(channel, value)

    def refresh_configuration(self, config: Config) -> None:
        """Rebuild sections/colours when the fixture patch changes.

        The DMX buffer is kept so values don't flash to zero on a repatch."""
        self.config = config
        self._rebuild_model()
        self.logger.info("DMX display refreshed with new configuration")
