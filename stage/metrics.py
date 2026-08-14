# ABOUTME: How big a fixture is on stage and how many bodies it has — the
# ABOUTME: measurements taken from a definition before any DMX has arrived.
"""Fixture metrics.

What a definition implies about its own size and shape, read from the OFL
document and the sidecar's `stage` hints. Qt-free and DMX-free on purpose: a
body has to be sized and placed before there is a state to resolve for it, and
the scene preview has to ask the same questions before it has a scene.
"""

from dataclasses import replace
from types import MappingProxyType
from typing import Tuple

from core.fixture_library import FixtureDef

# Stage coordinates are background-image pixels (see stage/controller.py), so
# a real-world size needs a scale to become one. Anchored on the LED bar: 1000mm
# tall renders 400px, the height its bespoke renderer used.
MM_PER_STAGE_PX = 2.5
DEFAULT_BODY_MM = (200.0, 200.0, 200.0)   # for a fixture that declares no size
MIN_BODY_PX, MAX_BODY_PX = 12.0, 640.0

DEFAULT_BODY_SPACING_PX = 175             # between bodies of one multi-body fixture

# Pixels spaced this much further apart than they are wide aren't a row of
# cells behind one lens; they are separate housings on one DMX slice.
SEPARATE_BODY_RATIO = 1.5

# What the scene preview scales a body's longer side to, in scene units. Two
# numbers because a fixture that throws something has to share the frame with
# it: the smaller its body, the more of that frame is the light — which is the
# whole point of previewing it. Sized off the throw itself rather than fixed, so
# a laser (which reaches further than a wash) still frames the same way.
PREVIEW_BODY_PX = 340.0
PREVIEW_BODY_OF_REACH = 0.15


def body_size(fdef: FixtureDef) -> Tuple[float, float]:
    """On-stage size of one body, in scene units.

    A `stage.size_px` hint in the sidecar wins outright; otherwise the fixture's
    real dimensions are scaled down and clamped to something visible.
    """
    override = fdef.stage.get("size_px")
    if isinstance(override, (list, tuple)) and len(override) == 2:
        return float(override[0]), float(override[1])
    dimensions = fdef.physical.dimensions_mm or DEFAULT_BODY_MM
    return tuple(max(MIN_BODY_PX, min(MAX_BODY_PX, mm / MM_PER_STAGE_PX))
                 for mm in dimensions[:2])


def body_count(fdef: FixtureDef) -> int:
    """How many physical bodies share this fixture's DMX slice.

    Almost always one. A matrix is a grid of cells *inside* a housing — OFL's
    X axis is where a pixel sits across the fixture's face, not a fixture of
    its own — so a 24-pixel bar is one bar, and a 5x5 flower is one flower.

    The exception is a set sold as several units on a single slice (the LED
    bar's eight bars). That shows up in the fixture's own physical data, as
    pixels spaced far wider apart than a pixel is wide, so it is read from
    there rather than assumed of every matrix.
    """
    override = fdef.stage.get("bodies")
    if isinstance(override, int) and override > 0:
        return override
    if not fdef.matrix:
        return 1
    spacing = fdef.physical.pixel_spacing_mm
    pixel = fdef.physical.pixel_dimensions_mm
    if (spacing and pixel and pixel[0] > 0
            and spacing[0] > pixel[0] * SEPARATE_BODY_RATIO):
        return max(1, fdef.matrix.pixel_count[0])
    return 1


def body_spacing(fdef: FixtureDef) -> int:
    """Gap between the bodies of a multi-body fixture, in scene units."""
    override = fdef.stage.get("body_spacing")
    if isinstance(override, (int, float)) and override > 0:
        return int(override)
    spacing = fdef.physical.pixel_spacing_mm
    if spacing and spacing[0] > 0:
        return int(spacing[0] / MM_PER_STAGE_PX)
    return DEFAULT_BODY_SPACING_PX


def preview_def(fdef: FixtureDef) -> FixtureDef:
    """`fdef` with its body resized for the scene preview.

    On stage a fixture is as big as it really is, standing next to others at the
    same scale, and that is the only place real dimensions mean anything. A
    preview holds one fixture, and what it shares the frame with is its own
    light — which is drawn in ABSOLUTE scene units: a halo capped at 400, a beam
    1220 long. So the real scale is the wrong one there. A 20 mm scanner head
    disappears inside its own beam while a 4 m panel leaves no room for its
    glow, and neither is telling you anything about the scene you are editing.

    Every body is scaled to the same longer side instead, keeping its aspect
    ratio (a bar is still a bar, a par still square) and its spacing, so the
    light always lands in the frame the same way.
    """
    from stage.emission.base import emission_reach

    reach = emission_reach(fdef)
    target = (reach * PREVIEW_BODY_OF_REACH if reach > 0.0 else PREVIEW_BODY_PX)
    width, height = body_size(fdef)
    scale = target / max(width, height)
    return replace(fdef, stage=MappingProxyType({
        **fdef.stage,
        "size_px": (width * scale, height * scale),
        "body_spacing": body_spacing(fdef) * scale,
    }))


def declared_cell_count(fdef: FixtureDef) -> int:
    """How many separately-lit cells this fixture's *definition* implies.

    The resolved state only knows once DMX has arrived, but a body has to be
    built before that — so this reads a declared cell count, else the most times
    any one colour repeats across the channels.
    """
    declared = fdef.stage.get("cells")
    if isinstance(declared, int) and declared > 0:
        return declared
    repeats: dict = {}
    for channel in fdef.channels:
        for capability in channel.capabilities:
            if capability.type == "ColorIntensity":
                colour = str(capability.get("color", ""))
                repeats[colour] = repeats.get(colour, 0) + 1
                break
    return max(repeats.values(), default=1)
