"""
Characterization test: the volumetric fog's maths, without a GPU.

CI cannot run the shader — the offscreen platform plugin refuses to create an
OpenGL context at all — so this does what `test_tonemap.py` does for the
tonemap: mirror the maths in Python, pin its *properties*, and then read the
GLSL source and assert the two still agree. The mirror is not the shader, but a
change to either that the other did not get will fail here.

What is pinned:

* Henyey-Greenstein is a real phase function — it integrates to 1 over the
  sphere, `g = 0` is isotropic, `g > 0` leans forward. Get this wrong and a
  beam aimed at the audience is no brighter than one crossing the stage, which
  is the single most recognisable thing about a beam in haze.
* Beer-Lambert extinction: monotone, bounded, and 1 in clean air.
* Exponential height fog thins upward, because haze settles.
* The renderer's own normalisation: the shader divides the phase by its
  broadside value, so `scattering = 1` means "as bright as the beam always
  looked" rather than a physical unit nothing else in this app uses.
* The foreshortening constant the fog march inverts is the one `cone_geometry`
  applied, so the marched shaft ends where the drawn one does.

Run with:
    uv run python tests/test_fog_math.py
"""

import math
import os
import random
import re
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.config import StageFogConfig
from stage.frame import FORESHORTEN_FLOOR
from stage.gpu import shaders
from stage.gpu.fog import FLOW_PX_S


def _fail_if(cond, msg):
    if cond:
        raise AssertionError(msg)


def _ok(msg):
    print(f"  ok: {msg}")


def _glsl_const(name: str) -> float:
    """A `const float` out of the fog shader, so the mirror below cannot be
    tuned independently of the thing it is a mirror of."""
    match = re.search(rf"const float {name} = (-?[0-9.]+);", shaders.FOG_FRAG)
    _fail_if(match is None, f"{name} is gone from the fog shader")
    return float(match.group(1))


FINE_RATIO = _glsl_const("FINE_RATIO")
FINE_WARP = _glsl_const("FINE_WARP")
FINE_DRIFT = _glsl_const("FINE_DRIFT")
COARSE_WEIGHT = _glsl_const("COARSE_WEIGHT")
TEXTURE_CONTRAST = _glsl_const("TEXTURE_CONTRAST")


# ---------------------------------------------------------------------------
# The mirror — this is FOG_FRAG's maths, in Python
# ---------------------------------------------------------------------------

def henyey_greenstein(cos_theta: float, g: float) -> float:
    gg = g * g
    d = 1.0 + gg - 2.0 * g * cos_theta
    return (1.0 - gg) / (4.0 * math.pi * max(d, 1e-4) ** 1.5)


def scatter_gain(cos_theta: float, g: float) -> float:
    """The phase function with its broadside value divided out."""
    return henyey_greenstein(cos_theta, g) / henyey_greenstein(0.0, g)


def transmittance(sigma: float, distance: float) -> float:
    """Beer-Lambert: what is left of a ray after `distance` of medium."""
    return math.exp(-sigma * distance)


def height_fog(density: float, height: float, scale: float,
               falloff: float) -> float:
    return density * math.exp(-falloff * max(0.0, height) / max(scale, 1.0))


def slant_length(drawn_length: float, foreshorten: float) -> float:
    """The throw a fixture has, from the length the light list drew."""
    clamped = min(1.0, max(0.0, foreshorten))
    return drawn_length / max(
        FORESHORTEN_FLOOR + (1.0 - FORESHORTEN_FLOOR) * clamped, 0.05)


# ---------------------------------------------------------------------------
# The air's own structure — `turbulence()`, mirrored
# ---------------------------------------------------------------------------

def _fract(x: float) -> float:
    """GLSL `fract`: x - floor(x), so negatives land in [0, 1) too."""
    return x - math.floor(x)


def hash31(point) -> float:
    p = [_fract(v * 0.3183099 + offset) * 17.0
         for v, offset in zip(point, (0.71, 0.113, 0.419))]
    return _fract(p[0] * p[1] * p[2] * (p[0] + p[1] + p[2]))


