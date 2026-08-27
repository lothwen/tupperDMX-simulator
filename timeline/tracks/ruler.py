"""
Ruler track for timeline time markers.

Displays time markers, bar numbers, and beat subdivisions.
"""

import math

from PyQt6.QtWidgets import (QGraphicsRectItem, QGraphicsLineItem,
                             QGraphicsTextItem, QGraphicsObject)
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QPainter,
                         QPainterPath, QPen)
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtWidgets import QGraphicsItem

from .base import BaseTrack
from core import markers as core_markers
from tupperlogger import get_logger
from ui.widgets import icons

# The top strip of the ruler band, which belongs to the markers alone: their
# flags and name chips sit in it, and only their stems cross into the ticks
# below. Everything else the ruler draws starts under it.
MARKER_LANE_H = 17

# Marker flag geometry, in DEVICE pixels (see MarkerFlag). The flag body fills
# the marker lane; the stem spans the whole band, down to where the ticks are,
# because the stem is the marker's actual position.
MARKER_STEM_W = 3
MARKER_FLAG_W = 20
MARKER_FLAG_H = 16
MARKER_FLAG_TOP = 1
MARKER_GLYPH_PX = 12
MARKER_CHIP_H = 14
MARKER_CHIP_GAP = 3
MARKER_CHIP_PAD = 5
MARKER_CHIP_MAX_W = 130
# Opacity of the drop line through the arrangement (hover/drag only).
MARKER_LINE_ALPHA = 190
# Screen-space snap radius, matching the clips' and song sections' feel.
MARKER_SNAP_PX = 6


