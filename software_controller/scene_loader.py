# ABOUTME: Loads LED scene definitions (banks of 7-channel scenes) from the
# ABOUTME: fixture-library def (fixtures/ledbar.json) into immutable LedScene records.

from dataclasses import dataclass
from typing import List

from fixture_library import load_fixture_defs


@dataclass(frozen=True)
class LedScene:
    """A single 7-channel LED scene as stored in the ledbar fixture def's banks.

    Channel values are kept as raw 0..255 ints — the clip library converts to
    MIDI velocity at drop time (channel_value // 2, capped at 127).
    """
    bank: str
    index: int
    name: str
    brightness: int
    blue: int
    green: int
    red: int
    animation: int
    speed: int
    direction: int


# Channel keys we expect in each scene dict. Anything else is ignored.
_CHANNEL_KEYS = ("brightness", "blue", "green", "red",
                 "animation", "speed", "direction")


def load_led_scenes(fixture_type: str = "ledbar") -> List[LedScene]:
    """Read the scene banks of a fixture-library type and return a flat list.

    Scenes preserve their bank, position (0-based index), and channel values.
    Entries lacking a 'name' key get a default label of "{BANK} #{1-based-index}".
    Malformed entries (non-dict) are silently skipped so a single bad row
    doesn't block the whole library. Returns [] if the type is unknown.
    """
    fdef = load_fixture_defs().get(fixture_type)
    if fdef is None:
        return []

    scenes: List[LedScene] = []
    for bank_name, scene_list in fdef.banks.items():
        if not isinstance(scene_list, list):
            continue
        for index, scene_dict in enumerate(scene_list):
            if not isinstance(scene_dict, dict):
                continue
            if not all(k in scene_dict for k in _CHANNEL_KEYS):
                # Skip scenes missing channel data rather than crash.
                continue
            if all(int(scene_dict[k]) == 0 for k in _CHANNEL_KEYS):
                # Skip placeholder scenes with all-zero channels — these are
                # reserved slots waiting to be defined.
                continue
            name = str(scene_dict.get("name") or f"{bank_name} #{index + 1}")
            scenes.append(LedScene(
                bank=str(bank_name),
                index=index,
                name=name,
                brightness=int(scene_dict["brightness"]),
                blue=int(scene_dict["blue"]),
                green=int(scene_dict["green"]),
                red=int(scene_dict["red"]),
                animation=int(scene_dict["animation"]),
                speed=int(scene_dict["speed"]),
                direction=int(scene_dict["direction"]),
            ))

    return scenes
