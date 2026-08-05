# ABOUTME: Parcels of medium with a lifetime — what a smoke machine leaves
# ABOUTME: hanging in the air long after it has stopped running.
"""Puffs.

A hazer and a smoke machine both put something in the air, and the whole
difference between them is *how much*, *how local*, and *for how long*:

* a hazer makes a thin, even layer that fills the room. Its job is to make
  everyone else's beam visible, which is the stage-wide `Atmosphere` level, and
  its ceiling is low on purpose — a hazer that could fog a room would be a smoke
  machine. What it puts in front of you is a lot of very thin parcels that
  travel a long way sideways, because that is what filling a room looks like;
* a smoke machine makes dense, local parcels that billow up, spread and thin out
  over the better part of a minute. Turning it off does not clear the stage:
  what it already made is still hanging there, and that hang is what an audience
  reads as smoke rather than as an effect switching on.

So smoke cannot be a function of the current DMX value, which is what it was
before this: five blobs whose opacity came straight off the fog channel, gone
the frame it closed. Each parcel is spawned once and then lives its own life.

**Every quantity is derived from a parcel's age, never integrated.** That is the
one design decision the rest of this file follows from: a field can be updated
at 30 Hz, at 3 Hz, or twice with an hour between, and the picture is the same —
there is no accumulated state to drift, and a stalled UI cannot leave the column
somewhere it should never have reached.

What a parcel does with that age is meant to be recognisable as smoke:

* it **rises, and stops rising**. Fog leaves the nozzle much colder and heavier
  than the room, gets pushed up by the fan, and loses that momentum as it mixes.
  A parcel that kept climbing at a constant rate would file off the top of the
  picture in formation, which is the single most obvious tell;
* it **spreads outward and downwind**. Each parcel gets its own lateral velocity
  and every parcel shares the room's slow drift, so a column becomes a layer;
* it **curls**. A little turbulence, growing as the parcel gets further from the
  nozzle and slower, and different for every parcel;
* it **grows by diffusion** — the radius goes as the square root of age, fast at
  first and then slowing, rather than linearly;
* and it **thins as it grows**, because the same fog is spread over more air.
  That is what finally makes it disappear, with the lifetime only cutting off a
  tail nobody can see.

Qt-free, and deliberately so — both renderers read the same field, so the raster
plume and the GPU density map cannot disagree about where the smoke is.
"""

import math
from dataclasses import dataclass
from typing import Iterator, List, NamedTuple, Optional, Tuple

#: How much of a puff's life is spent reaching full density. A parcel is not
#: fully formed the instant it leaves the nozzle, but it gets there fast — much
#: slower than this and the machine looks like it is lagging its own channel.
ATTACK_FRACTION = 0.08

#: What a parcel is worth the moment it appears. Not zero: fog leaving a nozzle
#: is already dense, and a machine whose first frame is invisible reads as lag.
MIN_FADE = 0.3

#: Where in its life a parcel starts being cut off. Dilution has already done
#: nearly all the thinning by here; this only stops a parcel winking out at a
#: density anyone could still see.
TAIL_START = 0.55

#: Cap on the timestep the field will integrate in one go. A stalled UI (a modal
#: dialog, a slow decode) must not spawn a ring's worth of parcels at once.
MAX_STEP_S = 0.25


class LivePuff(NamedTuple):
    """One parcel as it is *right now*, in the emitter's own coordinates."""

    x: float
    y: float
    z: float
    density: float
    radius: float
    z_extent: float
    #: How far the parcel has turned since birth, radians. Nothing about the
    #: medium depends on it — it is what stops a field of drawn parcels looking
    #: like the same stamp repeated, which is most of "smoke, not blobs".
    spin: float
    #: Which of the renderer's shapes this parcel wears, so one parcel keeps the
    #: same one for its whole life instead of flickering between them.
    variant: int


