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
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import QGraphicsItem

from ui.stage.emission.base import Emission
from ui.stage.frame import BEAM_LENGTH_PX, Frame
from ui.stage.light import LightList, linear_rgb

# A laser projector's fan. More lines than this is a solid sheet anyway.
MAX_LASER_BEAMS = 24
LASER_SPREAD_DEG = 50.0
LASER_LENGTH_PX = BEAM_LENGTH_PX * 1.4   # lasers throw further than a wash

PLUME_BLOBS = 5
PLUME_RISE_PX = 150.0
PLUME_WIDTH_PX = 120.0

# Half the pen width `_LaserItem` draws with. A laser is a line to the raster
# path and a very thin quad to the GPU, and this is where they agree.
LASER_HALF_WIDTH_PX = 1.0

# What a puff of fog is worth as light. Fog does not emit — it scatters
# whatever is already in the air — so it sits far below a lamp, and the raster
# path's own alpha ceiling (90/255) is the honest number to carry over.
PLUME_SCATTER = 90.0 / 255.0
_PLUME_RGB = (220, 225, 235)


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
        origin_x, origin_y = self.body.aim_origin()
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
        origin_x, origin_y = self.body.aim_origin()
        rgb = linear_rgb(beam.rgb)

        step = (spread / (count - 1)) if count > 1 else 0.0
        first = beam.axis_deg - spread / 2.0
        for index in range(count):
            out.add_line(origin_x, origin_y, rgb, beam.level,
                         first + index * step, length, LASER_HALF_WIDTH_PX)

    @classmethod
    def reach(cls, fdef) -> float:
        return LASER_LENGTH_PX


class _PlumeItem(QGraphicsItem):
    """A column of soft blobs drifting upward out of a nozzle."""

    def __init__(self, parent, origin_x: float, origin_y: float):
        super().__init__(parent)
        self._origin = QPointF(origin_x, origin_y)
        self.setZValue(-1)
        self._output = 0.0
        self._phase = 0.0
        #: False once the GPU layer draws this plume instead.
        self.enabled = True

    def set_plume(self, output: float, phase: float) -> None:
        self.prepareGeometryChange()
        self._output = output
        self._phase = phase
        self.setVisible(self.enabled and output > 0.01)
        self.update()

    def boundingRect(self) -> QRectF:
        return QRectF(self._origin.x() - PLUME_WIDTH_PX,
                      self._origin.y() - PLUME_RISE_PX - PLUME_WIDTH_PX,
                      PLUME_WIDTH_PX * 2, PLUME_RISE_PX + PLUME_WIDTH_PX * 2)

    def paint(self, painter: QPainter, option, widget) -> None:
        if self._output <= 0.01:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(PLUME_BLOBS):
            # Each blob is further along the same rise, so the column looks
            # continuous rather than like five separate puffs.
            travel = ((self._phase + index / PLUME_BLOBS) % 1.0)
            centre = QPointF(self._origin.x(),
                             self._origin.y() - travel * PLUME_RISE_PX)
            radius = PLUME_WIDTH_PX * (0.25 + 0.75 * travel)
            # Thins as it rises and spreads — fog does not stay a ball.
            alpha = int(90 * self._output * (1.0 - travel))
            if alpha <= 0:
                continue
            gradient = QRadialGradient(centre, radius)
            near = QColor(220, 225, 235, alpha)
            far = QColor(220, 225, 235, 0)
            gradient.setColorAt(0.0, near)
            gradient.setColorAt(1.0, far)
            painter.setBrush(QBrush(gradient))
            painter.drawEllipse(centre, radius, radius)


class FogPlume(Emission):
    """What comes out of a hazer or a smoke machine."""

    animates = True

    # How fast the column climbs, in plume-lengths per second.
    RISE_HZ = 0.25

    def build(self) -> None:
        origin_x, origin_y = self.body.aim_origin()
        self.plume = _PlumeItem(self.body, origin_x, origin_y - self.body.height / 2)

    def render(self, frame: Frame) -> None:
        output = frame.state.atmosphere.output
        self.plume.set_plume(output, (frame.t * self.RISE_HZ) % 1.0)

    def set_raster_light_visible(self, visible: bool) -> None:
        self.plume.enabled = visible
        if not visible:
            self.plume.setVisible(False)

    def emit_lights(self, frame: Frame, out: LightList) -> None:
        output = frame.state.atmosphere.output
        if output <= 0.01:
            return
        phase = (frame.t * self.RISE_HZ) % 1.0
        origin_x, origin_y = self.body.aim_origin()
        origin_y -= self.body.height / 2.0
        rgb = linear_rgb(_PLUME_RGB)
        for index in range(PLUME_BLOBS):
            # Each blob is further along the same rise, so the column looks
            # continuous rather than like five separate puffs.
            travel = (phase + index / PLUME_BLOBS) % 1.0
            # Thins as it rises and spreads — fog does not stay a ball.
            level = PLUME_SCATTER * output * (1.0 - travel)
            if level <= 0.0:
                continue
            out.add_blob(origin_x, origin_y - travel * PLUME_RISE_PX, rgb,
                         level, PLUME_WIDTH_PX * (0.25 + 0.75 * travel))

    @classmethod
    def reach(cls, fdef) -> float:
        return PLUME_RISE_PX
