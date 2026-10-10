"""
Resize handles for the timeline's track headers.

Two thin, draggable grab bands with the same look and the same contract — a
live preview signal per drag step so the timeline can re-lay itself, then one
commit signal on release for persistence:

  * `TrackResizeHandle` — a graphics item at the bottom border of a resizable
    track's header, dragged vertically to change that track's height;
  * `HeaderResizeHandle` — a widget on the seam between the header strip and
    the content view, dragged horizontally to change the strip's width.

They cannot be one class: the first is a `QGraphicsObject` positioned in the
header scene, the second a `QWidget` living in the timeline's layout *outside*
that scene, and the two hierarchies share neither the mouse-event protocol nor
a way to be positioned. What they do share — how the band is painted — is
`paint_grip` below.
"""

from typing import Callable

from PyQt6.QtWidgets import QGraphicsObject, QWidget, QSizePolicy
from PyQt6.QtCore import Qt, pyqtSignal, QRectF
from PyQt6.QtGui import QColor, QPainter

# The grab band is 5px across so it is easy to hit; the pill on it is 34 along.
BAND_THICKNESS = 5
GRIP_LENGTH = 34


def paint_grip(painter: QPainter, rect: QRectF, vertical: bool, active: bool) -> None:
    """Paint one grab band: a 1px separator hairline down the middle of it with
    a small centered grip pill riding that line.

    Visually discrete on purpose — the hairline reads as the seam it already
    is, and only the pill hints that the seam can be dragged. `vertical` picks
    the axis: the header divider's band runs down the seam, a track's runs
    along its bottom border.
    """
    thickness = rect.width() if vertical else rect.height()
    span = rect.height() if vertical else rect.width()

    # The hairline, centered across the band's thickness.
    line_color = QColor(90, 90, 90) if active else QColor(60, 60, 60)
    mid = thickness / 2.0 - 0.5
    if vertical:
        painter.fillRect(QRectF(rect.x() + mid, rect.y(), 1.0, span), line_color)
    else:
        painter.fillRect(QRectF(rect.x(), rect.y() + mid, span, 1.0), line_color)

    # The grip pill — the only prominent bit, hinting draggability.
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pill_long = min(GRIP_LENGTH, span)
    pill_short = thickness - 1.0
    w, h = (pill_short, pill_long) if vertical else (pill_long, pill_short)
    pill = QRectF(rect.x() + (rect.width() - w) / 2.0,
                  rect.y() + (rect.height() - h) / 2.0, w, h)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(150, 150, 150) if active else QColor(95, 95, 95))
    painter.drawRoundedRect(pill, pill_short / 2.0, pill_short / 2.0)


class TrackResizeHandle(QGraphicsObject):
    """A grippable bar at the bottom of a track header, for height resizing.

    The full band is interactive (easy to grab, resize cursor over the whole
    seam), and it spans the header strip — so it is re-measured whenever that
    strip is."""

    height_preview = pyqtSignal(int)    # new height during drag (clamped)
    height_committed = pyqtSignal(int)  # final new height on release (clamped)

    HANDLE_THICKNESS = BAND_THICKNESS  # the manager positions handles off this

    def __init__(self, width: int, min_height: int,
                 get_current_height: Callable[[], int], parent=None):
        super().__init__(parent)
        self._width = width
        self._min_height = min_height
        self._get_current_height = get_current_height
        self._is_dragging = False
        self._press_scene_y = 0.0
        self._press_height = 0
        self._hover = False

        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setZValue(6)  # Above track headers
        # Vertical-resize cursor (two arrows) whenever the pointer is over the
        # band; Qt shows it on hover and keeps it through the drag-grab.
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def boundingRect(self):
        return QRectF(0, 0, self._width, self.HANDLE_THICKNESS)

    def set_width(self, width: int) -> None:
        """Re-measure the band after the header strip was resized (dragged
        divider or UI-scale change) — it spans the header, and its grip pill is
        centered on that span."""
        width = int(width)
        if width == self._width:
            return
        self.prepareGeometryChange()
        self._width = width
        self.update()

    def paint(self, painter, option, widget):
        paint_grip(painter, self.boundingRect(), vertical=False,
                   active=self._hover or self._is_dragging)

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_dragging = True
            self._press_scene_y = event.scenePos().y()
            self._press_height = int(self._get_current_height())
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._is_dragging:
            self.height_preview.emit(self._dragged_height(event))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._is_dragging:
            self._is_dragging = False
            height = self._dragged_height(event)
            self.update()
            self.height_committed.emit(height)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _dragged_height(self, event) -> int:
        """The track height this pointer position asks for. The minimum is the
        handle's own — unlike the header's bounds, it is a plain constant."""
        delta = event.scenePos().y() - self._press_scene_y
        return max(self._min_height, int(self._press_height + delta))


class HeaderResizeHandle(QWidget):
    """A grippable bar between the track headers and the content view.

    TrackResizeHandle's band turned on its side, and a widget rather than a
    scene item: it sits on the seam *outside* the header view, which is
    fixed-width and clips the scene it draws."""

    width_preview = pyqtSignal(int)    # new header width during drag
    width_committed = pyqtSignal(int)  # final new header width on release

    def __init__(self, get_current_width: Callable[[], int], parent=None):
        super().__init__(parent)
        self._get_current_width = get_current_width
        self._is_dragging = False
        # The handle itself travels as the header resizes under it, so the drag
        # is measured in screen coordinates — a local x would jump by exactly
        # the delta it just applied on every move.
        self._press_global_x = 0.0
        self._press_width = 0
        self._hover = False

        self.setFixedWidth(BAND_THICKNESS)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.SizeHorCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        paint_grip(painter, QRectF(self.rect()), vertical=True,
                   active=self._hover or self._is_dragging)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_dragging = True
            self._press_global_x = event.globalPosition().x()
            self._press_width = int(self._get_current_width())
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._is_dragging:
            self.width_preview.emit(self._dragged_width(event))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._is_dragging:
            self._is_dragging = False
            width = self._dragged_width(event)
            self.update()
            self.width_committed.emit(width)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _dragged_width(self, event) -> int:
        """The header width this pointer position asks for. Unclamped: the
        bounds depend on the UI scale, so the timeline owns them."""
        return int(self._press_width + event.globalPosition().x() - self._press_global_x)
