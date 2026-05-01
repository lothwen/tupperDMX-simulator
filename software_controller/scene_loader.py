# ABOUTME: Loads LED scene definitions (banks of 7-channel scenes) from
# ABOUTME: dmx_config.json into immutable LedScene records for the clip library.

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Union


@dataclass(frozen=True)
class LedScene:
    """A single 7-channel LED scene as stored in dmx_config.json's led_banks.

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


def load_led_scenes(config_path: Union[str, Path]) -> List[LedScene]:
    """Read led_banks from a dmx_config.json file and return a flat list.

    Scenes preserve their bank, position (0-based index), and channel values.
    Entries lacking a 'name' key get a default label of "{BANK} #{1-based-index}".
    Malformed entries (non-dict) are silently skipped so a single bad row
    doesn't block the whole library.
    """
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    led_banks = data.get("led_banks", {})
    if not isinstance(led_banks, dict):
        return []

    scenes: List[LedScene] = []
    for bank_name, scene_list in led_banks.items():
        if not isinstance(scene_list, list):
            continue
        for index, scene_dict in enumerate(scene_list):
            if not isinstance(scene_dict, dict):
                continue
            if not all(k in scene_dict for k in _CHANNEL_KEYS):
                # Skip scenes missing channel data rather than crash.
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
