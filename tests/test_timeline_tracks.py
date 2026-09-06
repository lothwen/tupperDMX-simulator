"""
Characterization test: data-driven timeline tracks (one per patched fixture).

Locks the Phase-1 subtask-5 behavior:
  * one MIDI events track per fixture in the patch, in patch order;
  * each track shows only its fixture's clips, matched by MIDI channel index
    (= the fixture's patch position), incl. its own BLACKOUT end-marker;
  * global song-part notes (61-69) are excluded from every fixture lane and
    render as an arrangement overlay on the waveform track instead;
  * new clips can be routed to an exact fixture instance by channel;
  * the song's markers ride the ruler as draggable flags, committing on release.

Run with:
    uv run python tests/test_timeline_tracks.py
"""

import sys
import os
from dataclasses import dataclass

# Headless Qt for standalone runs (pytest sets this via conftest.py).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import note_layout
from midi.manager import MidiManager
from timeline.manager import Timeline


@dataclass
class FakeFixture:
    """Minimal stand-in for a patched fixture (type + DMX address + stable
    MIDI channel + optional name)."""
    type: str
    dmx_start: int
    channel: int
    name: str = ""


@dataclass
class OrderedFixture(FakeFixture):
    """A patched fixture that has been dragged to a row of its own."""
    order: int = -1


# ledbar@1 ch0, blinder@33 ch1, blinder@65 ch2 — two blinders so the per-instance
# (not per-type) split is exercised. The stable channel selects each fixture;
# clips are stamped with that channel (as the clip library does). Tracks display
# by DMX address; here DMX order happens to match channel order.
PATCH = [FakeFixture("ledbar", 1, 0),
         FakeFixture("blinder", 33, 1),
         FakeFixture("blinder", 65, 2)]
LEDBAR_CH, BLINDER1_CH, BLINDER2_CH = 0, 1, 2
# Standalone single-note clips. Note 1 drives the ledbar's first DMX channel;
# note 26 is a scene-load verb on a blinder (resolved per-fixture via
# describe_note). The channel selects which fixture in the patch the clip hits.
LEDBAR_CLIP, BLINDER_CLIP = 1, 26
INTRO = 61   # a global song-part verb (61-69), channel-agnostic
CHORUS = 63  # a global song-part verb (61-69), channel-agnostic


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


_APP = None  # module-level ref so the QApplication isn't garbage-collected


def _ensure_app():
    """A QApplication must exist (and stay alive) before any QWidget is built."""
    global _APP
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _make():
    _ensure_app()
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    m.set_song_duration(240.0)
    tl = Timeline(midi_manager=m, fixtures=PATCH)
    return m, tl


def _event(m, note, channel=None):
    for e in m.midi_events:
        if e["note"] == note and (channel is None or e.get("channel") == channel):
            return e
    raise AssertionError(f"no event note={note} channel={channel}")


def test_one_track_per_fixture():
    print("test_one_track_per_fixture")
    _, tl = _make()
    _assert(len(tl.midi_events_tracks) == 3, "three tracks (one per fixture)")
    labels = [t.label for t in tl.midi_events_tracks]
    _assert(labels == ["Ledbar", "Blinder 1", "Blinder 2"],
            f"labels disambiguate repeated types (got {labels})")
    idxs = [t.fixture_index for t in tl.midi_events_tracks]
    _assert(idxs == [0, 1, 2], "tracks carry their stable channel in DMX order")


def test_membership_by_channel():
    print("test_membership_by_channel")
    m, tl = _make()
    led = m.add_event(LEDBAR_CLIP, 1.0, 100, channel=LEDBAR_CH)    # ledbar, ch 0
    bl1 = m.add_event(BLINDER_CLIP, 2.0, 100, channel=BLINDER1_CH)  # 1st blinder
    bl2 = m.add_event(BLINDER_CLIP, 3.0, 100, channel=BLINDER2_CH)  # 2nd blinder
    _assert(led and bl1 and bl2, "events created")

    e_led = _event(m, LEDBAR_CLIP)
    e_bl1 = _event(m, BLINDER_CLIP, channel=1)
    e_bl2 = _event(m, BLINDER_CLIP, channel=2)
    t0, t1, t2 = tl.midi_events_tracks

    _assert(t0.matches_filter(e_led) and not t1.matches_filter(e_led)
            and not t2.matches_filter(e_led), "ledbar clip only on track 0")
    _assert(t1.matches_filter(e_bl1) and not t0.matches_filter(e_bl1)
            and not t2.matches_filter(e_bl1), "blinder1 clip only on track 1")
    _assert(t2.matches_filter(e_bl2) and not t0.matches_filter(e_bl2)
            and not t1.matches_filter(e_bl2), "blinder2 clip only on track 2")


def test_add_event_stamps_channel():
    print("test_add_event_stamps_channel")
    m, _ = _make()
    m.add_event(BLINDER_CLIP, 3.0, 100, channel=2)
    e = _event(m, BLINDER_CLIP, channel=2)
    _assert(e.get("channel") == 2, "event stamped with target channel")
    # The mirrored note (what the codec rebuilds the MIDI file from) too.
    note = next(n for n in m.midi_notes if n["note"] == BLINDER_CLIP)
    _assert(note.get("channel") == 2, "mirrored note stamped with target channel")


def test_song_parts_render_on_waveform_overlay():
    print("test_song_parts_render_on_waveform_overlay")
    from timeline.tracks.song_sections import SongSectionItem
    m, tl = _make()
    m.add_event(CHORUS, 5.0, 100)  # global song parts -> no channel
    m.add_event(INTRO, 1.0, 100)
    chorus = _event(m, CHORUS)
    _assert(m.is_song_part(chorus), "CHORUS is a song part")

    # Song parts no longer live on ANY fixture lane.
    for t in tl.midi_events_tracks:
        _assert(not t.matches_filter(chorus),
                "song part is excluded from every fixture lane")

    # They render as contiguous boxes on the waveform arrangement overlay.
    tl.refresh_from_manager()
    boxes = [it for it in tl.waveform_track._section_items
             if isinstance(it, SongSectionItem)]
    _assert(len(boxes) == 2, "two markers -> two section boxes")
    xs = sorted(b.pos().x() for b in boxes)
    _assert(xs[0] < xs[1], "boxes laid out left-to-right by time")


