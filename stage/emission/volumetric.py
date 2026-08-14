# ABOUTME: Emissions that live in the air rather than on the fixture — laser
# ABOUTME: fans and fog plumes.
"""Things in the air.

A laser is not a cone: it is a set of hard, thin, saturated lines that stay
visible where a wash would have faded out, because the light is coherent. Fog is
the opposite — no shape at all, just a volume that drifts and thins.

Both are drawn as a single custom item rather than a pile of children: a fan of
twenty beams as twenty QGraphicsItems would cost more than the rest of the rig
put together.
"""

import math
from typing import List, Optional, Tuple

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QGraphicsItem

from stage import puffs
from stage.emission.base import Emission
from stage.frame import BEAM_LENGTH_PX, Frame
from stage.light import LightList, linear_rgb

# A laser projector's fan. More lines than this is a solid sheet anyway.
MAX_LASER_BEAMS = 24
LASER_SPREAD_DEG = 50.0
LASER_LENGTH_PX = BEAM_LENGTH_PX * 1.4   # lasers throw further than a wash

# Half the pen width `_LaserItem` draws with. A laser is a line to the raster
# path and a very thin quad to the GPU, and this is where they agree.
LASER_HALF_WIDTH_PX = 1.0

#: How much room the scene preview reserves for a plume. It is a single number
#: for both machines because the emission is shared; the exact reach is a
#: property of the profile's `PuffSpec`, which `reach()` cannot see.
PLUME_REACH_PX = 520.0

#: A parcel's density as a raster alpha ceiling.
#:
#: Not 255, and the difference matters more than it looks. Qt composites
#: `SourceOver`, so overlapping parcels do not add — they saturate: a dozen of
#: them at alpha 0.3 leave 1 - 0.7^12 = 0.99, which is white, opaque, and
#: exactly the "filter over the stage" this is meant to be the opposite of.
#: Kept low enough that a bank of smoke reads as *thickening* rather than as a
#: lid, and thin enough that the stage stays visible through it.
PLUME_ALPHA = 78.0

#: Most parcels the raster path paints, and how much of a skipped parcel's
#: opacity the ones that are drawn carry for it.
#:
#: The field holds a couple of hundred, which the GPU rasterises for nothing but
#: which would be that many large alpha-blended blits per frame here. Sampling
#: them evenly bounds the fill rate whatever the machine has been doing.
#:
#: The two moved in opposite directions when the machines were retuned to fill a
#: room, and the second one is the interesting half. **Alpha does not stand in
#: for count.** Qt composites `SourceOver`, so a parcel drawn at three times the
#: alpha is nothing like three parcels — it is one parcel with a hard edge, and
#: over a room's worth of area that is exactly what it looked like: a handful of
#: separate balls instead of a bank. So the count went up until a hazer's layer
#: is continuous (measured: ~1 ms a frame for the whole plume, which is not the
#: budget the old cap was protecting) and the compensation came down until each
#: parcel is a wisp again.
PLUME_MAX_ITEMS = 80
PLUME_SHARE = 1.8

#: Below this a parcel is not worth its fill rate. See `_PlumeItem.paint`.
PLUME_MIN_ALPHA = 0.02

_PLUME_RGB = (216, 222, 233)
_PLUME_ARGB = (_PLUME_RGB[0] << 16) | (_PLUME_RGB[1] << 8) | _PLUME_RGB[2]

#: The sprite sheet a parcel is drawn with — see `_plume_sprites`. Small: a
#: parcel is drawn several times this size, and every pixel of it costs a `sin`
#: in the one-off build. Detail past this is invisible under the magnification
#: and the noise it carries is soft by construction.
SPRITE_PX = 64


