"""
Characterization test: the ACES tonemap the GPU layer resolves with.

The curve itself lives in GLSL (`stage/gpu/shaders.py`), where CI cannot run
it — the offscreen platform plugin refuses to make an OpenGL context at all.
So the maths is mirrored here in Python and its *properties* are pinned: that it
is monotonic, that black stays black, that it never exceeds full scale however
overdriven the input, and that "brighter than white" rolls toward white instead
of clipping per channel and shifting hue on the way.

The mirror is checked against the shader source line by line, so a constant
edited in one and not the other fails here rather than on stage.

Run with:
    uv run python tests/test_tonemap.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from stage.gpu import shaders


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


# --- the Python mirror of the GLSL -----------------------------------------
# Stephen Hill's fitted RRT+ODT, written the way it is published: ROW-major,
# each tuple a row, `_mul` doing row . vector. The shader has to transpose these
# into GLSL's column-major mat3 constructor, and that transpose is exactly the
# thing this file exists to keep honest.
ACES_IN = ((0.59719, 0.35458, 0.04823),
           (0.07600, 0.90834, 0.01566),
           (0.02840, 0.13383, 0.83777))
ACES_OUT = ((1.60475, -0.53108, -0.07367),
            (-0.10208, 1.10813, -0.00605),
            (-0.00327, -0.07276, 1.07602))


def _mul(matrix, vector):
    return tuple(sum(row[i] * vector[i] for i in range(3)) for row in matrix)


def _rrt_and_odt_fit(v):
    a = tuple(c * (c + 0.0245786) - 0.000090537 for c in v)
    b = tuple(c * (0.983729 * c + 0.4329510) + 0.238081 for c in v)
    return tuple(a[i] / b[i] for i in range(3))


def aces_fitted(colour):
    """Linear HDR in, display-referred 0..1 out."""
    colour = _mul(ACES_IN, colour)
    colour = _rrt_and_odt_fit(colour)
    colour = _mul(ACES_OUT, colour)
    return tuple(max(0.0, min(1.0, c)) for c in colour)


# --- the properties that matter --------------------------------------------
def test_black_stays_black():
    print("test_black_stays_black")
    out = aces_fitted((0.0, 0.0, 0.0))
    _assert(all(abs(c) < 1e-6 for c in out),
            f"nothing in, nothing out {tuple(round(c, 6) for c in out)}")


def test_the_curve_is_monotonic():
    print("test_the_curve_is_monotonic")
    previous = -1.0
    value = 0.0
    while value <= 16.0:
        out = aces_fitted((value, value, value))[0]
        _fail_if(out < previous - 1e-9,
                 f"the curve turned back on itself at {value}")
        previous = out
        value += 0.05
    _assert(True, "brighter input never produces a darker output, 0..16")


def test_it_never_exceeds_full_scale():
    print("test_it_never_exceeds_full_scale")
    for value in (1.0, 2.0, 4.0, 10.0, 100.0, 1e4):
        out = aces_fitted((value, value * 0.5, value * 0.25))
        _fail_if(any(c > 1.0 or c < 0.0 for c in out),
                 f"{value} mapped outside 0..1: {out}")
    _assert(True, "however overdriven the input, the output stays displayable")


def test_double_full_reads_brighter_than_full():
    """The whole reason for the HDR buffer: two lights on one patch — or the
    exposure raised — have to survive as a visible difference, not clip to the
    same white."""
    print("test_double_full_reads_brighter_than_full")
    at_full = aces_fitted((1.0, 1.0, 1.0))[0]
    overdriven = aces_fitted((2.0, 2.0, 2.0))[0]
    _assert(overdriven > at_full + 0.05,
            f"2.0 is visibly brighter than 1.0 ({at_full:.3f} -> "
            f"{overdriven:.3f}) rather than both clipping to white")
    _assert(at_full < 1.0,
            f"and full scale is not already clipped ({at_full:.3f}), so there "
            f"is headroom left to see it in")


def test_a_saturated_colour_rolls_toward_white():
    """A deep wash pushed hard should desaturate the way a camera does, not
    clip one channel and swing hue."""
    print("test_a_saturated_colour_rolls_toward_white")
    dim = aces_fitted((0.25, 0.0, 0.5))
    hot = aces_fitted((8.0, 0.0, 16.0))
    _assert(min(hot) > min(dim),
            f"the empty channel fills in as it gets hotter "
            f"({min(dim):.3f} -> {min(hot):.3f})")
    # Saturation, not absolute spread: the lit channels rise too, so the gap
    # between brightest and darkest can widen while the colour still washes
    # out. What "toward white" means is that the gap shrinks *relative to* how
    # bright the colour has become.
    saturation_dim = (max(dim) - min(dim)) / max(max(dim), 1e-6)
    saturation_hot = (max(hot) - min(hot)) / max(max(hot), 1e-6)
    _assert(saturation_hot < saturation_dim - 0.1,
            f"so the colour converges on white rather than clipping "
            f"(saturation {saturation_dim:.3f} -> {saturation_hot:.3f})")


def test_the_mirror_matches_the_shader():
    """The constants here and the constants in the GLSL are the same numbers,
    and the shader transposes them exactly once."""
    print("test_the_mirror_matches_the_shader")
    source = shaders.RESOLVE_FRAG

    for name, matrix in (("ACES_IN", ACES_IN), ("ACES_OUT", ACES_OUT)):
        match = re.search(rf"const mat3 {name} = mat3\(([^)]*)\)", source)
        _fail_if(match is None, f"{name} not found in the shader")
        numbers = [float(n) for n in re.findall(r"-?\d+\.\d+", match.group(1))]
        _fail_if(len(numbers) != 9, f"{name} has {len(numbers)} components")
        # GLSL fills mat3 by COLUMN, so the shader's literal read in order is
        # the transpose of the row-major matrix published by Hill.
        transposed = [matrix[row][col] for col in range(3) for row in range(3)]
        _fail_if(any(abs(a - b) > 1e-9 for a, b in zip(numbers, transposed)),
                 f"{name} in the shader is not the transpose of the "
                 f"row-major matrix — the tonemap will tint everything")
    _assert(True, "both matrices are the published values, transposed once")

    for constant in ("0.0245786", "0.000090537", "0.983729", "0.4329510",
                     "0.238081"):
        _fail_if(constant not in source,
                 f"the fit constant {constant} is missing from the shader")
    _assert(True, "and the RRT+ODT fit constants agree")


def _fail_if(cond, msg):
    if cond:
        raise AssertionError(msg)


def main():
    test_black_stays_black()
    test_the_curve_is_monotonic()
    test_it_never_exceeds_full_scale()
    test_double_full_reads_brighter_than_full()
    test_a_saturated_colour_rolls_toward_white()
    test_the_mirror_matches_the_shader()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
