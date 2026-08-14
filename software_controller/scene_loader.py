# ABOUTME: Loads scene definitions (banks of per-channel scenes) from any
# ABOUTME: fixture-library def into immutable Scene records (keyed by channel field).

from dataclasses import dataclass
from typing import Dict, List

from core.fixture_library import resolve_fixture


@dataclass(frozen=True)
class Scene:
    """One scene from a fixture def's banks.

    ``values`` maps each of the fixture's channel fields to its raw 0..255
    value (the clip library converts to MIDI velocity at drop time, value//2).
    Works for any fixture type, not just the LED bar.
    """
    fixture_type: str
    bank: str
    index: int
    name: str
    values: Dict[str, int]


def load_scenes(fixture_type: str, mode: str = "") -> List[Scene]:
    """Read the scene banks of a fixture-library type and return a flat list.

    Scenes preserve their bank, position (0-based index), and per-channel
    values. Entries lacking a 'name' key get a default label of
    "{BANK} #{1-based-index}". Scenes missing any of the fixture's channel
    fields, or with all channels zero (reserved placeholder slots), are
    skipped so a single bad row doesn't block the library. Returns [] if the
    type is unknown. `mode` picks which mode's channel set the scenes must
    cover; empty means the type's default.
    """
    fdef = resolve_fixture(fixture_type, mode)
    if fdef is None:
        return []

    fields = [ch.field for ch in fdef.channels]
    scenes: List[Scene] = []
    for bank_name, scene_list in fdef.banks.items():
        if not isinstance(scene_list, list):
            continue
        for index, scene_dict in enumerate(scene_list):
            if not isinstance(scene_dict, dict):
                continue
            if not all(f in scene_dict for f in fields):
                # Skip scenes missing channel data rather than crash.
                continue
            values = {f: int(scene_dict[f]) for f in fields}
            if all(v == 0 for v in values.values()):
                # Skip placeholder scenes with all-zero channels.
                continue
            name = str(scene_dict.get("name") or f"{bank_name} #{index + 1}")
            scenes.append(Scene(fixture_type=fixture_type, bank=str(bank_name),
                                index=index, name=name, values=values))

    return scenes
