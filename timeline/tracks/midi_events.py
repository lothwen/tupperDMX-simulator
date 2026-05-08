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
from PyQt6.QtGui import QColor, QBrush, QPen, QLinearGradient, QPainter, QPolygonF
from PyQt6.QtCore import pyqtSignal, QRectF, Qt, QPointF

from .base import BaseTrack
from ..widgets.tooltip import CustomTooltip
from tupperlogger import get_logger


def build_render_units(events):
    """Collapse same-fixture events into render units (singles or groups).

    A "render unit" is the visual atom on the timeline track: either a
    single ungrouped event or a scene group whose members share a
    `group_id`. Returns a list sorted by time. Pure function — used by
    MidiEventsTrack.redraw_events but also unit-tested in isolation.
    """
    if not events:
        return []
    sorted_events = sorted(events, key=lambda e: e["time"])

    units = []
    by_group: dict = {}
    for e in sorted_events:
        gid = e.get("group_id")
        if gid:
            by_group.setdefault(gid, []).append(e)
        else:
            units.append({
                "kind": "single",
                "time": e["time"],
                "event_ids": [e["id"]],
                "representative": e,
                "group_id": None,
            })

    for gid, members in by_group.items():
        # Defensive: if a group has only 1 member (corrupt / partial data),
        # still emit it but flagged as a degenerate single so callers don't
        # have to handle that edge case.
        kind = "group" if len(members) >= 2 else "single"
        units.append({
            "kind": kind,
            "time": members[0]["time"],
            "event_ids": [m["id"] for m in members],
            "representative": members[0],
            "group_id": gid if kind == "group" else None,
        })

    units.sort(key=lambda u: u["time"])
    return units


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
# Selection-border styling.
SELECTION_BORDER_COLOR = QColor(255, 215, 0)  # warm gold — visible on any clip color
SELECTION_BORDER_WIDTH_PX = 2

# Fade-handle styling. The triangle marker sits at the top edge of the clip.
# Its right-angle vertex marks the fade endpoint along the time axis; the
# legs extend INWARD into the clip body so the marker stays visible at the
# zero-fade default position (clip corner) and never paints outside the clip.
FADE_HANDLE_SIZE_PX = 10
FADE_HANDLE_COLOR = QColor(255, 215, 0)        # match the selection-border gold
FADE_LINE_COLOR = QColor(255, 215, 0, 220)
FADE_LINE_WIDTH_PX = 2