@dataclass(frozen=True)
class PuffSpec:
    """How one kind of machine makes its medium.

    The two shipped specs are at the bottom of this module. A profile picks one;
    nothing else in the render engine knows the difference between a hazer and a
    smoke machine.
    """

    #: Ceiling on the fog output this machine can reach, whatever its DMX says.
    #: A hazer driven to full is still only running a hazer, and this is the one
    #: honest way to say so. It governs the *rate*: how hard the machine works.
    max_output: float

    capacity: int          # live parcels; the oldest is recycled past this
    lifetime_s: float
    rate_hz: float         # parcels per second at full throttle
    density: float         # what one parcel is worth at full throttle
    radius_px: float       # at birth

    #: Diffusion coefficient, px² per second. The radius goes as
    #: `sqrt(r0² + 2·D·age)` — variance grows linearly with time, which is what
    #: diffusion means and why a puff balloons at first and then barely changes.
    diffusion_px2_s: float

    rise_px_s: float       # how fast it leaves the nozzle
    #: How long the parcel keeps that momentum. It rises
    #: `rise_px_s · rise_decay_s` in total and then hangs, because fog is not
    #: buoyant once it has mixed — it is a cold, heavy fluid being pushed.
    rise_decay_s: float

    spread_px_s: float     # each parcel's own lateral velocity, ± this
    wind_px_s: float       # the room's drift, shared by every parcel
    turbulence_px: float   # how far it wanders off that path
    z_extent_px: float     # how deep into the picture the parcel reaches

    def throttle(self, output: float) -> float:
        """How hard this machine is working, 0..1, for a resolved fog level.

        A hazer at DMX full is at *its* full, which is not a smoke machine's
        full — the ceiling is applied here, once, rather than being smuggled
        into every quantity downstream.
        """
        if output <= 0.0 or self.max_output <= 0.0:
            return 0.0
        return min(1.0, output / self.max_output)

    def reach_px(self) -> float:
        """How far from the nozzle this machine's fog gets before it is gone.

        The renderers size their viewport and their bounds with it, so it is
        derived from the same numbers the motion is rather than guessed at.
        """
        rise = self.rise_px_s * self.rise_decay_s
        lateral = (self.spread_px_s + self.wind_px_s) * self.lifetime_s
        radius = math.sqrt(self.radius_px ** 2
                           + 2.0 * self.diffusion_px2_s * self.lifetime_s)
        return max(rise, lateral) + radius + self.turbulence_px


class _Puff:
    """One parcel's birth conditions. Everything else is derived from its age."""

    __slots__ = ("x", "y", "z", "born", "density", "drift", "swirl", "phase",
                 "variant")

    def __init__(self, x: float, y: float, z: float, born: float,
                 density: float, drift: float, swirl: float, phase: float,
                 variant: int):
        self.x = x
        self.y = y
        self.z = z
        self.born = born
        self.density = density
        self.drift = drift        # lateral velocity, px/s
        self.swirl = swirl        # turbulence amplitude and sense, px
        self.phase = phase        # where in its own wander it starts
        self.variant = variant


