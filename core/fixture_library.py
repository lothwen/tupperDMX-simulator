# ABOUTME: Data-driven fixture-type library. Loads fixture *type* definitions from
# ABOUTME: JSON (shipped fixtures/ + user fixtures/) so new types need no Python code.
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple
import json
import os
import shutil
import sys

from core.paths import bundle_dir, userprefs_dir
from core import ofl
from core.fixture_sidecar import (
    EMPTY_EXTRAS, SIDECAR_SUFFIX, ModeExtras, Sidecar, apply_banks,
    banks_mode_key, parse_sidecar, sidecar_filename, target_filename,
    write_sidecar,
)
from tupperlogger import get_logger

logger = get_logger("fixture_library")

# Channel semantic roles. These MIRROR the ROLE_* constants in
# software_controller/main.py — keep the two in sync. They live here too so the
# JSON-facing name->int mapping and the renderer have a Qt-free import source.
ROLE_GENERIC = 0
ROLE_BRIGHTNESS = 1
ROLE_RED = 2
ROLE_GREEN = 3
ROLE_BLUE = 4
ROLE_WHITE_WARM = 5
ROLE_WHITE_COOL = 6
ROLE_ANIMATION = 7
ROLE_SPEED = 8
ROLE_DIRECTION = 9
ROLE_STROBE = 10
ROLE_MODE = 11

ROLE_NAMES = {
    "generic": ROLE_GENERIC,
    "brightness": ROLE_BRIGHTNESS,
    "red": ROLE_RED,
    "green": ROLE_GREEN,
    "blue": ROLE_BLUE,
    "white_warm": ROLE_WHITE_WARM,
    "white_cool": ROLE_WHITE_COOL,
    "animation": ROLE_ANIMATION,
    "speed": ROLE_SPEED,
    "direction": ROLE_DIRECTION,
    "strobe": ROLE_STROBE,
    "mode": ROLE_MODE,
}

# Per-channel behaviour flags (bitmask). Mirror CH_* in software_controller/main.py.
FLAG_NAMES = {"blend": 0x01, "knob_level": 0x02, "knob_speed": 0x04}

@dataclass(frozen=True)
class ChannelSpec:
    field: str   # attribute name (also the universe order within the fixture)
    role: int    # semantic role (ROLE_*)
    flags: int   # behaviour flags bitmask
    # Everything below is optional detail carried over from the OFL channel, for
    # consumers that want more than the role/flags summary (the stage renderer
    # reads capabilities; note_layout prefers `label` over a prettified field).
    label: str = ""                       # the OFL channel's display name
    capabilities: Tuple[ofl.Capability, ...] = ()
    default_value: int = 0
    constant: bool = False
    precedence: str = "LTP"
    resolution_bytes: int = 1
    fine_of: int = -1                     # index of the coarse slot, else -1
    fine_shift: int = 0                   # left-shift when folded into the coarse value
    pixel_key: str = ""                   # matrix pixel this channel drives
    # A switching channel: what this slot does depends on the live value of the
    # slot at `switch_of`. Everything above describes its DEFAULT resolution, so
    # role/label/flags stay stable for the note layout and clip library; the
    # stage resolves the live one from `switch_options`.
    switch_of: int = -1
    switch_options: Tuple[ofl.SwitchOption, ...] = ()


@dataclass(frozen=True)
class FixtureDef:
    """One fixture type resolved to ONE mode."""
    name: str            # registry key, e.g. "par_rgb" or "ADJ/Focus Spot 4Z"
    channels: List[ChannelSpec]
    banks: Dict[str, List[dict]]  # bank-name -> list of scene dicts (raw, by field)
    chases: List[dict]            # list of {"banks": [...], "scenes": [...]}
    bar_count: int = 1   # physical bars to seed for a "bar" fixture (else 1)
    # OFL-derived detail. `mode` names which mode these channels came from.
    mode: str = ""
    # Folder the definition was found in, relative to its fixtures dir ("" for a
    # root-level file). The fixture browser groups by it — see `_load_dir`.
    manufacturer: str = ""
    categories: Tuple[str, ...] = ()
    physical: ofl.OflPhysical = field(default_factory=ofl.OflPhysical)
    matrix: Optional[ofl.OflMatrix] = None
    effects: Mapping[str, dict] = field(default_factory=lambda: MappingProxyType({}))
    stage: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def channel_count(self) -> int:
        return len(self.channels)


