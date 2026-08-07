# ABOUTME: The cone of light a fixture throws, the lamp behind it, and the same
# ABOUTME: lamp with no cone for the fixtures that only light a surface.
"""Cones.

A fixture whose optics throw a shaft gets a visible cone. How visible depends on
the air: `frame.haze` scales it, because a beam in clean air is not a thing you
can see. Which fixtures those are is `ui/stage/optics.py`'s decision, made from
the fixture's own optics and handed here as a `BeamKind`; a fixture that only
lights a surface — an LED bar, a flood panel — gets `LampGlow` and no cone item
at all, so there is nothing for a stray zoom channel to switch back on.

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
from ui.stage.base import DIFFUSE_GLOW, GlowingEllipse
from ui.stage.emission.base import Emission
from ui.stage.frame import (
    BEAM_LENGTH_PX, FORESHORTEN_FLOOR, BeamParams, Frame,
)
from ui.stage.light import LightList, linear_rgb
from ui.stage.optics import MAX_HALF_ANGLE_DEG, BeamKind

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

# How wide a cone may get relative to its own throw. `tan()` climbs steeply near
# the vertical — 89° is 57x, a 18000px sheet off a 320px beam — and a cone much
# wider than it is long stopped reading as a shaft long before that. Past this
# it is a wall of light, so there is nothing to gain by drawing it any wider.
MAX_SPREAD_RATIO = 2.0

# How far the lamp's glow spreads past the lens. When there is no cone the glow
# IS the light and has to carry the whole fixture (the LED bar's 20x look);
# once a beam is drawn the glow is only the flare around the lens, and left at
# 20x it floods the picture and hides the beam it belongs to.
GLOW_WITHOUT_BEAM = 20.0
GLOW_WITH_WASH = 7.0
GLOW_WITH_BEAM = 4.0

# A ceiling on the glow in absolute scene units, because the multipliers above
# are relative to the lamp and a big fixture has a big lamp: 20x on an ARRI
# SkyPanel S360C is a 2650px radius, which whites out a 1920px stage. Sized
# against BEAM_LENGTH_PX so the widest glow and the longest beam are comparable.
MAX_GLOW_RADIUS_PX = 400.0


class BeamCone(QGraphicsPolygonItem):
    """A soft cone of light spreading from its parent fixture's lens."""

    def __init__(self, parent, origin_x: float, origin_y: float):
        super().__init__(parent)
        self._origin = QPointF(origin_x, origin_y)
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setZValue(-1)     # behind the fixture body
        self.setVisible(False)
        #: False once the GPU layer draws this beam instead.
        self.enabled = True

    def apply_beam(self, beam: BeamParams, haze: float = 1.0,
                   hard: bool = False,
                   rgb: Optional[Tuple[int, int, int]] = None) -> None:
        """Redraw for one frame. `hard` is the spot-vs-wash distinction: a hard
        beam is tighter and brighter, and stays visible in less haze.

        `rgb` overrides the beam's colour, for a fixture whose face is several
        colours at once: what it throws is their mix."""
        if (not self.enabled or beam.half_angle_deg is None
                or beam.level <= BEAM_MIN_INTENSITY):
            self.setVisible(False)
            return

        length, spread = cone_geometry(beam, hard)

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


def _drawable_angle(half_angle_deg: Optional[float]) -> float:
    """A half-angle the cone geometry can actually use.

    The spread is `tan(half_angle)`, which passes 1.6e16 at 90° and goes
    negative beyond it — a QPolygonF out at 1e18 scene units. Nothing the
    classifier returns comes near that (the widest fixture in a 631-strong
    library rests at 32.5°), but a `Zoom` capability can resolve past 180° and a
    `stage.beam` hint can force a cone onto a 270° tube, so the wall is held
    here rather than trusted to every caller.
    """
    return max(1.0, min(MAX_HALF_ANGLE_DEG, half_angle_deg or 1.0))