def test_clearing_the_track_clears_the_waveform():
    print("test_clearing_the_track_clears_the_waveform")
    _, tl = _make()
    # (duration, PCM) as MainWindow injects it; PCM None = decode still running,
    # which still lays the band out.
    tl.set_audio_source(lambda path: (30.0, None))

    tl.set_files("/tmp/backing.wav")
    _assert(tl.waveform_track.waveform_item is not None,
            "a monitored track draws the waveform band")

    tl.set_files(None)
    _assert(tl.waveform_track.waveform_item is None,
            "clearing the song's last track removes the waveform")
    _assert(tl.audio_file is None, "and the timeline forgets the file")

    tl.set_files("/tmp/backing.wav")
    _assert(tl.waveform_track.waveform_item is not None,
            "loading one back draws it again")


def test_timeline_spans_the_longest_track():
    """The scene spans the whole SONG — the longest of backing, guide and
    MIDI — so monitoring a 20-second guide can't trim a 5-minute song."""
    print("test_timeline_spans_the_longest_track")
    m, tl = _make()
    durations = {"/tmp/backing.wav": 300.0, "/tmp/guide.wav": 20.0}
    tl.set_audio_source(lambda path: (durations.get(path), None))

    tl.set_song_tracks(("/tmp/backing.wav", "/tmp/guide.wav"))
    tl.set_files("/tmp/backing.wav")
    full = int(300.0 * 1.05 * tl.PIXELS_PER_SECOND)
    _assert(tl.content_width == full,
            "the backing (the longest track) sets the length")

    tl.set_files("/tmp/guide.wav")   # monitor -> the short guide
    _assert(tl.content_width == full,
            "switching the monitor to a short guide keeps the song's length")
    _assert(m.song_end_time() >= 300.0,
            "and clip-edit bounds keep it too")

    # A sequence running past every audio track wins instead.
    m.get_duration = lambda: 400.0
    tl.apply_song_extent()
    _assert(tl.content_width == int(400.0 * 1.05 * tl.PIXELS_PER_SECOND),
            "MIDI longer than the audio sets the length")

    # Nothing loaded at all -> the default window, not a zero-width scene.
    m.get_duration = lambda: 0.0
    tl.set_song_tracks(())
    tl.set_files(None)
    _assert(tl.content_width == tl.PIXELS_PER_SECOND * tl.SECONDS_TO_DISPLAY,
            "a song with no tracks falls back to the default window")


def test_blackout_scoped_to_its_fixture():
    print("test_blackout_scoped_to_its_fixture")
    m, tl = _make()
    eid = m.add_event(BLINDER_CLIP, 3.0, 100, channel=2)   # 2nd blinder clip
    _assert(m.replace_with_blackout(eid), "clip replaced with a BLACKOUT")
    bo = next(e for e in m.midi_events
              if m.is_blackout(e) and e.get("channel") == 2)
    t0, t1, t2 = tl.midi_events_tracks
    _assert(t2.matches_filter(bo) and not t0.matches_filter(bo)
            and not t1.matches_filter(bo),
            "channel-2 BLACKOUT end-marker belongs only to Blinder 2")


def test_channel_clip_drops_only_on_its_own_fixture():
    """A channel clip carries the fixture the library built it from, because
    its meaning is that fixture's channel list: dropping the ledbar's channel 1
    on a blinder lane would author "whatever the blinder's channel 1 is", which
    is the confusion the tag exists to stop. Verbs addressed through the target
    fixture (a colour macro) and untagged payloads are unaffected."""
    print("test_channel_clip_drops_only_on_its_own_fixture")
    m, tl = _make()
    y_led = tl._midi_track_y(0) + 5        # ledbar lane (DMX 1)
    y_blinder = tl._midi_track_y(1) + 5    # 1st blinder lane (DMX 33)

    _assert(tl.can_accept_clip_drop(LEDBAR_CLIP, y_led, "ledbar"),
            "the ledbar's channel clip lands on the ledbar's own lane")
    _assert(not tl.can_accept_clip_drop(LEDBAR_CLIP, y_blinder, "ledbar"),
            "the same clip is refused on the blinder lane, channel 1 or not")
    _assert(tl.can_accept_clip_drop(LEDBAR_CLIP, y_blinder),
            "an untagged clip still drops wherever the note applies")
    # The blinder DOES define chases, so the refusal below is the fixture tag
    # doing the work rather than note_applies_to's coarser "has chases at all".
    _assert(tl.can_accept_clip_drop(note_layout.CHASE_LOAD, y_blinder),
            "the blinder has chases, so an untagged chase note applies to it")
    _assert(not tl.can_accept_clip_drop(note_layout.CHASE_LOAD, y_blinder,
                                        "ledbar"),
            "a ledbar chase is refused there — its velocity indexes a LIST")
    _assert(tl.can_accept_clip_drop(note_layout.COLOR_MACRO_BASE, y_blinder),
            "a colour macro is addressed by the target fixture, so it lands")
    # Scoped to the TYPE, not to the instance the library card was built from:
    # the library has one group per patched type, so a blinder channel clip has
    # to reach every blinder in the patch.
    _assert(tl.can_accept_clip_drop(1, tl._midi_track_y(2) + 5, "blinder"),
            "a blinder channel clip reaches the second blinder too")

    _assert(not tl.handle_clip_drop(LEDBAR_CLIP, 100, 100.0, y_blinder,
                                    "ledbar"),
            "the refused drop is rejected, not silently retargeted")
    _assert(not any(e["note"] == LEDBAR_CLIP for e in m.midi_events),
            "and inserts nothing")
    _assert(tl.handle_clip_drop(LEDBAR_CLIP, 100, 100.0, y_led, "ledbar"),
            "the matching lane still accepts it")
    _assert(_event(m, LEDBAR_CLIP).get("channel") == LEDBAR_CH,
            "landing on the ledbar's own channel")


def test_a_value_bearing_clip_keeps_its_velocity_of_zero():
    """A dropped clip's velocity is floored at 1 so a plain verb's note_on is
    never mistaken for a note_off — but on a value-bearing note the velocity IS
    the value. Flooring it put every "first chase" clip onto the SECOND chase,
    and made a fixture with only one chase undroppable."""
    print("test_a_value_bearing_clip_keeps_its_velocity_of_zero")
    m, tl = _make()
    y_blinder = tl._midi_track_y(1) + 5

    _assert(tl.handle_clip_drop(note_layout.CHASE_LOAD, 0, 100.0, y_blinder),
            "a clip for the FIRST chase (velocity 0) is accepted")
    _assert(_event(m, note_layout.CHASE_LOAD)["velocity"] == 0,
            "and lands at velocity 0 — chase index 0, not chase index 1")

    m2, tl2 = _make()
    tl2.handle_clip_drop(note_layout.BLACKOUT_FIXTURE, 0, 100.0,
                         tl2._midi_track_y(1) + 5)
    _assert(_event(m2, note_layout.BLACKOUT_FIXTURE)["velocity"] == 1,
            "a plain verb still floors at 1, so its note_on can never read "
            "back as a note_off")


