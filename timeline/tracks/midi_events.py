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

from typing import List, Optional

from PyQt6.QtWidgets import (QGraphicsTextItem, QGraphicsObject,
                             QGraphicsItem, QMenu, QPushButton, QGraphicsProxyWidget,
                             QWidget, QFrame, QLabel, QHBoxLayout, QVBoxLayout)
from PyQt6.QtGui import (QColor, QBrush, QPen, QLinearGradient, QPainter,
                         QPolygonF, QIcon)
from PyQt6.QtCore import pyqtSignal, QRectF, Qt, QPointF, QSize

from ui.widgets import icons

from .base import BaseTrack
from .clip_geometry import (
    ClipBounds, Occupant,
    MODE_IDLE, MODE_MOVE, MODE_RESIZE_LEFT, MODE_RESIZE_RIGHT, MODE_GROUP_MOVE,
    zone_at, resolve_move_x, resolve_resize_x, compute_bounds,
)
from core import note_layout
from core.fixture_library import color_for_type, GROUP_COLORS
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


# Edge-zone width (screen pixels) for resize gestures. Converted to
# scene-space at the current zoom in _zone_at so the edges stay a constant
# on-screen width regardless of zoom level.
RESIZE_ZONE_PX = 8
# Snap radius — if a dragged edge ends within this many screen-pixels of a
# neighboring clip edge, snap to it. Converted to scene-space at the current
# zoom in the drag-preview methods so the snap zone stays a constant
# on-screen distance.
SNAP_PX = 6
# Minimum visible width for a clip's hit area (so sub-pixel clips remain
# clickable until the user zooms in).
MIN_HIT_WIDTH_PX = 1
# Visual styling for clip boxes.
CLIP_CORNER_RADIUS_PX = 4
# Vertical breathing room around a clip, as a FRACTION of the track height
# (not fixed pixels — tracks are user-resizable, so a ratio keeps the top/bottom
# gap proportional at any track height). 0.0 = clips fill the lane flush with the
# neighbouring tracks; e.g. 0.1 leaves 10% of the lane height blank above and
# below each clip.
CLIP_VERTICAL_MARGIN_RATIO = 0.1
CLIP_GRADIENT_LIGHTEN = 0 # 118   # top of gradient = base.lighter(LIGHTEN)
CLIP_GRADIENT_DARKEN = 0 # 140    # bottom of gradient = base.darker(DARKEN)
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

# Auto-mode visualization clip styling (read-only song-part preview, see
# AutoSceneClip). Diagonal hatch + dashed border mark it as auto / not editable;
# the hatch is drawn in device pixels so its spacing/angle stay constant at any
# zoom. Fill is the scene swatch colour.
AUTO_CLIP_HATCH_SPACING_PX = 7
AUTO_CLIP_HATCH_ALPHA = 70

