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
  picture in formation, which is the single most obvious tell — and one that
  every parcel loses at the same moment is the second, so the push and the
  losing of it are both dealt out per parcel;
* it **disperses**. This is the term that fills a room, and the one that was
  missing: turbulent mixing is a *random walk*, so a parcel's displacement grows
  as the square root of its age in whatever direction it happened to get pushed,
  and a cloud of them expands in every direction at once. What was here before
  was only a constant lateral velocity dealt out with a bias away from the
  middle, which is a fan of straight lines — the "two-branch tree" that made
  every run of the machine look like the same run of the machine;
* it **curls**, on three octaves at once and on its own frequencies, decorrelated
  between the two axes. One sine shared by X and Y is a parcel sliding up and
  down a diagonal, which reads as machinery rather than as air;
* it **grows by diffusion** — the radius goes as the square root of age, fast at
  first and then slowing, rather than linearly;
* and it **thins as it grows**, because the same fog is spread over more air.
  That is what finally makes it disappear, with the lifetime only cutting off a
  tail nobody can see.

Qt-free, and deliberately so — both renderers read the same field, so the raster
plume and the GPU density map cannot disagree about where the smoke is, nor
(see `PARCEL_*` at the bottom) about what one of them looks like.
"""

import math
from dataclasses import dataclass
from typing import Iterator, List, NamedTuple, Optional, Tuple

#: How much of a puff's life it spends settling from what it was worth at the
#: nozzle to what its spec says it is worth. Fast — much slower than this and
#: the machine looks like it is lagging its own channel.
MIX_FRACTION = 0.08

#: What a parcel is worth the moment it appears, as a multiple of its spec's
#: `density`. Fog leaves a nozzle as a dense slug and thins as room air mixes
#: into it, so above 1 is a punch at the machine and below 1 a fade-in; either
#: way it is over by `MIX_FRACTION` of the parcel's life.
#:
#: Never 0 — a machine whose first frame is invisible reads as lag. Above 1 it
#: is what a parcel's PEAK density is measured against: `spec.density` is the
#: settled value, `spec.density * NOZZLE_DENSITY` the ceiling the light list
#: carries. The raster plume clamps its alpha at 1.0, so past that the two
#: paths draw the nozzle differently until the parcel has mixed.
NOZZLE_DENSITY = 2.0

#: Where in its life a parcel starts being cut off. Dilution has already done
#: nearly all the thinning by here; this only stops a parcel winking out at a
#: density anyone could still see.
TAIL_START = 0.55

#: Cap on the timestep the field will integrate in one go. A stalled UI (a modal
#: dialog, a slow decode) must not spawn a ring's worth of parcels at once.
MAX_STEP_S = 0.25

#: How much of a parcel's turbulent wandering is vertical, and how much of *that*
#: survives pointing downward.
#:
#: A room's eddies are not isotropic: it is bounded above and below and open
#: across, so air churns much further sideways than it does up and down. And the
#: down half is damped again because the floor is there — fog that reaches it
#: runs along it rather than through it, which is also why a stage under a smoke
#: machine has a layer round everyone's ankles instead of a hole under the
#: nozzle.
EDDY_VERTICAL = 0.55
EDDY_FLOOR = 0.35

#: The octaves a parcel meanders on: (frequency, amplitude), amplitudes summing
#: to 1 so the whole wander stays inside ±`turbulence_px`.
#:
#: Sines rather than an integrated random walk, because every quantity in this
#: module is derived from age and an integral is exactly the state that would
#: drift. Three of them at incommensurate frequencies never repeat within a
#: parcel's life, so the path reads as chaos rather than as a wobble; one alone
#: (which is what this was) is a parcel swinging on a pendulum.
WANDER_OCTAVES = ((1.0, 0.60), (2.31, 0.27), (5.13, 0.13))

#: How much slower the vertical meander runs than the horizontal one. Anything
#: but 1: at 1 the two axes beat together and the parcel travels a diagonal.
WANDER_Y_RATE = 0.71


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

    #: Live parcels; the oldest is recycled past this. Both machines are sized
    #: so `capacity / rate_hz` IS `lifetime_s` — the ring wraps at the moment
    #: the oldest parcel would have expired anyway, so a machine at full
    #: throttle holds exactly one life's worth and nothing is ever cut short.
    #: Below full it makes fewer than it can hold and the ring never fills.
    capacity: int

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

    #: How far either side of those two numbers a parcel may be dealt, 0..1. A
    #: nozzle does not push every puff the same, and a column whose every parcel
    #: stops climbing at the same instant draws a line across the stage.
    rise_spread: float

    #: Turbulent dispersion, px² per second — the eddies of the room, as opposed
    #: to `diffusion_px2_s`, which is the parcel growing where it stands. Each
    #: parcel is pushed in one direction it keeps, by `sqrt(2·K·age)`: a random
    #: walk, so the cloud spreads in every direction at once and slows as it
    #: goes. **This is what fills a room**, and the reason a machine's output no
    #: longer has to be a fan of straight lines to reach the wings.
    eddy_px2_s: float

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

    def walk_px(self) -> float:
        """How far turbulent dispersion carries a parcel over its whole life."""
        return math.sqrt(2.0 * self.eddy_px2_s * self.lifetime_s)

    def climb_px(self) -> float:
        """The highest a parcel's own buoyancy can lift it, before the walk.

        The luckiest parcel gets `rise_spread` more push AND keeps it for
        `rise_spread` longer, so both ends of the deal compound here.
        """
        return (self.rise_px_s * (1.0 + self.rise_spread)
                * self.rise_decay_s * (1.0 + self.rise_spread))

    def reach_px(self) -> float:
        """How far from the nozzle this machine's fog gets before it is gone.

        Derived from the same numbers the motion is rather than guessed at, so
        it stays honest when they are retuned. Nothing renders with it — both
        renderers bound themselves to what is actually in the air — but it is
        what the tests measure a machine's spread against.
        """
        walk = self.walk_px()
        rise = self.climb_px() + walk * EDDY_VERTICAL
        lateral = ((self.spread_px_s + self.wind_px_s) * self.lifetime_s + walk)
        radius = math.sqrt(self.radius_px ** 2
                           + 2.0 * self.diffusion_px2_s * self.lifetime_s)
        return max(rise, lateral) + radius + self.turbulence_px


class _Puff:
    """One parcel's birth conditions. Everything else is derived from its age.

    Everything a parcel does differently from its neighbours is dealt here,
    once, and then never touched again — which is what keeps `_refresh` a pure
    function of age and what makes two machines on the same patch look like two
    machines rather than one drawn twice.
    """

    __slots__ = ("x", "y", "z", "born", "density", "drift", "rise", "decay",
                 "walk_x", "walk_y", "swirl_x", "swirl_y", "phase_x", "phase_y",
                 "rate", "variant")

    def __init__(self, x: float, y: float, z: float, born: float,
                 density: float, drift: float, rise: float, decay: float,
                 walk_x: float, walk_y: float, swirl_x: float, swirl_y: float,
                 phase_x: float, phase_y: float, rate: float, variant: int):
        self.x = x
        self.y = y
        self.z = z
        self.born = born
        self.density = density
        self.drift = drift        # lateral velocity, px/s
        self.rise = rise          # this parcel's own push out of the nozzle
        self.decay = decay        # and how long it keeps it, seconds
        self.walk_x = walk_x      # the direction its eddy took it, unit-ish
        self.walk_y = walk_y
        self.swirl_x = swirl_x    # meander amplitude and sense, px, per axis
        self.swirl_y = swirl_y
        self.phase_x = phase_x    # where in its own wander it starts, per axis
        self.phase_y = phase_y
        self.rate = rate          # how fast that wander runs
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
        # Which way this parcel's eddy pushed it. Uniform in angle, then
        # squashed vertically and again downward, because a room's turbulence
        # is not isotropic and the floor is in the way.
        angle = _hash01(index * 29 + 11) * math.tau
        walk_y = math.sin(angle) * EDDY_VERTICAL
        if walk_y > 0.0:                      # stage down is +Y
            walk_y *= EDDY_FLOOR
        puff = _Puff(
            x=origin[0] + (jitter - 0.5) * spec.radius_px,
            y=origin[1] + (_hash01(index * 5 + 9) - 0.5) * spec.radius_px * 0.4,
            z=(_hash01(index * 7 + 1) - 0.5) * spec.z_extent_px,
            born=now,
            density=spec.density * throttle,
            # Uniform, and NOT biased away from the middle. It used to be: a
            # nozzle does throw fog out to both sides, so the magnitude was kept
            # off zero to stop the column piling back onto its own centre line.
            # But a constant velocity dealt with a two-sided bias is precisely a
            # fan of straight lines leaving in two directions — the reported
            # "two-branch tree", and the same tree every time the machine ran.
            # Reaching the wings is `eddy_px2_s`'s job now, and a random walk
            # fills the space between the branches instead of avoiding it.
            drift=(_hash01(index * 13 + 3) - 0.5) * 2.0 * spec.spread_px_s,
            rise=spec.rise_px_s * _spread(_hash01(index * 31 + 17),
                                          spec.rise_spread),
            decay=spec.rise_decay_s * _spread(_hash01(index * 37 + 21),
                                              spec.rise_spread),
            walk_x=math.cos(angle),
            walk_y=walk_y,
            # Signed and independent per axis: one amplitude shared by both is a
            # parcel sliding up and down a 45 degree line.
            swirl_x=(_hash01(index * 17 + 5) - 0.5) * 2.0 * spec.turbulence_px,
            swirl_y=(_hash01(index * 19 + 25) - 0.5) * 2.0 * spec.turbulence_px,
            phase_x=_hash01(index * 23 + 7) * math.tau,
            phase_y=_hash01(index * 41 + 33) * math.tau,
            # Its own clock, so no two parcels curl in step even where they
            # happened to be dealt the same phase.
            rate=0.35 + 0.55 * _hash01(index * 43 + 39),
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

            # Buoyancy the parcel loses as it mixes: it rises `rise * decay` in
            # total and then hangs there. Both are its own, so a bank of smoke
            # has a ragged top rather than a hard line where every parcel ran
            # out of push at once.
            climb = puff.rise * puff.decay * (1.0 - math.exp(-age / puff.decay))
            # Turbulent dispersion. Displacement goes as the square root of age
            # because this is a random walk, not a velocity — which is why a
            # cloud of these fills the room instead of leaving it in formation.
            walk = math.sqrt(2.0 * spec.eddy_px2_s * age)
            # Meander builds as the parcel slows and gets further from the
            # nozzle, so the column leaves straight and the layer wanders.
            developed = 1.0 - math.exp(-age / puff.decay)
            swing = age * puff.rate
            wander_x = puff.swirl_x * developed * _wander(swing, puff.phase_x)
            wander_y = puff.swirl_y * developed * _wander(
                swing * WANDER_Y_RATE, puff.phase_y)

            live.append(LivePuff(
                x=(puff.x + (puff.drift + spec.wind_px_s) * age
                   + puff.walk_x * walk + wander_x),
                y=puff.y - climb + puff.walk_y * walk + wander_y,
                z=puff.z,
                density=density,
                radius=radius,
                # A parcel spreads in depth as it spreads across the picture.
                z_extent=spec.z_extent_px * radius / spec.radius_px,
                spin=puff.phase_x + age * 0.22 * (
                    1.0 if puff.swirl_x >= 0 else -1.0),
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

    Off the nozzle to what it settles at, flat while dilution does the real
    thinning, then a soft cut so nothing winks out at a density you could still
    have seen. It is deliberately NOT the whole story: a parcel disappears
    because it has spread over more air, not because a timer said so.
    """
    if fraction < 0.0 or fraction >= 1.0:
        return 0.0
    mixed = min(1.0, fraction / MIX_FRACTION)
    tail = 1.0 - _smoothstep(TAIL_START, 1.0, fraction)
    return (NOZZLE_DENSITY + (1.0 - NOZZLE_DENSITY) * mixed) * tail


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = max(0.0, min(1.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


def _spread(unit: float, fraction: float) -> float:
    """A 0..1 hash as a multiplier within ±`fraction` of 1."""
    return 1.0 + (unit - 0.5) * 2.0 * fraction


def _wander(t: float, phase: float) -> float:
    """A parcel's meander at its own scaled time `t`, in ±1.

    Three octaves at incommensurate frequencies: the path never repeats inside
    a parcel's life, so what it does next is not something the eye can predict
    from what it just did. That unpredictability IS the thing being asked for
    here — one sine is a pendulum, and a stage full of pendulums swinging at one
    rate is the effect that was there before.
    """
    total = 0.0
    for frequency, amplitude in WANDER_OCTAVES:
        total += amplitude * math.sin(phase * frequency + t * frequency)
    return total


def _hash01(index: int) -> float:
    """A stable 0..1 from a counter. Not random — repeatable."""
    return (math.sin(index * 12.9898) * 43758.5453) % 1.0


# ---------------------------------------------------------------------------
# What one parcel looks like
#
# Here, and not in either renderer, for the same reason the field is here: the
# raster plume bakes these into its sprite sheet and `gpu/shaders.py`
# interpolates the identical numbers into MEDIUM_FRAG, so the two paths cannot
# draw different smoke from the same parcels.
#
# They are the shape, not the pixels. The two paths hash their noise
# differently — one in Python at build time, one in GLSL per fragment — so a
# given parcel is not the same *picture* on both. It is the same KIND of
# picture, which is all that was ever asked: same octaves, same weights, same
# mean, so a bank of smoke has the same statistics whichever renderer drew it.
# ---------------------------------------------------------------------------

#: The soft disc a parcel is before any noise, `exp(-FALLOFF·r²)`, cut to
#: nothing between `RIM` and the edge so no parcel can ever show its own square.
#:
#: `RIM` is a *purely radial* term, which makes it the one thing here that can
#: force a circle no matter what the noise does — and at 0.62 it owned the outer
#: 38% of the radius, which is exactly the band (r ≈ 0.56–0.89) where the noise
#: was pushing the outline in and out. So it is far out now, and `FALLOFF` is
#: steep enough that the disc has already faded by the time it engages. Pushing
#: the rim out *without* steepening was tried and is worse, not better: the
#: parcel then ends in a hard circular edge instead of a soft circular fade.
PARCEL_FALLOFF = 3.0
PARCEL_RIM = 0.92

#: The two octaves of value noise that break that disc up, in cycles across the
#: parcel, and how the coarse one is weighed against the fine.
#:
#: This is the whole difference between smoke and a field of balls. A parcel
#: drawn as a radial gradient is a *ball*, and no matter how many are stacked
#: the eye finds the circles — which is exactly what the GPU path was doing,
#: because the medium map rasterised each parcel as a clean Gaussian and left
#: all the structure to a stage-wide noise field sliding along behind it.
#:
#: `COARSE` is the one that matters for the *silhouette*: ~2 lattice cells
#: across the parcel, so it is a handful of big lobes rather than grain, and it
#: is what the outline's bumps and notches are. `FINE` only ragged-edges those
#: lobes, which is why `MIX` leans well toward the coarse one.
PARCEL_NOISE_COARSE = 1.1
PARCEL_NOISE_FINE = 5.0
PARCEL_NOISE_MIX = 0.75

#: How that noise modulates the disc: `max(0, FLOOR + GAIN · noise)`.
#:
#: **The floor is negative on purpose, and it is the whole trick.** Value noise
#: does not span 0..1 in practice — over a parcel these two octaves measure
#: 0.11..0.90, bunched hard around 0.516 with a standard deviation of 0.145. Fed
#: through a positive floor the modulation could only reach 0.45×..1.56× and
#: **never zero**, so the disc showed through everywhere: whatever the noise did,
#: the outline it was drawn on stayed a circle. Taking the floor below zero lets
#: the low tail carve right through to nothing, which is what turns the
#: silhouette into lobes and notches with holes between them — about a third of
#: a parcel is now genuinely empty.
#:
#: The two are **coupled**, and `_parcel_noise_floor` is the coupling: the noise
#: averages 0.516, so the mean modulation is held at 1 and the raggedness moves
#: density around without adding or removing any. That is what keeps this a
#: *texture* control rather than a second density control — turn `GAIN` up to
#: make a parcel less like a circle, and the bank does not get brighter with it.
PARCEL_NOISE_GAIN = 5.35

#: Where the noise sits on average. Not a free parameter — see above.
PARCEL_NOISE_MEAN = 0.516


def _parcel_noise_floor(gain: float) -> float:
    """The floor that keeps `FLOOR + GAIN · noise` averaging 1."""
    return 1.0 - gain * PARCEL_NOISE_MEAN


PARCEL_NOISE_FLOOR = _parcel_noise_floor(PARCEL_NOISE_GAIN)

#: How many shapes there are to deal out. One shape repeated a hundred times is
#: a wallpaper pattern; a parcel picks one for life and turns slowly, so a bank
#: never shows the same shape twice in the same orientation. Eight rather than
#: five because a bank draws ~50 parcels, and five shapes repeat ten times each.
PARCEL_VARIANTS = 8


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
    capacity=100,
    lifetime_s=20.0,            # long enough to reach the far wall and hang
    rate_hz=5.0,                # 100 at 5 Hz: the ring wraps as the oldest goes
    density=0.34,
    radius_px=95.0,
    diffusion_px2_s=1200.0,     # ~95 -> ~240 px over a life
    rise_px_s=46.0,
    rise_decay_s=9.0,           # climbs ~415 px given the time, ~370 within one
    rise_spread=0.45,           # and anywhere from ~125 to ~870, per parcel
    eddy_px2_s=9000.0,          # ~600 px of random walk over a life
    spread_px_s=22.0,           # a gentle fan; the walk does the reaching
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
    capacity=400,
    lifetime_s=20.0,
    rate_hz=20.0,               # a fat bank fast: 400 at 20 Hz is that same life
    density=0.85,
    radius_px=58.0,
    diffusion_px2_s=560.0,      # ~58 -> ~160 px over a life
    rise_px_s=200.0,
    rise_decay_s=2.2,           # billows up ~440 px, then hangs and spreads
    rise_spread=0.40,           # ~160 to ~860, per parcel
    eddy_px2_s=4600.0,          # ~430 px of random walk over a life
    spread_px_s=66.0,
    wind_px_s=6.0,
    turbulence_px=95.0,
    z_extent_px=110.0,
)
