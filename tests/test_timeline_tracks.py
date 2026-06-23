"""
Characterization test: data-driven timeline tracks (one per patched fixture).

Locks the Phase-1 subtask-5 behavior:
  * one MIDI events track per fixture in the patch, in patch order;
  * each track shows only its fixture's clips, matched by MIDI channel index
    (= the fixture's patch position), incl. its own BLACKOUT end-marker;
  * global song-part notes (61-69) appear on every track — editable on their
    home lane, read-only markers elsewhere;
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
    """Minimal stand-in for a patched fixture (type + optional name)."""
    type: str
    name: str = ""


# [ledbar@0, blinder@1, blinder@2] — two blinders so the per-instance (not
# per-type) split is exercised. The patch index is the MIDI channel that selects
# each fixture; clips are stamped with that channel (as the clip library does).
PATCH = [FakeFixture("ledbar"), FakeFixture("blinder"), FakeFixture("blinder")]
LEDBAR_CH, BLINDER1_CH, BLINDER2_CH = 0, 1, 2
# Standalone single-note clips. Note 1 drives the ledbar's first DMX channel;
# note 26 is a scene-load verb on a blinder (resolved per-fixture via
# describe_note). The channel selects which fixture in the patch the clip hits.
LEDBAR_CLIP, BLINDER_CLIP = 1, 26
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
    _assert(idxs == [0, 1, 2], "tracks carry their patch index in order")


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


def test_song_part_on_every_track():
    print("test_song_part_on_every_track")
    m, tl = _make()
    m.add_event(CHORUS, 5.0, 100)  # global song part -> no channel; home lane 0
    chorus = _event(m, CHORUS)
    _assert(m.is_song_part(chorus), "CHORUS is a song part")
    t0, t1, t2 = tl.midi_events_tracks
    _assert(t0.matches_filter(chorus) and t1.matches_filter(chorus)
            and t2.matches_filter(chorus), "song part appears on every track")
    _assert(t0._is_home_song_part(chorus), "track 0 (channel 0) is the home lane")
    _assert(not t1._is_home_song_part(chorus)
            and not t2._is_home_song_part(chorus),
            "blinder lanes render it as a read-only marker")


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
    test_song_part_on_every_track()
    test_blackout_scoped_to_its_fixture()
    test_empty_patch_no_tracks()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
