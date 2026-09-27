# ABOUTME: What the click plays and when it is on — beat events off a TempoMap
# ABOUTME: and the stop-click rule. Qt-free; the scheduler lives in app/metronome.
"""The click's musical half, apart from any clock or sound card.

`app/metronome.py` decides WHEN a beat reaches the output (anchor, lookahead,
frames); this module decides WHAT that beat is — its time on the timeline, its
number in the bar, and the kind of sound it takes — and whether the click
should be sounding at a position at all. The sound for each kind is
`audio/click.py`'s.

A beat is addressed by its continuous INDEX on the tempo map (negative through
a count-in), never by a free-running counter: the accent then survives a tempo
change, and a count-in's beats land on the same bar cycle the song continues
with, because Python's % is floor-based. The bar is the map's meter.
"""

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from core.markers import KIND_CLICK_STOP
from core.tempo_map import BEATS_PER_BAR, TempoMap

#: The kinds of sound a click event takes (keys of `audio.click.ClickSounds`).
KIND_ACCENT = "accent"   # the downbeat, beat 1 — unless the accent is off
KIND_BEAT = "beat"       # every other beat
KINDS = (KIND_ACCENT, KIND_BEAT)


@dataclass(frozen=True)
class ClickEvent:
    index: int       # continuous beat index on the tempo map
    seconds: float   # timeline time it falls at
    number: int      # 1..beats per bar
    kind: str        # one of KINDS


def beat_number(index: int, beats_per_bar: int = BEATS_PER_BAR) -> int:
    """The count in the bar of beat `index`, from 1 (floor-based, so negative
    indices work)."""
    return (int(index) % beats_per_bar) + 1


def event_at(tempo_map: TempoMap, index: int,
             accent: bool = True) -> ClickEvent:
    """Beat `index` as the click sounds it. With `accent` off, beat 1 takes
    the same sound as every other beat — its NUMBER stays 1, since the bar is
    still counted (the spoken count-in and the web remote read it)."""
    number = beat_number(index, tempo_map.beats_per_bar)
    return ClickEvent(int(index), tempo_map.seconds_at_beat(index), number,
                      KIND_ACCENT if accent and number == 1 else KIND_BEAT)


def first_index_at(tempo_map: TempoMap, seconds: float) -> int:
    """The first beat at or after `seconds` — ceil(), so a beat already behind
    that position is not sounded late."""
    return math.ceil(tempo_map.beat_at(seconds))


def click_stop_time(markers: Sequence) -> Optional[float]:
    """When the click stops, or None if it never does.

    The EARLIEST stop-click wins: the marker is one-way (there is no
    start-click), so a second one further down the song changes nothing."""
    stops = [float(m.time) for m in markers if m.kind == KIND_CLICK_STOP]
    return min(stops) if stops else None


def click_stopped_at(markers: Sequence, position: float) -> bool:
    """Whether the click is silent at `position` — the question every seek,
    resume and song load asks, since the marker's effect outlives its crossing."""
    stop = click_stop_time(markers)
    return stop is not None and position >= stop
