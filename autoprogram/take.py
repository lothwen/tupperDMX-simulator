# ABOUTME: One song's generated show — a TAKE: its audio decoded and analysed
# ABOUTME: (analyse), the show programmed and written to a .mid (program).
# ABOUTME: Shared by the app's AutoProgrammer and scripts/autoprogram_cli.py. QtCore.
"""How a take is made.

  1. analysis.py maps the song: the beat grid, its sections and which of them
     play the same music (their MATERIAL), how much energy each carries, the
     climax, how the song ends, its builds, impacts, stops and the accents of
     its quiet passages. The guide track is analysed when the song has one
     (the full mix), else the backing.
  2. Every patched fixture gets a ROLE from its capabilities, never its name:
     a MOVER pans or tilts, FOG emits fog, a PIXEL bar has an animation
     selector and mixes colour, a WASH mixes colour, a BLINDER only has white
     or a dimmer. Fixtures patched on one DMX address are one lane; two lanes
     of one fixture type, rigged alike either side of the stage, are a
     MIRRORED pair — one fixture to the eye, lit with one look and hit
     together, alternating only where a hit is answered within half a bar.
  3. A section lights as much of the rig as it has energy for, and the song
     builds to its climax. A quiet one lights only its LEAD role — just its
     middle (one fixture or a mirrored pair), dimmed, where the song opens
     from nothing or dies away after its climax; a mid or loud one a CAST of
     the rig, the lead first, more of it the louder the section and the
     nearer the climax, so a part coming back lights at least what it did.
     A loud CHORUS is lit as the climax is: all of the rig but its reserve —
     the outermost unit of the last role in — its looks changing fast,
     landing on a hit after a blackout breath where a build leads into it.
     The CLIMAX — the song's last strong section, however its loudness
     compares — goes further: it opens out to the reserve, changes its looks
     twice as often as a chorus and strobes into every phrase line. The
     band's first entrance and every step up to loud land as a hit too;
     before the climax a hit lights only its section's cast. The BLINDERS
     fire on the song's own hits and the landing after a build, nowhere else
     until its last stretch — the last chorus, the climax, the outro — where
     they may also strobe in short bursts. The band stopping is a blackout,
     and the finale plays the way the song ends: a last hit cut dead, a last
     hit left to ring, or a burn-down.
  4. Every MATERIAL gets its looks, its colours and its choreography once, so
     a chorus looks the same each time it comes back: its cast lit together,
     with looks in turn, alternating, chased from the middle out or stacked a
     unit at a time, never as the part before it. The colours are one
     palette — three hues and white, every fixture wearing the same ones —
     chosen from the song's key and drive unless the options name one.
  5. The LOOKS are the fixture library's own scenes, judged by what their
     channels do (how much light, which colours, strobing, animated). A
     section ranks them by the song part the library tags them for, how well
     their colours sit in the palette and how much light they give; where the
     library has nothing that fits, a look is built from the fixture's
     capabilities in the palette's colours. Scenes that hand their colour to
     the palette (R, G and B at full) wear the section's colours. Impacts take
     the brightest look, builds the strobes.
  6. PACE (0-8) sets how often looks change and the cast takes its turns
     (four turns to a look), each step about twice as often: at 0 a section
     holds one look throughout, all its cast lit, at 8 a chorus changes every
     beat and the climax every half beat. Every step keeps
     the song's shape — each section starts a look of its own and lights its
     own cast, and the impacts, builds and strongest accents always land. The
     SEED picks the take: other lead roles, look orders and choreographies.
  7. Everything is authored through MidiManager, the same calls a drop on the
     timeline makes, so the .mid is what the app writes for hand-made clips.
     No lane is left in auto mode; the song parts label the arrangement band.

The two halves are separate calls because they change for different reasons:
the analysis follows the audio and Options.tuning, the programming the
palette, pace, seed and Options.style — so a take that differs only in
how it is programmed reuses its Analysis (seconds) and programs again
(a fraction of one).

A take is written to the .mid it is given and nowhere else: making one never
touches the show.
"""
import os
import zlib
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import mido

from autoprogram import analysis, colours, decode, writer
from autoprogram.director import DEFAULT_PACE, Skipped, Style, clashes
from core import note_layout
from core.config import SongConfig
from midi.manager import MidiManager

GUIDE, BACKING = "guide", "backing"
# The steps a take is made in, as `stage` is told them.
DECODE, ANALYSE, PROGRAM = "decode", "analyse", "program"


class TakeError(Exception):
    """A take that cannot be made: no audio to analyse, or no .mid written."""


@dataclass
class Options:
    track: Optional[str] = None         # GUIDE | BACKING; None: the guide when the song has one
    pace: int = DEFAULT_PACE            # 0..PACE_STEPS: how often looks change
    palette: Optional[str] = None       # a colours.palette spec; None: the song's mood
    seed: Optional[int] = None          # None: the song's own, from its title
    tuning: analysis.Tuning = field(default_factory=analysis.Tuning)   # how the audio is read
    style: Style = field(default_factory=Style)                       # how the show is directed


@dataclass
class Analysis:
    """A song's audio mapped: what a take is programmed from. One analysis
    serves every take that differs only in how it is programmed."""
    track: str
    path: str
    bpm: Optional[int]                  # the song's tempo the grid was searched around
    meter: str
    tuning: analysis.Tuning
    map: analysis.SongMap