class _LaserItem(QGraphicsItem):
    """The whole fan, painted in one item."""

    def __init__(self, parent, origin_x: float, origin_y: float):
        super().__init__(parent)
        self._origin = QPointF(origin_x, origin_y)
        self.setZValue(-1)
        self._rgb: Tuple[int, int, int] = (255, 255, 255)
        self._level = 0.0
        self._axis = 90.0
        self._spread = LASER_SPREAD_DEG
        self._count = 0
        self._length = LASER_LENGTH_PX
        #: False once the GPU layer draws this fan instead.
        self.enabled = True

    def set_fan(self, rgb, level: float, axis_deg: float, spread_deg: float,
                count: int, length: float) -> None:
        self.prepareGeometryChange()
        self._rgb, self._level = rgb, level
        self._axis, self._spread = axis_deg, spread_deg
        self._count, self._length = count, length
        self.setVisible(self.enabled and level > 0.01 and count > 0)
        self.update()

    def boundingRect(self) -> QRectF:
        reach = self._length + 8
        return QRectF(self._origin.x() - reach, self._origin.y() - reach,
                      reach * 2, reach * 2)

    def paint(self, painter: QPainter, option, widget) -> None:
        if self._level <= 0.01 or self._count <= 0:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = QColor(*(max(0, min(255, int(c * self._level))) for c in self._rgb))
        # Laser light is thin and hard; the alpha stays high where a beam cone
        # would have faded, which is what makes it read as a laser.
        colour.setAlpha(200)
        pen = QPen(colour, 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)

        step = (self._spread / (self._count - 1)) if self._count > 1 else 0.0
        first = self._axis - self._spread / 2.0
        for index in range(self._count):
            radians = math.radians(first + index * step)
            painter.drawLine(
                self._origin,
                QPointF(self._origin.x() + math.cos(radians) * self._length,
                        self._origin.y() + math.sin(radians) * self._length))


