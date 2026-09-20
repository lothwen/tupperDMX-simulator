"""
Waveform track for audio visualization.

Handles audio waveform rendering and dragging functionality.
"""

from PyQt6.QtWidgets import QGraphicsRectItem, QGraphicsDropShadowEffect
from PyQt6.QtGui import QColor, QBrush, QPen, QPixmap, QPainter
from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QRectF, QLine
from PyQt6.QtWidgets import QGraphicsObject, QGraphicsItem

from .base import BaseTrack
from .song_sections import SongSectionOverlay
from core.waveform import bin_extremes
from tupperlogger import get_logger

# Dimmed so the song sections' tints read cleanly over it.
WAVEFORM_OPACITY = 0.90


def render_waveform_pixmap(pcm, width: int, height: int) -> QPixmap:
    """One column per pixel, drawn straight onto a transparent pixmap.

    Replaces a matplotlib figure whose width was the whole song (a 4-minute
    song is a 5000 px canvas, i.e. a 50-inch figure at 100 dpi) rendered to PNG
    and decoded back. The bins are the asymmetric min/max envelope rather than
    a symmetric bar, which is the shape the plotted version had."""
    pixmap = QPixmap(max(1, width), max(1, height))
    pixmap.fill(Qt.GlobalColor.transparent)
    if pcm is None or len(pcm) == 0 or width < 1:
        return pixmap   # geometry now, samples when the decode lands

    mins, maxs = bin_extremes(pcm, width)
    scale = max(float(maxs.max()), -float(mins.min()))
    if scale <= 0:
        return pixmap   # digital silence draws nothing, not a division by zero
    # Half-height off the LAST row, not the row count: at height/2 a full-scale
    # sample lands on y == height, one row outside the pixmap, and Qt clips it
    # away rather than complaining.
    mid = (height - 1) / 2.0
    top = mid - maxs / scale * mid
    bottom = mid - mins / scale * mid
    # Whole columns in one drawLines call: 5000 individual drawLine calls out
    # of a Python loop is most of the cost of drawing at all.
    lines = [QLine(x, int(top[x]), x, int(bottom[x])) for x in range(width)]

    painter = QPainter(pixmap)
    painter.setPen(QPen(QColor("white"), 1))
    painter.drawLines(lines)
    painter.end()
    return pixmap


class DraggableWaveform(QGraphicsObject):
    """Draggable waveform graphics item.

    Two signals, because a drag has two different audiences. `position_changed`
    is the live preview every mouse-move frame — it is what keeps a playing
    transport following the drag, and it also fires when the offset is set
    programmatically. `offset_committed` fires once, on mouse release, and only
    for a drag that actually moved the item: it carries where the drag STARTED
    as well as where it ended, which is the only moment the app knows by how
    much the audio just slid under the show.
    """

    position_changed = pyqtSignal(QPointF)
    offset_committed = pyqtSignal(float, float)   # old_x, new_x (scene px)

    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self.pixmap = pixmap
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.y_pos = 0
        self._press_x = None
        # [(scene_x0, scene_x1, QColor)]: the song sections' colours. Kept in
        # SCENE x, because sections are anchored in timeline time — an offset
        # drag slides the audio under them, and the colours stay put.
        self.tints = []
        # paint() only builds the tinted layer for the part on screen.
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption)

    def set_tints(self, tints):
        self.tints = list(tints)
        self.update()

    def boundingRect(self):
        return QRectF(self.pixmap.rect())

    def paint(self, painter, option, widget):
        painter.fillRect(self.boundingRect(), QColor(50, 50, 100, 200))
        painter.setOpacity(WAVEFORM_OPACITY)
        if not self.tints:
            painter.drawPixmap(self.boundingRect().toRect(), self.pixmap,
                               self.pixmap.rect())
            return
        # The samples take each section's colour and the band behind them
        # keeps its own: recolour a copy of the visible slice through its
        # alpha (SourceIn) rather than painting colour over the whole band.
        shown = option.exposedRect.intersected(self.boundingRect()).toAlignedRect()
        if shown.isEmpty():
            return
        layer = QPixmap(shown.size())
        layer.fill(Qt.GlobalColor.transparent)
        tinter = QPainter(layer)
        tinter.drawPixmap(0, 0, self.pixmap, shown.x(), shown.y(),
                          shown.width(), shown.height())
        tinter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        left = self.x() + shown.x()
        for x0, x1, color in self.tints:
            tinter.fillRect(QRectF(x0 - left, 0, x1 - x0, shown.height()), color)
        tinter.end()
        painter.drawPixmap(shown.topLeft(), layer)

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

    def mousePressEvent(self, event):
        self._press_x = self.x()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        # Let the base class land the move first, then report the whole drag.
        super().mouseReleaseEvent(event)
        press_x, self._press_x = self._press_x, None
        if press_x is not None and self.x() != press_x:
            self.offset_committed.emit(press_x, self.x())


