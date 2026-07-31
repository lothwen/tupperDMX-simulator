# ABOUTME: Open Fixture Library (schema 12.5.1) document parser — channels,
# ABOUTME: capabilities, matrix/fine-channel expansion and mode resolution.
"""Open Fixture Library format support.

Implements the parts of the OFL fixture schema this app consumes, and nothing
else: it knows nothing about semantic roles, scene banks or renderers. Those
live in `core/fixture_library.py` (which maps an OflFixture onto the app's
FixtureDef) and `core/fixture_state.py` (which turns a DMX slice into something
drawable). Qt-free.

The unit everything downstream wants is a `ResolvedMode`: one OFL mode flattened
to an ordered list of DMX slots, with matrix inserts expanded, fine channels
linked to their coarse channel and unused slots kept in place (they still
consume an address).

We are a *consumer*, not a validator: anything unrecognised is preserved rather
than rejected (unknown capability types keep their properties, an unresolvable
channel key still occupies its address). Only a document that isn't an OFL
fixture at all is refused.
"""

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import re

from tupperlogger import get_logger

logger = get_logger("ofl")

# --- tunable anchors ---------------------------------------------------------
# OFL speaks in keywords ("slow", "fast") and percentages as often as in real
# units, so turning either into a number needs an assumed scale. These only
# affect how animated things look; nothing in the spec fixes them.
SPEED_MAX_HZ = 20.0       # what "fast" / "100%" means on a speed entity
DISTANCE_MAX_M = 20.0     # what "far" / "100%" means on a distance entity
CT_WARM_K = 2800.0        # "warm" / "CTO" / -100%
CT_DEFAULT_K = 5600.0     # "default" / 0%
CT_COLD_K = 8000.0        # "cold" / "CTB" / +100%

_RESOLUTIONS = {"8bit": 1, "16bit": 2, "24bit": 3}

# Longest-first so "ms" wins over "s" and "m^3/min" over "m".
_UNIT_RE = re.compile(
    r"^(-?\d+(?:\.\d+)?)\s*(%|Hz|bpm|rpm|ms|m\^3/min|lm|deg|K|s|m)$")

# Keyword vocabularies, per entity kind (definitions.json#/entities).
_KEYWORDS: Dict[str, Dict[str, float]] = {
    "brightness": {"off": 0.0, "dark": 0.15, "bright": 1.0},
    "percent": {"off": 0.0, "low": 0.25, "high": 1.0},
    "parameter": {"off": 0.0, "low": 0.25, "high": 1.0, "slow": 0.25, "fast": 1.0,
                  "small": 0.25, "big": 1.0, "instant": 0.0, "short": 0.25, "long": 1.0},
    "speed": {"stop": 0.0, "slow": 1.0, "fast": SPEED_MAX_HZ,
              "slow reverse": -1.0, "fast reverse": -SPEED_MAX_HZ},
    "rotationSpeed": {"stop": 0.0, "slow CW": 1.0, "fast CW": SPEED_MAX_HZ,
                      "slow CCW": -1.0, "fast CCW": -SPEED_MAX_HZ},
    "time": {"instant": 0.0, "short": 0.5, "long": 5.0},
    "colorTemperature": {"warm": CT_WARM_K, "CTO": CT_WARM_K, "default": CT_DEFAULT_K,
                         "cold": CT_COLD_K, "CTB": CT_COLD_K},
    "beamAngle": {"closed": 0.0, "narrow": 10.0, "wide": 60.0},
    "distance": {"near": 1.0, "far": DISTANCE_MAX_M},
    "angle": {},
}

