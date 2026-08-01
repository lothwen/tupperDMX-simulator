# ABOUTME: Turns a fixture's DMX slice into what it actually emits — colour,
# ABOUTME: intensity, strobe, effect, beam — driven by its OFL capabilities.
"""The photometric model, both directions.

`resolve_state` is DMX in, light out: the half of the stage engine that doesn't
draw anything. A renderer asks "what is this fixture doing right now?" and gets
a `FixtureState` back; how to paint that is the renderer's problem. Keeping the
two apart means the same resolution logic serves a pixel bar, a blinder and a
par, and it stays Qt-free and directly testable.

`solve_color` is the inverse the controller needs: light in, DMX out — "make
this fixture emit this colour, with whatever emitters it happens to have".

Everything is derived from the fixture's OFL capabilities rather than from
channel positions, so a fixture nobody wrote code for still resolves correctly:
a channel's meaning comes from the capability covering its current DMX value.

Emitters
--------
A fixture can have more than one independently-controllable cell. Two sources:

  * a matrix, where each channel names the pixel it drives;
  * repeated colour channels, where the n-th channel of a given colour belongs
    to cell n. That is what makes a blinder's cool1/cool2/warm1/warm2 resolve to
    two warm/cold cells — and it generalises, so any OFL blinder works without
    per-fixture code.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from core import ofl
from core.fixture_library import FixtureDef, color_slots

# The 13 mixable colours OFL's ColorIntensity can name, as the RGB each one
# contributes at full. Warm/Cold White match the tints the bespoke renderers
# used, so converting them changes nothing on screen.
REFERENCE_RGB: Dict[str, Tuple[int, int, int]] = {
    "Red": (255, 0, 0),
    "Green": (0, 255, 0),
    "Blue": (0, 0, 255),
    "Cyan": (0, 255, 255),
    "Magenta": (255, 0, 255),
    "Yellow": (255, 255, 0),
    "Amber": (255, 126, 0),
    "White": (255, 255, 255),
    "Warm White": (255, 200, 100),
    "Cold White": (200, 220, 255),
    "UV": (85, 0, 220),
    "Lime": (191, 255, 0),
    "Indigo": (75, 0, 130),
}

# Shutter effects that mean "no light", whatever the intensity channel says.
_CLOSED_SHUTTERS = frozenset({"Closed"})


@dataclass(frozen=True)
class EmitterState:
    """One independently-controllable cell: a matrix pixel, or a blinder bulb."""
    key: str = ""
    rgb: Tuple[int, int, int] = (0, 0, 0)
    intensity: float = 0.0

    def scaled_rgb(self) -> Tuple[int, int, int]:
        """The emitter's colour with its intensity applied — what to paint."""
        return tuple(min(255, int(c * self.intensity)) for c in self.rgb)


@dataclass(frozen=True)
class FixtureState:
    """What a fixture is emitting for one DMX frame."""
    intensity: float = 1.0                  # 0..1 master; 1.0 with no Intensity channel
    rgb: Tuple[int, int, int] = (255, 255, 255)
    has_color: bool = False                 # False -> the fixture emits plain white
    color_temp_k: Optional[float] = None
    shutter: str = "Open"
    strobe_hz: Optional[float] = None
    effect: str = ""                        # the running effect's name, if any
    effect_dmx: int = 0
    effect_speed_hz: Optional[float] = None
    zoom_deg: Optional[float] = None
    beam_deg: Optional[Tuple[float, float]] = None
    emitters: Tuple[EmitterState, ...] = ()
    values: Tuple[int, ...] = ()            # per-slot values, fine bytes folded in

    def scaled_rgb(self) -> Tuple[int, int, int]:
        """The fixture's colour with its master intensity applied."""
        return tuple(min(255, int(c * self.intensity)) for c in self.rgb)


def kelvin_to_rgb(kelvin: float) -> Tuple[int, int, int]:
    """Approximate a colour temperature as RGB (Tanner Helland's fit)."""
    temp = max(1000.0, min(40000.0, kelvin)) / 100.0

    if temp <= 66:
        red = 255.0
        green = 99.4708025861 * _log(temp) - 161.1195681661
    else:
        red = 329.698727446 * ((temp - 60) ** -0.1332047592)
        green = 288.1221695283 * ((temp - 60) ** -0.0755148492)

    if temp >= 66:
        blue = 255.0
    elif temp <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * _log(temp - 10) - 305.0447927307

    return tuple(max(0, min(255, int(c))) for c in (red, green, blue))


