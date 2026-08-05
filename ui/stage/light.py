# ABOUTME: The light list — every emission's output as renderer-neutral
# ABOUTME: primitives in linear float, for the GPU layer to accumulate.
"""Light, as data.

The raster path draws light by setting brushes on QGraphicsItems, and a brush is
an 8-bit sRGB colour: it cannot hold "twice as bright as white", and two of them
overlapping do not add. Both of those are what a lighting rig actually does, so
the GPU layer needs the light described *before* it was flattened into a brush.

That is this: each emission appends what it is emitting, in **linear** float with
**unbounded** intensity, and the GPU layer accumulates the lot in an HDR buffer
and tonemaps once at the end. Beams crossing therefore add past full scale, and
`config.window.stage_exposure` decides how much of that headroom is visible —
neither of which survives being flattened into a brush.

Two deliberate choices:

*Body-local coordinates.* An emission already computes its lamps and cones in
its body's own frame, so it writes them unchanged; the layer adds the body's
scene position while packing. A fixture dragged in design mode therefore needs a
repaint, not a re-render.

*No per-primitive objects.* One growable `array('f')`, cleared by resetting a
write index. Same reasoning as `BeamParams` being mutable and reused: across a
rig at 20–100 Hz, allocation is the cost that matters, and a stage full of
matrix cells is several thousand primitives a second.
"""

from array import array
from typing import Iterator, NamedTuple, Sequence, Tuple

#: Floats per primitive. Also the VBO stride, which is why it is a round number.
STRIDE = 16

# What a primitive is. The layer draws each kind with its own shader.
#
# Four of them are LIGHT — something a fixture emits. BLOB is the odd one out:
# it is MEDIUM, a parcel of air with something in it. Fog does not glow, it
# scatters whatever is already there, so a blob carries a density and a tint
# rather than a radiance, and the fog pass integrates light through it. (With
# volumetric fog off it still draws as a faint additive puff, which is what it
# was before and what the raster path continues to do.)
POINT = 0.0   # a glowing lamp or bulb: radius
CONE = 1.0    # a beam: axis_deg, length, half_width, softness, hardness
QUAD = 2.0    # a lit face: w, h  (matrix cells, panels)
LINE = 3.0    # a laser: axis_deg, length, half_width
BLOB = 4.0    # a parcel of medium: radius, and z/z_extent in the spare slots

_EMPTY: Tuple[float, float, float] = (0.0, 0.0, 0.0)

#: One primitive's worth of zeros, to grow the buffer by. Bound once: this is
#: on the path every new primitive takes until the list reaches its size.
_ZEROS: Tuple[float, ...] = (0.0,) * STRIDE


def _decode(value: float) -> float:
    """One sRGB-encoded 0..1 component as linear light."""
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


#: sRGB byte -> linear, precomputed. `linear_rgb` runs once per primitive per
#: frame, which is often enough that the pow() is worth avoiding.
_SRGB_TO_LINEAR: Tuple[float, ...] = tuple(
    _decode(index / 255.0) for index in range(256))


def linear_rgb(rgb: Sequence[float]) -> Tuple[float, float, float]:
    """A 0-255 sRGB triplet as linear floats.

    Accepts floats as well as ints: an animated matrix cell mixes its colour in
    float before it is ever clamped, and rounding it here would be the only
    place that precision is lost.
    """
    red, green, blue = rgb[0], rgb[1], rgb[2]
    return (_SRGB_TO_LINEAR[0 if red < 0 else 255 if red > 255 else int(red)],
            _SRGB_TO_LINEAR[0 if green < 0 else 255 if green > 255 else int(green)],
            _SRGB_TO_LINEAR[0 if blue < 0 else 255 if blue > 255 else int(blue)])


def linear_to_srgb(value: float) -> float:
    """The inverse of `_decode`, for tests and read-outs. Clamps to 0..1."""
    value = 0.0 if value < 0.0 else 1.0 if value > 1.0 else value
    if value <= 0.0031308:
        return value * 12.92
    return 1.055 * (value ** (1.0 / 2.4)) - 0.055


class Primitive(NamedTuple):
    """One light, unpacked. A view for tests and debugging — the renderer reads
    the flat buffer instead."""
    kind: float
    x: float
    y: float
    rgb: Tuple[float, float, float]
    intensity: float
    radius: float
    axis_deg: float
    length: float
    half_width: float
    softness: float
    hardness: float
    foreshorten: float
    w: float
    h: float

    # A BLOB has no axis and no throw, so it spends those two slots on the two
    # things a parcel of medium does need. Named here so nothing has to read
    # `axis_deg` and mean depth.
    @property
    def z(self) -> float:
        """BLOB only: how far into the picture the parcel sits."""
        return self.axis_deg

    @property
    def z_extent(self) -> float:
        """BLOB only: how deep it reaches, either side of `z`."""
        return self.length

    @property
    def density(self) -> float:
        """BLOB only: `intensity` read as what it is for a medium."""
        return self.intensity


