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
from PyQt6.QtGui import QColor


class TrackResizeHandle(QGraphicsObject):
    """A thin grippable bar at the bottom of a track header for height resizing."""

    height_preview = pyqtSignal(int)    # new height during drag (clamped)
    height_committed = pyqtSignal(int)  # final new height on release (clamped)

    HANDLE_THICKNESS = 5

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

    def boundingRect(self):
        return QRectF(0, 0, self._width, self.HANDLE_THICKNESS)

    def paint(self, painter, option, widget):
        color = QColor(120, 120, 120) if (self._hover or self._is_dragging) else QColor(70, 70, 70)
        painter.fillRect(self.boundingRect(), color)

    def _set_view_cursor(self, cursor):
        if self.scene() and self.scene().views():
            for view in self.scene().views():
                view.setCursor(cursor)

    def hoverEnterEvent(self, event):
        self._hover = True
        self._set_view_cursor(Qt.CursorShape.SizeVerCursor)
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        if not self._is_dragging:
            self._set_view_cursor(Qt.CursorShape.ArrowCursor)
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_dragging = True
            self._press_scene_y = event.scenePos().y()
            self._press_height = int(self._get_current_height())
            self._set_view_cursor(Qt.CursorShape.SizeVerCursor)
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
            self._set_view_cursor(Qt.CursorShape.ArrowCursor if not self._hover
                                  else Qt.CursorShape.SizeVerCursor)
            self.update()
            self.height_committed.emit(new_height)
            event.accept()
            return
        super().mouseReleaseEvent(event)
