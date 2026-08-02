# ABOUTME: The cone of light a lensed fixture throws, and the lamp behind it.
"""Cones.

A fixture that declares a lens (or carries a zoom channel) throws a visible
cone. How visible depends on the air: `frame.haze` scales it, because a beam in
clean air is not a thing you can see. Fixtures with no optics — an LED bar —
get no cone at all, which is what they looked like before.

The cone is drawn in the plane of the stage image, aimed by `beam.axis_deg` and
shortened by `beam.foreshorten` (see `ui/stage/geometry.py`); with no pan or
tilt channel those are "straight down, full length" and the shape is identical
to the one the engine drew before fixtures could aim.
"""

import math
from typing import Optional, Tuple

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF,
)
from PyQt6.QtWidgets import QGraphicsItem, QGraphicsPolygonItem

from ui.stage import wheelart
from ui.stage.base import GlowingEllipse
from ui.stage.emission.base import Emission
from ui.stage.frame import BEAM_LENGTH_PX, BeamParams, Frame

# Capability types that mean "this fixture has optics", i.e. throws a cone.
_OPTIC_CAPABILITIES = frozenset({"Zoom", "BeamAngle"})

BEAM_MAX_ALPHA = 90        # opacity at the lens, fading to nothing at the end
BEAM_MIN_INTENSITY = 0.02  # below this the cone is hidden entirely
LAMP_INSET = 0.15          # fraction of the body inset on each side

# Above this many zones the lamps are sampled: a wash with a big matrix behind
# it would otherwise put hundreds of glowing items on the scene for one fixture.
MAX_LAMPS = 24

# How much of the cone's visibility comes from the air rather than from the
# fixture. At zero haze a wash still shows a hint of its beam; a hard beam
# without haze is nearly all lens flare and no shaft.
HAZE_FLOOR = 0.25

# What makes a spot look like a spot rather than a wash: a tighter cone that
# holds its brightness all the way down instead of dissolving.
HARD_BEAM_NARROWING = 0.55
HARD_BEAM_ALPHA = 150

# How far the lamp's glow spreads past the lens. When there is no cone the glow
# IS the light and has to carry the whole fixture (the LED bar's 20x look);
# once a beam is drawn the glow is only the flare around the lens, and left at
# 20x it floods the picture and hides the beam it belongs to.
GLOW_WITHOUT_BEAM = 20.0
GLOW_WITH_WASH = 7.0
GLOW_WITH_BEAM = 4.0


class BeamCone(QGraphicsPolygonItem):
    """A soft cone of light spreading from its parent fixture's lens."""

    def __init__(self, parent, origin_x: float, origin_y: float):
        super().__init__(parent)
        self._origin = QPointF(origin_x, origin_y)
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setZValue(-1)     # behind the fixture body
        self.setVisible(False)

    def apply_beam(self, beam: BeamParams, haze: float = 1.0,
                   hard: bool = False,
                   rgb: Optional[Tuple[int, int, int]] = None) -> None:
        """Redraw for one frame. `hard` is the spot-vs-wash distinction: a hard
        beam is tighter and brighter, and stays visible in less haze.

        `rgb` overrides the beam's colour, for a fixture whose face is several
        colours at once: what it throws is their mix."""
        if beam.half_angle_deg is None or beam.level <= BEAM_MIN_INTENSITY:
            self.setVisible(False)
            return

        angle = max(1.0, beam.half_angle_deg)
        if hard:
            angle *= HARD_BEAM_NARROWING
        length = beam.length_px * (0.15 + 0.85 * beam.foreshorten)
        # An iris closes the aperture; frost opens the edge back up.
        spread = math.tan(math.radians(angle)) * length
        spread *= max(0.05, beam.aperture)
        spread *= 1.0 + beam.softness
        # Pointing at the viewer: what was a shaft becomes a bloom.
        spread /= max(0.3, 0.3 + 0.7 * beam.foreshorten)

        self.prepareGeometryChange()
        self.setPolygon(_cone_polygon(self._origin, beam.axis_deg, length, spread))

        tip = _tip(self._origin, beam.axis_deg, length)
        gradient = QLinearGradient(self._origin, tip)
        colour = beam.rgb if rgb is None else rgb
        near = QColor(*(max(0, min(255, int(c * beam.level))) for c in colour))
        peak = HARD_BEAM_ALPHA if hard else BEAM_MAX_ALPHA
        near.setAlpha(int(peak * beam.level
                          * (HAZE_FLOOR + (1.0 - HAZE_FLOOR) * haze)))
        far = QColor(near)
        far.setAlpha(0)
        gradient.setColorAt(0.0, near)
        gradient.setColorAt(1.0, far)
        self.setBrush(QBrush(gradient))
        self.setVisible(True)


