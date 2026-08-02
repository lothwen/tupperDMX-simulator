# ABOUTME: Turns a fixture's DMX slice into what it actually emits — colour,
# ABOUTME: intensity, beam shape, position, strobe, effect — from OFL capabilities.
"""The photometric model, both directions.

`resolve_state` is DMX in, light out: the half of the stage engine that doesn't
draw anything. A renderer asks "what is this fixture doing right now?" and gets
a `FixtureState` back; how to paint that is the renderer's problem. Keeping the
two apart means the same resolution logic serves a pixel bar, a blinder, a
moving head and a hazer, and it stays Qt-free and directly testable.

`solve_color` is the inverse the controller needs: light in, DMX out — "make
this fixture emit this colour, with whatever emitters it happens to have".

Everything is derived from the fixture's OFL capabilities rather than from
channel positions, so a fixture nobody wrote code for still resolves correctly:
a channel's meaning comes from the capability covering its current DMX value.
All 43 OFL capability types are dispatched here; a type this file ignores
(`NoFunction`, `Generic`, `Maintenance`) is one that emits nothing by
definition.

Shape
-----
`FixtureState` groups what it knows into small frozen records — `color`,
`optics`, `motion`, `strobe`, `program`, `atmosphere`, `wheels` — rather than
carrying forty flat fields. The names the app used before the groups existed
(`rgb`, `shutter`, `effect`, `zoom_deg`, …) are still there as read-only views
onto them, so every existing reader keeps working.

Emitters
--------
A fixture can have more than one independently-controllable cell. Three sources:

  * a matrix, where each channel names the pixel it drives;
  * a matrix *group*, where one channel commands several named pixels;
  * repeated channels, where the n-th channel of a given colour — or the n-th
    plain intensity channel — belongs to cell n. That is what makes a blinder's
    cool1/cool2/warm1/warm2 resolve to two warm/cold cells, and it generalises,
    so any OFL blinder works without per-fixture code.
"""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

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

# A prism whose facet count nothing declares. OFL puts the count on a wheel
# slot, which a fixture with no wheels block simply doesn't have; three is the
# commonest prism on the market and reads correctly as "the beam split up".
_DEFAULT_PRISM_FACETS = 3

_EMPTY_MAP: Mapping = MappingProxyType({})


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
class ColorState:
    """What colour the fixture is making, and with which cells."""
    rgb: Tuple[int, int, int] = (255, 255, 255)
    has_color: bool = False                 # False -> the fixture emits plain white
    color_temp_k: Optional[float] = None
    emitters: Tuple[EmitterState, ...] = ()
    # A colour wheel is a gel in front of the lamp: it multiplies whatever the
    # lamp already makes rather than replacing it.
    filter_rgb: Optional[Tuple[int, int, int]] = None


@dataclass(frozen=True)
class BladeState:
    """One framing shutter: how far in it is, and how it is rotated."""
    insertion: float = 0.0
    angle_deg: float = 0.0


@dataclass(frozen=True)
class OpticsState:
    """Everything between the lamp and the air."""
    zoom_deg: Optional[float] = None            # Zoom (and BeamAngle)
    beam_angle_deg: Optional[float] = None      # BeamAngle on its own
    lens_deg: Optional[Tuple[float, float]] = None   # the fixture's declared optics
    focus_m: Optional[float] = None             # Focus
    iris: float = 1.0                           # Iris; 1.0 = wide open
    iris_effect: Tuple[str, Optional[float]] = ("", None)
    frost: float = 0.0                          # Frost; 0.0 = none
    frost_effect: Tuple[str, Optional[float]] = ("", None)
    prism_facets: int = 0                       # 0 = no prism in the beam
    prism_spin_hz: float = 0.0
    prism_angle_deg: Optional[float] = None
    blades: Mapping[str, BladeState] = _EMPTY_MAP
    blade_system_deg: Optional[float] = None
    beam_offset_deg: Tuple[float, float] = (0.0, 0.0)   # BeamPosition (h, v)


