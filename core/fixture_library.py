# ABOUTME: Data-driven fixture-type library. Loads fixture *type* definitions from
# ABOUTME: JSON (shipped fixtures/ + user fixtures/) so new types need no Python code.
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
import json
import os

from core.paths import bundle_dir, userprefs_dir
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

VALID_RENDERS = {"par", "bar", "blinder", "generic"}


@dataclass(frozen=True)
class ChannelSpec:
    field: str   # attribute name (also the universe order within the fixture)
    role: int    # semantic role (ROLE_*)
    flags: int   # behaviour flags bitmask


@dataclass(frozen=True)
class FixtureDef:
    name: str            # registry key, e.g. "par_rgb"
    channels: List[ChannelSpec]
    render: str          # "par" | "bar" | "blinder" | "generic"
    banks: Dict[str, List[dict]]  # bank-name -> list of scene dicts (raw, by field)
    chases: List[dict]            # list of {"banks": [...], "scenes": [...]}
    bar_count: int = 1   # physical bars to seed for a "bar" fixture (else 1)

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


def parse_fixture_def(data: dict) -> Optional[FixtureDef]:
    """Parse one fixture-definition dict. Returns None if malformed."""
    if not isinstance(data, dict):
        return None

    name = str(data.get("name", "")).strip()
    if not name:
        return None

    render = str(data.get("render", "generic"))
    if render not in VALID_RENDERS:
        render = "generic"

    channels_data = data.get("channels")
    if not isinstance(channels_data, list) or not channels_data:
        return None

    channels: List[ChannelSpec] = []
    for ch in channels_data:
        if not isinstance(ch, dict):
            return None
        ch_field = str(ch.get("field", "")).strip()
        if not ch_field:
            return None

        role = ch.get("role", "generic")
        if isinstance(role, str):
            role_int = ROLE_NAMES.get(role, ROLE_GENERIC)
        elif isinstance(role, int):
            role_int = role
        else:
            role_int = ROLE_GENERIC

        flags_int = 0
        for flag in ch.get("flags", []):
            if isinstance(flag, str):
                flags_int |= FLAG_NAMES.get(flag, 0)
        channels.append(ChannelSpec(field=ch_field, role=role_int, flags=flags_int))

    # Optional scene/bank/chase data (carried as raw dicts; the software
    # controller turns each scene dict into per-channel values). Tolerant of
    # missing/malformed sections so a partial def still yields valid channels.
    banks_data = data.get("banks")
    banks: Dict[str, List[dict]] = {}
    if isinstance(banks_data, dict):
        for bank_name, scenes in banks_data.items():
            if isinstance(scenes, list):
                banks[str(bank_name)] = [s for s in scenes if isinstance(s, dict)]

    chases_data = data.get("chases")
    chases: List[dict] = [c for c in chases_data if isinstance(c, dict)] \
        if isinstance(chases_data, list) else []

    bar_count = data.get("bar_count", 1)
    if not isinstance(bar_count, int) or isinstance(bar_count, bool) or bar_count < 1:
        bar_count = 1

    return FixtureDef(name=name, channels=channels, render=render,
                      banks=banks, chases=chases, bar_count=bar_count)


def role_index_map(fdef: FixtureDef) -> Dict[int, int]:
    """Map each role to the index of its first channel within the DMX slice."""
    index: Dict[int, int] = {}
    for i, ch in enumerate(fdef.channels):
        index.setdefault(ch.role, i)
    return index


def _load_dir(directory) -> Dict[str, FixtureDef]:
    defs: Dict[str, FixtureDef] = {}
    if not os.path.isdir(directory):
        return defs
    for path in sorted(Path(directory).glob("*.json")):
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Skipping fixture def '{path}': {e}")
            continue
        fdef = parse_fixture_def(data)
        if fdef is None:
            logger.warning(f"Skipping malformed fixture def '{path}'")
            continue
        defs[fdef.name] = fdef
    return defs


_cache: Optional[Dict[str, FixtureDef]] = None


def load_fixture_defs(force_reload: bool = False) -> Dict[str, FixtureDef]:
    """Return {name: FixtureDef} from the shipped then the user fixtures dirs.

    User defs override shipped defs of the same name. The result is memoized;
    pass force_reload=True (or call clear_cache()) after writing new files.
    """
    global _cache
    if _cache is not None and not force_reload:
        return _cache
    defs = _load_dir(fixtures_dir_shipped())
    defs.update(_load_dir(fixtures_dir_user()))
    _cache = defs
    return defs


def clear_cache() -> None:
    """Drop the memoized definitions (used by tests / after an import)."""
    global _cache
    _cache = None


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
