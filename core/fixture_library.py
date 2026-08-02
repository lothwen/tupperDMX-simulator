# ABOUTME: Data-driven fixture-type library. Loads fixture *type* definitions from
# ABOUTME: JSON (shipped fixtures/ + user fixtures/) so new types need no Python code.
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
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

# Per-channel behaviour flags (bitmask). Mirror CH_* in software_controller/main.py.
FLAG_NAMES = {"blend": 0x01, "knob_level": 0x02, "knob_speed": 0x04}

@dataclass(frozen=True)
class ChannelSpec:
    field: str   # attribute name (also the universe order within the fixture)
    flags: int   # behaviour flags bitmask
    # Everything below is carried over from the OFL channel. `capabilities` is
    # the authority on what this channel does — what it emits, what it means at
    # a given value, whether it is a colour and which one — read directly by the
    # stage engine, the channel-family labels and the colour lookups.
    label: str = ""                       # the OFL channel's display name
    capabilities: Tuple[ofl.Capability, ...] = ()
    default_value: int = 0
    constant: bool = False
    precedence: str = "LTP"
    resolution_bytes: int = 1
    fine_of: int = -1                     # index of the coarse slot, else -1
    fine_shift: int = 0                   # left-shift when folded into the coarse value
    pixel_key: str = ""                   # matrix pixel this channel drives
    pixel_group: str = ""                 # matrix group it drives, when not one pixel
    # A sidecar-declared display name, overriding what the capabilities say this
    # channel is. Kept apart from `label` so that stays the pristine OFL name.
    display_name: str = ""
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
    # OFL-derived detail. `mode` names which mode these channels came from.
    mode: str = ""
    # Folder the definition was found in, relative to its fixtures dir ("" for a
    # root-level file). The fixture browser groups by it — see `_load_dir`.
    manufacturer: str = ""
    categories: Tuple[str, ...] = ()
    physical: ofl.OflPhysical = field(default_factory=ofl.OflPhysical)
    matrix: Optional[ofl.OflMatrix] = None
    # The fixture's wheels, by name. A WheelSlot capability says which slot is in
    # the beam; this says what that slot is — a gel colour, a gobo, a prism.
    wheels: Mapping[str, ofl.Wheel] = field(
        default_factory=lambda: MappingProxyType({}))
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
# types, 13 mixable colours), and that description is what the whole app reads:
# the stage engine resolves it per frame, `color_slots` addresses colour by name
# and `channel_families` labels it. All this bridge adds is the one thing OFL
# cannot express — the behaviour flags — plus a sidecar's display name.

# Capability types whose value is a level worth fading between. Effects, strobes,
# wheel slots and mode selectors are discrete: interpolating them would sweep
# through every intermediate program (or every gobo) instead of cutting to the
# new one. Note what is deliberately absent — `EffectSpeed`, `Speed` and
# `Effect`: the shipped blinder derives its flags from this table, so adding
# them would start fading its speed channel and change real controller output.
_BLEND_CAPABILITIES = frozenset({
    "Intensity", "ColorIntensity", "ColorTemperature",
    "Zoom", "Focus", "Iris", "Frost", "BeamAngle", "BeamPosition", "Pan", "Tilt",
    # The continuous ones: a value that names a rate or a depth, where sweeping
    # between two settings is exactly what the fixture does anyway.
    "PanContinuous", "TiltContinuous", "PanTiltSpeed", "Rotation",
    "PrismRotation", "WheelRotation", "WheelSlotRotation",
    "BladeInsertion", "BladeRotation", "BladeSystemRotation",
    "Fog", "FogOutput", "SoundSensitivity", "EffectParameter",
})

# Tiebreak order when two capability types cover the same amount of DMX on one
# channel: the one that describes the light itself wins over one that describes
# how it shapes it, then how it moves, then what program it runs. Types absent
# here rank last, and two capabilities of the SAME type (say a Red and a Green
# covering half the range each) fall back to the order the fixture declares them
# in. `NoFunction` is missing on purpose — it never competes.
_TYPE_PRIORITY = (
    # what the light IS
    "ColorIntensity", "ColorPreset", "ColorTemperature", "Intensity",
    # what it passes through
    "WheelSlot", "WheelRotation", "WheelSlotRotation", "WheelShake",
    "ShutterStrobe", "StrobeSpeed", "StrobeDuration",
    "Prism", "PrismRotation", "Iris", "IrisEffect", "Frost", "FrostEffect",
    "Zoom", "Focus", "BeamAngle", "BeamPosition",
    "BladeInsertion", "BladeRotation", "BladeSystemRotation",
    # where it points
    "Pan", "PanContinuous", "Tilt", "TiltContinuous", "PanTiltSpeed",
    # what it fills the air with
    "Fog", "FogOutput", "FogType",
    # what program is running
    "Effect", "EffectSpeed", "EffectDuration", "EffectParameter",
    "SoundSensitivity", "Rotation", "Speed", "Time",
    # housekeeping
    "Maintenance", "Generic",
)


