# ABOUTME: Profiles for fixtures that are a lamp in a housing — pars, washes,
# ABOUTME: plain dimmers, and the last-resort body for anything unrecognised.
"""Lamps.

The commonest shape on any stage: one housing, one lamp, optionally a cone of
light. A colour changer takes its hue from the resolved state; a dimmer has no
colour to take, so it glows white at whatever level its intensity channel says.
Both are the same body — which of them a fixture is only changes what its
channels happen to do, and that is the features' business, not the profile's.
"""

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QGraphicsRectItem

from core.fixture_library import FixtureDef
from ui.stage.base import BodyFixture
from ui.stage.emission.cone import WashLamp
from ui.stage.registry import register_renderer

HOUSING_COLOR = QColor(20, 20, 20)


class LampFixture(BodyFixture):
    """A housing with a single glowing lamp, plus a beam when there's a lens."""

    def build_housing(self) -> None:
        self.housing = QGraphicsRectItem(0, 0, self.width, self.height, self)
        self.housing.setBrush(HOUSING_COLOR)

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return WashLamp

    @property
    def lamp(self):
        """The glowing element. Kept on the body: the designer, the scene
        preview and the parity tests all reach for it by name."""
        return self.emission.lamp

    @property
    def beam(self):
        return self.emission.cone


@register_renderer("Color Changer")
class ColorChangerFixture(LampFixture):
    """A colour-mixing wash or par."""


@register_renderer("Dimmer")
class DimmerFixture(LampFixture):
    """A single-channel dimmer. Same body; its state simply has no colour."""


class GenericStageFixture(LampFixture):
    """The fallback body.

    Used when a fixture's categories name nothing we render and its channels
    don't suggest anything more specific — something from a corner of the OFL
    catalogue nobody has drawn yet. It still shows the fixture's position, its
    designer label and whatever colour and level it resolves to, so the rig is
    never missing a light just because its kind is unfamiliar.

    Deliberately registers no category: it is only ever reached by fallthrough.
    """
