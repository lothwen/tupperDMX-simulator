# ABOUTME: Authoring a generated show: every lane's resolved cues written as
# ABOUTME: clips, scenes and fades through MidiManager — the same calls a drop
# ABOUTME: on the timeline makes — and program(), the whole pipeline. QtCore.
"""From cues to the song's .mid.

The only module of the package that writes MIDI. It owns what the app's
persistence needs of hand-made content, so a generated .mid reloads as what
was authored: MIN_GAP between a lane's clips, fades of MIN_FADE or more that
end between two 100 ms steps and keep FADE_MARGIN clear of their clip's
edges, one-channel looks written as clips, never as one-note scenes.
"""
import math
from typing import Dict, List, Optional

import mido

from autoprogram import analysis
from autoprogram.cues import MIN_GAP, Cue, resolve
from autoprogram.director import (DEFAULT_PACE, Director, Lane, Skipped, Style,
                                  build_rig, clashes)
from autoprogram.looks import TIER_NAMES
from core import note_layout
from midi.constants import FADE_OUT_END_GAP, FADE_STEP_SECONDS
from midi.manager import MidiManager

MIN_FADE = 0.25        # shorter ramps have too few steps for the app to
                       # recognise them on load: they would come back as clips
FADE_MARGIN = 0.15     # a ramp keeps this clear of its clip's edges, or the
                       # load-time fade detector reads the neighbouring
                       # dimmer note as part of the ramp