def value_noise(point) -> float:
    lattice = [math.floor(v) for v in point]
    f = [v - i for v, i in zip(point, lattice)]
    f = [v * v * (3.0 - 2.0 * v) for v in f]

    def corner(dx, dy, dz):
        return hash31((lattice[0] + dx, lattice[1] + dy, lattice[2] + dz))

    def mix(a, b, t):
        return a + (b - a) * t

    return mix(
        mix(mix(corner(0, 0, 0), corner(1, 0, 0), f[0]),
            mix(corner(0, 1, 0), corner(1, 1, 0), f[0]), f[1]),
        mix(mix(corner(0, 0, 1), corner(1, 0, 1), f[0]),
            mix(corner(0, 1, 1), corner(1, 1, 1), f[0]), f[1]), f[2])


def turbulence(point, texture: float, scale: float = 260.0,
               seconds: float = 0.0) -> float:
    """How much thicker or thinner the air is here than the smooth field says."""
    if texture <= 0.0:
        return 1.0
    inverse = 1.0 / scale
    drift = [f * seconds * inverse for f in FLOW_PX_S]
    q = [p * inverse + d for p, d in zip(point, drift)]
    coarse = value_noise(q)
    warp = (coarse, coarse * 0.73, coarse * 1.31)
    r = [q[i] * FINE_RATIO + warp[i] * FINE_WARP + drift[i] * FINE_DRIFT + 19.3
         for i in range(3)]
    fine = value_noise(r)
    n = fine + (coarse - fine) * COARSE_WEIGHT
    s = min(1.0, max(-1.0, (n - 0.5) * TEXTURE_CONTRAST))
    return max(0.0, 1.0 + texture * s)


def _air_samples(texture: float, seconds: float = 0.0, count: int = 4000):
    """A spread of points through a stage-sized volume."""
    rng = random.Random(20260805)
    return [turbulence((rng.uniform(-1600.0, 1600.0),
                        rng.uniform(-1000.0, 1000.0),
                        rng.uniform(-700.0, 700.0)), texture, seconds=seconds)
            for _ in range(count)]


# ---------------------------------------------------------------------------
# Phase function
# ---------------------------------------------------------------------------

def test_the_phase_function_integrates_to_one():
    print("test_the_phase_function_integrates_to_one")
    # A phase function redistributes light; it must not create or destroy any.
    # Integrated over the sphere with dOmega = 2*pi*sin(theta) dtheta.
    steps = 20000
    for g in (0.0, 0.3, 0.55, 0.8, -0.4):
        total = 0.0
        for i in range(steps):
            theta = (i + 0.5) * math.pi / steps
            total += (henyey_greenstein(math.cos(theta), g)
                      * 2.0 * math.pi * math.sin(theta) * math.pi / steps)
        _fail_if(abs(total - 1.0) > 1e-3,
                 f"HG with g={g} integrates to {total:.5f}, not 1")
        _ok(f"g={g:+.2f} integrates to {total:.5f}")


def test_zero_g_is_isotropic():
    print("test_zero_g_is_isotropic")
    values = [henyey_greenstein(math.cos(a), 0.0)
              for a in (0.0, 0.7, 1.6, 2.4, math.pi)]
    _fail_if(max(values) - min(values) > 1e-9,
             "g=0 must scatter the same in every direction")
    _ok(f"every direction gets {values[0]:.6f} (= 1/4pi)")


def test_forward_scattering_leans_forward():
    print("test_forward_scattering_leans_forward")
    # This is the one that matters on stage: a beam pointing at you puts its
    # whole output into your eye, and the same beam crossing the picture only
    # shows you what it scatters sideways.
    for g in (0.3, 0.55, 0.8):
        head_on = scatter_gain(1.0, g)
        broadside = scatter_gain(0.0, g)
        away = scatter_gain(-1.0, g)
        _fail_if(not (head_on > broadside > away),
                 f"g={g} must fall off from head-on to away "
                 f"({head_on:.2f}, {broadside:.2f}, {away:.2f})")
        _fail_if(head_on < 2.0,
                 f"g={g} barely favours forward ({head_on:.2f}x) — a beam aimed "
                 "at the audience has to be dramatically brighter")
        _ok(f"g={g}: head-on {head_on:6.2f}x, broadside 1.00x, away {away:.2f}x")

    _fail_if(scatter_gain(1.0, -0.5) >= scatter_gain(-1.0, -0.5),
             "a negative g must lean backward instead")
    _ok("a negative g leans backward")


def test_broadside_is_the_unit():
    print("test_broadside_is_the_unit")
    # The renderer's normalisation, and the reason `scattering = 1.0` is a
    # sensible default rather than an arbitrary one.
    for g in (0.0, 0.3, 0.55, 0.8, -0.4):
        _fail_if(abs(scatter_gain(0.0, g) - 1.0) > 1e-9,
                 f"a beam across the picture must scatter 1.0 at g={g}")
    _ok("a beam crossing the picture scatters exactly 1.0, at every g")


