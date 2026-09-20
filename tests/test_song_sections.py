"""
Song sections on the waveform band: the arrangement rules and the gestures.

Locks the overlay's contract:
  * a section is a START marker; the strip is contiguous, the last section
    ending at the song's true end (not the scene's 240 s floor);
  * each box is a header bar — name left, length in bars right, both fitted to
    what is on screen — that never blocks the gestures on the box beneath;
  * an edge drags one boundary, live, clamped so sections never reorder or
    stack; a lone box's body never moves;
  * everything snaps to beats, to whole bars with Shift, and not at all with
    the toolbar's Snap off;
  * a click selects and seeks, a sweep selects several, and dragging a body of
    a 2+ selection moves them all by one delta — one undo step;
  * a dropped part splits the section it lands in, or retypes the one starting
    there, and never lands past the song's end;
  * Delete removes the selection unconfirmed; the selection survives redraws.

Run with:
    uv run python tests/test_song_sections.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QColor, QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import QApplication

from core import note_layout
from midi.manager import MidiManager
from timeline import arrangement
from timeline.manager import Timeline
from timeline.tracks import song_sections
from timeline.tracks.song_sections import SongSectionItem

INTRO, VERSE, CHORUS, BREAK, BRIDGE, SOLO = 61, 62, 63, 64, 65, 67
SONG_LENGTH = 64.0   # at the default 120 BPM a bar is 2 s


class _Fixture:
    def __init__(self, type_, dmx_start, channel):
        self.type, self.dmx_start, self.channel, self.name = (
            type_, dmx_start, channel, "")


PATCH = [_Fixture("ledbar", 1, 0)]


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


_APP = None


def _make(parts=((INTRO, 2.0), (VERSE, 10.0), (CHORUS, 20.0))):
    """A shown timeline on a 64 s song carrying `parts` [(note, time)]."""
    global _APP
    _APP = QApplication.instance() or QApplication([])
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    tl = Timeline(midi_manager=m, fixtures=PATCH)
    tl.set_audio_source(lambda path: (SONG_LENGTH, None))
    tl.set_song_tracks(["/tmp/backing.wav"])
    for note, when in parts:
        m.add_event(note, when, 127)
    tl.resize(1400, 400)
    tl.show()
    tl.set_zoom(1.0)
    tl.refresh_from_manager()
    return m, tl


def _starts(m):
    return [(e['note'], round(e['time'], 3))
            for e in sorted(m.midi_events, key=lambda e: e['time'])
            if m.is_song_part(e)]


def _overlay(tl):
    return tl.waveform_track.sections


def _box(tl, note):
    return next(b for b in _overlay(tl).boxes if b.note == note)


def _box_y(tl):
    return tl._waveform_y() + 10


def _send(tl, kind, seconds, y, buttons=None,
          mods=Qt.KeyboardModifier.NoModifier):
    view = tl.content_view
    local = QPointF(view.mapFromScene(
        QPointF(seconds * tl.PIXELS_PER_SECOND, y)))
    button = (Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove
              else Qt.MouseButton.LeftButton)
    if buttons is None:
        buttons = (Qt.MouseButton.LeftButton
                   if kind == QEvent.Type.MouseButtonPress
                   else Qt.MouseButton.NoButton)
    event = QMouseEvent(kind, local, view.viewport().mapToGlobal(local),
                        button, buttons, mods)
    QApplication.sendEvent(view.viewport(), event)


def _settle(tl):
    """Run the redraw an edit scheduled — directly, never by pumping the event
    loop: once tests/test_headless_smoke.py has run in this process, Qt's
    posted-event delivery segfaults (see test_prefs_reload._in_subprocess)."""
    if tl._refresh_pending:
        tl._do_deferred_refresh()


def _drag(tl, from_s, to_s, y=None, mods=Qt.KeyboardModifier.NoModifier,
          during=None):
    y = _box_y(tl) if y is None else y
    _send(tl, QEvent.Type.MouseButtonPress, from_s, y, mods=mods)
    for k in range(1, 7):
        _send(tl, QEvent.Type.MouseMove, from_s + (to_s - from_s) * k / 6, y,
              buttons=Qt.MouseButton.LeftButton, mods=mods)
    if during is not None:
        during()
    _send(tl, QEvent.Type.MouseButtonRelease, to_s, y, mods=mods)
    _settle(tl)


def _click(tl, seconds, mods=Qt.KeyboardModifier.NoModifier):
    _send(tl, QEvent.Type.MouseButtonPress, seconds, _box_y(tl), mods=mods)
    _send(tl, QEvent.Type.MouseButtonRelease, seconds, _box_y(tl), mods=mods)
    _settle(tl)


def _edge_px(tl):
    """One screen px, in seconds, at the current zoom."""
    return 1.0 / (tl.content_view.transform().m11() * tl.PIXELS_PER_SECOND)


# --- the Qt-free rules ---------------------------------------------------------

def test_spans_run_to_the_next_start():
    print("test_spans_run_to_the_next_start")
    _assert(arrangement.spans([2.0, 10.0, 20.0], 64.0)
            == [(2.0, 10.0), (10.0, 20.0), (20.0, 64.0)],
            "each section runs to the next start, the last to the song end")
    _assert(arrangement.spans([70.0], 64.0) == [(70.0, 70.0)],
            "a start past the end is an empty span, not a negative one")


def test_move_bounds_hold_a_boundary_between_its_neighbours():
    print("test_move_bounds_hold_a_boundary_between_its_neighbours")
    starts = [0.0, 10.0, 20.0, 30.0]
    _assert(arrangement.move_bounds(starts, [1], 64.0, 0.5) == (-9.5, 9.5),
            "one start moves up to a beat short of either neighbour")
    _assert(arrangement.move_bounds(starts, [1, 2], 64.0, 0.5) == (-9.5, 9.5),
            "a group is bounded by the unmoved starts around it only")
    _assert(arrangement.move_bounds([2.0, 10.0], [0], 64.0, 0.5)[0] == -2.0,
            "nothing moves before t=0")
    _assert(arrangement.move_bounds([2.0, 60.0], [1], 64.0, 0.5)[1] == 3.5,
            "the last start keeps a beat before the song's end")
    lo, hi = arrangement.move_bounds([5.0, 5.1], [1], 64.0, 0.5)
    _assert(lo == 0.0 and hi > 0.0,
            "an already-crowded start may move away but never closer")


def test_resolve_drop_splits_or_retypes():
    print("test_resolve_drop_splits_or_retypes")
    starts = [0.0, 10.0, 20.0]
    _assert(arrangement.resolve_drop(starts, 15.0, 0.5) == ("insert", 2),
            "inside a section: a new start splits it")
    _assert(arrangement.resolve_drop(starts, 10.2, 0.5) == ("retype", 1),
            "on a start: the section starting there changes part")
    _assert(arrangement.resolve_drop([], 3.0, 0.5) == ("insert", 0),
            "an empty song takes its first section")


# --- the overlay ------------------------------------------------------------------

def test_each_box_is_a_header_bar_ending_with_the_song():
    print("test_each_box_is_a_header_bar_ending_with_the_song")
    _, tl = _make()
    band_top = tl._waveform_y()
    overlay = _overlay(tl)
    boxes = overlay.boxes
    _assert(len(boxes) == 3, "three markers -> three boxes")
    _assert(all(b.pos().y() == band_top for b in boxes),
            "the boxes are aligned to the top of the band")
    headers = [(h.name, h.number) for h in overlay._labels]
    _assert(headers == [("INTRO", "4"), ("VERSE", "5"), ("CHORUS", "22")],
            f"each header names its part and its length in bars ({headers})")
    _assert(all(h.pos().y() == band_top for h in overlay._labels),
            "the words sit in the header bar at the top of the box")
    tl.set_files("/tmp/backing.wav")   # lays the waveform out (no samples)
    wave = tl.waveform_track.waveform_item
    header = overlay.header_height()
    _assert(wave.y() == band_top + header
            and wave.boundingRect().height()
            == tl.waveform_track_height - header,
            "the waveform fills the band below the header bar, which covers "
            "none of it")
    last = boxes[-1]
    right = (last.pos().x() + last.rect.width()) / tl.PIXELS_PER_SECOND
    _assert(abs(right - SONG_LENGTH) < 0.01,
            f"the last section ends with the song ({right:.2f}s), not at 240s")


def test_the_header_bar_has_a_vertical_sheen():
    print("test_the_header_bar_has_a_vertical_sheen")
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QImage, QPainter
    _, tl = _make()
    overlay = _overlay(tl)
    image = QImage(tl.content_scene.sceneRect().size().toSize(),
                   QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    tl.content_scene.render(painter, QRectF(image.rect()),
                            tl.content_scene.sceneRect())
    painter.end()

    box = _box(tl, CHORUS)
    x = int(box.pos().x() + box.rect.width() / 2)   # clear of name and length
    top = image.pixelColor(x, int(box.pos().y() + 1))
    bottom = image.pixelColor(x, int(box.pos().y()
                                    + overlay.header_height() - 2))
    _assert(top.lightness() > bottom.lightness() + 10,
            f"the bar is lighter at the top than at the bottom "
            f"({top.lightness()} vs {bottom.lightness()})")
    _assert(abs(top.hue() - box.color.hue()) <= 2
            and abs(bottom.hue() - box.color.hue()) <= 2,
            "and keeps the part's own hue all the way down")


def test_header_words_fit_read_and_let_the_box_work():
    print("test_header_words_fit_read_and_let_the_box_work")
    # INTRO 60px wide, VERSE 10px, at the default zoom.
    _, tl = _make(((INTRO, 2.0), (VERSE, 5.0), (BREAK, 5.5), (CHORUS, 20.0)))
    overlay = _overlay(tl)
    zoom = tl.content_view.transform().m11()
    intro, verse = overlay._labels[0], overlay._labels[1]
    _assert(intro.isVisible() and intro.name and intro.number == "",
            f"a 60px header keeps its name and drops the length "
            f"({intro.name!r} {intro.number!r})")
    _assert(not verse.isVisible(),
            "a 10px header is too narrow for any words")
    _assert(song_sections._ink(QColor("#37c737")).lightness() < 128
            and song_sections._ink(QColor("#461c64")).lightness() > 128,
            "dark words on a bright part, light words on a dark one")
    chorus = _box(tl, CHORUS)
    header = overlay._labels[chorus.index]
    view = tl.content_view
    right = view.mapToScene(view.viewport().width(), 0).x()
    _assert(header.boundingRect().width() <= chorus.rect.width() * zoom + 1
            and header.pos().x() + header.boundingRect().width() / zoom
            <= right + 1,
            "the words never spill past their own box, nor off the screen")

    # Over the header words, on CHORUS's left edge: the box still owns hover.
    _send(tl, QEvent.Type.MouseMove, 20.0 + _edge_px(tl), tl._waveform_y() + 6)
    _assert(tl.content_view.cursor().shape() == Qt.CursorShape.SizeHorCursor,
            "hovering the edge through the header shows the resize cursor")
    _assert(chorus.toolTip().startswith("CHORUS"),
            "and the box, not its header words, carries the tooltip")


def test_the_waveform_takes_the_section_colour_and_the_band_does_not():
    print("test_the_waveform_takes_the_section_colour_and_the_band_does_not")
    import numpy as np
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QImage, QPainter
    from timeline.tracks.waveform import WAVEFORM_OPACITY as alpha
    _, tl = _make()
    # Quiet everywhere but 50-60 s, so at 30 s the samples fill only the
    # middle of the waveform and its top rows are the band behind it.
    rate = 1000
    t = np.arange(int(SONG_LENGTH * rate)) / rate
    loud = np.where((t >= 50) & (t < 60), 1.0, 0.3)
    tl.set_audio_source(lambda path: (SONG_LENGTH,
                                      np.sin(2 * np.pi * 7 * t) * loud))
    tl.set_files("/tmp/backing.wav")

    # Half colour, half white, whatever the shipped mix is tuned to: this is
    # about the samples taking the section's colour, not how strong it is set.
    mix, song_sections.WAVE_TINT_MIX = song_sections.WAVE_TINT_MIX, 0.5
    try:
        tl.refresh_from_manager()        # recolours the waveform
        wave = tl.waveform_track.waveform_item
        image = QImage(tl.content_scene.sceneRect().size().toSize(),
                       QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        tl.content_scene.render(painter, QRectF(image.rect()),
                                tl.content_scene.sceneRect())
        painter.end()
        tint = song_sections._wave_tint(_box(tl, CHORUS).color).getRgb()[:3]
    finally:
        song_sections.WAVE_TINT_MIX = mix

    pps = tl.PIXELS_PER_SECOND
    mid = int(wave.y() + wave.boundingRect().height() / 2)
    top = int(wave.y() + 3)
    lead_in, chorus = int(1.25 * pps), int(30.25 * pps)   # off the grid lines
    rgb = lambda x, y: image.pixelColor(x, y).getRgb()[:3]
    _assert(rgb(chorus, top) == rgb(lead_in, top),
            "the band behind the samples keeps its colour inside a section")
    ground = rgb(chorus, top)
    want = [(1 - alpha) * g + alpha * c for g, c in zip(ground, tint)]
    got = rgb(chorus, mid)
    _assert(all(abs(a - b) <= 3 for a, b in zip(got, want)),
            f"the samples take CHORUS's tint ({got} vs {tuple(map(round, want))})")
    _assert(rgb(lead_in, mid) != got,
            "and before the first section they keep their own colour")


def test_an_edge_drags_one_boundary_live_snapped_and_clamped():
    print("test_an_edge_drags_one_boundary_live_snapped_and_clamped")
    m, tl = _make()
    seen = {}

    def mid_drag():
        intro, verse = _box(tl, INTRO), _box(tl, VERSE)
        seen['intro_end'] = ((intro.pos().x() + intro.rect.width())
                             / tl.PIXELS_PER_SECOND)
        seen['verse_start'] = verse.pos().x() / tl.PIXELS_PER_SECOND
        seen['intro_len'] = _overlay(tl)._labels[0].number
        seen['verse_tint'] = tl.waveform_track._tints[1][0] / tl.PIXELS_PER_SECOND

    # VERSE's left edge, carried to 13.4 s: the nearest beat is 13.5 s.
    _drag(tl, 10.0 + _edge_px(tl), 13.4, during=mid_drag)
    _assert(abs(seen['intro_end'] - 13.5) < 0.01
            and abs(seen['verse_start'] - 13.5) < 0.01,
            "mid-drag, both neighbours already show the new boundary")
    _assert(abs(seen['verse_tint'] - 13.5) < 0.01,
            "the waveform's colours follow the boundary live")
    _assert(seen['intro_len'] == "5.75",
            f"and INTRO's header its new length in bars ({seen['intro_len']})")
    _assert(_starts(m) == [(INTRO, 2.0), (VERSE, 13.5), (CHORUS, 20.0)],
            f"the release commits one boundary, snapped to a beat ({_starts(m)})")

    # INTRO's RIGHT edge is the same boundary.
    _drag(tl, 13.5 - 2 * _edge_px(tl), 12.3)
    _assert(_starts(m)[1] == (VERSE, 12.5),
            f"a section's right edge moves the next start ({_starts(m)})")

    # Carried far past CHORUS, it stops a beat short instead of reordering.
    _drag(tl, 12.5 + _edge_px(tl), 40.0)
    _assert([n for n, _ in _starts(m)] == [INTRO, VERSE, CHORUS]
            and _starts(m)[1][1] == 19.5,
            f"no reorder, no stacking: held a beat before CHORUS ({_starts(m)})")


def test_a_lone_body_drag_moves_nothing_and_sweeps_a_selection():
    print("test_a_lone_body_drag_moves_nothing_and_sweeps_a_selection")
    m, tl = _make()
    before = _starts(m)
    _drag(tl, 5.0, 25.0)   # from inside INTRO across VERSE into CHORUS
    _assert(_starts(m) == before, "a lone section's body never moves")
    _assert({b.note for b in _overlay(tl).selected_boxes()}
            == {INTRO, VERSE, CHORUS},
            "the drag swept every box it crossed into the selection")


def test_a_body_drag_moves_a_multi_selection_together():
    print("test_a_body_drag_moves_a_multi_selection_together")
    m, tl = _make()
    ctrl = Qt.KeyboardModifier.ControlModifier
    _click(tl, 15.0)                 # VERSE
    _click(tl, 30.0, mods=ctrl)      # + CHORUS
    _assert({b.note for b in _overlay(tl).selected_boxes()} == {VERSE, CHORUS},
            "Ctrl+click adds to the selection")
    _drag(tl, 30.0, 33.7)            # CHORUS body: +3.7 s snaps to +3.5 s
    _assert(_starts(m) == [(INTRO, 2.0), (VERSE, 13.5), (CHORUS, 23.5)],
            f"both selected starts moved by one beat-snapped delta ({_starts(m)})")
    _assert({b.note for b in _overlay(tl).selected_boxes()} == {VERSE, CHORUS},
            "the selection survives the redraw the move scheduled")
    m.undo()
    _assert(_starts(m) == [(INTRO, 2.0), (VERSE, 10.0), (CHORUS, 20.0)],
            "one Ctrl+Z undoes the whole group move")


def test_a_click_selects_and_seeks():
    print("test_a_click_selects_and_seeks")
    _, tl = _make()
    seeks = []
    tl.seek_requested.connect(seeks.append)
    _click(tl, 15.0)
    _assert([b.note for b in _overlay(tl).selected_boxes()] == [VERSE],
            "a plain click selects that section only")
    _assert(len(seeks) == 1 and abs(seeks[0] - 15.0) < 0.1,
            f"and seeks to where it landed ({seeks})")


def test_delete_removes_the_selection_unconfirmed():
    print("test_delete_removes_the_selection_unconfirmed")
    m, tl = _make()
    _click(tl, 15.0)
    _click(tl, 30.0, mods=Qt.KeyboardModifier.ControlModifier)
    box = _box(tl, CHORUS)
    box.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete,
                                Qt.KeyboardModifier.NoModifier))
    _settle(tl)
    _assert(_starts(m) == [(INTRO, 2.0)],
            f"both selected sections are gone, no dialog asked ({_starts(m)})")
    m.undo()
    _assert(len(_starts(m)) == 3, "one Ctrl+Z brings both back")


def test_a_drop_splits_retypes_and_snaps():
    print("test_a_drop_splits_retypes_and_snaps")
    m, tl = _make()
    pps = tl.PIXELS_PER_SECOND
    lane_y = tl._midi_track_y(0) + 10
    _assert(tl.can_accept_clip_drop(BRIDGE, lane_y),
            "a song part is accepted over a fixture lane too")
    _assert(_overlay(tl).preview_drop(BRIDGE, 15.3 * pps, False),
            "the drag previews where the part would land")
    preview = _overlay(tl)._preview_box
    _assert(preview.isVisible()
            and abs(preview.pos().x() / pps - 15.5) < 0.01
            and abs((preview.pos().x() + preview.rect.width()) / pps
                    - 20.0) < 0.01,
            "the preview spans the new section: beat-snapped start to the next")
    header = _overlay(tl)._preview_label
    _assert((header.name, header.number) == ("BRIDGE", "2.25"),
            f"its header names the part and the length it would have "
            f"({header.name!r} {header.number!r})")
    _assert(tl.handle_clip_drop(BRIDGE, 127, 15.3 * pps, lane_y),
            "the drop lands")
    _settle(tl)
    _assert(not preview.isVisible(), "and clears the preview")
    _assert(_starts(m) == [(INTRO, 2.0), (VERSE, 10.0), (BRIDGE, 15.5),
                           (CHORUS, 20.0)],
            f"inside VERSE, BRIDGE splits it on a beat ({_starts(m)})")

    tl.handle_clip_drop(BREAK, 127, 20.2 * pps, lane_y)
    _settle(tl)
    _assert(_starts(m) == [(INTRO, 2.0), (VERSE, 10.0), (BRIDGE, 15.5),
                           (BREAK, 20.0)],
            f"on CHORUS's start, BREAK retypes it rather than stacking "
            f"({_starts(m)})")
    _assert(not tl.handle_clip_drop(VERSE, 127, 63.9 * pps, lane_y),
            "nothing lands past the song's last beat")


def test_snap_is_a_beat_shift_a_bar_and_off_is_off():
    print("test_snap_is_a_beat_shift_a_bar_and_off_is_off")
    m, tl = _make()
    shift = Qt.KeyboardModifier.ShiftModifier
    _drag(tl, 10.0 + _edge_px(tl), 13.4, mods=shift)
    _assert(_starts(m)[1] == (VERSE, 14.0),
            f"Shift snaps an edge drag to the nearest bar ({_starts(m)})")
    pps = tl.PIXELS_PER_SECOND
    tl.handle_clip_drop(BRIDGE, 127, 5.3 * pps, 0, whole_bar=True)
    _assert((BRIDGE, 6.0) in _starts(m),
            f"and a Shift drop to the nearest bar ({_starts(m)})")

    def off_grid(t):   # near no beat line (a beat is 0.5 s at 120 BPM)
        return abs(t * 2 - round(t * 2)) > 0.1

    tl.set_snap_enabled(False)   # the toolbar's Snap button, off
    _drag(tl, 14.0 + _edge_px(tl), 16.37)
    verse = _starts(m)[2][1]
    _assert(abs(verse - 16.37) < 0.1 and off_grid(verse),
            f"Snap off: an edge lands where it is let go ({verse})")
    _drag(tl, verse + _edge_px(tl), 17.37, mods=shift)
    verse = _starts(m)[2][1]
    _assert(abs(verse - 17.37) < 0.1 and off_grid(verse),
            f"Snap off: Shift does not snap either ({verse})")
    tl.handle_clip_drop(SOLO, 127, 30.33 * pps, 0)
    _assert(any(n == SOLO and abs(t - 30.33) < 0.01 for n, t in _starts(m)),
            f"Snap off: a drop lands where it is let go ({_starts(m)})")


def test_nudge_moves_a_whole_bar_or_nothing():
    print("test_nudge_moves_a_whole_bar_or_nothing")
    m, tl = _make()
    overlay = _overlay(tl)
    _assert(not overlay.nudge_selected(1), "no selection: nothing to nudge")
    _click(tl, 15.0)
    overlay.nudge_selected(1)
    _settle(tl)
    _assert(_starts(m)[1] == (VERSE, 12.0), "Alt+→ moves VERSE one bar")
    overlay.nudge_selected(-1, fine=True)
    _settle(tl)
    _assert(_starts(m)[1] == (VERSE, 11.5), "Alt+Shift+← moves it one beat")
    for _ in range(5):
        overlay.nudge_selected(1)
        _settle(tl)
    _assert(_starts(m)[1] == (VERSE, 19.5),
            f"a bar that would crowd CHORUS is refused whole ({_starts(m)})")


def test_labels_pin_to_the_visible_part_of_a_long_section():
    print("test_labels_pin_to_the_visible_part_of_a_long_section")
    _, tl = _make(((INTRO, 2.0), (VERSE, 50.0)))
    tl.set_zoom(3.0)
    view = tl.content_view
    view.horizontalScrollBar().setValue(int(25 * tl.PIXELS_PER_SECOND * 3.0))
    view_left = view.mapToScene(0, 0).x() / tl.PIXELS_PER_SECOND
    label = _overlay(tl)._labels[0]
    x = label.pos().x() / tl.PIXELS_PER_SECOND
    _assert(view_left > 2.0, f"INTRO's start is scrolled off ({view_left:.1f}s)")
    _assert(label.isVisible() and view_left <= x < 50.0,
            f"its label sits at the visible left, not the box centre ({x:.1f}s)")


def test_the_section_menu_splits_where_it_was_opened():
    print("test_the_section_menu_splits_where_it_was_opened")
    m, tl = _make()
    overlay = _overlay(tl)
    shown = []

    class PickingMenu(song_sections.QMenu):
        def exec(self, *_):
            actions = self.actions()
            shown.extend(a.text() for a in actions)
            split = next(a for a in actions if a.text() == "Split here")
            return next(a for a in split.menu().actions()
                        if a.text() == "BRIDGE")

    real, song_sections.QMenu = song_sections.QMenu, PickingMenu
    try:
        overlay._section_menu(_box(tl, VERSE), 15.1 * tl.PIXELS_PER_SECOND,
                              None)
    finally:
        song_sections.QMenu = real
    _settle(tl)
    _assert("Delete 'VERSE' (INTRO extends)" in shown,
            f"the delete item says which section takes the room ({shown})")
    _assert((BRIDGE, 15.0) in _starts(m),
            f"Split here ▸ BRIDGE starts a section on the beat ({_starts(m)})")


def main():
    test_spans_run_to_the_next_start()
    test_move_bounds_hold_a_boundary_between_its_neighbours()
    test_resolve_drop_splits_or_retypes()
    test_each_box_is_a_header_bar_ending_with_the_song()
    test_the_header_bar_has_a_vertical_sheen()
    test_header_words_fit_read_and_let_the_box_work()
    test_the_waveform_takes_the_section_colour_and_the_band_does_not()
    test_an_edge_drags_one_boundary_live_snapped_and_clamped()
    test_a_lone_body_drag_moves_nothing_and_sweeps_a_selection()
    test_a_body_drag_moves_a_multi_selection_together()
    test_a_click_selects_and_seeks()
    test_delete_removes_the_selection_unconfirmed()
    test_a_drop_splits_retypes_and_snaps()
    test_snap_is_a_beat_shift_a_bar_and_off_is_off()
    test_nudge_moves_a_whole_bar_or_nothing()
    test_labels_pin_to_the_visible_part_of_a_long_section()
    test_the_section_menu_splits_where_it_was_opened()
    print("all song-section tests passed")


if __name__ == "__main__":
    main()
