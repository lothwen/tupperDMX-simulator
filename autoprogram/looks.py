# ABOUTME: What a fixture can show, read from its capabilities: Voice (its
# ABOUTME: channels by function, its role, frames built for it) and Looks (its
# ABOUTME: library's scenes judged by what their channels do). Qt-free.
"""A fixture's looks, never from its name: every judgement here reads the
capabilities its channels declare, so any OFL fixture works with no sidecar,
and one with a sidecar brings its library's scenes, tagged by song part.

A TIER is how much light a section asks for — from one fixture glowing to the
climax — and the looks are ranked against it.
"""
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

from autoprogram.colours import WHITE, hue, hue_gap
from core import fixture_library as fl
from core import note_layout, ofl
from core.fixture_state import capability_at, solve_color

# How much of the rig a section lights: one fixture glowing, the lead role,
# everything but the blinders, everything, the climax.
GLOW, LEAD, BAND, FULL, PEAK = range(5)
TIER_NAMES = ("glow", "lead", "band", "full", "peak")
# The light a section's look gives when the library has nothing for it.
LEVEL = {GLOW: 0.3, LEAD: 0.43, BAND: 0.67, FULL: 1.0, PEAK: 1.0}
DARK = 0.04            # a look giving less light than this is dark
HIT_MIN = 0.6          # a library look dimmer than this is no hit
HUE_REACH = 50.0       # degrees of hue within which a colour sits in the palette
MIN_FIT = 0.3          # a look fitting the palette worse clashes with it

CHROMATIC = {"Red", "Green", "Blue", "Cyan", "Magenta", "Yellow"}
# What each OFL colour adds to a cell's RGB, for judging a scene's hues.
EMITTED = {"Red": (1, 0, 0), "Green": (0, 1, 0), "Blue": (0, 0, 1), "Cyan": (0, 1, 1),
           "Magenta": (1, 0, 1), "Yellow": (1, 1, 0), "Amber": (1, 0.5, 0),
           "White": (1, 1, 1), "Cold White": (0.9, 0.95, 1), "Warm White": (1, 0.8, 0.6),
           "Lime": (0.6, 1, 0), "Indigo": (0.3, 0, 1), "UV": (0.4, 0, 1)}
# Where a lane looks for scenes when its library tags none for a part.
PART_FALLBACK = {"INTRO": ["INTRO", "BREAK", "BRIDGE"], "VERSE": ["VERSE", "BRIDGE", "INST"],
                 "CHORUS": ["CHORUS", "INST", "SOLO"], "BREAK": ["BREAK", "BRIDGE", "INTRO"],
                 "BRIDGE": ["BRIDGE", "BREAK", "VERSE"], "INST": ["INST", "SOLO", "CHORUS"],
                 "OUTRO": ["OUTRO", "END", "INTRO"]}


# ---------------------------------------------------------------------------
# What a fixture's channels can do, read from its capabilities
# ---------------------------------------------------------------------------
def _range8(cap) -> Tuple[int, int]:
    low, high = cap.dmx_range
    return (low >> 8, high >> 8) if high > 255 else (low, high)


def _at(cap, frac: float) -> int:
    low, high = _range8(cap)
    return int(round(low + (high - low) * max(0.0, min(1.0, frac))))


def _mid(cap) -> int:
    return _at(cap, 0.5)


def _angle(cap, key: str) -> Optional[float]:
    return ofl.parse_entity(cap.get(key), "angle")


def _fraction(cap, value: int) -> float:
    """Where `value` sits inside `cap`'s range, 0-1."""
    low, high = _range8(cap)
    return (value - low) / (high - low) if high > low else 0.5


