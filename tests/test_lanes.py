"""
Unit test: the timeline's patch lookups (timeline/lanes.py).

Covers what the lane strip asks about the patch — the header label per
fixture, the row order, and resolving a MIDI channel to its fixture — none of
which needs a widget.

Run with:
    uv run python tests/test_lanes.py
"""

import sys
import os
import subprocess
from dataclasses import dataclass

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from timeline import lanes


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


@dataclass
class Fx:
    type: str
    dmx_start: int = 1
    channel: int = -1
    name: str = ""
    order: int = -1
    mode: str = ""


def test_qt_free():
    print("test_qt_free")
    # The lane strip is a widget; which fixture a lane is remains a question
    # about the patch. Checked in a subprocess, since the test run has already
    # imported Qt into THIS one.
    probe = ("import sys; import timeline.lanes; "
             "sys.exit(1 if 'PyQt6' in sys.modules else 0)")
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT,
                            capture_output=True, text=True)
    _assert(result.returncode == 0,
            f"importing timeline.lanes pulls in no Qt ({result.stderr.strip()})")


def test_labels_name_type_and_ordinals():
    print("test_labels_name_type_and_ordinals")
    labels = lanes.fixture_labels([Fx("ledbar"), Fx("blinder"), Fx("blinder")])
    _assert(labels == ["Ledbar", "Blinder 1", "Blinder 2"],
            f"a repeated type gets 1-based ordinals ({labels})")
    labels = lanes.fixture_labels([Fx("blinder", name="  Stage left  "),
                                   Fx("blinder")])
    # The ordinal counts every fixture of that type in the patch, named ones
    # included — hence "Blinder 1" for the only unnamed blinder of two.
    _assert(labels == ["Stage left", "Blinder 1"],
            f"a named fixture keeps its (stripped) name ({labels})")
    _assert(lanes.fixture_labels([]) == [], "an empty patch has no labels")


def test_display_key_orders_rows_then_dmx():
    print("test_display_key_orders_rows_then_dmx")
    placed = Fx("a", dmx_start=99, order=0)
    unplaced_low = Fx("b", dmx_start=5)
    unplaced_high = Fx("c", dmx_start=40)
    rows = sorted([unplaced_high, unplaced_low, placed], key=lanes.display_key)
    _assert(rows == [placed, unplaced_low, unplaced_high],
            "a dragged row leads; the rest keep DMX order below it")
    _assert(lanes.display_key(Fx("d", dmx_start=7, order=None))
            == lanes.display_key(Fx("d", dmx_start=7)),
            "order None reads as unplaced, like -1")


def test_channel_lookup_and_positional_fallback():
    print("test_channel_lookup_and_positional_fallback")
    patch = [Fx("ledbar", dmx_start=1, channel=4),
             Fx("blinder", dmx_start=33, channel=0)]
    _assert(lanes.fixture_for_channel(patch, 4) is patch[0],
            "a stable channel wins over the row it sits on")
    _assert(lanes.fixture_type(patch, 0) == "blinder", "type by channel")
    _assert(lanes.dmx_start(patch, 4) == 1, "dmx_start by channel")
    _assert(lanes.fixture_for_channel(patch, 9) is None, "an unpatched channel -> None")
    _assert(lanes.fixture_for_channel(patch, None) is None, "no channel -> None")
    _assert(lanes.fixture_type(patch, 9) is None and lanes.dmx_start(patch, 9) == 1,
            "an unpatched channel has no type and the default address")

    synthetic = [Fx("ledbar"), Fx("blinder")]   # built without stable channels
    _assert(lanes.fixture_for_channel(synthetic, 1) is synthetic[1],
            "without a stable channel the display row doubles as one")


def test_fdef_resolves_the_shipped_definition():
    print("test_fdef_resolves_the_shipped_definition")
    patch = [Fx("ledbar", channel=0), Fx("nope-not-a-fixture", channel=1)]
    fdef = lanes.fdef(patch, 0)
    _assert(fdef is not None and fdef.channel_count > 0,
            "a patched type resolves to its definition")
    _assert(lanes.fdef(patch, 1) is None, "an unknown type resolves to None")
    _assert(lanes.fdef(patch, 7) is None, "an unpatched channel resolves to None")


def main():
    test_qt_free()
    test_labels_name_type_and_ordinals()
    test_display_key_orders_rows_then_dmx()
    test_channel_lookup_and_positional_fallback()
    test_fdef_resolves_the_shipped_definition()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
