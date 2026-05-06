"""
Waveform track for audio visualization.

Handles audio waveform rendering and dragging functionality.
"""

import io
import librosa
import matplotlib.pyplot as plt

from PyQt6.QtWidgets import QGraphicsRectItem, QGraphicsProxyWidget
from PyQt6.QtGui import QColor, QBrush, QPen, QPixmap
from PyQt6.QtCore import pyqtSignal, QPointF, QRectF
from PyQt6.QtWidgets import QGraphicsObject, QGraphicsItem

from .base import BaseTrack
from tupperlogger import get_logger


class DraggableWaveform(QGraphicsObject):
    """Draggable waveform graphics item."""
    
    position_changed = pyqtSignal(QPointF)

    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self.pixmap = pixmap
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.y_pos = 0

    def boundingRect(self):
        return QRectF(self.pixmap.rect())

    def paint(self, painter, option, widget):
        painter.fillRect(self.boundingRect(), QColor(50, 50, 100, 200))
        painter.drawPixmap(self.boundingRect().toRect(), self.pixmap, self.pixmap.rect())

    def setYPos(self, y):
        self.y_pos = y
        self.setY(y)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            new_pos = QPointF(value)
            new_pos.setY(self.y_pos)
            self.position_changed.emit(new_pos)
            return new_pos
        return super().itemChange(change, value)


class WaveformTrack(BaseTrack):
    """Track for displaying and managing audio waveforms."""
    
    def __init__(self, timeline_manager, content_scene, header_scene):
        super().__init__(timeline_manager, content_scene, header_scene)
        self.waveform_item = None
        self._audio_controls_proxy = None  # Persistent proxy — survives header redraws
        self.logger = get_logger("timeline.tracks.waveform")
        
    def get_track_height(self) -> int:
        """Get the height of the waveform track."""
        return self.timeline_manager.waveform_track_height
        
    def draw_header(self, y_offset: int):
        """Draw the waveform track header."""
        self.clear_header_items()

        # Draw header background
        header_rect = QGraphicsRectItem(
            0, y_offset,
            self.timeline_manager.HEADER_WIDTH,
            self.get_track_height()
        )
        header_rect.setBrush(QBrush(QColor(50, 50, 50)))
        header_rect.setPen(QPen(QColor(20, 20, 20)))
        self.header_scene.addItem(header_rect)
        self.header_items.append(header_rect)

        # Add audio controls — keep a single persistent proxy so the underlying
        # QWidget isn't destroyed when the header is redrawn during reflow.
        if hasattr(self.timeline_manager, 'audio_controls_container'):
            if self._audio_controls_proxy is None:
                self._audio_controls_proxy = QGraphicsProxyWidget()
                self._audio_controls_proxy.setWidget(self.timeline_manager.audio_controls_container)
                # Stay above the header background, which is re-added on every reflow
                # and would otherwise occlude the proxy at the same z-value.
                self._audio_controls_proxy.setZValue(1)
            if self._audio_controls_proxy.scene() is None:
                self.header_scene.addItem(self._audio_controls_proxy)
            proxy_height = self._audio_controls_proxy.size().height()
            centered_y = y_offset + max(0, (self.get_track_height() - proxy_height) / 2)
            self._audio_controls_proxy.setPos(5, centered_y)

    def clear_header_items(self):
        """Clear only header items (preserve the persistent audio-controls proxy)."""
        for item in self.header_items:
            if item.scene():
                item.scene().removeItem(item)
        self.header_items.clear()
        
    def draw_waveform(self, audio_file: str, y_offset: int):
        """Draw waveform from audio file."""
        # Preserve any existing horizontal offset (e.g. song-start offset) so
        # redraws triggered by track resizing don't snap the waveform back to x=0.
        preserved_x = self.waveform_item.x() if self.waveform_item else 0.0

        if self.waveform_item:
            self.content_scene.removeItem(self.waveform_item)
            self.waveform_item = None

        try:
            y, sr = librosa.load(audio_file, sr=None)
            song_length_seconds = librosa.get_duration(y=y, sr=sr)

            # Tell the MIDI manager the real song length so its clip-edit
            # bounds aren't pinned to the (potentially tiny) duration of
            # whatever MIDI events happen to be present.
            mm = getattr(self.timeline_manager, "midi_manager", None)
            if mm is not None and hasattr(mm, "set_song_duration"):
                mm.set_song_duration(song_length_seconds)

            # Extend content width by 5% beyond song duration to show final MIDI events
            extended_duration = song_length_seconds * 1.05
            self.timeline_manager.content_width = int(extended_duration * self.timeline_manager.PIXELS_PER_SECOND)
            self.content_scene.setSceneRect(0, 0, self.timeline_manager.content_width, self.timeline_manager.scene_height)
            
            # Redraw grid and ruler with new width
            self.timeline_manager.draw_time_grid()
            if hasattr(self.timeline_manager, 'ruler_track'):
                self.timeline_manager.ruler_track.redraw()

            waveform_height = self.get_track_height()
            
            dpi = 100
            # Only create waveform for actual song duration, not extended space
            waveform_width = int(song_length_seconds * self.timeline_manager.PIXELS_PER_SECOND)
            fig = plt.figure(figsize=(waveform_width / dpi, waveform_height / dpi), dpi=dpi)
            ax = fig.add_subplot(1, 1, 1)
            librosa.display.waveshow(y, sr=sr, ax=ax, color='w')
            ax.set_xlim([0, song_length_seconds])
            ax.axis('off')
            fig.tight_layout(pad=0)
            fig.patch.set_alpha(0)
            ax.patch.set_alpha(0)

            buf = io.BytesIO()
            fig.savefig(buf, format='png', transparent=True)
            buf.seek(0)
            waveform_pixmap = QPixmap()
            waveform_pixmap.loadFromData(buf.read())
            plt.close(fig)

            self.waveform_item = DraggableWaveform(waveform_pixmap)
            self.waveform_item.setYPos(y_offset)
            self.waveform_item.setX(preserved_x)
            self.waveform_item.setZValue(0.5)  # Keep waveform above grid but below events
            self.waveform_item.position_changed.connect(self._on_waveform_moved)
            self.content_scene.addItem(self.waveform_item)
            self.items.append(self.waveform_item)

        except Exception as e:
            self.logger.error(f"Error drawing waveform: {e}")

    def _on_waveform_moved(self, pos):
        """Handle waveform position changes."""
        offset_sec = pos.x() / self.timeline_manager.PIXELS_PER_SECOND
        self.timeline_manager.waveform_offset_changed.emit(offset_sec)

    def redraw(self):
        """Redraw the waveform track."""
        # This will be called by the timeline manager when needed
        pass