class PuffField:
    """A machine's live parcels of medium.

    A ring: a smoke machine left running for an hour holds exactly `capacity`
    parcels, not thousands. Positions are derived from age rather than
    integrated, so the field cannot drift out of step with the clock however
    irregularly it is updated.
    """

    __slots__ = ("spec", "_puffs", "_live", "_pending", "_last", "_spawned")

    def __init__(self, spec: PuffSpec):
        self.spec = spec
        self._puffs: List[_Puff] = []
        self._live: List[LivePuff] = []
        # Parcels owed. Starts full: a machine that has just been switched on
        # already has fog at the nozzle, so the first frame shows something
        # rather than waiting out a spawn interval.
        self._pending = 1.0
        self._last: Optional[float] = None
        self._spawned = 0          # total ever, so scatter is stable per parcel

    # -- the one call per frame ----------------------------------------
    def update(self, now: float, output: float,
               origin: Tuple[float, float]) -> None:
        """Spawn what this frame is owed, then age everything.

        `output` is the fixture's resolved fog level, 0..1, before this
        machine's own ceiling; `origin` is the nozzle in body-local coordinates.
        """
        elapsed = 0.0 if self._last is None else max(0.0, min(MAX_STEP_S,
                                                              now - self._last))
        self._last = now

        throttle = self.spec.throttle(output)
        if throttle > 0.0:
            self._pending += throttle * self.spec.rate_hz * elapsed
            while self._pending >= 1.0:
                self._pending -= 1.0
                self._spawn(now, throttle, origin)
        else:
            # Back to "one ready at the nozzle", so bumping the machine on again
            # shows immediately instead of waiting out an interval it half-owed.
            self._pending = 1.0

        self._refresh(now)

    def _spawn(self, now: float, throttle: float,
               origin: Tuple[float, float]) -> None:
        spec = self.spec
        index = self._spawned
        self._spawned += 1
        # Deterministic scatter. `random` would make the tests unrepeatable and
        # two machines on the same patch produce visibly identical columns.
        jitter = _hash01(index)
        puff = _Puff(
            x=origin[0] + (jitter - 0.5) * spec.radius_px,
            y=origin[1] + (_hash01(index * 5 + 9) - 0.5) * spec.radius_px * 0.4,
            z=(_hash01(index * 7 + 1) - 0.5) * spec.z_extent_px,
            born=now,
            density=spec.density * throttle,
            # Signed, and biased away from the middle: a nozzle throws fog out
            # to both sides, and a Gaussian-looking spread would just make a
            # thicker column rather than a layer that reaches the wings.
            drift=_signed(_hash01(index * 13 + 3)) * spec.spread_px_s,
            swirl=_signed(_hash01(index * 17 + 5)) * spec.turbulence_px,
            phase=_hash01(index * 23 + 7) * math.tau,
            variant=index,
        )
        if len(self._puffs) < spec.capacity:
            self._puffs.append(puff)
        else:
            # Recycle the oldest. They are appended in birth order and retired
            # in it too, so index 0 is always the one nearest the end of its life.
            self._puffs.pop(0)
            self._puffs.append(puff)

    def _refresh(self, now: float) -> None:
        """Re-derive every live parcel, and retire the ones that are done."""
        spec = self.spec
        live = self._live
        live.clear()
        expired = 0
        for puff in self._puffs:
            age = now - puff.born
            if age >= spec.lifetime_s:
                expired += 1
                continue
            fraction = age / spec.lifetime_s

            # Diffusion: variance grows linearly with time, so the radius grows
            # as its square root — fast out of the nozzle, then barely at all.
            radius = math.sqrt(spec.radius_px ** 2
                               + 2.0 * spec.diffusion_px2_s * age)
            # The same fog in a bigger parcel is thinner fog. This is what
            # actually makes smoke fade; `_fade` only trims the ends.
            dilution = (spec.radius_px / radius) ** 2
            density = puff.density * dilution * _fade(fraction)
            if density <= 0.0:
                continue

            # Buoyancy the parcel loses as it mixes: it rises
            # `rise_px_s * rise_decay_s` in total and then hangs there.
            climb = spec.rise_px_s * spec.rise_decay_s * (
                1.0 - math.exp(-age / spec.rise_decay_s))
            # Turbulence builds as the parcel slows and gets further from the
            # nozzle, so the column leaves straight and the layer wanders.
            developed = 1.0 - math.exp(-age / spec.rise_decay_s)
            wander = puff.swirl * developed * math.sin(puff.phase + age * 0.55)

            live.append(LivePuff(
                x=puff.x + (puff.drift + spec.wind_px_s) * age + wander,
                y=puff.y - climb + wander * 0.35,
                z=puff.z,
                density=density,
                radius=radius,
                # A parcel spreads in depth as it spreads across the picture.
                z_extent=spec.z_extent_px * radius / spec.radius_px,
                spin=puff.phase + age * 0.22 * (1.0 if puff.swirl >= 0 else -1.0),
                variant=puff.variant,
            ))
        if expired:
            del self._puffs[:expired]

    # -- reading -------------------------------------------------------
    def __iter__(self) -> Iterator[LivePuff]:
        return iter(self._live)

    def __len__(self) -> int:
        return len(self._live)

    @property
    def settled(self) -> bool:
        """True once nothing is left in the air. The clock can stop."""
        return not self._puffs

    def clear(self) -> None:
        self._puffs.clear()
        self._live.clear()
        self._pending = 0.0
        self._last = None