def _tip(origin: QPointF, axis_deg: float, length: float) -> QPointF:
    radians = math.radians(axis_deg)
    return QPointF(origin.x() + math.cos(radians) * length,
                   origin.y() + math.sin(radians) * length)


def _cone_polygon(origin: QPointF, axis_deg: float, length: float,
                  spread: float) -> QPolygonF:
    """The triangle from the lens out to a flat end cap `spread` wide."""
    radians = math.radians(axis_deg)
    tip = _tip(origin, axis_deg, length)
    # Perpendicular to the axis, so the cap stays square to the beam however
    # the fixture is aimed.
    across = QPointF(-math.sin(radians) * spread, math.cos(radians) * spread)
    return QPolygonF([origin,
                      QPointF(tip.x() - across.x(), tip.y() - across.y()),
                      QPointF(tip.x() + across.x(), tip.y() + across.y())])


class WashLamp(Emission):
    """One glowing lamp per zone, plus the cone when the fixture has optics.

    The common case by a distance: pars, washes, plain dimmers, and any moving
    head whose channels don't describe a hard-edged beam. Nearly all of them are
    a single lamp, and that is exactly what one zone draws.

    A wash whose face is split into independently-driven zones — a segmented
    blinder-wash, anything with a small matrix — gets one lamp per zone, laid
    out across its face the way the segments are. The cone takes their mix:
    by the time the light lands the zones have overlapped into one colour.
    """

    def build(self) -> None:
        body = self.body
        zones = max(1, body.cell_count())
        count = min(zones, MAX_LAMPS)
        # More zones than lamps: sample across them rather than drawing the
        # first few, so the whole face is still represented.
        self.emitter_index = [index * zones // count for index in range(count)]

        inset_x, inset_y = body.width * LAMP_INSET, body.height * LAMP_INSET
        slot = (body.width - 2 * inset_x) / count
        size = min(slot, body.height - 2 * inset_y)
        top = (body.height - size) / 2
        multiplier = (GLOW_WITH_WASH if self.reach(body.fdef)
                      else GLOW_WITHOUT_BEAM)
        self.lamps = []
        for index in range(count):
            lamp = GlowingEllipse(inset_x + index * slot + (slot - size) / 2,
                                  top, size, size, body)
            lamp.glow_radius_multiplier = multiplier
            self.lamps.append(lamp)

        origin_x, origin_y = body.aim_origin()
        self.cone = BeamCone(body, origin_x, origin_y)

    @property
    def lamp(self):
        """The lamp, for the single-zone fixtures that have only one — the name
        the profile, the scene preview and the parity tests reach for."""
        return self.lamps[0]

    def render(self, frame: Frame) -> None:
        beam = frame.beam
        emitters = frame.state.emitters
        # Per-zone only when the state drives every zone this body shows;
        # `emitter_index` rises with the lamps, so its last entry is the
        # highest emitter asked for.
        per_zone = len(self.lamps) > 1 and len(emitters) > self.emitter_index[-1]

        for index, lamp in enumerate(self.lamps):
            if per_zone:
                emitter = emitters[self.emitter_index[index]]
                rgb, level = emitter.rgb, emitter.intensity * beam.gate
            else:
                rgb, level = beam.rgb, beam.level
            lamp.setGlowColor(self.body.clamped_color(rgb, level),
                              min(1.0, level))

        self.cone.apply_beam(beam, frame.haze,
                             rgb=self._mixed(emitters) if per_zone else None)

    def _mixed(self, emitters) -> Tuple[int, int, int]:
        """The colour this body's zones make together.

        The zones' own colours, averaged — a dark zone pulls the mix down, which
        is how one lit segment of four throws a quarter of the light. Levels are
        deliberately left out: the beam already carries the fixture's dimmer,
        and counting it here too would dim the cone twice.
        """
        shown = [emitters[index] for index in self.emitter_index]
        return tuple(sum(e.rgb[component] for e in shown) // len(shown)
                     for component in range(3))

    @classmethod
    def reach(cls, fdef) -> float:
        """A cone only appears when the fixture has optics to throw it with —
        a declared lens, or a channel that sets the beam's spread."""
        if fdef.physical.lens_degrees is not None:
            return BEAM_LENGTH_PX
        has_optics = any(cap.type in _OPTIC_CAPABILITIES
                         for spec in fdef.channels for cap in spec.capabilities)
        return BEAM_LENGTH_PX if has_optics else 0.0


class GoboPlane(QGraphicsItem):
    """The pattern a gobo cuts into the beam, drawn where the beam lands.

    One item for the whole pattern — a breakup gobo is sixty-odd blobs, and
    sixty QGraphicsItems per fixture is not a thing a stage can afford. The
    shape itself is cached by `wheelart`; this only places, spins and tints it.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setZValue(-1)
        self.setVisible(False)
        self._path: Optional[QPainterPath] = None
        self._radius = 1.0
        self._rgb: Tuple[int, int, int] = (255, 255, 255)
        self._level = 0.0

    def apply_beam(self, beam: BeamParams, origin: QPointF, haze: float) -> None:
        path = wheelart.gobo_path(beam.gobo, _gobo_radius(beam))
        if path is None or beam.level <= BEAM_MIN_INTENSITY:
            self.setVisible(False)
            return

        self.prepareGeometryChange()
        self._path = path
        self._radius = _gobo_radius(beam)
        self._rgb, self._level = beam.rgb, beam.level
        # Sit at the far end of the beam, where the pattern would be in focus.
        length = beam.length_px * (0.15 + 0.85 * beam.foreshorten)
        radians = math.radians(beam.axis_deg)
        self.setPos(origin.x() + math.cos(radians) * length * 0.78,
                    origin.y() + math.sin(radians) * length * 0.78)
        self.setRotation(beam.gobo_spin_deg)
        self.setOpacity(min(1.0, 0.35 + 0.5 * haze))
        self.setVisible(True)
        self.update()

    def boundingRect(self) -> QRectF:
        reach = self._radius * 1.2
        return QRectF(-reach, -reach, reach * 2, reach * 2)

    def paint(self, painter, option, widget) -> None:
        if self._path is None or self._level <= 0:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = QColor(*(max(0, min(255, int(c * self._level))) for c in self._rgb))
        colour.setAlpha(190)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(colour))
        painter.drawPath(self._path)


def _gobo_radius(beam: BeamParams) -> float:
    """How big the pattern lands, from the beam's own spread."""
    angle = max(1.0, beam.half_angle_deg or 12.0) * HARD_BEAM_NARROWING
    length = beam.length_px * (0.15 + 0.85 * beam.foreshorten)
    return max(6.0, math.tan(math.radians(angle)) * length * 0.78
               * max(0.05, beam.aperture))


class HardBeam(Emission):
    """A defined-edge beam, and as many copies of it as the prism is making.

    What separates a spot from a wash on stage is that you can see where the
    beam stops, and what is cut into it. Drawn brighter and tighter than
    `WashLamp`'s cone, with the gobo pattern where the beam lands; a prism
    splits it into several — one cone per facet, built up front and hidden when
    the prism is out, so a frame never allocates.
    """

    #: A prism with more facets than this smears into a disc anyway.
    MAX_COPIES = 8

    animates = True

    def build(self) -> None:
        body = self.body
        inset_x, inset_y = body.width * LAMP_INSET, body.height * LAMP_INSET
        size = min(body.width - 2 * inset_x, body.height - 2 * inset_y)
        self.lamp = GlowingEllipse((body.width - size) / 2,
                                   (body.height - size) / 2, size, size, body)
        self.lamp.glow_radius_multiplier = GLOW_WITH_BEAM
        origin_x, origin_y = body.aim_origin()
        self._origin = QPointF(origin_x, origin_y)
        self.cones = [BeamCone(body, origin_x, origin_y)
                      for _ in range(self.MAX_COPIES)]
        self.cone = self.cones[0]
        self.gobo = GoboPlane(body)

    def render(self, frame: Frame) -> None:
        beam = frame.beam
        self.lamp.setGlowColor(self.body.clamped_color(beam.rgb, beam.level),
                               min(1.0, beam.level))
        self.gobo.apply_beam(beam, self._origin, frame.haze)

        copies = beam.copies[:self.MAX_COPIES] or (0.0,)
        # A prism divides the light it splits rather than multiplying it.
        share = 1.0 / max(1, len(copies)) ** 0.5
        base_axis = beam.axis_deg
        base_level = beam.level
        for index, cone in enumerate(self.cones):
            if index >= len(copies):
                cone.setVisible(False)
                continue
            # The copies fan out around the beam's own axis; a single "copy" at
            # offset 0 is just the beam itself.
            beam.axis_deg = base_axis + _prism_spread(copies, index)
            beam.level = base_level * (1.0 if len(copies) == 1 else share)
            cone.apply_beam(beam, frame.haze, hard=True)
        beam.axis_deg, beam.level = base_axis, base_level

    @classmethod
    def reach(cls, fdef) -> float:
        return BEAM_LENGTH_PX


def _prism_spread(copies, index: int) -> float:
    """Where one prism copy sits, relative to the undeviated beam.

    A prism deviates its copies by a few degrees, not by the full circle the
    facet angles describe — those say which way round the rosette each copy
    goes, and this turns that into a believable spread on a flat stage.
    """
    if len(copies) == 1:
        return 0.0
    return math.sin(math.radians(copies[index])) * PRISM_SPREAD_DEG


PRISM_SPREAD_DEG = 9.0


class BeamFan(Emission):
    """A rosette of narrow beams — a flower effect, a moonflower, a derby.

    One body, many beams that all do the same thing: the fixture spins them
    rather than aiming them individually, so `motion.rotation_hz` turns the
    whole rosette.
    """

    animates = True
    BEAMS = 6
    HALF_ANGLE_DEG = 4.0

    def build(self) -> None:
        body = self.body
        size = min(body.width, body.height) * 0.5
        self.lamp = GlowingEllipse((body.width - size) / 2,
                                   (body.height - size) / 2, size, size, body)
        self.lamp.glow_radius_multiplier = GLOW_WITH_BEAM
        origin_x, origin_y = body.aim_origin()
        self.cones = [BeamCone(body, origin_x, origin_y) for _ in range(self.BEAMS)]

    def render(self, frame: Frame) -> None:
        beam = frame.beam
        self.lamp.setGlowColor(self.body.clamped_color(beam.rgb, beam.level),
                               min(1.0, beam.level))

        base_axis = beam.axis_deg
        base_angle = beam.half_angle_deg
        spin = self.body.rosette_angle(frame)
        beam.half_angle_deg = self.HALF_ANGLE_DEG
        step = 360.0 / self.BEAMS
        for index, cone in enumerate(self.cones):
            # Spread across the front of the stage rather than all the way
            # round: half the rosette would otherwise point into the truss.
            beam.axis_deg = base_axis + ((spin + index * step) % 360.0 - 180.0) * 0.35
            cone.apply_beam(beam, frame.haze, hard=True)
        beam.axis_deg, beam.half_angle_deg = base_axis, base_angle

    @classmethod
    def reach(cls, fdef) -> float:
        return BEAM_LENGTH_PX