# Opacity applied to every clip on a muted (eye-off) track so the lane reads as
# silenced but its content stays visible.
MUTED_CLIP_OPACITY = 0.35


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

    def _zoom_x(self) -> float:
        """Horizontal scale factor from the view transform."""
        scene = self.scene()
        if scene and scene.views():
            return scene.views()[0].transform().m11()
        return 1.0

    def _triangle_for_paint(self, zoom_x: float) -> QPolygonF:
        sx = FADE_HANDLE_SIZE_PX / zoom_x if zoom_x > 0 else FADE_HANDLE_SIZE_PX
        sy = FADE_HANDLE_SIZE_PX
        if self.side == "in":
            return QPolygonF([QPointF(0, 0), QPointF(sx, 0), QPointF(0, sy)])
        return QPolygonF([QPointF(0, 0), QPointF(-sx, 0), QPointF(0, sy)])

    def boundingRect(self) -> QRectF:
        zoom_x = self._zoom_x()
        sx = FADE_HANDLE_SIZE_PX / zoom_x if zoom_x > 0 else FADE_HANDLE_SIZE_PX
        sy = FADE_HANDLE_SIZE_PX
        if self.side == "in":
            return QRectF(0, 0, sx, sy)
        return QRectF(-sx, 0, sx, sy)

    def paint(self, painter, option, widget):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QBrush(FADE_HANDLE_COLOR))
        fade_handle_pen = QPen(QColor(20, 20, 20))
        fade_handle_pen.setCosmetic(True)
        painter.setPen(fade_handle_pen)
        zoom_x = painter.transform().m11()
        painter.drawPolygon(self._triangle_for_paint(zoom_x))

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
    group_move_committed = pyqtSignal(float)        # delta_seconds (whole selection)

    # Drag-mode enum — defined in clip_geometry; mirrored as class attributes
    # so the many existing self.MODE_* references keep working.
    MODE_IDLE = MODE_IDLE
    MODE_MOVE = MODE_MOVE
    MODE_RESIZE_LEFT = MODE_RESIZE_LEFT
    MODE_RESIZE_RIGHT = MODE_RESIZE_RIGHT
    MODE_GROUP_MOVE = MODE_GROUP_MOVE

    def __init__(self, x, y, width, height, event_id: str, event_data: dict,
                 bounds: ClipBounds, is_blackout: bool = False,
                 group_id: Optional[str] = None,
                 group_event_ids: Optional[List[str]] = None,
                 locked: bool = False,
                 parent=None):
        super().__init__(parent)
        self.rect = QRectF(0, 0, max(width, MIN_HIT_WIDTH_PX), height)
        self.setPos(x, y)

        self.event_id = event_id
        self.event_data = event_data
        self.bounds = bounds
        self.is_blackout_clip = is_blackout
        # Locked clips (their track's lock toggle is on) are read-only: no
        # select/move/resize/fade/edit, and no context menu. Hover tooltips
        # (pure info) stay available.
        self.locked = locked
        # Scene-clip grouping: when set, this DraggableMidiEvent represents a
        # whole scene group; the Timeline routes drag signals to the scene API
        # rather than the single-event API.
        self.group_id = group_id
        self.group_event_ids = list(group_event_ids) if group_event_ids else []

        # Qt item flags. Locked clips are non-selectable/focusable so they
        # can't be marquee-selected or dragged.
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, not locked)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, not locked)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)

        # Drag state
        self._mode = self.MODE_IDLE
        self._press_scene_pos = None
        self._press_self_pos = None         # self.pos() at press time
        self._press_width = float(width)    # rect width at press time

        # Multi-selection state. When this clip is part of a 2+ selection it
        # enters "multi" mode: fade handles hidden, edge-resize disabled (the
        # whole clip is a MOVE handle), and a drag slides the entire selection.
        self._multi_selected = False
        # Set when a Ctrl+click toggled selection on press, so the matching
        # release can be swallowed (otherwise Qt's default mouseReleaseEvent
        # re-toggles the selection on a no-move click, undoing ours).
        self._ctrl_toggle_pending = False
        # Captured at the start of a group move: list of
        # (clip, start_pos, text_start_pos) plus the clamped pixel delta range.
        self._group_move_peers = []
        self._group_min_dx = 0.0
        self._group_max_dx = 0.0

        # External hooks
        self.timeline = None
        self.text_item = None
        self.last_right_click_scene_pos = None

        # Painting state (overridden by setBrush/setPen unless BLACKOUT)
        self._brush = QBrush(QColor(100, 100, 100))
        self._pen = QPen(QColor(20, 20, 20))
        self._pen.setCosmetic(True)

        # Tooltip — a single shared widget owned by the Timeline manager is
        # reused by every clip (see Timeline.clip_tooltip). This clip only
        # tracks whether it currently has one shown.
        self._tooltip_shown = False

        # Fade durations (seconds). Sourced from event_data when present;
        # default to 0 so a clip with no fades renders cleanly. The handles
        # use these as their pixel positions and as the live-preview value
        # during a drag.
        self.fade_in_seconds = float(event_data.get("fade_in", 0.0)) if event_data else 0.0
        self.fade_out_seconds = float(event_data.get("fade_out", 0.0)) if event_data else 0.0
        self._fade_in_handle: Optional[FadeHandle] = None
        self._fade_out_handle: Optional[FadeHandle] = None
        if not is_blackout and not locked:
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
        tx = painter.transform()
        corner_rx = CLIP_CORNER_RADIUS_PX / tx.m11() if tx.m11() > 0 else CLIP_CORNER_RADIUS_PX
        corner_ry = CLIP_CORNER_RADIUS_PX / abs(tx.m22()) if tx.m22() != 0 else CLIP_CORNER_RADIUS_PX
        painter.drawRoundedRect(rect, corner_rx, corner_ry)

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

    def _zoom_x(self) -> float:
        """Horizontal view zoom (m11); 1.0 when the view isn't available.

        Screen-pixel UI constants (snap radius, resize-zone width) are divided
        by this so they stay a constant on-screen size regardless of zoom.
        """
        if self.timeline is not None:
            m11 = self.timeline.content_view.transform().m11()
            if m11 > 0:
                return m11
        return 1.0

    def _zone_at(self, local_x: float) -> str:
        """Classify a local x position into a drag zone.

        RESIZE_ZONE_PX is a screen-space width; convert it to scene-space at the
        current zoom so the resize edges stay ~8px on-screen and don't swallow
        the whole clip (and its MOVE zone) when zoomed in.
        """
        return zone_at(local_x, self.rect.width(),
                       RESIZE_ZONE_PX / self._zoom_x(), self._multi_selected)

    def _set_view_cursor(self, cursor_shape):
        if self.scene() and self.scene().views():
            for view in self.scene().views():
                view.setCursor(cursor_shape)

    # --- Tooltip ----------------------------------------------------------

    def _show_persistent_tooltip(self, local_pos):
        if not self.event_data or self.is_blackout_clip:
            # No tooltip on BLACKOUTs — they're just gaps.
            return
        if not self.timeline:
            return
        tooltip = self.timeline.clip_tooltip()

        event_info = self.event_data.get('info', {})
        part_name = getattr(event_info, 'part', f'Note {self.event_data["note"]}')
        velocity = self.event_data.get('velocity', 0)
        time = self.event_data.get('time', 0.0)
        note = self.event_data.get('note', 0)

        bar_number, beat_in_bar = self.timeline.tempo_map.bar_beat_at(time)
        tooltip_text = (f"{part_name}\n"
                        f"MIDI Note: {note}\n"
                        f"Time: {time:.3f}s\n"
                        f"Bar: {bar_number}, Beat: {beat_in_bar}\n"
                        f"Velocity: {velocity}")

        scene_pos = self.mapToScene(local_pos)
        if self.scene() and self.scene().views():
            for view in self.scene().views():
                global_pos = view.mapToGlobal(view.mapFromScene(scene_pos))
                global_pos.setY(global_pos.y() + 20)
                tooltip.show_at(global_pos, tooltip_text)
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

    def _selected_sibling_clips(self):
        """All currently-selected non-BLACKOUT clips in the scene (incl. self)."""
        scene = self.scene()
        if scene is None:
            return []
        return [it for it in scene.selectedItems()
                if isinstance(it, DraggableMidiEvent) and not it.is_blackout_clip]

    def set_multi_selected(self, value: bool):
        """Toggle multi-selection mode: hide fade handles, disable resize.

        Driven centrally by the Timeline whenever the selection count changes.
        """
        if self._multi_selected == value:
            return
        self._multi_selected = value
        for handle in (self._fade_in_handle, self._fade_out_handle):
            if handle is not None:
                handle.setVisible(not value)

    def _begin_group_move(self, press_scene_pos):
        """Capture peer positions + clamp range for a whole-selection move."""
        self._mode = self.MODE_GROUP_MOVE
        self._press_scene_pos = press_scene_pos
        self._group_move_peers = []
        for clip in self._selected_sibling_clips():
            text_pos = clip.text_item.pos() if clip.text_item is not None else None
            self._group_move_peers.append((clip, clip.pos(), text_pos))
        if self.timeline is not None:
            self._group_min_dx, self._group_max_dx = \
                self.timeline.selection_move_bounds_px()
        else:
            self._group_min_dx, self._group_max_dx = 0.0, 0.0

    def _apply_group_move_preview(self, delta_x: float):
        """Slide every selected clip (and its label) by a clamped delta."""
        clamped = max(self._group_min_dx, min(delta_x, self._group_max_dx))
        for clip, start_pos, text_start in self._group_move_peers:
            clip.setPos(start_pos.x() + clamped, start_pos.y())
            if clip.text_item is not None and text_start is not None:
                clip.text_item.setPos(text_start.x() + clamped, text_start.y())

    def mousePressEvent(self, event):
        if self.locked:
            # Read-only: swallow the press so there's no select/drag/menu.
            if self.timeline:
                self.timeline.hide_clip_tooltip()
            self._tooltip_shown = False
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if self.timeline:
                self.timeline.hide_clip_tooltip()
            self._tooltip_shown = False
            ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)

            if ctrl and not self.is_blackout_clip:
                # Ctrl+click toggles this clip in/out of the selection; no drag.
                self.setSelected(not self.isSelected())
                self._mode = self.MODE_IDLE
                self._press_scene_pos = None
                self._ctrl_toggle_pending = True
                event.accept()
                return

            if self.isSelected() and len(self._selected_sibling_clips()) >= 2:
                # Drag the whole multi-selection as a rigid block.
                self._begin_group_move(event.scenePos())
                event.accept()
                return

            # Single-clip behavior (unchanged).
            self._select_exclusive()
            self._mode = self._zone_at(event.pos().x())
            self._press_scene_pos = event.scenePos()
            self._press_self_pos = self.pos()
            self._press_width = self.rect.width()
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton:
            # Keep the selection intact when right-clicking a clip that's part
            # of a multi-selection (so "Delete N events" acts on all of them).
            if not (self.isSelected() and len(self._selected_sibling_clips()) >= 2):
                self._select_exclusive()
            self.last_right_click_scene_pos = event.scenePos()
            self.show_context_menu(event.screenPos())
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and not self.is_blackout_clip and not self.locked):
            self.edit_requested.emit(self.event_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if self._mode == self.MODE_IDLE or self._press_scene_pos is None:
            super().mouseMoveEvent(event)
            return

        delta_x = event.scenePos().x() - self._press_scene_pos.x()

        if self._mode == self.MODE_GROUP_MOVE:
            self._apply_group_move_preview(delta_x)
        elif self._mode == self.MODE_MOVE:
            self._apply_move_preview(delta_x)
        elif self._mode == self.MODE_RESIZE_LEFT:
            self._apply_resize_left_preview(delta_x)
        elif self._mode == self.MODE_RESIZE_RIGHT:
            self._apply_resize_right_preview(delta_x)
        event.accept()

    def mouseReleaseEvent(self, event):
        # A Ctrl+click already toggled selection on press; swallow the release
        # so Qt's default item selection handling doesn't toggle it back.
        if self._ctrl_toggle_pending:
            self._ctrl_toggle_pending = False
            event.accept()
            return
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
            if self._mode == self.MODE_GROUP_MOVE:
                clamped = max(self._group_min_dx,
                              min(event.scenePos().x() - self._press_scene_pos.x(),
                                  self._group_max_dx))
                self.group_move_committed.emit(clamped / pps)
            elif self._mode == self.MODE_MOVE:
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
        self._group_move_peers = []
        super().mouseReleaseEvent(event)

    # --- Drag previews ----------------------------------------------------
    # Each preview computes the proposed new position/width, clamps to bounds,
    # snaps to nearby targets, and updates the visual state. No data mutation.

    def _grid_x(self):
        """The nearest-grid-line callable clips snap to, or None.

        Delegates to the timeline so snapping follows the visible grid: the
        per-second lines in Time mode, or the bar/beat-division lines in Bars
        mode. Returns None when the timeline is unavailable.
        """
        if self.timeline is None:
            return None
        return self.timeline.snap_grid_x()

    def _snap_targets(self):
        """Neighbour-edge snap targets, or empty when snapping is disabled."""
        if self.timeline is not None and not self.timeline.snap_enabled:
            return []
        return self.bounds.snap_xs

    def _apply_move_preview(self, delta_x: float):
        # SNAP_PX is a screen-space radius; convert to scene-space at the
        # current zoom so the snap zone stays a constant on-screen distance.
        proposed = self._press_self_pos.x() + delta_x
        new_x = resolve_move_x(proposed, self.bounds.move_intervals,
                               self._snap_targets(), SNAP_PX / self._zoom_x(),
                               self._grid_x())
        if new_x is None:
            return  # no slot fits; nothing to preview
        self.setPos(new_x, self.pos().y())
        self._reposition_text(new_x)

    def _apply_resize_left_preview(self, delta_x: float):
        # The right edge is fixed; left edge moves.
        original_right = self._press_self_pos.x() + self._press_width
        new_x = resolve_resize_x(self._press_self_pos.x() + delta_x,
                                 self.bounds.resize_left_min_x,
                                 self.bounds.resize_left_max_x,
                                 self._snap_targets(), SNAP_PX / self._zoom_x(),
                                 self._grid_x())
        new_width = max(MIN_HIT_WIDTH_PX, original_right - new_x)
        self.setPos(new_x, self.pos().y())
        self.setRect(QRectF(0, 0, new_width, self.rect.height()))
        self._reposition_text(new_x)
        self._refresh_text_truncation(new_width)
        self._clamp_fades_to_width(new_width)

    def _apply_resize_right_preview(self, delta_x: float):
        # The left edge is fixed; right edge moves.
        original_left = self._press_self_pos.x()
        new_right = resolve_resize_x(original_left + self._press_width + delta_x,
                                     self.bounds.resize_right_min_x,
                                     self.bounds.resize_right_max_x,
                                     self._snap_targets(), SNAP_PX / self._zoom_x(),
                                     self._grid_x())
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
        if self.locked:
            # No move/resize affordance on a locked clip.
            self._set_view_cursor(Qt.CursorShape.ArrowCursor)
            super().hoverMoveEvent(event)
            return
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
        if self.timeline:
            self.timeline.hide_clip_tooltip()
        self._tooltip_shown = False

    # --- Context menu -----------------------------------------------------

    def show_context_menu(self, global_pos):
        main_window = self.timeline.parent() if self.timeline else None
        while main_window and not hasattr(main_window, 'centralWidget'):
            main_window = main_window.parent()
        menu = QMenu(main_window)

        selected_clips = self._selected_sibling_clips()
        if self.isSelected() and len(selected_clips) >= 2:
            # Multi-selection: a single action that deletes them all at once.
            delete_action = menu.addAction(f"Delete {len(selected_clips)} events")
            if self.timeline is not None:
                delete_action.triggered.connect(self.timeline.delete_selected)
            menu.exec(global_pos)
            return

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


class AutoSceneClip(QGraphicsObject):
    """Read-only visualization of what the song-part program runs for one
    section on this fixture, shown only while the fixture's 'A' (auto) toggle is
    on. Carries no MIDI event and is never persisted — it's recomputed on every
    redraw from the song-part markers + the fixture's def. Fully non-interactive
    (a preview aid); the diagonal hatch + dashed border mark it as auto / not
    editable. Hatch/border ink flips black<->white for contrast against the
    fill so it stays visible on both light and dark scene colours."""

    def __init__(self, x, y, width, height, color: QColor):
        super().__init__()
        self.rect = QRectF(0, 0, width, height)
        self._color = QColor(color)
        b = color.red() * 0.299 + color.green() * 0.587 + color.blue() * 0.114
        ink = QColor(0, 0, 0) if b >= 128 else QColor(235, 235, 235)
        self._border_color = ink
        self._hatch_color = QColor(ink)
        self._hatch_color.setAlpha(AUTO_CLIP_HATCH_ALPHA)
        self.setPos(x, y)
        # Ignore all input so it never steals hover/drag/clicks from the empty
        # space behind it or the manual clips painted on top.
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, False)

    def boundingRect(self):
        return QRectF(0, 0, self.rect.width(), self.rect.height())

    def paint(self, painter, option, widget):
        rect = self.boundingRect()
        # Scene-colour fill (square corners — distinct from the rounded,
        # gradient-filled editable clips).
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(self._color))
        painter.drawRect(rect)

        # Diagonal hatch, drawn in device space so its 45° angle and spacing
        # stay constant under the view's horizontal-only zoom.
        dev = painter.transform().mapRect(rect)
        painter.save()
        painter.resetTransform()
        painter.setClipRect(dev)
        pen = QPen(self._hatch_color)
        pen.setWidth(1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        x = dev.left() - dev.height()
        while x < dev.right():
            painter.drawLine(QPointF(x, dev.bottom()),
                             QPointF(x + dev.height(), dev.top()))
            x += AUTO_CLIP_HATCH_SPACING_PX
        painter.restore()

        # Dashed border = read-only / auto marker.
        pen = QPen(self._border_color)
        pen.setWidth(1)
        pen.setCosmetic(True)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)