def test_channel_clips_read_their_slot_number():
    """A channel clip on the timeline names the channel AND the slot it drives:
    the shipped blinder repeats "Cold White" on channels 2 and 3, so the name
    alone cannot say which one a clip holds. A note the lane's fixture doesn't
    have as a channel keeps the generic label describe_note gave it."""
    print("test_channel_clips_read_their_slot_number")
    m, tl = _make()
    blinder = tl.midi_events_tracks[1]         # 1st blinder, 8 channels
    ledbar = tl.midi_events_tracks[0]          # ledbar, 7 channels

    m.add_event(2, 1.0, 100, channel=BLINDER1_CH)
    m.add_event(3, 2.0, 100, channel=BLINDER1_CH)
    e2, e3 = _event(m, 2, channel=BLINDER1_CH), _event(m, 3, channel=BLINDER1_CH)
    _assert(e2["info"].part == e3["info"].part,
            f"the blinder repeats a channel name ({e2['info'].part})")
    _assert(blinder._clip_label(e2) == f"{e2['info'].part} (ch.2)",
            f"a channel clip reads '{blinder._clip_label(e2)}'")
    _assert(blinder._clip_label(e2) != blinder._clip_label(e3),
            "so the two identically-named channels no longer read alike")

    m.add_event(note_layout.COLOR_MACRO_BASE, 3.0, 100, channel=BLINDER1_CH)
    macro = _event(m, note_layout.COLOR_MACRO_BASE, channel=BLINDER1_CH)
    _assert(blinder._clip_label(macro) == macro["info"].part,
            "a colour macro drives no single channel, so it gains no number")

    m.add_event(8, 4.0, 100, channel=LEDBAR_CH)   # past the ledbar's 7 slots
    wide = _event(m, 8, channel=LEDBAR_CH)
    _assert(ledbar._clip_label(wide) == wide["info"].part,
            f"a note the fixture has no channel for stays '{wide['info'].part}'")


def test_display_order_by_dmx_binding_by_channel():
    """Tracks lay out top-to-bottom by DMX address, but each binds to its
    fixture's stable channel — the two are decoupled. Here a fixture with the
    HIGHEST channel has the LOWEST DMX address, so it must render on top while
    still matching only its own channel's clips."""
    print("test_display_order_by_dmx_binding_by_channel")
    _ensure_app()
    # channel order [2, 0, 1] vs DMX order [1, 33, 65] -> display must reorder.
    patch = [FakeFixture("ledbar", 1, 2),
             FakeFixture("blinder", 33, 0),
             FakeFixture("blinder", 65, 1)]
    m = MidiManager(note_layout.build_note_map(), fixtures=patch)
    m.create_empty_track()
    m.set_song_duration(240.0)
    tl = Timeline(midi_manager=m, fixtures=patch)

    # Display order follows DMX address (ledbar@1 on top), but fixture_index
    # carries each fixture's stable channel.
    _assert([t.fixture_index for t in tl.midi_events_tracks] == [2, 0, 1],
            "tracks ordered by DMX address, fixture_index = stable channel")

    # A clip on channel 2 (the ledbar) matches only the top track.
    m.add_event(LEDBAR_CLIP, 1.0, 100, channel=2)
    e = _event(m, LEDBAR_CLIP, channel=2)
    top, mid, bot = tl.midi_events_tracks
    _assert(top.matches_filter(e) and not mid.matches_filter(e)
            and not bot.matches_filter(e),
            "channel-2 clip binds to the top (ledbar) lane regardless of order")


def _ordered(patch):
    _ensure_app()
    m = MidiManager(note_layout.build_note_map(), fixtures=patch)
    m.create_empty_track()
    m.set_song_duration(240.0)
    return Timeline(midi_manager=m, fixtures=patch)


def test_saved_row_outranks_the_dmx_address():
    """A lane dragged to a row keeps it across a reload: `order` wins over the
    DMX address, and a fixture that has never been dragged (-1 — every fixture
    in a show saved before the strip could be reordered) falls to the bottom in
    DMX order rather than jumping the placed ones."""
    print("test_saved_row_outranks_the_dmx_address")
    tl = _ordered([OrderedFixture("ledbar", 1, 0, order=2),
                   OrderedFixture("blinder", 33, 1, order=0),
                   OrderedFixture("blinder", 65, 2, order=1)])
    _assert([t.fixture_index for t in tl.midi_events_tracks] == [1, 2, 0],
            "lanes lay out by their saved row, not by DMX address")

    tl = _ordered([OrderedFixture("ledbar", 1, 0, order=1),
                   OrderedFixture("blinder", 33, 1),            # never dragged
                   OrderedFixture("blinder", 65, 2, order=0)])
    _assert([t.fixture_index for t in tl.midi_events_tracks] == [2, 0, 1],
            "an unplaced fixture sorts below the placed ones")


def test_grip_drag_restacks_the_lanes_and_commits_on_release():
    """The reorder gesture: carrying a header past a neighbour's centre restacks
    the lanes underneath it (leaving the gap it will drop into), the carried
    header floats off the row grid meanwhile, and the release settles it and
    announces the new order by MIDI channel."""
    print("test_grip_drag_restacks_the_lanes_and_commits_on_release")
    _, tl = _make()
    committed = []
    tl.track_order_changed.connect(committed.append)

    top = tl.midi_events_tracks[0]
    height = top.get_track_height()
    row0 = tl._midi_track_y(0)

    tl.begin_track_reorder(top)
    tl.update_track_reorder(500.0)                       # anchors the gesture
    tl.update_track_reorder(500.0 + 1.8 * height)
    _assert([t.fixture_index for t in tl.midi_events_tracks] == [1, 2, 0],
            "carried past two lanes, the lane restacks to the bottom row")
    _assert(abs(top._header_proxy.pos().y() - (row0 + 1.8 * height)) < 0.5,
            "the carried header floats with the cursor, off the row grid")
    _assert(committed == [], "nothing is committed mid-drag")

    tl.end_track_reorder()
    _assert(committed == [[1, 2, 0]], "release commits the order, by channel")
    _assert(abs(top._header_proxy.pos().y() - tl._midi_track_y(2)) < 0.5,
            "the header settles onto its new row")