@dataclass(frozen=True)
class MotionState:
    """Where the fixture is pointing, and how fast it is getting there."""
    pan_deg: Optional[float] = None
    tilt_deg: Optional[float] = None
    pan_spin_hz: float = 0.0                # PanContinuous (signed)
    tilt_spin_hz: float = 0.0               # TiltContinuous (signed)
    move_speed_hz: Optional[float] = None   # PanTiltSpeed, as a rate
    move_time_s: Optional[float] = None     # PanTiltSpeed, as a duration
    rotation_hz: float = 0.0                # Rotation (signed)
    rotation_deg: Optional[float] = None


@dataclass(frozen=True)
class StrobeState:
    """The shutter: all ten OFL shutter effects, plus the rate behind them."""
    effect: str = "Open"
    hz: Optional[float] = None
    duration_s: Optional[float] = None
    random: bool = False
    sound: bool = False


@dataclass(frozen=True)
class ProgramState:
    """The fixture's own built-in effect, if one is running."""
    name: str = ""
    preset: str = ""
    dmx: int = 0
    speed_hz: Optional[float] = None
    duration_s: Optional[float] = None
    parameter: Optional[float] = None
    sound: bool = False
    sensitivity: Optional[float] = None


@dataclass(frozen=True)
class AtmosphereState:
    """What the fixture is putting into the air. Hazers and smoke machines."""
    output: float = 0.0        # 0..1
    kind: str = ""             # "Fog" | "Haze" | "" (undeclared)


@dataclass(frozen=True)
class WheelState:
    """One wheel's live position — which slot is in the beam, and its motion."""
    name: str = ""
    slot_number: Optional[float] = None
    slot: Optional[ofl.WheelSlot] = None
    next_slot: Optional[ofl.WheelSlot] = None
    blend: float = 0.0              # 0 = on `slot`; 0.5 = halfway to `next_slot`
    spin_hz: float = 0.0            # the whole wheel turning
    angle_deg: Optional[float] = None
    slot_spin_hz: float = 0.0       # the slot turning in its holder
    slot_angle_deg: Optional[float] = None
    shake_hz: float = 0.0
    shake_deg: float = 0.0
    shake_scope: str = ""           # "wheel" | "slot"


@dataclass(frozen=True)
class FixtureState:
    """What a fixture is emitting for one DMX frame."""
    intensity: float = 1.0                  # 0..1 master; 1.0 with no Intensity channel
    values: Tuple[int, ...] = ()            # per-slot values, fine bytes folded in
    color: ColorState = field(default_factory=ColorState)
    optics: OpticsState = field(default_factory=OpticsState)
    motion: MotionState = field(default_factory=MotionState)
    strobe: StrobeState = field(default_factory=StrobeState)
    program: ProgramState = field(default_factory=ProgramState)
    atmosphere: AtmosphereState = field(default_factory=AtmosphereState)
    wheels: Mapping[str, WheelState] = _EMPTY_MAP

    def scaled_rgb(self) -> Tuple[int, int, int]:
        """The fixture's colour with its master intensity applied."""
        return tuple(min(255, int(c * self.intensity)) for c in self.rgb)

    # -- views onto the groups -----------------------------------------
    # The flat names this record carried before it grew groups. Everything
    # outside the stage engine — the timeline's clip swatches, the clip library,
    # the scenes tab's read-out — still reads these, and they are the shape
    # `solve_color` inverts, so they stay.

    @property
    def rgb(self) -> Tuple[int, int, int]:
        return self.color.rgb

    @property
    def has_color(self) -> bool:
        return self.color.has_color

    @property
    def color_temp_k(self) -> Optional[float]:
        return self.color.color_temp_k

    @property
    def emitters(self) -> Tuple[EmitterState, ...]:
        return self.color.emitters

    @property
    def shutter(self) -> str:
        return self.strobe.effect

    @property
    def strobe_hz(self) -> Optional[float]:
        return self.strobe.hz

    @property
    def effect(self) -> str:
        return self.program.name

    @property
    def effect_dmx(self) -> int:
        return self.program.dmx

    @property
    def effect_speed_hz(self) -> Optional[float]:
        return self.program.speed_hz

    @property
    def zoom_deg(self) -> Optional[float]:
        return self.optics.zoom_deg

    @property
    def beam_deg(self) -> Optional[Tuple[float, float]]:
        return self.optics.lens_deg


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


