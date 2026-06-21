"""
Characterization test: the one-time .mid note-map migration (subtask 4).

scripts/migrate_midi_v2.py rewrites old-layout sequences (note encodes fixture
+ verb, channel 0) to the generalized layout (note = verb, channel = fixture
instance). This guards the remap table, the both-blinder fan-out, the precount
velocity clamp, the take-manual injection, and idempotency.

Run with:
    uv run python tests/test_midi_migration.py
"""

import os
import sys
import tempfile

import mido

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import migrate_midi_v2 as mig


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _old_track(notes):
    """Build a track of (note, velocity) pairs as note_on/note_off on channel 0."""
    tr = mido.MidiTrack()
    for note, vel in notes:
        tr.append(mido.Message("note_on", note=note, velocity=vel, channel=0, time=0))
        tr.append(mido.Message("note_off", note=note, velocity=0, channel=0, time=120))
    return tr


def _note_ons(track):
    """[(note, channel, velocity)] for real note-ons in a track."""
    return [(m.note, m.channel, m.velocity) for m in track
            if m.type == "note_on" and m.velocity > 0]


def test_remap_channels_and_fanout():
    print("test_remap_channels_and_fanout")
    old = _old_track([
        (1, 100),   # ledbar brightness
        (5, 64),    # ledbar animation (also old manual-control trigger)
        (20, 50),   # color white
        (26, 80),   # both blinders scene 0
        (38, 110),  # precount beat (both blinders, instant)
        (40, 6),    # START
        (41, 64),   # INTRO
        (50, 100),  # blackout LED
        (51, 100),  # blackout blinders
    ])
    new, unknown = mig._transform_track(old)
    ons = _note_ons(new)

    _assert(not unknown, "no unmapped notes")
    _assert((1, 0, 100) in ons, "note 1 -> ledbar ch0, unchanged")
    _assert((5, 0, 64) in ons, "note 5 -> ledbar ch0 channel set")
    _assert((48, 0, 64) in ons, "note 5 injects take-manual (48) on ch0")
    _assert((72, 0, 50) in ons, "note 20 -> color macro 72 ch0")
    _assert((24, 1, 80) in ons and (24, 2, 80) in ons,
            "note 26 -> scene 24 fanned out to blinder ch1 AND ch2")
    _assert((24, 1, 1) in ons and (24, 2, 1) in ons,
            "note 38 (precount) -> 24 on both blinders, velocity clamped to 1")
    _assert((60, 0, 6) in ons, "note 40 -> START 60 ch0")
    _assert((61, 0, 64) in ons, "note 41 -> INTRO 61 ch0")
    _assert((50, 0, 100) in ons, "note 50 -> blackout 50 ch0 (ledbar)")
    _assert((50, 1, 100) in ons and (50, 2, 100) in ons,
            "note 51 -> blackout 50 on both blinder channels")


def test_file_migration_backup_and_idempotent():
    print("test_file_migration_backup_and_idempotent")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "song.mid")
        mid = mido.MidiFile(ticks_per_beat=480)
        mid.tracks.append(_old_track([(26, 80), (41, 64)]))
        mid.save(path)

        _assert(mig.migrate_file(path) is True, "first migration runs")
        _assert(os.path.exists(path + ".bak"), "original backed up to .bak")

        # Re-running is a no-op (marker present).
        _assert(mig.migrate_file(path) is False, "second migration is skipped")

        out = mido.MidiFile(path)
        ons = _note_ons([m for tr in out.tracks for m in tr])
        _assert((24, 1, 80) in ons and (24, 2, 80) in ons,
                "migrated file routes blinder scene to both channels")


def main():
    test_remap_channels_and_fanout()
    test_file_migration_backup_and_idempotent()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
