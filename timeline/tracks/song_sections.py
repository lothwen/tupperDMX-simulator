"""
Song-structure sections on the waveform band.

A song part (INTRO/VERSE/CHORUS/… — MIDI notes 61-69) is a global verb stored
as ONE point in time: where that section starts. The overlay draws the parts as
translucent, contiguous boxes over the waveform, each running to the next start
(the last to the song's end), so what the user edits is really the strip's
BOUNDARIES. The rules for that — nothing reorders, nothing stacks, what a drop
does — are Qt-free in `timeline.arrangement`.

Each box is a solid header bar carrying the part's name and its length in bars
— so no text sits on the waveform's peaks — with a 1px edge where it starts;
the waveform itself, and not the band behind it, takes the part's colour
(`WaveformTrack.set_waveform_tints`). WAVE_GRIP_* sets how much of the
band's bottom the boxes leave to the waveform, where a drag slides the audio
offset.

A lone box never moves, because the audio under it doesn't either:

* an EDGE drags that one boundary; both neighbours follow live;
* a click selects (Ctrl toggles, Shift extends) and a plain one also seeks;
* a drag across the boxes sweeps a selection, and once two or more are
  selected, dragging any selected box moves them all by one delta;
* a double-click picks the part; right-click is the section menu.

`SongSectionOverlay` owns every item and gesture, since one boundary or group
move reshapes several boxes at once; `SongSectionItem` is a box handing its
events to the overlay. Edits commit once, on release, through the timeline's
song-section handlers (one undo step each), and the redraw they schedule
rebuilds the boxes — the selection is kept by event id across it.
"""

from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional, Set, Tuple

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QIcon,
                         QLinearGradient, QPainter, QPen, QPixmap)
from PyQt6.QtWidgets import (QApplication, QGraphicsItem, QGraphicsLineItem,
                             QGraphicsObject, QMenu)

from core import note_layout
from core.tempo_map import BEATS_PER_BAR
from timeline import arrangement

# Screen px either side of a boundary that grab it rather than the box.
EDGE_PX = 5
# A drop or "add here" this close (screen px) to a start retypes that section
# instead of splitting — never less than a beat, which is the gap a move keeps.
RETYPE_PX = 8
MIN_BOX_WIDTH_PX = 2
# A section: a header bar (name + length); its colour tints the waveform.
HEADER_PX = 18
HEADER_FONT_PX = 10
# The bar's vertical sheen, as QColor.lighter/darker percentages of the part's
# colour. Kept gentle: the name and length are read off this.
HEADER_TOP_LIGHTER = 118
HEADER_BOTTOM_DARKER = 112
# The waveform's colour in a section: the part's colour mixed with white — a
# tint — so a dark part (BRIDGE) still reads against the band.
WAVE_TINT_MIX = 0.00    # share of the part's colour
PREVIEW_TINT_ALPHA = 90      # a drop's ghost is filled, to show what it covers
# How much of the band's bottom the boxes leave to the waveform's own offset
# drag (a ratio of the band, at least the px floor) — 0 and 0: none.
WAVE_GRIP_RATIO = 0.0
MIN_WAVE_GRIP_PX = 0
LABEL_PAD_PX = 6
# Narrower than this a label says nothing (the tooltip still does), and zoomed
# out the neighbours' labels would print over each other.
MIN_LABEL_PX = 16
# Between the name and the length when both fit.
NUMBER_GAP_PX = 8

_SELECTED_PEN = QColor(230, 230, 230)


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def _swatch(color: str) -> QIcon:
    pixmap = QPixmap(12, 12)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)


def _wave_tint(color: QColor) -> QColor:
    """`color` mixed with white by WAVE_TINT_MIX."""
    k = WAVE_TINT_MIX
    return QColor(*(round(c * k + 255 * (1 - k))
                    for c in (color.red(), color.green(), color.blue())))


def _ink(color: QColor) -> QColor:
    """Dark or light text for a `color` ground — the lane clips' brightness
    rule, so a header reads the way a clip of the same colour does."""
    brightness = (color.red() * 0.299 + color.green() * 0.587
                  + color.blue() * 0.114)
    return QColor(20, 20, 20) if brightness >= 128 else QColor(235, 235, 235)


