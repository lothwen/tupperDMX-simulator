"""
Characterization test: the software controller's data-driven patch.

Subtask 2 makes the emulator build its patch from config.fixtures (type +
dmx_start) instead of a hardcoded module global. This guards the refactor:

  1. A controller built from the migrated shipped config produces a patch
     identical to the legacy default (same types, same DMX bases).
  2. Replaying a fixed MIDI sequence on the config-driven controller and on the
     default-fallback controller yields byte-identical present[] buffers — i.e.
     the observable DMX output did not change.

The sequence uses the generalized note map: the note is a verb and the MIDI
channel selects the fixture (0 = ledbar, 1 / 2 = blinders).

No Art-Net / threads are started; DMXController.__init__ only builds buffers.

Run with:
    uv run python tests/test_software_patch.py
"""

import sys
import os
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.config import FixtureInstance, FixturePosition
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
# is stable. (channel, pitch, velocity) — channel selects the fixture.
NOTE_SEQUENCE = [
    (0, 48, 64),   # ch0 ledbar: take manual control
    (0, 1, 100),   # ch0 ledbar: channel offset 1 (brightness)
    (0, 4, 90),    # ch0 ledbar: channel offset 4 (red)
    (0, 72, 0),    # ch0 ledbar: color macro white
    (0, 60, 6),    # ch0 ledbar: START hue_index=1, mode=COMPLEMENTARY (no random)
    (0, 61, 64),   # INTRO song part (global)
    (0, 62, 64),   # VERSE song part (global)
    (0, 64, 64),   # BREAK song part (global -> blinders)
    (1, 24, 80),   # ch1 blinder: manual scene 0
    (2, 24, 80),   # ch2 blinder: manual scene 0
    (0, 50, 0),    # ch0 ledbar: blackout
    (1, 50, 0),    # ch1 blinder: blackout
    (2, 50, 0),    # ch2 blinder: blackout
]


def _replay(controller):
    random.seed(1234)  # deterministic for any incidental random.* use
    for channel, pitch, velocity in NOTE_SEQUENCE:
        controller.handle_note_on(channel, pitch, velocity)
    return list(controller.present)


def test_patch_matches_legacy_default():
    print("test_patch_matches_legacy_default")
    cfg_driven = DMXController(SHIPPED_FIXTURES)
    default = DMXController()  # no patch -> shipped config.json default

    _assert(cfg_driven.num_fixtures == 3, "three fixtures patched from config")
    _assert([f.type for f in cfg_driven.fixtures] ==
            [FIXTURE_TYPES["ledbar"], FIXTURE_TYPES["blinder"], FIXTURE_TYPES["blinder"]],
            "patch types are [ledbar, blinder, blinder]")
    _assert([f.dmx_base for f in cfg_driven.fixtures] == [1, 33, 65],
            "patch DMX bases match the shipped addresses")

    _assert([f.dmx_base for f in cfg_driven.fixtures] == [f.dmx_base for f in default.fixtures],
            "config-driven patch addresses equal the legacy default")


def test_dmx_output_unchanged():
    print("test_dmx_output_unchanged")
    cfg_present = _replay(DMXController(SHIPPED_FIXTURES))
    default_present = _replay(DMXController())
    _assert(cfg_present == default_present,
            "config-driven and default controllers emit identical present[]")


def test_none_defaults_but_explicit_empty_is_empty():
    print("test_none_defaults_but_explicit_empty_is_empty")
    # No patch injected (None) falls back to the shipped default...
    default = DMXController()
    _assert(default.num_fixtures == 3, "no patch (None) falls back to the default")
    _assert([f.dmx_base for f in default.fixtures] == [1, 33, 65],
            "fallback patch matches the shipped addresses")
    # ...but an explicit empty list is honoured (e.g. the user removed every
    # fixture, or repatch([])), so the controller routes nothing.
    empty = DMXController([])
    _assert(empty.num_fixtures == 0, "explicit empty patch yields no fixtures")


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
    test_none_defaults_but_explicit_empty_is_empty()
    test_generic_fixture_inert_and_output_unchanged()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