def test_the_phase_function_survives_its_singularity():
    print("test_the_phase_function_survives_its_singularity")
    # g = 1 makes the denominator zero at head-on. The config clamps to +-0.95,
    # but the shader must not produce an infinity even if something else does.
    for g in (0.949, 0.95, 0.99):
        value = henyey_greenstein(1.0, g)
        _fail_if(value != value or value == float("inf"),
                 f"g={g} head-on produced {value}")
    _ok("extreme g stays finite (the max(d, 1e-4) floor holds)")


# ---------------------------------------------------------------------------
# Extinction and the density field
# ---------------------------------------------------------------------------

def test_extinction_is_beer_lambert():
    print("test_extinction_is_beer_lambert")
    _fail_if(abs(transmittance(0.0, 5000.0) - 1.0) > 1e-12,
             "clean air must not dim anything")
    _ok("clean air transmits everything")

    previous = 1.0
    for distance in range(0, 2000, 100):
        value = transmittance(0.002, distance)
        _fail_if(value > previous + 1e-12, "transmittance must never rise")
        _fail_if(value < 0.0, "and never go negative")
        previous = value
    _ok(f"monotone down to {previous:.4f} over 2000 units")

    # Marching in two halves must equal marching in one go, or the step count
    # would change how thick the air looks.
    whole = transmittance(0.003, 800.0)
    halves = transmittance(0.003, 400.0) * transmittance(0.003, 400.0)
    _fail_if(abs(whole - halves) > 1e-12,
             "splitting a march must not change the result")
    _ok("splitting the march changes nothing (exp is multiplicative)")


def test_height_fog_thins_upward():
    print("test_height_fog_thins_upward")
    previous = None
    for height in (0, 100, 300, 600, 1200):
        value = height_fog(0.5, height, 520.0, 1.6)
        if previous is not None:
            _fail_if(value >= previous, f"fog must thin with height ({height})")
        previous = value
    _ok(f"0.5 at the floor falls to {previous:.4f} at 1200 units up")

    _fail_if(abs(height_fog(0.5, 0.0, 520.0, 1.6) - 0.5) > 1e-12,
             "at the floor it is simply the density asked for")
    _ok("and is exactly the asked-for density at the floor")

    # A falloff of zero is a slab of even density — legal, and the only way to
    # get one, so it must not be optimised away.
    flat = [height_fog(0.5, h, 520.0, 0.0) for h in (0, 500, 1000)]
    _fail_if(max(flat) - min(flat) > 1e-12,
             "falloff 0 must give an even slab")
    _ok("falloff 0 gives an even slab")


def test_the_slant_length_inverts_what_the_cone_applied():
    print("test_the_slant_length_inverts_what_the_cone_applied")
    from stage.emission.cone import _foreshortened

    for foreshorten in (0.0, 0.15, 0.5, 0.85, 1.0):
        throw = 320.0
        drawn = throw * _foreshortened(foreshorten)
        recovered = slant_length(drawn, foreshorten)
        _fail_if(abs(recovered - throw) > 1e-6,
                 f"foreshorten={foreshorten}: recovered {recovered:.2f}, "
                 f"not the {throw} the fixture actually throws")
    _ok("the march recovers the fixture's real throw at every angle")

    _fail_if(abs(slant_length(320.0, 1.0) - 320.0) > 1e-9,
             "a beam across the picture is drawn at its full length")
    _ok("and a beam across the picture is unchanged")


# ---------------------------------------------------------------------------
# The mirror against the shader
# ---------------------------------------------------------------------------

