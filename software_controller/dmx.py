# DMX data structures
import json
import os
from dataclasses import dataclass, fields

@dataclass
class LedScene:
    brightness: int = 0
    blue: int = 0
    green: int = 0
    red: int = 0
    animation: int = 0
    speed: int = 0
    direction: int = 0

@dataclass
class BlinderScene:
    brightness: int = 0
    cool1: int = 0
    cool2: int = 0
    warm1: int = 0
    warm2: int = 0
    strobe: int = 0
    mode: int = 0
    speed: int = 0

def load_dmx_config():
    script_dir = os.path.dirname(os.path.realpath(__file__))
    config_path = os.path.join(script_dir, 'dmx_config.json')
    with open(config_path, 'r') as f:
        config = json.load(f)

    def _only_fields(cls, scene):
        # Drop metadata keys (e.g. "name") that aren't dataclass channels.
        allowed = {f.name for f in fields(cls)}
        return {k: v for k, v in scene.items() if k in allowed}

    led_banks = {bank_name: [LedScene(**_only_fields(LedScene, scene)) for scene in scenes] for bank_name, scenes in config['led_banks'].items()}
    blinder_banks = {bank_name: [BlinderScene(**_only_fields(BlinderScene, scene)) for scene in scenes] for bank_name, scenes in config['blinder_banks'].items()}
    
    return led_banks, blinder_banks, config['led_chases'], config['blinder_chases']

led_banks, blinder_banks, led_chases, blinder_chases = load_dmx_config()
