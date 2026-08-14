# ABOUTME: Profiles for fixtures you look straight at — pixel bars, blinders
# ABOUTME: and strobes.
"""Arrays and panels.

Three fixtures whose light is a surface rather than a beam, which is exactly
what makes their housings awkward: the emission covers every pixel of the body,
so there is nothing *behind* it to draw. The bar and the strobe get a
`chassis.frame` laid over the face instead — a border with a hole, the way a
real extrusion's frame laps the edge of the strip inside it — and clip that
face to the housing's outline, since a rounded frame over a square face leaves
the four corners hanging outside the fixture. Only the blinder, whose bulbs
leave room around themselves, has a housing underneath.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPainterPath
from PyQt6.QtWidgets import QGraphicsItem

from core.fixture_library import FixtureDef
from stage.body import BodyFixture
from stage.emission.surface import BulbArray, FlatPanel, MatrixCells
from stage.frame import Frame
from stage.profiles import chassis
from stage.registry import register_renderer

#: Longer than this on one axis and the fixture is an extrusion, so it gets end
#: plates. Below it — a matrix, a square strobe — a plain border is the whole
#: housing.
EXTRUSION_ASPECT = 3.0

#: The frame, as a fraction of the body's shorter side. A bar is mostly face:
#: take much more than this and the housing eats the light it is framing.
FRAME_FRACTION = 0.10


class _FramedSurface(BodyFixture):
    """A body whose emission fills it, so the housing can only frame it.

    And must *clip* it. The emission is built to cover the body exactly, corner
    to corner, while the frame's outer edge is rounded — so the square corners
    of the face showed through outside the housing, four red tabs on a fixture
    that was meant to end at its own outline. `shape()` is the housing's
    silhouette and the body clips its children to it, which puts the face
    inside the frame at every radius instead of only along the straights.
    """

    def build_housing(self) -> None:
        width, height = self.width, self.height
        short = min(width, height)
        radius = short * 0.16

        self._silhouette = chassis.rounded(0, 0, width, height, radius)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape,
                     True)

        # `surround`, not `frame`: a Frame in this engine is one tick of light.
        self.surround = chassis.ChassisPart(
            chassis.frame(width, height, short * FRAME_FRACTION, radius), self)
        self.surround.setZValue(1)  # over a face that was built to fill the body

        self.end_plates = None
        if max(width, height) < short * EXTRUSION_ASPECT:
            return
        cap = short * 0.45
        plates = (((0, 0, cap, height), (width - cap, 0, cap, height))
                  if width > height else
                  ((0, 0, width, cap), (0, height - cap, width, cap)))
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        for x, y, w, h in plates:
            path.addPath(chassis.rounded(x, y, w, h, radius))
        self.end_plates = chassis.ChassisPart(path, self)
        self.end_plates.setZValue(1)

    def shape(self) -> QPainterPath:
        """The housing's outline — what the children are clipped to, and what
        the designer hit-tests against."""
        return self._silhouette


@register_renderer("Pixel Bar", "Matrix")
class PixelBarFixture(_FramedSurface):
    """A strip or grid of cells with a glow, driven per-cell by the animation.

    Most are one body showing the whole matrix. The bodies of a bar-set (an LED
    bar's eight bars) all mirror the same DMX slice, so what makes them differ
    is which columns of the running effect they show — `column` is where each
    one starts.
    """

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return MatrixCells

    @property
    def cells(self):
        return self.emission.cells

    @property
    def column(self) -> int:
        return self.emission.column

    @property
    def current_asset(self):
        """Which animation is running, if any. Read by the parity tests."""
        return self.emission.current_asset

    @property
    def current_movie(self):
        return self.emission.current_movie


@register_renderer("Blinder")
class BlinderFixture(BodyFixture):
    """A housing with one glowing bulb per emitter, stacked vertically.

    The one array fixture with room to spare: `BulbArray` keeps its bulbs to
    three quarters of the width and margins them top and bottom, so the housing
    can sit behind them properly — a box in a bracket with a reflector tray the
    bulbs stand in — rather than being laid over the top.
    """

    def build_housing(self) -> None:
        width, height = self.width, self.height
        radius = min(width, height) * 0.10

        self.bracket = chassis.ChassisPart(
            chassis.bracket(width, height), self,
            top=chassis.DARK_TOP, bottom=chassis.DARK_BOTTOM, rim=chassis.DARK_RIM)
        self.housing = chassis.ChassisPart(
            chassis.rounded(width * 0.10, height * 0.04,
                            width * 0.80, height * 0.92, radius), self,
            barrel=0.30)
        # The tray is deliberately wider than any one bulb: it has to hold
        # whatever the state resolves to, and how many bulbs that is only
        # becomes known once DMX has arrived.
        self.tray = chassis.ChassisPart(
            chassis.rounded(width * 0.15, height * 0.09,
                            width * 0.70, height * 0.82, radius * 0.7), self,
            top=chassis.LENS_TOP, bottom=chassis.LENS_BOTTOM,
            rim=chassis.DARK_RIM)

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return BulbArray

    def paint_housing(self, frame: Frame) -> None:
        rgb, level = frame.beam.rgb, frame.beam.level
        self.housing.set_tint(rgb, level * 0.30)
        self.bracket.set_tint(rgb, level * 0.15)

    @property
    def bulbs(self):
        return self.emission.bulbs


@register_renderer("Strobe")
class StrobeFixture(_FramedSurface):
    """A flat panel that flashes at the fixture's strobe rate.

    A dedicated strobe is a surface rather than a lamp in a housing — there is
    no optic to speak of, just something that flashes.
    """

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return FlatPanel

    @property
    def panel(self):
        return self.emission.panel