def test_the_air_is_ragged_enough_to_see():
    print("test_the_air_is_ragged_enough_to_see")
    # The reported problem: the texture control "did nothing much". It was not
    # doing nothing — value noise is a weighted average of eight lattice
    # hashes, so it piles up around its own middle and two octaves of it swing
    # about a fifth either side of the smooth field. Modulating a density by
    # ±20% reads as a faint dirtying of a flat sheet, not as billows, however
    # far the slider is pushed. `TEXTURE_CONTRAST` is what stretches it out
    # first, and this is the property that has to survive: at the shipped
    # setting, the air has to be nearly empty in places and much thicker in
    # others, because that contrast IS the visible structure.
    samples = sorted(_air_samples(StageFogConfig().texture))
    count = len(samples)
    thin = samples[count // 20]
    thick = samples[19 * count // 20]

    _fail_if(thin > 0.35,
             f"the thin side of the air only reaches {thin:.2f} of the smooth "
             f"field — pockets that never come close to clearing read as a "
             f"uniform veil, which is the look this control exists to break")
    _fail_if(thick < 1.6,
             f"and the thick side only reaches {thick:.2f} — with nothing "
             f"standing out against the veil there are no billows to see")
    _ok(f"at the shipped texture the air runs {thin:.2f}..{thick:.2f} "
        f"of the smooth field")

    # And the control has somewhere left to go: the exaggerated end is what
    # separates billows into banks with genuinely clear air between them.
    empty = sum(1 for v in _air_samples(3.0) if v <= 0.0) / count
    _fail_if(empty < 0.1,
             f"at full texture only {empty:.1%} of the air is actually clear — "
             f"the top of the slider has to be smoke in banks, not just more "
             f"of the same haze")
    _ok(f"and at full texture {empty:.0%} of it is clear air")


def test_raggedness_is_not_a_second_density_control():
    print("test_raggedness_is_not_a_second_density_control")
    # `stage_haze` is meant to be the only control over how much fog there is;
    # this one is only about how it is distributed. The clamp at zero does bias
    # the average upward — there is no such thing as less than no fog — so the
    # invariant is not exact, and this is where its size is held. If a change to
    # the noise makes the bias big, the two sliders have started overlapping.
    for texture in (0.0, 0.4, 1.0, StageFogConfig().texture, 2.0, 3.0):
        mean = statistics.fmean(_air_samples(texture))
        _fail_if(abs(mean - 1.0) > 0.2,
                 f"texture {texture} multiplies the average density by "
                 f"{mean:.3f} — turning the air ragged is quietly turning the "
                 f"fog up or down with it")
    _ok("the average density holds across the whole range of the control")


def test_the_air_moves():
    print("test_the_air_moves")
    # Haze in a room is not still, and a texture that does not move is a dirty
    # lens rather than air. Two things have to be true, and only the first is
    # about the shader: the field at a fixed point has to be materially
    # different a second later.
    texture = StageFogConfig().texture
    still = _air_samples(texture, seconds=0.0)
    later = _air_samples(texture, seconds=1.0)
    moved = statistics.fmean(abs(a - b) for a, b in zip(still, later))
    _fail_if(moved < 0.15,
             f"a second of drift changes the air by {moved:.3f} of the smooth "
             f"field — at that rate the fog is indistinguishable from a still "
             f"texture over any span anyone watches")
    _ok(f"a second of drift moves the air by {moved:.2f}")

    # The other half is that the two octaves travel at different speeds, so the
    # field CHURNS rather than sliding past as one rigid pattern. Undo the
    # bulk translation and there must still be something left changing.
    dx, dy, dz = (f * 1.0 for f in FLOW_PX_S)
    rng = random.Random(11)
    residual = []
    for _ in range(2000):
        point = (rng.uniform(-1600.0, 1600.0), rng.uniform(-1000.0, 1000.0),
                 rng.uniform(-700.0, 700.0))
        # The same parcel of air, followed along its own drift.
        followed = (point[0] - dx, point[1] - dy, point[2] - dz)
        residual.append(abs(turbulence(point, texture, seconds=0.0)
                            - turbulence(followed, texture, seconds=1.0)))
    _fail_if(statistics.fmean(residual) < 0.05,
             "following the air's own drift cancels the change exactly, so the "
             "whole field is one rigid pattern sliding behind the picture — "
             "which reads as a moving texture rather than as moving air")
    _ok("and it churns as it travels, rather than sliding rigidly")


def _glsl_body(source: str, name: str) -> str:
    match = re.search(rf"float {name}\([^)]*\)\s*\{{(.*?)\n\}}", source, re.S)
    _fail_if(match is None, f"{name}() is gone from the shader")
    return match.group(1)


def test_the_mirror_matches_the_shader():
    print("test_the_mirror_matches_the_shader")
    source = shaders.FOG_FRAG

    # The phase function, term by term. A sign or an exponent slipping here is
    # invisible until someone notices beams look flat.
    body = _glsl_body(source, "henyey_greenstein")
    for fragment in ("float gg = g * g;",
                     "1.0 + gg - 2.0 * g * cos_theta",
                     "(1.0 - gg) / (4.0 * PI * pow(max(d, 1e-4), 1.5))"):
        _fail_if(fragment not in body,
                 f"henyey_greenstein no longer contains `{fragment}` — the "
                 "Python mirror in this file is now describing something else")
    _ok("Henyey-Greenstein matches the mirror term for term")

    gain = _glsl_body(source, "scatter_gain")
    _fail_if("henyey_greenstein(0.0, g)" not in gain,
             "scatter_gain must divide by the BROADSIDE value; anything else "
             "changes what `scattering = 1.0` means")
    _ok("scatter_gain still normalises on broadside")

    _fail_if("transmittance *= exp(-sigma * dt);" not in source,
             "the march no longer applies Beer-Lambert extinction")
    _ok("the march applies Beer-Lambert per step")

    # The air's structure, which the three tests above measure through a mirror
    # of exactly these lines.
    body = _glsl_body(source, "turbulence")
    for fragment, why in (
        ("clamp((n - 0.5) * TEXTURE_CONTRAST, -1.0, 1.0)",
         "the stretch is what makes the texture control visible at all"),
        ("vec3 drift = u_flow * u_time * u_noise_scale;",
         "the air has to drift, or it is a dirty lens"),
        ("drift * FINE_DRIFT", "and its octaves have to shear past each other"),
        ("mix(value_noise(r), coarse, COARSE_WEIGHT)",
         "the fine octave is warped by the coarse one"),
        ("max(0.0, 1.0 + u_texture * s)",
         "and a pocket of air can be empty but never negative"),
    ):
        _fail_if(fragment not in body,
                 f"turbulence() no longer contains `{fragment}` — {why}, and "
                 f"the Python mirror in this file now describes something else")
    _ok("turbulence matches the mirror line for line")

    _fail_if("exp(-u_height_falloff * height)" not in source,
             "the height fog is no longer exponential")
    _ok("height fog is exponential in the shader too")

    # PI, spelled out in the shader, has to be PI.
    match = re.search(r"const float PI = ([0-9.]+);", shaders.FOG_VERT)
    _fail_if(match is None, "PI is gone")
    _fail_if(abs(float(match.group(1)) - math.pi) > 1e-6,
             f"PI in the shader is {match.group(1)}")
    _ok(f"PI is {match.group(1)}")


def test_the_foreshorten_floor_is_shared():
    print("test_the_foreshorten_floor_is_shared")
    match = re.search(r"const float FORESHORTEN_FLOOR = ([0-9.]+);",
                      shaders.FOG_VERT)
    _fail_if(match is None, "FORESHORTEN_FLOOR is not in the shader")
    _fail_if(abs(float(match.group(1)) - FORESHORTEN_FLOOR) > 1e-9,
             f"the shader says {match.group(1)}, stage.frame says "
             f"{FORESHORTEN_FLOOR} — the marched shaft would end somewhere the "
             "drawn one does not")
    _ok(f"both stages of the fog program get {FORESHORTEN_FLOOR} from Python")

    _fail_if("float slant_length(float drawn_length, float foreshorten)"
             not in shaders.FOG_VERT,
             "slant_length must be shared by both shader stages — the vertex "
             "shader sizes a beam's bound with it and the fragment marches it")
    _ok("and both stages share one slant_length()")


def test_the_march_terminates_early():
    print("test_the_march_terminates_early")
    # Not decoration: without these a rig of small fixtures marches the whole
    # slab per light, and the pass stops being affordable.
    source = shaders.FOG_FRAG
    _fail_if("if (transmittance < 0.01) { break; }" not in source,
             "the march no longer stops once nothing behind it can be seen")
    _fail_if("if (density <= 1e-4) { continue; }" not in source,
             "the march no longer skips clean air")
    _ok("early-out and empty-air skip are still there")

    _fail_if("bool cone_segment(" not in source,
             "the march no longer solves the cone it is marching")
    _fail_if("half_chord = reach * reach - dot(offset, offset)" not in source,
             "the march no longer clips to the light's bounding sphere")
    _ok("and the segment is solved rather than swept")


# ---------------------------------------------------------------------------
# The segment — the reason any of this is visible
# ---------------------------------------------------------------------------

def cone_segment(scene, camera_z, apex, axis, k):
    """FOG_FRAG's `cone_segment`, mirrored. Returns (enter, leave) or None."""
    o = (scene[0] - apex[0], scene[1] - apex[1], -camera_z - apex[2])
    kk = 1.0 + k * k
    along = sum(a * b for a, b in zip(o, axis))
    a = 1.0 - kk * axis[2] * axis[2]
    b = 2.0 * (o[2] - kk * along * axis[2])
    c = sum(v * v for v in o) - kk * along * along
    if a <= 1e-4:
        return None
    disc = b * b - 4.0 * a * c
    if disc <= 0.0:
        return None
    root = math.sqrt(disc)
    return ((-b - root) / (2.0 * a), (-b + root) / (2.0 * a))


def test_every_sample_lands_inside_the_beam():
    print("test_every_sample_lands_inside_the_beam")
    # This is the bug that made the whole feature invisible, so it is the one
    # most worth pinning. A beam is a thin sheet in depth — a 25 degree par is
    # about 66 units thick at its widest — and a march spread evenly over a
    # 1400-unit slab steps 58 units at a time, so TWO samples of twenty-four
    # landed in the cone and the shaft never appeared. Solving the cone puts
    # every sample inside it.
    camera_z, steps = 770.0, 24
    apex = (276.0, 250.0, 0.0)
    axis = (0.0, 1.0, 0.0)          # straight down the picture
    slant, half_width = 320.0, 70.9
    k = half_width / slant

    scene = (apex[0], apex[1] + 150.0)      # 150 units down the shaft
    segment = cone_segment(scene, camera_z, apex, axis, k)
    _fail_if(segment is None, "the ray through the middle of a shaft must hit it")
    enter, leave = segment

    inside = 0
    for i in range(steps):
        t = enter + (i + 0.5) * (leave - enter) / steps
        point = (scene[0], scene[1], -camera_z + t)
        offset = tuple(p - a for p, a in zip(point, apex))
        along = sum(a * b for a, b in zip(offset, axis))
        distance = math.sqrt(sum(v * v for v in offset))
        across = math.sqrt(max(0.0, distance * distance - along * along))
        if 0.0 < along <= slant and across <= k * along + 1e-6:
            inside += 1
    _fail_if(inside < steps,
             f"only {inside}/{steps} samples landed in the cone — the shaft "
             "will be undersampled into invisibility again")
    _ok(f"all {steps} samples land inside the cone")

    # And the segment really is the cone's thickness, not the slab's.
    thickness = leave - enter
    expected = 2.0 * math.sqrt(max(0.0, (k * 150.0) ** 2))
    _fail_if(abs(thickness - expected) > 1.0,
             f"segment is {thickness:.1f} units, but the cone is {expected:.1f} "
             "thick there")
    _ok(f"and the segment is the cone's own {thickness:.1f} units, not 1400")

    # A ray that misses the cone entirely must say so rather than march.
    _fail_if(cone_segment((apex[0] + 900.0, apex[1] + 150.0), camera_z, apex,
                          axis, k) is not None,
             "a ray well outside the cone must report a miss")
    _ok("a ray that misses the cone reports a miss")


def test_a_beam_aimed_at_the_viewer_falls_back():
    print("test_a_beam_aimed_at_the_viewer_falls_back")
    # When the ray lies inside the cone's own opening angle the quadratic
    # degenerates. The shader detects that and keeps the bounding sphere, which
    # is a perfectly good answer; what it must not do is divide by nothing.
    apex = (400.0, 300.0, 0.0)
    axis = (0.0, 0.0, -1.0)                  # straight at the viewer
    result = cone_segment((400.0, 300.0), 770.0, apex, axis, 0.22)
    _fail_if(result is not None,
             "a beam down the lens must fall back to the sphere, not solve")
    _ok("a beam aimed at the viewer falls back to its bounding sphere")


def main():
    test_the_phase_function_integrates_to_one()
    test_zero_g_is_isotropic()
    test_forward_scattering_leans_forward()
    test_broadside_is_the_unit()
    test_the_phase_function_survives_its_singularity()
    test_extinction_is_beer_lambert()
    test_height_fog_thins_upward()
    test_the_slant_length_inverts_what_the_cone_applied()
    test_the_air_is_ragged_enough_to_see()
    test_raggedness_is_not_a_second_density_control()
    test_the_air_moves()
    test_the_mirror_matches_the_shader()
    test_the_foreshorten_floor_is_shared()
    test_the_march_terminates_early()
    test_every_sample_lands_inside_the_beam()
    test_a_beam_aimed_at_the_viewer_falls_back()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
