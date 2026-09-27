# ABOUTME: TempoMap — where the bar lines fall when a song changes tempo
# ABOUTME: mid-way. Qt-free, both directions of the seconds <-> bar/beat map.
"""The tempo map of one song: a base BPM plus timed tempo changes.

Everything that draws or plays a song measures its x axis in SECONDS, so a
tempo change never moves a clip — it only moves the bar lines drawn underneath
them. That is what makes a tempo map safe to edit on a song that already has
its lights programmed, and it is why the map lives in the show manifest rather
than in the .mid: `midi.codec.apply_tempo` rescales every delta tick to hold
the wall-clock time of each event and replaces every `set_tempo` with a single
value, so extra tempo metas would drag the whole sequence with them.

Beats are counted CONTINUOUSLY across a change: a marker dropped mid-bar
shortens the remainder of that bar instead of restarting the bar numbering.
A song has ONE meter. A BEAT here is one of the meter's own notes — one click —
so a bar is its numerator in beats; the BPM, though, ALWAYS counts quarter
notes, so a /8 beat lasts half a quarter: 6/8 at 120 is six clicks a bar, a
quarter of a second apart, two to every quarter the BPM counts.
Anything before the first segment — the count-in's negative timeline time —
extrapolates backwards at the first segment's tempo, so a count-in sized by
this map lands exactly on beat 1 at t=0.
"""

import math
from typing import Iterator, List, Sequence, Tuple

# The meter a song counts in when it names none — which is every song saved
# before meters existed, so those keep counting the 4/4 they were built in.
BEATS_PER_BAR = 4

#: The meters a song may count in (the click popup offers exactly these).
METERS = ("3/4", "4/4", "5/4", "7/4", "3/8", "6/8")
DEFAULT_METER = "4/4"

#: A grid `divisions` value (the toolbar's beat-division combo): a line on
#: every beat. 1 is bar lines only and 8 half-beats — in any meter.
GRID_BEATS = 4

MIN_BPM = 40
MAX_BPM = 300

# A grid walk is bounded rather than trusted: a caller asking for a division
# of a four-hour scene should get a short grid, not a hung UI thread.
_MAX_GRID_LINES = 20000


def valid_meter(value) -> str:
    """`value` if it is one of METERS, else the default — what a manifest
    entry hand-edited to "4/3" or "banana" loads as."""
    return value if value in METERS else DEFAULT_METER


def clamp_bpm(value) -> int:
    """A BPM inside the musical range this app plays (matches BpmState)."""
    try:
        return max(MIN_BPM, min(MAX_BPM, int(round(float(value)))))
    except (TypeError, ValueError):
        return 120


