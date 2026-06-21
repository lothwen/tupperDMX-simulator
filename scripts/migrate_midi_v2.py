#!/usr/bin/env python3
"""One-time migration of saved .mid sequences to the generalized note map.

Old layout: the MIDI *note number* encoded BOTH the fixture and the verb, and
everything played on MIDI channel 0 (e.g. notes 1-7 = LED channels, 26-30 =
both blinders, 41-49 = song parts, 50/51 = LED/blinder blackout).

New layout (ROADMAP subtask 4): the note is a UNIVERSAL verb and the MIDI
*channel* = the fixture instance's index in the patch (ledbar=0, blinder1=1,
blinder2=2). Verbs were also renumbered to the tidy C4=START layout.

This rewrites each file accordingly:
  * remaps every note to its new number,
  * stamps the MIDI channel of the fixture it targets,
  * fans the old "both blinders" notes out onto channels 1 AND 2,
  * forces precount flashes to an instant blend (velocity 1),
  * injects a take-manual-control note (48) wherever an LED manual scene used
    the animation note (5) as its implicit trigger.

Usage:
    uv run python scripts/migrate_midi_v2.py SONG.mid [more.mid ...]
    uv run python scripts/migrate_midi_v2.py --dry-run path/to/*.mid

Each file is backed up to <name>.bak and rewritten in place (unless --dry-run).
Files already carrying the v2 marker are skipped, so re-running is safe.
"""

import argparse
import os
import sys

import mido

# Patch channels (instance index in config.fixtures: [ledbar, blinder, blinder]).
LEDBAR, BLINDER1, BLINDER2 = 0, 1, 2
BLINDERS = (BLINDER1, BLINDER2)

MARKER_TEXT = "tupperdmx_notemap_v2"

# old_note -> list of (new_note, channel, velocity_override). velocity_override
# is None to keep the original velocity; it only applies to the note_on.
NOTE_MAP: dict[int, list[tuple[int, int, int | None]]] = {}

# 1-7: LED channel offsets — unchanged number, channel 0.
for n in range(1, 8):
    NOTE_MAP[n] = [(n, LEDBAR, None)]

# 20-23: LED color macros -> 72-75.
for old, new in zip(range(20, 24), range(72, 76)):
    NOTE_MAP[old] = [(new, LEDBAR, None)]

# 26-30: both-blinder scenes 0-4 -> scene loaders 24-28 on BOTH blinder channels.
for old, new in zip(range(26, 31), range(24, 29)):
    NOTE_MAP[old] = [(new, ch, None) for ch in BLINDERS]

# 38/39: precount beat/downbeat = blinder scenes 0/1, instant. -> 24/25 on both
# blinders, velocity forced to 1 so the scene loads with a ~instant blend.
NOTE_MAP[38] = [(24, ch, 1) for ch in BLINDERS]
NOTE_MAP[39] = [(25, ch, 1) for ch in BLINDERS]

# 40: START -> 60 (ledbar carries the palette).
NOTE_MAP[40] = [(60, LEDBAR, None)]

# 41-49: song parts -> 61-69 (global verbs; emitted on channel 0).
for old in range(41, 50):
    NOTE_MAP[old] = [(old + 20, LEDBAR, None)]

# 50: blackout LED -> 50 on channel 0.
NOTE_MAP[50] = [(50, LEDBAR, None)]
# 51: blackout blinders -> 50 on BOTH blinder channels.
NOTE_MAP[51] = [(50, ch, None) for ch in BLINDERS]

# The animation note (5) used to double as the manual-LED-control trigger; the
# new layout has a dedicated take-manual verb (48). Inject it alongside note 5.
TAKE_MANUAL = 48


def _abs_events(track):
    """[(abs_tick, msg)] for a track."""
    out, t = [], 0
    for msg in track:
        t += msg.time
        out.append((t, msg))
    return out


def _transform_track(track):
    """Return (new_track, stats) for one track. Pure note remap + channel stamp
    + fan-out + take-manual injection; meta/other messages pass through."""
    abs_events = _abs_events(track)
    out = []  # (abs_tick, order_hint, msg)
    unknown = set()

    for abs_tick, msg in abs_events:
        if msg.type not in ("note_on", "note_off"):
            out.append((abs_tick, 0, msg))
            continue

        targets = NOTE_MAP.get(msg.note)
        if targets is None:
            unknown.add(msg.note)
            out.append((abs_tick, 0, msg))
            continue

        for new_note, channel, vel_override in targets:
            velocity = msg.velocity
            if msg.type == "note_on" and msg.velocity > 0 and vel_override is not None:
                velocity = vel_override
            out.append((abs_tick, 0, msg.copy(
                note=new_note, channel=channel, velocity=velocity)))

        # Inject the take-manual verb next to the animation note (note 5).
        if msg.note == 5:
            if msg.type == "note_on" and msg.velocity > 0:
                out.append((abs_tick, 1, mido.Message(
                    "note_on", note=TAKE_MANUAL, channel=LEDBAR, velocity=64)))
            else:
                out.append((abs_tick, 1, mido.Message(
                    "note_off", note=TAKE_MANUAL, channel=LEDBAR, velocity=0)))

    # Stable sort by (tick, order_hint) then re-delta.
    out.sort(key=lambda e: (e[0], e[1]))
    new_track = mido.MidiTrack()
    prev = 0
    for abs_tick, _hint, msg in out:
        new_track.append(msg.copy(time=abs_tick - prev))
        prev = abs_tick
    return new_track, unknown


def _already_migrated(mid):
    return any(m.type == "marker" and getattr(m, "text", "") == MARKER_TEXT
               for tr in mid.tracks for m in tr)


def migrate_file(path, dry_run=False):
    mid = mido.MidiFile(path)
    if _already_migrated(mid):
        print(f"  skip (already v2): {path}")
        return False

    new_mid = mido.MidiFile(type=mid.type, ticks_per_beat=mid.ticks_per_beat)
    all_unknown = set()
    for i, track in enumerate(mid.tracks):
        new_track, unknown = _transform_track(track)
        all_unknown |= unknown
        if i == 0:  # tag the file as migrated
            new_track.insert(0, mido.MetaMessage("marker", text=MARKER_TEXT, time=0))
        new_mid.tracks.append(new_track)

    if all_unknown:
        print(f"  WARNING: passed through unmapped notes {sorted(all_unknown)} in {path}")

    if dry_run:
        print(f"  would migrate: {path}")
        return True

    backup = path + ".bak"
    if not os.path.exists(backup):
        os.replace(path, backup)
    else:
        print(f"  (backup {backup} exists, leaving it)")
    new_mid.save(path)
    print(f"  migrated: {path}  (backup: {backup})")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Migrate .mid files to the v2 note map.")
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

    print(f"\n{'Would migrate' if args.dry_run else 'Migrated'} {changed} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
