"""
Characterization test: the light list (ui/stage/light.py).

The engine draws every frame twice — once as 8-bit brushes for the raster
scene, and once as `LightPrimitive`s in linear float for the GPU layer to
accumulate in an HDR buffer. This covers the second one: that each emission
describes what it is emitting, in body-local coordinates, with intensity that
is allowed to exceed 1.0.

The raster path is the reason this exists: it flattens every emission into an
8-bit brush, so two lights on the same patch make mud rather than something
brighter. Here they stay separate, in float, for the HDR buffer to add up.

Deliberately Qt-light and GL-free: no context, no framebuffer, no pixels. What
is asserted is the description of the light, which is the thing that has to be
right before any shader can be.

Run with:
    uv run python tests/test_light_list.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.fixture_library import resolve_fixture
from ui.stage import renderer_for
from ui.stage.emission.cone import HardBeam
from ui.stage.light import (
    BLOB, CONE, LINE, POINT, QUAD, LightList, linear_rgb, linear_to_srgb,
)
import ofl_factory


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _body(fdef, **kwargs):
    return renderer_for(fdef)(fdef, 0.0, 0.0, **kwargs)


def _lights(body, dmx):
    """Drive a body with one DMX frame and return its light list."""
    body.update_dmx(bytes(dmx))
    return body.lights


#: Capability types that mean "turn this channel up to light the fixture".
_LEVEL_CAPABILITIES = frozenset({"Intensity", "ColorIntensity"})


def _lit_frame(fdef):
    """A DMX frame that lights a fixture *steadily*.

    Emphatically not `[255] * channel_count`: on the shipped blinder that puts
    channel 5 into Strobe, so `beam.level` becomes a function of the wall clock
    and any assertion about it passes or fails depending on when the test ran.
    Only the level channels go up; everything else stays at zero.
    """
    return bytes(255 if any(cap.type in _LEVEL_CAPABILITIES
                            for cap in channel.capabilities) else 0
                 for channel in fdef.channels)


# ---------------------------------------------------------------- buffer ---
def test_the_buffer_is_reused_across_frames(qapp):
    print("test_the_buffer_is_reused_across_frames")
    lights = LightList()
    for _ in range(64):
        lights.add_point(0.0, 0.0, (1.0, 1.0, 1.0), 1.0, 4.0)
    size = len(lights._data)
    _assert(len(lights) == 64, "64 primitives written")

    lights.clear()
    _assert(len(lights) == 0 and len(lights.buffer()) == 0,
            "clear() empties the list")
    for _ in range(64):
        lights.add_point(0.0, 0.0, (1.0, 1.0, 1.0), 1.0, 4.0)
    _assert(len(lights._data) == size,
            "and refilling it allocates nothing further")


def test_colour_is_linear_not_srgb(qapp):
    print("test_colour_is_linear_not_srgb")
    _assert(linear_rgb((255, 255, 255)) == (1.0, 1.0, 1.0),
            "white is 1.0 in linear")
    _assert(linear_rgb((0, 0, 0)) == (0.0, 0.0, 0.0), "black is 0.0")
    mid = linear_rgb((128, 128, 128))[0]
    _assert(0.21 < mid < 0.22,
            f"mid grey decodes to ~0.216, not 0.5 (got {mid:.4f})")
    for value in (0, 1, 63, 128, 200, 254, 255):
        back = linear_to_srgb(linear_rgb((value, value, value))[0]) * 255.0
        _assert(abs(back - value) < 0.6, f"sRGB {value} round-trips ({back:.2f})")

    # A matrix cell mixes its colour in float before anything clamps it.
    _assert(linear_rgb((300.0, -5.0, 12.5))[0] == 1.0,
            "out-of-range components clamp rather than raising")


# ------------------------------------------------------------- emissions ---
def test_a_lit_blinder_emits_a_point_per_bulb(qapp):
    """The counterpart to the dark case: glare, one primitive per bulb."""
    print("test_a_lit_blinder_emits_a_point_per_bulb")
    fdef = resolve_fixture("blinder", "")
    if fdef is None:
        print("  skip: no shipped blinder definition")
        return
    body = _body(fdef)
    try:
        points = _lights(body, _lit_frame(fdef)).of_kind(POINT)
        _assert(len(points) == len(body.bulbs),
                f"a lit blinder emits one point per bulb ({len(points)})")
        peak = max(p.intensity for p in points)
        _assert(abs(peak - 1.0) < 1e-4,
                f"at the level its DMX asks for, unclamped ({peak:.2f})")
    finally:
        body.cleanup()


def test_a_dark_fixture_emits_nothing(qapp):
    print("test_a_dark_fixture_emits_nothing")
    fdef = resolve_fixture("blinder", "")
    if fdef is None:
        print("  skip: no shipped blinder definition")
        return
    body = _body(fdef)
    try:
        _assert(len(_lights(body, bytes(fdef.channel_count))) == 0,
                "a fixture at zero puts nothing in the buffer")
    finally:
        body.cleanup()


def test_only_a_fixture_with_optics_throws_a_cone(qapp):
    print("test_only_a_fixture_with_optics_throws_a_cone")
    panel = _body(ofl_factory.fdef(
        categories=("Blinder",),
        channels={"d": ofl_factory.channel("Intensity")}))
    try:
        lights = _lights(panel, [255])
        _assert(not lights.of_kind(CONE),
                "a fixture with no optics throws no shaft")
        _assert(len(lights) > 0, "but it is still emitting light")
    finally:
        panel.cleanup()

    spot = _body(ofl_factory.fdef(
        categories=("Moving Head",),
        channels={
            "d": ofl_factory.channel("Intensity"),
            "g": ofl_factory.channel("GoboWheel", slotNumber=1),
            "z": ofl_factory.channel("Zoom", angleStart="8deg", angleEnd="20deg"),
        }))
    try:
        cones = _lights(spot, [255, 0, 0]).of_kind(CONE)
        _assert(cones, "a fixture with optics does")
        cone = cones[0]
        _assert(cone.length > 0 and cone.half_width > 0,
                f"with real geometry (len {cone.length:.0f}, "
                f"half-width {cone.half_width:.0f})")
        # Which kind of beam this is belongs to the classifier (see
        # test_stage_optics.py); what matters here is that the light list says
        # the same thing the raster emission does.
        expected = 1.0 if isinstance(spot.emission, HardBeam) else 0.0
        _assert(cone.hardness == expected,
                f"and hardness agreeing with the emission drawn "
                f"({type(spot.emission).__name__} -> {cone.hardness})")
    finally:
        spot.cleanup()


def test_a_prism_divides_the_light_it_splits(qapp):
    print("test_a_prism_divides_the_light_it_splits")
    channels = {
        "d": ofl_factory.channel("Intensity"),
        "g": ofl_factory.channel("GoboWheel", slotNumber=1),
    }
    plain = _body(ofl_factory.fdef(categories=("Moving Head",),
                                   channels=dict(channels)))
    prism = _body(ofl_factory.fdef(
        categories=("Moving Head",),
        channels=dict(channels,
                      p=ofl_factory.channel("Prism", angleStart="0deg",
                                            angleEnd="360deg"))))
    try:
        undivided = _lights(plain, [255, 0]).of_kind(CONE)
        _assert(len(undivided) == 1, "no prism channel, one beam")
        base = undivided[0].intensity

        copies = _lights(prism, [255, 0, 255]).of_kind(CONE)
        _assert(len(copies) > 1, f"a prism splits it into {len(copies)}")
        _assert(copies[0].intensity < base,
                f"each copy dimmer than the undivided beam "
                f"({copies[0].intensity:.4f} < {base:.4f})")
        _assert(abs(copies[0].intensity - base / len(copies) ** 0.5) < 1e-4,
                "by 1/sqrt(N) — a prism divides light, it does not make it")
        _assert(sum(c.intensity for c in copies) > base,
                "though the rosette together is brighter than one copy")
        _assert(len({round(c.axis_deg, 4) for c in copies}) == len(copies),
                "and the copies fan out rather than stacking")
    finally:
        plain.cleanup()
        prism.cleanup()


def test_a_matrix_emits_one_quad_per_cell(qapp):
    print("test_a_matrix_emits_one_quad_per_cell")
    fdef = resolve_fixture("ledbar", "")
    if fdef is None:
        print("  skip: no shipped ledbar definition")
        return
    body = _body(fdef)
    try:
        quads = _lights(body, _lit_frame(fdef)).of_kind(QUAD)
        _assert(len(quads) == len(body.emission.cells),
                f"one quad per drawn cell ({len(quads)})")
        _assert(all(q.w > 0 and q.h > 0 for q in quads),
                "each carrying the cell's own size")
    finally:
        body.cleanup()


def test_coordinates_are_body_local(qapp):
    """The layer adds the body's scene position while packing, so a fixture
    dragged in design mode needs a repaint and not a re-render."""
    print("test_coordinates_are_body_local")
    fdef = resolve_fixture("blinder", "")
    if fdef is None:
        print("  skip: no shipped blinder definition")
        return
    here = renderer_for(fdef)(fdef, 0.0, 0.0)
    far = renderer_for(fdef)(fdef, 900.0, 400.0)
    try:
        frame = _lit_frame(fdef)
        a = [(p.x, p.y) for p in _lights(here, frame).primitives()]
        b = [(p.x, p.y) for p in _lights(far, frame).primitives()]
        _assert(a == b,
                "two bodies of one type emit identical coordinates wherever "
                "they stand")
        _assert(here.pos() != far.pos(), "even though their positions differ")
    finally:
        here.cleanup()
        far.cleanup()


def test_every_category_can_be_asked_for_its_light(qapp):
    """The registry claims all 17 OFL categories; none may explode here."""
    print("test_every_category_can_be_asked_for_its_light")
    from ui.stage.registry import registered_categories
    categories = registered_categories()
    checked = 0
    for category in sorted(categories):
        fdef = ofl_factory.fdef(
            categories=(category,),
            channels={"d": ofl_factory.channel("Intensity")})
        body = _body(fdef)
        try:
            for level in (0, 128, 255):
                body.update_dmx(bytes([level] * max(1, fdef.channel_count)))
                for primitive in body.lights.primitives():
                    _assert_finite(primitive, category)
            checked += 1
        finally:
            body.cleanup()
    _assert(checked == len(categories),
            f"all {checked} registered categories describe their light")


def _assert_finite(primitive, category):
    import math
    for name, value in zip(primitive._fields, primitive):
        values = value if isinstance(value, tuple) else (value,)
        for one in values:
            if not math.isfinite(one):
                raise AssertionError(
                    f"{category}: {name} is not finite ({one})")
    if primitive.intensity < 0.0:
        raise AssertionError(f"{category}: negative intensity")


def test_a_laser_emits_lines_and_a_hazer_blobs(qapp):
    print("test_a_laser_emits_lines_and_a_hazer_blobs")
    laser = _body(ofl_factory.fdef(
        categories=("Laser",),
        channels={"d": ofl_factory.channel("Intensity")}))
    try:
        lines = _lights(laser, [255]).of_kind(LINE)
        _assert(lines, f"a lit laser emits a fan of lines ({len(lines)})")
        _assert(len({round(l.axis_deg, 3) for l in lines}) == len(lines),
                "each on its own axis")
    finally:
        laser.cleanup()

    hazer = _body(ofl_factory.fdef(
        categories=("Hazer",),
        channels={"f": ofl_factory.channel("Fog")}))
    try:
        blobs = _lights(hazer, [255]).of_kind(BLOB)
        _assert(blobs, f"a running hazer emits fog blobs ({len(blobs)})")
        _assert(all(b.intensity < 0.5 for b in blobs),
                "fog scatters light rather than emitting it, so it stays dim")
    finally:
        hazer.cleanup()


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_the_buffer_is_reused_across_frames(app)
    test_colour_is_linear_not_srgb(app)
    test_a_lit_blinder_emits_a_point_per_bulb(app)
    test_a_dark_fixture_emits_nothing(app)
    test_only_a_fixture_with_optics_throws_a_cone(app)
    test_a_prism_divides_the_light_it_splits(app)
    test_a_matrix_emits_one_quad_per_cell(app)
    test_coordinates_are_body_local(app)
    test_every_category_can_be_asked_for_its_light(app)
    test_a_laser_emits_lines_and_a_hazer_blobs(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
