"""
Waveform track for audio visualization.

Handles audio waveform rendering and dragging functionality.
"""

import io
import librosa
import matplotlib.pyplot as plt

from PyQt6.QtWidgets import (QGraphicsRectItem,
                             QGraphicsTextItem, QGraphicsDropShadowEffect)
from PyQt6.QtGui import QColor, QBrush, QPen, QPixmap
from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QRectF
from PyQt6.QtWidgets import QGraphicsObject, QGraphicsItem

from .base import BaseTrack
from .song_sections import SongSectionItem
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
        # Song-structure overlay items (kept separate from self.items, which
        # holds the waveform pixmap, so a section redraw never disposes it).
        self._section_items = []
        self._stale_sections = []  # prevents premature C++ destruction
        self.logger = get_logger("timeline.tracks.waveform")
        
    def get_track_height(self) -> int:
        """Get the height of the waveform track."""
        return self.timeline_manager.waveform_track_height
        
    def draw_header(self, y_offset: int):
        """Draw the waveform track header background.

        The visible controls (MASTER/METRO volumes) live on the manager-owned
        combined header panel that overlays this band.
        """
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

    def clear_header_items(self):
        """Clear the header background items."""
        for item in self.header_items:
            if item.scene():
                item.scene().removeItem(item)
        self.header_items.clear()

    def clear_items(self):
        """Also dispose the song-section overlay items (kept outside self.items)."""
        super().clear_items()
        for item in self._section_items:
            if item.scene():
                item.scene().removeItem(item)
        self._section_items.clear()
        self._stale_sections = []
        
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

    def draw_song_sections(self, events, y_offset: int):
        """Draw the song-part arrangement as translucent contiguous boxes over
        the waveform. `events` are the cached song-part events (already filtered
        by the manager); each box spans from its marker to the next, the last to
        the song end. Items live in `_section_items`, disposed defensively (see
        MidiEventsTrack.redraw_events) so the scene's BSP index never references
        freed memory."""
        stale = list(self._section_items)
        for item in stale:
            item.setVisible(False)
            item.setEnabled(False)
            item.prepareGeometryChange()
            if item.scene():
                item.scene().removeItem(item)
            if hasattr(item, 'deleteLater'):
                item.deleteLater()
        self._section_items.clear()
        self._stale_sections = stale

        if not events:
            return

        tm = self.timeline_manager
        pps = tm.PIXELS_PER_SECOND
        height = self.get_track_height()

        mm = getattr(tm, "midi_manager", None)
        song_end = (mm.song_end_time() if mm and hasattr(mm, "song_end_time")
                    else (tm.content_width / pps if pps else 0.0))

        ordered = sorted(events, key=lambda e: e['time'])
        for i, ev in enumerate(ordered):
            start_time = ev['time']
            end_time = ordered[i + 1]['time'] if i + 1 < len(ordered) else song_end
            duration = max(0.0, end_time - start_time)

            info = ev.get('info')
            part = getattr(info, 'part', f"Note {ev.get('note')}")
            color = getattr(info, 'color', '#888888')

            x_pos = start_time * pps
            width = max(duration * pps, 2)

            box = SongSectionItem(x_pos, y_offset, width, height,
                                  event_id=ev['id'], part=part, color=color)
            box.set_timeline(tm)
            box.setZValue(1)  # above the waveform pixmap (z=0.5)
            box.move_committed.connect(tm.on_song_section_moved)
            box.edit_requested.connect(tm.on_midi_event_edit_by_id)
            box.delete_requested.connect(tm.on_song_section_delete)
            self.content_scene.addItem(box)
            self._section_items.append(box)

            # Unscaled label, repositioned by the box during a drag.
            available = (width - 10) * tm.content_view.transform().m11()
            fm = tm.content_view.fontMetrics()
            label = QGraphicsTextItem(tm.get_truncated_text(part, available, fm))
            label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            label.setDefaultTextColor(QColor(25, 25, 25))
            label_font = label.font()
            label_font.setBold(True)
            label.setFont(label_font)
            # Center the label within the box. The label ignores view
            # transforms, so its bounding rect is in device px; convert its
            # width back to scene px via the horizontal zoom (vertical is 1:1).
            zoom = tm.content_view.transform().m11() or 1.0
            tw = label.boundingRect().width()
            th = label.boundingRect().height()
            label.setPos(x_pos + width / 2 - (tw / zoom) / 2,
                         y_offset + (height - th) / 2)
            label.setZValue(2)
            label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.content_scene.addItem(label)
            self._section_items.append(label)
            box.text_item = label

    def redraw(self):
        """Redraw the waveform track."""
        # This will be called by the timeline manager when needed
        pass