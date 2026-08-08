"""
Characterization test: the light list (stage/light.py).

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
from stage import puffs, renderer_for
from stage.emission.cone import BeamFan, HardBeam, lens_glow_radii
from stage.glow import DIFFUSE_GLOW, SHARP_GLOW
from stage.frame import FORESHORTEN_FLOOR
from stage.light import (
    BLOB, CONE, LINE, POINT, QUAD, STRIDE, LightList, linear_rgb,
    linear_to_srgb,
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


def test_a_moving_heads_lens_flares_where_a_par_washes(qapp):
    """A head's lamp is the aperture of its beam, not a light beside it.

    Given a par's halo as well, a head reads as a beam with a wash bolted to
    it — and on this path especially, where a POINT is spread over sixty lamp
    radii and lights the air around the fixture whatever its beam is doing. So
    the lens keeps a fraction of it, and grows that fraction back as the beam
    turns down the lens: aimed at you, a beam really does put its whole output
    in your eye, and there is still only one beam doing it.

    Scoped to the profile, not to the emission: the same `WashLamp` on a par is
    a lamp in a housing and keeps its glow, which the last part asserts.
    """
    print("test_a_moving_heads_lens_flares_where_a_par_washes")
    # The ramp itself. A head halfway round is still a beam seen from the side,
    # so most of the bloom has to be left for the end of the turn — linear, a
    # head tilted halfway wore a bigger halo than the flat one this replaced.
    rest, half, full = (lens_glow_radii(1.0),
                        lens_glow_radii((1.0 + FORESHORTEN_FLOOR) / 2.0),
                        lens_glow_radii(FORESHORTEN_FLOOR))
    _assert(rest < half < full,
            f"the lens grows as the beam turns toward the viewer "
            f"({rest:.2f} -> {half:.2f} -> {full:.2f})")
    _assert(full - half > half - rest,
            "and most of that growth is in the last part of the turn")

    channels = {"d": ofl_factory.channel("Intensity"),
                "r": ofl_factory.channel("ColorIntensity", color="Red"),
                "t": ofl_factory.channel("Tilt", angleStart="0deg",
                                         angleEnd="270deg"),
                "z": ofl_factory.channel("Zoom", angleStart="10deg",
                                         angleEnd="40deg")}
    head = _body(ofl_factory.fdef(categories=("Moving Head", "Color Changer"),
                                  channels=dict(channels)))
    par = _body(ofl_factory.fdef(categories=("Color Changer",),
                                 channels=dict(channels)))
    try:
        # Tilt centred: the beam crosses the picture, side-on to the viewer.
        rest = _lights(head, [255, 255, 128, 0])
        _assert(rest.of_kind(CONE), "a lit head throws a beam")
        lens = rest.of_kind(POINT)
        _assert(len(lens) == 1, "and one flare at the lens, no more")

        washed = _lights(par, [255, 255, 128, 0]).of_kind(POINT)
        _assert(lens[0].radius < washed[0].radius / 2.0,
                f"a fraction of what the same channels glow in a par "
                f"({lens[0].radius:.1f} vs {washed[0].radius:.1f})")

        # A lens is small enough that the classic falloff's shoulder reads as
        # the rim of a disc, so it fades on the diffuse curve instead. Only it:
        # every other lamp on the stage keeps the falloff it always had.
        _assert(head.emission.lamp.glow_profile is DIFFUSE_GLOW,
                "a lens fades diffusely, with no radius the eye can find")
        _assert(par.emission.lamp.glow_profile is SHARP_GLOW
                and SHARP_GLOW == ((0.0, 180), (0.13, 130), (0.25, 90),
                                   (0.5, 40), (1.0, 0)),
                "while every other lamp keeps the stage's classic falloff")

        # Aimed straight down the lens. Set on the frame rather than driven
        # through the tilt channel because a head *slews*: it would arrive
        # several seconds after the one DMX frame a test hands it.
        frame = head._frame
        frame.beam.foreshorten = FORESHORTEN_FLOOR
        bloom = LightList()
        head.emission.emit_lights(frame, bloom)
        aimed = bloom.of_kind(POINT)
        # By the ramp, rather than by a multiple of its own: what is wired here
        # is that this path reads `lens_glow_radii` for the frame's aim, and the
        # ramp's shape is already pinned at the top of this test. Where its two
        # ends sit is a look decision — a number here only restates them, and
        # goes red the next time the lens is tuned.
        grew = aimed[0].radius / lens[0].radius
        ramp = lens_glow_radii(FORESHORTEN_FLOOR) / lens_glow_radii(1.0)
        _assert(abs(grew - ramp) < 0.01,
                f"blooming as the beam turns down the lens "
                f"({aimed[0].radius:.1f} from {lens[0].radius:.1f}, x{grew:.2f} "
                f"against the ramp's x{ramp:.2f})")

        _assert(len(_lights(head, [0, 0, 128, 0])) == 0,
                "and nothing at all from a head that is switched off")
    finally:
        head.cleanup()
        par.cleanup()


def test_a_flowers_lens_is_its_rosette_not_a_wash(qapp):
    """A flower makes beams, so its lamp is the optic they leave through.

    The same rule as the head above, on the emission that draws a rosette:
    everything a moonflower puts out goes down those beams, so a full halo at
    the lens is a second light the fixture does not have — and on this path it
    is spread over sixty lamp radii, which washes the room a derby never
    washes.
    """
    print("test_a_flowers_lens_is_its_rosette_not_a_wash")
    channels = {"d": ofl_factory.channel("Intensity"),
                "r": ofl_factory.channel("ColorIntensity", color="Red")}
    flower = _body(ofl_factory.fdef(categories=("Flower",),
                                    channels=dict(channels)))
    par = _body(ofl_factory.fdef(categories=("Color Changer",),
                                 channels=dict(channels)))
    try:
        lit = _lights(flower, [255, 255])
        _assert(len(lit.of_kind(CONE)) == BeamFan.BEAMS,
                f"a lit flower throws its {BeamFan.BEAMS} beams")

        lens = lit.of_kind(POINT)
        _assert(len(lens) == 1, "and one flare at the lens, no more")
        washed = _lights(par, [255, 255]).of_kind(POINT)
        _assert(lens[0].radius < washed[0].radius / 2.0,
                f"a fraction of what the same channels glow in a par "
                f"({lens[0].radius:.1f} vs {washed[0].radius:.1f})")

        _assert(flower.emission.lamp.glow_profile is DIFFUSE_GLOW,
                "and it fades diffusely on the raster path, like any lens")
    finally:
        flower.cleanup()
        par.cleanup()


def test_a_prism_divides_the_light_it_splits(qapp):
    print("test_a_prism_divides_the_light_it_splits")
    channels = {
        "d": ofl_factory.channel("Intensity"),
        "g": ofl_factory.channel("GoboWheel", slotNumber=1),
    }
    # Both declare the same lens: the control has to throw a beam for the prism
    # to be compared against, and a head with no optics declared anywhere
    # resolves to no shaft at all (see `test_stage_optics.py`).
    lens = {"lens": {"degreesMinMax": [25, 25]}}
    plain = _body(ofl_factory.fdef(categories=("Moving Head",),
                                   channels=dict(channels), physical=lens))
    prism = _body(ofl_factory.fdef(
        categories=("Moving Head",),
        channels=dict(channels,
                      p=ofl_factory.channel("Prism", angleStart="0deg",
                                            angleEnd="360deg")),
        physical=lens))
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
    from stage.registry import registered_categories
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
        # A blob is MEDIUM, not light: `intensity` is a density and the fog pass
        # integrates other fixtures' beams through it. It is never a light
        # source, so even a hazer at full sits far below a lit lamp.
        #
        # Capped at the NOZZLE density rather than the settled one: a parcel
        # leaves the machine `NOZZLE_DENSITY` times thicker than it ends up and
        # mixes down to `spec.density` over the first breath of its life, so
        # what a fresh blob carries is the product of the two.
        # Slack for the array: a light list is float32, and a parcel this fresh
        # sits exactly ON the ceiling, so it reads back a hair above it.
        ceiling = puffs.HAZER.density * puffs.NOZZLE_DENSITY
        _assert(all(0.0 < b.density <= ceiling * (1.0 + 1e-5) for b in blobs),
                f"each carries a density, capped by what a hazer can make "
                f"({ceiling:.2f} at the nozzle, settling to "
                f"{puffs.HAZER.density})")
        _assert(all(b.radius > 0.0 and b.z_extent > 0.0 for b in blobs),
                "and an extent in the picture and in depth")
        # And what it LOOKS like, which the density map needs and used not to
        # get: rasterised as the clean Gaussian a blob used to be, a bank of
        # smoke on the GPU path was a pile of spheres, which is why it read
        # worse than the raster plume it was meant to improve on.
        _assert(all(0.0 <= b.variant < puffs.PARCEL_VARIANTS for b in blobs),
                f"each wearing one of the {puffs.PARCEL_VARIANTS} shapes the "
                "sprite sheet also deals from")
        _assert(len({round(b.spin, 4) for b in blobs}) == len(blobs),
                "and each turned to its own angle, so no two are the same "
                "stamp on either path")
    finally:
        hazer.cleanup()

    # The same channel on a smoke machine is a different medium entirely.
    smoke = _body(ofl_factory.fdef(
        categories=("Smoke",),
        channels={"f": ofl_factory.channel("Fog")}))
    try:
        thick = _lights(smoke, [255]).of_kind(BLOB)
        _assert(thick, f"a running smoke machine emits parcels too ({len(thick)})")
        _assert(max(b.density for b in thick) > max(b.density for b in blobs),
                "and they are denser than a hazer's at the same DMX")
    finally:
        smoke.cleanup()


def test_a_laser_is_never_drawn_thinner_than_a_pixel(qapp):
    """A laser is the one primitive thinner than the pixel that draws it.

    `LASER_HALF_WIDTH_PX` is a couple of scene units, and the view fits a
    1536-unit stage into a few hundred device pixels — so the quad the light
    pass builds for a beam comes out under a pixel wide. GL has no coverage: it
    shades the pixels whose centres land inside the triangle and drops the rest,
    which turns a beam at any angle into a dashed line and a fan into the fan of
    dashes this was reported as.

    So the vertex shader widens the quad to `MIN_LINE_HALF_PX` device pixels and
    hands back the brightness it gained by covering more of them — which is what
    an antialiased thin line is, and what Qt has been doing for the raster
    path's 2px pen all along. This mirrors that arithmetic and pins both halves:
    widen without dimming and the fan is a bright smear; dim without widening
    and it is still dashed.

    Source-level, because CI cannot compile a shader, let alone rasterise one.
    """
    print("test_a_laser_is_never_drawn_thinner_than_a_pixel")
    import re

    from stage.emission.volumetric import LASER_HALF_WIDTH_PX
    from stage.gpu import shaders
    from stage.gpu.layer import _px_per_scene

    def const(name):
        match = re.search(rf"const float {name} = (-?[0-9.]+);",
                          shaders.LIGHT_VERT)
        _assert(match is not None, f"{name} is still in the light shader")
        return float(match.group(1))

    min_half_px = const("MIN_LINE_HALF_PX")
    _assert(min_half_px >= 0.5,
            f"the floor is {min_half_px} device px — under half a pixel and a "
            "diagonal beam can still fall between pixel centres")

    def drawn(px_per_scene):
        """LIGHT_VERT's KIND_LINE branch: the half width it draws, and the
        brightness it keeps."""
        width = max(LASER_HALF_WIDTH_PX, 0.5)
        half = max(width, min_half_px / max(px_per_scene, 1e-4))
        return half, width / half

    # The scale the screenshot was taken at: a 1536-unit stage in a 465px view.
    scale = _px_per_scene(_fit_matrix(465, 1536), 465)
    half, dim = drawn(scale)
    _assert(half * scale >= min_half_px - 1e-6,
            f"at {scale:.2f} px per scene unit a beam is drawn "
            f"{2 * half * scale:.2f} px wide, not {2 * LASER_HALF_WIDTH_PX * scale:.2f}")
    _assert(dim < 1.0 and abs(dim * half - LASER_HALF_WIDTH_PX) < 1e-6,
            f"and gives back exactly what it gained ({dim:.2f}), so the fan "
            "carries the same light it always did")

    # Zoomed in far enough that the beam is genuinely wider than a pixel, the
    # floor must get out of the way entirely.
    half, dim = drawn(10.0)
    _assert(half == LASER_HALF_WIDTH_PX and dim == 1.0,
            "and a beam already wider than a pixel is left alone")

    # The fragment shader has to measure against that same half width. It used
    # to divide by 0.35, which lit a fifth of the quad it was handed.
    body = shaders.LIGHT_FRAG.split("v_kind == KIND_LINE")[1]
    _assert("float across = abs(v_local.y);" in body,
            "the beam's profile is measured in its own half widths, so what is "
            "drawn is the width the vertex shader just guaranteed")


def _fit_matrix(view_px: int, scene_units: int):
    """`StageView._scene_to_clip` for a scene fitted into a square-ish view."""
    from PyQt6.QtGui import QMatrix4x4
    scale = view_px / scene_units
    matrix = QMatrix4x4()
    matrix.translate(-1.0, 1.0)
    matrix.scale(2.0 / view_px, -2.0 / view_px)
    matrix.scale(scale, scale)
    return matrix


def test_smoke_alone_still_reaches_the_gpu(qapp):
    print("test_smoke_alone_still_reaches_the_gpu")
    # The lights and the parcels of medium ride in ONE instance buffer — the
    # media packed after the lights, which is why the medium pass can share the
    # light pass's upload. The upload used to BE the light pass's, and behind
    # its `count == 0` early return: a stage whose only emitter is a fog machine
    # has no lights at all, so nothing was ever uploaded and the medium pass
    # then drew a hundred and fifty instances out of a buffer that had never
    # been allocated. Reading off the end of a vertex buffer is undefined, and
    # on the driver this was found on it took the process down — a hazer running
    # through a blackout was enough.
    #
    # GL-free: what is pinned is that the upload does not depend on there being
    # any light, which is the part that was wrong.
    import ctypes

    from stage.gpu.layer import LightLayer

    class _Recorder:
        def __init__(self):
            self.allocated = []

        def bind(self):
            pass

        def release(self):
            pass

        def allocate(self, payload, size):
            self.allocated.append(size)

    layer = LightLayer(lambda: [])
    layer._instances = _Recorder()
    # A hazer's worth of parcels and not one light.
    layer._packed = (ctypes.c_float * (STRIDE * 3))(*([0.0] * (STRIDE * 3)))
    layer._light_count = 0
    layer._medium_count = 3
    layer._upload_instances()
    _assert(layer._instances.allocated == [STRIDE * 3 * 4],
            f"a stage of nothing but smoke still uploads its parcels "
            f"({layer._instances.allocated})")

    # And nothing at all uploads nothing, rather than an empty buffer.
    layer._instances = _Recorder()
    layer._packed = None
    layer._upload_instances()
    _assert(layer._instances.allocated == [],
            "while a stage with nothing on it uploads nothing")


def test_the_fog_stops_asking_for_frames_once_the_rig_stands_still(qapp):
    print("test_the_fog_stops_asking_for_frames_once_the_rig_stands_still")
    # The march dithers each ray's start and averages a few frames, so after the
    # picture stops changing it needs a handful more to settle — and it is the
    # only thing on the stage that asks for a repaint it was not given a reason
    # for. Which makes it the only thing that can spin: resetting the countdown
    # every frame the march RAN, rather than every frame the picture CHANGED,
    # means it never reaches zero and the stage redraws flat out for as long as
    # fog is switched on. GL-free — none of this needs a context.
    import ctypes

    from core.config import StageFogConfig
    from stage.gpu.fog import CONVERGE_FRAMES
    from stage.gpu.layer import LightLayer

    layer = LightLayer(lambda: [])
    layer.fog = StageFogConfig(enabled=True)
    layer._fog = object()                 # stand in for a compiled fog pass
    layer._packed = (ctypes.c_float * 2)(1.0, 2.0)
    layer._light_count = 1

    asked = []
    layer.request_repaint = lambda: asked.append(1)

    def frame():
        signature = layer._frame_signature()
        changed = signature != layer._last_signature
        layer._last_signature = signature
        layer._settle(changed)

    frame()
    _assert(len(asked) == 1, "the first frame starts the countdown")
    for _ in range(40):
        frame()
    _assert(len(asked) == CONVERGE_FRAMES,
            f"a rig standing still goes quiet after {CONVERGE_FRAMES} frames, "
            f"not forever ({len(asked)} over 41)")

    # Something moved: a parcel of smoke drifting is a genuinely different
    # picture, so the fog is redrawn and the countdown starts again.
    asked.clear()
    layer._packed = (ctypes.c_float * 2)(1.0, 9.0)
    frame()
    _assert(len(asked) == 1, "and starts again when the picture changes")
    _assert(layer._converge == CONVERGE_FRAMES - 1,
            "with a full countdown to settle into")


def test_drifting_air_is_a_moving_picture(qapp):
    print("test_drifting_air_is_a_moving_picture")
    # The other half of the same invariant. The march's noise flows with the
    # clock, so textured air genuinely looks different every frame with the rig
    # standing perfectly still — and `_frame_signature` cannot notice, because
    # time is deliberately not in it (putting it there would make `_settle`
    # reset its own countdown every frame, which is the spin that countdown
    # exists to stop). So the layer answers the question instead, and the
    # stage's 30 Hz clock is what acts on the answer.
    #
    # What matters is that every way of having nothing to animate says no.
    import ctypes

    from core.config import StageFogConfig
    from stage.gpu.layer import LightLayer

    layer = LightLayer(lambda: [])
    layer._fog = object()                 # stand in for a compiled fog pass
    layer._packed = (ctypes.c_float * 2)(1.0, 2.0)
    layer._light_count = 1
    layer.haze = 0.4

    layer.fog = StageFogConfig(enabled=True)
    _assert(layer.fog.texture > 0.0, "the shipped air is ragged")
    _assert(layer.air_is_drifting(),
            "textured air with a beam in it is a moving picture")

    layer.fog = StageFogConfig(enabled=True, texture=0.0)
    _assert(not layer.air_is_drifting(),
            "smooth air is not: `turbulence()` returns a constant, so there is "
            "nothing in the frame that time changes")

    layer.fog = StageFogConfig(enabled=False)
    _assert(not layer.air_is_drifting(),
            "and neither is a stage with the march switched off")

    # Nothing lit, nothing in the air and no room light to show it with: the
    # march draws an empty frame, and an empty frame does not move.
    layer.fog = StageFogConfig(enabled=True, light_intensity=0.0)
    layer._light_count = 0
    layer._medium_count = 0
    _assert(not layer.air_is_drifting(),
            "nor a stage where the march would draw nothing at all")

    # A hazer running in the dark, though, is exactly when the drift reads.
    layer._medium_count = 3
    _assert(layer.air_is_drifting(),
            "but smoke on a dark stage still drifts")


def test_the_fog_is_marched_without_the_stage_clip(qapp):
    print("test_the_fog_is_marched_without_the_stage_clip")
    # The clip confining the light to the stage image is a box in SURFACE
    # pixels, and every target the fog renders into is a different size — the
    # march is half the surface, the medium map an eighth of the scene. GL
    # scissors in absolute framebuffer pixels, so left enabled it cuts each of
    # them at the same absolute column: the march loses everything left of the
    # letterbox bar (a hard vertical line across the stage, fog only to the
    # right of it) and the medium map, all of ~192 texels wide, is missed
    # entirely by a clip that starts past it — so a hazer at full puts no smoke
    # on stage at all. Both were reported; they are the same bug.
    #
    # GL-free: a recording stand-in for the version functions is enough, since
    # what has to be true is about the ORDER of the state changes.
    import ctypes

    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QMatrix4x4

    from core.config import StageFogConfig
    from stage.gpu.layer import LightLayer, _SCISSOR_TEST

    calls = []

    class FakeGL:
        def glEnable(self, cap):
            calls.append(("enable", cap))

        def glDisable(self, cap):
            calls.append(("disable", cap))

        def glScissor(self, x, y, w, h):
            calls.append(("scissor", x, y, w, h))

    class FakeFog:
        def render(self, **kwargs):
            calls.append(("march",))
            return 7          # a texture name; the layer only truth-tests it

    layer = LightLayer(lambda: [])
    layer.fog = StageFogConfig(enabled=True)
    layer._fns = FakeGL()
    layer._fog = FakeFog()
    layer._packed = (ctypes.c_float * 2)(1.0, 2.0)
    layer._light_count = 1

    # A stage letterboxed with bars either side — which is what puts `clip.x`
    # past the medium map's whole width.
    clip = QRect(355, 0, 964, 643)
    layer._set_clip(clip, 643)
    layer._march_fog(QMatrix4x4(), 1674, 643, clip)

    kinds = [c[0] for c in calls]
    _assert("march" in kinds, "the march runs")
    marched = kinds.index("march")
    _assert(("disable", _SCISSOR_TEST) in calls[:marched],
            "with the stage clip switched off, so neither the half-size march "
            "nor the 192-texel medium map is cut at a surface-pixel column")
    _assert(("enable", _SCISSOR_TEST) in calls[marched:],
            "and back on afterwards, because the resolve is what confines the "
            "fog to the stage image")
    _assert(("scissor", 355, 0, 964, 643) in calls[marched:],
            "restored to the same box, not a stale or wider one")
    _assert(layer._fog_texture == 7, "and the march's result is what resolves")


def test_a_dark_stage_still_has_air_in_it(qapp):
    print("test_a_dark_stage_still_has_air_in_it")
    # `count == 0` is not the same question as "is there anything to draw".
    # The march's ambient pass lights the room's own baseline haze, and that is
    # stage-wide — it needs no fixture and no parcel of smoke. Returning early
    # on an empty light list is why the haze vanished the instant the last beam
    # went out and the last puff aged away, which is the one moment a hazy room
    # should read as most obviously hazy.
    #
    # GL-free: the draw calls are stubbed out, because what has to be right is
    # the decision to make them.
    from PyQt6.QtGui import QMatrix4x4

    from core.config import StageFogConfig
    from stage.gpu.layer import LightLayer

    def frame(**kwargs):
        layer = LightLayer(lambda: [])          # no bodies: nothing emitting
        layer._fns = object()                   # already "initialised"
        layer._fog = object()                   # stand in for a compiled pass
        for name, value in kwargs.items():
            setattr(layer, name, value)
        drawn = []
        layer._ensure_fbo = lambda w, h: None
        layer._set_clip = lambda clip, h: None
        layer._restore = lambda w, h: None
        layer._accumulate = lambda m, c: drawn.append(("accumulate", c))
        layer._march_fog = lambda m, w, h, clip: drawn.append(("march",))
        layer._resolve = lambda w, h: drawn.append(("resolve",))
        layer.render(QMatrix4x4(), 800, 600, clip=None)
        return drawn

    hazy = frame(fog=StageFogConfig(enabled=True), haze=0.4)
    _assert(("march",) in hazy,
            "a dark stage with haze in the room still marches the fog")
    _assert(("resolve",) in hazy, "and resolves it over the scene")
    _assert(("accumulate", 0) in hazy,
            "clearing the HDR buffer on the way, or the frame would keep the "
            "last lit one's light for as long as the fog kept it drawing")

    _assert(frame(fog=StageFogConfig(enabled=True), haze=0.0) == [],
            "clean air and a dark rig really is an empty frame")
    _assert(frame(fog=StageFogConfig(enabled=True, light_intensity=0.0),
                  haze=0.4) == [],
            "and so is haze with nothing lighting it — the ambient pass is "
            "what makes the air visible on its own")
    _assert(frame(fog=StageFogConfig(enabled=False), haze=0.4) == [],
            "with the march off there is nothing to draw the air with at all")


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_a_dark_stage_still_has_air_in_it(app)
    test_the_fog_is_marched_without_the_stage_clip(app)
    test_smoke_alone_still_reaches_the_gpu(app)
    test_the_fog_stops_asking_for_frames_once_the_rig_stands_still(app)
    test_drifting_air_is_a_moving_picture(app)
    test_the_buffer_is_reused_across_frames(app)
    test_colour_is_linear_not_srgb(app)
    test_a_lit_blinder_emits_a_point_per_bulb(app)
    test_a_dark_fixture_emits_nothing(app)
    test_only_a_fixture_with_optics_throws_a_cone(app)
    test_a_moving_heads_lens_flares_where_a_par_washes(app)
    test_a_flowers_lens_is_its_rosette_not_a_wash(app)
    test_a_prism_divides_the_light_it_splits(app)
    test_a_matrix_emits_one_quad_per_cell(app)
    test_coordinates_are_body_local(app)
    test_every_category_can_be_asked_for_its_light(app)
    test_a_laser_emits_lines_and_a_hazer_blobs(app)
    test_a_laser_is_never_drawn_thinner_than_a_pixel(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
