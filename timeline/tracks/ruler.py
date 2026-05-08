"""
Ruler track for timeline time markers.

Displays time markers, bar numbers, and beat subdivisions.
"""

from PyQt6.QtWidgets import QGraphicsRectItem, QGraphicsLineItem, QGraphicsTextItem, QGraphicsProxyWidget
from PyQt6.QtGui import QColor, QBrush, QPen
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGraphicsItem

from .base import BaseTrack
from tupperlogger import get_logger


class RulerTrack(BaseTrack):
    """Track for displaying timeline ruler with time markers."""
    
    def __init__(self, timeline_manager, content_scene, header_scene):
        super().__init__(timeline_manager, content_scene, header_scene)
        self.bar_text_items = []  # Store bar text items for label updates
        self.logger = get_logger("timeline.tracks.ruler")
        self.permanent_header_items = []  # Items that should never be cleared (metronome controls)
        
    def get_track_height(self) -> int:
        """Get the height of the ruler track."""
        return self.timeline_manager.RULER_TRACK_HEIGHT
        
    def draw_header(self):
        """Draw the ruler header with metronome controls."""
        # Only create permanent items (metronome controls) once
        if not self.permanent_header_items:
            ruler_header_bg = QGraphicsRectItem(0, 0, self.timeline_manager.HEADER_WIDTH, self.get_track_height())
            ruler_header_bg.setBrush(QBrush(QColor(50, 50, 50)))
            ruler_header_bg.setPen(QPen(QColor(20, 20, 20)))
            self.header_scene.addItem(ruler_header_bg)
            self.permanent_header_items.append(ruler_header_bg)

            # Add metronome controls (these should never be cleared)
            if hasattr(self.timeline_manager, 'metronome_controls_container'):
                proxy = QGraphicsProxyWidget()
                proxy.setWidget(self.timeline_manager.metronome_controls_container)
                proxy.setPos(5, 5)
                proxy.setZValue(1)
                self.header_scene.addItem(proxy)
                self.permanent_header_items.append(proxy)

    def draw_ruler(self):
        """Draw the ruler with time markers."""
        # Clear existing ruler items (content only, not header)
        for item in self.items:
            if item.scene():
                item.scene().removeItem(item)
        self.items.clear()
        self.bar_text_items.clear()

        ruler_bg = QGraphicsRectItem(0, 0, self.timeline_manager.content_width, self.get_track_height())
        ruler_bg.setBrush(QBrush(QColor(40, 40, 40)))
        ruler_bg.setPen(QPen(Qt.GlobalColor.transparent))
        self.content_scene.addItem(ruler_bg)
        self.items.append(ruler_bg)

        seconds_per_beat = 60.0 / self.timeline_manager.bpm
        seconds_per_bar = seconds_per_beat * 4
        
        if seconds_per_bar == 0: 
            return
        num_bars = int(self.content_scene.width() / (seconds_per_bar * self.timeline_manager.PIXELS_PER_SECOND))

        for bar_num in range(num_bars + 1):
            bar_x = bar_num * seconds_per_bar * self.timeline_manager.PIXELS_PER_SECOND
            
            bar_tick = QGraphicsLineItem(bar_x, 0, bar_x, self.get_track_height())
            bar_pen = QPen(QColor(200, 200, 200))
            bar_pen.setCosmetic(True)
            bar_tick.setPen(bar_pen)
            self.content_scene.addItem(bar_tick)
            self.items.append(bar_tick)

            bar_text = QGraphicsTextItem(str(bar_num + 1))
            bar_text.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            bar_text.setDefaultTextColor(QColor(200, 200, 200))
            bar_text.setPos(bar_x + 5, 5)
            self.content_scene.addItem(bar_text)
            self.items.append(bar_text)
            self.bar_text_items.append((bar_num + 1, bar_text))

            for beat_num in range(1, 4):
                beat_x = bar_x + (beat_num * seconds_per_beat * self.timeline_manager.PIXELS_PER_SECOND)
                beat_tick = QGraphicsLineItem(beat_x, self.get_track_height() * 0.5, beat_x, self.get_track_height())
                beat_pen = QPen(QColor(150, 150, 150))
                beat_pen.setCosmetic(True)
                beat_tick.setPen(beat_pen)
                self.content_scene.addItem(beat_tick)
                self.items.append(beat_tick)
        
        self.update_ruler_labels()

    def update_ruler_labels(self):
        """Update ruler label visibility based on zoom level."""
        if not hasattr(self.timeline_manager, 'content_view'):
            return
            
        transform = self.timeline_manager.content_view.transform()
        if not self.bar_text_items or transform.m11() == 0:
            return

        seconds_per_bar = (60.0 / self.timeline_manager.bpm) * 4
        bar_width_pixels = seconds_per_bar * self.timeline_manager.PIXELS_PER_SECOND * transform.m11()

        MIN_PIXEL_SPACING = 40

        if bar_width_pixels < MIN_PIXEL_SPACING:
            for bar_num, text_item in self.bar_text_items:
                if bar_num == 1 or bar_num % 5 == 0:
                    text_item.setVisible(True)
                else:
                    text_item.setVisible(False)
        else:
            for bar_num, text_item in self.bar_text_items:
                text_item.setVisible(True)

    def clear_items(self):
        """Override to preserve permanent header items (metronome controls)."""
        # Only clear regular items, not permanent ones
        for item in self.items:
            if item.scene():
                item.scene().removeItem(item)
        self.items.clear()
        
        # Clear regular header items but preserve permanent ones
        for item in self.header_items:
            if item.scene():
                item.scene().removeItem(item)
        self.header_items.clear()
        
        # Don't clear permanent_header_items - they persist for the lifetime of the track

    def cleanup(self):
        """Cleanup permanent header items when track is destroyed."""
        for item in self.permanent_header_items:
            if item.scene():
                item.scene().removeItem(item)
        self.permanent_header_items.clear()
        
        # Call parent cleanup for regular items
        self.clear_items()

    def redraw(self):
        """Redraw the ruler track."""
        self.draw_ruler()