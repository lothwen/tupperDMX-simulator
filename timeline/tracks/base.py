"""
Base track class for timeline tracks.

Provides common functionality and interface for all timeline track types.
"""

from PyQt6.QtCore import QObject
from typing import List, Any
from PyQt6.QtWidgets import QGraphicsItem, QGraphicsScene


class BaseTrack(QObject):
    """Base class for all timeline tracks."""
    
    def __init__(self, timeline_manager, content_scene: QGraphicsScene, header_scene: QGraphicsScene):
        super().__init__()
        self.timeline_manager = timeline_manager
        self.content_scene = content_scene
        self.header_scene = header_scene
        self.items: List[QGraphicsItem] = []
        self.header_items: List[QGraphicsItem] = []
        
    def clear_items(self):
        """Remove all items from scenes and clear item lists."""
        for item in self.items:
            if item.scene():
                item.scene().removeItem(item)
        self.items.clear()
        
        for item in self.header_items:
            if item.scene():
                item.scene().removeItem(item)
        self.header_items.clear()
        
    def redraw(self):
        """Redraw the track. Must be implemented by subclasses."""
        raise NotImplementedError("Subclasses must implement redraw()")
        
    def get_track_height(self) -> int:
        """Get the height of this track in pixels."""
        return 50  # Default height, can be overridden
        
    def get_y_offset(self) -> int:
        """Calculate the Y offset for this track based on previous tracks."""
        # This will be calculated by the timeline manager
        return 0