class LightList:
    """What one body is emitting this frame, in renderer-neutral terms.

    Refilled in place every render. `clear()` keeps the allocation, so a body
    that has been drawing for an hour has long since stopped growing it.
    """

    __slots__ = ("_data", "_count")

    def __init__(self) -> None:
        self._data = array("f")
        self._count = 0

    # -- writing -------------------------------------------------------
    def clear(self) -> None:
        self._count = 0

    def _slot(self) -> int:
        """Reserve one primitive's worth of floats and return its offset."""
        offset = self._count * STRIDE
        if offset + STRIDE > len(self._data):
            self._data.extend(_ZEROS)
        self._count += 1
        return offset

    def _write(self, kind: float, x: float, y: float,
               rgb: Tuple[float, float, float], intensity: float,
               radius: float = 0.0, axis_deg: float = 0.0, length: float = 0.0,
               half_width: float = 0.0, softness: float = 0.0,
               hardness: float = 0.0, foreshorten: float = 1.0,
               w: float = 0.0, h: float = 0.0) -> None:
        at = self._slot()
        data = self._data
        data[at] = kind
        data[at + 1] = x
        data[at + 2] = y
        data[at + 3] = rgb[0]
        data[at + 4] = rgb[1]
        data[at + 5] = rgb[2]
        data[at + 6] = intensity
        data[at + 7] = radius
        data[at + 8] = axis_deg
        data[at + 9] = length
        data[at + 10] = half_width
        data[at + 11] = softness
        data[at + 12] = hardness
        data[at + 13] = foreshorten
        data[at + 14] = w
        data[at + 15] = h

    def add_point(self, x: float, y: float, rgb: Tuple[float, float, float],
                  intensity: float, radius: float) -> None:
        """A glowing lamp or bulb. `intensity` may exceed 1.0 — that is the
        point of it."""
        self._write(POINT, x, y, rgb, intensity, radius=radius)

    def add_cone(self, x: float, y: float, rgb: Tuple[float, float, float],
                 intensity: float, axis_deg: float, length: float,
                 half_width: float, softness: float = 0.0,
                 hardness: float = 0.0, foreshorten: float = 1.0) -> None:
        """A beam leaving a lens. `hardness` is the spot-vs-wash distinction and
        `foreshorten` how much it points at the viewer, which is what decides
        how brightly it scatters."""
        self._write(CONE, x, y, rgb, intensity, axis_deg=axis_deg,
                    length=length, half_width=half_width, softness=softness,
                    hardness=hardness, foreshorten=foreshorten)

    def add_quad(self, x: float, y: float, w: float, h: float,
                 rgb: Tuple[float, float, float], intensity: float,
                 halo: float = 0.0) -> None:
        """A lit face: one matrix cell, or a whole panel.

        `halo` is how far its light carries into the air *past* the face, in
        scene units. Without it a cell lights exactly the rectangle it occupies
        — which the raster path is already painting, so the fixture reads as a
        picture of light rather than as something emitting any.
        """
        self._write(QUAD, x, y, rgb, intensity, radius=halo, w=w, h=h)

    def add_line(self, x: float, y: float, rgb: Tuple[float, float, float],
                 intensity: float, axis_deg: float, length: float,
                 half_width: float) -> None:
        """One laser beam: thin, hard, and still bright where a wash has faded."""
        self._write(LINE, x, y, rgb, intensity, axis_deg=axis_deg,
                    length=length, half_width=half_width)

    def add_blob(self, x: float, y: float, rgb: Tuple[float, float, float],
                 density: float, radius: float, z: float = 0.0,
                 z_extent: float = 0.0) -> None:
        """A parcel of medium — a puff of smoke, a pocket of haze.

        Not a light. `density` is how much of the air this parcel fills and
        `rgb` is what colour it scatters, so the fog pass integrates other
        fixtures' light *through* it rather than adding this to the frame. `z`
        and `z_extent` place it in depth: they ride in the `axis_deg` and
        `length` slots, which a blob has no use for.

        With volumetric fog off it falls back to being drawn as a faint additive
        puff, which is what it has always been and what the raster path does.
        """
        self._write(BLOB, x, y, rgb, density, radius=radius,
                    axis_deg=z, length=z_extent)

    # -- reading -------------------------------------------------------
    def __len__(self) -> int:
        return self._count

    def buffer(self) -> memoryview:
        """The packed floats, exactly `len(self) * STRIDE` of them."""
        return memoryview(self._data)[:self._count * STRIDE]

    def primitives(self) -> Iterator[Primitive]:
        """Unpacked, for tests. The renderer uses `buffer()`."""
        data = self._data
        for index in range(self._count):
            at = index * STRIDE
            yield Primitive(
                kind=data[at], x=data[at + 1], y=data[at + 2],
                rgb=(data[at + 3], data[at + 4], data[at + 5]),
                intensity=data[at + 6], radius=data[at + 7],
                axis_deg=data[at + 8], length=data[at + 9],
                half_width=data[at + 10], softness=data[at + 11],
                hardness=data[at + 12], foreshorten=data[at + 13],
                w=data[at + 14], h=data[at + 15])

    def of_kind(self, kind: float) -> list:
        """Every primitive of one kind. Tests ask this constantly."""
        return [p for p in self.primitives() if p.kind == kind]
