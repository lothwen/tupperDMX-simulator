# ABOUTME: Profiles for fixtures that fill the air rather than light it —
# ABOUTME: hazers, smoke machines and fans — plus the inert stand.
"""Air, wind and scaffolding.

A hazer emits no light at all, and yet it is the most important fixture on this
stage: without haze there is nothing for anyone's beam to scatter off, so every
cone in the rig is drawn against what these contribute (see
`ui/stage/atmosphere.py`). A fan and a stand emit nothing and change nothing —
they are on the stage because they are on the stage, and a rig that silently
dropped them would be lying about the patch.
"""

from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import QGraphicsEllipseItem, QGraphicsRectItem

from core.fixture_library import FixtureDef
from ui.stage.base import BodyFixture
from ui.stage.emission.base import NoEmission
from ui.stage.emission.volumetric import FogPlume
from ui.stage.frame import Frame
from ui.stage.geometry import Spinner
from ui.stage.registry import register_renderer

CHASSIS_COLOR = QColor(28, 28, 32)
NOZZLE_COLOR = QColor(60, 60, 68)
STAND_COLOR = QColor(38, 38, 42)


@register_renderer("Hazer", "Smoke")
class HazerFixture(BodyFixture):
    """A box with a nozzle, and the plume coming out of it.

    Also feeds the stage's haze level, which is what makes every *other*
    fixture's beam visible. Any fixture resolving a fog output contributes, not
    just this profile — plenty of movers have a hazer built in — but this is the
    one that draws a plume.
    """

    def build_housing(self) -> None:
        width, height = self.width, self.height
        self.chassis = QGraphicsRectItem(0, height * 0.25, width, height * 0.75, self)
        self.chassis.setBrush(QBrush(CHASSIS_COLOR))
        nozzle = min(width, height) * 0.22
        self.nozzle = QGraphicsEllipseItem((width - nozzle) / 2,
                                           height * 0.25 - nozzle / 2,
                                           nozzle, nozzle, self)
        self.nozzle.setBrush(QBrush(NOZZLE_COLOR))

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return FogPlume

    def aim_origin(self):
        return self.width / 2.0, self.height * 0.25


@register_renderer("Fan")
class FanFixture(BodyFixture):
    """A cage with blades that turn. Moves air, emits nothing."""

    profile_animates = True

    def build_housing(self) -> None:
        width, height = self.width, self.height
        self.cage = QGraphicsEllipseItem(0, 0, width, height, self)
        self.cage.setBrush(QBrush(CHASSIS_COLOR))
        # One blade item, rotated: a fan reads as "spinning" from any single
        # mark going round, and this keeps it to one item.
        self.blade = QGraphicsRectItem(-width * 0.04, -height * 0.38,
                                       width * 0.08, height * 0.76, self)
        self.blade.setBrush(QBrush(NOZZLE_COLOR))
        self.blade.setPos(width / 2, height / 2)
        self._spinner = Spinner()

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return NoEmission

    def paint_housing(self, frame: Frame) -> None:
        # Whatever the fixture calls its channel, a fan's level is how fast it
        # turns — so the one thing it can show is its speed.
        speed = frame.state.motion.rotation_hz or frame.state.intensity * 4.0
        self.blade.setRotation(self._spinner.advance(speed, frame.t))


@register_renderer("Stand")
class StandFixture(BodyFixture):
    """A stand or truss. Holds things up; emits nothing, does nothing."""

    def build_housing(self) -> None:
        width, height = self.width, self.height
        self.post = QGraphicsRectItem(width * 0.42, 0, width * 0.16, height, self)
        self.post.setBrush(QBrush(STAND_COLOR))
        self.foot = QGraphicsRectItem(0, height * 0.9, width, height * 0.1, self)
        self.foot.setBrush(QBrush(STAND_COLOR))

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return NoEmission
