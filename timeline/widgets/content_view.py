"""
Custom graphics view for timeline content.

Provides zooming, context menus, and specialized mouse handling for timeline interaction.
"""

from PyQt6.QtWidgets import QGraphicsView, QMenu
from PyQt6.QtGui import QPainter
from PyQt6.QtCore import Qt
import librosa

from .tooltip import CustomTooltip


class ContentView(QGraphicsView):
    """Custom graphics view with timeline-specific functionality."""
    
    def __init__(self, timeline, parent=None):
        super().__init__(parent)
        self.timeline = timeline
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        
        # Enable context menu for right-clicks on empty areas
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        
        # Custom tooltip for piano roll notes
        self._piano_tooltip = None
        self._current_tooltip_item = None
        
    def show_context_menu(self, position):
        """Show context menu for adding events on empty timeline areas."""
        # Convert position to scene coordinates
        scene_pos = self.mapToScene(position)
        
        # Check if we're in the MIDI Events track area
        midi_events_y_start = (self.timeline.RULER_TRACK_HEIGHT + 
                              self.timeline.WAVEFORM_TRACK_HEIGHT + 
                              2 * self.timeline.TRACK_SPACING)
        midi_events_y_end = midi_events_y_start + self.timeline.MIDI_EVENTS_TRACK_HEIGHT
        
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

    def mouseMoveEvent(self, event):
        """Handle piano roll note tooltips."""
        scene_pos = self.mapToScene(event.pos())
        
        # Check for piano roll notes
        current_item = None
        for item in self.timeline.note_items:
            if item.contains(scene_pos):
                current_item = item
                break
        
        # Handle tooltip based on current item
        if current_item:
            # If this is a new item or first time, show tooltip
            if current_item != self._current_tooltip_item:
                # Hide any existing tooltip
                if self._piano_tooltip:
                    self._piano_tooltip.hide_tooltip()
                
                # Create custom tooltip if it doesn't exist
                if not self._piano_tooltip:
                    self._piano_tooltip = CustomTooltip(self.window())
                
                # Show tooltip for this note
                note_info = current_item.data(0)
                note_name = librosa.midi_to_note(note_info['note'])
                seconds_per_beat = 60.0 / self.timeline.bpm
                beat_number = note_info['time'] / seconds_per_beat
                bar_number = int(beat_number / 4) + 1
                beat_in_bar = int(beat_number % 4) + 1
                tooltip_text = f"{note_name}\\nTime: {note_info['time']:.2f}s\\nBar: {bar_number}, Beat: {beat_in_bar}\\nVelocity: {note_info['velocity']}"
                
                global_pos = event.globalPosition().toPoint()
                global_pos.setY(global_pos.y() + 20)  # Offset to avoid cursor
                self._piano_tooltip.show_at(global_pos, tooltip_text)
                self._current_tooltip_item = current_item
        else:
            # No item under cursor, hide tooltip
            if self._piano_tooltip:
                self._piano_tooltip.hide_tooltip()
            self._current_tooltip_item = None
        
        super().mouseMoveEvent(event)