def _foreshortened(foreshorten: float) -> float:
    """How much of its length a beam keeps once it is aimed at the viewer.

    The fog march inverts this to recover the throw the fixture actually has,
    which is why the constant is in `frame.py` where both can reach it.
    """
    return FORESHORTEN_FLOOR + (1.0 - FORESHORTEN_FLOOR) * foreshorten


def cone_geometry(beam: BeamParams, hard: bool = False) -> Tuple[float, float]:
    """How long this beam reaches, and how wide it is at the far end.

    The one place the shaft's shape is worked out. The raster cone draws a
    polygon from it and the light list hands the same two numbers to the GPU, so
    the two views of one beam cannot drift apart.
    """
    angle = _drawable_angle(beam.half_angle_deg)
    if hard:
        angle *= HARD_BEAM_NARROWING
    length = beam.length_px * _foreshortened(beam.foreshorten)
    # An iris closes the aperture; frost opens the edge back up.
    spread = min(math.tan(math.radians(angle)), MAX_SPREAD_RATIO) * length
    spread *= max(0.05, beam.aperture)
    spread *= 1.0 + beam.softness
    # Pointing at the viewer: what was a shaft becomes a bloom.
    spread /= max(0.3, 0.3 + 0.7 * beam.foreshorten)
    return length, spread


def _emit_cone(out: LightList, body, beam: BeamParams,
               rgb: Tuple[int, int, int], hard: bool = False,
               level: Optional[float] = None,
               axis_deg: Optional[float] = None) -> None:
    """Put one shaft on the light list, shaped exactly as `BeamCone` draws it.

    `level` and `axis_deg` override the beam's own for the callers that fan a
    beam into several — a prism's copies, a flower's rosette — so the shared
    `BeamParams` never has to be mutated to describe them.
    """
    if beam.half_angle_deg is None:
        return
    level = beam.level if level is None else level
    if level <= BEAM_MIN_INTENSITY:
        return
    length, spread = cone_geometry(beam, hard)
    origin_x, origin_y = body.aim_origin()
    out.add_cone(origin_x, origin_y, linear_rgb(rgb), level,
                 beam.axis_deg if axis_deg is None else axis_deg,
                 length, spread,
                 softness=beam.softness,
                 hardness=1.0 if hard else 0.0,
                 foreshorten=beam.foreshorten)


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


# How wide a lens's halo is at rest, in LAMP RADII: 1.0 reaches the edge of the
# lamp itself, 3.0 three times as far again.
#
# Its own unit rather than a fraction of `GLOW`, because that baseline is 4 to
# 20 lamp radii — a wash's halo — and every setting a lens wants is a small
# fraction of it, which crowded the whole usable range into the bottom
# hundredths of the dial. Here a tenth is a tenth of the lamp and the numbers
# say what they do.
#
# Not zero: a lit head has a visibly lit lens, and a bare dot with nothing
# around it reads as a pinprick rather than as glass with a lamp behind it.
LENS_GLOW_AT_REST = 0.15

# ...and what it grows to as the beam turns down the lens.
#
# Aimed at you, a beam puts its whole output in your eye instead of the little
# it scatters sideways, so the lens blooms out to something the size of a wash.
# It is still one beam and there is still no second light: you are just standing
# in it. The shaft is doing the same thing at the same moment — `cone_geometry`
# widens its spread, and the shader's `forward_scatter` brightens it — so this
# is the lens's share of one effect rather than a rule of its own.
LENS_GLOW_AT_VIEWER = 0.35


def lens_glow_radii(foreshorten: float) -> float:
    """How wide a lens's halo is, in lamp radii, for one frame's aim.

    `foreshorten` is 1.0 across the picture and `FORESHORTEN_FLOOR` straight
    down the lens, which is the most any fixture here can turn toward you.

    Squared, so the bloom belongs to the last part of the turn. Linear, a head
    tilted halfway already wore a bigger halo than the flat one this replaced —
    and a beam crossing the stage at 45° is still a beam you are looking at
    from the side. What the eye reads as "that one is pointing at me" happens
    close to the end of the range, which is also where `forward_scatter` puts
    the shaft's own brightening.
    """
    aimed = (1.0 - min(1.0, max(FORESHORTEN_FLOOR, foreshorten))) / (
        1.0 - FORESHORTEN_FLOOR)
    return (LENS_GLOW_AT_REST
            + (LENS_GLOW_AT_VIEWER - LENS_GLOW_AT_REST) * aimed * aimed)


