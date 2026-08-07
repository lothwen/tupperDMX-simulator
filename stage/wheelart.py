# ABOUTME: Draws what is on a wheel slot — gobo patterns, gel colours, prisms —
# ABOUTME: procedurally, from the slot's own OFL description.
"""Wheel art.

OFL names a gobo by a resource id (`gobos/biohazard`) that points into an SVG
library this app doesn't ship. Rather than vendor several hundred files for a
pattern that ends up a few dozen pixels across on a beam, each slot is drawn
from what its name says it is: a breakup, a dot pattern, radial lines, a spiral.

That trades exactness for coverage, and coverage is the point — a fixture
imported from OFL an hour ago gets a plausible gobo instead of a blank disc, and
one whose resource nobody anticipated still gets *something* keyed off its own
name, so two different gobos never look identical.
"""

import math
from typing import Dict, Optional, Tuple

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QPainterPath

from core import ofl

# Substrings in a gobo's resource id or name, and the pattern to draw for them.
# Ordered: the first match wins, so the more specific ideas come first.
_PATTERNS = (
    (("dot", "circle", "bubble", "ball"), "dots"),
    (("line", "bar", "stripe", "grid", "graphic"), "lines"),
    (("star", "flower", "leaf", "snow", "flake"), "star"),
    (("spiral", "swirl", "twist", "vortex"), "spiral"),
    (("triangle", "square", "hex", "geo", "shape"), "polygon"),
    (("breakup", "cloud", "water", "fire", "foliage", "texture"), "breakup"),
)
_DEFAULT_PATTERN = "breakup"

# Anything above this and the pattern is mush at the size a gobo is drawn.
MAX_GOBO_ELEMENTS = 64

_PATH_CACHE: Dict[Tuple[str, int], QPainterPath] = {}


def slot_rgb(slot: Optional[ofl.WheelSlot]) -> Optional[Tuple[int, int, int]]:
    """The gel colour a slot puts in the beam, if it is a colour at all.

    A two-colour slot (a split gel) is averaged — the beam passes through both
    and the audience sees one colour.
    """
    if slot is None or slot.type != "Color":
        return None
    colours = slot.colors
    if not colours:
        return None
    count = len(colours)
    return tuple(sum(c[i] for c in colours) // count for i in range(3))


def slot_blocks_light(slot: Optional[ofl.WheelSlot]) -> bool:
    """A closed slot is a shutter by another name."""
    return slot is not None and slot.type == "Closed"


def pattern_for(slot: Optional[ofl.WheelSlot]) -> str:
    """Which of the drawable patterns this slot's gobo looks most like."""
    if slot is None or slot.type not in ("Gobo", "AnimationGoboStart"):
        return ""
    text = f"{slot.get('resource', '')} {slot.name}".lower()
    for needles, pattern in _PATTERNS:
        if any(needle in text for needle in needles):
            return pattern
    return _DEFAULT_PATTERN


def gobo_path(slot: Optional[ofl.WheelSlot], radius: float) -> Optional[QPainterPath]:
    """The shape a gobo cuts out of the beam, centred on (0, 0).

    Cached per (pattern, radius): the path is the same every frame, and a
    spinning gobo would otherwise rebuild it thirty times a second.
    """
    pattern = pattern_for(slot)
    if not pattern:
        return None
    seed = _seed(slot)
    key = (f"{pattern}:{seed}", int(radius))
    path = _PATH_CACHE.get(key)
    if path is None:
        path = _build_path(pattern, seed, float(int(radius)))
        _PATH_CACHE[key] = path
    return path


def _seed(slot: ofl.WheelSlot) -> int:
    """A stable number from the slot's identity, so two gobos differ."""
    text = f"{slot.get('resource', '')}{slot.name}"
    return sum(ord(c) * (index + 1) for index, c in enumerate(text)) % 97


def _build_path(pattern: str, seed: int, radius: float) -> QPainterPath:
    path = QPainterPath()
    if pattern == "dots":
        rings = 2 + seed % 2
        for ring in range(1, rings + 1):
            distance = radius * ring / (rings + 0.5)
            count = min(MAX_GOBO_ELEMENTS, 4 + ring * 3 + seed % 3)
            size = radius * 0.13 / ring ** 0.4
            for index in range(count):
                angle = 2 * math.pi * index / count + seed * 0.1 * ring
                path.addEllipse(QPointF(math.cos(angle) * distance,
                                        math.sin(angle) * distance), size, size)
    elif pattern == "lines":
        count = min(MAX_GOBO_ELEMENTS, 4 + seed % 5)
        width = radius * 0.11
        for index in range(count):
            offset = -radius + (2 * radius) * (index + 0.5) / count
            path.addRect(QRectF(-radius, offset - width / 2, radius * 2, width))
    elif pattern == "star":
        points = 5 + seed % 4
        inner = radius * 0.42
        for index in range(points * 2):
            angle = math.pi * index / points - math.pi / 2
            reach = radius if index % 2 == 0 else inner
            point = QPointF(math.cos(angle) * reach, math.sin(angle) * reach)
            path.lineTo(point) if index else path.moveTo(point)
        path.closeSubpath()
    elif pattern == "spiral":
        arms = 2 + seed % 3
        for arm in range(arms):
            start = 2 * math.pi * arm / arms
            path.moveTo(0.0, 0.0)
            for step in range(1, 25):
                turn = start + step * 0.28
                reach = radius * step / 24.0
                path.lineTo(math.cos(turn) * reach, math.sin(turn) * reach)
    elif pattern == "polygon":
        sides = 3 + seed % 4
        for index in range(sides):
            angle = 2 * math.pi * index / sides - math.pi / 2
            point = QPointF(math.cos(angle) * radius, math.sin(angle) * radius)
            path.lineTo(point) if index else path.moveTo(point)
        path.closeSubpath()
    else:   # breakup: irregular blobs, the commonest gobo there is
        count = min(MAX_GOBO_ELEMENTS, 9 + seed % 7)
        for index in range(count):
            angle = 2.399963 * index + seed          # a phyllotaxis spread
            distance = radius * math.sqrt(index / count) * 0.92
            size = radius * (0.10 + 0.09 * ((seed + index) % 5) / 4.0)
            path.addEllipse(QPointF(math.cos(angle) * distance,
                                    math.sin(angle) * distance), size, size)
    return path