def fixtures_dir_shipped() -> str:
    """Directory of shipped, read-only fixture definitions (repo-root fixtures/)."""
    return os.path.join(bundle_dir(), "fixtures")


def fixtures_dir_user() -> Path:
    """Writable directory for user-imported fixture definitions: a fixtures/
    subfolder of the platform-conventional user-prefs directory."""
    return userprefs_dir() / "fixtures"


# --- the OFL bridge -----------------------------------------------------------
#
# OFL describes a channel by what it *does* over its DMX range (43 capability
# types, 13 mixable colours) rather than by a single semantic tag. The engine,
# the note layout and the clip library all speak ROLE_*, so each channel is
# summarised down to one role and one flag set here. This is the only place that
# translation happens.

# Capability type -> role. Anything absent is ROLE_GENERIC.
_ROLE_BY_CAPABILITY = {
    "Intensity": ROLE_BRIGHTNESS,
    "ShutterStrobe": ROLE_STROBE,
    "StrobeSpeed": ROLE_STROBE,
    "StrobeDuration": ROLE_STROBE,
    "Effect": ROLE_ANIMATION,
    "EffectSpeed": ROLE_SPEED,
    "EffectDuration": ROLE_SPEED,
    "EffectParameter": ROLE_SPEED,
    "Speed": ROLE_SPEED,
    "PanTiltSpeed": ROLE_SPEED,
    "Maintenance": ROLE_MODE,
}

# ColorIntensity `color` -> role. The other seven mixable colours (Cyan,
# Magenta, Yellow, Amber, UV, Lime, Indigo) have no ROLE_*, so they stay
# ROLE_GENERIC — the stage renderer mixes them from the capability directly.
_ROLE_BY_COLOR = {
    "Red": ROLE_RED,
    "Green": ROLE_GREEN,
    "Blue": ROLE_BLUE,
    "Warm White": ROLE_WHITE_WARM,
    "Cold White": ROLE_WHITE_COOL,
    "White": ROLE_WHITE_COOL,
}

# Capability types whose value is a level worth fading between. Effects, strobes
# and mode selectors are discrete: interpolating them would sweep through every
# intermediate program instead of cutting to the new one.
_BLEND_CAPABILITIES = frozenset({
    "Intensity", "ColorIntensity", "ColorTemperature",
    "Zoom", "Focus", "Iris", "Frost", "BeamAngle", "BeamPosition", "Pan", "Tilt",
})

# Tiebreak order when two capability types cover the same amount of DMX.
_ROLE_PRIORITY = (ROLE_RED, ROLE_GREEN, ROLE_BLUE, ROLE_WHITE_WARM, ROLE_WHITE_COOL,
                  ROLE_BRIGHTNESS, ROLE_STROBE, ROLE_ANIMATION, ROLE_SPEED,
                  ROLE_MODE, ROLE_GENERIC)

# ROLE_DIRECTION has no producer on purpose: OFL folds direction into the sign of
# a Rotation/Speed value, and guessing "negative speed means direction" would
# misfire on real fixtures. A sidecar declares it explicitly instead.


def _role_for_capability(cap: ofl.Capability) -> int:
    if cap.type == "ColorIntensity":
        return _ROLE_BY_COLOR.get(str(cap.get("color", "")), ROLE_GENERIC)
    return _ROLE_BY_CAPABILITY.get(cap.type, ROLE_GENERIC)


def dominant_capability(
        capabilities: Tuple[ofl.Capability, ...]) -> Optional[ofl.Capability]:
    """The capability that best characterises a channel.

    A channel can mix capability types across its range (a strobe channel that
    is `NoFunction` at the bottom, a mode selector that is several `Effect`s).
    The type covering the most DMX wins; `NoFunction` never counts, since it is
    by definition the part of the range that does nothing.

    Takes the capability tuple rather than the channel so it serves an
    `OflChannel` and an already-bridged `ChannelSpec` alike.
    """
    spans: Dict[tuple, int] = {}
    first: Dict[tuple, ofl.Capability] = {}
    for cap in capabilities:
        if cap.type == "NoFunction":
            continue
        key = (cap.type, str(cap.get("color", "")))
        low, high = cap.dmx_range
        spans[key] = spans.get(key, 0) + (high - low + 1)
        first.setdefault(key, cap)
    if not spans:
        return None
    widest = max(spans.values())
    winners = [key for key, span in spans.items() if span == widest]
    winners.sort(key=lambda k: _ROLE_PRIORITY.index(_role_for_capability(first[k])))
    return first[winners[0]]