class _FakeButtonEvent:
    """A stand-in for QMouseEvent, which PyQt6 will not let a test construct.

    The header's handlers only ask which button it was and accept it."""

    def __init__(self, button):
        self._button = button
        self.accepted = False

    def button(self):
        return self._button

    def accept(self):
        self.accepted = True


def test_a_header_double_click_asks_for_its_fixture_by_channel():
    """The header reports the fixture's STABLE MIDI channel — the binding key —
    not the row it happens to be sitting in. Reordering first is what tells the
    two apart: after the carry, row 0 is channel 1."""
    print("test_a_header_double_click_asks_for_its_fixture_by_channel")
    from PyQt6.QtCore import Qt
    _, tl = _make()

    asked = []
    tl.fixture_edit_requested.connect(asked.append)

    tl.begin_track_reorder(tl.midi_events_tracks[0])
    tl.update_track_reorder(500.0)
    tl.update_track_reorder(500.0 + 1.8 * tl.midi_events_tracks[0].get_track_height())
    tl.end_track_reorder()
    top = tl.midi_events_tracks[0]
    _assert(top.fixture_index == 1, "after the carry, row 0 is the channel-1 lane")

    event = _FakeButtonEvent(Qt.MouseButton.LeftButton)
    top._header_widget.mouseDoubleClickEvent(event)
    _assert(asked == [1], "the double-click names the channel, not the row")
    _assert(event.accepted, "and consumes the event")

    # A left PRESS must be accepted too, or the proxy widget never delivers a
    # double-click at all — but it must not itself ask for the editor.
    press = _FakeButtonEvent(Qt.MouseButton.LeftButton)
    top._header_widget.mousePressEvent(press)
    _assert(press.accepted, "a left press on the header is accepted")
    _assert(asked == [1], "but a single click does not open anything")


def test_the_header_says_it_is_clickable_while_hovered():
    """The row has no chrome to spare, so the affordance borrows what is
    already there: the address line — a read-out, not a control — says what a
    double-click does, and the fixture's accent stripe lightens."""
    print("test_the_header_says_it_is_clickable_while_hovered")
    from PyQt6.QtCore import Qt
    _m, tl = _make()
    header = tl.midi_events_tracks[0]._header_widget

    _assert(header.testAttribute(Qt.WidgetAttribute.WA_Hover),
            "Qt tracks hover on the header — its :hover rule is what arms it, "
            "and without one the widget never hears the pointer at all")

    address = header.info_label.text()
    _assert("Ch. 0" in address, "it rests showing the address")

    header.enterEvent(None)
    _assert(header.info_label.text() == "Double-click to edit",
            "and says what it does while pointed at")
    header.leaveEvent(None)
    _assert(header.info_label.text() == address,
            "then gives the address back, so nothing is lost to the hint")

    sheet = header.styleSheet()
    _assert(":hover" in sheet and "border-left-color" in sheet,
            "the accent brightens by COLOUR only — a border sits outside the "
            "content rect, so a wider one on hover would shove the whole row "
            "sideways and snap back, once per lane crossed")


def test_the_header_menu_is_a_real_popup_not_a_scene_item():
    """A header lives in a QGraphicsProxyWidget, and a popup parented to a
    proxied widget gets no window of its own — Qt embeds it in the graphics
    SCENE, where the viewport clips it. A header is 30-odd pixels tall, so all
    that survives is the sliver of menu overlapping its own lane."""
    print("test_the_header_menu_is_a_real_popup_not_a_scene_item")
    from PyQt6.QtWidgets import QMenu
    _m, tl = _make()

    asked = []
    tl.fixture_edit_requested.connect(asked.append)

    seen = {}
    original = QMenu.exec

    def stub(self, *args, **kwargs):
        seen["embedded"] = self.graphicsProxyWidget() is not None
        seen["labels"] = [a.text() for a in self.actions()]
        return self.actions()[0]

    QMenu.exec = stub
    try:
        tl.midi_events_tracks[0]._header_widget._show_context_menu()
    finally:
        QMenu.exec = original

    _assert(seen["embedded"] is False,
            "the menu is a real top-level popup, so it is not clipped to the "
            "header it came from")
    _assert(seen["labels"] == ["Edit 'Ledbar'…"],
            "and it names the lane's own fixture")
    _assert(asked == [0], "choosing it asks for that fixture by channel")


def test_a_lane_holds_its_row_until_it_has_passed_half_a_neighbour():
    """Grabbing a lane must not move anything, and a lane restacks only once it
    has been carried over half of the neighbour it is passing — the same going
    back up as coming down.

    The threshold is measured against the slot the lane would drop into. Judging
    it by the carried lane's centre against the closed-up neighbours instead
    lands a resting lane exactly on the next slot's tie point, so it swapped
    rows on the first pixel of the drag (and every later swap fired half a lane
    early, the header running a row ahead of the gap)."""
    print("test_a_lane_holds_its_row_until_it_has_passed_half_a_neighbour")
    _, tl = _make()
    top = tl.midi_events_tracks[0]
    height = top.get_track_height()
    rows = lambda: [t.fixture_index for t in tl.midi_events_tracks]

    tl.begin_track_reorder(top)
    tl.update_track_reorder(500.0)
    _assert(rows() == [0, 1, 2], "grabbing a lane leaves the order alone")
    # A pixel of travel is not a reorder, and neither is a third of a lane.
    tl.update_track_reorder(501.0)
    _assert(rows() == [0, 1, 2], "a pixel of travel restacks nothing")
    tl.update_track_reorder(500.0 + 0.4 * height)
    _assert(rows() == [0, 1, 2], "carried less than half a lane, it holds its row")
    tl.update_track_reorder(500.0 + 0.6 * height)
    _assert(rows() == [1, 0, 2], "carried past half a lane, it takes the next row")
    tl.update_track_reorder(500.0 + 0.4 * height)
    _assert(rows() == [0, 1, 2], "carried back over the same half, it returns")
    tl.end_track_reorder()


