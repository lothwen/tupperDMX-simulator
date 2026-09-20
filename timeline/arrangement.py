"""
What a song-section edit may do, without a scene.

A song part (INTRO/VERSE/…, notes 61-69) is stored as ONE point in time: where
that section starts. The overlay draws each as a box running to the next start
(the last to the song's end), so the arrangement is a strip of contiguous
sections and every edit is really an edit of its boundaries. Two rules keep the
strip honest, and both live here so they can be tested on plain floats:

* a move never reorders or stacks: moving starts are held between the unmoved
  starts around them, `min_gap` apart (one beat), and inside [0, song end);
* a new part dropped onto the strip splits the section it lands in — unless it
  lands on a start, in which case it retypes the section starting there, which
  is what keeps a drop from stacking two parts at one instant.
"""

from bisect import bisect_left
from typing import Iterable, List, Sequence, Tuple


def spans(starts: Sequence[float], song_end: float) -> List[Tuple[float, float]]:
    """(start, end) of every section: each runs to the next start, the last to
    `song_end`. A start at or past the end yields an empty span, not a
    negative one."""
    ends = list(starts[1:]) + [song_end]
    return [(s, max(s, e)) for s, e in zip(starts, ends)]


def move_bounds(starts: Sequence[float], moving: Iterable[int],
                song_end: float, min_gap: float) -> Tuple[float, float]:
    """(lo, hi): how far, in seconds, the starts at indices `moving` may shift
    TOGETHER without crossing or crowding a start that stays put.

    Moving starts keep their spacing to each other, so only an unmoved
    neighbour, t=0 and the song end bound the shift. An arrangement that is
    already tighter than `min_gap` (an old file) may still move AWAY from the
    crowding: each bound is capped at zero, never forcing a move."""
    moving = set(moving)
    lo, hi = float("-inf"), float("inf")
    for i in moving:
        t = starts[i]
        lo = max(lo, -t)
        hi = min(hi, song_end - min_gap - t)
        if i > 0 and i - 1 not in moving:
            lo = max(lo, starts[i - 1] + min_gap - t)
        if i + 1 < len(starts) and i + 1 not in moving:
            hi = min(hi, starts[i + 1] - min_gap - t)
    if not moving:
        return 0.0, 0.0
    return min(lo, 0.0), max(hi, 0.0)


def resolve_drop(starts: Sequence[float], t: float,
                 radius: float) -> Tuple[str, int]:
    """What a part landing at `t` does: ("retype", i) when `t` is within
    `radius` of start i (the nearest, if two are), else ("insert", i) with i the
    index the new start takes."""
    nearest = min(range(len(starts)), key=lambda i: abs(starts[i] - t),
                  default=None)
    if nearest is not None and abs(starts[nearest] - t) < radius:
        return "retype", nearest
    return "insert", bisect_left(list(starts), t)
