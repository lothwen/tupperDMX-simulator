"""
Characterization test: what the stage renderers draw for a given DMX slice.

The safety net for the render-engine generalization. It pins the visible result
— glow colour, glow intensity, cell colour, animation choice — that the three
hand-written render classes produced, and now holds the category/capability-driven
engine to exactly the same numbers. The golden values below were captured from
the old renderers before any of this was rewritten.

Three behaviours here are easy to lose in a rewrite and are called out:

  * the ledbar's channel order is B, G, R (`fixtures/ledbar.json`), so
    `data[1..3]` is blue, green, red. Reading it as RGB silently swaps every
    colour in every show.
  * a blinder is drawn at the level its DMX asks for, like every other fixture:
    a full warm bulb is the (255, 200, 100) tint it mixes from. Pushing the
    stage past full is the exposure's job, not a per-fixture multiplier's.
  * animation DMX maps to a GIF by `value // 4 + 1`, and values >= 140 select a
    number past the 35 shipped GIFs, which falls back to a flat colour fill.

Offscreen Qt + a shared QApplication come from conftest.py (the `qapp` fixture).

Run with:
    uv run python tests/test_stage_render_parity.py
"""

import sys
import os
import glob

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.paths import asset_path
from core.fixture_library import resolve_fixture
from ui.stage import renderer_for

WILD_WASH = "Wild Wash Pro 648 RGBW"


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _rgb(color):
    return (color.red(), color.green(), color.blue())


def _render(type_name, data, **kwargs):
    """Feed a DMX slice to whichever renderer the fixture's categories pick."""
    fdef = resolve_fixture(type_name)
    fixture = renderer_for(fdef)(fdef, 0, 0, **kwargs)
    fixture.update_dmx(bytearray(data))
    return fixture


def _lamp(type_name, data):
    """Render through a single-lamp fixture; return (rgb, intensity)."""
    fixture = _render(type_name, data)
    return _rgb(fixture.lamp.glow_color), fixture.lamp.glow_intensity


# (type, dmx) -> (rgb, intensity)
GOLDEN_GENERIC = {
    ("par_rgb", (255, 10, 20, 30)): ((10, 20, 30), 1.0),
    ("par_rgb", (128, 200, 100, 50)): ((100, 50, 25), 128 / 255),
    ("par_rgb", (0, 255, 255, 255)): ((0, 0, 0), 0.0),
    # No brightness channel -> full intensity; `white` is a cool-white tint.
    (WILD_WASH, (10, 20, 30, 0)): ((10, 20, 30), 1.0),
    (WILD_WASH, (0, 0, 0, 255)): ((200, 220, 255), 1.0),
}

# dmx -> (bulb1 rgb, bulb2 rgb, intensity)
GOLDEN_BLINDER = {
    # Both bulbs warm at full: the warm tint they mix to.
    (255, 0, 0, 255, 255, 0, 0, 0): ((255, 200, 100), (255, 200, 100), 1.0),
    # Top bulb cool only, bottom warm only — the per-bulb split, at half master.
    (128, 255, 0, 0, 255, 0, 0, 0): ((100, 110, 128), (128, 100, 50), 128 / 255),
    # Warm + cool together saturate to white.
    (255, 255, 255, 255, 255, 0, 0, 0): ((255, 255, 255), (255, 255, 255), 1.0),
    # Master brightness at 0 blacks out regardless of the colour channels.
    (0, 255, 255, 255, 255, 0, 0, 0): ((0, 0, 0), (0, 0, 0), 0.0),
}


def test_lamp_fixture_colours(qapp):
    print("test_lamp_fixture_colours")
    for (type_name, data), (rgb, intensity) in GOLDEN_GENERIC.items():
        got_rgb, got_i = _lamp(type_name, data)
        _assert(got_rgb == rgb, f"{type_name} {data} -> glow {rgb}")
        _assert(abs(got_i - min(1.0, intensity)) < 1e-6,
                f"{type_name} {data} -> intensity {min(1.0, intensity):.3f}")


def test_blinder_bulbs(qapp):
    print("test_blinder_bulbs")
    for data, (rgb1, rgb2, intensity) in GOLDEN_BLINDER.items():
        fx = _render("blinder", data)
        _assert(len(fx.bulbs) == 2, "the blinder builds two bulbs")
        _assert(_rgb(fx.bulbs[0].glow_color) == rgb1, f"blinder {data} -> top bulb {rgb1}")
        _assert(_rgb(fx.bulbs[1].glow_color) == rgb2, f"blinder {data} -> bottom bulb {rgb2}")
        _assert(abs(fx.bulbs[0].glow_intensity - intensity) < 1e-6,
                f"blinder {data} -> intensity {intensity:.3f}")