# --- the read direction -------------------------------------------------------
#
# One handler per capability family, looked up by type. A long if/elif chain
# would do the same job, but a table makes "which of OFL's 43 types does this
# app understand?" a question you can answer by reading one dict — and adding a
# type is a function, not an edit to a hundred-line branch.

@dataclass(frozen=True)
class _Slot:
    """Everything a handler needs to know about the slot it is reading."""
    index: int
    spec: Any
    cap: ofl.Capability
    value: int
    emitter: int
    fdef: FixtureDef


class _Builder:
    """Mutable accumulator for one frame. Frozen into a FixtureState at the end.

    Exists so handlers can each write one small thing without threading a dozen
    locals through them; nothing outside this module ever sees it.
    """

    def __init__(self, fdef: FixtureDef):
        self.fdef = fdef

        # Intensity, in three flavours: whole-fixture, per-cell and per-group.
        self.plain_levels: List[float] = []
        self.cell_levels: Dict[int, float] = {}
        self.group_levels: Dict[str, float] = {}

        # Colour accumulators, in float space so several colours can mix before
        # a single clamp at the end.
        self.mix: Dict[int, List[float]] = {}
        self.preset: Dict[int, Tuple[int, int, int]] = {}
        self.pixel_names: Dict[int, str] = {}
        self.has_color = False
        self.color_temp_k: Optional[float] = None
        self.filter_rgb: Optional[Tuple[int, int, int]] = None

        self.optics: Dict[str, Any] = {}
        self.blades: Dict[str, Dict[str, float]] = {}
        self.motion: Dict[str, Any] = {}
        self.strobe: Dict[str, Any] = {"effect": "Open"}
        self.program: Dict[str, Any] = {}
        self.atmosphere: Dict[str, Any] = {}
        self.wheels: Dict[str, Dict[str, Any]] = {}

    def wheel(self, name: str) -> Dict[str, Any]:
        return self.wheels.setdefault(name, {"name": name})

    def blade(self, name: str) -> Dict[str, float]:
        return self.blades.setdefault(name, {})


_Handler = Callable[[_Builder, _Slot], None]
_HANDLERS: Dict[str, _Handler] = {}


def _handles(*types: str):
    """Register a handler for one or more OFL capability types."""
    def decorate(fn: _Handler) -> _Handler:
        for cap_type in types:
            _HANDLERS[cap_type] = fn
        return fn
    return decorate


def _level(slot: _Slot, key: str, kind: str) -> float:
    """A 0..1 level from a declared entity, falling back to the DMX position."""
    declared = ofl.capability_value(slot.cap, slot.value, key, kind)
    return slot.cap.position(slot.value) if declared is None else declared


# -- intensity and colour ------------------------------------------------------

@_handles("Intensity")
def _intensity(builder: _Builder, slot: _Slot) -> None:
    """A dimmer. Whose dimmer depends on what the channel is attached to.

    A matrix channel dims its own pixel, a group channel dims its members, and
    repeated plain dimmers are repeated cells — the same rule the colour
    channels follow. A fixture with one dimmer (nearly all of them) takes the
    last path, which is the whole-fixture master.
    """
    level = _level(slot, "brightness", "brightness")
    if slot.spec.pixel_key:
        builder.cell_levels[slot.emitter] = level
    elif slot.spec.pixel_group:
        builder.group_levels[slot.spec.pixel_group] = level
    else:
        builder.plain_levels.append(level)