def test_passing_a_collapsed_lane_only_costs_half_of_ITS_height():
    """The swap threshold is half the height of the lane being passed, not half
    a nominal row — so a collapsed neighbour is passed in half of its own short
    height."""
    print("test_passing_a_collapsed_lane_only_costs_half_of_ITS_height")
    _, tl = _make()
    top, middle = tl.midi_events_tracks[0], tl.midi_events_tracks[1]
    middle.set_collapsed(True)
    tl.reflow_tracks()
    short = middle.get_track_height()
    _assert(short < top.get_track_height(), "the middle lane is collapsed")

    tl.begin_track_reorder(top)
    tl.update_track_reorder(500.0)
    tl.update_track_reorder(500.0 + 0.4 * short)
    _assert([t.fixture_index for t in tl.midi_events_tracks] == [0, 1, 2],
            "under half the collapsed lane, the order holds")
    tl.update_track_reorder(500.0 + 0.6 * short)
    _assert([t.fixture_index for t in tl.midi_events_tracks] == [1, 0, 2],
            "over half of it, the lanes swap — no full row of travel needed")
    tl.end_track_reorder()


def test_a_crossing_moves_the_clips_rather_than_rebuilding_them():
    """Crossing a row must translate the lanes' existing clips, not redraw them.

    Redrawing every lane on every crossing cost ~65ms on a 240-clip song and
    ~300ms at 1000 — several mouse events' worth each time. Qt compresses the
    moves it could not deliver, so the lane lurched to wherever the cursor had
    got to by then rather than following it. The release does the one honest
    redraw."""
    print("test_a_crossing_moves_the_clips_rather_than_rebuilding_them")
    m, tl = _make()
    m.add_event(LEDBAR_CLIP, 1.0, 100, channel=LEDBAR_CH)
    m.add_event(LEDBAR_CLIP, 5.0, 100, channel=LEDBAR_CH)
    tl.refresh_from_manager()

    top = tl.midi_events_tracks[0]
    height = top.get_track_height()
    before = [id(item) for item in top.items]
    ys = [item.y() for item in top.items]
    _assert(before, "the top lane has clips to move")

    tl.begin_track_reorder(top)
    tl.update_track_reorder(500.0)
    tl.update_track_reorder(500.0 + 0.6 * height)
    _assert([t.fixture_index for t in tl.midi_events_tracks] == [1, 0, 2],
            "the lane crossed a row")
    _assert([id(item) for item in top.items] == before,
            "the crossing kept the very same clip items — no rebuild")
    _assert(all(abs((new - old) - height) < 0.5
                for new, old in zip([i.y() for i in top.items], ys)),
            "and translated them down by exactly one lane")
    tl.end_track_reorder()


def test_the_carry_ignores_screen_coordinates():
    """The carry must ride the header VIEWPORT's own coordinates.

    A widget inside a QGraphicsProxyWidget is handed a GLOBAL position, and
    mapping one back into the scene has to go through the window's origin —
    which under Wayland is a guess the compositor never confirms. That round
    trip was not the identity: mid-drag the origin shifted and the lane jumped
    the height of the entire strip (-416px in one event, 47 crossings in a
    single gesture). The events below carry a global position of (0, 0), a
    deliberate lie: nothing may read it."""
    print("test_the_carry_ignores_screen_coordinates")
    from PyQt6.QtCore import QEvent, QPointF, QPoint, Qt
    from PyQt6.QtGui import QMouseEvent
    _, tl = _make()
    viewport = tl.header_view.viewport()

    _assert(abs(tl._header_scene_y(40.0)
                - tl.header_view.mapToScene(QPoint(0, 40)).y()) < 0.001,
            "a viewport y resolves through the view, with no global round trip")

    def move_at(view_y):
        return QMouseEvent(QEvent.Type.MouseMove, QPointF(5.0, float(view_y)),
                           QPointF(0.0, 0.0),          # the lie
                           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)

    track = tl.midi_events_tracks[0]
    height = track.get_track_height()
    anchor = tl.header_view.mapFromScene(QPointF(0.0, tl._midi_track_y(0))).y()

    tl.begin_track_reorder(track)
    tl.eventFilter(viewport, move_at(anchor))
    _assert([t.fixture_index for t in tl.midi_events_tracks] == [0, 1, 2],
            "the anchor move leaves the order alone")
    tl.eventFilter(viewport, move_at(anchor + 1.6 * height))
    _assert([t.fixture_index for t in tl.midi_events_tracks] == [1, 2, 0],
            "the viewport position alone carries the lane to the bottom row")
    tl.end_track_reorder()


def test_a_drag_cannot_outlive_its_gesture():
    """A carried lane must be dropped as soon as the button is no longer held,
    even when the grip never sees the release.

    The scene hands the mouse grab back the instant a move arrives unpressed —
    it is dispatched as a hover, and the grip is not told. Without this the
    drag outlived the gesture: every later movement of an unpressed mouse went
    on carrying the lane, restacking the whole strip at random."""
    print("test_a_drag_cannot_outlive_its_gesture")
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    _, tl = _make()
    track = tl.midi_events_tracks[0]

    def move(buttons):
        return QMouseEvent(QEvent.Type.MouseMove, QPointF(0, 0), QPointF(0, 0),
                           Qt.MouseButton.NoButton, buttons,
                           Qt.KeyboardModifier.NoModifier)

    tl.begin_track_reorder(track)
    tl.update_track_reorder(500.0)
    tl.eventFilter(tl, move(Qt.MouseButton.LeftButton))
    _assert(tl._reorder_drag is not None,
            "a move with the button held keeps the drag alive")

    tl.eventFilter(tl, move(Qt.MouseButton.NoButton))
    _assert(tl._reorder_drag is None,
            "a move with no button held ends the drag")

    # And the release, wherever it lands in the app.
    tl.begin_track_reorder(track)
    tl.update_track_reorder(500.0)
    tl.eventFilter(tl, QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(0, 0), QPointF(0, 0),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))
    _assert(tl._reorder_drag is None, "a release anywhere in the app ends it")


def test_grip_drag_stays_inside_the_strip():
    print("test_grip_drag_stays_inside_the_strip")
    _, tl = _make()
    committed = []
    tl.track_order_changed.connect(committed.append)

    bottom = tl.midi_events_tracks[-1]
    tl.begin_track_reorder(bottom)
    tl.update_track_reorder(500.0)
    tl.update_track_reorder(-4000.0)  # yanked far above the strip
    _assert([t.fixture_index for t in tl.midi_events_tracks] == [2, 0, 1],
            "dragged past the top, the lane stops at the first row")
    _assert(bottom._header_proxy.pos().y() >= tl._midi_tracks_top() - 0.5,
            "the carried header is clamped to the top of the strip")
    tl.end_track_reorder()

    # Press and release without moving: the lane never left its row.
    tl.begin_track_reorder(tl.midi_events_tracks[0])
    tl.end_track_reorder()
    _assert(committed == [[2, 0, 1]], "a press with no movement commits nothing")


