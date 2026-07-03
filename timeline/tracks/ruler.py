"""
Ruler track for timeline time markers.

Displays time markers, bar numbers, and beat subdivisions.
"""

import math

from PyQt6.QtWidgets import QGraphicsRectItem, QGraphicsLineItem, QGraphicsTextItem
from PyQt6.QtGui import QColor, QBrush, QPen
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGraphicsItem

from .base import BaseTrack
from tupperlogger import get_logger


class RulerTrack(BaseTrack):
    """Track for displaying timeline ruler with time markers."""
    
    # Time-mode tick spacing (clock seconds, BPM-independent).
    TIME_MINOR_INTERVAL = 1  # minor tick every second
    TIME_MAJOR_INTERVAL = 5  # major tick + m:ss label every 5 seconds

    def __init__(self, timeline_manager, content_scene, header_scene):
        super().__init__(timeline_manager, content_scene, header_scene)
        self.bar_text_items = []  # Store bar text items for label updates
        self.time_text_items = []  # Store time text items (seconds, item) for label updates
        self.logger = get_logger("timeline.tracks.ruler")
        self.permanent_header_items = []  # Items that should never be cleared (metronome controls)
        
    def get_track_height(self) -> int:
        """Get the height of the ruler track."""
        return self.timeline_manager.RULER_TRACK_HEIGHT
        
    def draw_header(self):
        """Draw the ruler header background.

        The visible controls live on the manager-owned combined header panel
        (TRACKS / MASTER / METRO), which overlays this band as one block.
        """
        # Only create the permanent background once.
        if not self.permanent_header_items:
            ruler_header_bg = QGraphicsRectItem(0, 0, self.timeline_manager.HEADER_WIDTH, self.get_track_height())
            ruler_header_bg.setBrush(QBrush(QColor(50, 50, 50)))
            ruler_header_bg.setPen(QPen(QColor(20, 20, 20)))
            self.header_scene.addItem(ruler_header_bg)
            self.permanent_header_items.append(ruler_header_bg)

    def draw_ruler(self):
        """Draw the ruler with bar or clock-time markers based on the mode."""
        # Clear existing ruler items (content only, not header)
        for item in self.items:
            if item.scene():
                item.scene().removeItem(item)
        self.items.clear()
        self.bar_text_items.clear()
        self.time_text_items.clear()

        ruler_bg = QGraphicsRectItem(0, 0, self.timeline_manager.content_width, self.get_track_height())
        ruler_bg.setBrush(QBrush(QColor(40, 40, 40)))
        ruler_bg.setPen(QPen(Qt.GlobalColor.transparent))
        self.content_scene.addItem(ruler_bg)
        self.items.append(ruler_bg)

        if getattr(self.timeline_manager, "ruler_mode", "bars") == "time":
            self._draw_time_ticks()
        else:
            self._draw_bar_ticks()

        self.update_ruler_labels()

    def _draw_bar_ticks(self):
        """Draw bar lines, in-bar subdivision ticks and bar-number labels.

        Each bar is split into ``beat_division`` parts (1 = bar lines only,
        4 = beats, 8 = half-beats), matching the toolbar's beat-division combo.
        """
        seconds_per_beat = 60.0 / self.timeline_manager.bpm
        seconds_per_bar = seconds_per_beat * 4
        divisions = self.timeline_manager.beat_division

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
            text_y = (self.get_track_height() - bar_text.boundingRect().height()) / 2
            bar_text.setPos(bar_x + 2, text_y)
            self.content_scene.addItem(bar_text)
            self.items.append(bar_text)
            self.bar_text_items.append((bar_num + 1, bar_text))

            seconds_per_div = seconds_per_bar / divisions
            for div_num in range(1, divisions):
                div_x = bar_x + (div_num * seconds_per_div * self.timeline_manager.PIXELS_PER_SECOND)
                div_tick = QGraphicsLineItem(div_x, self.get_track_height() * 0.5, div_x, self.get_track_height())
                div_pen = QPen(QColor(150, 150, 150))
                div_pen.setCosmetic(True)
                div_tick.setPen(div_pen)
                self.content_scene.addItem(div_tick)
                self.items.append(div_tick)

    def _draw_time_ticks(self):
        """Draw clock-time ticks and m:ss labels (Time mode), BPM-independent."""
        pps = self.timeline_manager.PIXELS_PER_SECOND
        total_seconds = int(self.content_scene.width() / pps)

        for sec in range(total_seconds + 1):
            tick_x = sec * pps
            is_major = sec % self.TIME_MAJOR_INTERVAL == 0

            if is_major:
                tick = QGraphicsLineItem(tick_x, 0, tick_x, self.get_track_height())
                pen = QPen(QColor(200, 200, 200))
            else:
                tick = QGraphicsLineItem(tick_x, self.get_track_height() * 0.5, tick_x, self.get_track_height())
                pen = QPen(QColor(150, 150, 150))
            pen.setCosmetic(True)
            tick.setPen(pen)
            self.content_scene.addItem(tick)
            self.items.append(tick)

            if is_major:
                label = QGraphicsTextItem(f"{sec // 60}:{sec % 60:02d}")
                label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
                label.setDefaultTextColor(QColor(200, 200, 200))
                label_y = (self.get_track_height() - label.boundingRect().height()) / 2
                label.setPos(tick_x + 2, label_y)
                self.content_scene.addItem(label)
                self.items.append(label)
                self.time_text_items.append((sec, label))

    def update_ruler_labels(self):
        """Update ruler label visibility based on zoom level."""
        if not hasattr(self.timeline_manager, 'content_view'):
            return

        transform = self.timeline_manager.content_view.transform()
        if transform.m11() == 0:
            return

        if getattr(self.timeline_manager, "ruler_mode", "bars") == "time":
            self._update_time_labels(transform.m11())
        else:
            self._update_bar_labels(transform.m11())

    def _update_bar_labels(self, scale):
        """Hide bar-number labels when bars get too dense to read."""
        if not self.bar_text_items:
            return

        seconds_per_bar = (60.0 / self.timeline_manager.bpm) * 4
        bar_width_pixels = seconds_per_bar * self.timeline_manager.PIXELS_PER_SECOND * scale

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

    def _update_time_labels(self, scale):
        """Hide m:ss labels when major ticks get too dense to read."""
        if not self.time_text_items:
            return

        label_spacing_pixels = self.TIME_MAJOR_INTERVAL * self.timeline_manager.PIXELS_PER_SECOND * scale

        MIN_PIXEL_SPACING = 40

        if label_spacing_pixels < MIN_PIXEL_SPACING:
            step = math.ceil(MIN_PIXEL_SPACING / label_spacing_pixels)
            for sec, text_item in self.time_text_items:
                major_index = sec // self.TIME_MAJOR_INTERVAL
                text_item.setVisible(major_index % step == 0)
        else:
            for _sec, text_item in self.time_text_items:
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