@_handles("ColorIntensity")
def _color_intensity(builder: _Builder, slot: _Slot) -> None:
    fraction = _level(slot, "brightness", "brightness")
    base = REFERENCE_RGB.get(str(slot.cap.get("color", "")), (255, 255, 255))
    channel_mix = builder.mix.setdefault(slot.emitter, [0.0, 0.0, 0.0])
    for component in range(3):
        channel_mix[component] += base[component] * fraction
    builder.has_color = True


@_handles("ColorPreset")
def _color_preset(builder: _Builder, slot: _Slot) -> None:
    colour = _preset_rgb(slot.cap, slot.value)
    if colour is not None:
        builder.preset[slot.emitter] = colour
        builder.has_color = True
    temperature = ofl.capability_value(slot.cap, slot.value, "colorTemperature",
                                       "colorTemperature")
    if temperature is not None:
        builder.color_temp_k = temperature


@_handles("ColorTemperature")
def _color_temperature(builder: _Builder, slot: _Slot) -> None:
    temperature = ofl.capability_value(slot.cap, slot.value, "colorTemperature",
                                       "colorTemperature")
    if temperature is not None:
        # A temperature determines the output colour just as a mix does.
        builder.color_temp_k = temperature
        builder.has_color = True


# -- shutter -------------------------------------------------------------------

@_handles("ShutterStrobe")
def _shutter_strobe(builder: _Builder, slot: _Slot) -> None:
    builder.strobe["effect"] = str(slot.cap.get("shutterEffect", "Open"))
    builder.strobe["hz"] = _strobe_rate(slot.cap, slot.value)
    builder.strobe["duration_s"] = ofl.capability_value(
        slot.cap, slot.value, "duration", "time")
    builder.strobe["random"] = bool(slot.cap.get("randomTiming"))
    builder.strobe["sound"] = bool(slot.cap.get("soundControlled"))


@_handles("StrobeSpeed")
def _strobe_speed(builder: _Builder, slot: _Slot) -> None:
    rate = _strobe_rate(slot.cap, slot.value)
    if rate is None:
        return
    builder.strobe["hz"] = rate
    # A fixture whose strobe lives on its own channel never says "Strobe" on a
    # shutter channel, because it hasn't got one. Asking for a rate IS asking to
    # strobe; without this the rate resolved and nothing ever flashed.
    if rate and builder.strobe.get("effect") == "Open":
        builder.strobe["effect"] = "Strobe"


@_handles("StrobeDuration")
def _strobe_duration(builder: _Builder, slot: _Slot) -> None:
    duration = ofl.capability_value(slot.cap, slot.value, "duration", "time")
    if duration:
        builder.strobe["duration_s"] = duration
        builder.strobe["hz"] = 1.0 / duration
        if builder.strobe.get("effect") == "Open":
            builder.strobe["effect"] = "Strobe"


# -- the fixture's own programs ------------------------------------------------

@_handles("Effect")
def _effect(builder: _Builder, slot: _Slot) -> None:
    builder.program["name"] = str(slot.cap.get("effectName")
                                  or slot.cap.get("effectPreset") or "")
    builder.program["preset"] = str(slot.cap.get("effectPreset") or "")
    builder.program["dmx"] = slot.value
    speed = ofl.capability_value(slot.cap, slot.value, "speed", "speed")
    if speed is not None:
        builder.program["speed_hz"] = speed
    duration = ofl.capability_value(slot.cap, slot.value, "duration", "time")
    if duration is not None:
        builder.program["duration_s"] = duration
    parameter = ofl.capability_value(slot.cap, slot.value, "parameter", "parameter")
    if parameter is not None:
        builder.program["parameter"] = parameter
    if slot.cap.get("soundControlled"):
        builder.program["sound"] = True


@_handles("EffectSpeed", "Speed")
def _effect_speed(builder: _Builder, slot: _Slot) -> None:
    # A dedicated speed channel overrides whatever the effect declared.
    speed = ofl.capability_value(slot.cap, slot.value, "speed", "speed")
    if speed is not None:
        builder.program["speed_hz"] = speed


