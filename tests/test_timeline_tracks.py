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
            "and the hidden chip is out of the flag's hit area too")

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
    test_display_order_by_dmx_binding_by_channel()
    test_rebuild_for_patch_adds_and_removes_tracks()
    test_rebuild_for_patch_keeps_clip_binding()
    test_rebuild_to_empty_patch()
    test_manager_update_fixtures_reflects_new_patch()
    test_empty_patch_no_tracks()
    test_ui_scale_remeasures_the_headers()
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