# Header toggle-bar buttons (SVG icons via ui.widgets.icons; frames styled via the app
# stylesheet by objectName). The lock swaps open<->closed through its icon's
# On state; the eye keeps one glyph and signals "muted" via its checked (amber)
# background. The chevron points down when expanded and right when collapsed.
def _header_button(object_name: str, tooltip: str, *, text: str = "",
                   icon: Optional[QIcon] = None,
                   checkable: bool = False) -> QPushButton:
    """A 22px header button showing an icon (or short text), styled by QSS."""
    btn = QPushButton(text)
    if icon is not None:
        btn.setIcon(icon)
        btn.setIconSize(QSize(13, 13))
    btn.setObjectName(object_name)
    btn.setFixedSize(22, 22)
    btn.setToolTip(tooltip)
    btn.setCheckable(checkable)
    return btn


class TrackHeaderWidget(QWidget):
    """The per-fixture timeline track header.

    Left to right: a reorder grip (visual placeholder for now) and a collapse
    chevron, a type-coloured swatch that matches the fixture's clip-library
    section, the fixture name over its MIDI/DMX address, and the eye (mute) /
    lock / A (auto) toggle bar. Each toggle emits a signal; the owning
    MidiEventsTrack routes it to the MidiManager, and pushes state back in via
    set_states(). The chevron collapses the track to a single row, hiding the
    MIDI/DMX address line and flipping its glyph to point right."""

    eye_toggled = pyqtSignal(bool)      # True == muted
    lock_toggled = pyqtSignal(bool)     # True == locked
    auto_toggled = pyqtSignal(bool)     # True == auto
    collapse_toggled = pyqtSignal(bool)  # True == collapsed

    def __init__(self, label: str, channel: int, dmx_start: int, dmx_end: int,
                 accent: str, parent=None):
        super().__init__(parent)
        self.setObjectName("TrackHeader")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Per-instance accent: the left border + swatch use the fixture colour;
        # the rest of the styling comes from the app stylesheet by objectName.
        self.setStyleSheet(
            "QWidget#TrackHeader { background-color: #242424; "
            f"border-left: 4px solid {accent}; "
            "border-bottom: 1px solid #444; "
            "border-right: 1px solid #444; }")

        root = QHBoxLayout(self)
        root.setContentsMargins(8, 4, 6, 4)
        root.setSpacing(4)

        self.grip = _header_button("TrackGrip",
                                   "Drag to reorder (coming soon)",
                                   icon=icons.icon("drag", "#8a8a8a"))
        self.grip.setEnabled(False)
        # Checked (collapsed) swaps the icon to the right-pointing chevron.
        self.chevron = _header_button("TrackChevron",
                                      "Collapse track", checkable=True,
                                      icon=icons.icon("chevron-down", "#8a8a8a",
                                                      on_name="chevron-right"))
        self.chevron.toggled.connect(self._on_chevron_toggled)
        root.addWidget(self.grip)
        root.addWidget(self.chevron)

        self.swatch = QFrame()
        self.swatch.setObjectName("TrackSwatch")
        self.swatch.setFixedSize(12, 12)
        self.swatch.setStyleSheet(
            f"background-color: {accent}; border-radius: 2px;")

        # Swatch sits on the name's row; the address sits below, left-aligned to
        # the same edge as the swatch. Stretches keep the block vertically
        # centred as the track height grows (they collapse to 0 when small).
        text_col = QVBoxLayout()
        text_col.setContentsMargins(2, 0, 0, 0)
        text_col.setSpacing(0)
        self.name_label = QLabel(label)
        self.name_label.setObjectName("TrackName")
        # 0-based, the raw MIDI channel — the same number the patch stores and
        # every other surface shows.
        addr = f"Ch. {channel}"
        if dmx_end >= dmx_start:
            addr += f"  ·  DMX {dmx_start}–{dmx_end}"
        self.info_label = QLabel(addr)
        self.info_label.setObjectName("TrackInfo")

        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(6)
        name_row.addWidget(self.swatch)
        name_row.addWidget(self.name_label)
        name_row.addStretch(1)

        text_col.addStretch(1)
        text_col.addLayout(name_row)
        text_col.addWidget(self.info_label)
        text_col.addStretch(1)
        root.addLayout(text_col, 1)

        self.eye_btn = _header_button("TrackEyeButton",
                                      "Hide / mute this track's MIDI output",
                                      icon=icons.icon("eye"),
                                      checkable=True)
        self.eye_btn.toggled.connect(self.eye_toggled.emit)

        # Checked (locked) swaps to the closed padlock, dark for contrast on
        # the gold fill.
        self.lock_btn = _header_button("TrackLockButton",
                                       "Lock this track (no move, resize or new clips)",
                                       icon=icons.icon("unlock",
                                                       on_name="lock",
                                                       on_color="#1d1d1d"),
                                       checkable=True)
        self.lock_btn.toggled.connect(self.lock_toggled.emit)

        self.auto_btn = _header_button("AutoModeButton",
                                       "Follow the song-part program (auto)",
                                       text="A",
                                       checkable=True)
        self.auto_btn.toggled.connect(self.auto_toggled.emit)

        root.addWidget(self.eye_btn)
        root.addWidget(self.lock_btn)
        root.addWidget(self.auto_btn)

    def set_states(self, *, auto: bool, mute: bool, lock: bool,
                   auto_enabled: bool):
        """Reflect the persisted/runtime state on the toggles without
        re-emitting (blockSignals guards the programmatic setChecked)."""
        for btn, val in ((self.eye_btn, mute), (self.lock_btn, lock),
                         (self.auto_btn, auto)):
            btn.blockSignals(True)
            btn.setChecked(val)
            btn.blockSignals(False)
        self.auto_btn.setEnabled(auto_enabled)

    def _on_chevron_toggled(self, collapsed: bool):
        """Hide the MIDI/DMX address row (the checked chevron icon flips to
        point right by itself), then let the owning track restack the timeline
        to the new (collapsed) height."""
        self.info_label.setVisible(not collapsed)
        self.collapse_toggled.emit(collapsed)

    def set_collapsed(self, collapsed: bool):
        """Reflect collapse state on the chevron/address row without emitting
        collapse_toggled — used when the toolbar collapses/expands every lane at
        once, so the owning track can batch a single reflow."""
        self.chevron.blockSignals(True)
        self.chevron.setChecked(collapsed)
        self.chevron.blockSignals(False)
        self.info_label.setVisible(not collapsed)