def _log(value: float) -> float:
    import math
    return math.log(value) if value > 0 else 0.0


def _fold_values(fdef: FixtureDef, dmx: Sequence[int]) -> List[int]:
    """Per-slot values with fine bytes folded into their coarse channel.

    A slot the DMX slice doesn't reach falls back to the channel's declared
    default, so a short frame reads as "not set yet" rather than as blackout.
    """
    raw = [dmx[i] if i < len(dmx) else spec.default_value
           for i, spec in enumerate(fdef.channels)]

    folded = list(raw)
    for index, spec in enumerate(fdef.channels):
        if spec.fine_of >= 0:
            continue
        if spec.resolution_bytes > 1:
            value = raw[index] << (8 * (spec.resolution_bytes - 1))
            for other, fine in enumerate(fdef.channels):
                if fine.fine_of == index:
                    value |= raw[other] << fine.fine_shift
            folded[index] = value
    return folded


def _active_capabilities(spec, values: Sequence[int]):
    """The capabilities in force on a slot right now.

    Normally just the channel's own. A *switching* channel is the exception: its
    slot holds a different channel depending on where its trigger channel
    currently sits, so the same address can be a strobe in one program and an
    effect speed in another, and reading it with the wrong capability list is
    how a fixture ends up strobing when it should be fading.
    """
    if spec.switch_of < 0 or not spec.switch_options:
        return spec.capabilities
    trigger = values[spec.switch_of] if spec.switch_of < len(values) else 0
    for option in spec.switch_options:
        low, high = option.trigger_range
        if low <= trigger <= high:
            return option.channel.capabilities if option.channel else ()
    return spec.capabilities


def _emitter_index(spec, capabilities, colour_seen: Dict[str, int],
                   pixels: Dict[str, int]) -> int:
    """Which cell a channel drives.

    Matrix channels name their pixel outright. Otherwise, repeated channels of
    the same colour are read as repeated cells — the n-th "Warm White" belongs
    to cell n — which is how a multi-cell blinder resolves with no per-fixture
    knowledge.
    """
    if spec.pixel_key:
        return pixels.setdefault(spec.pixel_key, len(pixels))
    for cap in capabilities:
        if cap.type == "ColorIntensity":
            colour = str(cap.get("color", ""))
            index = colour_seen.get(colour, 0)
            colour_seen[colour] = index + 1
            return index
    return 0


