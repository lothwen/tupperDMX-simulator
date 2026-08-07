# ABOUTME: Profiles for fixtures that fill the air rather than light it —
# ABOUTME: hazers, smoke machines and fans — plus the inert stand.
"""Air, wind and scaffolding.

A hazer emits no light at all, and yet it is the most important fixture on this
stage: without haze there is nothing for anyone's beam to scatter off, so every
cone in the rig is drawn against what these contribute (see
`stage/atmosphere.py`). A fan and a stand emit nothing and change nothing —
they are on the stage because they are on the stage, and a rig that silently
dropped them would be lying about the patch.
"""

from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import QGraphicsEllipseItem, QGraphicsRectItem

from core.fixture_library import FixtureDef
from stage import puffs
from stage.body import BodyFixture
from stage.emission.base import NoEmission
from stage.emission.volumetric import FogPlume
from stage.frame import Frame
from stage.geometry import Spinner
from stage.registry import register_renderer

CHASSIS_COLOR = QColor(28, 28, 32)
NOZZLE_COLOR = QColor(60, 60, 68)
STAND_COLOR = QColor(38, 38, 42)


@register_renderer("Hazer")
class HazerFixture(BodyFixture):
    """A box with a nozzle, and the plume coming out of it.

    Also feeds the stage's haze level, which is what makes every *other*
    fixture's beam visible. Any fixture resolving a fog output contributes, not
    just this profile — plenty of movers have a hazer built in — but this is the
    one that draws a plume.

    A hazer's ceiling is low on purpose (`puffs.HAZER.max_output`): what it
    makes is a thin, even layer that fills a room, and a hazer that could put
    out a smoke machine's density would be a smoke machine.
    """

    #: Which machine this is, for the plume's parcels.
    fog_spec = puffs.HAZER

    #: What to weigh this fixture's output as when its definition declares no
    #: OFL `fogType`. Without it a machine's *category* — the one thing every
    #: definition does declare — was never consulted at all.
    default_fog_kind = "Haze"

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


@register_renderer("Smoke")
class SmokeFixture(HazerFixture):
    """The same box, making something else entirely.

    A smoke machine puts out dense, local parcels that rise, spread and take the
    better part of a minute to thin out — so closing its channel does not clear
    the stage, and that hang is what reads as smoke rather than as an effect
    being switched off. It is the *lower* contributor to stage-wide haze for the
    same reason: fog stays where it was made instead of filling the room.
    """

    fog_spec = puffs.SMOKE
    default_fog_kind = "Fog"


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
