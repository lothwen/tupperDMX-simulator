# ABOUTME: The stage-wide haze level — what makes any beam visible at all.
"""Atmosphere.

A beam of light is invisible in clean air; you see it because there is haze for
it to scatter off. That makes beam visibility the one genuinely *cross-fixture*
quantity on the stage — a hazer downstage lights up a moving head's beam on the
other side of the rig — so it can't live on a body. One of these belongs to the
stage, and every body reads it.

Any fixture whose capabilities resolve a fog output contributes, not just the
ones categorised Hazer: plenty of movers ship with a hazer inside.
"""

from typing import Dict, Optional

# Haze hangs in a room: it builds in a couple of seconds and takes the better
# part of a minute to clear, and a beam that appears and vanishes with the
# hazer's DMX looks wrong in a way people notice immediately.
ATTACK_S = 1.5
DECAY_S = 12.0

# Fog is denser but stays low and local; haze is what fills the air. A smoke
# machine still helps the beams, just less than a hazer running the same output.
_KIND_WEIGHT = {"Haze": 1.0, "Fog": 0.6}
DEFAULT_BASELINE = 0.15


class Atmosphere:
    """How much haze is in the air, 0..1, and who is putting it there.

    Plain Python, no Qt: every body reads `level` on the frame it is already
    drawing, so nothing needs to be signalled — and a module-level QObject built
    at import time is invalid by the time the app starts.
    """

    def __init__(self, baseline: float = DEFAULT_BASELINE):
        self._baseline = _clamp(baseline)
        self._contributions: Dict[int, float] = {}
        self._level = self._baseline
        self._last: Optional[float] = None

    # -- what the stage reads ------------------------------------------
    @property
    def level(self) -> float:
        return self._level

    @property
    def baseline(self) -> float:
        return self._baseline

    def set_baseline(self, value: float) -> None:
        """The manual floor — a room that is already hazy before any fixture."""
        self._baseline = _clamp(value)

    # -- what fixtures write -------------------------------------------
    def contribute(self, key: int, output: float, kind: str = "") -> None:
        amount = _clamp(output) * _KIND_WEIGHT.get(kind, 1.0)
        if self._contributions.get(key) != amount:
            self._contributions[key] = amount

    def release(self, key: int) -> None:
        self._contributions.pop(key, None)

    @property
    def target(self) -> float:
        """Where the level is heading. Sources combine without ever exceeding 1."""
        clear = 1.0 - self._baseline
        for amount in self._contributions.values():
            clear *= (1.0 - amount)
        return 1.0 - clear

    def advance(self, now: float) -> float:
        """Move the level toward its target. Called once per clock tick."""
        target = self.target
        elapsed = 0.0 if self._last is None else max(0.0, min(0.25, now - self._last))
        self._last = now

        span = ATTACK_S if target > self._level else DECAY_S
        self._level += (target - self._level) * min(1.0, elapsed / span)
        return self._level


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


# The atmosphere a body uses when nothing gave it one — the scene preview, which
# has no stage and no hazer, just the room's baseline.
DEFAULT_ATMOSPHERE = Atmosphere()
