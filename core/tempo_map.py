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
Anything before the first segment — the count-in's negative timeline time —
extrapolates backwards at the first segment's tempo, so a count-in sized by
this map lands exactly on beat 1 at t=0.
"""

import math
from typing import Iterator, List, Sequence, Tuple

# The app is 4/4 everywhere (count-in bars, ruler bars, the downbeat the
# metronome accents); one constant so a future meter change has one place.
BEATS_PER_BAR = 4

MIN_BPM = 40
MAX_BPM = 300

# A grid walk is bounded rather than trusted: a caller asking for a division
# of a four-hour scene should get a short grid, not a hung UI thread.
_MAX_GRID_LINES = 20000


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
    """

    def __init__(self, base_bpm=120, changes: Sequence[Tuple[float, int]] = ()):
        self.base_bpm = clamp_bpm(base_bpm)
        # (start_seconds, start_beat, seconds_per_beat), ordered by both.
        self._segments: List[Tuple[float, float, float]] = [
            (0.0, 0.0, 60.0 / self.base_bpm)]
        self._changes: List[Tuple[float, int]] = []

        for seconds, bpm in sorted(((float(t), clamp_bpm(b))
                                    for t, b in changes),
                                   key=lambda c: c[0]):
            self._add(seconds, bpm)

    # -- construction --------------------------------------------------
    def _add(self, seconds: float, bpm: int) -> None:
        spb = 60.0 / bpm
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
        """The tempo sounding at `seconds` (the base BPM before t=0)."""
        return int(round(60.0 / self._segment_at_seconds(seconds)[2]))

    def beat_at(self, seconds: float) -> float:
        """Continuous beat number at `seconds` — 0.0 at t=0, negative before."""
        start_seconds, start_beat, spb = self._segment_at_seconds(seconds)
        return start_beat + (seconds - start_seconds) / spb

    def seconds_at_beat(self, beat: float) -> float:
        """Timeline seconds of continuous beat `beat` (the inverse of beat_at)."""
        start_seconds, start_beat, spb = self._segment_at_beat(beat)
        return start_seconds + (beat - start_beat) * spb

    def beat_seconds_at(self, seconds: float) -> float:
        """Length of one beat where `seconds` falls."""
        return self._segment_at_seconds(seconds)[2]

    def bar_seconds_at(self, seconds: float) -> float:
        """Length of one bar where `seconds` falls."""
        return self._segment_at_seconds(seconds)[2] * BEATS_PER_BAR

    def shortest_bar_seconds(self) -> float:
        """The narrowest bar anywhere in the map — what a label-density check
        has to survive, since one fast section decides whether numbers collide."""
        return min(spb for _s, _b, spb in self._segments) * BEATS_PER_BAR

    # -- bar / beat ----------------------------------------------------
    def bar_beat_at(self, seconds: float) -> Tuple[int, int]:
        """(bar, beat) at `seconds`, both 1-based, as a musician counts them."""
        index = math.floor(self.beat_at(seconds))
        bar = index // BEATS_PER_BAR + 1
        return int(bar), int(index % BEATS_PER_BAR) + 1

    def seconds_at_bar_beat(self, bar: float, beat: float = 1.0) -> float:
        """Timeline seconds of a 1-based bar.beat position."""
        return self.seconds_at_beat((bar - 1) * BEATS_PER_BAR + (beat - 1))

    # -- grid ----------------------------------------------------------
    def grid_lines(self, until_seconds: float,
                   divisions: int = BEATS_PER_BAR
                   ) -> Iterator[Tuple[float, int, bool]]:
        """Walk the visible grid: (seconds, bar_number, is_bar_line).

        `divisions` is lines per bar, matching the toolbar's beat-division combo
        (1 = bar lines only, 4 = beats, 8 = half-beats)."""
        divisions = max(1, int(divisions))
        step = BEATS_PER_BAR / divisions
        for index in range(_MAX_GRID_LINES):
            beat = index * step
            seconds = self.seconds_at_beat(beat)
            if seconds > until_seconds:
                return
            yield (seconds, int(beat // BEATS_PER_BAR) + 1,
                   index % divisions == 0)

    def nearest_grid_seconds(self, seconds: float,
                             divisions: int = BEATS_PER_BAR) -> float:
        """The grid line nearest `seconds` — what a clip drag snaps to.

        A uniform grid could be snapped to with one modulo; across a tempo
        change the spacing differs on either side of the playhead, so the round
        happens in BEATS and converts back."""
        divisions = max(1, int(divisions))
        step = BEATS_PER_BAR / divisions
        beat = self.beat_at(seconds)
        return self.seconds_at_beat(round(beat / step) * step)