def _flags_from_names(names: Any) -> int:
    flags = 0
    for name in names or []:
        if isinstance(name, str):
            flags |= FLAG_NAMES.get(name, 0)
    return flags


def _channel_spec(slot: ofl.ResolvedChannel, extras: ModeExtras) -> ChannelSpec:
    """One resolved OFL slot -> the ChannelSpec the rest of the app consumes."""
    # An unused or unresolvable slot still occupies a DMX address, so it gets a
    # placeholder field rather than being dropped (which would shift every
    # channel after it, and with them every note binding).
    if slot.channel is None:
        return ChannelSpec(field=slot.key or f"__unused_{slot.index}",
                           role=ROLE_GENERIC, flags=0, label="—")

    channel = slot.channel
    override = extras.channels.get(slot.key, {})

    if slot.fine_of >= 0:
        # A fine byte is part of its coarse channel's value, not a channel in
        # its own right: keeping it ROLE_GENERIC stops role_index_map's
        # first-wins scan from ever resolving a role to the fine slot, and stops
        # the clip library counting it as a second red.
        coarse = dominant_capability(channel.capabilities)
        blend = FLAG_NAMES["blend"] if (coarse and coarse.type in _BLEND_CAPABILITIES) else 0
        return ChannelSpec(
            field=slot.key, role=ROLE_GENERIC, flags=blend,
            label=channel.name, capabilities=channel.capabilities,
            default_value=channel.default_value, constant=channel.constant,
            precedence=channel.precedence, resolution_bytes=channel.resolution_bytes,
            fine_of=slot.fine_of, fine_shift=slot.fine_shift,
            pixel_key=channel.pixel_key)

    dominant = dominant_capability(channel.capabilities)
    role = _role_for_capability(dominant) if dominant else ROLE_GENERIC
    flags = FLAG_NAMES["blend"] if (dominant and dominant.type in _BLEND_CAPABILITIES) else 0

    if "role" in override:
        role = ROLE_NAMES.get(str(override["role"]), role)
    if "flags" in override:
        # A declared flag set replaces the derived one wholesale — the knob
        # flags can't be derived (two fixtures' brightness channels differ), so
        # a sidecar that mentions flags is stating the whole truth for them.
        flags = _flags_from_names(override["flags"])

    return ChannelSpec(
        field=slot.key, role=role, flags=flags,
        label=channel.name, capabilities=channel.capabilities,
        default_value=channel.default_value, constant=channel.constant,
        precedence=channel.precedence, resolution_bytes=channel.resolution_bytes,
        pixel_key=channel.pixel_key,
        switch_of=slot.switch_of, switch_options=slot.switch_options)


def build_fixture_def(key: str, fixture: ofl.OflFixture,
                      sidecar: Optional[Sidecar] = None,
                      mode_name: str = "",
                      manufacturer: str = "") -> Optional[FixtureDef]:
    """Combine an OFL document, its sidecar and a mode into a FixtureDef."""
    if not mode_name and sidecar is not None:
        mode_name = sidecar.default_mode
    mode = ofl.resolve_mode(fixture, mode_name)
    if mode is None:
        return None
    extras = sidecar.for_mode(mode.name) if sidecar is not None else EMPTY_EXTRAS

    return FixtureDef(
        name=key,
        channels=[_channel_spec(slot, extras) for slot in mode.channels],
        banks={name: list(scenes) for name, scenes in extras.banks.items()},
        chases=list(extras.chases),
        # A matrix's X extent is how many physical bodies share this DMX slice
        # (the ledbar's 8 bars all mirror the same 7 channels), so it is also
        # how many render objects the stage seeds.
        bar_count=fixture.matrix.pixel_count[0] if fixture.matrix else 1,
        mode=mode.name,
        manufacturer=manufacturer,
        categories=fixture.categories,
        physical=mode.physical,
        matrix=fixture.matrix,
        effects=extras.effects,
        stage=extras.stage,
    )


