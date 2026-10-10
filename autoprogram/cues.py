# ABOUTME: A cue — one lane's look at one moment, with the priority that lets
# ABOUTME: a hit override a section's look for a while — and resolve(), which
# ABOUTME: settles a lane's cues into what is written. Qt-free.
"""Cues: what the director decides, before anything is authored.

A cue is in AUDIO seconds. A cue with a HOLD is an override: for that long,
cues weaker than it are dropped, and when it closes on nothing the section's
own look comes back.
"""
from dataclasses import dataclass
from typing import List, Optional

# Cue priorities: a cue is dropped inside the window of a stronger one.
BASE, BUILD, ACCENT, IMPACT, STOP, FINAL = 0, 1, 2, 3, 4, 5
MIN_GAP = 0.102        # seconds between two clips of one lane (a note lasts 0.1 s)


@dataclass
class Cue:
    t: float                       # AUDIO seconds
    values: Optional[dict]         # None: blackout
    name: str
    prio: int = BASE
    hold: float = 0.0              # an override's window, from t
    fade_in: float = 0.0
    fade_out: float = 0.0


def resolve(cues: List[Cue], end: float) -> List[Cue]:
    """Drop cues inside a stronger cue's window, put the base look back when
    a window closes on nothing, and keep MIN_GAP between clips."""
    cues = sorted((c for c in cues if c.t <= end + 1e-6), key=lambda c: (c.t, -c.prio))
    windows = [c for c in cues if c.hold > 0]

    def covered(c, at=None):
        at = c.t if at is None else at
        return any(w is not c and w.prio > c.prio and w.t - MIN_GAP < at < w.t + w.hold
                   for w in windows)

    kept = [c for c in cues if not covered(c)]
    base = [c for c in cues if c.prio == BASE]
    for w in [c for c in kept if c.hold > 0]:
        close = w.t + w.hold
        if close >= end - MIN_GAP or covered(w, close):
            continue
        if any(c is not w and close - MIN_GAP <= c.t <= close + 2 * MIN_GAP for c in kept):
            continue                             # something lands there anyway
        prior = [c for c in base if c.t <= close]
        if prior:
            kept.append(Cue(close, prior[-1].values, prior[-1].name))
    kept.sort(key=lambda c: (c.t, -c.prio))
    spaced: List[Cue] = []
    for c in kept:
        if spaced and c.t - spaced[-1].t < MIN_GAP:
            if c.prio > spaced[-1].prio:
                spaced[-1] = c
            continue
        spaced.append(c)
    return spaced
