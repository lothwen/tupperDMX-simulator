"""
Song-structure section boxes overlaid on the waveform track.

A "song part" (INTRO/VERSE/CHORUS/… — MIDI notes 61-69) is a global verb that
defines the song's arrangement. Instead of rendering on every fixture lane, the
parts are drawn here as translucent, contiguous boxes on top of the waveform:
each box spans from its marker to the next (the last to the song end).

`SongSectionItem` is a deliberately simple draggable: horizontal move only (no
resize / fades / grouping), beat-snapped, committing on mouse release. It reuses
the same commit-on-release contract the MIDI clips use, but routes to dedicated
manager handlers because a song part is a point-in-time global marker, not a
fixture-lane clip with BLACKOUT gap machinery.
"""

from typing import Optional

from PyQt6.QtCore import Qt, QRectF, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPen, QPainter
from PyQt6.QtWidgets import (QGraphicsObject, QGraphicsItem, QGraphicsTextItem,
                             QMenu)

# Screen-space snap radius (matches the MIDI clips' SNAP_PX feel).
SNAP_PX = 6
MIN_BOX_WIDTH_PX = 2
SECTION_FILL_ALPHA = 90


class SongSectionItem(QGraphicsObject):
    """A translucent song-section box on the waveform overlay. Movable
    horizontally; double-click edits, context menu / Delete removes."""

    # Committed on mouseRelease only (mid-drag is purely visual).
    move_committed = pyqtSignal(str, float)   # event_id, new_time
    edit_requested = pyqtSignal(str)          # event_id
    delete_requested = pyqtSignal(str)        # event_id

    def __init__(self, x, y, width, height, event_id: str, part: str,
                 color, parent=None):
        super().__init__(parent)
        self.rect = QRectF(0, 0, max(width, MIN_BOX_WIDTH_PX), height)
        self.setPos(x, y)

        self.event_id = event_id
        self.part = part
        self._color = QColor(color)

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(
            Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)

        # External hooks (set by the track after construction).
        self.timeline = None
        self.text_item: Optional[QGraphicsTextItem] = None

        # Drag state.
        self._press_scene_x = None
        self._press_self_x = None
        self._dragging = False

    # --- Geometry / paint -------------------------------------------------

    def boundingRect(self):
        return QRectF(0, 0, self.rect.width(), self.rect.height())

    def paint(self, painter, option, widget):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        fill = QColor(self._color)
        fill.setAlpha(SECTION_FILL_ALPHA)
        painter.setBrush(QBrush(fill))
        if self.isSelected():
            pen = QPen(QColor(255, 255, 255))
            pen.setWidth(2)
        else:
            pen = QPen(QColor(20, 20, 20))
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawRect(self.boundingRect())

    def set_timeline(self, timeline):
        self.timeline = timeline

    # --- Snapping ---------------------------------------------------------

    def _zoom_x(self) -> float:
        if self.timeline is None:
            return 1.0
        m11 = self.timeline.content_view.transform().m11()
        return m11 if m11 > 0 else 1.0

    def _snap_x(self, x: float) -> float:
        """Clamp to >=0 and snap to the nearest beat line within SNAP_PX."""
        x = max(0.0, x)
        if self.timeline is None:
            return x
        bpm = getattr(self.timeline, "bpm", 0)
        pps = self.timeline.PIXELS_PER_SECOND
        if bpm <= 0 or pps <= 0:
            return x
        beat_px = (60.0 / bpm) * pps
        if beat_px <= 0:
            return x
        nearest = round(x / beat_px) * beat_px
        if abs(nearest - x) <= SNAP_PX / self._zoom_x():
            return max(0.0, nearest)
        return x

    def _reposition_text(self, new_x: float):
        """Keep the label horizontally centered in the box as it drags (the
        label ignores view transforms, so its width is in device px)."""
        if self.text_item is not None:
            tw = self.text_item.boundingRect().width()
            centered_x = new_x + self.rect.width() / 2 - (tw / self._zoom_x()) / 2
            self.text_item.setPos(centered_x, self.text_item.pos().y())

    # --- Mouse ------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSelected(True)
            self._press_scene_x = event.scenePos().x()
            self._press_self_x = self.pos().x()
            self._dragging = False
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.setSelected(True)
            self._show_context_menu(event.screenPos())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_scene_x is None:
            super().mouseMoveEvent(event)
            return
        delta_x = event.scenePos().x() - self._press_scene_x
        if abs(delta_x) >= 1.0:
            self._dragging = True
        new_x = self._snap_x(self._press_self_x + delta_x)
        self.setPos(new_x, self.pos().y())
        self._reposition_text(new_x)
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._dragging and self.timeline is not None:
            pps = self.timeline.PIXELS_PER_SECOND
            self.move_committed.emit(self.event_id, self.pos().x() / pps)
        self._press_scene_x = None
        self._press_self_x = None
        self._dragging = False
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.edit_requested.emit(self.event_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_requested.emit(self.event_id)
            event.accept()
            return
        super().keyPressEvent(event)

    def hoverEnterEvent(self, event):
        if self.timeline is not None:
            # Set the cursor on the view (not the viewport) to match the MIDI
            # clips / fade handles. Touching viewport().setCursor() would give
            # the viewport its own cursor and stop it inheriting the view's,
            # silently killing the clips' resize/move cursors afterwards.
            self.timeline.content_view.setCursor(Qt.CursorShape.SizeHorCursor)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        if self.timeline is not None:
            self.timeline.content_view.setCursor(Qt.CursorShape.ArrowCursor)
        super().hoverLeaveEvent(event)

    def _show_context_menu(self, global_pos):
        menu = QMenu()
        edit_action = menu.addAction(f"Edit '{self.part}'…")
        delete_action = menu.addAction(f"Delete '{self.part}'")
        chosen = menu.exec(global_pos)
        if chosen is edit_action:
            self.edit_requested.emit(self.event_id)
        elif chosen is delete_action:
            self.delete_requested.emit(self.event_id)