class TempoMap:
    """Segments of constant tempo, addressable by seconds or by beat.

    `changes` is an iterable of (seconds, bpm) in any order. A change at or
    before t=0 simply means the song starts at that tempo; two changes at the
    same time collapse to the later one in the iteration order.
    `meter` is the song's, one of METERS; tempo changes never change it. The
    BPMs in and out are quarter-note BPMs whatever the meter.
    """

    def __init__(self, base_bpm=120, changes: Sequence[Tuple[float, int]] = (),
                 meter: str = DEFAULT_METER):
        self.base_bpm = clamp_bpm(base_bpm)
        self.meter = valid_meter(meter)
        beats, note = self.meter.split("/")
        self.beats_per_bar = int(beats)
        self.beat_note = int(note)
        # Quarters per beat: 1 for a /4 meter, 1/2 for /8 (two clicks to the
        # quarter the BPM counts).
        self._quarters_per_beat = 4 / self.beat_note
        # (start_seconds, start_beat, seconds_per_beat), ordered by both.
        self._segments: List[Tuple[float, float, float]] = [
            (0.0, 0.0, self._beat_seconds(self.base_bpm))]
        self._changes: List[Tuple[float, int]] = []

        for seconds, bpm in sorted(((float(t), clamp_bpm(b))
                                    for t, b in changes),
                                   key=lambda c: c[0]):
            self._add(seconds, bpm)

    # -- construction --------------------------------------------------
    def _beat_seconds(self, bpm: int) -> float:
        """Length of one beat of the meter at quarter-note `bpm`."""
        return 60.0 / bpm * self._quarters_per_beat

    def _add(self, seconds: float, bpm: int) -> None:
        spb = self._beat_seconds(bpm)
        if seconds <= 0.0:
            # The song starts at this tempo; nothing precedes it to count.
            self._segments[0] = (0.0, 0.0, spb)
            self._changes = [c for c in self._changes if c[0] > 0.0]
            self._changes.insert(0, (0.0, bpm))
            return
        start_seconds, start_beat, prev_spb = self._segments[-1]
        if seconds <= start_seconds:
            # Same instant as the previous change: the later one wins.
            self._segments[-1] = (start_seconds, start_beat, spb)
            self._changes[-1] = (start_seconds, bpm)
            return
        beat = start_beat + (seconds - start_seconds) / prev_spb
        self._segments.append((seconds, beat, spb))
        self._changes.append((seconds, bpm))

    # -- queries -------------------------------------------------------
    def is_constant(self) -> bool:
        """True when one tempo covers the whole song."""
        return len(self._segments) == 1

    def changes(self) -> List[Tuple[float, int]]:
        """The tempo changes this map was built from, ordered by time."""
        return list(self._changes)

    def _segment_at_seconds(self, seconds: float):
        segment = self._segments[0]
        for candidate in self._segments[1:]:
            if candidate[0] > seconds:
                break
            segment = candidate
        return segment

    def _segment_at_beat(self, beat: float):
        segment = self._segments[0]
        for candidate in self._segments[1:]:
            if candidate[1] > beat:
                break
            segment = candidate
        return segment

    def bpm_at(self, seconds: float) -> int:
        """The tempo sounding at `seconds` (the base BPM before t=0), in
        quarter notes."""
        spb = self._segment_at_seconds(seconds)[2]
        return int(round(60.0 * self._quarters_per_beat / spb))

    def beat_at(self, seconds: float) -> float:
        """Continuous beat number at `seconds` — 0.0 at t=0, negative before."""
        start_seconds, start_beat, spb = self._segment_at_seconds(seconds)
        return start_beat + (seconds - start_seconds) / spb

    def seconds_at_beat(self, beat: float) -> float:
        """Timeline seconds of continuous beat `beat` (the inverse of beat_at)."""
        start_seconds, start_beat, spb = self._segment_at_beat(beat)
        return start_seconds + (beat - start_beat) * spb

    def beat_seconds_at(self, seconds: float) -> float:
        """Length of one beat (one click) where `seconds` falls."""
        return self._segment_at_seconds(seconds)[2]

    def bar_seconds_at(self, seconds: float) -> float:
        """Length of one bar where `seconds` falls."""
        return self._segment_at_seconds(seconds)[2] * self.beats_per_bar

    def shortest_bar_seconds(self) -> float:
        """The narrowest bar anywhere in the map — what a label-density check
        has to survive, since one fast section decides whether numbers collide."""
        return (min(spb for _s, _b, spb in self._segments)
                * self.beats_per_bar)

    # -- bar / beat ----------------------------------------------------
    def bar_beat_at(self, seconds: float) -> Tuple[int, int]:
        """(bar, beat) at `seconds`, both 1-based, as a musician counts them."""
        index = math.floor(self.beat_at(seconds))
        bar = index // self.beats_per_bar + 1
        return int(bar), int(index % self.beats_per_bar) + 1

    def seconds_at_bar_beat(self, bar: float, beat: float = 1.0) -> float:
        """Timeline seconds of a 1-based bar.beat position."""
        return self.seconds_at_beat((bar - 1) * self.beats_per_bar + (beat - 1))

    # -- grid ----------------------------------------------------------
    def _grid_step(self, divisions: int) -> Tuple[int, int]:
        """Beats between two grid lines, as the fraction (num, den).

        `divisions` names the toolbar's grid — 1 = bar lines, GRID_BEATS =
        beats, 8 = half-beats — which counts lines per bar only in 4/4; in any
        meter a bar is still a bar and a beat still a beat. Kept a fraction so
        whether a line falls on a bar is integer arithmetic, not a float
        compare."""
        divisions = max(1, int(divisions))
        if divisions == 1:
            return self.beats_per_bar, 1
        return GRID_BEATS, divisions

    def grid_lines(self, until_seconds: float,
                   divisions: int = GRID_BEATS
                   ) -> Iterator[Tuple[float, int, bool]]:
        """Walk the visible grid: (seconds, bar_number, is_bar_line)."""
        num, den = self._grid_step(divisions)
        per_bar = den * self.beats_per_bar
        for index in range(_MAX_GRID_LINES):
            seconds = self.seconds_at_beat(index * num / den)
            if seconds > until_seconds:
                return
            yield (seconds, index * num // per_bar + 1,
                   index * num % per_bar == 0)

    def nearest_grid_seconds(self, seconds: float,
                             divisions: int = GRID_BEATS) -> float:
        """The grid line nearest `seconds` — what a clip drag snaps to.

        A uniform grid could be snapped to with one modulo; across a tempo
        change the spacing differs on either side of the playhead, so the round
        happens in BEATS and converts back."""
        num, den = self._grid_step(divisions)
        step = num / den
        beat = self.beat_at(seconds)
        return self.seconds_at_beat(round(beat / step) * step)
