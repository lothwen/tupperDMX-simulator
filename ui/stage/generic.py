# ABOUTME: Last-resort renderer for fixtures no category or channel shape matches.
"""The fallback body.

Used when a fixture's categories name nothing we render and its channels don't
suggest a lamp either — a hazer, a fan, something from a corner of the OFL
catalogue nobody has drawn yet. It still shows the fixture's position, its
designer label and whatever colour and level it resolves to, so the rig is never
missing a light just because its kind is unfamiliar.
"""

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QGraphicsRectItem

from core.fixture_state import FixtureState
from ui.stage.base import BodyFixture, GlowingEllipse

HOUSING_COLOR = QColor(20, 20, 20)
LAMP_INSET = 0.15


class GenericStageFixture(BodyFixture):
    """A plain housing with one glowing cell."""

    def build_body(self) -> None:
        self.housing = QGraphicsRectItem(0, 0, self.width, self.height, self)
        self.housing.setBrush(HOUSING_COLOR)

        inset_x = self.width * LAMP_INSET
        inset_y = self.height * LAMP_INSET
        size = min(self.width - 2 * inset_x, self.height - 2 * inset_y)
        self.lamp = GlowingEllipse((self.width - size) / 2, (self.height - size) / 2,
                                   size, size, self)

    def apply_state(self, state: FixtureState) -> None:
        factor = self.intensity_factor()
        self.lamp.setGlowColor(self.clamped_color(state.rgb, factor),
                               min(1.0, factor))