def test_a_dmx_clash_reddens_both_lanes_that_share_the_channels():
    """A patch clash is a property of the whole rig, so it shows on the strip.

    The address row is what the clash is about, so it is the row that reports
    it — via a `state` property the stylesheet paints red, plus a tooltip
    naming what it collides with. It is re-asked on every reflow rather than
    baked in when the header was built, so it appears and clears without the
    lane being rebuilt.
    """
    print("test_a_dmx_clash_reddens_both_lanes_that_share_the_channels")
    _, tl = _make()

    def state(track):
        return track._header_widget.info_label.property("state")

    for track in tl.midi_events_tracks:
        _assert(not state(track), f"lane ch{track.fixture_index} starts clean")

    # par-rgbw is 6 ch: at 36 it lands inside the blinder's 33-40.
    clashing = list(PATCH) + [FakeFixture("par-rgbw", 36, 3, name="Front Par")]
    tl.rebuild_for_patch(clashing)
    by_channel = {t.fixture_index: t for t in tl.midi_events_tracks}

    _assert(state(by_channel[1]) == "clash", "the blinder that was landed on is flagged")
    _assert(state(by_channel[3]) == "clash", "and so is the fixture that landed on it")
    _assert(state(by_channel[0]) != "clash" and state(by_channel[2]) != "clash",
            "lanes that share nothing stay as they were")
    _assert("Front Par" in by_channel[1]._header_widget.info_label.toolTip(),
            "the blinder's tooltip names the fixture on its channels")
    _assert("blinder" in by_channel[3]._header_widget.info_label.toolTip().lower(),
            "and the par's names the blinder — the flag is symmetric")

    # The words are left alone: the row has no width for a marker, so only the
    # colour changes and the address it is warning about stays readable.
    _assert("DMX 36" in by_channel[3]._header_widget.info_label.text(),
            "the address itself is not displaced by the warning")

    # Moving it clear must clear both, on a plain reflow.
    clashing[-1].dmx_start = 200
    for track in tl.midi_events_tracks:
        track.sync_header_state()
    _assert(not state(by_channel[1]) and not state(by_channel[3]),
            "a legal patch clears the flag on both lanes")


def test_rebuild_for_patch_adds_and_removes_tracks():
    print("test_rebuild_for_patch_adds_and_removes_tracks")
    _, tl = _make()
    _assert(len(tl.midi_events_tracks) == 3, "starts with 3 tracks")

    # Add a 4th fixture (par-rgbw @17 ch3): tracks re-sort by DMX address,
    # ledbar@1, par-rgbw@17, blinder@33, blinder@65 -> channels 0, 3, 1, 2.
    added = list(PATCH) + [FakeFixture("par-rgbw", 17, 3)]
    tl.rebuild_for_patch(added)
    _assert([t.fixture_index for t in tl.midi_events_tracks] == [0, 3, 1, 2],
            "rebuild added a lane, re-sorted by DMX, bound by stable channel")
    _assert(tl.midi_events_resize_handle is not None, "midi resize handle rebuilt")

    # Remove the ledbar: its lane drops, survivors keep their channels.
    tl.rebuild_for_patch([f for f in added if f.type != "ledbar"])
    _assert([t.fixture_index for t in tl.midi_events_tracks] == [3, 1, 2],
            "removing a fixture drops only its lane")


def test_rebuild_for_patch_keeps_clip_binding():
    print("test_rebuild_for_patch_keeps_clip_binding")
    m, tl = _make()
    m.add_event(BLINDER_CLIP, 2.0, 100, channel=2)  # 2nd blinder clip
    tl.refresh_from_manager()
    tl.rebuild_for_patch(list(PATCH) + [FakeFixture("par-rgbw", 17, 3)])

    e = _event(m, BLINDER_CLIP, channel=2)
    track2 = next(t for t in tl.midi_events_tracks if t.fixture_index == 2)
    track3 = next(t for t in tl.midi_events_tracks if t.fixture_index == 3)
    _assert(track2.matches_filter(e), "clip still binds to its channel after rebuild")
    _assert(not track3.matches_filter(e), "clip is not on the newly added lane")


def test_rebuild_to_empty_patch():
    print("test_rebuild_to_empty_patch")
    _, tl = _make()
    tl.rebuild_for_patch([])
    _assert(tl.midi_events_tracks == [], "rebuild to empty patch drops all lanes")
    _assert(tl.midi_events_resize_handle is None, "no midi resize handle when empty")
    tl.refresh_from_manager()  # must not raise with zero tracks
    _assert(True, "refresh after emptying the patch is a no-op")


def test_manager_update_fixtures_reflects_new_patch():
    print("test_manager_update_fixtures_reflects_new_patch")
    _ensure_app()
    from core.fixture_library import load_fixture_defs
    m = MidiManager(note_layout.build_note_map(), fixtures=[FakeFixture("ledbar", 1, 0)])
    m.update_fixtures([FakeFixture("ledbar", 1, 0), FakeFixture("blinder", 33, 1)])
    _assert(m.channel_note_map(1) != m.channel_note_map(0),
            "channel 1 now resolves to the blinder (differs from the ledbar at ch0)")
    _assert(len(m.channel_note_map(1)) == load_fixture_defs()["blinder"].channel_count,
            "channel 1's note map matches the blinder's channel count")


def test_empty_patch_no_tracks():
    print("test_empty_patch_no_tracks")
    _ensure_app()
    m = MidiManager(note_layout.build_note_map())
    m.create_empty_track()
    tl = Timeline(midi_manager=m, fixtures=[])
    _assert(tl.midi_events_tracks == [], "no fixtures -> no MIDI tracks (no crash)")
    tl.refresh_from_manager()  # must not raise with zero tracks
    _assert(True, "refresh with empty patch is a no-op")


def test_ui_scale_remeasures_the_headers():
    print("test_ui_scale_remeasures_the_headers")
    _, tl = _make()
    designed = Timeline.HEADER_WIDTH
    _assert(tl.HEADER_WIDTH == designed, "opens at the designed width")

    tl.set_ui_scale(1.5)
    _assert(tl.HEADER_WIDTH == round(designed * 1.5), "the strip follows the scale")
    _assert(tl.header_view.width() == tl.HEADER_WIDTH, "so does the header view")
    _assert(tl.header_scene.sceneRect().width() == tl.HEADER_WIDTH,
            "and the scene it draws into")
    # The ruler's band is created once and kept, so it is the one that has to be
    # resized in place rather than redrawn.
    ruler_bg = tl.ruler_track.permanent_header_items[0]
    _assert(ruler_bg.rect().width() == tl.HEADER_WIDTH, "the ruler band too")
    for track in tl.midi_events_tracks:
        _assert(track._header_widget.width() == tl.HEADER_WIDTH,
                f"lane '{track.label}' redrew at the new width")

    tl.set_ui_scale(0.75)
    _assert(tl.HEADER_WIDTH == round(designed * 0.75), "and back down again")
    _assert(tl.ruler_track.permanent_header_items[0].rect().width()
            == tl.HEADER_WIDTH, "the ruler band shrinks with it")