# unit suffix -> factor onto the kind's canonical unit. "%" is handled per kind
# below because its meaning varies (fraction, Hz, degrees, Kelvin...).
_UNIT_FACTORS: Dict[str, Dict[str, float]] = {
    "brightness": {},
    "percent": {},
    "parameter": {},
    "speed": {"Hz": 1.0, "bpm": 1 / 60.0, "rpm": 1 / 60.0},
    "rotationSpeed": {"Hz": 1.0, "bpm": 1 / 60.0, "rpm": 1 / 60.0},
    "time": {"s": 1.0, "ms": 1 / 1000.0},
    "colorTemperature": {"K": 1.0},
    "beamAngle": {"deg": 1.0},
    "angle": {"deg": 1.0},
    "distance": {"m": 1.0},
}

# What "100%" means, per kind. None -> percent is a plain 0..1 fraction.
_PERCENT_SCALE: Dict[str, Optional[float]] = {
    "brightness": None, "percent": None, "parameter": None,
    "speed": SPEED_MAX_HZ, "rotationSpeed": SPEED_MAX_HZ,
    "time": None, "beamAngle": 90.0, "angle": 360.0, "distance": DISTANCE_MAX_M,
}


def parse_entity(value: Any, kind: str) -> Optional[float]:
    """One OFL entity value -> a float in `kind`'s canonical unit, or None.

    Canonical units: brightness/percent/parameter -> 0..1, speed/rotationSpeed
    -> Hz (signed; negative is reverse), time -> seconds, colorTemperature ->
    Kelvin, beamAngle/angle -> degrees, distance -> metres.

    Returns None for anything unparseable — including units that can't convert
    (lumens on a brightness, m^3/min on a fog output). Callers fall back to the
    DMX position within the capability's range, which is always available.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    keyword = _KEYWORDS.get(kind, {}).get(text)
    if keyword is not None:
        return keyword

    match = _UNIT_RE.match(text)
    if match is None:
        return None
    number, unit = float(match.group(1)), match.group(2)

    if unit == "%":
        scale = _PERCENT_SCALE.get(kind)
        if kind == "colorTemperature":
            return _color_temperature_from_percent(number)
        return number / 100.0 * (scale if scale is not None else 1.0)

    factor = _UNIT_FACTORS.get(kind, {}).get(unit)
    return None if factor is None else number * factor


def _color_temperature_from_percent(percent: float) -> float:
    """OFL colour temperature as a percentage: -100% is warm, 0% neutral,
    +100% cold — a bipolar scale, not a 0..100 ramp."""
    fraction = max(-1.0, min(1.0, percent / 100.0))
    if fraction < 0:
        return CT_DEFAULT_K + (CT_DEFAULT_K - CT_WARM_K) * fraction
    return CT_DEFAULT_K + (CT_COLD_K - CT_DEFAULT_K) * fraction


def interp(start: Optional[float], end: Optional[float], t: float) -> Optional[float]:
    """None-tolerant lerp. One endpoint alone means a constant."""
    if start is None:
        return end
    if end is None:
        return start
    return start + (end - start) * max(0.0, min(1.0, t))


# --- capabilities ------------------------------------------------------------

@dataclass(frozen=True)
class Capability:
    """One capability: what the channel does over a span of its DMX range."""
    type: str
    dmx_range: Tuple[int, int]          # inclusive, in the channel's resolution space
    props: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def get(self, key: str, default: Any = None) -> Any:
        return self.props.get(key, default)

    def position(self, value: int) -> float:
        """Where `value` sits within this capability's range, 0..1."""
        low, high = self.dmx_range
        if high <= low:
            return 0.0
        return max(0.0, min(1.0, (value - low) / (high - low)))


def capability_value(cap: Capability, value: int, key: str,
                     kind: str) -> Optional[float]:
    """The interpolated entity value of `cap` at DMX `value`.

    Prefers the `<key>Start`/`<key>End` pair (interpolated across the
    capability's DMX range), falling back to the flat `<key>` form.
    """
    start = parse_entity(cap.get(key + "Start"), kind)
    end = parse_entity(cap.get(key + "End"), kind)
    if start is None and end is None:
        return parse_entity(cap.get(key), kind)
    return interp(start, end, cap.position(value))


# --- channels ----------------------------------------------------------------

