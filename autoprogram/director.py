# ABOUTME: The director of a generated show: the rig as lanes with roles,
# ABOUTME: each section's plan (tier, lead, colours, cast, choreography) on the
# ABOUTME: song's arc to its climax, and every lane's cues. Qt-free.
"""The show's dramaturgy, from the song map to every lane's cues.

A section lights as much of the rig as it has energy for, and the song as a
whole builds to its climax. A quiet section lights only its LEAD role — just
its middle, dimmed, where the song opens from nothing or dies away after its
climax. A mid or loud one lights a CAST: the lead first, then the rig's other
units in the order the song brought them in, more of them the louder the
section and the nearer the climax — so a part coming back lights at least
what it lit before. On a rig of RESERVE_MIN units or more (blinders aside),
the outermost unit of the last role in is held back: the CLIMAX opens the
stage out to its width, the first time the whole rig is lit together.

A loud CHORUS is lit as the climax is — everything but that reserve, its
looks changing fast, landing on a hit with a blackout breath where a build
leads into it — so the song's hook stands out every time it comes. The
climax stays above it: it opens out to the reserve, changes its looks twice
as often and strobes into its phrase lines.

Every MATERIAL gets its looks, colours and CHOREOGRAPHY once, so a chorus
looks the same each time it comes back: its cast lit together, with looks in
turn, alternating, chased from the middle out, or stacked a unit at a time —
never the choreography of the part before it. A loud section keeps its cast
lit, alternating only one of four units or more; the turns are for the
quieter ones.

The flashes mark the song's shape rather than every loud moment: the band's
first entrance and every step up to loud land as a hit. Before the climax a
flash or a build lights only its section's cast and the blinders, and the
BLINDERS are kept for the song's own hits — its impacts, the landing after a
build — never on its kicks, its snare or every chorus. Only in the song's
last stretch (its last chorus, the climax, the outro) may they also strobe,
in short bursts: the last beat of a build or a climax fill.

Two fixtures of one type either side of the stage are a MIRRORED pair, one
fixture to the eye: they play the same look at the same moment, and a hit
lands on both. One side alone looks like a mistake, so the only time a pair
splits is a quick answer — hits close enough that the other side replies
before the first is forgotten.

Everything here is in AUDIO seconds; the writer adds the track's offset.
"""
import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from autoprogram import analysis
from autoprogram.colours import PART_COLOURS, WHITE, hue
from autoprogram.cues import ACCENT, BASE, BUILD, FINAL, IMPACT, MIN_GAP, STOP, Cue
from autoprogram.looks import BAND, FULL, GLOW, LEAD, PEAK, Look, Looks, Voice
from core import fixture_library as fl

GLOW_ENERGY = 0.2      # a quiet section below this energy only glows
GLOW_SCALE = 0.5       # how much of its look a glowing lane gives

PACE_STEPS = 8         # the pace runs 0..PACE_STEPS: one step, one power of two at PEAK
DEFAULT_PACE = 4
# How often a section's look changes, as a power of two of bars, going from
# the first exponent at pace 0 to the second at PACE_STEPS: from one look a
# section down to one every 4 bars glowing, every 2 bars on the lead, every
# bar for the band, every 2 beats at full and every beat at the climax (whose
# curve runs a notch steeper, so it is the busiest at any pace but 0).
# From SECTION_EXP up, one look holds the whole section.
SECTION_EXP = 5
PACE = {GLOW: (5, 2), LEAD: (5, 1), BAND: (5, 0), FULL: (5, -1), PEAK: (5, -3)}
PULSE_BEATS = 2        # a chase's dark steps play only when looks change this often
CYCLE = 4              # most looks a material cycles through on one lane
BLINDER_HOLD, BLINDER_FADE = 1.6, 1.3   # a blinder's flash on a hit, held then dying away
FILL_BARS = (8, 4)     # the climax strobes into a line this many bars apart, below / from pace 4
MIDDLE_SHARE = 0.1     # a lane this near the rig's middle (of the rig's width) stands in it
ANSWER_BARS = 0.5      # hits this close may alternate sides; further apart a pair hits as one

# How a section's cast takes turns, chosen once per material: every unit lit
# on one look, every unit lit on looks a step apart, every other unit lit and
# swapping, one unit at a time from the middle out, or a unit more each step.
TOGETHER, IN_TURN, ALTERNATE, CHASE, STACK = "together", "in turn", "alternate", "chase", "stack"
MOVING = (ALTERNATE, CHASE, STACK, IN_TURN)   # for a material first heard at most mid-loud
FILLING = (TOGETHER, IN_TURN, ALTERNATE)      # for one first heard loud
STEPS_PER_LOOK = 4     # a choreography steps this many times for each look change
# The share of the units with something to play that a mid / loud section
# lights, from the song's start to its climax: at least two of them.
BAND_SHARE = (0.35, 0.7)
FULL_SHARE = (0.67, 1.0)
RESERVE_MIN = 3        # a rig of this many units (blinders aside) keeps one for the climax


@dataclass(frozen=True)
class Style:
    """The choices a user may move (the app's Advanced panel). The defaults
    are the values above."""
    glow_energy: float = GLOW_ENERGY
    glow_scale: float = GLOW_SCALE
    cycle: int = CYCLE


@dataclass(frozen=True)
class Feel:
    """A preset of the pace and the Style: the app's Feel slider."""
    name: str
    pace: int
    style: Style


