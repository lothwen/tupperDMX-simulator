"""
Custom graphics view for timeline content.

Provides zooming, context menus, and specialized mouse handling for timeline interaction.
"""

import json

from PyQt6.QtWidgets import QGraphicsView, QMenu, QRubberBand
from PyQt6.QtGui import QPainter, QKeySequence
from PyQt6.QtCore import Qt, QRect

from ui.clip_library import (CLIP_MIME_TYPE, DEFAULT_DROP_VELOCITY,
                             SCENE_MIME_TYPE)

# Drag distance (viewport pixels) before an empty-space press becomes a
# rubber-band selection instead of a plain click (which seeks).
RUBBER_BAND_THRESHOLD_PX = 4


class ContentView(QGraphicsView):
    """Custom graphics view with timeline-specific functionality."""

    def __init__(self, timeline, parent=None):
        super().__init__(parent)
        self.timeline = timeline
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)

        # Accept drops from the clip library (custom MIME).
        self.setAcceptDrops(True)

        # Rubber-band selection state (custom — so a plain click still seeks).
        self._rubber_band = None
        self._rubber_origin = None       # viewport QPoint of the press
        self._rubber_press_scene = None  # scene QPointF of the press
        self._rubber_active = False      # True once the drag passes threshold
        self._rubber_ctrl = False        # additive (keep prior selection)
        self._rubber_base_ids = set()    # selection to preserve in additive mode

        # Enable context menu for right-clicks on empty areas
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)


    def show_context_menu(self, position):
        """Show context menu for adding events on empty timeline areas."""
        # Convert position to scene coordinates
        scene_pos = self.mapToScene(position)
        
        # Check if we're in the MIDI Events track area
        midi_events_y_start = (self.timeline.RULER_TRACK_HEIGHT +
                              self.timeline.waveform_track_height +
                              2 * self.timeline.TRACK_SPACING)
        midi_events_y_end = midi_events_y_start + self.timeline.midi_events_track_height
        
        if midi_events_y_start <= scene_pos.y() <= midi_events_y_end:
            # Check if we clicked on an empty area (no MIDI event at this position)
            item_at_pos = self.scene().itemAt(scene_pos, self.transform())
            
            # Import here to avoid circular imports
            from ..tracks.midi_events import DraggableMidiEvent
            
            # If there's no item or it's not a DraggableMidiEvent, show add menu
            if not isinstance(item_at_pos, DraggableMidiEvent):
                # Create context menu with main window as parent for proper styling
                main_window = self.timeline.parent()
                while main_window and not hasattr(main_window, 'centralWidget'):
                    main_window = main_window.parent()
                
                menu = QMenu(main_window if main_window else self)
                
                add_action = menu.addAction("Add event at this position")
                
                # Calculate the time from the click position
                time_seconds = max(0.0, scene_pos.x() / self.timeline.PIXELS_PER_SECOND)
                add_action.triggered.connect(lambda: self.timeline.on_add_event_requested(time_seconds))
                
                # Show menu at the clicked position
                menu.exec(self.mapToGlobal(position))

    # --- Selection (Ctrl+click handled by clips; rubber-band here) --------

    def _all_clips(self):
        """Every non-BLACKOUT clip currently in the scene."""
        from ..tracks.midi_events import DraggableMidiEvent
        if self.scene() is None:
            return []
        return [it for it in self.scene().items()
                if isinstance(it, DraggableMidiEvent) and not it.is_blackout_clip]

    def _selected_clips(self):
        return [it for it in self._all_clips() if it.isSelected()]

    def _clip_at(self, scene_pos):
        """Topmost non-BLACKOUT clip under a scene position, or None."""
        from ..tracks.midi_events import DraggableMidiEvent
        for item in self.scene().items(scene_pos):
            if isinstance(item, DraggableMidiEvent) and not item.is_blackout_clip:
                return item
        return None

    def mousePressEvent(self, event):
        # A left press on empty space inside a MIDI lane starts a potential
        # rubber-band. Presses on a clip, or outside the lanes (ruler/waveform),
        # fall through to the default handling (clip drag / seek propagation).
        if event.button() == Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())
            in_lane = self.timeline.fixture_at_y(scene_pos.y()) is not None
            if in_lane and self._clip_at(scene_pos) is None:
                self._rubber_origin = event.position().toPoint()
                self._rubber_press_scene = scene_pos
                self._rubber_active = False
                self._rubber_ctrl = bool(
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                if self._rubber_ctrl:
                    self._rubber_base_ids = {c.event_id for c in self._selected_clips()}
                else:
                    self._rubber_base_ids = set()
                    self.scene().clearSelection()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._rubber_origin is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            cur = event.position().toPoint()
            if not self._rubber_active:
                if (cur - self._rubber_origin).manhattanLength() < RUBBER_BAND_THRESHOLD_PX:
                    return
                self._rubber_active = True
                if self._rubber_band is None:
                    self._rubber_band = QRubberBand(
                        QRubberBand.Shape.Rectangle, self.viewport())
            rect = QRect(self._rubber_origin, cur).normalized()
            self._rubber_band.setGeometry(rect)
            self._rubber_band.show()
            self._update_rubber_selection(rect)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if (self._rubber_origin is not None
                and event.button() == Qt.MouseButton.LeftButton):
            was_active = self._rubber_active
            press_scene = self._rubber_press_scene
            ctrl = self._rubber_ctrl
            if self._rubber_band is not None:
                self._rubber_band.hide()
            self._rubber_origin = None
            self._rubber_press_scene = None
            self._rubber_active = False
            self._rubber_base_ids = set()
            # A plain click (no drag) seeks — except a bare Ctrl+click on empty
            # space, which is a no-op so it doesn't disturb the playhead.
            if not was_active and not ctrl:
                self.timeline._seek_to_scene_x(press_scene.x())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _update_rubber_selection(self, viewport_rect):
        """Select clips intersecting the band (plus the additive-mode base)."""
        scene_rect = self.mapToScene(viewport_rect).boundingRect()
        for clip in self._all_clips():
            inside = clip.sceneBoundingRect().intersects(scene_rect)
            clip.setSelected(inside or clip.event_id in self._rubber_base_ids)

    def keyPressEvent(self, event):
        """Delete / copy / paste selected MIDI clips.

        Delete and copy route through Timeline so the selection logic, BLACKOUT
        conversion, and clipboard live in one place (shared with the right-click
        menu); paste drops the clipboard at the current playhead. Scoped to the
        timeline view so text fields keep their native copy/paste.
        """
        if event.key() == Qt.Key.Key_Delete:
            if self._selected_clips():
                self.timeline.delete_selected()
                event.accept()
                return
        elif event.matches(QKeySequence.StandardKey.Copy):
            if self._selected_clips():
                self.timeline.copy_selected()
                event.accept()
                return
        elif event.matches(QKeySequence.StandardKey.Paste):
            if self.timeline.midi_manager and self.timeline.midi_manager.has_clipboard():
                self.timeline.paste_at_playhead()
                event.accept()
                return
        super().keyPressEvent(event)

    def wheelEvent(self, event):
        """Handle zooming with Ctrl+wheel."""
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            zoom_in_factor = 1.15
            zoom_out_factor = 1 / zoom_in_factor

            if event.angleDelta().y() > 0:
                self.scale(zoom_in_factor, 1)
            else:
                view_width = self.viewport().width()
                scene_width = self.scene().width()
                
                if scene_width <= 0: 
                    return

                min_scale = view_width / scene_width
                current_scale = self.transform().m11()

                if current_scale * zoom_out_factor < min_scale:
                    self.resetTransform()
                    self.scale(min_scale, 1)
                else:
                    self.scale(zoom_out_factor, 1)
            self.timeline.update_ruler_labels()
            self.timeline.update_midi_text_truncation()
        else:
            super().wheelEvent(event)

    # --- Clip library drag & drop ----------------------------------------

    def _decode_clip_payload(self, mime):
        """Return (note, velocity) from a clip-library MIME, or None."""
        if not mime.hasFormat(CLIP_MIME_TYPE):
            return None
        try:
            raw = bytes(mime.data(CLIP_MIME_TYPE)).decode("utf-8")
            payload = json.loads(raw)
            note = int(payload["note"])
            velocity = int(payload.get("velocity", DEFAULT_DROP_VELOCITY))
            return note, velocity
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def _decode_scene_payload(self, mime):
        """Return (name, channels) from a scene-library MIME, or None."""
        if not mime.hasFormat(SCENE_MIME_TYPE):
            return None
        try:
            raw = bytes(mime.data(SCENE_MIME_TYPE)).decode("utf-8")
            payload = json.loads(raw)
            name = str(payload["name"])
            channels = payload["channels"]
            if not isinstance(channels, dict):
                return None
            return name, channels
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if (self._decode_clip_payload(mime) is not None
                or self._decode_scene_payload(mime) is not None):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        mime = event.mimeData()
        scene_pos = self.mapToScene(event.position().toPoint())

        scene_decoded = self._decode_scene_payload(mime)
        if scene_decoded is not None:
            if self.timeline.can_accept_scene_drop(scene_pos.y()):
                event.acceptProposedAction()
            else:
                event.ignore()
            return

        clip_decoded = self._decode_clip_payload(mime)
        if clip_decoded is None:
            event.ignore()
            return
        note, _ = clip_decoded
        if self.timeline.can_accept_clip_drop(note, scene_pos.y()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        mime = event.mimeData()
        scene_pos = self.mapToScene(event.position().toPoint())

        scene_decoded = self._decode_scene_payload(mime)
        if scene_decoded is not None:
            name, channels = scene_decoded
            if self.timeline.handle_scene_drop(name, channels,
                                               scene_pos.x(), scene_pos.y()):
                event.acceptProposedAction()
            else:
                event.ignore()
            return

        clip_decoded = self._decode_clip_payload(mime)
        if clip_decoded is None:
            event.ignore()
            return
        note, velocity = clip_decoded
        if self.timeline.handle_clip_drop(note, velocity,
                                          scene_pos.x(), scene_pos.y()):
            event.acceptProposedAction()
        else:
            event.ignore()