class _HeaderText(QGraphicsObject):
    """The words in a section's header bar: its name on the left and its
    length (bars) on the right, both inside the part of the box on screen.
    Unscaled — it ignores the view's zoom, where the bar itself (drawn by the
    box) stretches with it — and click/hover-through, so the box beneath keeps
    its edges, gestures and tooltip."""

    def __init__(self, z: float):
        super().__init__()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations,
                     True)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(z)
        self.name = ""
        self.number = ""
        self._size = (0.0, 0.0)
        self._fonts = (QFont(), QFont())
        self._ink = QColor()

    def show_text(self, name: str, number: str, width: float, height: float,
                  fonts, ink: QColor):
        """`name` and `number` as they fit `width` device px (either may be
        empty — the caller decides what fits)."""
        self.prepareGeometryChange()
        self.name, self.number = name, number
        self._size = (max(0.0, width), height)
        self._fonts, self._ink = fonts, ink
        self.setVisible(bool(name or number))
        self.update()

    def boundingRect(self):
        return QRectF(0, 0, *self._size)

    def paint(self, painter, option, widget):
        width, height = self._size
        painter.setPen(self._ink)
        middle = Qt.AlignmentFlag.AlignVCenter
        if self.name:
            painter.setFont(self._fonts[0])
            painter.drawText(QRectF(LABEL_PAD_PX, 0, width, height),
                             Qt.AlignmentFlag.AlignLeft | middle, self.name)
        if self.number:
            painter.setFont(self._fonts[1])
            painter.drawText(QRectF(0, 0, width - LABEL_PAD_PX, height),
                             Qt.AlignmentFlag.AlignRight | middle, self.number)


class SongSectionItem(QGraphicsObject):
    """One section box: its header bar, tint and start edge. Selectable and
    focusable (Delete removes the selection); every mouse event is the
    overlay's to interpret. A `ghost` is the drop preview's box — outlined,
    and inert."""

    def __init__(self, overlay: "SongSectionOverlay", index: int,
                 event_id: str, part: str, note: Optional[int], color,
                 ghost: bool = False):
        super().__init__()
        self.overlay = overlay
        self.index = index          # position in time order
        self.event_id = event_id
        self.ghost = ghost
        self.rect = QRectF(0, 0, MIN_BOX_WIDTH_PX, 1)
        self._header = 0.0
        self.length = ""            # in bars, as the header shows it
        self.set_part(part, note, color)

        if ghost:
            self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            return
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(
            Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)

    def set_part(self, part: str, note: Optional[int], color):
        self.part = part
        self.note = note
        self.color = QColor(color)
        self.update()

    def set_geometry(self, x: float, y: float, width: float, height: float,
                     header: float):
        self.prepareGeometryChange()
        self.rect = QRectF(0, 0, max(width, MIN_BOX_WIDTH_PX), height)
        self._header = min(header, height)
        self.setPos(x, y)

    def boundingRect(self):
        return QRectF(0, 0, self.rect.width(), self.rect.height())

    def paint(self, painter, option, widget):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = self.boundingRect()
        if self.ghost:
            fill = QColor(self.color)
            fill.setAlpha(PREVIEW_TINT_ALPHA)
            painter.fillRect(rect, fill)
        header = QRectF(0, 0, rect.width(), self._header)
        sheen = QLinearGradient(0, header.top(), 0, header.bottom())
        sheen.setColorAt(0.0, self.color.lighter(HEADER_TOP_LIGHTER))
        sheen.setColorAt(1.0, self.color.darker(HEADER_BOTTOM_DARKER))
        painter.fillRect(header, QBrush(sheen))
        edge = QPen(self.color)
        edge.setCosmetic(True)
        painter.setPen(edge)
        painter.drawLine(QPointF(0, 0), QPointF(0, rect.height()))
        if self.ghost:
            outline = QPen(_SELECTED_PEN, 1.5, Qt.PenStyle.DashLine)
        elif self.isSelected():
            outline = QPen(_SELECTED_PEN, 2)
        else:
            return
        outline.setCosmetic(True)
        painter.setPen(outline)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)

    def mousePressEvent(self, event):
        self.overlay.press(self, event)
        event.accept()

    def mouseMoveEvent(self, event):
        self.overlay.move(event)
        event.accept()

    def mouseReleaseEvent(self, event):
        self.overlay.release(event)
        event.accept()

    def mouseDoubleClickEvent(self, event):
        self.overlay.double_click(self, event)
        event.accept()

    def hoverMoveEvent(self, event):
        self.overlay.hover(self, event.pos().x())
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        self.overlay.hover(None, 0.0)
        super().hoverLeaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.overlay.delete_selected()
            event.accept()
            return
        super().keyPressEvent(event)


