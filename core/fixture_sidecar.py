# ABOUTME: Parser for the .tdmx.json sidecar carrying TupperDMX authoring data
# ABOUTME: that the Open Fixture Library format has nowhere to put.
"""The `.tdmx.json` sidecar.

An OFL document describes a *product*: its channels, what each DMX value does,
its physical shape. It has no room for how this app authors a show with it —
scene banks, chases, song-part tags, which channels follow the blend/knob knobs,
which built-in effect maps to which animation asset. The OFL root is
`additionalProperties: false`, so those can't ride inside it either.

They live in a sibling file instead: `fixtures/ledbar.json` stays a pristine OFL
document that any OFL tool can read, and `fixtures/ledbar.tdmx.json` carries the
rest. A fixture dropped in straight from the OFL project works with no sidecar
at all — it simply has no scene banks.

    {
      "tdmx": 1,                    // format version (required)
      "fixture": "ledbar.json",     // sibling OFL file; default "<stem>.json"
      "key": "ledbar",              // registry key; default the OFL "name"
      "default_mode": "7ch",
      "modes": {
        "*":   { ...extras applying to every mode... },
        "7ch": { ...overrides for this mode... }
      }
    }

Each mode block holds `channels` (per-channel `role`/`flags` overrides, keyed by
resolved OFL channel key), `banks`, `chases`, `effects` and `stage`.

**A sidecar can never change the DMX footprint.** Channel count, order and
addressing come only from the OFL document. That is what keeps saved .mid files
— which bind note N to the N-th channel — correct across an edit here.
"""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional

from tupperlogger import get_logger

logger = get_logger("fixture_sidecar")

SIDECAR_SUFFIX = ".tdmx.json"
SIDECAR_VERSION = 1

# The block applying to every mode, before any named-mode overrides.
ANY_MODE = "*"


@dataclass(frozen=True)
class ModeExtras:
    """The authoring data for one fixture type in one mode."""
    channels: Mapping[str, dict] = field(default_factory=lambda: MappingProxyType({}))
    banks: Mapping[str, List[dict]] = field(default_factory=lambda: MappingProxyType({}))
    chases: tuple = ()
    effects: Mapping[str, dict] = field(default_factory=lambda: MappingProxyType({}))
    stage: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


EMPTY_EXTRAS = ModeExtras()


@dataclass(frozen=True)
class Sidecar:
    version: int = SIDECAR_VERSION
    fixture: str = ""          # target OFL filename ("" -> "<stem>.json")
    key: str = ""              # registry key override ("" -> the OFL name)
    default_mode: str = ""
    modes: Mapping[str, ModeExtras] = field(default_factory=lambda: MappingProxyType({}))

    def for_mode(self, mode_name: str) -> ModeExtras:
        """Extras for `mode_name`: the "*" block with the named block layered on.

        `channels` merges per channel key so a named mode can tweak one channel;
        everything else replaces wholesale, since a bank set only makes sense as
        a whole.
        """
        shared = self.modes.get(ANY_MODE)
        named = self.modes.get(mode_name)
        if named is None:
            return shared or EMPTY_EXTRAS
        if shared is None:
            return named
        return ModeExtras(
            channels=MappingProxyType({**shared.channels, **named.channels}),
            banks=named.banks or shared.banks,
            chases=named.chases or shared.chases,
            effects=named.effects or shared.effects,
            stage=named.stage or shared.stage,
        )


def _parse_mode_extras(data: Any) -> ModeExtras:
    if not isinstance(data, dict):
        return EMPTY_EXTRAS

    channels = {str(key): value for key, value in (data.get("channels") or {}).items()
                if isinstance(value, dict)}

    # Bank order is semantic: note 36+n selects the n-th bank and the first one
    # is the fixture's boot bank. json.load preserves insertion order; nothing
    # here may re-sort it.
    banks: Dict[str, List[dict]] = {}
    for name, scenes in (data.get("banks") or {}).items():
        if isinstance(scenes, list):
            banks[str(name)] = [s for s in scenes if isinstance(s, dict)]

    raw_chases = data.get("chases")
    chases = tuple(c for c in raw_chases if isinstance(c, dict)) \
        if isinstance(raw_chases, list) else ()

    effects = {str(name): spec for name, spec in (data.get("effects") or {}).items()
               if isinstance(spec, dict)}
    stage = data.get("stage") if isinstance(data.get("stage"), dict) else {}

    return ModeExtras(
        channels=MappingProxyType(channels),
        banks=MappingProxyType(banks),
        chases=chases,
        effects=MappingProxyType(effects),
        stage=MappingProxyType(dict(stage)),
    )


def parse_sidecar(data: Any, source: str = "") -> Optional[Sidecar]:
    """Parse one `.tdmx.json` document, or None if it isn't usable."""
    where = f" in '{source}'" if source else ""
    if not isinstance(data, dict):
        logger.warning(f"Malformed sidecar{where}: expected a JSON object")
        return None

    version = data.get("tdmx")
    if not isinstance(version, int) or isinstance(version, bool):
        logger.warning(f"Malformed sidecar{where}: missing the 'tdmx' version key")
        return None
    if version > SIDECAR_VERSION:
        logger.warning(f"Ignoring sidecar{where}: format version {version} is newer "
                       f"than the supported {SIDECAR_VERSION}")
        return None

    modes = {str(name): _parse_mode_extras(block)
             for name, block in (data.get("modes") or {}).items()}

    return Sidecar(
        version=version,
        fixture=str(data.get("fixture") or ""),
        key=str(data.get("key") or ""),
        default_mode=str(data.get("default_mode") or ""),
        modes=MappingProxyType(modes),
    )


def target_filename(sidecar_path_name: str, sidecar: Optional[Sidecar]) -> str:
    """The OFL filename a sidecar belongs to.

    Explicit `fixture` wins; otherwise `ledbar.tdmx.json` pairs with
    `ledbar.json`.
    """
    if sidecar is not None and sidecar.fixture:
        return sidecar.fixture
    return sidecar_path_name[:-len(SIDECAR_SUFFIX)] + ".json"