@_handles("EffectDuration", "Time")
def _effect_duration(builder: _Builder, slot: _Slot) -> None:
    duration = ofl.capability_value(slot.cap, slot.value, "duration", "time")
    if duration is None:
        duration = ofl.capability_value(slot.cap, slot.value, "time", "time")
    if duration is not None:
        builder.program["duration_s"] = duration
        if duration > 0 and builder.program.get("speed_hz") is None:
            builder.program["speed_hz"] = 1.0 / duration


@_handles("EffectParameter")
def _effect_parameter(builder: _Builder, slot: _Slot) -> None:
    builder.program["parameter"] = _level(slot, "parameter", "parameter")


@_handles("SoundSensitivity")
def _sound_sensitivity(builder: _Builder, slot: _Slot) -> None:
    builder.program["sensitivity"] = _level(slot, "soundSensitivity", "percent")
    builder.program["sound"] = True


# -- the beam ------------------------------------------------------------------

@_handles("Zoom")
def _zoom(builder: _Builder, slot: _Slot) -> None:
    angle = ofl.capability_value(slot.cap, slot.value, "angle", "beamAngle")
    if angle is not None:
        builder.optics["zoom_deg"] = angle


@_handles("BeamAngle")
def _beam_angle(builder: _Builder, slot: _Slot) -> None:
    angle = ofl.capability_value(slot.cap, slot.value, "angle", "beamAngle")
    if angle is not None:
        builder.optics["beam_angle_deg"] = angle
        # A beam-angle channel IS the fixture's live spread, the same thing a
        # zoom reports, so renderers only have to read one field.
        builder.optics["zoom_deg"] = angle


@_handles("BeamPosition")
def _beam_position(builder: _Builder, slot: _Slot) -> None:
    horizontal = ofl.capability_value(slot.cap, slot.value, "horizontalAngle",
                                      "horizontalAngle")
    vertical = ofl.capability_value(slot.cap, slot.value, "verticalAngle",
                                    "verticalAngle")
    current = builder.optics.get("beam_offset_deg", (0.0, 0.0))
    builder.optics["beam_offset_deg"] = (
        current[0] if horizontal is None else horizontal,
        current[1] if vertical is None else vertical)


@_handles("Focus")
def _focus(builder: _Builder, slot: _Slot) -> None:
    distance = ofl.capability_value(slot.cap, slot.value, "distance", "distance")
    if distance is not None:
        builder.optics["focus_m"] = distance


@_handles("Iris")
def _iris(builder: _Builder, slot: _Slot) -> None:
    builder.optics["iris"] = _level(slot, "openPercent", "irisPercent")


@_handles("Frost")
def _frost(builder: _Builder, slot: _Slot) -> None:
    builder.optics["frost"] = _level(slot, "frostIntensity", "percent")


@_handles("IrisEffect", "FrostEffect")
def _optic_effect(builder: _Builder, slot: _Slot) -> None:
    key = "iris_effect" if slot.cap.type == "IrisEffect" else "frost_effect"
    builder.optics[key] = (str(slot.cap.get("effectName") or ""),
                           ofl.capability_value(slot.cap, slot.value,
                                                "speed", "speed"))


@_handles("Prism", "PrismRotation")
def _prism(builder: _Builder, slot: _Slot) -> None:
    if slot.cap.type == "Prism":
        builder.optics["prism_facets"] = _prism_facets(slot)
    speed = ofl.capability_value(slot.cap, slot.value, "speed", "rotationSpeed")
    if speed is not None:
        builder.optics["prism_spin_hz"] = speed
    angle = ofl.capability_value(slot.cap, slot.value, "angle", "rotationAngle")
    if angle is not None:
        builder.optics["prism_angle_deg"] = angle


@_handles("BladeInsertion", "BladeRotation")
def _blade(builder: _Builder, slot: _Slot) -> None:
    blade = builder.blade(str(slot.cap.get("blade", "")))
    if slot.cap.type == "BladeInsertion":
        blade["insertion"] = _level(slot, "insertion", "insertion")
    else:
        angle = ofl.capability_value(slot.cap, slot.value, "angle", "rotationAngle")
        if angle is not None:
            blade["angle_deg"] = angle


