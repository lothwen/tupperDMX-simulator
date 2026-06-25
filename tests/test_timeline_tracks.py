"""
Characterization test: data-driven timeline tracks (one per patched fixture).

Locks the Phase-1 subtask-5 behavior:
  * one MIDI events track per fixture in the patch, in patch order;
  * each track shows only its fixture's clips, matched by MIDI channel index
    (= the fixture's patch position), incl. its own BLACKOUT end-marker;
  * global song-part notes (61-69) are excluded from every fixture lane and
    render as an arrangement overlay on the waveform track instead;
  * new clips can be routed to an exact fixture instance by channel.

Run with:
    uv run python tests/test_timeline_tracks.py
"""

import sys
import os
from dataclasses import dataclass

# Headless Qt for standalone runs (pytest sets this via conftest.py).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import note_layout
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

    # Add a 4th fixture (par_rgb @17 ch3): tracks re-sort by DMX address,
    # ledbar@1, par_rgb@17, blinder@33, blinder@65 -> channels 0, 3, 1, 2.
    added = list(PATCH) + [FakeFixture("par_rgb", 17, 3)]
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
    tl.rebuild_for_patch(list(PATCH) + [FakeFixture("par_rgb", 17, 3)])

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
    from fixture_library import load_fixture_defs
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


def main():
    _ensure_app()
    test_one_track_per_fixture()
    test_membership_by_channel()
    test_add_event_stamps_channel()
    test_song_parts_render_on_waveform_overlay()
    test_blackout_scoped_to_its_fixture()
    test_display_order_by_dmx_binding_by_channel()
    test_rebuild_for_patch_adds_and_removes_tracks()
    test_rebuild_for_patch_keeps_clip_binding()
    test_rebuild_to_empty_patch()
    test_manager_update_fixtures_reflects_new_patch()
    test_empty_patch_no_tracks()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