def test_header_width_is_dragged_and_outranks_the_scale():
    print("test_header_width_is_dragged_and_outranks_the_scale")
    _, tl = _make()
    committed = []
    tl.header_width_changed.connect(committed.append)

    # What the divider's drag emits: a preview per move, one commit on release.
    tl.header_resize_handle.width_preview.emit(320)
    _assert(tl.HEADER_WIDTH == 320, "the strip follows the drag")
    _assert(tl.header_view.width() == 320, "so does the header view")
    _assert(tl.header_scene.sceneRect().width() == 320, "and the scene it draws into")
    _assert(tl.ruler_track.permanent_header_items[0].rect().width() == 320,
            "and the ruler's kept band")
    _assert(committed == [], "mid-drag is visual — nothing is persisted yet")

    tl.header_resize_handle.width_committed.emit(320)
    _assert(committed == [320], "the release is what asks for persistence")

    # A width chosen by hand is the user's, not the scale's (same rule the
    # right sidebar's width follows) — but never below what its rows need.
    tl.set_ui_scale(1.0)
    _assert(tl.HEADER_WIDTH == 320, "a UI-scale change leaves a hand-sized strip alone")
    tl.set_ui_scale(1.5)
    _assert(tl.HEADER_WIDTH == 320, "even a bigger one, while the width still fits")

    tl.header_resize_handle.width_preview.emit(10)
    _assert(tl.HEADER_WIDTH == round(Timeline.MIN_HEADER_WIDTH * 1.5),
            "the floor scales with the interface (the rows it protects do)")
    tl.header_resize_handle.width_preview.emit(9999)
    _assert(tl.HEADER_WIDTH == Timeline.MAX_HEADER_WIDTH,
            "and the ceiling caps it, whatever the scale")


def test_persisted_header_width_reopens_the_strip():
    print("test_persisted_header_width_reopens_the_strip")
    m = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    m.create_empty_track()
    tl = Timeline(midi_manager=m, fixtures=PATCH, initial_header_width=310, ui_scale=1.5)
    _assert(tl.HEADER_WIDTH == 310,
            "a saved width opens the strip, in place of the scaled design width")
    tl.set_ui_scale(1.25)
    _assert(tl.HEADER_WIDTH == 310, "and survives a scale change")

    fresh = Timeline(midi_manager=m, fixtures=PATCH, initial_header_width=0, ui_scale=1.5)
    _assert(fresh.HEADER_WIDTH == round(Timeline.HEADER_WIDTH * 1.5),
            "0 means never sized by hand — the scale decides")


class _FakeMouse:
    """A stand-in for QGraphicsSceneMouseEvent, which PyQt6 refuses to build.

    The flag's handlers only ever ask for the scene position and accept the
    event, so this is the whole contract."""

    def __init__(self, scene_x: float):
        self._x = float(scene_x)

    def scenePos(self):
        from PyQt6.QtCore import QPointF
        return QPointF(self._x, 5.0)

    def accept(self):
        pass


def _drag_flag(flag, from_x, to_x):
    """Press, move, release — the flag commits on the release, like the clips."""
    flag.mousePressEvent(_FakeMouse(from_x))
    flag.mouseMoveEvent(_FakeMouse(to_x))
    flag.mouseReleaseEvent(_FakeMouse(to_x))


def test_ruler_numbers_keep_clear_of_the_marker_lane():
    print("test_ruler_numbers_keep_clear_of_the_marker_lane")
    from timeline.tracks import ruler as ruler_mod
    lane = ruler_mod.MARKER_LANE_H

    _assert(ruler_mod.MARKER_FLAG_TOP + ruler_mod.MARKER_FLAG_H <= lane,
            "a flag and its chip fit inside the lane that is theirs")

    _m, tl = _make()
    tl.set_voice_tts([(12.0, "aqui viene el coro")])
    for mode, items in (("time", "time_text_items"), ("bars", "bar_text_items")):
        tl.set_ruler_mode(mode)
        labels = getattr(tl.ruler_track, items)
        _assert(len(labels) > 0, f"{mode} mode numbers the ruler")
        for _value, item in labels:
            # A QGraphicsTextItem pads its ink by the document margin.
            ink_top = item.pos().y() + item.document().documentMargin()
            _assert(ink_top >= lane,
                    f"and every {mode} number is drawn below the marker lane")
            break  # one per mode is the invariant; they share a y


def test_ruler_markers_draw_as_flags():
    print("test_ruler_markers_draw_as_flags")
    _m, tl = _make()
    tl.set_voice_tts([(12.0, "aqui viene el coro")])
    tl.set_control_markers([(30.0, "tempo", "132 BPM"),
                            (60.0, "click_stop", ""),
                            (90.0, "segue", "next song, no count-in")])

    flags = tl.ruler_track.marker_items
    _assert([f.kind for f in flags] == ["voice", "tempo", "click_stop", "segue"],
            "every marker kind gets a flag, in time order")
    _assert(all(f.pos().x() == f.marker_time * tl.PIXELS_PER_SECOND
                for f in flags),
            "each flag stands at its own time")
    _assert(flags[0].chip_text == "aqui viene el coro"
            and flags[1].chip_text == "132 BPM",
            "a voice cue's chip says what it says, a tempo's says its BPM")
    _assert(flags[2].chip_text == "Stop click" and flags[3].chip_text == "Segue",
            "and a kind with no payload names itself")

    tl.set_voice_tts([])
    tl.set_control_markers([])
    _assert(tl.ruler_track.marker_items == [], "clearing the song clears them")