def lens_glow_scale(foreshorten: float, base_glow: float) -> float:
    """The same halo as a fraction of `base_glow`, which is what both paths
    scale: the raster multiplier is in lamp radii and the light list's POINT
    radius is spread over `GLOW_REACH` of them, so handing each its own
    baseline's share keeps one number describing both."""
    return lens_glow_radii(foreshorten) / max(1e-6, base_glow)


def _glow_multiplier(base_glow: float, radius: float, scale: float) -> float:
    """A glow radius in lamp radii, held under the absolute ceiling.

    The cap is in scene units and the multiplier is relative to the lamp, so a
    big fixture reaches it long before a small one — which is the whole reason
    it exists (20x on an ARRI SkyPanel is a 2650px halo on a 1920px stage).
    """
    return min(base_glow * scale, MAX_GLOW_RADIUS_PX / max(1.0, radius))


class Lensed:
    """Shared by the emissions whose lamp might be a lens rather than a light.

    A moving head throws everything it makes down its beam, so its lamp is the
    aperture that beam leaves through and not a second light beside it. Which
    fixtures those are is the profile's call (`BodyFixture.lamp_is_lens`), and
    it only applies where a shaft is actually drawn — on a body whose lamp IS
    its light there would be nothing left to emit.
    """

    #: Whether this body puts a shaft in the air as well as lighting its face.
    DRAWS_CONE = True

    @property
    def lens_only(self) -> bool:
        return self.body.lamp_is_lens and self.DRAWS_CONE

    def glow_scale(self, foreshorten: float = 1.0) -> float:
        """How much of `GLOW` this lamp shows, for one frame's aim.

        1.0 on a lamp that is its own light — the halo is what it emits, and
        where it points changes nothing about that.
        """
        return (lens_glow_scale(foreshorten, self.GLOW)
                if self.lens_only else 1.0)