def role_index_map(fdef: FixtureDef) -> Dict[int, int]:
    """Map each role to the index of its first channel within the DMX slice."""
    index: Dict[int, int] = {}
    for i, ch in enumerate(fdef.channels):
        index.setdefault(ch.role, i)
    return index


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Skipping fixture def '{path}': {e}")
        return None


@dataclass(frozen=True)
class _Source:
    """Where a registered fixture type came from.

    Kept per key so a mode other than the default can be resolved on demand
    without re-reading the filesystem, and so an edit to the sidecar knows
    which files to write (see `save_fixture_banks`).
    """
    fixture: ofl.OflFixture
    sidecar: Optional[Sidecar]
    manufacturer: str
    ofl_path: Path
    sidecar_path: Optional[Path]   # None when the definition ships without one


def _manufacturer_of(path: Path, root: Path) -> str:
    """The folder `path` sits in, relative to its fixtures dir.

    A file directly in the fixtures dir has no manufacturer (""). Nesting deeper
    than one level keeps the whole relative path, so the folder layout is always
    what the key and the browser show.
    """
    relative = path.parent.relative_to(root)
    return "" if relative == Path(".") else relative.as_posix()


def _load_dir(directory) -> Tuple[Dict[str, FixtureDef], Dict[str, _Source]]:
    """Load one fixture-defs layer.

    A definition is one or two files: the OFL document, and optionally a
    `.tdmx.json` sidecar beside it. Sidecars are indexed first so each OFL file
    can be paired with its own as it is parsed.

    Subfolders are the *manufacturer*: `ADJ/focus-spot-4z.json` registers as
    "ADJ/Focus Spot 4Z". Namespacing by folder is what lets two makers ship a
    fixture of the same name without one silently replacing the other; files
    directly in the fixtures dir keep their bare key, so the shipped defs
    (ledbar, blinder, …) and every patch referring to them are unaffected.
    """
    defs: Dict[str, FixtureDef] = {}
    sources: Dict[str, _Source] = {}
    if not os.path.isdir(directory):
        return defs, sources

    root = Path(directory)
    paths = sorted(root.rglob("*.json"))

    # Pass A: sidecars, keyed by the OFL file each one targets. A sidecar only
    # ever pairs with a document in its OWN folder, so the key is the full path.
    sidecars: Dict[Path, Tuple[Sidecar, Path]] = {}
    ofl_paths = {p for p in paths if not p.name.endswith(SIDECAR_SUFFIX)}
    for path in paths:
        if not path.name.endswith(SIDECAR_SUFFIX):
            continue
        data = _read_json(path)
        if data is None:
            continue
        sidecar = parse_sidecar(data, str(path))
        if sidecar is None:
            continue
        target = path.parent / target_filename(path.name, sidecar)
        if target not in ofl_paths:
            logger.warning(f"Sidecar '{path}' targets '{target.name}', which is not "
                           f"beside it; ignoring it")
            continue
        sidecars[target] = (sidecar, path)

    # Pass B: the OFL documents themselves.
    for path in paths:
        if path.name.endswith(SIDECAR_SUFFIX):
            continue
        data = _read_json(path)
        if data is None:
            continue
        fixture = ofl.parse_ofl(data, str(path))
        if fixture is None:
            # parse_ofl has already logged which required key is missing. A
            # definition that doesn't resolve means its fixtures vanish from any
            # patch using it (Config._parse_fixtures drops unknown types), so
            # the reason needs to be findable in the log.
            continue

        sidecar, sidecar_path = sidecars.get(path, (None, None))
        manufacturer = _manufacturer_of(path, root)
        base = (sidecar.key if sidecar is not None and sidecar.key else fixture.name)
        key = f"{manufacturer}/{base}" if manufacturer else base
        fdef = build_fixture_def(key, fixture, sidecar, manufacturer=manufacturer)
        if fdef is None:
            logger.warning(f"Skipping fixture def '{path}': no usable mode")
            continue
        defs[key] = fdef
        sources[key] = _Source(fixture, sidecar, manufacturer, path, sidecar_path)

    return defs, sources


_cache: Optional[Dict[str, FixtureDef]] = None
_sources: Dict[str, _Source] = {}
_mode_cache: Dict[Tuple[str, str], Optional[FixtureDef]] = {}
_generation = 0
_show_fixtures_dir: Optional[str] = None


