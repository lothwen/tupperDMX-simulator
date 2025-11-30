# DMX data structures
import json
import os
from dataclasses import dataclass

@dataclass
class LedScene:
    brightness: int = 255
    blue: int = 255
    green: int = 255
    red: int = 255
    animation: int = 0
    speed: int = 255
    direction: int = 0

@dataclass
class BlinderScene:
    brightness: int = 255
    cool1: int = 255
    cool2: int = 255
    warm1: int = 255
    warm2: int = 255
    strobe: int = 0
    mode: int = 0
    speed: int = 0

def load_dmx_config():
    script_dir = os.path.dirname(os.path.realpath(__file__))
    config_path = os.path.join(script_dir, 'dmx_config.json')
    with open(config_path, 'r') as f:
        config = json.load(f)

    led_banks = {bank_name: [LedScene(**scene) for scene in scenes] for bank_name, scenes in config['led_banks'].items()}
    blinder_banks = {bank_name: [BlinderScene(**scene) for scene in scenes] for bank_name, scenes in config['blinder_banks'].items()}
    
    return led_banks, blinder_banks, config['led_chases'], config['blinder_chases']

led_banks, blinder_banks, led_chases, blinder_chases = load_dmx_config()
