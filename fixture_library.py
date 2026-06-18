# ABOUTME: Data-driven fixture-type library. Loads fixture *type* definitions from
# ABOUTME: JSON (shipped fixtures/ + user fixtures/) so new types need no Python code.
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
import json
import os
import sys

import platformdirs

from tupperlogger import get_logger

logger = get_logger("fixture_library")

_APP_NAME = "tupperDMX"

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

    @property
    def channel_count(self) -> int:
        return len(self.channels)


def fixtures_dir_shipped() -> str:
    """Directory of shipped, read-only fixture definitions (repo-root fixtures/)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "fixtures")


def fixtures_dir_user() -> Path:
    """Writable directory for user-imported fixture definitions.

    Mirrors config.userprefs_dir() (platform-conventional) with a fixtures/
    subfolder. Kept independent of config.py to avoid an import cycle.
    """
    if sys.platform == "win32":
        base = Path(platformdirs.user_data_dir(_APP_NAME, appauthor=False, roaming=True))
    elif sys.platform == "darwin":
        base = Path(platformdirs.user_data_dir(_APP_NAME, appauthor=False))
    else:
        base = Path(platformdirs.user_config_dir(_APP_NAME, appauthor=False))
    return base / "fixtures"


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

    return FixtureDef(name=name, channels=channels, render=render)


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
