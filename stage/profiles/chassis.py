# ABOUTME: How a fixture's housing is painted — the shared palette and the one
# ABOUTME: painted part every profile builds its chassis out of.
"""Painted chassis parts.

A fixture body is a dark object photographed in a dark room, and getting that
wrong is what made the old bodies read as paper cut-outs laid over the backdrop.
Two things fix it, and neither is geometry:

**Value and hue.** Where fixtures hang, `stage_background.png` is RGB 4-12 and
*warm* — its blue channel is near zero. A flat neutral grey at 28-45 is both too
light and the wrong colour, so it floats. These parts sit lower and carry the
same warmth, and get their visibility from contrast *inside* the shape rather
than from being brighter than the room.

**A lit edge.** One vertical gradient with a bright first stop gives a top rim
and a shadowed underside in a single fill — the whole reason a flat-shaded
drawing reads as an object. A dark contour underneath separates it from the
backdrop without lightening it.

Detail is drawn only when there is room for it: half the catalogue renders under
60 scene units, and vents on a 20 px head are mud. `LOD_DETAIL` is the cut.
"""

from typing import List, Optional, Tuple

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen,
)
from PyQt6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem

# The housing. Warm-dark, because the room is.
SHELL_RIM = QColor(104, 99, 92)      # the lit top edge
SHELL_TOP = QColor(48, 46, 43)
SHELL_BOTTOM = QColor(18, 17, 17)

# Anything recessed or shadowed — a base plate, a bezel, a vent.
DARK_RIM = QColor(62, 59, 55)
DARK_TOP = QColor(30, 29, 27)
DARK_BOTTOM = QColor(11, 11, 11)

# Glass, before whatever the lamp puts in front of it.
LENS_RIM = QColor(46, 46, 52)
LENS_TOP = QColor(20, 20, 24)
LENS_BOTTOM = QColor(7, 7, 9)

#: The contour. Darker than the darkest backdrop, so a body reads as a
#: silhouette even against the one lit patch of brick.
CONTOUR = QColor(0, 0, 0, 170)

#: Below this on-screen scale, a part draws its shape and nothing else.
LOD_DETAIL = 0.55

#: How much of the fixture's own colour the housing picks up at full level.
#: Spill, not a second lamp — the emission already draws the lens flare, and a
#: chassis that competed with it just looked like two lights in one place.
TINT_WEIGHT = 0.30


def shade(rect: QRectF, top: QColor, bottom: QColor, rim: QColor) -> QLinearGradient:
    """A top-lit vertical gradient across `rect`."""
    gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    gradient.setColorAt(0.0, rim)
    gradient.setColorAt(0.12, top)
    gradient.setColorAt(1.0, bottom)
    return gradient


def rounded(x: float, y: float, w: float, h: float, radius: float) -> QPainterPath:
    """A rounded rectangle as a path, ready to be unioned with others."""
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, w, h), radius, radius)
    return path


def frame(width: float, height: float, thickness: float,
          radius: float) -> QPainterPath:
    """A border with a hole in it.

    For the fixtures you look straight at — a bar, a strobe — whose emission
    covers every pixel of the body, so there is no room *behind* it for a
    housing. This is drawn over the face instead (raise its `zValue`), leaving
    the lit part showing through the hole the way a real extrusion's frame laps
    the edge of the strip inside it.

    `thickness` is clamped so a frame can never close over the face it is meant
    to surround — on a 12 px tube it thins away to nothing instead.
    """
    limit = min(width, height) * 0.4
    thickness = max(0.5, min(thickness, limit))
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)  # outer minus inner
    path.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
    path.addRoundedRect(QRectF(thickness, thickness,
                               width - 2 * thickness, height - 2 * thickness),
                        radius * 0.5, radius * 0.5)
    return path


def bracket(width: float, height: float) -> QPainterPath:
    """The U a fixture hangs in: an arm down each side, a bridge over the top.

    Drawn behind the body, so only the arms show either side — which is what
    you see of a yoke from the front, and most of what makes a box read as a
    fixture rather than as a box.
    """
    radius = min(width, height) * 0.06
    arm = width * 0.085
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)  # or the overlaps cancel out
    for left in (0.0, width - arm):
        path.addPath(rounded(left, height * 0.08, arm, height * 0.90, radius))
    path.addPath(rounded(width * 0.34, 0.0, width * 0.32, height * 0.11, radius))
    return path


class ChassisPart(QGraphicsItem):
    """One painted piece of a housing: a path, shaded, contoured and tintable.

    Parts are plain child items, so a profile still moves them with
    `setRotation`/`setTransform` exactly as it moved the primitives they
    replaced. Everything is built once; `set_tint` is the only per-frame call.
    """

    def __init__(self, path: QPainterPath, parent: QGraphicsItem, *,
                 top: QColor = SHELL_TOP, bottom: QColor = SHELL_BOTTOM,
                 rim: QColor = SHELL_RIM, contour: bool = True,
                 barrel: float = 0.0):
        super().__init__(parent)
        self._path = path
        self._bounds = path.boundingRect()
        self._brush = QBrush(shade(self._bounds, top, bottom, rim))
        self._pen = QPen(CONTOUR, 0) if contour else QPen(Qt.PenStyle.NoPen)
        self._pen.setCosmetic(True)
        self._barrel = self._barrel_brush(barrel) if barrel > 0 else None
        self._details: List[Tuple[QPainterPath, QBrush]] = []
        self._tint: Optional[QColor] = None

    def _barrel_brush(self, strength: float) -> QBrush:
        """Sides darkened toward the edges, which is what makes a box a tube."""
        gradient = QLinearGradient(self._bounds.topLeft(), self._bounds.topRight())
        edge = QColor(0, 0, 0, int(255 * strength))
        gradient.setColorAt(0.0, edge)
        gradient.setColorAt(0.42, QColor(0, 0, 0, 0))
        gradient.setColorAt(0.62, QColor(0, 0, 0, 0))
        gradient.setColorAt(1.0, edge)
        return QBrush(gradient)

    def add_detail(self, path: QPainterPath, color: QColor) -> None:
        """A vent, a panel, a screw. Drawn only when the fixture is big enough.

        The bounds grow to hold it. A detail that sits outside the part's own
        path — a foot below a case, a lug beyond a bracket — is otherwise
        painted outside the rect Qt was told to repaint, and comes and goes with
        whatever else happens to dirty that patch of scene.
        """
        self._details.append((path, QBrush(color)))
        self._bounds = self._bounds.united(path.boundingRect())

    def set_tint(self, rgb: Tuple[int, int, int], weight: float) -> None:
        """Pick up `weight` of the fixture's own output as spill."""
        alpha = int(255 * max(0.0, min(1.0, weight)) * TINT_WEIGHT)
        tint = QColor(*rgb, alpha) if alpha > 0 else None
        if tint != self._tint:
            self._tint = tint
            self.update()

    def boundingRect(self) -> QRectF:
        return self._bounds.adjusted(-1, -1, 1, 1)

    def shape(self) -> QPainterPath:
        return self._path

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(self._pen)
        painter.setBrush(self._brush)
        painter.drawPath(self._path)

        detailed = QStyleOptionGraphicsItem.levelOfDetailFromTransform(
            painter.worldTransform()) >= LOD_DETAIL
        if detailed:
            painter.setPen(Qt.PenStyle.NoPen)
            for path, brush in self._details:
                painter.setBrush(brush)
                painter.drawPath(path)

        if self._barrel is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._barrel)
            painter.drawPath(self._path)

        if self._tint is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(self._tint))
            painter.drawPath(self._path)