class FadeHandle(QGraphicsObject):
    """Triangle handle on a clip's top edge, draggable along the time axis.

    `side` is "in" (left handle, marks fade-IN end) or "out" (right handle,
    marks fade-OUT start). The handle's local origin (0, 0) sits at its
    fade endpoint along the clip's top edge; the triangle is drawn inward
    so the marker is always visible inside the clip body, including at
    zero-fade where it tucks into the corner.

    Drag commits go to `fade_committed(side, new_seconds)`. During a drag
    the parent clip's `preview_fade(side, seconds)` is called continuously
    so the diagonal fade line repaints in real time — the same "preview vs
    commit" pattern used by clip move/resize.
    """

    fade_committed = pyqtSignal(str, float)  # side ("in"/"out"), seconds

    def __init__(self, side: str, parent_clip):
        super().__init__(parent_clip)
        self.side = side
        self._parent_clip = parent_clip
        self._dragging = False
        self._press_scene_x = 0.0
        self._press_self_x = 0.0
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        # Sit above the clip body but below text labels so handles stay
        # grabbable on top of the clip without occluding the part name.
        self.setZValue(3)

    def _triangle(self) -> QPolygonF:
        s = FADE_HANDLE_SIZE_PX
        if self.side == "in":
            # Right-angle at (0, 0); legs go RIGHT and DOWN into the clip.
            return QPolygonF([QPointF(0, 0), QPointF(s, 0), QPointF(0, s)])
        # side == "out": legs go LEFT and DOWN.
        return QPolygonF([QPointF(0, 0), QPointF(-s, 0), QPointF(0, s)])

    def boundingRect(self) -> QRectF:
        s = FADE_HANDLE_SIZE_PX
        if self.side == "in":
            return QRectF(0, 0, s, s)
        return QRectF(-s, 0, s, s)

    def paint(self, painter, option, widget):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QBrush(FADE_HANDLE_COLOR))
        fade_handle_pen = QPen(QColor(20, 20, 20))
        fade_handle_pen.setCosmetic(True)
        painter.setPen(fade_handle_pen)
        painter.drawPolygon(self._triangle())

    def hoverEnterEvent(self, event):
        # Hover never leaves the parent clip when the cursor moves onto a
        # child handle, so the parent's tooltip would otherwise stay frozen
        # under the drag. Hiding it here mirrors what the parent does on
        # its own hoverLeave / mousePress.
        self._parent_clip.hide_tooltip()
        if self.scene() and self.scene().views():
            for view in self.scene().views():
                view.setCursor(Qt.CursorShape.SizeHorCursor)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        if self.scene() and self.scene().views():
            for view in self.scene().views():
                view.setCursor(Qt.CursorShape.ArrowCursor)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        # Belt-and-braces: also hide on press in case hoverEnter never
        # fired (e.g. cursor was outside the handle's bounding rect when
        # the press began due to a fast pointer move).
        self._parent_clip.hide_tooltip()
        self._dragging = True
        self._press_scene_x = event.scenePos().x()
        self._press_self_x = self.pos().x()
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._dragging:
            super().mouseMoveEvent(event)
            return
        delta = event.scenePos().x() - self._press_scene_x
        proposed_x = self._press_self_x + delta
        clamped_x = self._parent_clip.clamp_fade_handle_x(self.side, proposed_x)
        self.setPos(clamped_x, 0.0)
        # Live preview — parent recomputes the diagonal fade line.
        seconds = self._parent_clip.handle_x_to_fade_seconds(self.side, clamped_x)
        self._parent_clip.preview_fade(self.side, seconds)
        event.accept()

    def mouseReleaseEvent(self, event):
        if not self._dragging:
            super().mouseReleaseEvent(event)
            return
        self._dragging = False
        seconds = self._parent_clip.handle_x_to_fade_seconds(self.side, self.pos().x())
        # Only commit if there was meaningful motion. A pure click should
        # not trigger a redraw — same convention as the clip-body drags.
        if abs(event.scenePos().x() - self._press_scene_x) >= 1.0:
            self.fade_committed.emit(self.side, seconds)
        event.accept()


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
    fade_in_committed = pyqtSignal(str, float)      # event_id, fade_in seconds
    fade_out_committed = pyqtSignal(str, float)     # event_id, fade_out seconds
    edit_requested = pyqtSignal(str)                # event_id
    delete_requested = pyqtSignal(str)              # event_id
    add_at_position_requested = pyqtSignal(float)   # time_seconds

    # Drag-mode enum (string-based for readability).
    MODE_IDLE = "idle"
    MODE_MOVE = "move"
    MODE_RESIZE_LEFT = "resize_left"
    MODE_RESIZE_RIGHT = "resize_right"

    def __init__(self, x, y, width, height, event_id: str, event_data: dict,
                 bounds: ClipBounds, is_blackout: bool = False,
                 group_id: Optional[str] = None,
                 group_event_ids: Optional[List[str]] = None,
                 parent=None):
        super().__init__(parent)
        self.rect = QRectF(0, 0, max(width, MIN_HIT_WIDTH_PX), height)
        self.setPos(x, y)

        self.event_id = event_id
        self.event_data = event_data
        self.bounds = bounds
        self.is_blackout_clip = is_blackout
        # Scene-clip grouping: when set, this DraggableMidiEvent represents a
        # whole scene group; the Timeline routes drag signals to the scene API
        # rather than the single-event API.
        self.group_id = group_id
        self.group_event_ids = list(group_event_ids) if group_event_ids else []

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
        self._pen.setCosmetic(True)

        # Tooltip
        self._tooltip_shown = False
        self._custom_tooltip = None

        # Fade durations (seconds). Sourced from event_data when present;
        # default to 0 so a clip with no fades renders cleanly. The handles
        # use these as their pixel positions and as the live-preview value
        # during a drag.
        self.fade_in_seconds = float(event_data.get("fade_in", 0.0)) if event_data else 0.0
        self.fade_out_seconds = float(event_data.get("fade_out", 0.0)) if event_data else 0.0
        self._fade_in_handle: Optional[FadeHandle] = None
        self._fade_out_handle: Optional[FadeHandle] = None
        if not is_blackout:
            self._fade_in_handle = FadeHandle("in", self)
            self._fade_out_handle = FadeHandle("out", self)
            self._fade_in_handle.fade_committed.connect(self._on_handle_committed)
            self._fade_out_handle.fade_committed.connect(self._on_handle_committed)
            self._reposition_fade_handles()

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
        if self.isSelected():
            sel_pen = QPen(SELECTION_BORDER_COLOR)
            sel_pen.setWidth(SELECTION_BORDER_WIDTH_PX)
            sel_pen.setCosmetic(True)
            painter.setPen(sel_pen)
        else:
            painter.setPen(self._pen)
        painter.drawRoundedRect(rect, CLIP_CORNER_RADIUS_PX, CLIP_CORNER_RADIUS_PX)

        # Diagonal fade lines: from same-side bottom corner up to the fade
        # handle on the top edge. Drawn only when the fade is non-zero so a
        # plain clip stays clean. Uses cosmetic stroke so the line stays
        # FADE_LINE_WIDTH_PX regardless of view zoom.
        fade_pen = QPen(FADE_LINE_COLOR)
        fade_pen.setWidth(FADE_LINE_WIDTH_PX)
        fade_pen.setCosmetic(True)
        if self.fade_in_seconds > 0.0:
            x = self._fade_in_pixels()
            painter.setPen(fade_pen)
            painter.drawLine(QPointF(0, rect.height()), QPointF(x, 0))
        if self.fade_out_seconds > 0.0:
            x = rect.width() - self._fade_out_pixels()
            painter.setPen(fade_pen)
            painter.drawLine(QPointF(rect.width(), rect.height()), QPointF(x, 0))

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.update()
        return super().itemChange(change, value)

    def setBrush(self, brush):
        self._brush = brush
        self.update()

    def setPen(self, pen):
        pen.setCosmetic(True)
        self._pen = pen
        self.update()

    def setRect(self, rect):
        self.prepareGeometryChange()
        self.rect = rect
        self.update()

    def set_timeline(self, timeline):
        self.timeline = timeline
        # Now that PIXELS_PER_SECOND is known, place the fade handles at
        # their real x coords. Without this, they sit at pps=1 fallback
        # positions until the first resize event re-computes them.
        self._reposition_fade_handles()

    def set_text_item(self, text_item):
        self.text_item = text_item

    # --- Fade handles -----------------------------------------------------

    def _pps(self) -> float:
        """PIXELS_PER_SECOND from the timeline; 1.0 fallback when unwired."""
        return self.timeline.PIXELS_PER_SECOND if self.timeline else 1.0

    def _fade_in_pixels(self) -> float:
        return max(0.0, self.fade_in_seconds * self._pps())

    def _fade_out_pixels(self) -> float:
        return max(0.0, self.fade_out_seconds * self._pps())

    def _reposition_fade_handles(self):
        """Place handles at the current fade endpoints along the top edge."""
        if self._fade_in_handle is not None:
            self._fade_in_handle.setPos(self._fade_in_pixels(), 0.0)
        if self._fade_out_handle is not None:
            self._fade_out_handle.setPos(
                self.rect.width() - self._fade_out_pixels(), 0.0)

    def clamp_fade_handle_x(self, side: str, proposed_x: float) -> float:
        """Clamp a fade handle's x to keep fades inside the clip and not crossing.

        Called by FadeHandle during a drag. The opposite handle is treated
        as a hard wall — no cross-fades, per Mr. Lothwen's spec.
        """
        w = self.rect.width()
        if side == "in":
            return max(0.0, min(proposed_x, w - self._fade_out_pixels()))
        return max(self._fade_in_pixels(), min(proposed_x, w))

    def handle_x_to_fade_seconds(self, side: str, x: float) -> float:
        """Convert a clamped handle x position back into fade duration seconds."""
        pps = self._pps()
        if side == "in":
            return max(0.0, x / pps) if pps else 0.0
        # side == "out": fade_out is the distance from clip's right edge.
        return max(0.0, (self.rect.width() - x) / pps) if pps else 0.0

    def preview_fade(self, side: str, seconds: float):
        """Live-update fade lines while a handle is being dragged."""
        if side == "in":
            self.fade_in_seconds = max(0.0, seconds)
        else:
            self.fade_out_seconds = max(0.0, seconds)
        self.update()

    def _on_handle_committed(self, side: str, seconds: float):
        if side == "in":
            self.fade_in_committed.emit(self.event_id, seconds)
        else:
            self.fade_out_committed.emit(self.event_id, seconds)

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

    def _select_exclusive(self):
        """Make this clip the sole selection in the scene.

        BLACKOUT clips are skipped — they're invisible gap markers, not
        user-actionable items.
        """
        if self.is_blackout_clip:
            return
        scene = self.scene()
        if scene is not None:
            for item in scene.selectedItems():
                if item is not self:
                    item.setSelected(False)
        self.setSelected(True)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            zone = self._zone_at(event.pos().x())
            if self._custom_tooltip:
                self._custom_tooltip.hide_tooltip()
            self._tooltip_shown = False

            self._select_exclusive()
            self._mode = zone
            self._press_scene_pos = event.scenePos()
            self._press_self_pos = self.pos()
            self._press_width = self.rect.width()
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton:
            self._select_exclusive()
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

        # A pure click (no drag) must NOT commit — committing triggers a
        # full timeline refresh that destroys this item and wipes the
        # selection the press just established.
        moved = False
        if self._press_scene_pos is not None:
            moved = abs(event.scenePos().x() - self._press_scene_pos.x()) >= 1.0

        if moved:
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
        self._clamp_fades_to_width(new_width)

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
        self._clamp_fades_to_width(new_width)

    def _clamp_fades_to_width(self, new_width: float):
        """Shrink fades visually if a clip resize would push them off-clip.

        Display-only — the manager re-clamps on its own setters and we'll
        also re-clamp on the next data redraw. This just keeps the preview
        line/handle from drifting outside the clip during a live resize.
        """
        pps = self._pps()
        if pps <= 0:
            return
        max_total_px = max(0.0, new_width)
        in_px = self._fade_in_pixels()
        out_px = self._fade_out_pixels()
        if in_px + out_px > max_total_px:
            # Proportionally shrink to fit, preserving the relative balance.
            total = in_px + out_px
            scale = max_total_px / total if total > 0 else 0.0
            self.fade_in_seconds = (in_px * scale) / pps
            self.fade_out_seconds = (out_px * scale) / pps
        self._reposition_fade_handles()
        self.update()

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
        self.hide_tooltip()
        self._set_view_cursor(Qt.CursorShape.ArrowCursor)
        super().hoverLeaveEvent(event)

    def hide_tooltip(self):
        """Dismiss the persistent tooltip, if showing.

        Exposed publicly so child items (fade handles) can dismiss it when
        they take over a gesture — Qt does not auto-fire hoverLeave on this
        item when the cursor moves onto a child, so the tooltip would
        otherwise stay frozen for the duration of the child interaction.
        """
        if self._custom_tooltip:
            self._custom_tooltip.hide_tooltip()
        self._tooltip_shown = False

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

    def __init__(self, timeline_manager, content_scene, header_scene,
                 fixture_filter: Optional[str] = None):
        super().__init__(timeline_manager, content_scene, header_scene)
        self.fixture_filter = fixture_filter
        self.logger = get_logger("timeline.tracks.midi_events")

    def matches_filter(self, event) -> bool:
        if not self.fixture_filter:
            return True
        info = event.get('info') if event else None
        return getattr(info, 'fixture', None) == self.fixture_filter

    def get_track_height(self) -> int:
        return self.timeline_manager.midi_events_track_height

    def draw_header(self, y_offset: int):
        for item in self.header_items:
            if item.scene():
                item.scene().removeItem(item)
        self.header_items.clear()

        header_rect = QGraphicsRectItem(
            0, y_offset,
            self.timeline_manager.HEADER_WIDTH,
            self.get_track_height()
        )
        header_rect.setBrush(QBrush(QColor(50, 50, 50)))
        header_rect.setPen(QPen(QColor(20, 20, 20)))
        self.header_scene.addItem(header_rect)
        self.header_items.append(header_rect)

        title = "MIDI Events"
        if self.fixture_filter:
            title = f"{self.fixture_filter.capitalize()}"
        header_text = QGraphicsTextItem(title)
        header_text.setDefaultTextColor(QColor(200, 200, 200))
        header_text.setPos(5, y_offset + 5)
        self.header_scene.addItem(header_text)
        self.header_items.append(header_text)

    # --- Bounds computation ----------------------------------------------

    def _is_blackout_event(self, event) -> bool:
        # Prefix match so each fixture can have its own gap-marker note
        # (BLACKOUT for ledbar, BLACKOUT_BLINDERS for blinders, etc.).
        info = event.get('info') if event else None
        part = getattr(info, 'part', '') if info else ''
        return (part or '').upper().startswith("BLACKOUT")

    def _is_blackout_unit(self, unit) -> bool:
        """A render unit is a blackout iff its representative event is."""
        return self._is_blackout_event(unit.get("representative"))

    def _compute_bounds(self, units, idx: int, song_end_time: float) -> ClipBounds:
        """Return pixel-space bounds for the unit at index `idx`.

        Operates on render units (single events or scene groups) so that a
        7-event scene clip moves/resizes as a single occupant.
        """
        pps = self.timeline_manager.PIXELS_PER_SECOND
        from midi.manager import MIN_EVENT_DURATION

        cur = units[idx]
        cur_time = cur["time"]
        # cur_right is the unit's visual right edge — used by RESIZE_RIGHT
        # bounds and snap targets. cur_width_t is the *collision* width used
        # by MOVE bounds: when this is the last unit on the track the visual
        # extent stretches to song_end, but we treat the clip as point-sized
        # so it can be moved freely (matches manager.move_event behavior).
        if idx + 1 < len(units):
            cur_right = units[idx + 1]["time"]
            cur_width_t = max(MIN_EVENT_DURATION, cur_right - cur_time)
        else:
            cur_right = song_end_time
            cur_width_t = MIN_EVENT_DURATION

        # MOVE bounds: free-space slots wide enough to fit the current unit.
        obstacles: List[Tuple[float, float]] = []
        for j, u in enumerate(units):
            if j == idx or self._is_blackout_unit(u):
                continue
            j_end = (units[j + 1]["time"]
                     if j + 1 < len(units) else song_end_time)
            obstacles.append((u["time"], j_end))
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
            move_intervals.append((slot_start * pps,
                                   (slot_end - cur_width_t) * pps))

        # RESIZE_LEFT bounds — the unit's start time can move toward the
        # closest non-BLACKOUT predecessor.
        resize_left_min_t = 0.0
        for j in range(idx - 1, -1, -1):
            if not self._is_blackout_unit(units[j]):
                resize_left_min_t = units[j]["time"] + MIN_EVENT_DURATION
                break
        resize_left_max_t = cur_right - MIN_EVENT_DURATION

        # RESIZE_RIGHT bounds — the unit's right edge can move toward the
        # closest non-BLACKOUT successor.
        resize_right_min_t = cur_time + MIN_EVENT_DURATION
        resize_right_max_t = song_end_time
        for j in range(idx + 1, len(units)):
            if not self._is_blackout_unit(units[j]):
                resize_right_max_t = units[j]["time"]
                break

        # Snap targets: every other unit's start & end edges.
        snap_xs: List[float] = []
        for j, u in enumerate(units):
            if j == idx:
                continue
            snap_xs.append(u["time"] * pps)
            j_right = (units[j + 1]["time"]
                       if j + 1 < len(units) else song_end_time)
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

    def _scene_clip_color(self, members: List[dict]) -> QColor:
        """Derive a clip color from a scene group's RGB channel values.

        Notes 2/3/4 carry blue/green/red. Velocity * 2 = channel value.
        Brightness (note 1) scales the resulting color so dim scenes read dim.
        """
        red = green = blue = 0
        brightness = 255
        for m in members:
            v2 = min(255, m["velocity"] * 2)
            if m["note"] == 1:
                brightness = v2
            elif m["note"] == 2:
                blue = v2
            elif m["note"] == 3:
                green = v2
            elif m["note"] == 4:
                red = v2
        # Apply brightness as a 0..1 multiplier; floor to a visible minimum so
        # near-black scenes don't disappear against the dark track background.
        scale = max(brightness / 255.0, 0.18)
        return QColor(int(red * scale), int(green * scale), int(blue * scale))

    def redraw_events(self, cached_events, y_offset: int):
        # Clear existing content items
        for item in self.items:
            if item.scene():
                item.scene().removeItem(item)
        self.items.clear()

        if not cached_events:
            return

        try:
            mm = self.timeline_manager.midi_manager
            # song_end_time() falls back to the audio duration / 240s default
            # when MIDI is sparse, so the visual extent of last clips matches
            # the move-bounds the manager will enforce on commit.
            song_length_seconds = (mm.song_end_time() if mm else 240.0)
            song_end_time = song_length_seconds * 1.05
            pps = self.timeline_manager.PIXELS_PER_SECOND
            track_height = self.get_track_height()

            # Filter by fixture so each track only handles its own clips —
            # neighbor lookups, MOVE bounds, and BLACKOUT gap logic all
            # operate on the per-track slice, not the global event list.
            events = [e for e in cached_events if self.matches_filter(e)]
            if not events:
                return

            # Collapse same-time scene groups into single render units before
            # drawing. A 7-event group becomes ONE clip box.
            units = build_render_units(events)
            if not units:
                return

            for i, unit in enumerate(units):
                # BLACKOUTs are pure data — gap markers used by the manager
                # for placement bounds and snap targets. They have no visual
                # representation and must not catch mouse events (no hover
                # cursor changes, no left-drag, no context menu): rendering
                # an invisible-but-hit-active rect would steal hover/drag
                # gestures from the empty space behind it. Skip entirely.
                if self._is_blackout_unit(unit):
                    continue

                start_time = unit["time"]
                end_time = (units[i + 1]["time"]
                            if i + 1 < len(units) else song_end_time)
                duration = max(0.0, end_time - start_time)
                rep = unit["representative"]

                x_pos = start_time * pps
                width = max(duration * pps, MIN_HIT_WIDTH_PX)
                bounds = self._compute_bounds(units, i, song_end_time)

                is_group = unit["kind"] == "group"
                event_rect = DraggableMidiEvent(
                    x_pos, y_offset, width, track_height,
                    event_id=rep["id"], event_data=rep,
                    bounds=bounds, is_blackout=False,
                    group_id=unit["group_id"] if is_group else None,
                    group_event_ids=unit["event_ids"] if is_group else None,
                )
                event_rect.set_timeline(self.timeline_manager)

                # The Timeline routes group vs single via the carried group_id.
                event_rect.move_committed.connect(self.timeline_manager.on_midi_event_moved)
                event_rect.left_edge_committed.connect(self.timeline_manager.on_midi_event_left_edge)
                event_rect.right_edge_committed.connect(self.timeline_manager.on_midi_event_right_edge)
                event_rect.fade_in_committed.connect(self.timeline_manager.on_midi_event_fade_in)
                event_rect.fade_out_committed.connect(self.timeline_manager.on_midi_event_fade_out)
                event_rect.edit_requested.connect(self.timeline_manager.on_midi_event_edit_by_id)
                event_rect.delete_requested.connect(self.timeline_manager.on_midi_event_delete_by_id)
                event_rect.add_at_position_requested.connect(self.timeline_manager.on_add_event_requested)

                # Pick the clip color: scene groups derive from their RGB
                # channel values; single events use their info.color.
                if is_group:
                    members = [e for e in events
                               if e.get("group_id") == unit["group_id"]]
                    color = self._scene_clip_color(members)
                    event_rect.setBrush(QBrush(color))
                    event_rect.setPen(QPen(QColor(20, 20, 20)))
                else:
                    info = rep['info']
                    color = QColor(info.color)
                    event_rect.setBrush(QBrush(color))
                    event_rect.setPen(QPen(QColor(20, 20, 20)))
                event_rect.setZValue(1)
                self.content_scene.addItem(event_rect)
                self.items.append(event_rect)

                if is_group:
                    label_text = rep.get("scene_name") or "Scene"
                    members = [e for e in events
                               if e.get("group_id") == unit["group_id"]]
                    color = self._scene_clip_color(members)
                else:
                    info = rep['info']
                    label_text = info.part
                    color = QColor(info.color)
                brightness = (color.red() * 0.299 + color.green() * 0.587 + color.blue() * 0.114)
                text_color = QColor(255, 255, 255) if brightness < 128 else QColor(0, 0, 0)
                zoom_factor = self.timeline_manager.content_view.transform().m11()
                available_width = (width - 10) * zoom_factor
                fm = self.timeline_manager.content_view.fontMetrics()
                text_to_display = self.timeline_manager.get_truncated_text(label_text, available_width, fm)

                text_item = QGraphicsTextItem(text_to_display)
                text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
                text_item.setDefaultTextColor(text_color)
                text_item.setPos(x_pos + 5, y_offset + 5)
                text_item.setZValue(2)
                text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
                text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, False)
                text_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                text_item.setAcceptHoverEvents(False)
                text_item.setData(0, label_text)
                text_item.setData(1, rep['id'])

                event_rect.set_text_item(text_item)

                self.content_scene.addItem(text_item)
                self.items.append(text_item)

        except Exception as e:
            self.logger.error(f"Error redrawing midi events: {e}")

    def redraw(self):
        """Redraw the MIDI events track."""
        pass
