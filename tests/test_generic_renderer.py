"""
Characterization test: the data-driven GenericFixture renderer.

Verifies that GenericFixture maps role-tagged DMX channels onto its glow colour:
RGB is picked from the right slice indices, brightness scales the result, and a
fixture without a brightness channel glows at full. Offscreen Qt + a shared
QApplication come from conftest.py (the `qapp` fixture).

Run with:
    uv run python tests/test_generic_renderer.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fixture_library import parse_fixture_def, role_index_map, ROLE_BRIGHTNESS


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


PAR_RGB = {
    "name": "par_rgb",
    "render": "par",
    "channels": [
        {"field": "brightness", "role": "brightness", "flags": ["blend"]},
        {"field": "red", "role": "red", "flags": ["blend"]},
        {"field": "green", "role": "green", "flags": ["blend"]},
        {"field": "blue", "role": "blue", "flags": ["blend"]},
    ],
}


def _make(fdef):
    from fixtures import GenericFixture
    return GenericFixture(0, 0, fdef.channel_count, role_index_map(fdef))


def test_rgb_full_brightness(qapp):
    print("test_rgb_full_brightness")
    fx = _make(parse_fixture_def(PAR_RGB))
    fx.update_dmx(bytearray([255, 10, 20, 30]))
    c = fx.bulb.glow_color
    _assert((c.red(), c.green(), c.blue()) == (10, 20, 30),
            "RGB picked from the right slice indices at full brightness")


def test_brightness_scales(qapp):
    print("test_brightness_scales")
    fx = _make(parse_fixture_def(PAR_RGB))
    fx.update_dmx(bytearray([128, 200, 100, 50]))
    c = fx.bulb.glow_color
    _assert(c.red() == int(200 * 128 / 255), "red scaled by brightness")
    _assert(c.green() == int(100 * 128 / 255), "green scaled by brightness")
    _assert(c.blue() == int(50 * 128 / 255), "blue scaled by brightness")


def test_no_brightness_defaults_full(qapp):
    print("test_no_brightness_defaults_full")
    rgb_only = {
        "name": "rgb", "render": "par",
        "channels": [
            {"field": "red", "role": "red"},
            {"field": "green", "role": "green"},
            {"field": "blue", "role": "blue"},
        ],
    }
    fdef = parse_fixture_def(rgb_only)
    _assert(ROLE_BRIGHTNESS not in role_index_map(fdef), "fixture has no brightness channel")
    fx = _make(fdef)
    fx.update_dmx(bytearray([60, 70, 80]))
    c = fx.bulb.glow_color
    _assert((c.red(), c.green(), c.blue()) == (60, 70, 80),
            "RGB shown at full when no brightness channel is present")


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_rgb_full_brightness(app)
    test_brightness_scales(app)
    test_no_brightness_defaults_full(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