@dataclass
class Programmed:
    """One lane of a take: the fixture it drives (and its twins on the same
    DMX address), the role it was given, what was written on it and the
    lane it mirrors across the stage."""
    channel: int
    fixture: str
    role: str
    twins: List[int]
    scenes: int
    clips: int
    mirror: Optional[int] = None


@dataclass
class Take:
    """One generated show, written to `midi_path`."""
    song: SongConfig
    options: Options                    # as asked for
    track: str                          # what they came to
    palette: str                        # the spec it wore, a random hue drawn and named
    seed: int
    bpm: int                            # the tempo its .mid carries
    midi_path: str
    summary: List[str]                  # the song's map, then what was programmed
    warnings: List[str]                 # the analysis's, then the patch's
    events: int
    fades: int
    analysis: Analysis
    lanes: List[Programmed]
    skipped: List[Skipped]


def tracks(song: SongConfig) -> List[str]:
    """The tracks of `song` there is audio to analyse in, the guide (the full
    mix) first."""
    return ([GUIDE] if song.guide and os.path.exists(song.guide) else []) + \
           ([BACKING] if song.audio and os.path.exists(song.audio) else [])


def track_of(song: SongConfig, options: Options) -> str:
    """The track a take of `song` at `options` analyses."""
    return options.track or (GUIDE if song.guide else BACKING)


def default_seed(song: SongConfig) -> int:
    """The take a song gets unless asked for another, from its title."""
    return zlib.crc32((song.title or "").encode())


def song_tempo(song: SongConfig) -> Optional[int]:
    """The tempo the song runs at: its .mid's, else the manifest's; None when
    it has neither."""
    if song.midi and os.path.exists(song.midi):
        for msg in mido.merge_tracks(mido.MidiFile(song.midi).tracks):
            if msg.type == "set_tempo":
                return int(round(mido.tempo2bpm(msg.tempo)))
    bpm = song.cache.get("bpm")
    return int(bpm) if bpm else None


def _quiet(_step: str, _text: str) -> None:
    pass


def analyse(song: SongConfig, track: str, bpm: Optional[int] = None,
            tuning: analysis.Tuning = analysis.Tuning(),
            stage: Callable[[str, str], None] = _quiet) -> Analysis:
    """Decode and map the `track` of `song`. `bpm` is the song's tempo,
    where the grid search starts; None lets the analysis find it. `stage`
    is told each step (DECODE, ANALYSE) and what it does, as it starts."""
    path = song.guide if track == GUIDE else song.audio
    if not path or not os.path.exists(path):
        raise TakeError(f"'{song.title}' has no {track} audio to analyse")
    stage(DECODE, f"Decoding {os.path.basename(path)}")
    rate, mono = decode.decode(path)
    stage(ANALYSE, "Analysing beats, bars and sections")
    meter = song.meter or "4/4"
    return Analysis(track, path, bpm, meter, tuning,
                    analysis.analyse_samples(rate, mono, bpm, meter, tuning))


def program(fixtures, song: SongConfig, mapped: Analysis, options: Options, midi_path: str,
            keep: Optional[dict] = None,
            stage: Callable[[str, str], None] = _quiet) -> Take:
    """Program `song` from its analysis on the patch `fixtures` and write it
    to `midi_path`. `keep` is the (palette, mute, lock) per channel the song
    keeps of its program (writer.lane_state); None reads it from the song's
    .mid. `stage` is told when the PROGRAM step starts."""
    song_map = mapped.map
    offset = song.guide_offset if mapped.track == GUIDE else song.offset
    seed = default_seed(song) if options.seed is None else options.seed
    palette, colours_of = colours.palette(options.palette or colours.mood_palette(song_map), seed)
    bpm = mapped.bpm or int(round(song_map.bpm))
    if keep is None and song.midi and os.path.exists(song.midi):
        current = MidiManager(note_layout.build_note_map(), fixtures=fixtures)
        current.load_from_file(song.midi)
        keep = writer.lane_state(current, fixtures)

    count = len(fixtures)
    stage(PROGRAM, f"Programming {count} fixture{'' if count == 1 else 's'}")
    mm, lanes, skipped, counts, fades, director = writer.program(
        fixtures, song_map, colours_of, offset, bpm, keep,
        options.pace, seed, options.style)
    if not mm.export_to_file(midi_path, bpm=bpm):
        raise TakeError(f"could not write {midi_path}")
    summary = analysis.describe(song_map).splitlines()
    summary.append(f"palette '{palette}', pace {options.pace}, seed {seed}, "
                   f"{bpm} BPM in the file")
    summary += writer.report(song_map, director, lanes, skipped, counts)
    programmed = [Programmed(lane.channel, lane.voice.fdef.name, lane.role, list(lane.twins),
                             counts[lane.channel]["scenes"], counts[lane.channel]["clips"],
                             lane.mirror.channel if lane.mirror else None)
                  for lane in lanes]
    return Take(song, options, mapped.track, palette, seed, bpm, midi_path, summary,
                list(song_map.warnings) + clashes(lanes), len(mm.midi_events), fades,
                mapped, programmed, skipped)


def generate(fixtures, song: SongConfig, options: Options, midi_path: str,
             bpm: Optional[int] = None, keep: Optional[dict] = None,
             stage: Callable[[str, str], None] = _quiet) -> Take:
    """Make a take of `song` on the patch `fixtures` and write it to
    `midi_path`: analyse(), then program()."""
    mapped = analyse(song, track_of(song, options), bpm, options.tuning, stage)
    return program(fixtures, song, mapped, options, midi_path, keep, stage)