def resolve_state(fdef: FixtureDef, dmx: Sequence[int]) -> FixtureState:
    """Resolve one fixture's DMX slice into what it emits."""
    values = _fold_values(fdef, dmx)

    intensity: Optional[float] = None
    shutter = "Open"
    strobe_hz: Optional[float] = None
    effect = ""
    effect_dmx = 0
    effect_speed_hz: Optional[float] = None
    color_temp_k: Optional[float] = None
    zoom_deg: Optional[float] = None
    has_color = False

    # Per-emitter colour accumulators, in float space so several colours can mix
    # before a single clamp at the end.
    mix: Dict[int, List[float]] = {}
    preset: Dict[int, Tuple[int, int, int]] = {}
    pixels: Dict[str, int] = {}
    pixel_names: Dict[int, str] = {}
    colour_seen: Dict[str, int] = {}

    for index, spec in enumerate(fdef.channels):
        if spec.fine_of >= 0:
            continue
        value = values[index]
        # Unused slots and values no capability covers contribute nothing (a
        # placeholder slot has no capabilities at all).
        capabilities = _active_capabilities(spec, values)
        cap = _capability_at(capabilities, value)
        if cap is None:
            continue

        emitter = _emitter_index(spec, capabilities, colour_seen, pixels)
        if spec.pixel_key:
            pixel_names[emitter] = spec.pixel_key

        if cap.type == "Intensity":
            level = ofl.capability_value(cap, value, "brightness", "brightness")
            intensity = cap.position(value) if level is None else level

        elif cap.type == "ColorIntensity":
            level = ofl.capability_value(cap, value, "brightness", "brightness")
            fraction = cap.position(value) if level is None else level
            base = REFERENCE_RGB.get(str(cap.get("color", "")), (255, 255, 255))
            channel_mix = mix.setdefault(emitter, [0.0, 0.0, 0.0])
            for component in range(3):
                channel_mix[component] += base[component] * fraction
            has_color = True

        elif cap.type == "ColorPreset":
            colour = _preset_rgb(cap, value)
            if colour is not None:
                preset[emitter] = colour
                has_color = True
            temperature = ofl.capability_value(cap, value, "colorTemperature",
                                               "colorTemperature")
            if temperature is not None:
                color_temp_k = temperature

        elif cap.type == "ColorTemperature":
            color_temp_k = ofl.capability_value(cap, value, "colorTemperature",
                                                "colorTemperature")
            if color_temp_k is not None:
                # A temperature determines the output colour just as a mix does.
                has_color = True

        elif cap.type == "ShutterStrobe":
            shutter = str(cap.get("shutterEffect", "Open"))
            strobe_hz = _strobe_rate(cap, value)

        elif cap.type in ("StrobeSpeed", "StrobeDuration"):
            rate = _strobe_rate(cap, value)
            if rate is not None:
                strobe_hz = rate

        elif cap.type == "Effect":
            effect = str(cap.get("effectName") or cap.get("effectPreset") or "")
            effect_dmx = value
            speed = ofl.capability_value(cap, value, "speed", "speed")
            if speed is not None:
                effect_speed_hz = speed

        elif cap.type in ("EffectSpeed", "Speed", "EffectDuration"):
            # A dedicated speed channel overrides whatever the effect declared.
            speed = ofl.capability_value(cap, value, "speed", "speed")
            if speed is not None:
                effect_speed_hz = speed

        elif cap.type in ("Zoom", "BeamAngle"):
            zoom_deg = ofl.capability_value(cap, value, "angle", "beamAngle")

    # A fixture with no Intensity channel is always on; its colour channels are
    # the only level control it has.
    master = 1.0 if intensity is None else intensity
    if shutter in _CLOSED_SHUTTERS:
        master = 0.0

    emitters = _build_emitters(fdef, mix, preset, pixel_names, master,
                               color_temp_k)
    rgb = emitters[0].rgb if emitters else (255, 255, 255)

    return FixtureState(
        intensity=max(0.0, min(1.0, master)),
        rgb=rgb,
        has_color=has_color,
        color_temp_k=color_temp_k,
        shutter=shutter,
        strobe_hz=strobe_hz,
        effect=effect,
        effect_dmx=effect_dmx,
        effect_speed_hz=effect_speed_hz,
        zoom_deg=zoom_deg,
        beam_deg=fdef.physical.lens_degrees,
        emitters=emitters,
        values=tuple(values),
    )


def _build_emitters(fdef, mix, preset, pixel_names, master,
                    color_temp_k) -> Tuple[EmitterState, ...]:
    """Assemble the per-cell states, honouring a declared cell count."""
    driven = set(mix) | set(preset)
    count = max(driven) + 1 if driven else 1
    declared = fdef.stage.get("cells")
    if isinstance(declared, int) and declared > 0:
        count = max(count, declared)

    states = []
    for index in range(count):
        if index in preset:
            rgb = preset[index]
        elif index in mix:
            rgb = tuple(min(255, int(c)) for c in mix[index])
        elif driven:
            # Some cells are lit and this one isn't.
            rgb = (0, 0, 0)
        else:
            # Nothing drives colour at all: white, unless a temperature says
            # otherwise below.
            rgb = None

        if color_temp_k is not None:
            tint = kelvin_to_rgb(color_temp_k)
            rgb = tint if rgb is None else tuple(
                min(255, int(c * t / 255)) for c, t in zip(rgb, tint))
        elif rgb is None:
            rgb = (255, 255, 255)

        states.append(EmitterState(key=pixel_names.get(index, ""), rgb=rgb,
                                   intensity=max(0.0, min(1.0, master))))
    return tuple(states)


# --- the write direction ------------------------------------------------------
#
# `resolve_state` mixes a fixture's colours additively from REFERENCE_RGB.
# `solve_color` inverts that same model, so what the controller writes is what
# the stage draws. (A real CMY mixer is subtractive; the app has no subtractive
# model anywhere, and being consistent with the renderer matters more here than
# being right about a dye wheel.)

# Colours that carry no hue, so a palette has nothing to say to them.
_ACHROMATIC = frozenset({"White", "Warm White", "Cold White"})


