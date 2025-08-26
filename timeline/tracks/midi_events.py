"""
MIDI Events track for timeline.

Handles display and interaction with MIDI events including drag-and-drop editing,
context menus, and visual representation.
"""

from PyQt6.QtWidgets import (QGraphicsRectItem, QGraphicsTextItem, QGraphicsObject, 
                             QGraphicsItem, QMenu)
from PyQt6.QtGui import QColor, QBrush, QPen
from PyQt6.QtCore import pyqtSignal, QRectF, Qt

from .base import BaseTrack
from ..widgets.tooltip import CustomTooltip
from tupperlogger import get_logger


class DraggableMidiEvent(QGraphicsObject):
    """A MIDI event box that can be resized by dragging its left border."""
    
    timing_changed = pyqtSignal(int, float)  # event_index, new_time
    edit_requested = pyqtSignal(int)  # event_index
    delete_requested = pyqtSignal(int)  # event_index
    add_at_position_requested = pyqtSignal(float)  # time_seconds
    
    def __init__(self, x, y, width, height, event_index, event_data, parent=None):
        super().__init__(parent)
        # Store only width and height in rect, position with setPos()
        self.rect = QRectF(0, 0, width, height)
        self.setPos(x, y)  # Position the item in scene coordinates
        self.event_index = event_index
        self.event_data = event_data
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True) 
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)
        
        # Resize handle zone (left edge)
        self.resize_zone_width = 10
        self.is_resizing = False
        self.resize_start_pos = None
        self.original_width = width
        
        # Store reference to timeline for coordinate conversion
        self.timeline = None
        self.text_item = None  # Reference to the associated text label
        
        # Store the last right-click position for "Add event at this position"
        self.last_right_click_scene_pos = None
        
        # Default brush and pen
        self._brush = QBrush(QColor(100, 100, 100))
        self._pen = QPen(QColor(20, 20, 20))
        
        # Tooltip state management
        self._tooltip_shown = False
        self._custom_tooltip = None
        
        # Logger for debug messages
        self.logger = get_logger("timeline.tracks.midi_events.draggable")
        
    def boundingRect(self):
        # Return a rect that starts at (0,0) for proper item-local coordinates
        return QRectF(0, 0, self.rect.width(), self.rect.height())
    
    def paint(self, painter, option, widget):
        painter.setBrush(self._brush)
        painter.setPen(self._pen)
        # Paint at (0,0) in item coordinates, not scene coordinates
        painter.drawRect(self.boundingRect())
    
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
        """Set reference to the associated text label."""
        self.text_item = text_item
        
    def _show_persistent_tooltip(self, local_pos):
        """Show tooltip with detailed event information."""
        if not self.event_data:
            return
            
        # Create custom tooltip if it doesn't exist
        if not self._custom_tooltip:
            if self.scene() and self.scene().views():
                main_window = self.scene().views()[0].window()
                self._custom_tooltip = CustomTooltip(main_window)
            
        if not self._custom_tooltip:
            return
            
        # Get event information
        event_info = self.event_data.get('info', {})
        part_name = getattr(event_info, 'part', f'Note {self.event_data["note"]}')
        velocity = self.event_data.get('velocity', 0)
        time = self.event_data.get('time', 0.0)
        note = self.event_data.get('note', 0)
        
        # Calculate bar and beat information
        if self.timeline:
            seconds_per_beat = 60.0 / self.timeline.bpm
            beat_number = time / seconds_per_beat
            bar_number = int(beat_number / 4) + 1
            beat_in_bar = int(beat_number % 4) + 1
            
            # Create detailed tooltip text
            tooltip_text = (f"{part_name}\n"
                          f"MIDI Note: {note}\n"
                          f"Time: {time:.3f}s\n"
                          f"Bar: {bar_number}, Beat: {beat_in_bar}\n"
                          f"Velocity: {velocity}")
        else:
            # Fallback if timeline not available
            tooltip_text = (f"{part_name}\n"
                          f"MIDI Note: {note}\n"
                          f"Time: {time:.3f}s\n"
                          f"Velocity: {velocity}")
        
        # Get global position for tooltip
        scene_pos = self.mapToScene(local_pos)
        if self.scene() and self.scene().views():
            for view in self.scene().views():
                global_pos = view.mapToGlobal(view.mapFromScene(scene_pos))
                # Offset tooltip slightly to avoid covering the cursor
                global_pos.setY(global_pos.y() + 20)
                self._custom_tooltip.show_at(global_pos, tooltip_text)
                break
        
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            local_pos = event.pos()
            
            # Check if we're in the resize zone (left edge)
            if local_pos.x() <= self.resize_zone_width:
                self.logger.debug(f"Starting MIDI event resize for event {self.event_index}")
                # Hide tooltip when starting drag operation
                if self._custom_tooltip:
                    self._custom_tooltip.hide_tooltip()
                self._tooltip_shown = False
                
                self.is_resizing = True
                self.resize_start_pos = event.scenePos()
                self.original_x = self.pos().x()
                self.original_width = self.rect.width()
                event.accept()
                return
        elif event.button() == Qt.MouseButton.RightButton:
            # Store the scene position of the right-click for \"Add event at this position\"
            self.last_right_click_scene_pos = event.scenePos()
            self.show_context_menu(event.screenPos())
            event.accept()
            return
        
        super().mousePressEvent(event)
        
    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.edit_requested.emit(self.event_index)
            event.accept()
            return
        
        super().mouseDoubleClickEvent(event)
        
    def show_context_menu(self, global_pos):
        """Show right-click context menu."""
        # Find the main window for proper styling
        main_window = self.timeline.parent() if self.timeline else None
        while main_window and not hasattr(main_window, 'centralWidget'):
            main_window = main_window.parent()
        
        menu = QMenu(main_window)
        
        # Edit Properties action
        edit_action = menu.addAction("Edit Properties...")
        edit_action.triggered.connect(lambda: self.edit_requested.emit(self.event_index))
        
        # Add separator
        menu.addSeparator()
        
        # Add event at this position action
        add_action = menu.addAction("Add event at this position")
        # Calculate time from the mouse click position
        if self.last_right_click_scene_pos and self.timeline:
            click_time = max(0.0, self.last_right_click_scene_pos.x() / self.timeline.PIXELS_PER_SECOND)
            add_action.triggered.connect(lambda: self.add_at_position_requested.emit(click_time))
        else:
            # Fallback to event time if position not available
            event_time = self.event_data['time']
            add_action.triggered.connect(lambda: self.add_at_position_requested.emit(event_time))
        
        # Add separator
        menu.addSeparator()
        
        # Delete Event action
        delete_action = menu.addAction("Delete Event")
        delete_action.triggered.connect(lambda: self.delete_requested.emit(self.event_index))
        
        menu.exec(global_pos)
    
    def mouseMoveEvent(self, event):
        if self.is_resizing and self.resize_start_pos:
            # Calculate new position based on mouse movement (dragging left edge)
            current_pos = event.scenePos()
            delta_x = current_pos.x() - self.resize_start_pos.x()
            new_x = self.original_x + delta_x
            
            # Prevent moving left border before 0.0 seconds (x = 0)
            new_x = max(0, new_x)
            
            # Calculate new width (since we're moving the left edge, width changes inversely)
            right_edge = self.original_x + self.original_width  # Keep right edge fixed
            new_width = max(20, right_edge - new_x)  # Minimum width of 20px
            
            # If new_width would be less than minimum, adjust new_x
            if new_width == 20:
                new_x = right_edge - 20
                # But still ensure we don't go below 0
                new_x = max(0, new_x)
            
            # Update position and size
            self.setPos(new_x, self.pos().y())
            new_rect = QRectF(0, 0, new_width, self.rect.height())
            self.setRect(new_rect)
            
            # Update text position to follow the left edge and recalculate truncation
            if self.text_item and self.timeline:
                self.text_item.setPos(new_x + 5, self.text_item.pos().y())
                
                # Recalculate text truncation based on new width
                # Account for zoom level since text ignores transformations
                zoom_factor = self.timeline.content_view.transform().m11()
                available_width = (new_width - 10) * zoom_factor  # 5px padding on each side, adjusted for zoom
                font_metrics = self.timeline.content_view.fontMetrics()
                full_text = self.text_item.data(0)  # Get the stored full text
                if full_text:
                    text_to_display = self.timeline.get_truncated_text(full_text, available_width, font_metrics)
                    self.text_item.setPlainText(text_to_display)
            
            event.accept()
            return
            
        super().mouseMoveEvent(event)
    
    def mouseReleaseEvent(self, event):
        if self.is_resizing and self.timeline:
            # Calculate the new timing for THIS event (the one being moved)
            new_start_time = self.pos().x() / self.timeline.PIXELS_PER_SECOND
            self.logger.debug(f"MIDI event {self.event_index} moved to time {new_start_time:.3f}s")
            self.timing_changed.emit(self.event_index, new_start_time)
            
        self.is_resizing = False
        self.resize_start_pos = None
        super().mouseReleaseEvent(event)
    
    def hoverEnterEvent(self, event):
        # Show tooltip when entering the item
        if self.event_data:
            self._show_persistent_tooltip(event.pos())
            self._tooltip_shown = True
        
        super().hoverEnterEvent(event)

    def hoverMoveEvent(self, event):
        local_pos = event.pos()
        
        # Change cursor when over resize zone (left edge)
        if local_pos.x() <= self.resize_zone_width:
            # Set cursor on the view instead of the item
            if self.scene() and self.scene().views():
                for view in self.scene().views():
                    view.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            # Set cursor on the view instead of the item
            if self.scene() and self.scene().views():
                for view in self.scene().views():
                    view.setCursor(Qt.CursorShape.ArrowCursor)
            
        super().hoverMoveEvent(event)
        
    def hoverLeaveEvent(self, event):
        # Hide custom tooltip when leaving and reset tooltip state
        if self._custom_tooltip:
            self._custom_tooltip.hide_tooltip()
        self._tooltip_shown = False
        
        # Reset cursor on the view when leaving
        if self.scene() and self.scene().views():
            for view in self.scene().views():
                view.setCursor(Qt.CursorShape.ArrowCursor)
        super().hoverLeaveEvent(event)