@dataclass(frozen=True)
class OflChannel:
    key: str
    name: str
    fine_aliases: Tuple[str, ...] = ()
    resolution_bytes: int = 1           # 1 | 2 | 3
    default_value: int = 0
    highlight_value: Optional[int] = None
    constant: bool = False
    precedence: str = "LTP"             # "LTP" | "HTP"
    capabilities: Tuple[Capability, ...] = ()
    pixel_key: str = ""                 # set when expanded from a template channel

    @property
    def max_value(self) -> int:
        return 256 ** self.resolution_bytes - 1

    def capability_at(self, value: int) -> Optional[Capability]:
        """The capability covering `value`, or None if nothing does."""
        for cap in self.capabilities:
            low, high = cap.dmx_range
            if low <= value <= high:
                return cap
        return None


def _dmx_value(raw: Any, max_value: int) -> Optional[int]:
    """A dmxValue (int) or dmxValuePercent ("50%") in the channel's space."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str) and raw.endswith("%"):
        try:
            return int(round(float(raw[:-1]) / 100.0 * max_value))
        except ValueError:
            return None
    return None


def _parse_capability(data: dict, max_value: int, default_range: bool) -> Optional[Capability]:
    if not isinstance(data, dict):
        return None
    props = {k: v for k, v in data.items() if k not in ("type", "dmxRange")}
    if default_range:
        span = (0, max_value)
    else:
        raw = data.get("dmxRange")
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            return None
        low = _dmx_value(raw[0], max_value)
        high = _dmx_value(raw[1], max_value)
        if low is None or high is None:
            return None
        span = (low, high)
    return Capability(type=str(data.get("type", "Generic")), dmx_range=span,
                      props=MappingProxyType(props))


def _parse_channel(key: str, data: dict) -> OflChannel:
    if not isinstance(data, dict):
        data = {}
    aliases = tuple(str(a) for a in data.get("fineChannelAliases", [])
                    if isinstance(a, str))
    resolution = _RESOLUTIONS.get(data.get("dmxValueResolution"), 1 + len(aliases))
    max_value = 256 ** resolution - 1

    caps: List[Capability] = []
    single = data.get("capability")
    if isinstance(single, dict):
        cap = _parse_capability(single, max_value, default_range=True)
        if cap is not None:
            caps.append(cap)
    elif isinstance(data.get("capabilities"), list):
        for entry in data["capabilities"]:
            cap = _parse_capability(entry, max_value, default_range=False)
            if cap is not None:
                caps.append(cap)

    return OflChannel(
        key=key,
        name=str(data.get("name") or key),
        fine_aliases=aliases,
        resolution_bytes=resolution,
        default_value=_dmx_value(data.get("defaultValue"), max_value) or 0,
        highlight_value=_dmx_value(data.get("highlightValue"), max_value),
        constant=bool(data.get("constant", False)),
        precedence=str(data.get("precedence") or "LTP"),
        capabilities=tuple(caps),
    )


def _substitute(value: Any, pixel_key: str) -> Any:
    """Replace $pixelKey throughout a template channel's data."""
    if isinstance(value, str):
        return value.replace("$pixelKey", pixel_key)
    if isinstance(value, list):
        return [_substitute(v, pixel_key) for v in value]
    if isinstance(value, dict):
        return {_substitute(k, pixel_key): _substitute(v, pixel_key)
                for k, v in value.items()}
    return value


# --- physical / matrix -------------------------------------------------------

@dataclass(frozen=True)
class OflPhysical:
    dimensions_mm: Optional[Tuple[float, float, float]] = None
    weight_kg: Optional[float] = None
    power_w: Optional[float] = None
    bulb_type: str = ""
    bulb_color_temp_k: Optional[float] = None
    lens_name: str = ""
    lens_degrees: Optional[Tuple[float, float]] = None
    pixel_dimensions_mm: Optional[Tuple[float, float, float]] = None
    pixel_spacing_mm: Optional[Tuple[float, float, float]] = None


