# ABOUTME: Profiles for fixtures you look straight at — pixel bars, blinders
# ABOUTME: and strobes.
"""Arrays and panels.

Three fixtures whose light is a surface rather than a beam. What they have in
common is that the emission does all the drawing; the housing is either nothing
at all (a bar is its cells) or a plain box behind the bulbs.
"""

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QGraphicsRectItem

from core.fixture_library import FixtureDef
from ui.stage.base import BodyFixture
from ui.stage.emission.surface import BulbArray, FlatPanel, MatrixCells
from ui.stage.registry import register_renderer

HOUSING_COLOR = QColor(20, 20, 20)


@register_renderer("Pixel Bar", "Matrix")
class PixelBarFixture(BodyFixture):
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
    """A housing with one glowing bulb per emitter, stacked vertically."""

    def build_housing(self) -> None:
        self.housing = QGraphicsRectItem(0, 0, self.width, self.height, self)
        self.housing.setBrush(HOUSING_COLOR)

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return BulbArray

    @property
    def bulbs(self):
        return self.emission.bulbs

    def overdrive(self) -> float:
        """Blinders are drawn overdriven.

        `multiply_factor` (2.0 on the patched blinders) pushes the mix past full
        so a half-open bulb already saturates — blinders read as glare, not as a
        colour. It is a per-instance look rather than something the fixture
        emits, so it is applied here and nowhere else; every other renderer
        draws its fixture at the level the DMX asks for.
        """
        multiplier = getattr(self.instance, "multiply_factor", 1.0) or 1.0
        return float(multiplier)


@register_renderer("Strobe")
class StrobeFixture(BodyFixture):
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