class LaserFan(Emission):
    """A projector throwing a fan of thin, saturated beams."""

    animates = True

    def build(self) -> None:
        origin_x, origin_y = self.body.emit_origin()
        self.fan = _LaserItem(self.body, origin_x, origin_y)

    def render(self, frame: Frame) -> None:
        beam = frame.beam
        # A laser's "gobo" is its pattern program: the effect channel decides
        # how many beams are in the fan and how wide it opens.
        program = frame.state.program
        count = MAX_LASER_BEAMS if program.name else max(2, MAX_LASER_BEAMS // 3)
        spread = LASER_SPREAD_DEG * max(0.2, beam.aperture)
        self.fan.set_fan(beam.rgb, beam.level, beam.axis_deg, spread, count,
                         LASER_LENGTH_PX * max(0.3, beam.foreshorten))

    def set_raster_light_visible(self, visible: bool) -> None:
        self.fan.enabled = visible
        if not visible:
            self.fan.setVisible(False)

    def emit_lights(self, frame: Frame, out: LightList) -> None:
        beam = frame.beam
        if beam.level <= 0.01:
            return
        program = frame.state.program
        count = MAX_LASER_BEAMS if program.name else max(2, MAX_LASER_BEAMS // 3)
        spread = LASER_SPREAD_DEG * max(0.2, beam.aperture)
        length = LASER_LENGTH_PX * max(0.3, beam.foreshorten)
        origin_x, origin_y = self.body.emit_origin()
        rgb = linear_rgb(beam.rgb)

        step = (spread / (count - 1)) if count > 1 else 0.0
        first = beam.axis_deg - spread / 2.0
        for index in range(count):
            out.add_line(origin_x, origin_y, rgb, beam.level,
                         first + index * step, length, LASER_HALF_WIDTH_PX)

    @classmethod
    def reach(cls, fdef) -> float:
        return LASER_LENGTH_PX


#: The sprite sheet, once something has asked for it. See `_plume_sprites`.
_SPRITE_CACHE: Optional[List] = None


def _plume_sprites() -> List["QPixmap"]:
    """The shapes a parcel is drawn with, built once and then reused.

    A parcel painted as a radial gradient is a *ball*, and a field of balls is
    the thing everybody recognises as "not smoke" — no matter how many you
    stack, the eye finds the circles. Smoke has structure at every scale, so
    each sprite here is a soft disc multiplied by two octaves of value noise:
    the outline goes ragged, the inside goes blotchy, and the same field of
    parcels reads as billows.

    Several of them, because one sprite repeated a hundred times is a wallpaper
    pattern; a parcel picks one for life and turns slowly, so a bank of smoke
    never shows the same shape twice in the same orientation.

    Every number that decides the shape comes from `puffs.PARCEL_*`, because
    `MEDIUM_FRAG` builds the same shape from the same constants and the two
    quietly drifting apart is exactly how the GPU path ended up drawing clean
    spheres while this one drew smoke.

    Built lazily and cached. It is ~45 ms of Python — a `sin` per lattice
    corner per pixel — which is why the sprites are small and why there are five
    rather than fifty; and it cannot be done at import time anyway, because
    `QPixmap` needs a QGuiApplication and this module is imported long before
    there is one.
    """
    global _SPRITE_CACHE
    if _SPRITE_CACHE is not None:
        return _SPRITE_CACHE

    from PyQt6.QtGui import QImage, QPixmap

    sprites = []
    half = SPRITE_PX / 2.0
    for variant in range(puffs.PARCEL_VARIANTS):
        image = QImage(SPRITE_PX, SPRITE_PX, QImage.Format.Format_ARGB32)
        image.fill(0)
        seed = variant * 977 + 13
        for y in range(SPRITE_PX):
            dy = (y + 0.5 - half) / half
            for x in range(SPRITE_PX):
                dx = (x + 0.5 - half) / half
                r = math.hypot(dx, dy)
                if r >= 1.0:
                    continue
                # A soft disc, gone well before the edge so no sprite can ever
                # show its own square.
                falloff = (math.exp(-puffs.PARCEL_FALLOFF * r * r)
                           * (1.0 - _smoothstep(puffs.PARCEL_RIM, 1.0, r)))
                if falloff <= 0.0:
                    continue
                # Two octaves: one that shapes the outline, one that mottles it.
                coarse = puffs.PARCEL_NOISE_COARSE
                fine = puffs.PARCEL_NOISE_FINE
                noise = (puffs.PARCEL_NOISE_MIX
                         * _noise2(dx * coarse, dy * coarse, seed)
                         + (1.0 - puffs.PARCEL_NOISE_MIX)
                         * _noise2(dx * fine, dy * fine, seed + 41))
                alpha = falloff * (puffs.PARCEL_NOISE_FLOOR
                                   + puffs.PARCEL_NOISE_GAIN * noise)
                value = int(max(0.0, min(1.0, alpha)) * 255)
                if value:
                    image.setPixel(x, y, (value << 24) | _PLUME_ARGB)
        sprites.append(QPixmap.fromImage(image))
    _SPRITE_CACHE = sprites
    return sprites


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = max(0.0, min(1.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


def _noise2(x: float, y: float, seed: int) -> float:
    """Value noise in 0..1. Small, self-contained, and only ever run while the
    sprite sheet is being built — never per frame."""
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy
    fx = fx * fx * (3.0 - 2.0 * fx)
    fy = fy * fy * (3.0 - 2.0 * fy)
    def corner(cx, cy):
        h = math.sin((cx * 127.1 + cy * 311.7 + seed) * 0.7548) * 43758.5453
        return h - math.floor(h)
    top = corner(ix, iy) + (corner(ix + 1, iy) - corner(ix, iy)) * fx
    bottom = (corner(ix, iy + 1)
              + (corner(ix + 1, iy + 1) - corner(ix, iy + 1)) * fx)
    return top + (bottom - top) * fy


class _PlumeItem(QGraphicsItem):
    """The machine's live parcels, painted.

    Reads the same `PuffField` the GPU density map is built from, so the two
    paths cannot disagree about where the smoke is — and so the hang after the
    channel closes is visible on a machine with no usable GL driver too.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setZValue(-1)
        # Smoke is not a thing you can click on, and a bank of it covers most of
        # the stage — left hit-testable it would swallow every press meant for
        # the fixture underneath, or for the stage behind it.
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._puffs = ()
        self._bounds = QRectF()
        self._stride = 1
        #: False once the GPU layer draws this plume instead.
        self.enabled = True

    def set_puffs(self, field) -> None:
        """Adopt the field. Held by reference: it is refilled in place every
        frame, and `set_puffs` is called right after it was."""
        self.prepareGeometryChange()
        self._puffs = field
        self._bounds = _bounds_of(field)
        # Paint every n-th parcel, so the fill rate is bounded by the sprite
        # count rather than by how long the machine has been running.
        self._stride = max(1, (len(field) + PLUME_MAX_ITEMS - 1)
                           // PLUME_MAX_ITEMS)
        self.setVisible(self.enabled and len(field) > 0)
        self.update()

    def boundingRect(self) -> QRectF:
        # Derived from what is actually in the air rather than from the spec's
        # worst case: a parcel at the end of its life is enormous and invisible,
        # and reserving room for it would repaint a quarter of the stage.
        return self._bounds

    def paint(self, painter: QPainter, option, widget) -> None:
        sprites = _plume_sprites()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # Skipping parcels thins the bank, so what is drawn carries their share.
        share = min(PLUME_SHARE, float(self._stride))
        for index, puff in enumerate(self._puffs):
            if index % self._stride:
                continue
            alpha = PLUME_ALPHA * min(1.0, puff.density) * share / 255.0
            # The parcels at the end of their life are both the faintest and by
            # far the largest, so this floor drops what costs the most fill rate
            # and contributes the least. It is the whole difference between a
            # bank of smoke costing a few milliseconds and costing twenty.
            if alpha <= PLUME_MIN_ALPHA:
                continue
            sprite = sprites[puff.variant % len(sprites)]
            painter.save()
            painter.translate(puff.x, puff.y)
            painter.rotate(math.degrees(puff.spin))
            painter.setOpacity(min(1.0, alpha))
            painter.drawPixmap(
                QRectF(-puff.radius, -puff.radius,
                       puff.radius * 2.0, puff.radius * 2.0),
                sprite, QRectF(0, 0, SPRITE_PX, SPRITE_PX))
            painter.restore()


def _bounds_of(field) -> QRectF:
    """The rectangle the live parcels occupy, in body-local coordinates."""
    left = top = right = bottom = None
    for puff in field:
        if left is None:
            left, top = puff.x - puff.radius, puff.y - puff.radius
            right, bottom = puff.x + puff.radius, puff.y + puff.radius
            continue
        left = min(left, puff.x - puff.radius)
        top = min(top, puff.y - puff.radius)
        right = max(right, puff.x + puff.radius)
        bottom = max(bottom, puff.y + puff.radius)
    if left is None:
        return QRectF()
    return QRectF(left, top, right - left, bottom - top)


class FogPlume(Emission):
    """What comes out of a hazer or a smoke machine.

    The parcels live in a `PuffField`, not in this frame's DMX: a smoke machine
    that has just been closed is still full of smoke, and that hang is the whole
    difference between smoke and an effect switching off. Which machine this is
    comes from the profile's `fog_spec` — a hazer's ceiling is low and its
    parcels are thin, a smoke machine's are dense and short-lived.
    """

    animates = True
    reach_rises = True      # a plume climbs out of the nozzle; a beam falls

    def build(self) -> None:
        origin_x, origin_y = self.body.emit_origin()
        self.origin = (origin_x, origin_y - self.body.height / 2.0)
        self.field = puffs.PuffField(
            getattr(self.body, "fog_spec", puffs.HAZER))
        self.plume = _PlumeItem(self.body)

    def render(self, frame: Frame) -> None:
        self.field.update(frame.t, frame.state.atmosphere.output, self.origin)
        self.plume.set_puffs(self.field)

    def set_raster_light_visible(self, visible: bool) -> None:
        self.plume.enabled = visible
        if not visible:
            self.plume.setVisible(False)

    def emit_lights(self, frame: Frame, out: LightList) -> None:
        # Medium, not light: what the fog pass integrates other fixtures' beams
        # through. `render` has already advanced the field for this frame.
        #
        # `spin` and `variant` ride along because the density map has to draw a
        # parcel the way the sprite sheet above does — ragged, and turned to its
        # own angle. Rasterised as the clean Gaussian it was, a bank of smoke on
        # the GPU path was a pile of spheres with a stage-wide noise field
        # sliding along behind it, which is why it read worse than the raster
        # plume it was supposed to improve on.
        rgb = linear_rgb(_PLUME_RGB)
        for puff in self.field:
            out.add_blob(puff.x, puff.y, rgb, puff.density, puff.radius,
                         z=puff.z, z_extent=puff.z_extent, spin=puff.spin,
                         variant=puff.variant % puffs.PARCEL_VARIANTS)

    def cleanup(self) -> None:
        self.field.clear()
        self.plume.set_puffs(self.field)

    @classmethod
    def reach(cls, fdef) -> float:
        return PLUME_REACH_PX