@_handles("BladeSystemRotation")
def _blade_system(builder: _Builder, slot: _Slot) -> None:
    angle = ofl.capability_value(slot.cap, slot.value, "angle", "rotationAngle")
    if angle is not None:
        builder.optics["blade_system_deg"] = angle


# -- where it points -----------------------------------------------------------

@_handles("Pan", "Tilt")
def _pan_tilt(builder: _Builder, slot: _Slot) -> None:
    angle = ofl.capability_value(slot.cap, slot.value, "angle", "rotationAngle")
    if angle is not None:
        builder.motion["pan_deg" if slot.cap.type == "Pan" else "tilt_deg"] = angle


@_handles("PanContinuous", "TiltContinuous")
def _pan_tilt_continuous(builder: _Builder, slot: _Slot) -> None:
    speed = ofl.capability_value(slot.cap, slot.value, "speed", "rotationSpeed")
    if speed is not None:
        key = "pan_spin_hz" if slot.cap.type == "PanContinuous" else "tilt_spin_hz"
        builder.motion[key] = speed


@_handles("PanTiltSpeed")
def _pan_tilt_speed(builder: _Builder, slot: _Slot) -> None:
    speed = ofl.capability_value(slot.cap, slot.value, "speed", "speed")
    if speed is not None:
        builder.motion["move_speed_hz"] = speed
    duration = ofl.capability_value(slot.cap, slot.value, "duration", "time")
    if duration is not None:
        builder.motion["move_time_s"] = duration


@_handles("Rotation")
def _rotation(builder: _Builder, slot: _Slot) -> None:
    speed = ofl.capability_value(slot.cap, slot.value, "speed", "rotationSpeed")
    if speed is not None:
        builder.motion["rotation_hz"] = speed
    angle = ofl.capability_value(slot.cap, slot.value, "angle", "rotationAngle")
    if angle is not None:
        builder.motion["rotation_deg"] = angle


# -- wheels --------------------------------------------------------------------

@_handles("WheelSlot")
def _wheel_slot(builder: _Builder, slot: _Slot) -> None:
    number = ofl.capability_value(slot.cap, slot.value, "slotNumber", "slotNumber")
    if number is None:
        return
    for name in ofl.wheel_names(slot.cap, slot.spec.label or slot.spec.field):
        state = builder.wheel(name)
        state["slot_number"] = number
        _resolve_slot(builder.fdef, name, number, state)


@_handles("WheelShake")
def _wheel_shake(builder: _Builder, slot: _Slot) -> None:
    speed = ofl.capability_value(slot.cap, slot.value, "shakeSpeed", "speed")
    angle = ofl.capability_value(slot.cap, slot.value, "shakeAngle", "swingAngle")
    number = ofl.capability_value(slot.cap, slot.value, "slotNumber", "slotNumber")
    for name in ofl.wheel_names(slot.cap, slot.spec.label or slot.spec.field):
        state = builder.wheel(name)
        state["shake_scope"] = str(slot.cap.get("isShaking", "wheel"))
        if speed is not None:
            state["shake_hz"] = speed
        if angle is not None:
            state["shake_deg"] = angle
        if number is not None:
            state["slot_number"] = number
            _resolve_slot(builder.fdef, name, number, state)


@_handles("WheelRotation", "WheelSlotRotation")
def _wheel_rotation(builder: _Builder, slot: _Slot) -> None:
    whole = slot.cap.type == "WheelRotation"
    speed = ofl.capability_value(slot.cap, slot.value, "speed", "rotationSpeed")
    angle = ofl.capability_value(slot.cap, slot.value, "angle", "rotationAngle")
    for name in ofl.wheel_names(slot.cap, slot.spec.label or slot.spec.field):
        state = builder.wheel(name)
        if speed is not None:
            state["spin_hz" if whole else "slot_spin_hz"] = speed
        if angle is not None:
            state["angle_deg" if whole else "slot_angle_deg"] = angle