def _xyz(raw: Any) -> Optional[Tuple[float, float, float]]:
    if isinstance(raw, (list, tuple)) and len(raw) == 3:
        try:
            return (float(raw[0]), float(raw[1]), float(raw[2]))
        except (TypeError, ValueError):
            return None
    return None


def _parse_physical(data: Any) -> OflPhysical:
    if not isinstance(data, dict):
        return OflPhysical()
    bulb = data.get("bulb") if isinstance(data.get("bulb"), dict) else {}
    lens = data.get("lens") if isinstance(data.get("lens"), dict) else {}
    pixels = data.get("matrixPixels") if isinstance(data.get("matrixPixels"), dict) else {}
    degrees = lens.get("degreesMinMax")
    return OflPhysical(
        dimensions_mm=_xyz(data.get("dimensions")),
        weight_kg=data.get("weight") if isinstance(data.get("weight"), (int, float)) else None,
        power_w=data.get("power") if isinstance(data.get("power"), (int, float)) else None,
        bulb_type=str(bulb.get("type", "")),
        bulb_color_temp_k=bulb.get("colorTemperature")
        if isinstance(bulb.get("colorTemperature"), (int, float)) else None,
        lens_name=str(lens.get("name", "")),
        lens_degrees=(float(degrees[0]), float(degrees[1]))
        if isinstance(degrees, (list, tuple)) and len(degrees) == 2 else None,
        pixel_dimensions_mm=_xyz(pixels.get("dimensions")),
        pixel_spacing_mm=_xyz(pixels.get("spacing")),
    )