def write(mm: MidiManager, lane: Lane, cues: List[Cue], offset: float, fades: list) -> Dict[str, int]:
    """Author `cues` on the lane: a blackout clip, a single-channel clip when
    one channel changes, else a full scene."""
    counts = {"scenes": 0, "clips": 0}
    last: Optional[dict] = None
    # A track whose audio starts before the timeline (a negative offset) puts
    # cues before 0. Only the latest of them matters — it is what is lit at 0
    # — and stacking the rest on 0 would make one clip of several.
    early = [c for c in cues if c.t + offset < 0]
    cues = [c for c in cues if c.t + offset >= 0]
    if early and (not cues or cues[0].t + offset >= MIN_GAP):
        cues.insert(0, Cue(-offset, early[-1].values, early[-1].name, early[-1].prio,
                           fade_out=early[-1].fade_out))
    for c in cues:
        t = round(max(0.0, c.t + offset), 4)
        if c.values is None:
            if last is not None:
                mm.add_event(note_layout.BLACKOUT_FIXTURE, t, 100, lane.channel)
                counts["clips"] += 1
            last = None
            continue
        # One channel is a clip, never a scene: a scene is two or more notes
        # at one instant, and a one-note "scene" reloads as loose clips.
        single = len(c.values) == 1
        if single or (last is not None and not (c.fade_in or c.fade_out)):
            diff = {f: x for f, x in c.values.items()
                    if last is None or x // 2 != last.get(f, 0) // 2}
            if not diff:
                continue
            if len(diff) == 1:
                (f, x), = diff.items()
                slot = lane.voice.index[f]
                if slot < note_layout.CHANNEL_OFFSET_RANGE[1]:
                    mm.add_event(slot + 1, t, x // 2, lane.channel)
                    last = dict(last or c.values)
                    last[f] = x
                    counts["clips"] += 1
                    continue
        gid = mm.add_scene(t, c.values, c.name, lane.channel)
        last = dict(c.values)
        counts["scenes"] += 1
        if gid and (c.fade_in or c.fade_out) and mm.can_fade_scene(gid):
            level = c.values.get(lane.voice.level, 254) if lane.voice.level else 254
            fades.append((gid, c.fade_in, c.fade_out, level // 2))
    return counts


def safe_fade(seconds: float, end_gap: float = 0.0) -> float:
    """`seconds` cut down so the ramp ends half-way between two of its 100 ms
    steps, or 0 when that leaves under MIN_FADE. A ramp that ends just past a
    step writes two notes milliseconds apart, and the load-time detector
    takes the second for a duplicate: it comes back as a clip."""
    steps = math.floor((seconds - end_gap - FADE_STEP_SECONDS / 2) / FADE_STEP_SECONDS)
    fade = steps * FADE_STEP_SECONDS + FADE_STEP_SECONDS / 2 + end_gap
    return round(fade, 4) if fade >= MIN_FADE else 0.0


def visible_width(mm, gid) -> float:
    members = [e for e in mm.midi_events if e.get("group_id") == gid]
    start, lane = members[0]["time"], members[0]["channel"]
    later = [e["time"] for e in mm.midi_events
             if e["channel"] == lane and e["time"] > start and e.get("group_id") != gid]
    return (min(later) if later else mm.song_end_time()) - start


def lane_state(doc: MidiManager, fixtures) -> dict:
    """Each patched channel's (palette, mute, lock) in `doc` — what program()
    keeps of the song's current .mid."""
    return {inst.channel: (doc.fixture_palette(inst.channel),
                           inst.channel in doc.mute_channels(),
                           inst.channel in doc.lock_channels())
            for inst in fixtures}


def program(fixtures, song: analysis.SongMap, palette: dict, offset: float, bpm: int,
            keep: Optional[dict] = None, pace: int = DEFAULT_PACE, seed: int = 0,
            style: Style = Style()):
    """Author the show for `song` on the patch `fixtures`.

    `offset` turns audio seconds into timeline seconds (the analysed track's
    offset), `bpm` is the tempo the file carries, `keep` maps a MIDI channel
    to the (palette, mute, lock) the song's current .mid gives it,
    `pace` (0-8) sets how often looks change, `seed` picks the
    take and `style` holds the director's other choices. Returns
    ``(manager, lanes, skipped, clip counts per channel, fade count,
    director)``.
    """
    lanes, skipped = build_rig(fixtures)
    director = Director(song, lanes, palette, pace, seed, style)
    director.direct()

    mm = MidiManager(note_layout.build_note_map(), fixtures=fixtures)
    mm.create_empty_track()
    # Write at the song's own tempo from the start: an empty track defaults to
    # 120, and export(bpm=...) would then rescale and round every delta.
    mm.midi_data.tracks[0].append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm)))
    mm.set_song_duration(song.duration + offset)

    # Every edit rebuilds the whole .mid from the document's lists, and
    # re-derives the fade ramps of its lane; over thousands of edits that is
    # minutes of work, so it is all done once, at the end.
    fades = []
    counts = {}
    with mm.bulk_edit():
        # The song's own per-lane state survives: palette, mute and lock (auto
        # stays off on purpose — every lane is programmed by hand).
        for channel, (colours, mute, lock) in (keep or {}).items():
            if colours:
                mm.set_fixture_palette(channel, colours)
            if mute:
                mm.set_fixture_mute(channel, True)
            if lock:
                mm.set_fixture_lock(channel, True)
        for part, sec in song.parts():
            if part != "END":
                mm.add_event(note_layout.SONG_PART_BASE + note_layout.SONG_PARTS.index(part),
                             round(max(0.0, sec.start + offset), 4), 127, None)
        mm.add_event(note_layout.SONG_PART_BASE + note_layout.SONG_PARTS.index("END"),
                     round(song.end_start + offset, 4), 127, None)
        for lane in lanes:
            cues = resolve(lane.cues, director.end)
            counts[lane.channel] = write(mm, lane, cues, offset, fades)
        for gid, fi, fo, velocity in fades:
            room = visible_width(mm, gid) - FADE_MARGIN
            fi = safe_fade(min(fi, room))
            # A fade-out whose first steps round to its starting level is read
            # back on load as a flat run that "turns" when it starts to fall:
            # its first notes come back as clips. Steep enough to drop a level
            # on the first step, it reads back whole.
            fo = safe_fade(min(fo, room - fi, velocity / 6.0), FADE_OUT_END_GAP)
            if fi:
                mm.set_scene_fade_in(gid, fi)
            if fo:
                mm.set_scene_fade_out(gid, fo)
    return mm, lanes, skipped, counts, len(fades), director


def report(song: analysis.SongMap, director: Director, lanes: List[Lane],
           skipped: List[Skipped], counts: Dict[int, Dict[str, int]]) -> List[str]:
    """What program() made, a line each: every section's plan, every lane's
    clips and scenes, and what was left out."""
    lines = []
    for k, ((part, sec), plan) in enumerate(zip(song.parts(), director.plans)):
        lines.append(f"  bars {sec.first:3d}-{sec.last:3d}  {part:7s} {sec.material}  "
                     f"{TIER_NAMES[plan.tier]:5s} lead {plan.lead or '-':7s} "
                     f"{len(plan.cast)} lit {plan.pattern:9s} "
                     + ("climax " if k == song.climax else "")
                     + ("repeat" if plan.occurrence else ""))
    if director.reserve:
        lines.append("  held back for the climax: ch "
                     + ", ".join(map(str, sorted(director.reserve))))
    for lane in lanes:
        c = counts[lane.channel]
        twins = f" (also drives ch {', '.join(map(str, lane.twins))})" if lane.twins else ""
        mirror = f" (mirrors ch {lane.mirror.channel})" if lane.mirror else ""
        lines.append(f"  ch {lane.channel:2d} {lane.voice.fdef.name:24s} {lane.role:7s} "
                     f"{c['scenes']:4d} scenes {c['clips']:4d} clips, "
                     f"{len(lane.used):2d} library scenes{twins}{mirror}")
    lines += [f"  skipped {item}" for item in skipped]
    lines += [f"  WARNING {line}" for line in clashes(lanes)]
    return lines