def cache_generation() -> int:
    """Bumped whenever the registry is invalidated.

    Lets a downstream cache (the software controller's compiled fixture types)
    notice a reload without fixture_library having to import it — which would
    be a cycle, since that module imports core.config, which imports this one.
    """
    return _generation


def set_show_fixtures_dir(path) -> None:
    """Activate (or clear, with None) the show-local fixture-defs layer — the
    fixtures/ folder inside the open show project. Show defs override user
    defs, which override shipped ones. Invalidates the memoized registry."""
    global _show_fixtures_dir
    new = str(path) if path else None
    if new != _show_fixtures_dir:
        _show_fixtures_dir = new
        clear_cache()


def load_fixture_defs(force_reload: bool = False) -> Dict[str, FixtureDef]:
    """Return {name: FixtureDef} from the shipped, user, then show-local
    fixtures dirs (later layers override same-name defs from earlier ones).

    The result is memoized; pass force_reload=True (or call clear_cache())
    after writing new files.
    """
    global _cache, _sources
    if _cache is not None and not force_reload:
        return _cache

    defs: Dict[str, FixtureDef] = {}
    sources: Dict[str, _Source] = {}
    layers = [fixtures_dir_shipped(), fixtures_dir_user()]
    if _show_fixtures_dir:
        layers.append(_show_fixtures_dir)
    for layer in layers:
        layer_defs, layer_sources = _load_dir(layer)
        defs.update(layer_defs)
        # A later layer replacing a def must drop the earlier layer's source
        # too, so an overridden key can't resolve modes from the old document.
        for key in layer_defs:
            sources.pop(key, None)
        sources.update(layer_sources)

    _cache = defs
    _sources = sources
    _mode_cache.clear()
    return defs


def resolve_fixture(type_name: str, mode: str = "") -> Optional[FixtureDef]:
    """A fixture type resolved to a specific mode.

    An empty or unknown mode falls back to the type's default. This is the
    canonical accessor; `load_fixture_defs()[t]` is the same thing at t's
    default mode.
    """
    defs = load_fixture_defs()
    if not mode:
        return defs.get(type_name)

    cached = _mode_cache.get((type_name, mode))
    if cached is not None:
        return cached

    source = _sources.get(type_name)
    if source is None:
        return defs.get(type_name)
    fdef = (build_fixture_def(type_name, source.fixture, source.sidecar, mode,
                              source.manufacturer)
            or defs.get(type_name))
    _mode_cache[(type_name, mode)] = fdef
    return fdef


def fixture_document(type_name: str) -> Optional[ofl.OflFixture]:
    """The parsed OFL document behind a type, mode-independent.

    The FixtureDef is one *mode* summarised for the engine; the browser also
    wants what the document says about the product as a whole (its metadata,
    links, every mode it declares), which only lives here.
    """
    source = _source_for(type_name)
    return source.fixture if source is not None else None


def _source_for(type_name: str) -> Optional[_Source]:
    """The loaded source record for a type, loading the registry if needed."""
    source = _sources.get(type_name)
    if source is None:
        load_fixture_defs()
        source = _sources.get(type_name)
    return source


def fixture_modes(type_name: str) -> List[str]:
    """The mode names a type declares, in order ([] if it has none or is
    unknown). The designer offers a mode picker only when there's a choice."""
    document = fixture_document(type_name)
    return [mode.name for mode in document.modes] if document else []


# --- editing a definition's scene banks ---------------------------------------
#
# The scene editor is the only writer. It touches `banks` (and the `chases` that
# name a renamed bank) and nothing else — never the OFL document, so a type's
# DMX footprint, and with it every saved note binding, is untouchable from here.


def _is_persistable(directory: Path) -> bool:
    """Whether writing into `directory` would actually outlive the session.

    A frozen build's shipped fixtures/ lives under PyInstaller's `_MEIPASS`,
    which *is* writable — and is deleted on exit. Writes there would vanish
    silently, so the bundle is treated as read-only whatever the mode bits say.
    """
    if getattr(sys, "frozen", False):
        try:
            directory.relative_to(Path(bundle_dir()))
            return False
        except ValueError:
            pass
    return os.access(str(directory), os.W_OK)