class MidiEventsTrack(BaseTrack):
    """Track for displaying and managing MIDI events."""
    
    def __init__(self, timeline_manager, content_scene, header_scene):
        super().__init__(timeline_manager, content_scene, header_scene)
        self.logger = get_logger("timeline.tracks.midi_events")
        
    def get_track_height(self) -> int:
        """Get the height of the MIDI events track."""
        return self.timeline_manager.MIDI_EVENTS_TRACK_HEIGHT
        
    def draw_header(self, y_offset: int):
        """Draw the MIDI events track header."""
        # Clear existing header items
        for item in self.header_items:
            if item.scene():
                item.scene().removeItem(item)
        self.header_items.clear()
        
        # Draw header background
        header_rect = QGraphicsRectItem(
            0, y_offset,
            self.timeline_manager.PIANO_WIDTH,
            self.get_track_height()
        )
        header_rect.setBrush(QBrush(QColor(50, 50, 50)))
        header_rect.setPen(QPen(QColor(20, 20, 20)))
        self.header_scene.addItem(header_rect)
        self.header_items.append(header_rect)

        # Add header text
        header_text = QGraphicsTextItem("MIDI Events")
        header_text.setDefaultTextColor(QColor(200, 200, 200))
        header_text.setPos(5, y_offset + 5)
        self.header_scene.addItem(header_text)
        self.header_items.append(header_text)
        
    def redraw_events(self, cached_events, y_offset: int):
        """Redraw the MIDI events track with cached data."""
        # Remove existing MIDI event items (content only, preserve header)
        for item in self.items:
            if item.scene():
                item.scene().removeItem(item)
        self.items.clear()
        
        if not cached_events:
            return
            
        try:
            # Get song length for calculating durations
            song_length_seconds = self.timeline_manager.midi_manager.get_duration() if self.timeline_manager.midi_manager else 240
            # Use extended timeline duration for last event calculation
            extended_duration = song_length_seconds * 1.05

            for i in range(len(cached_events)):
                event = cached_events[i]
                start_time = event['time']
                if i < len(cached_events) - 1:
                    duration = cached_events[i+1]['time'] - start_time
                else:
                    # For last event, extend to the end of extended timeline
                    duration = extended_duration - start_time
                    # Ensure minimum width for visibility
                    if duration <= 0:
                        duration = song_length_seconds * 0.05  # Use 5% of song as minimum
                
                x_pos = start_time * self.timeline_manager.PIXELS_PER_SECOND
                width = max(duration * self.timeline_manager.PIXELS_PER_SECOND, 20)  # Minimum 20px width
                
                # Create draggable MIDI event box
                event_rect = DraggableMidiEvent(x_pos, y_offset, width, self.get_track_height(), i, event)
                event_rect.set_timeline(self.timeline_manager)
                event_rect.timing_changed.connect(self.timeline_manager.on_midi_event_timing_changed)
                event_rect.edit_requested.connect(self.timeline_manager.on_midi_event_edit_requested)
                event_rect.delete_requested.connect(self.timeline_manager.on_midi_event_delete_requested)
                event_rect.add_at_position_requested.connect(self.timeline_manager.on_add_event_requested)
                
                info = event['info']
                color = QColor(info.color)
                event_rect.setBrush(QBrush(color))
                event_rect.setPen(QPen(QColor(20,20,20)))
                event_rect.setZValue(1)  # Ensure event box is above other items
                self.content_scene.addItem(event_rect)
                self.items.append(event_rect)

                # Determine optimal text color based on background color brightness
                brightness = (color.red() * 0.299 + color.green() * 0.587 + color.blue() * 0.114)
                text_color = QColor(255, 255, 255) if brightness < 128 else QColor(0, 0, 0)
                
                # Calculate maximum text that fits in the box width
                # Account for zoom level since text ignores transformations
                zoom_factor = self.timeline_manager.content_view.transform().m11()
                available_width = (width - 10) * zoom_factor  # 5px padding on each side, adjusted for zoom
                font_metrics = self.timeline_manager.content_view.fontMetrics()
                text_to_display = self.timeline_manager.get_truncated_text(info.part, available_width, font_metrics)
                
                text_item = QGraphicsTextItem(text_to_display)
                text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
                text_item.setDefaultTextColor(text_color)
                text_item.setPos(x_pos + 5, y_offset + 5)
                text_item.setZValue(2)  # Text should be above the box
                text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
                # Make text item completely transparent to mouse events
                text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
                text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, False)
                text_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                text_item.setAcceptHoverEvents(False)  # Disable hover events on text
                # Store the full text for tooltip
                text_item.setData(0, info.part)
                
                # Connect the text item to the event box
                event_rect.set_text_item(text_item)
                
                self.content_scene.addItem(text_item)
                self.items.append(text_item)

        except Exception as e:
            self.logger.error(f"Error redrawing midi events: {e}")

    def redraw(self):
        """Redraw the MIDI events track."""
        # This will be called by the timeline manager when needed
        pass