def test_ruler_marker_chips_yield_when_crowded():
    print("test_ruler_marker_chips_yield_when_crowded")
    _m, tl = _make()
    tl.set_voice_tts([(12.0, "aqui viene el coro"), (13.0, "y esto tambien")])
    tl.set_zoom(1.0)

    first, second = tl.ruler_track.marker_items
    _assert(not first.chip_shown,
            "a cue a second ahead of the next drops its name chip")
    _assert(second.chip_shown, "the last flag always keeps its own")
    _assert(first.boundingRect().width() < second.boundingRect().width(),
            "and the hidden chip stops being drawn")
    _assert(first.shape().boundingRect() == second.shape().boundingRect(),
            "the chip is never the handle either way: only the stem and the "
            "flag take clicks, the rest of the ruler still seeks")

    tl.set_zoom(8.0)
    _assert(first.chip_shown, "zooming in gives the chip its room back")


def test_ruler_marker_drops_a_line_only_under_the_cursor():
    print("test_ruler_marker_drops_a_line_only_under_the_cursor")
    _m, tl = _make()
    tl.set_control_markers([(30.0, "tempo", "132 BPM")])
    flag = tl.ruler_track.marker_items[0]
    band = tl.RULER_TRACK_HEIGHT

    _assert(flag.boundingRect().height() == band,
            "at rest a flag is just the ruler band")

    flag.set_hovered(True)
    _assert(flag.boundingRect().height() == tl.scene_height,
            "pointed at, it reaches down the whole arrangement")
    _assert(flag.shape().boundingRect().height() == band,
            "but the line is never part of the shape, so the lanes under it "
            "still take their own clicks")

    flag.set_hovered(False)
    _assert(flag.boundingRect().height() == band, "and it goes away again")

    # The height is measured as the line appears, never held: the scene grows
    # and shrinks under a marker with every lane collapse.
    tall = tl.scene_height
    tl.set_all_tracks_collapsed(True)
    _assert(tl.scene_height < tall, "collapsing the lanes shortens the scene")
    flag = tl.ruler_track.marker_items[0]
    flag.set_hovered(True)
    _assert(flag.boundingRect().height() == tl.scene_height,
            "so the next hover measures the scene it actually has now")


def test_ruler_marker_drag_commits_on_release():
    print("test_ruler_marker_drag_commits_on_release")
    _m, tl = _make()
    tl.set_snap_enabled(False)
    tl.set_voice_tts([(12.0, "cue")])
    tl.set_control_markers([(30.0, "tempo", "132 BPM")])

    moved = []
    tl.marker_move_committed.connect(lambda *args: moved.append(args))
    pps = tl.PIXELS_PER_SECOND
    voice, tempo = tl.ruler_track.marker_items

    _drag_flag(voice, 12.0 * pps, 25.0 * pps)
    _assert(moved == [("voice", 12.0, 25.0)],
            "a drag reports the kind, where it was and where it landed")

    moved.clear()
    tempo.mousePressEvent(_FakeMouse(30.0 * pps))
    tempo.mouseReleaseEvent(_FakeMouse(30.0 * pps))
    _assert(moved == [], "a click that never moved is not an edit")

    moved.clear()
    _drag_flag(tempo, 30.0 * pps, -400.0)
    _assert(moved == [("tempo", 30.0, 0.0)], "and a drag off the left clamps at 0")


def test_ruler_marker_double_click_asks_for_the_editor():
    print("test_ruler_marker_double_click_asks_for_the_editor")
    _m, tl = _make()
    tl.set_voice_tts([(12.0, "aqui viene el coro")])
    tl.set_control_markers([(64.0, "click_stop", "")])

    asked = []
    tl.marker_edit_requested.connect(lambda *args: asked.append(args))
    for flag in tl.ruler_track.marker_items:
        flag.mouseDoubleClickEvent(_FakeMouse(flag.marker_time))
    _assert(asked == [("voice", 12.0), ("click_stop", 64.0)],
            "a double-click names the marker the editor should open on")


def test_ruler_marker_drag_snaps_to_the_grid():
    print("test_ruler_marker_drag_snaps_to_the_grid")
    _m, tl = _make()
    tl.set_snap_enabled(True)
    tl.set_beat_division(4)
    tl.set_control_markers([(60.0, "click_stop", "")])

    moved = []
    tl.marker_move_committed.connect(lambda *args: moved.append(args))
    pps = tl.PIXELS_PER_SECOND
    beat = tl.tempo_map.seconds_at_beat(10)

    _drag_flag(tl.ruler_track.marker_items[0], 60.0 * pps, (beat + 0.02) * pps)
    _assert(moved == [("click_stop", 60.0, beat)],
            "dropped near a beat, a marker lands ON it (same grid as the clips)")


def main():
    _ensure_app()
    test_one_track_per_fixture()
    test_membership_by_channel()
    test_add_event_stamps_channel()
    test_song_parts_render_on_waveform_overlay()
    test_clearing_the_track_clears_the_waveform()
    test_timeline_spans_the_longest_track()
    test_blackout_scoped_to_its_fixture()
    test_channel_clip_drops_only_on_its_own_fixture()
    test_a_value_bearing_clip_keeps_its_velocity_of_zero()
    test_channel_clips_read_their_slot_number()
    test_display_order_by_dmx_binding_by_channel()
    test_saved_row_outranks_the_dmx_address()
    test_grip_drag_restacks_the_lanes_and_commits_on_release()
    test_a_header_double_click_asks_for_its_fixture_by_channel()
    test_the_header_says_it_is_clickable_while_hovered()
    test_the_header_menu_is_a_real_popup_not_a_scene_item()
    test_a_lane_holds_its_row_until_it_has_passed_half_a_neighbour()
    test_passing_a_collapsed_lane_only_costs_half_of_ITS_height()
    test_a_crossing_moves_the_clips_rather_than_rebuilding_them()
    test_the_carry_ignores_screen_coordinates()
    test_a_drag_cannot_outlive_its_gesture()
    test_grip_drag_stays_inside_the_strip()
    test_a_dmx_clash_reddens_both_lanes_that_share_the_channels()
    test_rebuild_for_patch_adds_and_removes_tracks()
    test_rebuild_for_patch_keeps_clip_binding()
    test_rebuild_to_empty_patch()
    test_manager_update_fixtures_reflects_new_patch()
    test_empty_patch_no_tracks()
    test_ui_scale_remeasures_the_headers()
    test_header_width_is_dragged_and_outranks_the_scale()
    test_persisted_header_width_reopens_the_strip()
    test_ruler_numbers_keep_clear_of_the_marker_lane()
    test_ruler_markers_draw_as_flags()
    test_ruler_marker_chips_yield_when_crowded()
    test_ruler_marker_drops_a_line_only_under_the_cursor()
    test_ruler_marker_drag_commits_on_release()
    test_ruler_marker_double_click_asks_for_the_editor()
    test_ruler_marker_drag_snaps_to_the_grid()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