# Slowest first: a slower feel changes looks less often, cycles fewer looks,
# lets more quiet sections only glow and glows dimmer. Steady is the defaults.
FEELS = (
    Feel("Still", 0, Style(glow_energy=0.4, glow_scale=0.3, cycle=1)),
    Feel("Calm", 2, Style(glow_energy=0.3, glow_scale=0.4, cycle=2)),
    Feel("Steady", DEFAULT_PACE, Style()),
    Feel("Busy", 6, Style(glow_energy=0.1, glow_scale=0.6, cycle=6)),
    Feel("Frantic", PACE_STEPS, Style(glow_energy=0.0, glow_scale=0.7, cycle=8)),
)
DEFAULT_FEEL = 2


def look_beats(tier, x: float, beats_per_bar: int, climax: bool = False) -> Optional[float]:
    """Beats between two looks of a section at `tier`, `x` of the way up the
    pace (0-1) — the climax's twice as fast as a chorus's, down to half a
    beat; None: one look for the whole section."""
    start, end = PACE[tier]
    exp = math.floor(start + (end - start) * x + 0.5)
    if exp >= SECTION_EXP:
        return None
    beats = max(1, int(round(beats_per_bar * 2.0 ** exp)))
    return beats / 2 if climax else beats


# ---------------------------------------------------------------------------
# The rig
# ---------------------------------------------------------------------------
@dataclass
class Lane:
    channel: int
    voice: Voice
    role: str
    x: float
    hanging: bool
    twins: List[int] = field(default_factory=list)
    cues: List[Cue] = field(default_factory=list)
    span: Tuple[int, int] = (0, 0)               # the DMX addresses it drives
    looks: Optional[Looks] = None
    last: Optional[dict] = None                  # the frame its last section ended on
    used: set = field(default_factory=set)       # the library scenes it plays
    positions: List[float] = field(default_factory=list)    # x of every body it drives
    # the lane of its type rigged alike across the stage, which it lights with
    mirror: Optional["Lane"] = field(default=None, compare=False, repr=False)

    def add(self, *args, **kwargs):
        self.cues.append(Cue(*args, **kwargs))


def _positions(inst) -> List[float]:
    return [float(p.x) for p in inst.bars] or [float(inst.x)]


@dataclass
class Skipped:
    """A patched fixture no lane drives, and why."""
    channel: int
    type: str
    reason: str                                  # "not resolvable" / "nothing to program"

    def __str__(self):
        return f"ch {self.channel} {self.type}: {self.reason}"


def build_rig(fixtures):
    """``(lanes, skipped)`` for a patch: one lane per programmable fixture
    (twins on one address share it, and stand where both do), mirrored
    pairs matched, and what was left out."""
    lanes: List[Lane] = []
    skipped: List[Skipped] = []
    by_address: Dict[tuple, Lane] = {}
    for inst in sorted(fixtures, key=lambda f: f.channel):
        fdef = fl.resolve_fixture(inst.type, inst.mode)
        if fdef is None or inst.channel < 0:
            skipped.append(Skipped(inst.channel, inst.type, "not resolvable"))
            continue
        key = (inst.dmx_start, fdef.name, fdef.mode)
        if key in by_address:                        # same DMX: one lane drives both
            by_address[key].twins.append(inst.channel)
            by_address[key].positions += _positions(inst)
            continue
        voice = Voice(fdef)
        role = voice.role()
        if role is None:
            skipped.append(Skipped(inst.channel, inst.type, "nothing to program"))
            continue
        lane = Lane(inst.channel, voice, role, 0.0, bool(inst.hanging),
                    positions=_positions(inst))
        lane.span = (inst.dmx_start, inst.dmx_start + fdef.channel_count - 1)
        lane.looks = Looks(voice, role, lane.hanging)
        by_address[key] = lane
        lanes.append(lane)
    for lane in lanes:
        lane.x = sum(lane.positions) / len(lane.positions)
    pair_mirrors(lanes)
    return lanes, skipped


def middle(lanes: List[Lane]) -> Tuple[float, float]:
    """``(x, reach)``: the middle of the rig — half-way across everything it
    hangs, fog machines aside — and how near it a lane stands in it."""
    xs = [x for lane in lanes if lane.role != "fog" for x in lane.positions]
    if not xs:
        return 0.0, 0.0
    return (min(xs) + max(xs)) / 2, MIDDLE_SHARE * (max(xs) - min(xs))


def pair_mirrors(lanes: List[Lane]) -> None:
    """Pair every lane with one of its fixture type, mode and rigging on the
    other side of the rig's middle, the closest mirror images first. A lane
    standing in the middle, or with no counterpart, stays on its own."""
    mid, reach = middle(lanes)
    kinds: Dict[tuple, List[Lane]] = {}
    for lane in lanes:
        if lane.role != "fog":
            fdef = lane.voice.fdef
            kinds.setdefault((fdef.name, fdef.mode, lane.hanging), []).append(lane)
    for kind in kinds.values():
        left = [lane for lane in kind if lane.x < mid - reach]
        right = [lane for lane in kind if lane.x > mid + reach]
        for a, b in sorted(((a, b) for a in left for b in right),
                           key=lambda p: abs((mid - p[0].x) - (p[1].x - mid))):
            if a.mirror is None and b.mirror is None:
                a.mirror, b.mirror = b, a