def test_ledbar_channel_order_is_bgr(qapp):
    print("test_ledbar_channel_order_is_bgr")
    # animation 200 -> past the shipped animations -> flat colour fill.
    bar = _render("ledbar", [255, 10, 20, 30, 200, 255, 0])
    try:
        _assert(len(bar.cells) == 40, "the bar builds one cell per matrix row")
        _assert(_rgb(bar.cells[0].brush().color()) == (30, 20, 10),
                "ledbar reads data[1..3] as blue, green, red")
        _assert(_rgb(bar.graphicsEffect().color()) == (30, 20, 10),
                "the drop-shadow glow takes the same colour")
    finally:
        bar.cleanup()


def test_ledbar_brightness_scales_pixels(qapp):
    print("test_ledbar_brightness_scales_pixels")
    bar = _render("ledbar", [128, 10, 20, 30, 240, 255, 0])
    try:
        _assert(_rgb(bar.cells[0].brush().color()) == (15, 10, 5),
                "cell colour scales by brightness/255")
    finally:
        bar.cleanup()


def _gif_for(value):
    """The GIF the animation channel selects at `value`, or None."""
    number = value // 4 + 1
    found = glob.glob(os.path.join(asset_path("animations"), f"{number:02d}-*.gif"))
    return os.path.basename(found[0]) if found else None


def test_animation_dmx_to_gif_mapping(qapp):
    print("test_animation_dmx_to_gif_mapping")
    # Pin the whole 0..255 mapping: 4 DMX steps per animation, 35 shipped GIFs,
    # everything from 140 up falling through to the flat-colour render.
    mapping = {v: _gif_for(v) for v in range(256)}
    _assert(mapping[0] == "01-ON.gif" and mapping[3] == "01-ON.gif",
            "DMX 0-3 -> animation 1")
    _assert(mapping[4] == "02-STROBO.gif", "DMX 4 -> animation 2")
    _assert(mapping[136] == "35-strobo+rain.gif" and mapping[139] == "35-strobo+rain.gif",
            "DMX 136-139 -> animation 35, the last shipped one")
    _assert(all(mapping[v] is None for v in range(140, 256)),
            "DMX 140+ selects no GIF -> flat colour fallback")
    _assert(len({g for g in mapping.values() if g}) == 35,
            "exactly 35 distinct animations are reachable")


def test_ledbar_animation_frames_modulate_colour(qapp):
    print("test_ledbar_animation_frames_modulate_colour")
    # Animation 1 (01-ON.gif) with a pure red DMX colour: the GIF is white, so
    # every lit cell comes out red-only.
    bar = _render("ledbar", [255, 0, 0, 255, 0, 255, 0])
    try:
        _assert(bar.current_movie is not None and bar.current_movie.isValid(),
                "a shipped animation loads a QMovie")
        greens_blues = {(_rgb(c.brush().color())[1], _rgb(c.brush().color())[2])
                        for c in bar.cells}
        _assert(greens_blues == {(0, 0)},
                "animation colour is modulated by the DMX colour (no green/blue leaks)")
    finally:
        bar.cleanup()


def test_each_body_shows_its_own_animation_column(qapp):
    print("test_each_body_shows_its_own_animation_column")
    # The bars share one DMX slice; what distinguishes them is which column of
    # the animation they show. DMX 64 selects 17-radar.gif, whose first frame
    # differs across columns — so the bars must not all look the same.
    bars = [_render("ledbar", [255, 255, 255, 255, 64, 255, 0], body_index=i)
            for i in range(8)]
    try:
        _assert({b.column for b in bars} == set(range(8)),
                "each body reads its own animation column")
        _assert(bars[0].current_asset == "17-radar.gif", "the radar animation loaded")
        columns = [tuple(_rgb(c.brush().color()) for c in b.cells) for b in bars]
        _assert(len(set(columns)) > 1,
                "a spatially-varying animation does not light every bar identically")
    finally:
        for bar in bars:
            bar.cleanup()


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_lamp_fixture_colours(app)
    test_blinder_bulbs(app)
    test_ledbar_channel_order_is_bgr(app)
    test_ledbar_brightness_scales_pixels(app)
    test_animation_dmx_to_gif_mapping(app)
    test_ledbar_animation_frames_modulate_colour(app)
    test_each_body_shows_its_own_animation_column(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