class MarkerFlag(QGraphicsObject):
    """One marker on the ruler: a stem, a filled flag carrying the kind's
    glyph, and a chip naming it.

    Painted in DEVICE pixels (`ItemIgnoresTransformations`) because the
    timeline zooms horizontally only — a flag sized in scene units would smear
    with the zoom, and a marker is a point in time, not a span.

    Dragging retimes the marker. Like every other draggable on this timeline
    the move is committed on RELEASE (`move_committed`); mid-drag is purely
    visual, and the redraw that follows the commit is what makes it real.

    Pointed at or dragged, the flag drops a dashed line through the whole
    arrangement — what relates a cue to the clips it is meant to sit with, and
    what a drag is aimed by. It is deliberately not permanent: half a dozen
    lines standing over the lanes is clutter, and a line that only exists under
    the cursor can never be caught holding a stale scene height.
    """

    move_committed = pyqtSignal(str, float, float)  # kind, old_time, new_time
    edit_requested = pyqtSignal(str, float)         # kind, time

    def __init__(self, kind: str, label: str, marker_time: float,
                 height: int, timeline_manager, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.marker_time = float(marker_time)
        self._timeline = timeline_manager
        self._height = height
        self._color = QColor(core_markers.kind_color(kind))
        self._glyph = icons.pixmap(
            core_markers.KIND_ICONS.get(kind, "speech"), MARKER_GLYPH_PX,
            "#141414")
        self._hovered = False
        self._dragging = False
        self._line_height = 0.0
        self.chip_shown = True
        self._press_scene_x = None
        self._press_pos_x = None

        self._font = self.font_for_chip()
        self.chip_text, self._chip_width = self._measure_chip(label)

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setToolTip(f"{core_markers.kind_label(kind)}: {label}" if label
                        else core_markers.kind_label(kind))

    # --- Geometry / paint -------------------------------------------------

    @staticmethod
    def font_for_chip() -> QFont:
        """The chip's font: the app default, one point down and bold — small
        enough to sit in a 25px ruler, heavy enough to read over the ticks."""
        font = QFont()
        font.setBold(True)
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        return font

    def _measure_chip(self, label: str):
        """The chip's text and width. A voice cue names itself with what it
        SAYS and a tempo marker with its BPM; the kinds whose label carries no
        such payload fall back to the kind's own name."""
        text = label if self.kind in (core_markers.KIND_VOICE,
                                      core_markers.KIND_TEMPO) else ""
        text = (text or core_markers.kind_label(self.kind)).strip()
        metrics = QFontMetricsF(self._font)
        text = metrics.elidedText(text, Qt.TextElideMode.ElideRight,
                                  MARKER_CHIP_MAX_W - 2 * MARKER_CHIP_PAD)
        return text, metrics.horizontalAdvance(text) + 2 * MARKER_CHIP_PAD

    def chip_extent(self) -> float:
        """How much room the flag needs, in device px, to show its chip."""
        return (MARKER_STEM_W + MARKER_FLAG_W + MARKER_CHIP_GAP
                + self._chip_width)

    def set_chip_shown(self, shown: bool) -> None:
        """Show or hide the name chip. Zoomed out, two cues a second apart
        would stack their chips into an unreadable pile — the flag itself is
        small enough to stay, so the text is what gives way (the same trade the
        bar numbers make)."""
        shown = bool(shown)
        if shown == self.chip_shown:
            return
        self.prepareGeometryChange()
        self.chip_shown = shown
        self.update()

    def boundingRect(self):
        width = (self.chip_extent() if self.chip_shown
                 else MARKER_STEM_W + MARKER_FLAG_W)
        height = (max(self._height, self._line_height) if self._line_active()
                  else self._height)
        return QRectF(-1, 0, width + 2, height)

    def shape(self):
        """The flag alone, never the drop line: the line is an overlay across
        the lanes, and a shape that included it would swallow every click meant
        for the clips underneath — and end the hover the moment it appeared."""
        path = QPainterPath()
        path.addRect(QRectF(-1, 0, self.boundingRect().width(), self._height))
        return path

    def _flag_path(self) -> QPainterPath:
        """The flag body: a rectangle with its bottom-right corner cut away,
        so the shape points back at the stem that carries the time."""
        left, top = MARKER_STEM_W, float(MARKER_FLAG_TOP)
        right, bottom = left + MARKER_FLAG_W, top + MARKER_FLAG_H
        notch = 5.0
        path = QPainterPath()
        path.moveTo(left, top)
        path.lineTo(right, top)
        path.lineTo(right, bottom - notch)
        path.lineTo(right - notch, bottom)
        path.lineTo(left, bottom)
        path.closeSubpath()
        return path

    def paint(self, painter, option, widget):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = (self._color.lighter(125) if self._line_active()
                 else QColor(self._color))

        if self._line_active() and self._line_height > self._height:
            trail = QColor(color)
            trail.setAlpha(MARKER_LINE_ALPHA)
            pen = QPen(trail, 1.0, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawLine(QPointF(MARKER_STEM_W / 2, self._height),
                             QPointF(MARKER_STEM_W / 2, self._line_height))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRect(QRectF(0, 0, MARKER_STEM_W, self._height))
        painter.drawPath(self._flag_path())

        painter.drawPixmap(
            QPointF(MARKER_STEM_W + (MARKER_FLAG_W - MARKER_GLYPH_PX) / 2,
                    MARKER_FLAG_TOP + (MARKER_FLAG_H - MARKER_GLYPH_PX) / 2),
            self._glyph)

        if not self.chip_shown:
            return
        chip = QRectF(MARKER_STEM_W + MARKER_FLAG_W + MARKER_CHIP_GAP,
                      MARKER_FLAG_TOP + 0.5, self._chip_width, MARKER_CHIP_H)
        painter.setBrush(QColor(22, 22, 22, 225))
        painter.setPen(QPen(color, 1.0))
        painter.drawRoundedRect(chip, 3, 3)
        painter.setPen(color)
        painter.setFont(self._font)
        painter.drawText(chip.adjusted(MARKER_CHIP_PAD, 0, -MARKER_CHIP_PAD, 0),
                         int(Qt.AlignmentFlag.AlignLeft
                             | Qt.AlignmentFlag.AlignVCenter),
                         self.chip_text)

    # --- Interaction ------------------------------------------------------

    def _line_active(self) -> bool:
        return self._hovered or self._dragging

    def set_hovered(self, hovered: bool) -> None:
        """Point at (or stop pointing at) the flag — what a hover event means,
        and the whole of what makes the drop line appear."""
        self._set_state(hovered=bool(hovered))

    def _set_state(self, hovered=None, dragging=None) -> None:
        """Apply a hover/drag change, measuring the arrangement as the line
        appears. The height is taken then rather than kept: the scene grows and
        shrinks with every lane collapse and track resize, and only a line that
        outlives the cursor could be caught holding an old one."""
        was = self._line_active()
        if hovered is not None:
            self._hovered = hovered
        if dragging is not None:
            self._dragging = dragging
        now = self._line_active()
        if now and not was:
            self._line_height = float(
                getattr(self._timeline, "scene_height", 0.0) or 0.0)
        if now != was:
            self.prepareGeometryChange()
        self.update()

    def hoverEnterEvent(self, event):
        self.set_hovered(True)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.set_hovered(False)
        super().hoverLeaveEvent(event)

    def _snap_x(self, x: float) -> float:
        """Clamp to >=0 and snap to the nearest visible grid line within
        MARKER_SNAP_PX — the same grid the clips snap to, so a cue lands on the
        beat it is meant for."""
        x = max(0.0, x)
        grid_x = self._timeline.snap_grid_x() if self._timeline else None
        if grid_x is None:
            return x
        zoom = self._timeline.content_view.transform().m11() or 1.0
        nearest = grid_x(x)
        if abs(nearest - x) <= MARKER_SNAP_PX / zoom:
            return max(0.0, nearest)
        return x

    def mousePressEvent(self, event):
        self._press_scene_x = event.scenePos().x()
        self._press_pos_x = self.pos().x()
        self._dragging = False
        event.accept()

    def mouseMoveEvent(self, event):
        if self._press_scene_x is None:
            super().mouseMoveEvent(event)
            return
        delta_x = event.scenePos().x() - self._press_scene_x
        if abs(delta_x) >= 1.0 and not self._dragging:
            # A drag holds the line up even where the cursor slips off the flag
            # (a clamp at 0, a long snap), which a hover alone would not.
            self._set_state(dragging=True)
        self.setPos(self._snap_x(self._press_pos_x + delta_x), self.pos().y())
        event.accept()

    def mouseDoubleClickEvent(self, event):
        """Hand the marker to the Cues dialog — its text, its BPM and its very
        existence are that dialog's job; the ruler only ever moves it.

        Nothing touches the flag after the signal goes out: the editor is
        modal, and whatever it commits redraws the ruler out from under it."""
        event.accept()
        self.edit_requested.emit(self.kind, self.marker_time)

    def mouseReleaseEvent(self, event):
        # The commit redraws the ruler, which disposes of this very item — so
        # the drag state is dropped BEFORE the signal goes out.
        dragged, x = self._dragging, self.pos().x()
        self._press_scene_x = None
        self._press_pos_x = None
        self._set_state(dragging=False)
        event.accept()
        pps = self._timeline.PIXELS_PER_SECOND if self._timeline else 0
        if not dragged or pps <= 0:
            return
        new_time = round(max(0.0, x / pps), 3)
        if abs(new_time - self.marker_time) > 1e-4:
            self.move_committed.emit(self.kind, self.marker_time, new_time)


class RulerTrack(BaseTrack):
    """Track for displaying timeline ruler with time markers."""
    
    # Time-mode tick spacing (clock seconds, BPM-independent).
    TIME_MINOR_INTERVAL = 1  # minor tick every second
    TIME_MAJOR_INTERVAL = 5  # major tick + m:ss label every 5 seconds

    def __init__(self, timeline_manager, content_scene, header_scene):
        super().__init__(timeline_manager, content_scene, header_scene)
        self.bar_text_items = []  # Store bar text items for label updates
        self.time_text_items = []  # Store time text items (seconds, item) for label updates
        self.logger = get_logger("timeline.tracks.ruler")
        self.permanent_header_items = []  # Items that should never be cleared (metronome controls)
        self.marker_items = []  # MarkerFlag items (own lifecycle, see _dispose_markers)
        self._stale_markers = []
        
    def get_track_height(self) -> int:
        """Get the height of the ruler track."""
        return self.timeline_manager.RULER_TRACK_HEIGHT
        
    def draw_header(self):
        """Draw the ruler header background.

        The visible controls live on the manager-owned combined header panel
        (TRACKS / MASTER / METRO), which overlays this band as one block.
        """
        # Only create the permanent background once.
        if not self.permanent_header_items:
            ruler_header_bg = QGraphicsRectItem(0, 0, self.timeline_manager.HEADER_WIDTH, self.get_track_height())
            ruler_header_bg.setBrush(QBrush(QColor(50, 50, 50)))
            ruler_header_bg.setPen(QPen(QColor(20, 20, 20)))
            self.header_scene.addItem(ruler_header_bg)
            self.permanent_header_items.append(ruler_header_bg)

    def resize_header(self, width: int) -> None:
        """Re-measure the background at a new header width.

        The band is created once and kept (see `draw_header`), so a UI-scale
        change has to reach it here — every other header redraws itself from
        `HEADER_WIDTH` on the next reflow.
        """
        for item in self.permanent_header_items:
            item.setRect(0, 0, width, self.get_track_height())

    def draw_ruler(self):
        """Draw the ruler with bar or clock-time markers based on the mode."""
        # Clear existing ruler items (content only, not header)
        for item in self.items:
            if item.scene():
                item.scene().removeItem(item)
        self.items.clear()
        self.bar_text_items.clear()
        self.time_text_items.clear()
        self._dispose_markers()

        ruler_bg = QGraphicsRectItem(0, 0, self.timeline_manager.content_width, self.get_track_height())
        ruler_bg.setBrush(QBrush(QColor(40, 40, 40)))
        ruler_bg.setPen(QPen(Qt.GlobalColor.transparent))
        self.content_scene.addItem(ruler_bg)
        self.items.append(ruler_bg)

        if getattr(self.timeline_manager, "ruler_mode", "bars") == "time":
            self._draw_time_ticks()
        else:
            self._draw_bar_ticks()

        self._draw_markers()

        self.update_ruler_labels()

    def _label_y(self, text_item) -> float:
        """Where a bar/time number sits: centred in the ticks' lane, not in the
        band — the band's top strip is the markers' and numbering it there
        would put the digits back under their chips."""
        return MARKER_LANE_H + (self.get_track_height() - MARKER_LANE_H
                                - text_item.boundingRect().height()) / 2

    def _draw_bar_ticks(self):
        """Draw bar lines, in-bar subdivision ticks and bar-number labels.

        Each bar is split into ``beat_division`` parts (1 = bar lines only,
        4 = beats, 8 = half-beats), matching the toolbar's beat-division combo.
        """
        pps = self.timeline_manager.PIXELS_PER_SECOND
        divisions = self.timeline_manager.beat_division
        tempo_map = self.timeline_manager.tempo_map
        until = self.content_scene.width() / pps

        # The walk is the tempo map's, so a bar after a tempo marker is drawn
        # at its real width rather than at the base tempo's.
        top = MARKER_LANE_H  # the ticks live under the marker lane
        for seconds, bar_num, is_bar in tempo_map.grid_lines(until, divisions):
            x = seconds * pps
            if not is_bar:
                div_tick = QGraphicsLineItem(
                    x, top + (self.get_track_height() - top) * 0.5,
                    x, self.get_track_height())
                div_pen = QPen(QColor(150, 150, 150))
                div_pen.setCosmetic(True)
                div_tick.setPen(div_pen)
                self.content_scene.addItem(div_tick)
                self.items.append(div_tick)
                continue

            bar_tick = QGraphicsLineItem(x, top, x, self.get_track_height())
            bar_pen = QPen(QColor(200, 200, 200))
            bar_pen.setCosmetic(True)
            bar_tick.setPen(bar_pen)
            self.content_scene.addItem(bar_tick)
            self.items.append(bar_tick)

            bar_text = QGraphicsTextItem(str(bar_num))
            bar_text.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            bar_text.setDefaultTextColor(QColor(200, 200, 200))
            bar_text.setPos(x + 2, self._label_y(bar_text))
            self.content_scene.addItem(bar_text)
            self.items.append(bar_text)
            self.bar_text_items.append((bar_num, bar_text))

    def _draw_time_ticks(self):
        """Draw clock-time ticks and m:ss labels (Time mode), BPM-independent."""
        pps = self.timeline_manager.PIXELS_PER_SECOND
        total_seconds = int(self.content_scene.width() / pps)

        top = MARKER_LANE_H  # the ticks live under the marker lane
        for sec in range(total_seconds + 1):
            tick_x = sec * pps
            is_major = sec % self.TIME_MAJOR_INTERVAL == 0

            if is_major:
                tick = QGraphicsLineItem(tick_x, top, tick_x, self.get_track_height())
                pen = QPen(QColor(200, 200, 200))
            else:
                tick = QGraphicsLineItem(
                    tick_x, top + (self.get_track_height() - top) * 0.5,
                    tick_x, self.get_track_height())
                pen = QPen(QColor(150, 150, 150))
            pen.setCosmetic(True)
            tick.setPen(pen)
            self.content_scene.addItem(tick)
            self.items.append(tick)

            if is_major:
                label = QGraphicsTextItem(f"{sec // 60}:{sec % 60:02d}")
                label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
                label.setDefaultTextColor(QColor(200, 200, 200))
                label.setPos(tick_x + 2, self._label_y(label))
                self.content_scene.addItem(label)
                self.items.append(label)
                self.time_text_items.append((sec, label))

    def _draw_markers(self):
        """Draw the song's marker track on the ruler: voice cues plus the
        control markers (tempo change, stop click, segue).

        Kind decides the colour and glyph, read from core.markers so a marker
        looks the same here as it does in the Cues dialog. Dragging a flag
        retimes it — everything else about a marker (its text, its BPM, adding
        and removing) still belongs to that dialog.

        The flags are QGraphicsObjects rebuilt on every redraw, so they are
        disposed of the same defensive way the clips are (see
        `_dispose_markers`): the commit that follows a drag redraws the ruler
        from inside the dragged flag's own event handler."""
        self._dispose_markers()
        pps = self.timeline_manager.PIXELS_PER_SECOND
        rows = [(t, core_markers.KIND_VOICE, text)
                for t, text in getattr(self.timeline_manager, "voice_tts", [])]
        rows += list(getattr(self.timeline_manager, "control_markers", []))

        for marker_time, kind, label in sorted(rows, key=lambda r: r[0]):
            flag = MarkerFlag(kind, label, float(marker_time),
                              self.get_track_height(), self.timeline_manager)
            flag.setPos(float(marker_time) * pps, 0)
            flag.setZValue(4)  # over the bar numbers, under the playhead (10)
            flag.move_committed.connect(
                self.timeline_manager.marker_move_committed)
            flag.edit_requested.connect(
                self.timeline_manager.marker_edit_requested)
            self.content_scene.addItem(flag)
            self.marker_items.append(flag)

    def _dispose_markers(self):
        """Retire the marker flags, holding the last batch alive one cycle.

        Same contract as the clips and song sections: an item is hidden and
        removed, then `deleteLater`d rather than dropped, because a flag can be
        replaced from inside its own mouse handler and the scene's BSP index
        must not be left pointing at freed memory."""
        stale = list(self.marker_items)
        for item in stale:
            item.setVisible(False)
            item.setEnabled(False)
            item.prepareGeometryChange()
            if item.scene():
                item.scene().removeItem(item)
            item.deleteLater()
        self.marker_items.clear()
        self._stale_markers = stale

    def update_ruler_labels(self):
        """Update ruler label visibility based on zoom level."""
        if not hasattr(self.timeline_manager, 'content_view'):
            return

        transform = self.timeline_manager.content_view.transform()
        if transform.m11() == 0:
            return

        if getattr(self.timeline_manager, "ruler_mode", "bars") == "time":
            self._update_time_labels(transform.m11())
        else:
            self._update_bar_labels(transform.m11())
        self._update_marker_chips(transform.m11())

    def _update_marker_chips(self, scale):
        """Hide a marker's name chip when the next marker is too close for it.

        Distances are in DEVICE px (the flags ignore the view transform), so
        the zoom has to be folded in by hand here."""
        pps = self.timeline_manager.PIXELS_PER_SECOND
        flags = self.marker_items  # already in time order
        for index, flag in enumerate(flags):
            if index + 1 >= len(flags):
                flag.set_chip_shown(True)
                continue
            gap = (flags[index + 1].marker_time - flag.marker_time) * pps * scale
            flag.set_chip_shown(gap >= flag.chip_extent() + MARKER_CHIP_GAP)

    def _update_bar_labels(self, scale):
        """Hide bar-number labels when bars get too dense to read."""
        if not self.bar_text_items:
            return

        # The NARROWEST bar decides: one fast section is enough to collide the
        # numbers, and hiding some labels but not others reads as a glitch.
        seconds_per_bar = self.timeline_manager.tempo_map.shortest_bar_seconds()
        bar_width_pixels = seconds_per_bar * self.timeline_manager.PIXELS_PER_SECOND * scale

        MIN_PIXEL_SPACING = 40

        if bar_width_pixels < MIN_PIXEL_SPACING:
            for bar_num, text_item in self.bar_text_items:
                if bar_num == 1 or bar_num % 5 == 0:
                    text_item.setVisible(True)
                else:
                    text_item.setVisible(False)
        else:
            for bar_num, text_item in self.bar_text_items:
                text_item.setVisible(True)

    def _update_time_labels(self, scale):
        """Hide m:ss labels when major ticks get too dense to read."""
        if not self.time_text_items:
            return

        label_spacing_pixels = self.TIME_MAJOR_INTERVAL * self.timeline_manager.PIXELS_PER_SECOND * scale

        MIN_PIXEL_SPACING = 40

        if label_spacing_pixels < MIN_PIXEL_SPACING:
            step = math.ceil(MIN_PIXEL_SPACING / label_spacing_pixels)
            for sec, text_item in self.time_text_items:
                major_index = sec // self.TIME_MAJOR_INTERVAL
                text_item.setVisible(major_index % step == 0)
        else:
            for _sec, text_item in self.time_text_items:
                text_item.setVisible(True)

    def clear_items(self):
        """Override to preserve permanent header items (metronome controls)."""
        self._dispose_markers()
        # Only clear regular items, not permanent ones
        for item in self.items:
            if item.scene():
                item.scene().removeItem(item)
        self.items.clear()
        
        # Clear regular header items but preserve permanent ones
        for item in self.header_items:
            if item.scene():
                item.scene().removeItem(item)
        self.header_items.clear()
        
        # Don't clear permanent_header_items - they persist for the lifetime of the track

    def cleanup(self):
        """Cleanup permanent header items when track is destroyed."""
        for item in self.permanent_header_items:
            if item.scene():
                item.scene().removeItem(item)
        self.permanent_header_items.clear()
        
        # Call parent cleanup for regular items
        self.clear_items()

    def redraw(self):
        """Redraw the ruler track."""
        self.draw_ruler()