def _resolve_slot(fdef: FixtureDef, wheel_name: str, number: float,
                  state: Dict[str, Any]) -> None:
    """Fill in which physical slot a slot number lands on, if the wheel is known.

    A fixture can name a wheel in its capabilities without declaring the wheel
    itself (plenty of OFL documents do). The number is still worth carrying —
    the renderer just has nothing to draw for it.
    """
    wheel = fdef.wheels.get(wheel_name)
    if wheel is None or not wheel.slots:
        return
    current, following, blend = ofl.slot_at(wheel, number)
    state["slot"] = current
    state["next_slot"] = following
    state["blend"] = blend


# -- the air -------------------------------------------------------------------

@_handles("Fog")
def _fog(builder: _Builder, slot: _Slot) -> None:
    output = ofl.capability_value(slot.cap, slot.value, "fogOutput", "fogOutput")
    # A plain Fog capability with no output declared means "on".
    builder.atmosphere["output"] = 1.0 if output is None else output
    kind = str(slot.cap.get("fogType", ""))
    if kind:
        builder.atmosphere["kind"] = kind


@_handles("FogOutput")
def _fog_output(builder: _Builder, slot: _Slot) -> None:
    builder.atmosphere["output"] = _level(slot, "fogOutput", "fogOutput")


@_handles("FogType")
def _fog_type(builder: _Builder, slot: _Slot) -> None:
    kind = str(slot.cap.get("fogType", ""))
    if kind:
        builder.atmosphere["kind"] = kind


@_handles("NoFunction", "Generic", "Maintenance")
def _emits_nothing(builder: _Builder, slot: _Slot) -> None:
    """Three types that are not the fixture emitting anything.

    `NoFunction` is a dead part of a range by definition, `Generic` is a channel
    OFL has no vocabulary for, and `Maintenance` is lamp-on/reset/fan plumbing.
    Listed rather than left to fall off the end of the table, so the table
    really is the answer to "what does this app do with each capability type".
    """


def resolve_state(fdef: FixtureDef, dmx: Sequence[int]) -> FixtureState:
    """Resolve one fixture's DMX slice into what it emits."""
    values = _fold_values(fdef, dmx)
    builder = _Builder(fdef)

    pixels: Dict[str, int] = {}
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
            builder.pixel_names[emitter] = spec.pixel_key

        handler = _HANDLERS.get(cap.type)
        if handler is not None:
            handler(builder, _Slot(index=index, spec=spec, cap=cap, value=value,
                                   emitter=emitter, fdef=fdef))

    return _freeze(builder, pixels, values)


def _freeze(builder: _Builder, pixels: Dict[str, int],
            values: List[int]) -> FixtureState:
    """Turn the accumulator into the immutable record renderers consume."""
    master, cell_levels = _levels(builder, pixels)
    if builder.strobe.get("effect") in _CLOSED_SHUTTERS:
        master = 0.0
        cell_levels = {}

    emitters = _build_emitters(builder, master, cell_levels)
    rgb = emitters[0].rgb if emitters else (255, 255, 255)

    optics = dict(builder.optics)
    optics["lens_deg"] = builder.fdef.physical.lens_degrees
    if builder.blades:
        optics["blades"] = MappingProxyType({
            name: BladeState(**values_) for name, values_ in builder.blades.items()})

    return FixtureState(
        intensity=max(0.0, min(1.0, master)),
        values=tuple(values),
        color=ColorState(rgb=rgb, has_color=builder.has_color,
                         color_temp_k=builder.color_temp_k, emitters=emitters,
                         filter_rgb=builder.filter_rgb),
        optics=OpticsState(**optics),
        motion=MotionState(**builder.motion),
        strobe=StrobeState(**builder.strobe),
        program=ProgramState(**builder.program),
        atmosphere=AtmosphereState(**builder.atmosphere),
        wheels=MappingProxyType({name: WheelState(**state)
                                 for name, state in builder.wheels.items()}),
    )