class WashLamp(Lensed, Emission):
    """One glowing lamp per zone, plus a soft cone.

    The common case by a distance: pars, washes, and any moving head whose
    optics don't describe a hard-edged beam. Nearly all of them are a single
    lamp, and that is exactly what one zone draws.

    A wash whose face is split into independently-driven zones — a segmented
    blinder-wash, anything with a small matrix — gets one lamp per zone, laid
    out across its face the way the segments are. The cone takes their mix:
    by the time the light lands the zones have overlapped into one colour.
    """

    #: How far the lamp's glow spreads, relative to the lamp.
    GLOW = GLOW_WITH_WASH

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
        multiplier = _glow_multiplier(self.GLOW, size / 2.0, self.glow_scale())
        self.lamps = []
        for index in range(count):
            lamp = GlowingEllipse(inset_x + index * slot + (slot - size) / 2,
                                  top, size, size, body)
            lamp.glow_radius_multiplier = multiplier
            if self.lens_only:
                lamp.glow_profile = DIFFUSE_GLOW
            self.lamps.append(lamp)

        origin_x, origin_y = body.aim_origin()
        self.cone = (BeamCone(body, origin_x, origin_y) if self.DRAWS_CONE
                     else None)

    @property
    def lamp(self):
        """The lamp, for the single-zone fixtures that have only one — the name
        the profile, the scene preview and the parity tests reach for."""
        return self.lamps[0]

    def set_raster_light_visible(self, visible: bool) -> None:
        for lamp in self.lamps:
            lamp.draw_glow = visible
            lamp.update()
        if self.cone is not None:
            self.cone.enabled = visible
            if not visible:
                self.cone.setVisible(False)

    def render(self, frame: Frame) -> None:
        beam = frame.beam
        emitters = frame.state.emitters
        # Per-zone only when the state drives every zone this body shows;
        # `emitter_index` rises with the lamps, so its last entry is the
        # highest emitter asked for.
        per_zone = len(self.lamps) > 1 and len(emitters) > self.emitter_index[-1]
        scale = self.glow_scale(beam.foreshorten)

        for index, lamp in enumerate(self.lamps):
            if per_zone:
                emitter = emitters[self.emitter_index[index]]
                rgb, level = emitter.rgb, emitter.intensity * beam.gate
            else:
                rgb, level = beam.rgb, beam.level
            lamp.set_glow_radius(_glow_multiplier(self.GLOW,
                                                  lamp.rect().width() / 2.0,
                                                  scale))
            lamp.setGlowColor(self.body.clamped_color(rgb, level),
                              min(1.0, level))

        if self.cone is not None:
            self.cone.apply_beam(beam, frame.haze,
                                 rgb=self._mixed(emitters) if per_zone else None)

    def emit_lights(self, frame: Frame, out: LightList) -> None:
        beam = frame.beam
        emitters = frame.state.emitters
        per_zone = len(self.lamps) > 1 and len(emitters) > self.emitter_index[-1]
        # The same lens rule the raster path applies, on this path's own
        # baseline: `GLOW_REACH` is the halo the shader spreads a lamp over.
        scale = self.glow_scale(beam.foreshorten)

        for index, lamp in enumerate(self.lamps):
            if per_zone:
                emitter = emitters[self.emitter_index[index]]
                rgb, level = emitter.rgb, emitter.intensity * beam.gate
            else:
                rgb, level = beam.rgb, beam.level
            if level <= 0.0:
                continue
            rect = lamp.rect()
            out.add_point(rect.center().x(), rect.center().y(),
                          linear_rgb(rgb), level, rect.width() / 2.0 * scale)

        if self.cone is not None:
            _emit_cone(out, self.body, beam,
                       self._mixed(emitters) if per_zone else beam.rgb)

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
        """How far the light gets. A constant, because by the time an emission
        class has been picked the question is already answered — that IS what
        picking `WashLamp` over `LampGlow` decided."""
        return BEAM_LENGTH_PX


class LampGlow(WashLamp):
    """The same lamps with no shaft: a panel, a flood, a strip, a soft box.

    A fixture that lights a surface rather than the air between. It is the same
    body as a wash — the lamps, the per-zone layout and the colour all behave
    identically — and the only difference is that no cone is ever built, so a
    zoom channel on a fixture that shouldn't have a beam cannot conjure one.

    The glow does the whole job here, so it spreads much further than it would
    around a lens it is only flaring off.
    """

    GLOW = GLOW_WITHOUT_BEAM
    DRAWS_CONE = False

    @classmethod
    def reach(cls, fdef) -> float:
        return 0.0


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
        length = beam.length_px * _foreshortened(beam.foreshorten)
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
    angle = _drawable_angle(beam.half_angle_deg or 12.0) * HARD_BEAM_NARROWING
    length = beam.length_px * _foreshortened(beam.foreshorten)
    return max(6.0, min(math.tan(math.radians(angle)), MAX_SPREAD_RATIO)
               * length * 0.78 * max(0.05, beam.aperture))


