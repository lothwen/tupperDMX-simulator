# ABOUTME: What the click plays and when it is on — beat events off a TempoMap
# ABOUTME: and the stop-click rule. Qt-free; the scheduler lives in app/metronome.
"""The click's musical half, apart from any clock or sound card.

`app/metronome.py` decides WHEN a beat reaches the output (anchor, lookahead,
frames); this module decides WHAT that beat is — its time on the timeline, its
1-4 number, and the kind of sound it takes — and whether the click should be
sounding at a position at all. The sound for each kind is `audio/click.py`'s.

A beat is addressed by its continuous INDEX on the tempo map (negative through
a count-in), never by a free-running counter: the accent then survives a tempo
change, and a count-in's beats land on the same 1-4 cycle the song continues
with, because Python's % is floor-based.
"""

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from core.markers import KIND_CLICK_STOP
from core.tempo_map import BEATS_PER_BAR, TempoMap

#: The kinds of sound a click event takes (keys of `audio.click.ClickSounds`).
KIND_ACCENT = "accent"   # the downbeat, beat 1
KIND_BEAT = "beat"       # every other beat
KINDS = (KIND_ACCENT, KIND_BEAT)


@dataclass(frozen=True)
class ClickEvent:
    index: int       # continuous beat index on the tempo map
    seconds: float   # timeline time it falls at
    number: int      # 1..BEATS_PER_BAR
    kind: str        # one of KINDS


def beat_number(index: int) -> int:
    """The 1-4 count of beat `index` (floor-based, so negative indices work)."""
    return (int(index) % BEATS_PER_BAR) + 1


def event_at(tempo_map: TempoMap, index: int) -> ClickEvent:
    """Beat `index` as the click sounds it."""
    number = beat_number(index)
    return ClickEvent(int(index), tempo_map.seconds_at_beat(index), number,
                      KIND_ACCENT if number == 1 else KIND_BEAT)


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
