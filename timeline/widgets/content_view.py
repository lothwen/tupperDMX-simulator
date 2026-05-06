"""
Custom graphics view for timeline content.

Provides zooming, context menus, and specialized mouse handling for timeline interaction.
"""

import json

from PyQt6.QtWidgets import QGraphicsView, QMenu
from PyQt6.QtGui import QPainter
from PyQt6.QtCore import Qt

from ui.clip_library import (CLIP_MIME_TYPE, DEFAULT_DROP_VELOCITY,
                             SCENE_MIME_TYPE)


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

    def keyPressEvent(self, event):
        """Handle Delete on the currently selected MIDI clip.

        Routes through the same delete_requested signal as the right-click
        "Delete Event" menu so the confirmation dialog and BLACKOUT-conversion
        logic are shared. We deliberately delete only the first selected clip:
        the delete handler triggers a full timeline refresh, invalidating any
        further references in our local list.
        """
        if event.key() == Qt.Key.Key_Delete:
            from ..tracks.midi_events import DraggableMidiEvent
            scene = self.scene()
            if scene is not None:
                for item in scene.selectedItems():
                    if isinstance(item, DraggableMidiEvent) and not item.is_blackout_clip:
                        item.delete_requested.emit(item.event_id)
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