class HardBeam(Lensed, Emission):
    """A defined-edge beam, and as many copies of it as the prism is making.

    What separates a spot from a wash on stage is that you can see where the
    beam stops, and what is cut into it. Drawn brighter and tighter than
    `WashLamp`'s cone, with the gobo pattern where the beam lands; a prism
    splits it into several — one cone per facet, built up front and hidden when
    the prism is out, so a frame never allocates.
    """

    #: A prism with more facets than this smears into a disc anyway.
    MAX_COPIES = 8

    GLOW = GLOW_WITH_BEAM

    animates = True

    def build(self) -> None:
        body = self.body
        inset_x, inset_y = body.width * LAMP_INSET, body.height * LAMP_INSET
        size = min(body.width - 2 * inset_x, body.height - 2 * inset_y)
        self.lamp = GlowingEllipse((body.width - size) / 2,
                                   (body.height - size) / 2, size, size, body)
        self.lamp.glow_radius_multiplier = _glow_multiplier(
            self.GLOW, size / 2.0, self.glow_scale())
        if self.lens_only:
            self.lamp.glow_profile = DIFFUSE_GLOW
        origin_x, origin_y = body.aim_origin()
        self._origin = QPointF(origin_x, origin_y)
        self.cones = [BeamCone(body, origin_x, origin_y)
                      for _ in range(self.MAX_COPIES)]
        self.cone = self.cones[0]
        self.gobo = GoboPlane(body)

    def render(self, frame: Frame) -> None:
        beam = frame.beam
        self.lamp.set_glow_radius(_glow_multiplier(
            self.GLOW, self.lamp.rect().width() / 2.0,
            self.glow_scale(beam.foreshorten)))
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

    def set_raster_light_visible(self, visible: bool) -> None:
        self.lamp.draw_glow = visible and not self.lens_only
        self.lamp.update()
        for cone in self.cones:
            cone.enabled = visible
            if not visible:
                cone.setVisible(False)

    def emit_lights(self, frame: Frame, out: LightList) -> None:
        beam = frame.beam
        if beam.level > 0.0:
            size = self.lamp.rect()
            out.add_point(size.center().x(), size.center().y(),
                          linear_rgb(beam.rgb), beam.level,
                          size.width() / 2.0 * self.glow_scale(beam.foreshorten))

        copies = beam.copies[:self.MAX_COPIES] or (0.0,)
        # A prism divides the light it splits rather than multiplying it.
        share = 1.0 if len(copies) == 1 else 1.0 / len(copies) ** 0.5
        for index in range(len(copies)):
            _emit_cone(out, self.body, beam, beam.rgb, hard=True,
                       level=beam.level * share,
                       axis_deg=beam.axis_deg + _prism_spread(copies, index))

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

    def set_raster_light_visible(self, visible: bool) -> None:
        self.lamp.draw_glow = visible
        self.lamp.update()
        for cone in self.cones:
            cone.enabled = visible
            if not visible:
                cone.setVisible(False)

    def emit_lights(self, frame: Frame, out: LightList) -> None:
        beam = frame.beam
        if beam.level > 0.0:
            rect = self.lamp.rect()
            out.add_point(rect.center().x(), rect.center().y(),
                          linear_rgb(beam.rgb), beam.level, rect.width() / 2.0)

        # The rosette's own half-angle, not the fixture's: these are always the
        # same narrow beams however the body is classified.
        base_angle = beam.half_angle_deg
        beam.half_angle_deg = self.HALF_ANGLE_DEG
        spin = self.body.rosette_angle(frame)
        step = 360.0 / self.BEAMS
        try:
            for index in range(self.BEAMS):
                offset = ((spin + index * step) % 360.0 - 180.0) * 0.35
                _emit_cone(out, self.body, beam, beam.rgb, hard=True,
                           axis_deg=beam.axis_deg + offset)
        finally:
            beam.half_angle_deg = base_angle

    @classmethod
    def reach(cls, fdef) -> float:
        return BEAM_LENGTH_PX


_BY_KIND = {
    BeamKind.NONE: LampGlow,
    BeamKind.WASH: WashLamp,
    BeamKind.HARD: HardBeam,
}


def emission_for_kind(kind: BeamKind):
    """The emission that draws a given kind of light.

    The join between `ui/stage/optics.py`, which decides what a fixture throws,
    and the classes here that draw it. A profile whose body is a lamp in a
    housing goes straight through this; the ones that are a shape of their own —
    a flower's rosette, a laser's fan, a bar's cells — name their emission
    directly, because for them the category has already settled it.
    """
    return _BY_KIND[kind]