def _fork_to_user_layer(source: _Source) -> Tuple[Path, Path]:
    """Copy a read-only definition into the writable user layer.

    Both files are copied under the same manufacturer folder and the same
    names, so the fork registers under the identical key — and, being a later
    layer, overrides the shipped one from the next reload on.
    """
    target_dir = Path(fixtures_dir_user()) / source.manufacturer
    target_dir.mkdir(parents=True, exist_ok=True)

    ofl_path = target_dir / source.ofl_path.name
    if not ofl_path.exists():
        shutil.copyfile(source.ofl_path, ofl_path)

    name = (source.sidecar_path.name if source.sidecar_path is not None
            else sidecar_filename(source.ofl_path))
    sidecar_path = target_dir / name
    if source.sidecar_path is not None and not sidecar_path.exists():
        shutil.copyfile(source.sidecar_path, sidecar_path)

    logger.info(f"Forked read-only fixture def '{source.ofl_path}' into "
                f"'{target_dir}' so it can be edited")
    return ofl_path, sidecar_path


def save_fixture_banks(type_name: str, mode: str,
                       banks: Mapping[str, List[dict]],
                       chases: Optional[List[dict]] = None) -> Optional[Path]:
    """Persist a type's scene banks to its `.tdmx.json`, and reload the registry.

    Writes beside the OFL document the type came from; when that isn't
    persistable (a shipped def in a frozen build, a read-only install) the
    definition is forked into the user layer first and written there. `mode` is
    the RESOLVED mode name the banks were authored against.

    Returns the sidecar path written, or None if the type is unknown.
    """
    source = _source_for(type_name)
    if source is None:
        logger.warning(f"Cannot save banks for unknown fixture type '{type_name}'")
        return None

    sidecar_path = source.sidecar_path
    if _is_persistable(source.ofl_path.parent):
        if sidecar_path is None:
            sidecar_path = source.ofl_path.with_name(sidecar_filename(source.ofl_path))
    else:
        _, sidecar_path = _fork_to_user_layer(source)

    raw: Any = {}
    if sidecar_path.exists():
        raw = _read_json(sidecar_path)
        if raw is None:
            # Unreadable, not absent: overwriting would silently discard whatever
            # is in there. _read_json has already logged why.
            logger.error(f"Refusing to overwrite unreadable sidecar '{sidecar_path}'")
            return None

    mode_key = banks_mode_key(raw, mode, len(source.fixture.modes) <= 1)
    write_sidecar(sidecar_path, apply_banks(raw, mode_key, banks, chases))

    # Bumps the generation, so the software controller rebuilds its compiled
    # fixture types and the new scenes are live without a restart.
    clear_cache()
    logger.info(f"Saved {sum(len(s) for s in banks.values())} scene(s) in "
                f"{len(banks)} bank(s) for '{type_name}' to '{sidecar_path}'")
    return sidecar_path


def clear_cache() -> None:
    """Drop the memoized definitions (used by tests / after an import)."""
    global _cache, _sources, _generation
    _cache = None
    _sources = {}
    _mode_cache.clear()
    _generation += 1


# --- fixture-type presentation colours ----------------------------------------
#
# Each patched fixture is coloured by its *type* so a fixture's clip-library
# section, its timeline clips and its track-header swatch all read as one group.
# The clip library and the timeline both derive the colour from here so the two
# never drift.

# Palette cycled across patched fixture types, ordered by DMX address.
GROUP_COLORS = ['#4a90e2', '#e2904a', '#4caf50', '#e24a4a', '#9b59b6',
                '#e24ad0', '#4ad0e2', '#7f8c8d', '#1abc9c', '#d4b106']


def patched_types(fixtures) -> List[str]:
    """Unique fixture-type names, deduped, ordered by DMX address."""
    seen: List[str] = []
    for inst in sorted(fixtures, key=lambda f: getattr(f, "dmx_start", 0)):
        t = getattr(inst, "type", None)
        if t and t not in seen:
            seen.append(t)
    return seen


def color_for_type(fixtures, ftype: str) -> str:
    """Hex colour assigned to `ftype` within `fixtures` (cycles GROUP_COLORS).

    Falls back to the first palette entry for an unpatched/unknown type."""
    types = patched_types(fixtures)
    if ftype in types:
        return GROUP_COLORS[types.index(ftype) % len(GROUP_COLORS)]
    return GROUP_COLORS[0]
