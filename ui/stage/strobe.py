# ABOUTME: Renders Strobe fixtures as a flat flashing panel.
"""Strobes.

A dedicated strobe is a flat panel rather than a lamp in a housing — there is no
optic to speak of, just a surface that flashes. The flashing itself comes from
the shared gate in `ui/stage/base.py`.
"""

from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import QGraphicsDropShadowEffect, QGraphicsRectItem

from core.fixture_state import FixtureState
from ui.stage.base import BodyFixture, StrobeGate
from ui.stage.registry import register_renderer

GLOW_BLUR_RADIUS = 60


@register_renderer("Strobe")
class StrobeFixture(BodyFixture):
    """A flat panel that flashes at the fixture's strobe rate."""

    def build_body(self) -> None:
        self.panel = QGraphicsRectItem(0, 0, self.width, self.height, self)
        self.panel.setBrush(QColor(20, 20, 20))

        glow = QGraphicsDropShadowEffect()
        glow.setBlurRadius(GLOW_BLUR_RADIUS)
        glow.setOffset(0)
        self.setGraphicsEffect(glow)
        self._strobe = StrobeGate(self._repaint)

    def apply_state(self, state: FixtureState) -> None:
        self._strobe.apply_state(state)
        self._repaint()

    def _repaint(self) -> None:
        factor = self.intensity_factor() * self._strobe.factor()
        self.panel.setBrush(QBrush(self.clamped_color(self.state.rgb, factor)))
        effect = self.graphicsEffect()
        if effect is not None:
            effect.setColor(QColor(*self.state.rgb, 255))

    def cleanup(self) -> None:
        self._strobe.stop()