def _levels(builder: _Builder,
            pixels: Dict[str, int]) -> Tuple[float, Dict[int, float]]:
    """The master level and the per-cell levels, from the dimmers that fired.

    Returns `(master, {cell: level})`. One plain dimmer (or none) is the whole
    fixture's master and leaves the cells alone — the path every fixture without
    per-pixel dimming takes.
    """
    cells = dict(builder.cell_levels)

    matrix = builder.fdef.matrix
    for group, level in builder.group_levels.items():
        members = matrix.pixel_groups.get(group, ()) if matrix else ()
        for key in members:
            if key in pixels:
                cell = pixels[key]
                cells[cell] = cells.get(cell, 1.0) * level

    plain = builder.plain_levels
    if len(plain) > 1:
        # Repeated dimmers are repeated cells, the same rule the colour channels
        # follow. The fixture-wide level is then the brightest of them, so a
        # reader that only wants one number still gets a useful one.
        for cell, level in enumerate(plain):
            cells[cell] = cells.get(cell, 1.0) * level
        return max(plain), cells
    if plain:
        return plain[0], cells
    # No dimmer at all: the fixture is always on, and its colour channels are
    # the only level control it has.
    return 1.0, cells


def _build_emitters(builder: _Builder, master: float,
                    cell_levels: Dict[int, float]) -> Tuple[EmitterState, ...]:
    """Assemble the per-cell states, honouring a declared cell count."""
    mix, preset = builder.mix, builder.preset
    driven = set(mix) | set(preset) | set(cell_levels)
    count = max(driven) + 1 if driven else 1
    declared = builder.fdef.stage.get("cells")
    if isinstance(declared, int) and declared > 0:
        count = max(count, declared)

    colour_driven = set(mix) | set(preset)
    states = []
    for index in range(count):
        if index in preset:
            rgb = preset[index]
        elif index in mix:
            rgb = tuple(min(255, int(c)) for c in mix[index])
        elif colour_driven:
            # Some cells are lit and this one isn't.
            rgb = (0, 0, 0)
        else:
            # Nothing drives colour at all: white, unless a temperature says
            # otherwise below.
            rgb = None

        if builder.color_temp_k is not None:
            tint = kelvin_to_rgb(builder.color_temp_k)
            rgb = tint if rgb is None else tuple(
                min(255, int(c * t / 255)) for c, t in zip(rgb, tint))
        elif rgb is None:
            rgb = (255, 255, 255)

        if builder.filter_rgb is not None:
            rgb = tuple(min(255, int(c * f / 255))
                        for c, f in zip(rgb, builder.filter_rgb))

        level = master * cell_levels.get(index, 1.0)
        states.append(EmitterState(key=builder.pixel_names.get(index, ""), rgb=rgb,
                                   intensity=max(0.0, min(1.0, level))))
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


def _prism_facets(slot: _Slot) -> int:
    """How many copies of the beam a prism makes.

    OFL puts the count on a wheel slot rather than on the capability, so this
    looks for a Prism slot on whichever wheel the capability names — and failing
    that, on any wheel the fixture has.
    """
    for name in ofl.wheel_names(slot.cap, slot.spec.label or slot.spec.field):
        wheel = slot.fdef.wheels.get(name)
        for wheel_slot in (wheel.slots if wheel else ()):
            if wheel_slot.type == "Prism":
                facets = wheel_slot.get("facets")
                if isinstance(facets, int) and facets >= 2:
                    return facets
    for wheel in slot.fdef.wheels.values():
        for wheel_slot in wheel.slots:
            if wheel_slot.type == "Prism":
                facets = wheel_slot.get("facets")
                if isinstance(facets, int) and facets >= 2:
                    return facets
    return _DEFAULT_PRISM_FACETS


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
    return ofl.hex_rgb(colors[0])


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