@dataclass
class _Gesture:
    """One left-button press on a box, from press to release."""
    kind: str                    # "edge" | "group" | "click" (→ "sweep")
    box: SongSectionItem
    press_x: float               # scene x
    press_screen: QPoint
    modifiers: Qt.KeyboardModifier
    boundary: Optional[int] = None       # edge: the start being dragged
    base: FrozenSet[str] = frozenset()   # sweep: the selection it adds to
    started: bool = False
    proposed: List[float] = field(default_factory=list)


class SongSectionOverlay:
    """The arrangement strip on the waveform band: its boxes, their labels,
    the drop preview, and every gesture that edits them."""

    def __init__(self, timeline, scene, tint_waveform):
        self.tm = timeline
        self.scene = scene
        # Hands the waveform [(scene_x0, scene_x1, QColor)] to colour itself.
        self._tint_waveform = tint_waveform
        self.boxes: List[SongSectionItem] = []   # time order
        self._labels: List[_HeaderText] = []
        self._starts: List[float] = []           # committed, same order
        self._song_end = 0.0
        self._y = 0.0
        self._band_height = 0
        self._height = 0
        self._header = HEADER_PX
        self._fonts = (QFont(), QFont())   # name, length — sized in draw()
        self._selected_ids: Set[str] = set()
        self._anchor_id: Optional[str] = None
        self._gesture: Optional[_Gesture] = None
        self._rebuilding = False
        self._stale = []  # disposed items, kept alive until the next rebuild
        self._preview_box: Optional[SongSectionItem] = None
        self._preview_line: Optional[QGraphicsLineItem] = None
        self._preview_label: Optional[_HeaderText] = None

        scene.selectionChanged.connect(self._on_scene_selection)
        # Labels pin to the visible left of their section, so they follow a
        # scroll (a zoom reaches refit_labels through the timeline).
        timeline.content_view.horizontalScrollBar().valueChanged.connect(
            self.refit_labels)

    # --- Building ---------------------------------------------------------

    def draw(self, events, y: float, band_height: int, song_end: float):
        """Rebuild the strip from the song-part `events` (any order)."""
        self._rebuilding = True
        try:
            self._dispose()
            self._gesture = None
            ordered = sorted(events, key=lambda e: e['time'])
            self._y = y
            self._band_height = band_height
            grip = max(MIN_WAVE_GRIP_PX, round(band_height * WAVE_GRIP_RATIO))
            self._height = max(1, band_height - grip)
            self._song_end = song_end
            self._size_header()
            self._starts = [float(e['time']) for e in ordered]
            for i, ev in enumerate(ordered):
                info = ev.get('info')
                box = SongSectionItem(
                    self, i, ev['id'],
                    getattr(info, 'part', f"Note {ev.get('note')}"),
                    ev.get('note'), getattr(info, 'color', '#888888'))
                box.setZValue(1)  # above the waveform pixmap (z=0.5)
                self.scene.addItem(box)
                self.boxes.append(box)
                label = _HeaderText(2)
                self.scene.addItem(label)
                self._labels.append(label)
            self._layout(self._starts)
            for box in self.boxes:
                box.setSelected(box.event_id in self._selected_ids)
                box.setToolTip(self._describe(box.index))
            self._selected_ids = {b.event_id for b in self.selected_boxes()}
        finally:
            self._rebuilding = False

    def clear(self):
        """Drop every item, the drop preview included."""
        self._dispose()
        self._stale = []
        for item in (self._preview_box, self._preview_line,
                     self._preview_label):
            if item is not None and item.scene():
                item.scene().removeItem(item)
        self._preview_box = self._preview_line = self._preview_label = None
        self._tint_waveform([])

    def _dispose(self):
        """Remove the boxes defensively (see MidiEventsTrack.redraw_events), so
        the scene's index never references freed memory."""
        stale = self.boxes + self._labels
        for item in stale:
            item.setVisible(False)
            item.setEnabled(False)
            item.prepareGeometryChange()
            if item.scene():
                item.scene().removeItem(item)
            item.deleteLater()
        self.boxes = []
        self._labels = []
        self._stale = stale

    def header_height(self) -> int:
        """The header bar's height at the timeline's UI scale — the strip at
        the top of the band that the waveform is drawn below, so no bar ever
        covers a peak."""
        return round(HEADER_PX * getattr(self.tm, "_ui_scale", 1.0))

    def _size_header(self):
        """The header bar and its fonts, at the timeline's UI scale."""
        scale = getattr(self.tm, "_ui_scale", 1.0)
        self._header = self.header_height()
        name, number = QFont(), QFont()
        for font in (name, number):
            font.setPixelSize(round(HEADER_FONT_PX * scale))
        name.setBold(True)
        name.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6 * scale)
        self._fonts = (name, number)

    def _layout(self, starts: List[float]):
        """Draw the strip as `starts` would make it — the committed starts, or a
        gesture's proposal while it is in progress (lengths included)."""
        pps = self.tm.PIXELS_PER_SECOND
        tints = []
        for box, (start, end) in zip(self.boxes,
                                     arrangement.spans(starts, self._song_end)):
            box.set_geometry(start * pps, self._y, (end - start) * pps,
                             self._height, self._header)
            box.length = self._bars(start, end)
            tints.append((start * pps, end * pps, _wave_tint(box.color)))
        self._tint_waveform(tints)
        self.refit_labels()

    def _bars(self, start: float, end: float) -> str:
        """A section's length in bars, to the nearest beat — markers in a real
        file sit a few ms off the grid, and 7.99 bars is 8 to a musician. Whole
        bars bare, a part bar in quarters (8.25 = 8 bars and a beat)."""
        tmap = self.tm.tempo_map
        beats = round(tmap.beat_at(end) - tmap.beat_at(start))
        return f"{beats / BEATS_PER_BAR:g}"

    def refit_labels(self, *_):
        """Fit each header's words to the part of its box on screen: the name
        pinned to the visible left, the length to the visible right, the length
        dropped first when both do not fit and the name elided after that."""
        if not self.boxes:
            return
        view = self.tm.content_view
        zoom = view.transform().m11() or 1.0
        view_left = view.mapToScene(0, 0).x()
        view_right = view.mapToScene(view.viewport().width(), 0).x()
        for box, label in zip(self.boxes, self._labels):
            x0 = box.pos().x()
            left = max(x0, view_left)
            right = min(x0 + box.rect.width(), view_right)
            self._fit_header(label, box.part, box.length, box.color,
                             (right - left) * zoom)
            label.setPos(left, self._y)

    def _fit_header(self, label: _HeaderText, name: str, number: str,
                    color: QColor, width: float):
        room = int(width) - 2 * LABEL_PAD_PX
        name_metrics = QFontMetrics(self._fonts[0])
        both = (name_metrics.horizontalAdvance(name) + NUMBER_GAP_PX
                + QFontMetrics(self._fonts[1]).horizontalAdvance(number))
        if both > room:
            number = ""
            name = (name_metrics.elidedText(name, Qt.TextElideMode.ElideRight,
                                            room)
                    if room >= MIN_LABEL_PX else "")
        label.show_text(name, number, width, self._header, self._fonts,
                        _ink(color))

    def _describe(self, index: int) -> str:
        start, end = arrangement.spans(self._starts, self._song_end)[index]
        tmap = self.tm.tempo_map
        # A hair past the boundary: a start exactly on a bar line must not
        # floor to the beat before it.
        bar0 = "%d.%d" % tmap.bar_beat_at(start + 1e-6)
        bar1 = "%d.%d" % tmap.bar_beat_at(end + 1e-6)
        return (f"{self.boxes[index].part} · bar {bar0} – {bar1} · "
                f"{_clock(start)} – {_clock(end)} ({end - start:.1f} s)")

    # --- Time helpers -----------------------------------------------------

    def _zoom(self) -> float:
        return self.tm.content_view.transform().m11() or 1.0

    def _min_gap(self) -> float:
        """The shortest a section may get: one beat."""
        return self.tm.tempo_map.shortest_bar_seconds() / BEATS_PER_BAR

    def _snap(self, seconds: float, whole_bar: bool) -> float:
        """The nearest beat — the nearest bar line with `whole_bar` (Shift) —
        in either ruler mode, since the tempo map is the song's beat grid
        whatever the ruler draws. With the toolbar's Snap off, no snapping."""
        if not self.tm.snap_enabled:
            return seconds
        return self.tm.tempo_map.nearest_grid_seconds(
            seconds, 1 if whole_bar else BEATS_PER_BAR)

    def _bar_beat(self, seconds: float) -> str:
        return "%d.%d" % self.tm.tempo_map.bar_beat_at(seconds + 1e-6)

    def _drop_target(self, seconds: float) -> Optional[Tuple[str, int]]:
        """What a part landing at `seconds` does (see arrangement.resolve_drop),
        or None past the point where a last section would have no room."""
        if seconds > self._song_end - self._min_gap():
            return None
        radius = max(self._min_gap(),
                     RETYPE_PX / (self._zoom() * self.tm.PIXELS_PER_SECOND))
        return arrangement.resolve_drop(self._starts, max(0.0, seconds),
                                        radius)

    # --- Selection --------------------------------------------------------

    def selected_boxes(self) -> List[SongSectionItem]:
        return [b for b in self.boxes if b.isSelected()]

    def select_all(self):
        for box in self.boxes:
            box.setSelected(True)

    def _select_only(self, box: SongSectionItem):
        self.scene.clearSelection()
        box.setSelected(True)
        self._anchor_id = box.event_id

    def _on_scene_selection(self):
        if self._rebuilding:
            return
        try:
            self._selected_ids = {b.event_id for b in self.selected_boxes()}
        except RuntimeError:
            # A QGraphicsScene deletes its items and THEN clears its selection
            # as it is destroyed (see StageController._on_selection_changed):
            # nothing is left to ask, and there is no selection to keep.
            self.boxes = []

    def _click(self, box: SongSectionItem, modifiers, scene_x: float):
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            box.setSelected(not box.isSelected())
            self._anchor_id = box.event_id
            return
        anchor = next((b for b in self.boxes if b.event_id == self._anchor_id),
                      None)
        if modifiers & Qt.KeyboardModifier.ShiftModifier and anchor is not None:
            lo, hi = sorted((anchor.index, box.index))
            self.scene.clearSelection()
            for b in self.boxes[lo:hi + 1]:
                b.setSelected(True)
            return
        self._select_only(box)
        self.tm._seek_to_scene_x(scene_x)

    # --- Gestures ---------------------------------------------------------

    def _boundary_at(self, box: SongSectionItem, local_x: float) -> Optional[int]:
        """The start a press at `local_x` in `box` grabs: its own on the left
        edge, the next box's on the right edge (the last box's right edge is the
        song's end, which is not a boundary), else None."""
        edge = EDGE_PX / self._zoom()
        if local_x <= edge:
            return box.index
        if (local_x >= box.rect.width() - edge
                and box.index + 1 < len(self.boxes)):
            return box.index + 1
        return None

    def hover(self, box: Optional[SongSectionItem], local_x: float):
        # Set the cursor on the view (not the viewport) to match the MIDI clips
        # / fade handles. Touching viewport().setCursor() would give the
        # viewport its own cursor and stop it inheriting the view's, silently
        # killing the clips' resize/move cursors afterwards.
        if self._gesture is not None:
            return
        cursor = Qt.CursorShape.ArrowCursor
        if box is not None:
            if self._boundary_at(box, local_x) is not None:
                cursor = Qt.CursorShape.SizeHorCursor
            elif box.isSelected() and len(self.selected_boxes()) >= 2:
                cursor = Qt.CursorShape.OpenHandCursor
        self.tm.content_view.setCursor(cursor)

    def press(self, box: SongSectionItem, event):
        if event.button() == Qt.MouseButton.RightButton:
            if not box.isSelected():
                self._select_only(box)
            self._section_menu(box, event.scenePos().x(),
                               event.screenPos())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        boundary = self._boundary_at(box, event.pos().x())
        if boundary is not None:
            kind = "edge"
        elif box.isSelected() and len(self.selected_boxes()) >= 2:
            kind = "group"
        else:
            kind = "click"
        self._gesture = _Gesture(kind, box, event.scenePos().x(),
                                 event.screenPos(), event.modifiers(), boundary)

    def move(self, event):
        g = self._gesture
        if g is None:
            return
        if not g.started:
            travel = (event.screenPos() - g.press_screen).manhattanLength()
            if travel < QApplication.startDragDistance():
                return
            g.started = True
            if g.kind == "click":
                g.kind = "sweep"
                if g.modifiers & Qt.KeyboardModifier.ControlModifier:
                    g.base = frozenset(self._selected_ids)
                else:
                    self.scene.clearSelection()
            elif g.kind == "group":
                self.tm.content_view.setCursor(Qt.CursorShape.ClosedHandCursor)
        x = event.scenePos().x()
        dx = (x - g.press_x) / self.tm.PIXELS_PER_SECOND
        whole_bar = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if g.kind == "edge":
            self._drag_boundary(g, dx, whole_bar, event)
        elif g.kind == "group":
            self._drag_group(g, dx, whole_bar, event)
        else:
            lo, hi = sorted((g.press_x, x))
            wanted = g.base | {b.event_id for b in self.boxes
                               if b.pos().x() < hi
                               and b.pos().x() + b.rect.width() > lo}
            for b in self.boxes:
                b.setSelected(b.event_id in wanted)

    def _drag_boundary(self, g: _Gesture, dx: float, whole_bar: bool, event):
        b = g.boundary
        t0 = self._starts[b]
        lo, hi = arrangement.move_bounds(self._starts, [b], self._song_end,
                                         self._min_gap())
        t = min(max(self._snap(t0 + dx, whole_bar), t0 + lo), t0 + hi)
        g.proposed = list(self._starts)
        g.proposed[b] = t
        self._layout(g.proposed)
        self._readout(event, f"{self.boxes[b].part} → bar {self._bar_beat(t)}"
                             f" · {_clock(t)}")

    def _drag_group(self, g: _Gesture, dx: float, whole_bar: bool, event):
        moving = [b.index for b in self.selected_boxes()]
        t0 = self._starts[g.box.index]
        lo, hi = arrangement.move_bounds(self._starts, moving, self._song_end,
                                         self._min_gap())
        delta = min(max(self._snap(t0 + dx, whole_bar) - t0, lo), hi)
        g.proposed = [t + delta if i in moving else t
                      for i, t in enumerate(self._starts)]
        self._layout(g.proposed)
        self._readout(event, f"{len(moving)} sections · {g.box.part} → bar "
                             f"{self._bar_beat(t0 + delta)} ({delta:+.2f} s)")

    def _readout(self, event, text: str):
        pos = event.screenPos() + QPoint(12, 20)
        self.tm.clip_tooltip().show_at(pos, text)

    def release(self, event):
        g, self._gesture = self._gesture, None
        if g is None:
            return
        self.tm.hide_clip_tooltip()
        self.hover(g.box, event.pos().x())
        if not g.started:
            self._click(g.box, g.modifiers, g.press_x)
            return
        if g.kind == "sweep":
            self._anchor_id = g.box.event_id
            return
        moves = {self.boxes[i].event_id: t
                 for i, t in enumerate(g.proposed)
                 if abs(t - self._starts[i]) > 1e-6}
        if moves:
            self.tm.on_song_sections_moved(moves)
        else:
            self._layout(self._starts)

    def double_click(self, box: SongSectionItem, event):
        self._gesture = None
        if event.button() != Qt.MouseButton.LeftButton:
            return
        menu = QMenu(self.tm.content_view)
        self._add_parts(menu, "retype", box.note)
        event_id = box.event_id
        chosen = menu.exec(event.screenPos())
        if chosen is not None:
            self.tm.on_song_sections_retyped([event_id], chosen.data()[1])

    # --- Keyboard ---------------------------------------------------------

    def delete_selected(self):
        ids = [b.event_id for b in self.selected_boxes()]
        if ids:
            self.tm.on_song_sections_deleted(ids)

    def nudge_selected(self, direction: int, fine: bool = False) -> bool:
        """Move every selected start one bar (a beat with `fine`) the way
        `direction` points — all of it, or not at all when a neighbour is in the
        way. False when no section is selected."""
        moving = [b.index for b in self.selected_boxes()]
        if not moving:
            return False
        tmap = self.tm.tempo_map
        t0 = self._starts[moving[0]]
        step = 1 if fine else BEATS_PER_BAR
        delta = tmap.seconds_at_beat(tmap.beat_at(t0) + direction * step) - t0
        lo, hi = arrangement.move_bounds(self._starts, moving, self._song_end,
                                         self._min_gap())
        if lo - 1e-9 <= delta <= hi + 1e-9:
            self.tm.on_song_sections_moved(
                {self.boxes[i].event_id: self._starts[i] + delta
                 for i in moving})
        return True

    # --- Menus ------------------------------------------------------------

    def _add_parts(self, menu: QMenu, verb: str, checked_note=None):
        """One action per song part, in its colour; each carries (verb, note)."""
        for k, name in enumerate(note_layout.SONG_PARTS):
            note = note_layout.SONG_PART_BASE + k
            info = note_layout.describe_note(note)
            action = menu.addAction(_swatch(info.color), name)
            action.setData((verb, note))
            if checked_note is not None:
                action.setCheckable(True)
                action.setChecked(note == checked_note)

    def _section_menu(self, box: SongSectionItem, scene_x: float,
                      global_pos: QPoint):
        """Everything is read off `box` BEFORE exec(): the menu's event loop can
        run a scheduled redraw, which frees the box."""
        targets = self.selected_boxes() if box.isSelected() else [box]
        ids = [b.event_id for b in targets]
        notes = {b.note for b in targets}
        start = self._starts[box.index]
        split_at = self._snap(scene_x / self.tm.PIXELS_PER_SECOND, False)
        split = self._drop_target(split_at)

        menu = QMenu(self.tm.content_view)
        self._add_parts(menu.addMenu("Change to"), "retype",
                        next(iter(notes)) if len(notes) == 1 else None)
        split_menu = menu.addMenu("Split here")
        self._add_parts(split_menu, "split")
        split_menu.setEnabled(split is not None and split[0] == "insert")
        menu.addAction(f"Play from '{box.part}'").setData(("play", None))
        menu.addSeparator()
        menu.addAction("Select all sections").setData(("all", None))
        if len(targets) > 1:
            delete_text = f"Delete {len(targets)} sections"
        elif box.index == 0:
            delete_text = f"Delete '{box.part}'"
        else:
            delete_text = (f"Delete '{box.part}' "
                           f"({self.boxes[box.index - 1].part} extends)")
        menu.addAction(delete_text).setData(("delete", None))

        chosen = menu.exec(global_pos)
        if chosen is None or chosen.data() is None:
            return
        verb, note = chosen.data()
        if verb == "retype":
            self.tm.on_song_sections_retyped(ids, note)
        elif verb == "split":
            self.tm.add_song_section(note, split_at)
        elif verb == "play":
            self.tm.play_from_requested.emit(start)
        elif verb == "all":
            self.select_all()
        elif verb == "delete":
            self.tm.on_song_sections_deleted(ids)

    def band_menu(self, scene_x: float, global_pos: QPoint):
        """Right-click on the band outside a box: add a section there (or, on
        a start, change the section starting there)."""
        at = self._snap(max(0.0, scene_x / self.tm.PIXELS_PER_SECOND), False)
        target = self._drop_target(at)
        if target is None:
            return
        kind, i = target
        menu = QMenu(self.tm.content_view)
        if kind == "retype":
            event_id = self.boxes[i].event_id
            self._add_parts(menu.addMenu(f"Change '{self.boxes[i].part}' to"),
                            "retype", self.boxes[i].note)
        else:
            self._add_parts(menu.addMenu("Add section here"), "split")
        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        _verb, note = chosen.data()
        if kind == "retype":
            self.tm.on_song_sections_retyped([event_id], note)
        else:
            self.tm.add_song_section(note, at)

    # --- Dropping a part from the clip library ----------------------------

    def preview_drop(self, note: int, scene_x: float, whole_bar: bool) -> bool:
        """Show where a part dragged at `scene_x` would land — a new boundary
        and the section it starts, or the section it would retype. False (and
        no preview) where it cannot land."""
        at = self._snap(max(0.0, scene_x / self.tm.PIXELS_PER_SECOND), whole_bar)
        target = self._drop_target(at)
        if target is None:
            self.clear_drop_preview()
            return False
        kind, i = target
        start = self._starts[i] if kind == "retype" else at
        following = i + 1 if kind == "retype" else i
        end = (self._starts[following] if following < len(self._starts)
               else self._song_end)
        self._show_preview(note, start, end, split=kind == "insert")
        return True

    def apply_drop(self, note: int, velocity: int, scene_x: float,
                   whole_bar: bool) -> bool:
        """Land a dropped part exactly where preview_drop showed it."""
        self.clear_drop_preview()
        at = self._snap(max(0.0, scene_x / self.tm.PIXELS_PER_SECOND), whole_bar)
        target = self._drop_target(at)
        if target is None:
            return False
        kind, i = target
        if kind == "retype":
            self.tm.on_song_sections_retyped([self.boxes[i].event_id], note)
        else:
            self.tm.add_song_section(note, at, velocity)
        return True

    def _show_preview(self, note: int, start: float, end: float, split: bool):
        if self._preview_box is None:
            self._preview_box = SongSectionItem(self, -1, "", "", None,
                                                "#888888", ghost=True)
            self._preview_box.setZValue(3)
            self._preview_line = QGraphicsLineItem()
            self._preview_line.setZValue(3)
            line_pen = QPen(_SELECTED_PEN, 2)
            line_pen.setCosmetic(True)
            self._preview_line.setPen(line_pen)
            self._preview_label = _HeaderText(4)
            for item in (self._preview_box, self._preview_line,
                         self._preview_label):
                item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                self.scene.addItem(item)
        info = note_layout.describe_note(note)
        pps = self.tm.PIXELS_PER_SECOND
        box = self._preview_box
        box.set_part(info.part, note, info.color)
        box.set_geometry(start * pps, self._y, (end - start) * pps,
                         self._height, self._header)
        x = start * pps
        self._preview_line.setLine(x, self._y, x, self._y + self._band_height)
        self._preview_line.setVisible(split)
        self._fit_header(self._preview_label, info.part,
                         self._bars(start, end), box.color,
                         box.rect.width() * self._zoom())
        self._preview_label.setPos(x, self._y)
        box.setVisible(True)
        # The ghost's label would print over the one it covers.
        self.refit_labels()
        for label in self._labels:
            if start * pps <= label.pos().x() < end * pps:
                label.setVisible(False)

    def clear_drop_preview(self):
        for item in (self._preview_box, self._preview_line,
                     self._preview_label):
            if item is not None:
                item.setVisible(False)
        self.refit_labels()
