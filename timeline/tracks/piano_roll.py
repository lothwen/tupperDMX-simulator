"""
Piano roll track for MIDI note visualization.

Handles display of piano keys and MIDI notes in a traditional piano roll format.
"""

from PyQt6.QtWidgets import QGraphicsRectItem, QGraphicsLineItem
from PyQt6.QtGui import QColor, QBrush, QPen
from PyQt6.QtCore import Qt

from .base import BaseTrack
from tupperlogger import get_logger


class PianoRollTrack(BaseTrack):
    """Track for displaying piano roll and MIDI notes."""
    
    def __init__(self, timeline_manager, content_scene, header_scene):
        super().__init__(timeline_manager, content_scene, header_scene)
        self.logger = get_logger("timeline.tracks.piano_roll")
        
    def get_track_height(self) -> int:
        """Get the height of the piano roll track."""
        note_range = self.timeline_manager.max_note - self.timeline_manager.min_note
        return self.timeline_manager.KEY_HEIGHT * note_range
        
    def draw_piano_keys(self, y_offset: int):
        """Draw the piano keys in the header."""
        # Clear existing piano roll items
        for item in self.header_items:
            if item.scene():
                item.scene().removeItem(item)
        self.header_items.clear()
        
        note_range = self.timeline_manager.max_note - self.timeline_manager.min_note
        if note_range <= 0: 
            return
        
        track_height = self.get_track_height()

        piano_rect = QGraphicsRectItem(0, y_offset, self.timeline_manager.PIANO_WIDTH, track_height)
        piano_rect.setBrush(QBrush(QColor(10, 10, 10)))
        piano_rect.setPen(QPen(QColor(20, 20, 20)))
        self.header_scene.addItem(piano_rect)
        self.header_items.append(piano_rect)

        for i in range(self.timeline_manager.min_note, self.timeline_manager.max_note + 1):
            y_pos = y_offset + (self.timeline_manager.max_note - i) * self.timeline_manager.KEY_HEIGHT
            is_black_key = (i % 12) in [1, 3, 6, 8, 10] 
            
            key_rect = QGraphicsRectItem(0, y_pos, self.timeline_manager.PIANO_WIDTH, self.timeline_manager.KEY_HEIGHT)
            key_rect.setBrush(QBrush(QColor(100, 100, 100) if is_black_key else QColor(250, 250, 250)))
            key_rect.setPen(QPen(QColor(20, 20, 20)))
            self.header_scene.addItem(key_rect)
            self.header_items.append(key_rect)

    def draw_piano_roll_grid(self, y_offset: int):
        """Draw horizontal grid lines for piano roll."""
        # Clear existing grid items
        for item in self.items:
            if item.scene():
                item.scene().removeItem(item)
        self.items.clear()
        
        for i in range(self.timeline_manager.min_note, self.timeline_manager.max_note + 1):
            y_pos = y_offset + (self.timeline_manager.max_note - i) * self.timeline_manager.KEY_HEIGHT
            is_black_key = (i % 12) in [1, 3, 6, 8, 10] 
            
            grid_line = QGraphicsLineItem(0, y_pos, self.timeline_manager.content_width, y_pos)
            grid_line.setPen(QPen(QColor(40, 40, 40) if is_black_key else QColor(50, 50, 50)))
            grid_line.setZValue(-1)  # Keep piano roll grid lines in background
            self.content_scene.addItem(grid_line)
            self.items.append(grid_line)

    def redraw_notes(self, cached_notes, y_offset: int):
        """Redraw piano roll notes based on cached data."""
        # Remove existing note items (but keep grid)
        note_items = [item for item in self.items if isinstance(item, QGraphicsRectItem)]
        for item in note_items:
            self.content_scene.removeItem(item)
            self.items.remove(item)
        
        # Add note items to timeline manager's note_items list for tooltip handling
        self.timeline_manager.note_items.clear()
        
        # Redraw notes from cached data
        for note in cached_notes:
            x_pos = note["time"] * self.timeline_manager.PIXELS_PER_SECOND
            y_pos = y_offset + (self.timeline_manager.max_note - note["note"]) * self.timeline_manager.KEY_HEIGHT
            width = note["duration"] * self.timeline_manager.PIXELS_PER_SECOND
            note_rect = QGraphicsRectItem(x_pos, y_pos, width, self.timeline_manager.KEY_HEIGHT)
            note_rect.setBrush(QBrush(QColor(150, 0, 0, 220)))
            note_rect.setPen(QPen(QColor(250, 250, 250)))
            note_rect.setData(0, note)
            self.content_scene.addItem(note_rect)
            self.items.append(note_rect)
            self.timeline_manager.note_items.append(note_rect)

    def redraw(self):
        """Redraw the piano roll track."""
        # This will be called by the timeline manager when needed
        pass