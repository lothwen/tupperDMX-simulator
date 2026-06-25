"""
Characterization test: the per-fixture color palette lifecycle.

The palette used to be a single global on the controller. It now lives on each
Fixture (palette_ptr / palette_index), with is_color_cycling as the
on/off gate so stopping the cycle never loses the palette. This guards that:

  - START stores the palette but does NOT start cycling (waits for a song part),
  - a song part arms cycling on the managed fixture,
  - a manual takeover (note 48) stops cycling but KEEPS the palette, and
  - the next song part re-arms cycling with the retained palette.

No Art-Net / threads are started; the program is driven by handle_note_on.

Run with:
    uv run python tests/test_palette_lifecycle.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from software_controller.main import DMXController

START = 60        # C4 - velocity encodes the palette
CHORUS = 63       # D#4 - a song part the ledbar is managed for (runs a chase)
MANUAL_TAKE = 48  # C3 - takes manual control of the fixture on the channel


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_palette_persists_across_manual_takeover():
    print("test_palette_persists_across_manual_takeover")

    ctrl = DMXController()
    led = ctrl.fixtures[0]
    led.auto = True  # opt the LED into the song-part program

    # START stores a multi-color palette but leaves cycling off.
    ctrl.handle_note_on(0, START, 64)
    _assert(led.palette_ptr and len(led.palette_ptr) > 1,
            "START stores a per-fixture palette")
    _assert(not led.is_color_cycling,
            "START does not start cycling on its own")

    # A song part arms cycling on the managed LED.
    ctrl.handle_note_on(0, CHORUS, 64)
    _assert(led.is_color_cycling, "a song part arms color cycling")
    saved = led.palette_ptr

    # Manual takeover stops cycling but keeps the palette.
    ctrl.handle_note_on(0, MANUAL_TAKE, 64)
    _assert(not led.is_color_cycling, "manual takeover stops cycling")
    _assert(led.palette_ptr is saved, "manual takeover keeps the palette")

    # The next song part re-arms cycling with the retained palette.
    ctrl.handle_note_on(0, CHORUS, 64)
    _assert(led.is_color_cycling and led.palette_ptr is saved,
            "next song part re-arms cycling with the retained palette")


def main():
    test_palette_persists_across_manual_takeover()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