class MidiEventsTrack(BaseTrack):
    """Track for displaying and managing MIDI events."""

    def __init__(self, timeline_manager, content_scene, header_scene,
                 fixture_index: int = 0, label: str = "MIDI Events"):
        super().__init__(timeline_manager, content_scene, header_scene)
        # This track owns one patched fixture, identified by its stable MIDI
        # channel (the binding key — independent of the track's display row,
        # which is ordered by DMX address). label is the header text shown for
        # the fixture.
        self.fixture_index = fixture_index
        self.label = label
        # View-only collapse state (not persisted): when True the lane shrinks
        # to a single header row (MIN_TRACK_HEIGHT), hiding the MIDI/DMX address.
        self.collapsed = False
        self._stale_items = []  # prevents premature C++ destruction (see redraw_events)
        # Persistent header proxy — the whole TrackHeaderWidget lives on one
        # QGraphicsProxyWidget that survives per-reflow header redraws (like the
        # waveform's audio-controls proxy) so its QWidget isn't destroyed.
        self._header_proxy = None
        self._header_widget = None
        self.logger = get_logger("timeline.tracks.midi_events")

    def clear_items(self):
        """Tear down for a patch rebuild / timeline cleanup. The header proxy
        lives outside header_items (so per-redraw header clears don't destroy
        it), so dispose it explicitly here."""
        super().clear_items()
        if self._header_proxy is not None:
            if self._header_proxy.scene():
                self._header_proxy.scene().removeItem(self._header_proxy)
            self._header_proxy.deleteLater()
            self._header_proxy = None
            self._header_widget = None

    def _software_controller(self):
        """The live software DMX controller, or None. Auto mode is a property of
        its runtime fixtures, so the toggle only applies when it's present."""
        mm = self.timeline_manager.midi_manager if self.timeline_manager else None
        return getattr(mm, "software_controller", None) if mm else None

    def _midi_manager(self):
        return self.timeline_manager.midi_manager if self.timeline_manager else None

    def _on_auto_toggled(self, checked: bool):
        # Auto is only meaningful with a software controller (the button is
        # disabled otherwise). Persist through the manager: it writes the
        # per-track note-71 setup flag, mirrors onto the controller, and commits
        # — the resulting data_changed redraws this lane's auto visualization.
        if self._software_controller() is None:
            return
        mm = self._midi_manager()
        if mm is not None:
            mm.set_fixture_auto(self.fixture_index, checked)

    def _on_eye_toggled(self, muted: bool):
        # Eye off == muted: the manager persists the note-71 mute bit, tells the
        # worker to skip this fixture's MIDI channel, and commits so the lane
        # redraws dimmed.
        mm = self._midi_manager()
        if mm is not None:
            mm.set_fixture_mute(self.fixture_index, muted)

    def _on_lock_toggled(self, locked: bool):
        # Lock persists the note-71 lock bit and commits so this lane's clips
        # redraw read-only (non-interactive) and drops onto it are blocked.
        mm = self._midi_manager()
        if mm is not None:
            mm.set_fixture_lock(self.fixture_index, locked)

    def _on_collapse_toggled(self, collapsed: bool):
        # Collapse is view-only (not persisted): shrink this lane to one header
        # row and restack every track below it. The header widget already hid
        # its address row and flipped the chevron glyph.
        self.collapsed = collapsed
        self.timeline_manager.reflow_tracks()

    def set_collapsed(self, collapsed: bool):
        """Set collapse state and sync the header, WITHOUT reflowing — the caller
        (a collapse-all/expand-all pass) batches a single reflow. Safe before the
        header widget is built."""
        self.collapsed = collapsed
        if self._header_widget is not None:
            self._header_widget.set_collapsed(collapsed)

    def sync_header_state(self):
        """Reflect this fixture's auto/mute/lock state on the header toggles.
        Auto follows the runtime controller (and is enabled only when one
        exists); mute/lock follow the document's persisted note-71 flags."""
        if self._header_widget is None:
            return
        controller = self._software_controller()
        mm = self._midi_manager()
        self._header_widget.set_states(
            auto=bool(controller is not None
                      and controller.is_fixture_auto(self.fixture_index)),
            mute=bool(mm is not None and mm.is_fixture_muted(self.fixture_index)),
            lock=bool(mm is not None and mm.is_fixture_locked(self.fixture_index)),
            auto_enabled=controller is not None)

    def matches_filter(self, event) -> bool:
        """True if `event` belongs on this fixture's lane.

        Song parts (61-69) are global verbs rendered as an arrangement overlay
        on the waveform (see WaveformTrack.draw_song_sections), not on any
        fixture lane. Everything else is scoped to the fixture whose patch index
        (= MIDI channel) matches this track.
        """
        mm = self.timeline_manager.midi_manager if self.timeline_manager else None
        if mm is not None and mm.is_song_part(event):
            return False
        if mm is not None:
            return mm._event_fixture(event) == self.fixture_index
        info = event.get('info') if event else None
        return getattr(info, 'fixture', None) == self.fixture_index

    def get_track_height(self) -> int:
        if self.collapsed:
            return self.timeline_manager.MIN_TRACK_HEIGHT
        return self.timeline_manager.midi_events_track_height

    def _clip_band(self, y_offset: int, track_height: int):
        """Vertical placement (top y, height) of a clip inside its lane, inset
        top and bottom by CLIP_VERTICAL_MARGIN_RATIO of the track height so the
        gap stays proportional as the user resizes tracks. Returns integers."""
        inset = round(track_height * CLIP_VERTICAL_MARGIN_RATIO)
        return y_offset + inset, track_height - 2 * inset

    def _header_accent(self) -> str:
        """The fixture's type colour (matches its clip-library section)."""
        tm = self.timeline_manager
        ftype = tm._fixture_type(self.fixture_index) if tm else None
        return color_for_type(tm.fixtures, ftype) if ftype else GROUP_COLORS[0]

    def draw_header(self, y_offset: int):
        # The whole header is one TrackHeaderWidget on a persistent proxy (built
        # once, then just repositioned/resized on reflow) — it paints its own
        # background, so no separate rect/text items are needed.
        if self._header_proxy is None:
            tm = self.timeline_manager
            inst = tm._fixture_for_channel(self.fixture_index)
            fdef = tm._fdef(self.fixture_index)
            dmx_start = getattr(inst, "dmx_start", 0) if inst else 0
            count = fdef.channel_count if fdef else 0
            dmx_end = dmx_start + count - 1 if count else dmx_start - 1
            self._header_widget = TrackHeaderWidget(
                self.label, self.fixture_index, dmx_start, dmx_end,
                self._header_accent())
            self._header_widget.eye_toggled.connect(self._on_eye_toggled)
            self._header_widget.lock_toggled.connect(self._on_lock_toggled)
            self._header_widget.auto_toggled.connect(self._on_auto_toggled)
            self._header_widget.collapse_toggled.connect(self._on_collapse_toggled)
            self._header_proxy = QGraphicsProxyWidget()
            self._header_proxy.setWidget(self._header_widget)
            self._header_proxy.setZValue(1)
        if self._header_proxy.scene() is None:
            self.header_scene.addItem(self._header_proxy)
        self._header_widget.setFixedSize(self.timeline_manager.HEADER_WIDTH,
                                         self.get_track_height())
        self._header_proxy.setPos(0, y_offset)
        self.sync_header_state()

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
        7-event scene clip moves/resizes as a single occupant. The geometry
        itself lives in clip_geometry.compute_bounds; here we just project the
        render units onto time-ordered Occupants and supply pps + MIN_DURATION.
        """
        from midi.manager import MIN_EVENT_DURATION
        pps = self.timeline_manager.PIXELS_PER_SECOND
        occupants = [Occupant(u["time"], self._is_blackout_unit(u))
                     for u in units]
        return compute_bounds(occupants, idx, song_end_time, pps,
                              MIN_EVENT_DURATION)

    def _scene_clip_color(self, members: List[dict]) -> QColor:
        """Derive a clip color from a scene group's channel values, using each
        channel's semantic role (so non-RGB fixtures like blinders still read
        as a sensible warm/cool colour). Falls back to the LED-bar note layout
        (1=brightness, 2=blue, 3=green, 4=red) when no fixture def resolves."""
        velocity_by_note = {m["note"]: m["velocity"] for m in members}
        channel = members[0].get("channel") if members else None
        fdef = (self.timeline_manager._fdef(channel)
                if self.timeline_manager else None)
        if fdef:
            r, g, b = note_layout.scene_rgb_from_notes(fdef, velocity_by_note)
            return QColor(r, g, b)

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

    def _auto_markers(self, cached_events):
        """``(managed, [(time, NoteInfo|None), ...] sorted)`` for the song-part
        program as it applies to THIS fixture.

        `managed` is True only when the fixture's 'A' (auto) toggle is on, a
        software controller exists, and its type has a song-part program — i.e.
        when song-part markers actually drive it. Each marker carries the scene
        info this fixture runs there, or None when the part isn't programmed
        (the program blacks the fixture out, so no clip is drawn). Returns
        ``(False, [])`` when the program doesn't drive this lane."""
        controller = self._software_controller()
        if controller is None or not controller.is_fixture_auto(self.fixture_index):
            return False, []
        mm = self.timeline_manager.midi_manager if self.timeline_manager else None
        if mm is None:
            return False, []
        fdef = self.timeline_manager._fdef(self.fixture_index)
        if fdef is None or not note_layout.song_part_refs(fdef):
            return False, []   # this fixture type has no song-part program
        markers = [(ev["time"],
                    note_layout.song_part_clip_info(
                        fdef, getattr(ev.get("info"), "part", "") or ""))
                   for ev in cached_events if mm.is_song_part(ev)]
        markers.sort(key=lambda m: m[0])
        return True, markers

    def _draw_auto_scene_clips(self, markers, units, song_end_time,
                               y_offset, pps, track_height, muted=False):
        """Draw the read-only song-part preview from `markers` (see
        _auto_markers). One AutoSceneClip per programmed marker, spanning from
        the marker to the next event that changes the fixture: the next manual
        clip that starts within the section, else the next song-part marker."""
        if not markers:
            return
        # The first manual clip after a marker ends its auto clip: the manual
        # clip fires its own note and changes the fixture; auto only resumes at
        # the next marker (it does not pick back up afterwards).
        manual_starts = sorted(units[i]["time"] for i in range(len(units))
                               if not self._is_blackout_unit(units[i]))
        eps = 1e-3

        for j, (sec_start, clip_info) in enumerate(markers):
            sec_end = (markers[j + 1][0] if j + 1 < len(markers)
                       else song_end_time)
            # Only programmed parts draw a clip; the rest leave the fixture dark.
            if clip_info is None:
                continue
            end = sec_end
            for s in manual_starts:
                if s > sec_start + eps:
                    if s < sec_end:
                        end = s
                    break
            if end - sec_start <= eps:
                continue
            self._add_auto_scene_clip(sec_start, end, clip_info.part,
                                      QColor(clip_info.color), y_offset, pps,
                                      track_height, muted)

    def _add_auto_scene_clip(self, start, end, label, color, y_offset, pps,
                             height, muted=False):
        """Add one AutoSceneClip (+ its unscaled label) for the range
        [start, end). Beneath the manual clips (z<1) and non-interactive."""
        x_pos = start * pps
        width = max((end - start) * pps, MIN_HIT_WIDTH_PX)
        clip_y, clip_h = self._clip_band(y_offset, height)
        clip = AutoSceneClip(x_pos, clip_y, width, clip_h, color)
        clip.setZValue(0.5)  # under manual clips (z=1) and their labels (z=2)
        if muted:
            clip.setOpacity(MUTED_CLIP_OPACITY)
        self.content_scene.addItem(clip)
        self.items.append(clip)

        brightness = color.red() * 0.299 + color.green() * 0.587 + color.blue() * 0.114
        text_color = QColor(235, 235, 235) if brightness < 128 else QColor(20, 20, 20)
        zoom = self.timeline_manager.content_view.transform().m11()
        available_width = (width - 10) * zoom
        fm = self.timeline_manager.content_view.fontMetrics()
        text = self.timeline_manager.get_truncated_text(label, available_width, fm)

        text_item = QGraphicsTextItem(text)
        text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        text_item.setDefaultTextColor(text_color)
        text_y = clip_y + (clip_h - text_item.boundingRect().height()) / 2
        text_item.setPos(x_pos + 5, text_y)
        text_item.setZValue(0.6)
        text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, False)
        text_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        text_item.setAcceptHoverEvents(False)
        if muted:
            text_item.setOpacity(MUTED_CLIP_OPACITY)
        self.content_scene.addItem(text_item)
        self.items.append(text_item)

    def redraw_events(self, cached_events, y_offset: int):
        # Hide the shared clip tooltip before destroying the clips. If the
        # cursor is hovering a clip when a redraw fires, that clip is deleted
        # below without ever receiving hoverLeaveEvent — without this the
        # tooltip would be left orphaned on screen.
        if self.timeline_manager:
            self.timeline_manager.hide_clip_tooltip()

        # Safely dispose of old scene items.  The scene's BSP spatial index
        # and internal dirty-item bookkeeping can retain pointers to items
        # across event-loop ticks.  If the C++ QGraphicsItem is destroyed
        # (via Python GC / SIP destructor) before those internal references
        # are cleared, the next repaint dereferences freed memory → SIGSEGV.
        #
        # Defence-in-depth strategy:
        #  1. Hide + disable so Qt's paint loop skips them.
        #  2. prepareGeometryChange() evicts from the BSP index.
        #  3. removeItem() detaches from the scene's item list.
        #  4. For QGraphicsObject-derived items (which are QObjects),
        #     schedule C++ destruction via deleteLater() — this defers the
        #     destructor until Qt has fully finished processing the current
        #     event batch, including any pending repaints.
        #  5. Keep Python references alive in _stale_items until the next
        #     redraw so that plain QGraphicsItems (like QGraphicsTextItem)
        #     also survive at least one full event-loop cycle.
        stale = list(self.items)
        for item in stale:
            item.setVisible(False)
            item.setEnabled(False)
            item.prepareGeometryChange()
            if item.scene():
                item.scene().removeItem(item)
            # QGraphicsObject (e.g. DraggableMidiEvent) inherits QObject
            # and supports deleteLater(); plain QGraphicsItems don't.
            if hasattr(item, 'deleteLater'):
                item.deleteLater()
        self.items.clear()
        # Previous stale batch can now be released (it survived a full cycle).
        # Current batch replaces it and will survive until the next redraw.
        self._stale_items = stale

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

            # Per-track header toggles: muted lanes draw every clip dimmed;
            # locked lanes draw clips read-only (non-interactive).
            muted = bool(mm and mm.is_fixture_muted(self.fixture_index))
            locked = bool(mm and mm.is_fixture_locked(self.fixture_index))

            # Filter by fixture so each track only handles its own clips —
            # neighbor lookups, MOVE bounds, and BLACKOUT gap logic all
            # operate on the per-track slice, not the global event list.
            events = [e for e in cached_events if self.matches_filter(e)]

            # Collapse same-time scene groups into single render units before
            # drawing. A 7-event group becomes ONE clip box.
            units = build_render_units(events)

            # The song-part program as it drives THIS lane (empty unless its 'A'
            # auto toggle is on). `managed` means song-part markers actually
            # change this fixture, so each marker both ends the manual clip
            # spanning it and starts the read-only auto-scene preview.
            managed, auto_markers = self._auto_markers(cached_events)

            # The auto visualization sits BENEATH the manual clips and must
            # render even when this lane has no manual clips of its own, so it
            # runs before the early-out below.
            self._draw_auto_scene_clips(auto_markers, units, song_end_time,
                                        y_offset, pps, track_height, muted)

            if not units:
                return

            marker_times = [t for t, _ in auto_markers]

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
                # A song-part marker fires its program here and changes the
                # fixture (to an auto scene or, for unprogrammed parts, black),
                # so it also ends a manual clip that spans it.
                if managed:
                    for t in marker_times:
                        if t > start_time + 1e-3:
                            if t < end_time:
                                end_time = t
                            break
                duration = max(0.0, end_time - start_time)
                rep = unit["representative"]

                x_pos = start_time * pps
                width = max(duration * pps, MIN_HIT_WIDTH_PX)

                bounds = self._compute_bounds(units, i, song_end_time)

                clip_y, clip_h = self._clip_band(y_offset, track_height)
                is_group = unit["kind"] == "group"
                event_rect = DraggableMidiEvent(
                    x_pos, clip_y, width, clip_h,
                    event_id=rep["id"], event_data=rep,
                    bounds=bounds, is_blackout=False,
                    group_id=unit["group_id"] if is_group else None,
                    group_event_ids=unit["event_ids"] if is_group else None,
                    locked=locked,
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
                event_rect.add_at_position_requested.connect(
                    lambda t, idx=self.fixture_index:
                    self.timeline_manager.on_add_event_requested(t, idx))
                event_rect.group_move_committed.connect(self.timeline_manager.on_selection_moved)

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
                text_y = clip_y + (clip_h - text_item.boundingRect().height()) / 2
                text_item.setPos(x_pos + 5, text_y)
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

                if muted:
                    event_rect.setOpacity(MUTED_CLIP_OPACITY)
                    text_item.setOpacity(MUTED_CLIP_OPACITY)

        except Exception as e:
            self.logger.error(f"Error redrawing midi events: {e}")

    def redraw(self):
        """Redraw the MIDI events track."""
        pass
