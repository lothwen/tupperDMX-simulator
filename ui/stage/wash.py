# ABOUTME: Renders Color Changer / Dimmer fixtures as a glowing lamp in a
# ABOUTME: housing, with a beam cone when the fixture declares optics.
"""Pars, washes and plain dimmers.

The common case: one housing, one lamp, optionally a cone of light. A colour
changer takes its hue from the resolved state; a dimmer has no colour to take,
so it glows white at whatever level its intensity channel says.
"""

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QGraphicsRectItem

from core.fixture_state import FixtureState
from ui.stage.base import BodyFixture, GlowingEllipse, StrobeGate
from ui.stage.beam import BeamCone
from ui.stage.registry import register_renderer

HOUSING_COLOR = QColor(20, 20, 20)
LAMP_INSET = 0.15          # fraction of the body inset on each side


class LampFixture(BodyFixture):
    """A housing with a single glowing lamp, plus a beam when there's a lens."""

    def build_body(self) -> None:
        self.housing = QGraphicsRectItem(0, 0, self.width, self.height, self)
        self.housing.setBrush(HOUSING_COLOR)

        inset_x = self.width * LAMP_INSET
        inset_y = self.height * LAMP_INSET
        size = min(self.width - 2 * inset_x, self.height - 2 * inset_y)
        self.lamp = GlowingEllipse((self.width - size) / 2, (self.height - size) / 2,
                                   size, size, self)
        self.beam = BeamCone(self, self.width / 2, self.height)
        self._strobe = StrobeGate(self._repaint)

    def apply_state(self, state: FixtureState) -> None:
        self._strobe.apply_state(state)
        self._repaint()

    def _repaint(self) -> None:
        factor = self.intensity_factor() * self._strobe.factor()
        self.lamp.setGlowColor(self.clamped_color(self.state.rgb, factor),
                               min(1.0, factor))
        self.beam.apply_state(self.state)

    def cleanup(self) -> None:
        self._strobe.stop()


@register_renderer("Color Changer")
class ColorChangerFixture(LampFixture):
    """A colour-mixing wash or par."""


@register_renderer("Dimmer")
class DimmerFixture(LampFixture):
    """A single-channel dimmer. Same body; its state simply has no colour."""