def _natural_key(text: str):
    """Sort key that orders "2" before "10" (OFL's eachPixelABC ordering)."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", text)]


@dataclass(frozen=True)
class OflMatrix:
    pixel_count: Tuple[int, int, int]                     # (x, y, z)
    pixel_keys: Tuple[Tuple[Tuple[Optional[str], ...], ...], ...]   # [z][y][x]
    pixel_groups: Mapping[str, Tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({}))

    def positions(self) -> Dict[str, Tuple[int, int, int]]:
        """pixel key -> its 1-indexed (x, y, z) position."""
        found: Dict[str, Tuple[int, int, int]] = {}
        for z, plane in enumerate(self.pixel_keys):
            for y, row in enumerate(plane):
                for x, key in enumerate(row):
                    if key:
                        found[key] = (x + 1, y + 1, z + 1)
        return found

    def keys_in_order(self, repeat_for: Any) -> Tuple[str, ...]:
        """Pixel (or group) keys in the order a `repeatFor` asks for.

        For the axis orders the LAST letter is the primary sort key, so
        `eachPixelXYZ` walks like reading a book: X varies fastest, Z slowest.
        """
        if isinstance(repeat_for, (list, tuple)):
            return tuple(str(k) for k in repeat_for)
        if repeat_for == "eachPixelGroup":
            return tuple(self.pixel_groups)

        positions = self.positions()
        if repeat_for == "eachPixelABC":
            return tuple(sorted(positions, key=_natural_key))

        axes = str(repeat_for)[len("eachPixel"):] if str(repeat_for).startswith("eachPixel") else ""
        if len(axes) != 3 or set(axes) != set("XYZ"):
            logger.warning(f"Unknown matrix repeatFor '{repeat_for}'")
            return ()
        first, second, third = ("XYZ".index(a) for a in axes)
        return tuple(sorted(positions, key=lambda k: (positions[k][third],
                                                      positions[k][second],
                                                      positions[k][first])))


def _default_pixel_key(x: int, y: int, z: int, defined: Sequence[int]) -> str:
    """OFL's auto-generated key for a pixelCount-only matrix: a bare number
    along a single axis, else a coordinate tuple of the defined axes."""
    coords = (x, y, z)
    if len(defined) <= 1:
        return str(coords[defined[0]] if defined else 1)
    return "(" + ", ".join(str(coords[axis]) for axis in defined) + ")"


def _parse_matrix(data: Any) -> Optional[OflMatrix]:
    if not isinstance(data, dict):
        return None

    raw_keys = data.get("pixelKeys")
    if isinstance(raw_keys, list) and raw_keys:
        planes = []
        for plane in raw_keys:
            rows = [tuple(str(k) if isinstance(k, str) else None for k in row)
                    for row in plane if isinstance(row, list)]
            planes.append(tuple(rows))
        pixel_keys = tuple(planes)
        depth = len(pixel_keys)
        height = max((len(p) for p in pixel_keys), default=0)
        width = max((len(r) for p in pixel_keys for r in p), default=0)
        count = (width, height, depth)
    else:
        count_raw = data.get("pixelCount")
        if not (isinstance(count_raw, (list, tuple)) and len(count_raw) == 3):
            return None
        try:
            count = (int(count_raw[0]), int(count_raw[1]), int(count_raw[2]))
        except (TypeError, ValueError):
            return None
        defined = [i for i, n in enumerate(count) if n > 1]
        pixel_keys = tuple(
            tuple(
                tuple(_default_pixel_key(x + 1, y + 1, z + 1, defined)
                      for x in range(count[0]))
                for y in range(count[1])
            )
            for z in range(count[2])
        )

    # Groups resolve against pixel positions, so build the matrix first.
    matrix = OflMatrix(pixel_count=count, pixel_keys=pixel_keys)
    groups = _parse_pixel_groups(data.get("pixelGroups"), matrix)
    return replace(matrix, pixel_groups=MappingProxyType(groups))


def _parse_pixel_groups(raw: Any, matrix: OflMatrix) -> Dict[str, Tuple[str, ...]]:
    """Resolve every pixelGroups form (explicit list, "all", or an
    x/y/z/name constraint object) to an explicit tuple of pixel keys."""
    if not isinstance(raw, dict):
        return {}
    positions = matrix.positions()
    ordered = matrix.keys_in_order("eachPixelXYZ")
    groups: Dict[str, Tuple[str, ...]] = {}
    for name, spec in raw.items():
        if spec == "all":
            groups[str(name)] = ordered
        elif isinstance(spec, list):
            groups[str(name)] = tuple(str(k) for k in spec)
        elif isinstance(spec, dict):
            groups[str(name)] = tuple(
                key for key in ordered
                if _matches_constraints(key, positions[key], spec))
    return groups


def _matches_constraints(key: str, position: Tuple[int, int, int], spec: dict) -> bool:
    for axis, index in (("x", 0), ("y", 1), ("z", 2)):
        for constraint in spec.get(axis, []) or []:
            if not _matches_number(position[index], constraint):
                return False
    for pattern in spec.get("name", []) or []:
        try:
            if not re.search(pattern, key):
                return False
        except re.error:
            return False
    return True


def _matches_number(value: int, constraint: Any) -> bool:
    """One pixelGroups number constraint: "even", "odd", "=3", ">2", "<=4",
    "3n", "3n+1" (OFL's nth-child-ish syntax)."""
    text = str(constraint).strip()
    if text == "even":
        return value % 2 == 0
    if text == "odd":
        return value % 2 == 1
    match = re.match(r"^(<=|>=|<|>|=)(\d+)$", text)
    if match:
        operator, number = match.group(1), int(match.group(2))
        return {"=": value == number, "<": value < number, ">": value > number,
                "<=": value <= number, ">=": value >= number}[operator]
    match = re.match(r"^(\d*)n(?:\s*\+\s*(\d+))?$", text)
    if match:
        step = int(match.group(1)) if match.group(1) else 1
        offset = int(match.group(2) or 0)
        return step > 0 and value >= offset and (value - offset) % step == 0
    return False


# --- resolved mode -----------------------------------------------------------

@dataclass(frozen=True)
class ResolvedChannel:
    """One DMX slot of a mode, in address order."""
    index: int                          # 0-based offset within the footprint
    key: str                            # "" for an explicitly unused slot
    channel: Optional[OflChannel]       # None for unused / unresolvable slots
    fine_of: int = -1                   # index of the coarse slot, else -1
    fine_shift: int = 0                 # left-shift when folded into the coarse value


@dataclass(frozen=True)
class ResolvedMode:
    name: str
    short_name: str
    channels: Tuple[ResolvedChannel, ...]
    physical: OflPhysical = field(default_factory=OflPhysical)

    @property
    def channel_count(self) -> int:
        return len(self.channels)


@dataclass(frozen=True)
class OflFixture:
    name: str
    short_name: str
    categories: Tuple[str, ...]
    physical: OflPhysical
    matrix: Optional[OflMatrix]
    available_channels: Mapping[str, OflChannel]
    template_channels: Mapping[str, OflChannel]
    modes: Tuple[ResolvedMode, ...]


def resolve_mode(fixture: OflFixture, mode_name: str = "") -> Optional[ResolvedMode]:
    """Look a mode up by name or shortName (case-insensitive). An empty or
    unknown name falls back to the first declared mode."""
    if not fixture.modes:
        return None
    if mode_name:
        wanted = mode_name.strip().lower()
        for mode in fixture.modes:
            if wanted in (mode.name.lower(), mode.short_name.lower()):
                return mode
    return fixture.modes[0]


# --- document parsing --------------------------------------------------------

_REQUIRED_KEYS = ("name", "categories", "modes")


def parse_ofl(data: dict, source: str = "") -> Optional[OflFixture]:
    """Parse one OFL fixture document, or return None if it isn't one.

    The rejection path is the user-facing surface of the OFL-only format, so it
    names the specific key that's missing rather than failing silently.
    """
    where = f" in '{source}'" if source else ""
    if not isinstance(data, dict):
        logger.warning(f"Not an OFL fixture{where}: expected a JSON object")
        return None
    for key in _REQUIRED_KEYS:
        if not data.get(key):
            logger.warning(f"Not an OFL fixture{where}: missing required key '{key}'")
            return None
    if not (data.get("availableChannels") or data.get("templateChannels")):
        logger.warning(f"Not an OFL fixture{where}: needs 'availableChannels' "
                       f"or 'templateChannels'")
        return None

    name = str(data["name"]).strip()
    physical = _parse_physical(data.get("physical"))
    matrix = _parse_matrix(data.get("matrix"))

    available = {str(k): _parse_channel(str(k), v)
                 for k, v in (data.get("availableChannels") or {}).items()}
    templates = {str(k): _parse_channel(str(k), v)
                 for k, v in (data.get("templateChannels") or {}).items()}

    # Expand every template channel for every pixel up front, so mode
    # resolution can look up an expanded key (or one of its fine aliases)
    # exactly like a normal channel.
    expanded: Dict[str, OflChannel] = {}
    if matrix and templates:
        raw_templates = data.get("templateChannels") or {}
        for pixel_key in matrix.positions():
            for template_key, template_data in raw_templates.items():
                resolved_key = str(template_key).replace("$pixelKey", pixel_key)
                channel = _parse_channel(resolved_key,
                                         _substitute(template_data, pixel_key))
                expanded[resolved_key] = replace(channel, pixel_key=pixel_key)

    universe = {**available, **expanded}
    fine_lookup: Dict[str, Tuple[str, int]] = {}
    for key, channel in universe.items():
        for position, alias in enumerate(channel.fine_aliases):
            fine_lookup[alias] = (key, position)

    modes = []
    for mode_data in data["modes"]:
        if not isinstance(mode_data, dict) or not mode_data.get("channels"):
            continue
        mode_name = str(mode_data.get("name", ""))
        if "mode" in mode_name.lower():
            # modeNameString forbids the substring; warn but keep the fixture.
            logger.warning(f"Mode name '{mode_name}'{where} contains 'mode', "
                           f"which OFL disallows")
        modes.append(ResolvedMode(
            name=mode_name,
            short_name=str(mode_data.get("shortName") or mode_name),
            channels=_resolve_channels(mode_data["channels"], universe,
                                       fine_lookup, matrix, source=source),
            physical=_merge_physical(data.get("physical"), mode_data.get("physical")),
        ))

    if not modes:
        logger.warning(f"Not an OFL fixture{where}: no usable modes")
        return None

    return OflFixture(
        name=name,
        short_name=str(data.get("shortName") or name),
        categories=tuple(str(c) for c in data["categories"]),
        physical=physical,
        matrix=matrix,
        available_channels=MappingProxyType(available),
        template_channels=MappingProxyType(templates),
        modes=tuple(modes),
    )


def _merge_physical(fixture_physical: Any, mode_physical: Any) -> OflPhysical:
    """A mode's `physical` overrides the fixture's, key by key."""
    if not isinstance(mode_physical, dict):
        return _parse_physical(fixture_physical)
    base = dict(fixture_physical) if isinstance(fixture_physical, dict) else {}
    base.update(mode_physical)
    return _parse_physical(base)


def _resolve_channels(entries: Sequence[Any], universe: Mapping[str, OflChannel],
                      fine_lookup: Mapping[str, Tuple[str, int]],
                      matrix: Optional[OflMatrix],
                      source: str = "") -> Tuple[ResolvedChannel, ...]:
    """Flatten a mode's `channels` array into ordered DMX slots."""
    where = f" in '{source}'" if source else ""
    slots: List[ResolvedChannel] = []
    emitted: Dict[str, int] = {}      # coarse channel key -> its slot index

    def emit(key: Optional[str]) -> None:
        index = len(slots)
        if key is None:
            slots.append(ResolvedChannel(index=index, key="", channel=None))
            return

        channel = universe.get(key)
        if channel is not None:
            emitted.setdefault(key, index)
            slots.append(ResolvedChannel(index=index, key=key, channel=channel))
            return

        fine = fine_lookup.get(key)
        if fine is not None:
            coarse_key, position = fine
            coarse = universe[coarse_key]
            # The coarse byte holds the top of the value; fine byte `position`
            # contributes the next one down.
            shift = 8 * (coarse.resolution_bytes - 2 - position)
            slots.append(ResolvedChannel(
                index=index, key=key, channel=coarse,
                fine_of=emitted.get(coarse_key, -1), fine_shift=max(0, shift)))
            if coarse_key not in emitted:
                logger.warning(f"Fine channel '{key}'{where} precedes its coarse "
                               f"channel '{coarse_key}'; treating it as 8-bit")
            return

        logger.warning(f"Unknown channel key '{key}'{where}; the slot still "
                       f"consumes a DMX address")
        slots.append(ResolvedChannel(index=index, key=key, channel=None))

    for entry in entries:
        if entry is None:
            emit(None)
        elif isinstance(entry, str):
            emit(entry)
        elif isinstance(entry, dict) and entry.get("insert") == "matrixChannels":
            if matrix is None:
                logger.warning(f"Matrix channel insert{where} without a matrix")
                continue
            for key in _expand_matrix_insert(entry, matrix, where):
                emit(key)
        else:
            logger.warning(f"Unsupported mode channel entry{where}: {entry!r}")

    return tuple(slots)


def _expand_matrix_insert(entry: dict, matrix: OflMatrix,
                          where: str) -> List[Optional[str]]:
    """Expand one `insert: matrixChannels` block into resolved channel keys."""
    pixels = matrix.keys_in_order(entry.get("repeatFor"))
    templates = entry.get("templateChannels")
    if not isinstance(templates, list):
        logger.warning(f"Matrix channel insert{where} without templateChannels")
        return []

    def resolved(template: Any, pixel: str) -> Optional[str]:
        return None if template is None else str(template).replace("$pixelKey", pixel)

    if entry.get("channelOrder") == "perChannel":
        return [resolved(t, p) for t in templates for p in pixels]
    return [resolved(t, p) for p in pixels for t in templates]