def _dominance_key(cap: ofl.Capability) -> tuple:
    """What makes two capabilities on one channel "the same thing".

    Two ranges only add up if they describe the same subject: the same colour,
    the same wheel, the same blade. Keying on the type alone made a channel that
    selects a colour wheel over half its range and a gobo wheel over the other
    half look like one 100%-wide wheel channel.
    """
    wheel = cap.get("wheel")
    if isinstance(wheel, (list, tuple)):
        wheel = ",".join(str(name) for name in wheel)
    return (cap.type, str(cap.get("color", "")), str(wheel or ""),
            str(cap.get("blade", "")))


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
        key = _dominance_key(cap)
        low, high = cap.dmx_range
        spans[key] = spans.get(key, 0) + (high - low + 1)
        first.setdefault(key, cap)
    if not spans:
        return None
    widest = max(spans.values())
    winners = [key for key, span in spans.items() if span == widest]
    winners.sort(key=lambda k: (_TYPE_PRIORITY.index(k[0])
                                if k[0] in _TYPE_PRIORITY else len(_TYPE_PRIORITY)))
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
                           flags=0, label="—")

    channel = slot.channel
    override = extras.channels.get(slot.key, {})
    dominant = dominant_capability(channel.capabilities)
    flags = FLAG_NAMES["blend"] if (dominant and dominant.type in _BLEND_CAPABILITIES) else 0

    if slot.fine_of >= 0:
        # A fine byte is part of its coarse channel's value, not a channel in
        # its own right, so it carries no display name of its own — everything
        # that reads one resolves it through the slot at `fine_of`.
        return ChannelSpec(
            field=slot.key, flags=flags,
            label=channel.name, capabilities=channel.capabilities,
            default_value=channel.default_value, constant=channel.constant,
            precedence=channel.precedence, resolution_bytes=channel.resolution_bytes,
            fine_of=slot.fine_of, fine_shift=slot.fine_shift,
            pixel_key=channel.pixel_key, pixel_group=channel.pixel_group)

    if "role" in override:
        logger.warning(
            f"Fixture channel '{slot.key}' declares the obsolete sidecar key "
            f"'role'; it is ignored — use \"label\" to override the displayed "
            f"name (what 'role' was actually doing)")
    if "flags" in override:
        # A declared flag set replaces the derived one wholesale — the knob
        # flags can't be derived (two fixtures' brightness channels differ), so
        # a sidecar that mentions flags is stating the whole truth for them.
        flags = _flags_from_names(override["flags"])

    return ChannelSpec(
        field=slot.key, flags=flags,
        label=channel.name, capabilities=channel.capabilities,
        default_value=channel.default_value, constant=channel.constant,
        precedence=channel.precedence, resolution_bytes=channel.resolution_bytes,
        pixel_key=channel.pixel_key, pixel_group=channel.pixel_group,
        display_name=str(override.get("label", "")),
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
        mode=mode.name,
        manufacturer=manufacturer,
        categories=fixture.categories,
        physical=mode.physical,
        matrix=fixture.matrix,
        wheels=fixture.wheels,
        effects=extras.effects,
        stage=extras.stage,
    )


def color_slots(fdef: FixtureDef) -> Dict[str, int]:
    """OFL colour name -> the first slot index driving it.

    The write-direction index: "which channel do I put red on". Keyed by OFL's
    own colour vocabulary rather than a fixed enum, so a fixture's Amber, UV or
    Lime is as addressable as its Red. Fine bytes are skipped — they carry part
    of their coarse channel's value, not a colour of their own.
    """
    slots: Dict[str, int] = {}
    for index, spec in enumerate(fdef.channels):
        if spec.fine_of >= 0:
            continue
        cap = dominant_capability(spec.capabilities)
        if cap is not None and cap.type == "ColorIntensity":
            colour = str(cap.get("color", ""))
            if colour:
                slots.setdefault(colour, index)
    return slots


def capability_types(fdef: FixtureDef) -> Set[str]:
    """The dominant capability type of every slot — the coarse "what kind of
    light is this" summary (does it mix colour at all, does it dim, …)."""
    types: Set[str] = set()
    for spec in fdef.channels:
        cap = dominant_capability(spec.capabilities)
        if cap is not None:
            types.add(cap.type)
    return types


def all_capability_types(fdef: FixtureDef) -> Set[str]:
    """Every capability type the fixture declares anywhere.

    `capability_types` answers "what is this channel mostly for", which is the
    wrong question when the answer decides whether to build a gobo layer at all:
    a channel that is 90% `Effect` with a strobe at the top of its range really
    does strobe. Switching channels count too — their alternate resolutions are
    things this fixture can do.
    """
    types: Set[str] = set()
    for spec in fdef.channels:
        if spec.fine_of >= 0:
            continue
        for cap in spec.capabilities:
            types.add(cap.type)
        for option in spec.switch_options:
            if option.channel is not None:
                types.update(cap.type for cap in option.channel.capabilities)
    types.discard("NoFunction")
    return types


def color_sources(fdef: FixtureDef) -> Set[str]:
    """How this fixture can be given a colour, if at all.

    The read-direction counterpart of `color_slots`: `"mix"` for additive colour
    channels, `"preset"` for canned colours, `"temperature"` for a CTO/CTB
    channel and `"wheel"` for a physical colour wheel. A fixture with none of
    them emits whatever its lamp emits.
    """
    present = all_capability_types(fdef)
    sources = set()
    if "ColorIntensity" in present:
        sources.add("mix")
    if "ColorPreset" in present:
        sources.add("preset")
    if "ColorTemperature" in present:
        sources.add("temperature")
    if any(slot.type == "Color" for wheel in fdef.wheels.values()
           for slot in wheel.slots):
        sources.add("wheel")
    return sources


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