def solve_color(fdef: FixtureDef, rgb: Sequence[int]) -> Dict[int, int]:
    """`{slot index: value}` making `fdef` emit `rgb` — the inverse of resolve_state.

    Three cases, in order:

      * the fixture mixes **Red + Green + Blue** — the components go straight to
        those three slots and every other colour slot is zeroed, so a leftover
        amber from the previous scene can't tint the new colour;
      * it mixes some other chromatic set (CMY, RGBA-without-blue, …) — the
        colour is fitted greedily onto whatever it has;
      * it mixes only whites, or no colour at all — `{}`. A palette is a hue,
        and a blinder has none to set; leaving it alone is what the role-based
        code did too.
    """
    slots = color_slots(fdef)
    if not slots:
        return {}

    target = [max(0, min(255, int(c))) for c in rgb]
    if {"Red", "Green", "Blue"} <= set(slots):
        values = {index: 0 for index in slots.values()}
        for colour, component in (("Red", 0), ("Green", 1), ("Blue", 2)):
            values[slots[colour]] = target[component]
        return values

    if not set(slots) - _ACHROMATIC:
        return {}
    return _greedy_mix(slots, target)


def _greedy_mix(slots: Dict[str, int], target: List[int]) -> Dict[int, int]:
    """Fit `target` with the fixture's colours, largest contribution first.

    Matching pursuit: repeatedly take the colour whose projection onto what is
    still missing supplies the most of it, at the level that best fits, and
    subtract what it gives. Deterministic, and exact whenever the fixture's
    colours actually span the target.
    """
    remaining = [float(c) for c in target]
    values = {index: 0 for index in slots.values()}
    candidates = {colour: REFERENCE_RGB[colour]
                  for colour in slots if colour in REFERENCE_RGB}

    while candidates:
        best = None
        best_level = 0.0
        best_gain = 0.0
        for colour, reference in candidates.items():
            norm = sum(float(c) * c for c in reference)
            level = sum(r * c for r, c in zip(remaining, reference)) / norm
            level = max(0.0, min(1.0, level))
            gain = level * sum(r * c for r, c in zip(remaining, reference))
            if gain > best_gain:
                best, best_level, best_gain = colour, level, gain
        if best is None:
            break
        values[slots[best]] = round(best_level * 255)
        for component, contribution in enumerate(REFERENCE_RGB[best]):
            remaining[component] = max(0.0, remaining[component]
                                       - best_level * contribution)
        del candidates[best]
    return values


def capability_at(fdef: FixtureDef, index: int, values: Sequence[int]):
    """The capability in force on slot `index`, given the whole DMX slice.

    Public because a channel can't be read in isolation: a switching channel's
    meaning depends on another slot's value, so anything showing "what is this
    channel doing right now" — the stage, the scene editor's read-outs — has to
    ask with the full slice or it will describe the wrong channel.
    """
    if not 0 <= index < len(fdef.channels):
        return None
    spec = fdef.channels[index]
    value = values[index] if index < len(values) else spec.default_value
    return _capability_at(_active_capabilities(spec, values), value)


def _capability_at(capabilities, value: int):
    """The capability covering `value` on a capability list."""
    for cap in capabilities:
        low, high = cap.dmx_range
        if low <= value <= high:
            return cap
    return None


def _preset_rgb(cap, value: int) -> Optional[Tuple[int, int, int]]:
    """The colour a ColorPreset shows at `value`, interpolating a start/end pair."""
    start = _first_hex(cap.get("colorsStart") or cap.get("colors"))
    end = _first_hex(cap.get("colorsEnd"))
    if start is None:
        return end
    if end is None:
        return start
    t = cap.position(value)
    return tuple(int(a + (b - a) * t) for a, b in zip(start, end))


def _first_hex(colors) -> Optional[Tuple[int, int, int]]:
    if not isinstance(colors, (list, tuple)) or not colors:
        return None
    text = str(colors[0]).lstrip("#")
    if len(text) != 6:
        return None
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return None


def _strobe_rate(cap, value: int) -> Optional[float]:
    """The flash rate a strobe capability asks for, in Hz.

    Falls back to the reciprocal of a declared duration when the capability
    describes how long each flash lasts rather than how often it fires.
    """
    speed = ofl.capability_value(cap, value, "speed", "speed")
    if speed is not None:
        return speed
    duration = ofl.capability_value(cap, value, "duration", "time")
    if duration:
        return 1.0 / duration
    return None
