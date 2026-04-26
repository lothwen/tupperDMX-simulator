"""
MIDI Events track for timeline.

Renders each MIDI event as a draggable, resizable clip — like clips in a
classical video editor / Audacity. Clips never overlap; gaps between
non-BLACKOUT clips are represented by transparent BLACKOUT clips.

Three drag modes per clip:
  * MOVE       — grab the body, slide horizontally (preserves width).
  * RESIZE_LEFT — grab the left edge zone, change start time only.
  * RESIZE_RIGHT — grab the right edge zone, change end time only.

All gestures clamp to neighbor bounds and snap to adjacent clip edges.
Persistence happens on mouseRelease via the *_committed signals;
mid-drag is purely visual.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from PyQt6.QtWidgets import (QGraphicsRectItem, QGraphicsTextItem, QGraphicsObject,
                             QGraphicsItem, QMenu)
from PyQt6.QtGui import QColor, QBrush, QPen, QLinearGradient, QPainter
from PyQt6.QtCore import pyqtSignal, QRectF, Qt

from .base import BaseTrack
from ..widgets.tooltip import CustomTooltip
from tupperlogger import get_logger


# Edge-zone widths (pixels in scene space) for resize gestures.
RESIZE_ZONE_PX = 8
# Snap radius — if a dragged edge ends within this many scene-pixels of a
# neighboring clip edge, snap to it.
SNAP_PX = 6
# Minimum visible width for a clip's hit area (so sub-pixel clips remain
# clickable until the user zooms in).
MIN_HIT_WIDTH_PX = 1
# Visual styling for clip boxes.
CLIP_CORNER_RADIUS_PX = 4
CLIP_GRADIENT_LIGHTEN = 118   # top of gradient = base.lighter(LIGHTEN)
CLIP_GRADIENT_DARKEN = 140    # bottom of gradient = base.darker(DARKEN)


@dataclass
class ClipBounds:
    """Pixel-space bounds and snap targets for a single clip's drag gestures.

    All values are in scene pixel coordinates (i.e. `time_seconds *
    PIXELS_PER_SECOND`). The fields encode the policy of "no overlap",
    "BLACKOUTs absorb moves", and "preserve target width during MOVE".
    """
    # MOVE mode: list of (min_x, max_x) intervals where the clip's LEFT edge
    # may land. Multiple intervals enable "jumping" over other clips when a
    # large enough slot exists on the far side.
    move_intervals: List[Tuple[float, float]] = field(default_factory=list)
    # RESIZE_LEFT mode: bounds on the clip's LEFT edge (start time).
    resize_left_min_x: float = 0.0
    resize_left_max_x: float = 0.0
    # RESIZE_RIGHT mode: bounds on the clip's RIGHT edge (end time).
    resize_right_min_x: float = 0.0
    resize_right_max_x: float = 0.0
    # Snap targets — neighbor edges to which a dragged edge will snap.
    snap_xs: List[float] = field(default_factory=list)


class DraggableMidiEvent(QGraphicsObject):
    """A MIDI clip box: movable, resizable, snap-aware. No overlap."""

    # Signals — committed on mouseRelease only.
    move_committed = pyqtSignal(str, float)         # event_id, new_time
    left_edge_committed = pyqtSignal(str, float)    # event_id, new_time
    right_edge_committed = pyqtSignal(str, float)   # event_id, new_right_time
    edit_requested = pyqtSignal(str)                # event_id
    delete_requested = pyqtSignal(str)              # event_id
    add_at_position_requested = pyqtSignal(float)   # time_seconds

    # Drag-mode enum (string-based for readability).
    MODE_IDLE = "idle"
    MODE_MOVE = "move"
    MODE_RESIZE_LEFT = "resize_left"
    MODE_RESIZE_RIGHT = "resize_right"

    def __init__(self, x, y, width, height, event_id: str, event_data: dict,
                 bounds: ClipBounds, is_blackout: bool = False, parent=None):
        super().__init__(parent)
        self.rect = QRectF(0, 0, max(width, MIN_HIT_WIDTH_PX), height)
        self.setPos(x, y)

        self.event_id = event_id
        self.event_data = event_data
        self.bounds = bounds
        self.is_blackout_clip = is_blackout

        # Qt item flags
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)

        # Drag state
        self._mode = self.MODE_IDLE
        self._press_scene_pos = None
        self._press_self_pos = None         # self.pos() at press time
        self._press_width = float(width)    # rect width at press time

        # External hooks
        self.timeline = None
        self.text_item = None
        self.last_right_click_scene_pos = None

        # Painting state (overridden by setBrush/setPen unless BLACKOUT)
        self._brush = QBrush(QColor(100, 100, 100))
        self._pen = QPen(QColor(20, 20, 20))

        # Tooltip
        self._tooltip_shown = False
        self._custom_tooltip = None

        self.logger = get_logger("timeline.tracks.midi_events.draggable")

    # --- Geometry / paint -------------------------------------------------

    def boundingRect(self):
        return QRectF(0, 0, self.rect.width(), self.rect.height())

    def paint(self, painter, option, widget):
        if self.is_blackout_clip:
            # BLACKOUT clips are visual gaps. Paint nothing — but the bounding
            # rect still catches mouse events so the user can drag/right-click
            # the empty space to manage the BLACKOUT.
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.boundingRect()
        # Build a subtle vertical gradient from the base color so the clip
        # reads with a bit of depth instead of a flat fill.
        base_color = self._brush.color()
        gradient = QLinearGradient(0, 0, 0, rect.height())
        gradient.setColorAt(0.0, base_color.lighter(CLIP_GRADIENT_LIGHTEN))
        gradient.setColorAt(1.0, base_color.darker(CLIP_GRADIENT_DARKEN))
        painter.setBrush(QBrush(gradient))
        painter.setPen(self._pen)
        painter.drawRoundedRect(rect, CLIP_CORNER_RADIUS_PX, CLIP_CORNER_RADIUS_PX)

    def setBrush(self, brush):
        self._brush = brush
        self.update()

    def setPen(self, pen):
        self._pen = pen
        self.update()

    def setRect(self, rect):
        self.prepareGeometryChange()
        self.rect = rect
        self.update()

    def set_timeline(self, timeline):
        self.timeline = timeline

    def set_text_item(self, text_item):
        self.text_item = text_item

    # --- Cursor / hit-test ------------------------------------------------

    def _zone_at(self, local_x: float) -> str:
        """Classify a local x position into a drag zone."""
        w = self.rect.width()
        if w <= 2 * RESIZE_ZONE_PX:
            # Narrow clip: no MOVE zone; split half/half between resize zones.
            return self.MODE_RESIZE_LEFT if local_x < w / 2 else self.MODE_RESIZE_RIGHT
        if local_x <= RESIZE_ZONE_PX:
            return self.MODE_RESIZE_LEFT
        if local_x >= w - RESIZE_ZONE_PX:
            return self.MODE_RESIZE_RIGHT
        return self.MODE_MOVE

    def _set_view_cursor(self, cursor_shape):
        if self.scene() and self.scene().views():
            for view in self.scene().views():
                view.setCursor(cursor_shape)

    # --- Tooltip ----------------------------------------------------------

    def _show_persistent_tooltip(self, local_pos):
        if not self.event_data or self.is_blackout_clip:
            # No tooltip on BLACKOUTs — they're just gaps.
            return
        if not self._custom_tooltip:
            if self.scene() and self.scene().views():
                main_window = self.scene().views()[0].window()
                self._custom_tooltip = CustomTooltip(main_window)
        if not self._custom_tooltip:
            return

        event_info = self.event_data.get('info', {})
        part_name = getattr(event_info, 'part', f'Note {self.event_data["note"]}')
        velocity = self.event_data.get('velocity', 0)
        time = self.event_data.get('time', 0.0)
        note = self.event_data.get('note', 0)

        if self.timeline:
            seconds_per_beat = 60.0 / self.timeline.bpm
            beat_number = time / seconds_per_beat
            bar_number = int(beat_number / 4) + 1
            beat_in_bar = int(beat_number % 4) + 1
            tooltip_text = (f"{part_name}\n"
                            f"MIDI Note: {note}\n"
                            f"Time: {time:.3f}s\n"
                            f"Bar: {bar_number}, Beat: {beat_in_bar}\n"
                            f"Velocity: {velocity}")
        else:
            tooltip_text = (f"{part_name}\n"
                            f"MIDI Note: {note}\n"
                            f"Time: {time:.3f}s\n"
                            f"Velocity: {velocity}")

        scene_pos = self.mapToScene(local_pos)
        if self.scene() and self.scene().views():
            for view in self.scene().views():
                global_pos = view.mapToGlobal(view.mapFromScene(scene_pos))
                global_pos.setY(global_pos.y() + 20)
                self._custom_tooltip.show_at(global_pos, tooltip_text)
                break

    # --- Mouse events -----------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            zone = self._zone_at(event.pos().x())
            if self._custom_tooltip:
                self._custom_tooltip.hide_tooltip()
            self._tooltip_shown = False

            self._mode = zone
            self._press_scene_pos = event.scenePos()
            self._press_self_pos = self.pos()
            self._press_width = self.rect.width()
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton:
            self.last_right_click_scene_pos = event.scenePos()
            self.show_context_menu(event.screenPos())
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.is_blackout_clip:
            self.edit_requested.emit(self.event_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if self._mode == self.MODE_IDLE or self._press_scene_pos is None:
            super().mouseMoveEvent(event)
            return

        delta_x = event.scenePos().x() - self._press_scene_pos.x()

        if self._mode == self.MODE_MOVE:
            self._apply_move_preview(delta_x)
        elif self._mode == self.MODE_RESIZE_LEFT:
            self._apply_resize_left_preview(delta_x)
        elif self._mode == self.MODE_RESIZE_RIGHT:
            self._apply_resize_right_preview(delta_x)
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._mode == self.MODE_IDLE or not self.timeline:
            super().mouseReleaseEvent(event)
            return

        pps = self.timeline.PIXELS_PER_SECOND
        if self._mode == self.MODE_MOVE:
            new_time = self.pos().x() / pps
            self.move_committed.emit(self.event_id, new_time)
        elif self._mode == self.MODE_RESIZE_LEFT:
            new_time = self.pos().x() / pps
            self.left_edge_committed.emit(self.event_id, new_time)
        elif self._mode == self.MODE_RESIZE_RIGHT:
            new_right_x = self.pos().x() + self.rect.width()
            self.right_edge_committed.emit(self.event_id, new_right_x / pps)

        self._mode = self.MODE_IDLE
        self._press_scene_pos = None
        self._press_self_pos = None
        super().mouseReleaseEvent(event)

    # --- Drag previews ----------------------------------------------------
    # Each preview computes the proposed new position/width, clamps to bounds,
    # snaps to nearby targets, and updates the visual state. No data mutation.

    def _snap(self, x: float) -> float:
        nearest = None
        for tx in self.bounds.snap_xs:
            d = abs(tx - x)
            if d <= SNAP_PX and (nearest is None or d < abs(nearest - x)):
                nearest = tx
        return nearest if nearest is not None else x

    def _apply_move_preview(self, delta_x: float):
        proposed = self._press_self_pos.x() + delta_x
        # Find the clamped position closest to `proposed` across all valid
        # MOVE intervals. Multiple intervals enable jumping over a neighbor
        # clip when a slot on the far side can fit this clip.
        best_clamped = None
        best_interval = None
        best_distance = float('inf')
        for lo, hi in self.bounds.move_intervals:
            clamped = max(lo, min(proposed, hi))
            d = abs(clamped - proposed)
            if d < best_distance:
                best_distance = d
                best_clamped = clamped
                best_interval = (lo, hi)
        if best_clamped is None:
            return  # no slot fits; nothing to preview
        # Snap, then re-clamp into the chosen interval so a snap target
        # outside this slot's valid range can't push us into overlap.
        snapped = self._snap(best_clamped)
        lo, hi = best_interval
        snapped = max(lo, min(snapped, hi))
        self.setPos(snapped, self.pos().y())
        self._reposition_text(snapped)

    def _apply_resize_left_preview(self, delta_x: float):
        # The right edge is fixed; left edge moves.
        original_right = self._press_self_pos.x() + self._press_width
        new_x = self._press_self_pos.x() + delta_x
        new_x = max(self.bounds.resize_left_min_x,
                    min(new_x, self.bounds.resize_left_max_x))
        new_x = self._snap(new_x)
        new_width = max(MIN_HIT_WIDTH_PX, original_right - new_x)
        self.setPos(new_x, self.pos().y())
        self.setRect(QRectF(0, 0, new_width, self.rect.height()))
        self._reposition_text(new_x)
        self._refresh_text_truncation(new_width)

    def _apply_resize_right_preview(self, delta_x: float):
        # The left edge is fixed; right edge moves.
        original_left = self._press_self_pos.x()
        new_right = original_left + self._press_width + delta_x
        new_right = max(self.bounds.resize_right_min_x,
                        min(new_right, self.bounds.resize_right_max_x))
        new_right = self._snap(new_right)
        new_width = max(MIN_HIT_WIDTH_PX, new_right - original_left)
        self.setRect(QRectF(0, 0, new_width, self.rect.height()))
        self._refresh_text_truncation(new_width)

    def _reposition_text(self, new_x: float):
        if self.text_item is None:
            return
        self.text_item.setPos(new_x + 5, self.text_item.pos().y())

    def _refresh_text_truncation(self, new_width: float):
        if self.text_item is None or self.timeline is None:
            return
        zoom = self.timeline.content_view.transform().m11()
        available = (new_width - 10) * zoom
        full_text = self.text_item.data(0)
        if not full_text:
            return
        fm = self.timeline.content_view.fontMetrics()
        self.text_item.setPlainText(self.timeline.get_truncated_text(full_text, available, fm))

    # --- Hover ------------------------------------------------------------

    def hoverEnterEvent(self, event):
        if self.event_data and not self.is_blackout_clip:
            self._show_persistent_tooltip(event.pos())
            self._tooltip_shown = True
        super().hoverEnterEvent(event)

    def hoverMoveEvent(self, event):
        zone = self._zone_at(event.pos().x())
        if zone in (self.MODE_RESIZE_LEFT, self.MODE_RESIZE_RIGHT):
            self._set_view_cursor(Qt.CursorShape.SizeHorCursor)
        else:
            self._set_view_cursor(Qt.CursorShape.SizeAllCursor)
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        if self._custom_tooltip:
            self._custom_tooltip.hide_tooltip()
        self._tooltip_shown = False
        self._set_view_cursor(Qt.CursorShape.ArrowCursor)
        super().hoverLeaveEvent(event)

    # --- Context menu -----------------------------------------------------

    def show_context_menu(self, global_pos):
        main_window = self.timeline.parent() if self.timeline else None
        while main_window and not hasattr(main_window, 'centralWidget'):
            main_window = main_window.parent()
        menu = QMenu(main_window)

        if not self.is_blackout_clip:
            edit_action = menu.addAction("Edit Properties...")
            edit_action.triggered.connect(lambda: self.edit_requested.emit(self.event_id))
            menu.addSeparator()

        add_action = menu.addAction("Add event at this position")
        if self.last_right_click_scene_pos and self.timeline:
            click_time = max(0.0, self.last_right_click_scene_pos.x() / self.timeline.PIXELS_PER_SECOND)
            add_action.triggered.connect(lambda: self.add_at_position_requested.emit(click_time))
        else:
            event_time = self.event_data['time']
            add_action.triggered.connect(lambda: self.add_at_position_requested.emit(event_time))

        menu.addSeparator()
        delete_label = "Remove blackout" if self.is_blackout_clip else "Delete Event"
        delete_action = menu.addAction(delete_label)
        delete_action.triggered.connect(lambda: self.delete_requested.emit(self.event_id))

        menu.exec(global_pos)


class MidiEventsTrack(BaseTrack):
    """Track for displaying and managing MIDI events."""

    def __init__(self, timeline_manager, content_scene, header_scene):
        super().__init__(timeline_manager, content_scene, header_scene)
        self.logger = get_logger("timeline.tracks.midi_events")

    def get_track_height(self) -> int:
        return self.timeline_manager.midi_events_track_height

    def draw_header(self, y_offset: int):
        for item in self.header_items:
            if item.scene():
                item.scene().removeItem(item)
        self.header_items.clear()

        header_rect = QGraphicsRectItem(
            0, y_offset,
            self.timeline_manager.PIANO_WIDTH,
            self.get_track_height()
        )
        header_rect.setBrush(QBrush(QColor(50, 50, 50)))
        header_rect.setPen(QPen(QColor(20, 20, 20)))
        self.header_scene.addItem(header_rect)
        self.header_items.append(header_rect)

        header_text = QGraphicsTextItem("MIDI Events")
        header_text.setDefaultTextColor(QColor(200, 200, 200))
        header_text.setPos(5, y_offset + 5)
        self.header_scene.addItem(header_text)
        self.header_items.append(header_text)

    # --- Bounds computation ----------------------------------------------

    def _is_blackout_event(self, event) -> bool:
        info = event.get('info') if event else None
        part = getattr(info, 'part', '') if info else ''
        return (part or '').upper() == "BLACKOUT"

    def _compute_bounds(self, events, idx: int, song_end_time: float) -> ClipBounds:
        """Return pixel-space bounds for clip at index `idx` of the sorted list."""
        pps = self.timeline_manager.PIXELS_PER_SECOND
        from midi.manager import MIN_EVENT_DURATION

        cur = events[idx]
        cur_time = cur['time']
        cur_right = (events[idx + 1]['time'] if idx + 1 < len(events) else song_end_time)
        cur_width_t = max(MIN_EVENT_DURATION, cur_right - cur_time)

        # MOVE bounds: collect every slot of free space (visible space NOT
        # occupied by another non-BLACKOUT clip) wide enough to fit cur_width_t.
        # This enables jumping over neighbors when there's room on the far side.
        obstacles: List[Tuple[float, float]] = []
        for j, e in enumerate(events):
            if j == idx or self._is_blackout_event(e):
                continue
            j_end = (events[j + 1]['time'] if j + 1 < len(events) else song_end_time)
            obstacles.append((e['time'], j_end))
        obstacles.sort()

        slots: List[Tuple[float, float]] = []
        cursor = 0.0
        for ostart, oend in obstacles:
            if ostart > cursor:
                slots.append((cursor, ostart))
            cursor = max(cursor, oend)
        if cursor < song_end_time:
            slots.append((cursor, song_end_time))

        move_intervals: List[Tuple[float, float]] = []
        for slot_start, slot_end in slots:
            if slot_end - slot_start + 1e-6 < cur_width_t:
                continue
            # Valid range for the LEFT edge inside this slot.
            move_intervals.append((slot_start * pps,
                                   (slot_end - cur_width_t) * pps))

        # RESIZE_LEFT bounds (only B.time changes).
        # min: just past the closest non-BLACKOUT predecessor (consume any BO).
        resize_left_min_t = 0.0
        for j in range(idx - 1, -1, -1):
            if not self._is_blackout_event(events[j]):
                resize_left_min_t = events[j]['time'] + MIN_EVENT_DURATION
                break
        resize_left_max_t = cur_right - MIN_EVENT_DURATION

        # RESIZE_RIGHT bounds (B's right edge changes).
        resize_right_min_t = cur_time + MIN_EVENT_DURATION
        resize_right_max_t = song_end_time
        for j in range(idx + 1, len(events)):
            if not self._is_blackout_event(events[j]):
                resize_right_max_t = events[j]['time']
                break

        # Snap targets: every other clip's edges (start & end) in pixel-space.
        snap_xs: List[float] = []
        for j, e in enumerate(events):
            if j == idx:
                continue
            snap_xs.append(e['time'] * pps)
            j_right = (events[j + 1]['time'] if j + 1 < len(events) else song_end_time)
            snap_xs.append(j_right * pps)
        snap_xs.extend([0.0, song_end_time * pps])

        return ClipBounds(
            move_intervals=move_intervals,
            resize_left_min_x=resize_left_min_t * pps,
            resize_left_max_x=resize_left_max_t * pps,
            resize_right_min_x=resize_right_min_t * pps,
            resize_right_max_x=resize_right_max_t * pps,
            snap_xs=snap_xs,
        )

    def redraw_events(self, cached_events, y_offset: int):
        # Clear existing content items
        for item in self.items:
            if item.scene():
                item.scene().removeItem(item)
        self.items.clear()

        if not cached_events:
            return

        try:
            song_length_seconds = (self.timeline_manager.midi_manager.get_duration()
                                   if self.timeline_manager.midi_manager else 240.0)
            song_end_time = song_length_seconds * 1.05
            pps = self.timeline_manager.PIXELS_PER_SECOND
            track_height = self.get_track_height()

            # Pre-sort defensively (manager already sorts; cheap insurance).
            events = list(cached_events)

            for i, event in enumerate(events):
                start_time = event['time']
                if i < len(events) - 1:
                    end_time = events[i + 1]['time']
                else:
                    end_time = song_end_time
                duration = max(0.0, end_time - start_time)

                x_pos = start_time * pps
                width = max(duration * pps, MIN_HIT_WIDTH_PX)
                is_blackout = self._is_blackout_event(event)
                bounds = self._compute_bounds(events, i, song_end_time)

                event_rect = DraggableMidiEvent(
                    x_pos, y_offset, width, track_height,
                    event_id=event['id'], event_data=event,
                    bounds=bounds, is_blackout=is_blackout,
                )
                event_rect.set_timeline(self.timeline_manager)

                # Connect new signals (event_id-based).
                event_rect.move_committed.connect(self.timeline_manager.on_midi_event_moved)
                event_rect.left_edge_committed.connect(self.timeline_manager.on_midi_event_left_edge)
                event_rect.right_edge_committed.connect(self.timeline_manager.on_midi_event_right_edge)
                event_rect.edit_requested.connect(self.timeline_manager.on_midi_event_edit_by_id)
                event_rect.delete_requested.connect(self.timeline_manager.on_midi_event_delete_by_id)
                event_rect.add_at_position_requested.connect(self.timeline_manager.on_add_event_requested)

                if is_blackout:
                    event_rect.setBrush(QBrush(Qt.GlobalColor.transparent))
                    event_rect.setPen(QPen(Qt.PenStyle.NoPen))
                else:
                    info = event['info']
                    color = QColor(info.color)
                    event_rect.setBrush(QBrush(color))
                    event_rect.setPen(QPen(QColor(20, 20, 20)))
                event_rect.setZValue(1)
                self.content_scene.addItem(event_rect)
                self.items.append(event_rect)

                if not is_blackout:
                    info = event['info']
                    color = QColor(info.color)
                    brightness = (color.red() * 0.299 + color.green() * 0.587 + color.blue() * 0.114)
                    text_color = QColor(255, 255, 255) if brightness < 128 else QColor(0, 0, 0)
                    zoom_factor = self.timeline_manager.content_view.transform().m11()
                    available_width = (width - 10) * zoom_factor
                    fm = self.timeline_manager.content_view.fontMetrics()
                    text_to_display = self.timeline_manager.get_truncated_text(info.part, available_width, fm)

                    text_item = QGraphicsTextItem(text_to_display)
                    text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
                    text_item.setDefaultTextColor(text_color)
                    text_item.setPos(x_pos + 5, y_offset + 5)
                    text_item.setZValue(2)
                    text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
                    text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, False)
                    text_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                    text_item.setAcceptHoverEvents(False)
                    text_item.setData(0, info.part)
                    text_item.setData(1, event['id'])

                    event_rect.set_text_item(text_item)

                    self.content_scene.addItem(text_item)
                    self.items.append(text_item)

        except Exception as e:
            self.logger.error(f"Error redrawing midi events: {e}")

    def redraw(self):
        """Redraw the MIDI events track."""
        pass
