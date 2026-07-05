"""
Track resize handle for the timeline header column.

Provides a thin, draggable graphics item placed at the bottom border of a
resizable track's header. Emits live preview signals during the drag so the
timeline can reflow, plus a final commit signal on mouse release for
persistence.
"""

from typing import Callable

from PyQt6.QtWidgets import QGraphicsObject, QGraphicsItem
from PyQt6.QtCore import Qt, pyqtSignal, QRectF
from PyQt6.QtGui import QColor, QPainter


class TrackResizeHandle(QGraphicsObject):
    """A thin grippable bar at the bottom of a track header for height resizing.

    Visually discrete: the separator is a 1px hairline across the full width,
    with a small centered grip pill that hints at draggability. The full band
    stays interactive (easy to grab, resize cursor over the whole seam)."""

    height_preview = pyqtSignal(int)    # new height during drag (clamped)
    height_committed = pyqtSignal(int)  # final new height on release (clamped)

    HANDLE_THICKNESS = 5
    GRIP_WIDTH = 34  # width of the centered grip pill

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

    def paint(self, painter, option, widget):
        active = self._hover or self._is_dragging
        rect = self.boundingRect()
        cy = rect.height() / 2.0

        # Full-width 1px separator hairline, centered in the grab band.
        line_color = QColor(90, 90, 90) if active else QColor(60, 60, 60)
        painter.fillRect(QRectF(0.0, cy - 0.5, rect.width(), 1.0), line_color)

        # Centered grip pill — the only prominent bit, hinting draggability.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        grip_w = min(self.GRIP_WIDTH, rect.width())
        grip_h = rect.height() - 1.0
        grip = QRectF((rect.width() - grip_w) / 2.0,
                      (rect.height() - grip_h) / 2.0, grip_w, grip_h)
        grip_color = QColor(150, 150, 150) if active else QColor(95, 95, 95)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(grip_color)
        painter.drawRoundedRect(grip, grip_h / 2.0, grip_h / 2.0)

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
            delta = event.scenePos().y() - self._press_scene_y
            new_height = max(self._min_height, int(self._press_height + delta))
            self.height_preview.emit(new_height)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._is_dragging:
            self._is_dragging = False
            delta = event.scenePos().y() - self._press_scene_y
            new_height = max(self._min_height, int(self._press_height + delta))
            self.update()
            self.height_committed.emit(new_height)
            event.accept()
            return
        super().mouseReleaseEvent(event)