class WaveformTrack(BaseTrack):
    """Track for displaying and managing audio waveforms."""
    
    def __init__(self, timeline_manager, content_scene, header_scene):
        super().__init__(timeline_manager, content_scene, header_scene)
        self.waveform_item = None
        # Song-structure overlay (its items are kept apart from self.items,
        # which holds the waveform pixmap, so a section redraw never disposes it).
        self._tints = []
        self.sections = SongSectionOverlay(timeline_manager, content_scene,
                                           self.set_waveform_tints)
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
        self.sections.clear()
        
    def _audio_info(self, audio_file: str):
        """(duration, PCM) for `audio_file` from the source the app injected.
        A timeline built without one (a bare widget, a test) degrades to a band
        with no waveform rather than raising."""
        source = getattr(self.timeline_manager, "audio_source", None)
        if source is None:
            return None, None
        return source(audio_file)

    def clear_waveform(self):
        """Drop the drawn waveform: the song has no monitored track any more
        (its backing/guide was cleared, or the setlist went empty). Nothing
        else redraws this band, so without it the last song's pixmap stays on
        the scene under a track that no longer exists."""
        if self.waveform_item is None:
            return
        if self.waveform_item.scene():
            self.content_scene.removeItem(self.waveform_item)
        if self.waveform_item in self.items:
            self.items.remove(self.waveform_item)
        self.waveform_item = None

    def draw_waveform(self, audio_file: str, y_offset: int):
        """Draw waveform from audio file."""
        # Preserve any existing horizontal offset (e.g. song-start offset) so
        # redraws triggered by track resizing don't snap the waveform back to x=0.
        preserved_x = self.waveform_item.x() if self.waveform_item else 0.0

        if self.waveform_item:
            self.content_scene.removeItem(self.waveform_item)
            self.waveform_item = None

        try:
            # Duration is a header read and the samples are whatever the audio
            # engine already decoded for playback — nothing is decoded here, so
            # this never blocks the UI thread. The samples can legitimately be
            # missing (the decode this same song change started is still
            # running); the band is laid out anyway and redrawn on arrival.
            song_length_seconds, pcm = self._audio_info(audio_file)
            if not song_length_seconds:
                self.logger.warning(f"No duration for {audio_file}")
                return

            # The scene spans the whole SONG (the longest of its tracks and
            # its MIDI), which the manager owns — this band only draws the one
            # file being monitored, and a short guide must not trim the ruler.
            self.timeline_manager.apply_song_extent()

            # The song sections' header bar has the top of the band.
            waveform_height = max(1, self.get_track_height()
                                  - self.sections.header_height())

            # Only create waveform for actual song duration, not extended space
            waveform_width = int(song_length_seconds * self.timeline_manager.PIXELS_PER_SECOND)
            waveform_pixmap = render_waveform_pixmap(
                pcm, waveform_width, waveform_height)

            self.waveform_item = DraggableWaveform(waveform_pixmap)
            self.waveform_item.set_tints(self._tints)
            self.place_waveform(y_offset)
            self.waveform_item.setX(preserved_x)
            self.waveform_item.setZValue(0.5)  # Keep waveform above grid but below events
            self.waveform_item.position_changed.connect(self._on_waveform_moved)
            self.waveform_item.offset_committed.connect(
                self._on_waveform_committed)
            self.content_scene.addItem(self.waveform_item)
            self.items.append(self.waveform_item)

        except Exception as e:
            self.logger.error(f"Error drawing waveform: {e}")

    def set_waveform_tints(self, tints):
        """Colour the waveform's samples by the song sections over them,
        [(scene_x0, scene_x1, QColor)] — kept for the next pixmap too, since a
        decode or a resize redraws the waveform without redrawing sections."""
        self._tints = list(tints)
        if self.waveform_item is not None:
            self.waveform_item.set_tints(self._tints)

    def place_waveform(self, y_offset: int):
        """Sit the waveform in the band at `y_offset`, below the song
        sections' header bar."""
        if self.waveform_item is not None:
            self.waveform_item.setYPos(y_offset + self.sections.header_height())

    def _on_waveform_moved(self, pos):
        """Live preview of a drag in progress (also fires on a programmatic
        set_waveform_offset). Nothing is persisted from here — that is the
        committed signal's job."""
        offset_sec = pos.x() / self.timeline_manager.PIXELS_PER_SECOND
        self.timeline_manager.waveform_offset_changed.emit(offset_sec)

    def _on_waveform_committed(self, old_x: float, new_x: float):
        """The drag ended: report old and new offsets in seconds."""
        pps = self.timeline_manager.PIXELS_PER_SECOND
        self.timeline_manager.waveform_offset_committed.emit(
            old_x / pps, new_x / pps)

    def draw_song_sections(self, events, y_offset: int):
        """Draw the song-part arrangement over the waveform band. `events` are
        the cached song-part events (already filtered by the manager); the last
        section runs to the song's true end, not the scene's padded one."""
        self.sections.draw(events, y_offset, self.get_track_height(),
                           self.timeline_manager.song_length())

    def redraw(self):
        """Redraw the waveform track."""
        # This will be called by the timeline manager when needed
        pass