class Voice:
    """A fixture's channels by function, and the frame for any look on it."""

    def __init__(self, fdef):
        self.fdef = fdef
        self.rest: Dict[str, int] = {}             # the neutral frame
        self.index = {spec.field: i for i, spec in enumerate(fdef.channels)}
        self.level: Optional[str] = None           # master dimmer
        self.colours: List[Tuple[str, str]] = []   # (field, OFL colour name)
        self.shutter: Optional[Tuple[str, list]] = None
        self.pan = self.tilt = None                # (field, degrees of travel)
        self.gobos: List[int] = []
        self.gobo_field: Optional[str] = None
        self.spin = None                           # (field, continuous rotation cap)
        self.prism = None                          # (field, value with it in)
        self.zoom = None                           # (field, narrow, wide)
        self.fog = None                            # (field, cap)
        self.effects = None                        # (field, [Effect caps])
        self.effect_speed = None                   # (field, cap)
        level_slot = fl.intensity_slot(fdef)

        for slot, spec in enumerate(fdef.channels):
            f, caps = spec.field, spec.capabilities
            dom = fl.dominant_capability(caps)
            kind = dom.type if dom else "NoFunction"
            default = spec.default_value >> 8 if spec.default_value > 255 else spec.default_value
            nothing = next((c for c in caps if c.type == "NoFunction"), None)
            rest = _range8(nothing)[0] if nothing else default
            if spec.fine_of >= 0:
                rest = 0
            elif slot == level_slot:
                self.level, rest = f, 0
            elif kind == "ColorIntensity":
                self.colours.append((f, str(dom.get("color", ""))))
                rest = 0
            elif kind in ("Pan", "Tilt"):
                start, end = _angle(dom, "angleStart"), _angle(dom, "angleEnd")
                travel = abs(end - start) if start is not None and end is not None else (
                    540.0 if kind == "Pan" else 270.0)
                setattr(self, kind.lower(), (f, travel))
                rest = 128
            elif kind == "ShutterStrobe":
                self.shutter = (f, [c for c in caps if c.type == "ShutterStrobe"])
                rest = self._shutter("Open") or rest
            elif kind == "WheelSlot":
                wheel = fdef.wheels.get(spec.label) or fdef.wheels.get(str(dom.get("wheel", "")))
                slots = [c for c in caps if c.type == "WheelSlot"]
                kinds = {}
                for c in slots:
                    n = int(c.get("slotNumber", 0) or 0)
                    if wheel and 1 <= n <= len(wheel.slots):
                        kinds[_mid(c)] = wheel.slots[n - 1].type
                opens = [v for v, k in kinds.items() if k == "Open"]
                rest = min(opens) if opens else (_mid(slots[0]) if slots else rest)
                gobos = sorted(v for v, k in kinds.items() if k == "Gobo")
                if gobos and not self.gobos:
                    self.gobos, self.gobo_field = gobos, f
            elif kind == "WheelSlotRotation" or kind == "WheelRotation":
                turning = next((c for c in caps if c.type == kind and c.get("speedStart")), None)
                if turning is not None and self.spin is None:
                    self.spin = (f, turning)
                rest = _range8(caps[0])[0]
            elif kind == "Prism":
                self.prism = (f, _mid(dom))
            elif kind == "Zoom":
                low, high = _angle(dom, "angleStart"), _angle(dom, "angleEnd")
                lo, hi = _range8(dom)
                narrow_first = low is None or high is None or low <= high
                self.zoom = (f, lo if narrow_first else hi, hi if narrow_first else lo)
            elif kind == "FogOutput":
                self.fog = (f, next(c for c in caps if c.type == "FogOutput"))
                rest = rest if nothing else 0
            elif kind == "Effect":
                effects = [c for c in caps if c.type == "Effect"]
                if len(effects) >= 3 and self.effects is None:
                    self.effects = (f, effects)
                    rest = _range8(effects[0])[0]
            elif kind == "EffectSpeed" and self.effect_speed is None:
                self.effect_speed = (f, dom)
            self.rest[f] = max(0, min(255, rest))

    # -- what the fixture is -------------------------------------------
    def role(self) -> Optional[str]:
        names = {name for _f, name in self.colours}
        if self.pan or self.tilt:
            return "mover"
        if self.fog and not self.colours:
            return "fog"
        if self.effects and names & CHROMATIC:
            return "pixel"
        if names & CHROMATIC:
            return "wash"
        if self.level or self.colours:
            return "blinder"
        return None

    def can_strobe(self) -> bool:
        return self.shutter is not None and self._shutter("Strobe") is not None

    def strobe_effect(self) -> Optional[int]:
        """An animation that strobes, for a bar with no shutter."""
        if not self.effects:
            return None
        cap = next((c for c in self.effects[1] if "strob" in str(c.get("effectName", "")).lower()),
                   None)
        return _mid(cap) if cap else None

    def _shutter(self, effect: str, frac: Optional[float] = None) -> Optional[int]:
        if not self.shutter:
            return None
        cap = next((c for c in self.shutter[1] if c.get("shutterEffect") == effect
                    and not c.get("randomTiming")), None)
        if cap is None:
            return None
        return _mid(cap) if frac is None else _at(cap, frac)

    # -- reading a scene ---------------------------------------------------
    def judge(self, name: str, scene: dict, library: bool = True) -> "Look":
        """`scene` as a Look: how much light it gives, whether white, and
        whether it strobes or animates — read from the capabilities its
        values select, never from its name."""
        values = dict(self.rest)
        values.update({f: int(x) for f, x in scene.items() if f in values})
        slots = [values[spec.field] for spec in self.fdef.channels]
        level = values[self.level] / 255 if self.level else 1.0
        cells = self._cells()
        if cells:
            level *= sum(max(values[f] for f in cell) for cell in cells) / (255 * len(cells))
        strobe, animated = None, False
        if self.shutter:
            f = self.shutter[0]
            cap = capability_at(self.fdef, self.index[f], slots)
            effect = cap.get("shutterEffect") if cap is not None and cap.type == "ShutterStrobe" else None
            if effect == "Closed":
                level = 0.0
            elif effect not in (None, "Open"):
                strobe = _fraction(cap, values[f])
        if self.effects:
            f, effects = self.effects
            cap = capability_at(self.fdef, self.index[f], slots)
            if cap is not None and cap.type == "Effect":
                if "strob" in str(cap.get("effectName", "")).lower():
                    strobe = values[self.effect_speed[0]] / 255 if self.effect_speed else 0.5
                elif cap.dmx_range != effects[0].dmx_range:
                    animated = True
        rgb = [f for f, n in self.colours if n in ("Red", "Green", "Blue")]
        palette = ({n for _f, n in self.colours} >= {"Red", "Green", "Blue"}
                   and all(values[f] >= 254 for f in rgb))
        r, g, b = note_layout.scene_rgb(self.fdef, values)
        top = max(r, g, b)
        white = not palette and top > 0 and (top - min(r, g, b)) / top < 0.3
        return Look(name, values, level, white, palette, strobe, animated, library)

    def _cells(self) -> List[List[str]]:
        """The colour fields of each emitter: the n-th of every colour name."""
        by_name: Dict[str, List[str]] = {}
        for f, name in self.colours:
            by_name.setdefault(name, []).append(f)
        count = max((len(fs) for fs in by_name.values()), default=0)
        return [[fs[k] for fs in by_name.values() if k < len(fs)] for k in range(count)]

    def cell_rgb(self, values: Dict[str, int]) -> List[Tuple[float, float, float]]:
        """The colour each emitter of a frame mixes, 0-255 a component."""
        names = dict(self.colours)
        out = []
        for cell in self._cells():
            rgb = [0.0, 0.0, 0.0]
            for f in cell:
                for k, share in enumerate(EMITTED.get(names[f], (0, 0, 0))):
                    rgb[k] += share * values.get(f, 0)
            out.append(tuple(min(255.0, x) for x in rgb))
        return out

    # -- building a frame --------------------------------------------------
    def frame(self, level: int = 254, colour=WHITE, *, cool: Optional[int] = None,
              warm: Optional[int] = None, strobe: Optional[float] = None,
              aim: Optional[Tuple[float, float]] = None, gobo: Optional[int] = None,
              spin: float = 0.0, prism: bool = False, zoom: Optional[str] = None,
              fog: float = 0.0, effect: Optional[int] = None,
              effect_speed: Optional[float] = None) -> dict:
        v = dict(self.rest)
        if self.level:
            v[self.level] = level
        self._colour(v, colour, level, cool, warm)
        if self.shutter and strobe is not None and self.can_strobe():
            v[self.shutter[0]] = self._shutter("Strobe", strobe)
        if aim is not None:
            pan_deg, tilt_deg = aim
            if self.pan:
                v[self.pan[0]] = self._degrees(self.pan[1], pan_deg)
            if self.tilt:
                v[self.tilt[0]] = self._degrees(self.tilt[1], tilt_deg)
        if gobo is not None and self.gobos:
            v[self.gobo_field] = self.gobos[gobo % len(self.gobos)]
        if spin and self.spin:
            v[self.spin[0]] = _at(self.spin[1], spin)
        if prism and self.prism:
            v[self.prism[0]] = self.prism[1]
        if zoom and self.zoom:
            v[self.zoom[0]] = self.zoom[1] if zoom == "narrow" else self.zoom[2]
        if self.fog:
            v[self.fog[0]] = _at(self.fog[1], fog) if fog > 0 else self.rest[self.fog[0]]
        if effect is not None and self.effects:
            v[self.effects[0]] = effect
        if effect_speed is not None and self.effect_speed:
            v[self.effect_speed[0]] = _at(self.effect_speed[1], effect_speed)
        return {k: max(0, min(254, int(x) // 2 * 2)) for k, x in v.items()}

    @staticmethod
    def _degrees(travel: float, offset: float) -> int:
        """DMX for `offset` degrees from the middle of `travel`."""
        return int(round(127.5 + offset / travel * 255))

    def _colour(self, v, colour, level, cool, warm):
        if not self.colours:
            return
        r, g, b, w = colour
        names = {n for _f, n in self.colours}
        rgb = {"Red", "Green", "Blue"} <= names
        has_white = bool(names & {"White", "Cold White", "Warm White"})
        scale = 1.0 if self.level else level / 254.0     # no master: level lives in the colour
        fitted = {}
        if not rgb and names & CHROMATIC:
            by_slot = solve_color(self.fdef, (r, g, b))
            fitted = {self.fdef.channels[i].field: x for i, x in by_slot.items()}
            for f, name in self.colours:                 # every cell of a matrix
                twin = next((x for k, x in fitted.items()
                             if dict(self.colours).get(k) == name), None)
                if twin is not None:
                    fitted.setdefault(f, twin)
        for f, name in self.colours:
            if f in fitted:
                x = fitted[f]
            elif name == "Red":
                x = r + (0 if has_white else w)
            elif name == "Green":
                x = g + (0 if has_white else w)
            elif name == "Blue":
                x = b + (0 if has_white else w)
            elif name in ("White", "Cold White"):
                x = w if rgb else (cool if cool is not None else max(w, (r + g + b) // 3))
            elif name == "Warm White":
                x = w if rgb else (warm if warm is not None else max(w, (r + g + b) // 3))
            elif name == "Amber":
                x = max(0, min(r, 2 * g) - b)
            else:
                x = 0
            v[f] = min(254, int(x * scale))


# ---------------------------------------------------------------------------
# Looks: the library's scenes, and frames for what it has none of
# ---------------------------------------------------------------------------
@dataclass
class Look:
    """One state of a fixture: a scene of its library, or a frame built for it."""
    name: str
    values: Dict[str, int]                 # the whole frame, field -> DMX
    level: float                           # how much light it gives, 0-1
    white: bool = False
    palette: bool = False                  # takes the show's colour (R, G and B at full)
    strobe: Optional[float] = None         # how fast it strobes, 0-1; None: steady
    animated: bool = False                 # runs one of the fixture's own animations
    library: bool = False
    split: bool = False                    # its emitters take the palette's two colours in turn

    @property
    def dark(self) -> bool:
        return self.level < DARK

    def frame(self, voice: Voice, colour=WHITE, scale: float = 1.0, partner=None) -> dict:
        """The DMX to write: `colour` put in when the look takes the
        palette's (`partner` on every other emitter of a split look), the
        light scaled by `scale`."""
        v = dict(self.values)
        if self.palette:
            # no master dimmer: the level lives in the colour
            level = 254 if voice.level else int(254 * self.level * scale)
            voice._colour(v, colour, level, None, None)
            if self.split and partner is not None:
                other = dict(v)
                voice._colour(other, partner, level, None, None)
                for cell in voice._cells()[1::2]:
                    v.update({f: other[f] for f in cell})
        elif not voice.level:
            for f, _name in voice.colours:
                v[f] = v[f] * scale
        if voice.level:
            v[voice.level] = v[voice.level] * scale
        return {k: max(0, min(254, int(x) // 2 * 2)) for k, x in v.items()}


class Looks:
    """What a lane can show: the scenes its fixture's library defines, judged
    by what their channels do, and frames built from its capabilities for
    whatever the library has nothing for."""

    def __init__(self, voice: Voice, role: str, hanging: bool):
        self.voice, self.role, self.hanging = voice, role, hanging
        fdef = voice.fdef
        self.scenes: Dict[Tuple[str, int], Look] = {}
        for bank, scenes in fdef.banks.items():
            for i, scene in enumerate(scenes):
                name = str(scene.get("name") or "")
                if name.strip():                 # an unnamed slot is a placeholder
                    self.scenes[(bank, i)] = voice.judge(name, scene)
        self.refs = note_layout.song_part_refs(fdef)
        self.steady = [look for look in self.scenes.values()
                       if not look.dark and look.strobe is None]

    # -- from the library ----------------------------------------------------
    def tagged(self, part: str, related: bool = True) -> List[Look]:
        """The steps of what the library tags for `part` — its scenes, then
        its chases step by step — strobes left out. With nothing lit tagged
        for it, those of the first related part that has some."""
        for candidate in (PART_FALLBACK.get(part, [part]) if related else [part]):
            steps: List[Look] = []
            for ref in self.refs.get(candidate, []):
                if ref.is_chase:
                    chase = self.voice.fdef.chases[ref.index]
                    keys = list(zip(chase.get("banks", []), chase.get("scenes", [])))
                else:
                    keys = [(ref.bank, ref.index)]
                steps += [self.scenes[k] for k in keys
                          if k in self.scenes and self.scenes[k].strobe is None]
            if any(not look.dark for look in steps):
                return steps
        return []

    # -- the library, else the fixture's capabilities --------------------------
    def hit(self) -> Look:
        """The brightest steady look, white rather than coloured."""
        plain = [look for look in self.steady if not look.animated] or self.steady
        best = max(plain, default=None,
                   key=lambda look: look.level + (0.5 if look.white else 0.4 if look.palette else 0))
        if best is not None and best.level >= HIT_MIN:
            return best
        v = self.voice
        if self.role == "mover":
            return self.made("Hit", v.frame(254, aim=(0, 72) if self.hanging else (0, 0),
                                            zoom="wide", prism=True))
        return self.made("Hit", v.frame(254))

    def near(self, level: float) -> Look:
        """A plain steady look giving about `level` of light."""
        plain = [look for look in self.steady if not look.animated]
        if plain:
            return min(plain, key=lambda look: abs(look.level - level))
        return self.made("Glow", self.voice.frame(int(254 * level), cool=0, warm=254))

    def strobes(self) -> List[Look]:
        """Strobing looks, slowest first; white ones when there are any."""
        lit = [look for look in self.scenes.values() if look.strobe is not None and not look.dark]
        if lit:
            pale = [look for look in lit if look.white or look.palette]
            return sorted(pale or lit, key=lambda look: look.strobe)
        v = self.voice
        if v.can_strobe():
            return [self.made("Strobe", v.frame(254, strobe=f)) for f in (0.2, 0.5, 0.85)]
        effect = v.strobe_effect()
        if effect is not None:
            return [self.made("Strobe", v.frame(254, effect=effect, effect_speed=f))
                    for f in (0.4, 0.7, 1.0)]
        return []

    # -- ranking the library for a section -------------------------------------
    def fit(self, look: Look, hues: List[float]) -> float:
        """How well `look`'s colours sit in a palette of `hues`, up to 1: a
        look that takes the palette's colours fits, white goes with anything,
        and each coloured emitter counts by how near its hue is to one of
        them — one far from all of them counts against it, so a rainbow
        does not pass for the palette on the strength of its one red cell."""
        if look.palette:
            return 1.0
        scores = []
        for rgb in self.voice.cell_rgb(look.values):
            if max(rgb) < 13:
                continue
            own = hue(rgb)
            gap = min((hue_gap(own, h) for h in hues), default=180) if own is not None else 0
            scores.append(0.6 if own is None else max(-1.0, 1 - gap / HUE_REACH))
        return sum(scores) / len(scores) if scores else 0.0

    def ranked(self, part: str, tier: int, hues: List[float]) -> List[Tuple[float, Look]]:
        """Every steady look the lane could play through a section of `part`
        at `tier`, best first: the library's for the part, then for a related
        one, scored by how well their colours fit the palette and how close
        their light is to the tier's — colour weighing the most, so a scene
        the library tags but in other colours loses to a built one in the
        palette's, and one that clashes with the palette (fitting it under
        MIN_FIT) left out while anything fits. A blinder plays only what is
        tagged for the part itself."""
        explicit = self.tagged(part, related=False)
        if self.role == "blinder":
            pool = [(2.0, look) for look in explicit if not look.dark]
        else:
            related = [] if explicit else self.tagged(part)
            pool = ([(2.0, look) for look in explicit] + [(1.0, look) for look in related]
                    + [(0.0, look) for look in self.steady])
            pool += [(-0.5, look) for look in self.made_pool(part.title(), tier, 1)]
        best: Dict[int, Tuple[float, float, Look]] = {}
        for tag, look in pool:
            if look.dark or look.strobe is not None:
                continue
            fit = self.fit(look, hues)
            score = (tag + (0.5 if look.library else 0.0) + 3.0 * fit
                     + 1.0 - abs(look.level - LEVEL[tier])
                     + (0.3 if look.animated and tier >= BAND else 0.0))
            if id(look) not in best or score > best[id(look)][0]:
                best[id(look)] = (score, fit, look)
        ranked = sorted(best.values(), key=lambda x: -x[0])
        fitting = [x for x in ranked if x[1] >= MIN_FIT]
        return [(score, look) for score, _fit, look in fitting or ranked]

    # -- built from the capabilities -----------------------------------------
    def made_pool(self, name: str, tier: int, way: int) -> List[Look]:
        """Looks for a section in the palette's colours, at its tier's level:
        a fixture of several emitters also split between the two colours, a
        mover swinging either side of straight down."""
        v, lv = self.voice, int(254 * LEVEL[tier])
        if self.role != "mover":
            looks = [self.made(name, v.frame(lv), palette=True)]
            if len(v._cells()) > 1:
                looks.append(replace(self.made(name + " split", v.frame(lv), palette=True),
                                     split=True))
            return looks
        loud = tier >= FULL
        return [self.made(name, v.frame(lv, aim=(-90, side * way * 30), zoom="narrow",
                                        gobo=0 if loud else None, prism=loud), palette=True)
                for side in (1, -1)]

    def made(self, name: str, values: dict, palette: bool = False) -> Look:
        return replace(self.voice.judge(name, values, library=False), palette=palette)