def clashes(lanes: List[Lane]) -> List[str]:
    """Different fixtures patched on shared DMX addresses, a line each."""
    out = []
    for i, a in enumerate(lanes):
        for b in lanes[i + 1:]:
            if a.span[0] <= b.span[1] and b.span[0] <= a.span[1]:
                out.append(f"ch {a.channel} ({a.voice.fdef.name}, DMX {a.span[0]}-{a.span[1]}) "
                           f"and ch {b.channel} ({b.voice.fdef.name}, DMX {b.span[0]}-"
                           f"{b.span[1]}) overlap: each will write over the other")
    return out


# ---------------------------------------------------------------------------
# The show
# ---------------------------------------------------------------------------
@dataclass
class Plan:
    """How a section is lit."""
    tier: int                      # GLOW / LEAD / BAND / FULL / PEAK
    lead: Optional[str]            # the role a sparse section lights first
    colours: Tuple[tuple, tuple]   # its material's two colours
    pattern: str                   # how its cast takes turns: TOGETHER, IN_TURN, ALTERNATE,
                                   # CHASE or STACK (a mirrored pair is always one unit)
    occurrence: int                # how many times its material played before
    arc: float = 0.0               # how far the song has come to its climax, 0-1
    cast: List[List[Lane]] = field(default_factory=list)   # the units it lights, in order

    @property
    def unison(self) -> bool:
        """A role's lanes play one look, and hits land a unit at a time."""
        return self.pattern in (TOGETHER, STACK)


