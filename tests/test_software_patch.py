"""
Characterization test: the software controller's data-driven patch.

Subtask 2 makes the emulator build its patch from config.fixtures (type +
dmx_start) instead of a hardcoded module global. This guards the refactor:

  1. A controller built from the migrated shipped config produces a patch
     identical to the legacy default (same types, same DMX bases, same LED /
     blinder1 / blinder2 handles).
  2. Replaying a fixed MIDI sequence on the config-driven controller and on the
     default-fallback controller yields byte-identical present[] buffers — i.e.
     the observable DMX output did not change.

No Art-Net / threads are started; DMXController.__init__ only builds buffers.

Run with:
    uv run python tests/test_software_patch.py
"""

import sys
import os
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import FixtureInstance, FixturePosition
from software_controller.main import (
    DMXController,
    FIXTURE_TYPES,
)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


# Mirrors the shipped config.json fixture list.
SHIPPED_FIXTURES = [
    FixtureInstance(type="ledbar", dmx_start=1,
                    bars=[FixturePosition(x=0, y=0)]),
    FixtureInstance(type="blinder", dmx_start=33, x=575, y=500),
    FixtureInstance(type="blinder", dmx_start=65, x=925, y=500),
]

# A deterministic note sequence touching the LED bar + both blinders. Velocities
# and song parts are chosen to avoid the random.* code paths so the comparison
# is stable. (channel, pitch, velocity)
NOTE_SEQUENCE = [
    (0, 5, 64),    # animation -> takes manual LED control
    (0, 1, 100),   # LED brightness
    (0, 4, 90),    # LED red
    (0, 20, 0),    # manual solid color (white)
    (0, 40, 6),    # START: hue_index=1, mode=COMPLEMENTARY (no random)
    (0, 41, 64),   # INTRO song part
    (0, 42, 64),   # VERSE song part
    (0, 44, 64),   # BREAK song part (both blinders)
    (0, 26, 80),   # manual blinder scene (both blinders)
    (0, 50, 0),    # blackout LED
    (0, 51, 0),    # blackout blinders
]


def _replay(controller):
    random.seed(1234)  # deterministic for any incidental random.* use
    for channel, pitch, velocity in NOTE_SEQUENCE:
        controller.handle_note_on(channel, pitch, velocity)
    return list(controller.present)


def test_patch_matches_legacy_default():
    print("test_patch_matches_legacy_default")
    cfg_driven = DMXController(SHIPPED_FIXTURES)
    default = DMXController()  # hardcoded fallback patch

    _assert(cfg_driven.num_fixtures == 3, "three fixtures patched from config")
    _assert([p.type for p in cfg_driven.patch] ==
            [FIXTURE_TYPES["ledbar"], FIXTURE_TYPES["blinder"], FIXTURE_TYPES["blinder"]],
            "patch types are [ledbar, blinder, blinder]")
    _assert([p.dmx_base for p in cfg_driven.patch] == [1, 33, 65],
            "patch DMX bases match the shipped addresses")
    _assert((cfg_driven.led, cfg_driven.blinder1, cfg_driven.blinder2) == (0, 1, 2),
            "LED / blinder handles resolve to 0 / 1 / 2")

    _assert([p.dmx_base for p in cfg_driven.patch] == [p.dmx_base for p in default.patch],
            "config-driven patch addresses equal the legacy default")


def test_dmx_output_unchanged():
    print("test_dmx_output_unchanged")
    cfg_present = _replay(DMXController(SHIPPED_FIXTURES))
    default_present = _replay(DMXController())
    _assert(cfg_present == default_present,
            "config-driven and default controllers emit identical present[]")


def test_empty_fixtures_falls_back_to_default():
    print("test_empty_fixtures_falls_back_to_default")
    fallback = DMXController([])
    _assert(fallback.num_fixtures == 3, "empty fixture list falls back to default patch")
    _assert((fallback.led, fallback.blinder1, fallback.blinder2) == (0, 1, 2),
            "fallback handles resolve to 0 / 1 / 2")


def test_generic_fixture_inert_and_output_unchanged():
    print("test_generic_fixture_inert_and_output_unchanged")
    # A library-defined generic type (par_rgb, 4ch) patched alongside the
    # built-ins must build without raising (synthetic default bank) and, since
    # the note sequence never addresses it, leave the full present[] unchanged.
    with_generic = SHIPPED_FIXTURES + [FixtureInstance(type="par_rgb", dmx_start=9)]
    ctrl = DMXController(with_generic)
    _assert(ctrl.num_fixtures == 4, "generic fixture added to the patch")

    generic_present = _replay(DMXController(with_generic))
    baseline_present = _replay(DMXController(SHIPPED_FIXTURES))
    _assert(generic_present == baseline_present,
            "adding an inert generic fixture leaves present[] unchanged")


def main():
    test_patch_matches_legacy_default()
    test_dmx_output_unchanged()
    test_empty_fixtures_falls_back_to_default()
    test_generic_fixture_inert_and_output_unchanged()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