def _fade(fraction: float) -> float:
    """A parcel's density envelope over its life, 0..1 of the way through.

    Up fast from an already-visible start, flat while dilution does the real
    thinning, then a soft cut so nothing winks out at a density you could still
    have seen. It is deliberately NOT the whole story: a parcel disappears
    because it has spread over more air, not because a timer said so.
    """
    if fraction < 0.0 or fraction >= 1.0:
        return 0.0
    attack = min(1.0, fraction / ATTACK_FRACTION)
    tail = 1.0 - _smoothstep(TAIL_START, 1.0, fraction)
    return (MIN_FADE + (1.0 - MIN_FADE) * attack) * tail


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = max(0.0, min(1.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


def _signed(unit: float) -> float:
    """A 0..1 hash as a ±1 spread that favours the edges over the middle.

    A nozzle throws fog outward. Sampling evenly (or worse, normally) around
    zero piles most parcels back onto the centre line, which builds a thicker
    column instead of a layer that reaches the wings.
    """
    side = 1.0 if unit >= 0.5 else -1.0
    magnitude = abs(unit - 0.5) * 2.0
    return side * (0.35 + 0.65 * magnitude)


def _hash01(index: int) -> float:
    """A stable 0..1 from a counter. Not random — repeatable."""
    return (math.sin(index * 12.9898) * 43758.5453) % 1.0


# ---------------------------------------------------------------------------
# The two machines
# ---------------------------------------------------------------------------

#: A hazer: a thin, even layer that fills the room, and a hard ceiling well
#: below full. Most of what it does is not these parcels at all — it is the
#: stage-wide `Atmosphere` level it feeds, which is what makes every other
#: fixture's beam visible. The parcels are the part you can point at: a lot of
#: them, each almost nothing, travelling far enough sideways to reach the wings.
HAZER = PuffSpec(
    max_output=0.35,
    capacity=110,
    lifetime_s=28.0,
    rate_hz=4.0,
    density=0.30,
    radius_px=95.0,
    diffusion_px2_s=2600.0,     # ~95 -> ~390 px across a life
    rise_px_s=40.0,
    rise_decay_s=6.0,           # climbs ~240 px and then hangs
    spread_px_s=62.0,           # ±1700 px over a life: the whole stage
    wind_px_s=9.0,
    turbulence_px=70.0,
    z_extent_px=140.0,
)

#: A smoke machine: dense, local, and still there long after the channel closed.
#: Fewer, fatter, faster parcels that do not travel as far, because fog stays
#: roughly where it was made — which is also why `Atmosphere` weighs it below
#: haze for the purpose of making everyone else's beam visible.
SMOKE = PuffSpec(
    max_output=1.0,
    capacity=124,
    lifetime_s=27.0,
    rate_hz=8.0,
    density=0.85,
    radius_px=58.0,
    diffusion_px2_s=1150.0,     # ~58 -> ~205 px across a life
    rise_px_s=105.0,
    rise_decay_s=3.5,           # billows up ~370 px, then hangs and spreads
    spread_px_s=44.0,           # ±750 px: a wide bank, not the whole stage
    wind_px_s=6.0,
    turbulence_px=95.0,
    z_extent_px=110.0,
)