class Director:
    """Writes every lane's cues from the song map, in audio seconds."""

    def __init__(self, song: analysis.SongMap, lanes: List[Lane], palette: dict,
                 pace: int = DEFAULT_PACE, seed: int = 0, style: Style = Style()):
        self.song, self.pal, self.style = song, palette, style
        self.x = max(0, min(PACE_STEPS, pace)) / PACE_STEPS
        self.beat, self.bar = song.beat, song.bar_length
        self.num = song.beats_per_bar
        self.bars = {b.number: b for b in song.bars}
        self.role = {r: sorted([lane for lane in lanes if lane.role == r], key=lambda x: x.x)
                     for r in ("wash", "blinder", "mover", "pixel", "fog")}
        self.lanes = lanes
        self.middle = middle(lanes)[0]
        # each lane's place among its role's units, the middle first
        self.unit = {lane.channel: k for r in self.role
                     for k, unit in enumerate(self.units(self.role[r])) for lane in unit}
        # a song cut dead goes dark when its sound stops, not at the bar line
        self.end = song.cut if song.ending == "cut" and song.cut else song.end_of_sound
        self.rng = random.Random(seed)
        self.hues = [h for h in (hue(palette[k]) for k in ("primary", "secondary", "contrast"))
                     if h is not None]
        self.cycles: Dict[tuple, List[Look]] = {}
        self.reveal: List[str] = []      # the roles in the order the song brings them in
        self.reserve: set = set()        # the channels held back for the climax
        self.plans = self.plan()
        self.stretch = self.last_stretch()
        # The accents are the hits of the quiet passages. A slower show keeps
        # fewer: strongest first, up to 4 bars apart at pace 0. Onset
        # strengths saturate, so ties are common — the gap spreads them.
        gap = 4 * self.bar * (1 - self.x)
        kept: List[float] = []
        for at, _s in sorted(song.accents, key=lambda a: -a[1]):
            if all(abs(at - k) >= gap for k in kept):
                kept.append(at)
        self.accent_times = sorted(kept)

    # -- time ---------------------------------------------------------------
    def t(self, bar: int, beat: float = 1, sixteenth: int = 1) -> float:
        return self.song.time(bar, beat, sixteenth)

    def bars_of(self, sec):
        return [b for b in self.song.bars if sec.first <= b.number <= sec.last]

    def pace(self, tier) -> Optional[int]:
        """Beats between two looks of a section at `tier`; None: one look
        for the whole section."""
        return look_beats(tier, self.x, self.num)

    def beats(self, k) -> Optional[float]:
        """Beats between two looks of section `k`: its tier's pace — the
        climax's twice as fast as a chorus's, down to half a beat."""
        return look_beats(self.plans[k].tier, self.x, self.num, k == self.song.climax)

    def grid(self, sec, beats: Optional[float]) -> List[Tuple[float, Optional[analysis.Bar]]]:
        """``(time, bar)`` of every look change in `sec`: its start, then
        every `beats` beats. A build bar belongs to the build."""
        out = [(sec.start, self.bars.get(sec.first))]
        if beats is None:
            return out
        total = (sec.last - sec.first + 1) * self.num
        for n in range(1, math.ceil(total / beats - 1e-9)):
            k = n * beats
            bar = self.bars.get(sec.first + int(k // self.num))
            if bar is not None and not bar.build:
                out.append((self.t(bar.number, 1 + k % self.num), bar))
        return out

    # -- the stage ---------------------------------------------------------
    def units(self, lanes: List[Lane]) -> List[List[Lane]]:
        """`lanes` as the eye sees them: a mirrored pair is one unit, left
        side first, any other lane a unit of its own; the middle of the stage
        first, out to its sides."""
        channels = {lane.channel for lane in lanes}
        units: List[List[Lane]] = []
        placed = set()
        for lane in lanes:
            if lane.channel in placed:
                continue
            pair = lane.mirror is not None and lane.mirror.channel in channels
            units.append(sorted([lane, lane.mirror], key=lambda x: x.x) if pair else [lane])
            placed.update(x.channel for x in units[-1])
        return sorted(units, key=lambda unit: sum(abs(lane.x - self.middle) for lane in unit)
                      / len(unit))

    def strikes(self, lanes: List[Lane], times: List[float],
                together: bool) -> List[Tuple[float, List[Lane]]]:
        """``(time, lanes it lands on)`` for hits at `times` going round
        `lanes`: a unit at a time, so both sides of a pair flash at once —
        except a run of hits within ANSWER_BARS of each other in a material
        played in turn, which goes round the lanes left to right, every side
        answered while the first still shows."""
        units, out = self.units(lanes), []
        runs: List[List[float]] = []
        for at in times:
            if runs and at - runs[-1][-1] <= ANSWER_BARS * self.bar + 1e-6:
                runs[-1].append(at)
            else:
                runs.append([at])
        turn = sweep = 0
        for run in runs:
            for at in run:
                if len(run) > 1 and not together:
                    out.append((at, [lanes[sweep % len(lanes)]]))
                    sweep += 1
                else:
                    out.append((at, units[turn % len(units)]))
                    turn += 1
        return out

    def second_phrase(self, sec) -> Optional[float]:
        """When the second phrase of `sec` starts: its own, or half-way on a
        4-bar line through a section of 8 bars or more."""
        if len(sec.phrases) > 1:
            return self.t(sec.phrases[1])
        length = sec.last - sec.first + 1
        return self.t(sec.first + 4 * max(1, round(length / 8))) if length >= 8 else None

    # -- the dramaturgy ---------------------------------------------------------
    def plan(self) -> List[Plan]:
        """Each section's tier and how far the song has come, its material's
        lead role, colours and choreography — chosen once, so a material is
        lit alike wherever it comes back — and the cast it lights. Quiet is
        the lead alone; quiet where the song opens, after its climax or with
        hardly any energy is the lead's middle glowing. A loud chorus before
        the climax is lit as the climax is. After the climax nothing goes
        above the band."""
        secs, climax = self.song.sections, self.song.climax
        tier_of = {analysis.QUIET: LEAD, analysis.MID: BAND, analysis.HIGH: FULL}
        by_material: Dict[str, tuple] = {}
        seen: Dict[str, int] = {}
        plans: List[Plan] = []
        first = secs[0].start if secs else 0.0
        top = secs[climax].start if climax is not None else (secs[-1].start if secs else 0.0)
        for k, sec in enumerate(secs):
            after = climax is not None and k > climax
            tier = tier_of.get(sec.level, LEAD)
            if k == climax or (sec.part == "CHORUS" and sec.level >= analysis.MID and not after):
                tier = PEAK
            elif sec.level == analysis.QUIET and (k == 0 or after
                                                  or sec.energy < self.style.glow_energy):
                tier = GLOW
            elif after:
                tier = min(tier, BAND)
            if sec.material not in by_material:
                taken = [colours for _lead, colours, _pattern in by_material.values()]
                before = by_material[secs[k - 1].material][2] if k else None
                by_material[sec.material] = (self.pick_lead(sec), self.pick_colours(sec, taken),
                                             self.pick_pattern(tier, before))
            lead, colours, pattern = by_material[sec.material]
            occurrence = seen.get(sec.material, 0)
            seen[sec.material] = occurrence + 1
            arc = 1.0 if after or top <= first else max(0.0, min(1.0, (sec.start - first)
                                                                 / (top - first)))
            plans.append(Plan(tier, lead, colours, pattern, occurrence, arc))
        self.reveal, self.reserve = self.reveal_order(plans)
        # a material keeps the choreography it first played at a tier, so it
        # does not turn into another as its cast grows
        shapes: Dict[tuple, str] = {}
        for k, (sec, plan) in enumerate(zip(secs, plans)):
            plan.cast = self.cast_of(k, sec, plan)
            key = (sec.material, plan.tier)
            if key not in shapes:
                shapes[key] = self.shape(plan.pattern, plan.tier, len(plan.cast))
            plan.pattern = shapes[key]
        return plans

    def pick_pattern(self, tier, before: Optional[str]) -> str:
        """A material's choreography: one that keeps the cast moving when it
        is first heard at most mid-loud, one that keeps it lit when loud —
        never the one of the material before it."""
        options = [p for p in (MOVING if tier <= BAND else FILLING) if p != before]
        return self.rng.choice(options)

    def reveal_order(self, plans) -> Tuple[List[str], set]:
        """``(roles, reserve)``: the roles in the order the song brings them
        in — as the sections before its climax lead, then the rest — and the
        unit held back for the climax, on a rig of RESERVE_MIN units or more:
        the outermost of the last role in that has two or more, so every
        role still plays and the climax opens the stage out to its width."""
        climax = self.song.climax
        last = len(plans) if climax is None else climax
        leads: List[str] = []
        for sec, plan in zip(self.song.sections[:last], plans[:last]):
            role = self.lead_of(sec, plan)
            if role and role != "blinder" and role not in leads:
                leads.append(role)
        roles = leads + [r for r in ("wash", "pixel", "mover") if self.role[r] and r not in leads]
        if climax is None or sum(len(self.units(self.role[r])) for r in roles) < RESERVE_MIN:
            return roles, set()
        for role in reversed(roles):
            units = self.units(self.role[role])
            if len(units) >= 2:
                return roles, {lane.channel for lane in units[-1]}
        return roles, set()

    def held(self, lane, k: Optional[int]) -> bool:
        """Whether `lane` is held back for the climax in section `k`."""
        climax = self.song.climax
        return lane.channel in self.reserve and (k is None or climax is None or k < climax)

    def cast_of(self, k, sec, plan) -> List[List[Lane]]:
        """The units `sec` lights, in the order they come in: its lead role's,
        then the others in the order the song brings their roles in, the
        blinders last — those with something to play, the reserve aside
        until the climax. A quiet section lights its lead role, a mid or loud
        one more of the rest the louder it is and the nearer the climax (a
        mid one chasing, three: a chase lights one at a time), the climax all
        of them."""
        lead = self.lead_of(sec, plan)
        order = []
        for role in dict.fromkeys(([lead] if lead else []) + self.reveal + ["blinder"]):
            order += [unit for unit in self.units(self.role[role])
                      if not self.held(unit[0], k)
                      and any(not look.dark for look in self.cycle(unit[0], sec, plan))]
        if plan.tier in (GLOW, LEAD):
            return [unit for unit in order if unit[0].role == lead]
        if plan.tier == PEAK:
            return order
        low, high = BAND_SHARE if plan.tier == BAND else FULL_SHARE
        count = math.floor(len(order) * (low + (high - low) * plan.arc) + 0.5)
        chases = plan.tier == BAND and plan.pattern == CHASE and self.step(BAND) is not None
        least = 3 if chases else 2
        return order[:max(min(least, len(order)), count)]

    def shape(self, pattern: str, tier: int, units: int) -> str:
        """The choreography a section of `tier` lighting `units` units plays
        of its material's: the climax, a glow and a section holding one look
        light the whole cast; a loud one too, alternating only a cast of four
        or more; a chase needs three units, alternating and stacking two."""
        whole = TOGETHER if pattern in (TOGETHER, STACK) else IN_TURN
        if tier in (GLOW, PEAK) or self.step(tier) is None:
            return whole
        if tier == FULL:
            return ALTERNATE if pattern in (ALTERNATE, CHASE) and units >= 4 else whole
        if pattern == CHASE and units < 3:
            pattern = ALTERNATE
        return whole if pattern in (ALTERNATE, STACK) and units < 2 else pattern

    def step(self, tier) -> Optional[int]:
        """Beats between two steps of a choreography at `tier`: STEPS_PER_LOOK
        to a look change, a beat at least; None where one look holds the
        section."""
        beats = self.pace(tier)
        return None if beats is None else max(1, int(round(beats / STEPS_PER_LOOK)))

    def lit(self, plan, unit: int, step: int) -> bool:
        """Whether the `unit`-th of the cast is lit at the `step`-th step."""
        if plan.pattern == ALTERNATE:
            return unit % 2 == step % 2
        if plan.pattern == CHASE:
            return unit == step % len(plan.cast)
        if plan.pattern == STACK:
            return step >= unit
        return True

    def section_index(self, bar: int) -> Optional[int]:
        return next((k for k, s in enumerate(self.song.sections) if s.first <= bar <= s.last),
                    None)

    def pick_lead(self, sec) -> Optional[str]:
        """The role a sparse section of this material lights: one its library
        tags looks for the part, else any but the blinders."""
        roles = [r for r in ("wash", "pixel", "mover") if self.role[r]]
        tagged = [r for r in roles + ["blinder"] if self.role[r]
                  and any(lane.looks.tagged(sec.part, related=False) for lane in self.role[r])]
        choices = tagged or roles or [r for r in ("blinder",) if self.role[r]]
        return self.rng.choice(choices) if choices else None

    def pick_colours(self, sec, taken) -> Tuple[tuple, tuple]:
        lead, partner = PART_COLOURS.get(sec.part, ("primary", "secondary"))
        pair = (self.pal[lead], self.pal[partner])
        return (pair[1], pair[0]) if pair in taken and pair[0] != pair[1] else pair

    def lead_of(self, sec, plan) -> Optional[str]:
        """The role leading `sec`: its material's, unless that has nothing to
        play here — then the first other role that has."""
        for role in [plan.lead] + [r for r in ("wash", "pixel", "mover", "blinder") if r != plan.lead]:
            if role and any(self.cycle(lane, sec, plan) for lane in self.role[role]):
                return role
        return None

    def position(self, lane, plan) -> Optional[int]:
        """Where `lane`'s unit stands in the section's cast; None: out of it."""
        return next((k for k, unit in enumerate(plan.cast) if lane in unit), None)

    def joins(self, lane, sec, plan) -> Optional[float]:
        """When `lane` lights in `sec`: from its start, from its second
        phrase — the band builds on its lead, a glow on the unit of the lead
        nearest the middle (one fixture, or a mirrored pair) — or (None) not
        at all, out of the cast. A cast taking turns starts together, and its
        choreography says who is lit when."""
        unit = self.position(lane, plan)
        if unit is None:
            return None
        if plan.pattern in (ALTERNATE, CHASE, STACK):
            return sec.start
        lead = self.lead_of(sec, plan)
        if plan.tier == GLOW:
            if unit == 0:
                return sec.start
            return self.second_phrase(sec) if self.x > 0 else None
        if (plan.tier == BAND and lead is not None and lane.role not in (lead, "blinder")
                and self.x > 0):
            return self.second_phrase(sec) or sec.start
        return sec.start

    def turns(self, sec, plan, unit: int) -> List[Tuple[float, bool]]:
        """``(time, lit)`` for the `unit`-th of the cast at every step of the
        section's choreography (none when it keeps the cast lit). A build bar
        belongs to the build."""
        if plan.pattern not in (ALTERNATE, CHASE, STACK):
            return []
        step = self.step(plan.tier)
        out = [(sec.start, self.lit(plan, unit, 0))]
        for k in range(step, (sec.last - sec.first + 1) * self.num, step):
            bar = self.bars.get(sec.first + k // self.num)
            if bar is not None and not bar.build:
                out.append((self.t(bar.number, 1 + k % self.num), self.lit(plan, unit, k // step)))
        return out

    def cycle(self, lane, sec, plan) -> List[Look]:
        """The looks `lane` steps through in this section's material, chosen
        once so the material looks the same wherever it comes back: the
        best-ranked, in a seeded order among those nearly as good — one order
        for a mirrored pair, whose two fixtures rank alike. The climax plays
        its material's looks at full."""
        tier = min(plan.tier, FULL)
        owner = min(lane.channel, lane.mirror.channel) if lane.mirror else lane.channel
        key = (owner, sec.material, tier)
        if key not in self.cycles:
            ranked = lane.looks.ranked(sec.part, tier, self.hues)
            top = ranked[0][0] if ranked else 0.0
            near = [look for score, look in ranked if score >= top - 0.75]
            rest = [look for score, look in ranked if score < top - 0.75]
            self.rng.shuffle(near)
            self.cycles[key] = (near + rest)[:1 if self.pace(tier) is None else self.style.cycle]
        return self.cycles[key]

    # -- the whole song -------------------------------------------------------
    def direct(self):
        for k, (sec, plan) in enumerate(zip(self.song.sections, self.plans)):
            for role in ("wash", "pixel", "mover", "blinder"):
                for lane in self.role[role]:
                    self.base(lane, sec, k)
            self.accents(sec, plan, k)
            if self.lands(k):
                self.land(k)
            elif self.enters(k) and not any(abs(at - sec.start) <= self.beat
                                            for at, _s in self.song.impacts):
                self.impact(sec.start, blinders=self.blinding(k))
        for b in self.song.bars:
            if b.build:
                self.build(b)
        for at, _strength in self.song.impacts:
            self.impact(at)
        if self.song.climax is not None:
            self.fills(self.song.climax)
        self.stops()
        self.fog()
        self.finale()
        for lane in self.lanes:
            lane.add(self.end, None, "END", FINAL)

    def put(self, lane, at, look: Look, colour=WHITE, prio=BASE, scale=1.0, partner=None,
            **kwargs) -> dict:
        frame = look.frame(lane.voice, colour, scale, partner)
        lane.add(at, frame, look.name, prio, **kwargs)
        if look.library:
            lane.used.add(look.name.strip())
        return frame

    # -- a section's own looks ---------------------------------------------------
    def base(self, lane, sec, k):
        """The section's looks on one lane, stepping on at every change of its
        grid from when the lane lights — dark until then, or throughout, out
        of the cast — and dark on the steps its choreography leaves it out
        of. A role's units play one look or looks a step apart, as the
        material's choreography says — the two lanes of a mirrored pair
        always together — and a section never opens on the look the last one
        closed with. A quiet section eases in where the song opens on it or a
        louder one ends."""
        plan = self.plans[k]
        join = self.joins(lane, sec, plan)
        beats = self.beats(k)
        looks = self.cycle(lane, sec, plan) if join is not None else []
        if beats is None or beats > PULSE_BEATS:
            looks = [look for look in looks if not look.dark]
        if not looks:
            lane.add(sec.start, None, "Dark")
            lane.last = None
            return
        if join > sec.start + 1e-6:
            lane.add(sec.start, None, "Dark")
            lane.last = None
        times = [at for at, _bar in self.grid(sec, beats) if at >= join - 1e-6]
        if not times or times[0] > join + 1e-6:
            times.insert(0, join)
        # every moment the look changes or the choreography turns the lane
        # on or off: (look index, lit), None where that one holds
        changes: Dict[float, list] = {}
        for g, at in enumerate(times):
            changes.setdefault(round(at, 6), [None, None])[0] = g
        for at, on in self.turns(sec, plan, self.position(lane, plan)):
            changes.setdefault(round(at, 6), [None, None])[1] = on
        c1, c2 = plan.colours
        shift = 0 if plan.unison else self.unit[lane.channel]
        scale = self.style.glow_scale if plan.tier == GLOW else 1.0
        n = len(looks)
        opening = (c1, c2) if shift % 2 == 0 else (c2, c1)
        start = next((s for s in range(shift, shift + n)
                      if looks[s % n].frame(lane.voice, opening[0], scale, opening[1]) != lane.last),
                     shift)
        ease = plan.tier <= LEAD and (k == 0 or self.plans[k - 1].tier > plan.tier)
        fade = min(3.0, 2 * self.bar) if k == 0 else min(2.0, self.bar)
        g, on, shown = 0, True, None
        for at in sorted(changes):
            g = g if changes[at][0] is None else changes[at][0]
            on = on if changes[at][1] is None else changes[at][1]
            if (g, on) == shown:
                continue
            first = shown is None
            shown = (g, on)
            if not on:
                lane.add(at, None, "Dark")
                lane.last = None
                continue
            colour, partner = (c1, c2) if (g + shift) % 2 == 0 else (c2, c1)
            lane.last = self.put(lane, at, looks[(start + g) % n], colour, scale=scale,
                                 partner=partner, fade_in=fade if ease and first else 0.0)

    # -- hits -----------------------------------------------------------------
    def enters(self, k) -> bool:
        """Whether section `k` is an entrance: the band's first, the song's
        opening when it opens with the band, or the music stepping up to
        loud."""
        tier = self.plans[k].tier
        before = [plan.tier for plan in self.plans[:k]]
        if tier >= BAND and all(t < BAND for t in before):
            return True
        return tier >= FULL and bool(before) and before[-1] < FULL

    def lands(self, k) -> bool:
        """Whether section `k` lands as the climax does: the climax itself,
        and a loud chorus coming in."""
        return k == self.song.climax or (self.plans[k].tier == PEAK
                                         and (k == 0 or self.plans[k - 1].tier < PEAK))

    def last_stretch(self) -> Optional[int]:
        """Where the song's last stretch starts: its last loud chorus, with
        the choruses straight before it, or the climax when no chorus comes
        before it; None in a song with no climax."""
        climax = self.song.climax
        if climax is None:
            return None
        secs = self.song.sections

        def chorus(k):
            return secs[k].part == "CHORUS" and self.plans[k].tier == PEAK

        k = next((k for k in range(climax, -1, -1) if chorus(k)), climax)
        while k > 0 and chorus(k - 1):
            k -= 1
        return k

    def blinding(self, k: Optional[int]) -> bool:
        """Whether the blinders may fire on more than the song's own hits in
        section `k`: from its last stretch — the last chorus, the climax, the
        outro — on, and even there in short bursts."""
        return k is not None and self.stretch is not None and k >= self.stretch

    def flashed(self, k: Optional[int], blinders: bool = True) -> List[Lane]:
        """The lanes a flash in section `k` lights: before the climax its
        cast, so the rig grows with the song, from the climax on every
        light — and the blinders, unless told not to."""
        lights = [lane for lane in self.lanes if lane.role != "fog"
                  and (blinders or lane.role != "blinder")]
        climax = self.song.climax
        if climax is None or (k is not None and k >= climax):
            return lights
        if k is None:                                # outside every section
            return [lane for lane in lights if not self.held(lane, k)]
        cast = {lane.channel for unit in self.plans[k].cast for lane in unit}
        return [lane for lane in lights if lane.role == "blinder" or lane.channel in cast]

    def accents(self, sec, plan, k):
        """The accents of a quiet passage flash round the washes — the pixel
        bars, or the movers, when there are none — and in a break round every
        lit role; never the blinders, kept for the song's hits, nor the unit
        held back for the climax."""
        hits = [at for at in self.accent_times if sec.start <= at < sec.end]
        if not hits:
            return
        groups = [self.role["wash"] or self.role["pixel"] or self.role["mover"]]
        if sec.part == "BREAK":
            groups += [self.role[r] for r in ("wash", "pixel", "mover")
                       if self.role[r] is not groups[0]]
        for lanes in groups:
            lanes = [lane for lane in lanes if not self.held(lane, k)]
            if not lanes:
                continue
            for at, hit in self.strikes(lanes, hits, plan.unison):
                for lane in hit:
                    self.put(lane, at, lane.looks.hit(), self.pal["contrast"], ACCENT,
                             hold=0.45, fade_out=0.35)

    def build(self, bar):
        """Into the next downbeat, on the lights of the section it leads to:
        a simple show holds one strobe (or swells) for the bar; a busier one
        climbs on more of its beats, then strobes. The blinders wait for the
        hit it lands on — in the song's last stretch they strobe its last
        beat."""
        steps = max(1, math.floor(self.num * self.x + 0.5))
        times = [self.t(bar.number, 1 + j * self.num / steps) for j in range(steps)]
        span = self.bar / steps
        into = self.section_index(bar.number + 1)
        k = self.section_index(bar.number) if into is None else into
        for lane in self.flashed(k):
            hit, strobes = lane.looks.hit(), lane.looks.strobes()
            if lane.role == "blinder":
                if self.blinding(k) and strobes:
                    at = self.t(bar.number, self.num)
                    self.put(lane, at, strobes[-1], prio=BUILD, hold=self.t(bar.number + 1) - at)
                continue
            if steps == 1:
                if strobes:
                    self.put(lane, times[0], strobes[len(strobes) // 2], prio=BUILD, hold=span)
                else:
                    self.put(lane, times[0], hit, prio=BUILD, scale=0.6, hold=span,
                             fade_in=span)
                continue
            for j, at in enumerate(times):
                f = (j + 1) / steps
                if strobes and f > 0.5:
                    look = strobes[min(len(strobes) - 1, int((2 * f - 1) * len(strobes) - 1e-9))]
                    self.put(lane, at, look, prio=BUILD, hold=span)
                else:
                    # short of the hit, so the downbeat still lands
                    self.put(lane, at, hit, prio=BUILD, scale=0.35 + 0.5 * f, hold=span)

    def impact(self, at, blinders: bool = True):
        """The lights of the section it lands in on its hit (flashed) — the
        blinders on the song's own hits, not on one the show adds unless
        told. In a loud section the music carries on at full, so the look
        snaps back; elsewhere the flash dies away first."""
        hold = 2 * self.beat
        k = self.section_index(self.song.bar_at(at + self.beat).number)
        sec = None if k is None else self.song.sections[k]
        decay = sec is None or sec.level != analysis.HIGH
        for lane in self.flashed(k, blinders):
            if lane.role == "blinder":
                self.put(lane, at, lane.looks.hit(), prio=IMPACT, hold=BLINDER_HOLD,
                         fade_out=BLINDER_FADE)
            else:
                self.put(lane, at, lane.looks.hit(), prio=IMPACT, hold=hold,
                         fade_out=0.85 if decay else 0.0)

    def land(self, k):
        """A loud chorus coming in, and the climax, lands on a hit of its
        lights, whether or not the song's hits put one there — the blinders
        only after a build or in the song's last stretch; when a build or an
        impact leads into it, the half beat before is a blackout — the breath
        before the drop."""
        sec = self.song.sections[k]
        landing = next((at for at, _s in self.song.impacts
                        if abs(at - sec.start) <= self.beat / 2), None)
        before = self.bars.get(sec.first - 1)
        built = before is not None and before.build
        if landing is None:
            self.impact(sec.start, blinders=built or self.blinding(k))
        if landing is None and not built:
            return
        hit = sec.start if landing is None else landing
        for lane in self.lanes:
            if lane.role != "fog":
                lane.add(hit - self.beat / 2, None, "Breath", IMPACT,
                         hold=self.beat / 2 - MIN_GAP)

    def fills(self, k):
        """What lifts the climax above the choruses lit like it: it strobes
        into its phrase lines — the second half of the bar before every
        FILL_BARS-th line from its start and before its end, on every light,
        its fastest strobe or a white hit where it has none; the blinders
        only its last beat. A fill is a hit; a build bar climbs there
        already."""
        sec = self.song.sections[k]
        every = FILL_BARS[1] if self.x >= 0.5 else FILL_BARS[0]
        for line in sorted(set(range(sec.first + every, sec.last + 1, every)) | {sec.last + 1}):
            bar = self.bars.get(line - 1)
            if bar is None or bar.build:
                continue
            for lane in self.flashed(k):
                at = self.t(line - 1, self.num if lane.role == "blinder" else 1 + self.num / 2)
                strobes = lane.looks.strobes()
                self.put(lane, at, strobes[-1] if strobes else lane.looks.hit(), prio=IMPACT,
                         hold=self.t(line) - at)

    def stops(self):
        """Where the band stops, the rig goes dark; the looks come back with
        the band."""
        for start, end in self.song.stops:
            if end - start < 2 * MIN_GAP:
                continue
            for lane in self.lanes:
                if lane.role != "fog":
                    lane.add(start, None, "Stop", STOP, hold=end - start - MIN_GAP)

    # -- fog, the ending ---------------------------------------------------------
    def fog(self):
        """Haze for a quiet opening, a puff into every build, every break,
        every loud chorus and the climax."""
        windows = []
        secs = self.song.sections
        if secs and secs[0].level == analysis.QUIET:
            windows.append((secs[0].start, secs[0].start + min(2.5 * self.bar, secs[0].end - secs[0].start)))
        for b in self.song.bars:
            if b.build:
                windows.append((b.start, b.start + self.bar + self.beat))
        for sec in secs:
            if sec.part == "BREAK":
                windows.append((sec.start, sec.start + 1.5 * self.bar))
        for k, sec in enumerate(secs):
            if self.lands(k):
                windows.append((sec.start - self.bar, sec.start + self.bar))
        merged = []
        for on, off in sorted(windows):
            if merged and on - merged[-1][1] < 3.0:
                merged[-1][1] = max(merged[-1][1], off)
            else:
                merged.append([on, off])
        for lane in self.role["fog"]:
            for on, off in merged:
                lane.add(on, lane.voice.frame(0, fog=1.0), "Fog")
                lane.add(min(off, self.end), None, "Fog off")

    def finale(self):
        """The song's last moment, the way it ends: the whole rig on its last
        hit until the sound stops dead (cut), or left to ring out to black
        (ring) — the blinders no longer than on any hit. A song that fades
        burns down instead."""
        song = self.song
        if song.ending not in ("cut", "ring") or song.final_hit is None:
            self.tail()
            return
        at = song.final_hit
        if self.end - at < MIN_GAP:
            return
        ring = max(0.0, (song.cut or self.end) - at) if song.ending == "ring" else 0.0
        for lane in self.lanes:
            if lane.role == "blinder":
                self.put(lane, at, lane.looks.hit(), prio=FINAL,
                         hold=min(self.end - at, BLINDER_HOLD), fade_out=min(ring, BLINDER_FADE))
            elif lane.role != "fog":
                self.put(lane, at, lane.looks.hit(), prio=FINAL, hold=self.end - at,
                         fade_out=ring)

    def tail(self):
        """The last bars dying away: whatever each lane shows burns down to
        black."""
        last = self.song.sections[-1] if self.song.sections else None
        if last is None:
            return
        # by level, not QUIET_MAX: the analysis may have been tuned to another
        quiet = [b for b in self.bars_of(last) if b.level <= analysis.QUIET]
        start = None
        for b in reversed(self.bars_of(last)):
            if b in quiet:
                start = b.start
            else:
                break
        if start is None or self.end - start < self.beat:
            return
        for lane in self.lanes:
            if lane.role == "fog":
                continue
            showing = [c for c in lane.cues if c.prio == BASE and c.t <= start + 1e-6]
            if not showing or showing[-1].values is None:
                continue
            look = showing[-1]
            if lane.voice.judge("", look.values).dark:
                continue
            lane.add(start, look.values, look.name, IMPACT, hold=self.end - start,
                     fade_out=self.end - start)
