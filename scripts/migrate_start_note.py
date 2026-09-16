#!/usr/bin/env python3
"""One-time migration: collapse each song's START notes (60) to one, at t=0.

START used to be a draggable clip — a fixture's colour palette, authored
anywhere on the timeline and any number of times per song. It is per-fixture
STATE now (see `core/note_layout`): one note 60 per MIDI channel, pinned at
t=0, alongside the note-71 setup flags. The app carries no migration code, so
existing shows are converted once, here.

Per file, per MIDI channel:
  * the EARLIEST note 60 wins — it is the palette the show already heard first;
  * every later one is deleted;
  * the survivor moves to tick 0, keeping its velocity (the packed hue + mode)
    and its original note_on -> note_off length.

A file with no note 60 is left alone: absence IS the default palette (hue 0,
mode 0), which is exactly what velocity 0 would have meant and what the app
writes nothing for.

Usage:
    uv run python scripts/migrate_start_note.py SONG.mid [more.mid ...]
    uv run python scripts/migrate_start_note.py --dry-run "Show.show/midis/"*.mid

Each changed file is backed up to <name>.bak and rewritten in place (unless
--dry-run). Re-running is safe: a file already holding one note 60 at tick 0
per channel is reported as unchanged and not rewritten.
"""

import argparse
import os
import sys

import mido

START_NOTE = 60
# Fallback length for a survivor whose note_off is missing, in beats. Matches
# the 0.1s-ish blip the app writes for the other pinned note (see
# MidiDocument.set_fixture_palette).
FALLBACK_LENGTH_BEATS = 0.25


def _is_note_off(msg):
    return msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0)


def _plan(track):
    """``(keep_by_channel, drop_count)`` for one track's note-60 messages.

    `keep_by_channel` maps a MIDI channel to ``(velocity, length_ticks)`` taken
    from the earliest note_on on it.
    """
    opens = {}          # channel -> (tick, velocity)
    lengths = {}        # channel -> length in ticks of the earliest note_on
    seen = 0
    tick = 0
    for msg in track:
        tick += msg.time
        if getattr(msg, "note", None) != START_NOTE:
            continue
        if msg.type == "note_on" and msg.velocity > 0:
            seen += 1
            if msg.channel not in opens:
                opens[msg.channel] = (tick, msg.velocity)
        elif _is_note_off(msg):
            start = opens.get(msg.channel)
            if start is not None and msg.channel not in lengths \
                    and tick > start[0]:
                lengths[msg.channel] = tick - start[0]
    keep = {ch: (velocity, lengths.get(ch, 0))
            for ch, (_tick, velocity) in opens.items()}
    return keep, max(0, seen - len(keep))


def _rewrite(track, keep, ticks_per_beat):
    """A copy of `track` with every note 60 stripped and the survivors
    re-inserted at tick 0, after the metas already sitting there."""
    fallback = max(1, int(ticks_per_beat * FALLBACK_LENGTH_BEATS))

    absolute, tick = [], 0
    for msg in track:
        tick += msg.time
        if getattr(msg, "note", None) == START_NOTE:
            continue
        absolute.append((tick, msg))

    head = []
    for channel in sorted(keep):
        velocity, length = keep[channel]
        length = length or fallback
        head.append((0, mido.Message("note_on", note=START_NOTE,
                                     channel=channel, velocity=velocity)))
        head.append((length, mido.Message("note_off", note=START_NOTE,
                                          channel=channel, velocity=0)))

    # Stable merge: the tick-0 metas the file opens with keep their place, the
    # palette note_ons follow them, and everything else stays in order.
    merged = sorted(absolute + head, key=lambda e: e[0])
    out = mido.MidiTrack()
    previous = 0
    for at, msg in merged:
        out.append(msg.copy(time=at - previous))
        previous = at
    return out


def _already_done(track):
    """True when every note 60 in `track` is a single tick-0 pair per channel."""
    tick = 0
    seen = set()
    for msg in track:
        tick += msg.time
        if getattr(msg, "note", None) != START_NOTE:
            continue
        if msg.type == "note_on" and msg.velocity > 0:
            if tick != 0 or msg.channel in seen:
                return False
            seen.add(msg.channel)
    return True


def migrate_file(path, dry_run=False):
    mid = mido.MidiFile(path)
    if all(_already_done(track) for track in mid.tracks):
        kept = sum(1 for track in mid.tracks for msg in track
                   if getattr(msg, "note", None) == START_NOTE
                   and msg.type == "note_on" and msg.velocity > 0)
        print(f"  unchanged ({kept} palette note(s) already at t=0)")
        return False

    new_mid = mido.MidiFile(type=mid.type, ticks_per_beat=mid.ticks_per_beat)
    dropped = 0
    for track in mid.tracks:
        keep, drop = _plan(track)
        dropped += drop
        new_mid.tracks.append(_rewrite(track, keep, mid.ticks_per_beat))
        for channel in sorted(keep):
            velocity, _length = keep[channel]
            hue, mode = divmod(velocity, 6)
            print(f"  ch{channel}: kept velocity {velocity} "
                  f"(hue {hue}, mode {mode}) -> t=0")
    if dropped:
        print(f"  dropped {dropped} later START note(s)")

    if dry_run:
        print("  would rewrite")
        return True

    backup = path + ".bak"
    if not os.path.exists(backup):
        import shutil
        shutil.copy2(path, backup)
    else:
        print(f"  (backup {backup} exists, leaving it)")
    new_mid.save(path)
    print(f"  rewritten (backup: {backup})")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Collapse each song's START notes (60) to one at t=0.")
    parser.add_argument("files", nargs="+", help="MIDI files to migrate")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args(argv)

    changed = 0
    for path in args.files:
        if not os.path.isfile(path):
            print(f"  not a file: {path}")
            continue
        print(path)
        try:
            if migrate_file(path, dry_run=args.dry_run):
                changed += 1
        except Exception as e:  # one bad file shouldn't abort the batch
            print(f"  ERROR migrating {path}: {e}")

    print(f"\n{'Would rewrite' if args.dry_run else 'Rewrote'} {changed} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
