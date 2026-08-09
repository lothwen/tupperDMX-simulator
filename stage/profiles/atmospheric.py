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

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QPainterPath

from core.fixture_library import FixtureDef
from stage import puffs
from stage.body import BodyFixture
from stage.emission.base import NoEmission
from stage.emission.volumetric import FogPlume
from stage.frame import Frame
from stage.geometry import Spinner
from stage.profiles import chassis
from stage.registry import register_renderer


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
        """A case standing on the floor with a snout on top of it.

        The one fixture here that is not hung: a fog machine sits on the deck,
        so it gets feet rather than a bracket, and its nozzle points up out of
        the top plate — which is where `aim_origin` puts the plume.
        """
        width, height = self.width, self.height
        radius = min(width, height) * 0.09
        origin_x = self.aim_origin()[0]
        snout = min(width, height) * 0.11

        # The feet are part of the case, not a detail on it: a machine that
        # loses its feet the moment it is drawn small is a box floating on the
        # deck, and LOD is exactly when that happens.
        case = QPainterPath()
        case.setFillRule(Qt.FillRule.WindingFill)
        case.addPath(chassis.rounded(width * 0.03, height * 0.26,
                                     width * 0.94, height * 0.66, radius))
        for side in (0.07, 0.77):
            case.addPath(chassis.rounded(width * side, height * 0.88,
                                         width * 0.16, height * 0.12,
                                         radius * 0.4))
        self.chassis = chassis.ChassisPart(case, self, barrel=0.35)
        self.deck = chassis.ChassisPart(
            chassis.rounded(width * 0.13, height * 0.20,
                            width * 0.74, height * 0.14, radius * 0.6), self,
            top=chassis.DARK_TOP, bottom=chassis.DARK_BOTTOM, rim=chassis.DARK_RIM)
        self.nozzle = chassis.ChassisPart(
            chassis.rounded(origin_x - snout, height * 0.06,
                            snout * 2, height * 0.22, snout * 0.5), self,
            top=chassis.DARK_TOP, bottom=chassis.DARK_BOTTOM, rim=chassis.SHELL_RIM)
        self.nozzle.add_detail(_disc(origin_x, height * 0.09, snout * 0.6),
                               chassis.LENS_BOTTOM)

        # The control panel every one of these has on its flank.
        self.chassis.add_detail(
            chassis.rounded(width * 0.60, height * 0.40,
                            width * 0.30, height * 0.22, radius * 0.5),
            chassis.LENS_BOTTOM)

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
        """A guard ring, the blade behind it, and a hub it turns on.

        Drawn in that order and not this one: the blade goes *inside* the cage,
        so the cage is built first and raised over it. Still one blade item —
        a fan reads as spinning from any single mark going round.
        """
        width, height = self.width, self.height
        centre_x, centre_y = width / 2.0, height / 2.0
        span = min(width, height)

        self.blade = chassis.ChassisPart(
            chassis.rounded(-width * 0.045, -height * 0.36,
                            width * 0.09, height * 0.72, span * 0.045), self,
            top=chassis.DARK_TOP, bottom=chassis.DARK_BOTTOM, rim=chassis.SHELL_RIM)
        self.blade.setPos(centre_x, centre_y)

        self.cage = chassis.ChassisPart(
            _ring(centre_x, centre_y, span / 2.0, span * 0.42), self,
            barrel=0.30)
        self.cage.setZValue(1)
        for radius in (0.34, 0.24, 0.14):
            self.cage.add_detail(
                _ring(centre_x, centre_y, span * radius, span * (radius - 0.02)),
                chassis.DARK_TOP)

        self.hub = chassis.ChassisPart(
            _disc(centre_x, centre_y, span * 0.09), self,
            top=chassis.DARK_TOP, bottom=chassis.DARK_BOTTOM, rim=chassis.DARK_RIM)
        self.hub.setZValue(2)
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
    """A stand or truss. Holds things up; emits nothing, does nothing.

    Two shapes, because the category covers both: a lighting stand is taller
    than it is wide and is a post on a base, while most of what OFL files here
    is wider than it is tall — a T-bar or a truss span, which is a crossbar on
    legs. Drawing every one as a post made the wide ones a fat slab.
    """

    def build_housing(self) -> None:
        width, height = self.width, self.height
        radius = min(width, height) * 0.12
        parts = (self._post(width, height, radius) if height >= width
                 else self._span(width, height, radius))
        self.post, self.foot = (
            chassis.ChassisPart(path, self, top=chassis.DARK_TOP,
                                bottom=chassis.DARK_BOTTOM,
                                rim=chassis.DARK_RIM)
            for path in parts)

    @staticmethod
    def _post(width: float, height: float, radius: float):
        """A column on a splayed base."""
        column = chassis.rounded(width * 0.40, 0, width * 0.20,
                                 height * 0.92, radius)
        base = QPainterPath()
        base.setFillRule(Qt.FillRule.WindingFill)
        base.addPath(chassis.rounded(0, height * 0.88, width, height * 0.12,
                                     radius))
        base.addPath(chassis.rounded(width * 0.30, height * 0.74,
                                     width * 0.40, height * 0.18, radius))
        return column, base

    @staticmethod
    def _span(width: float, height: float, radius: float):
        """A crossbar on two legs."""
        bar = chassis.rounded(0, 0, width, height * 0.26, radius)
        legs = QPainterPath()
        legs.setFillRule(Qt.FillRule.WindingFill)
        for left in (width * 0.12, width * 0.76):
            legs.addPath(chassis.rounded(left, height * 0.20,
                                         width * 0.12, height * 0.80, radius))
        return bar, legs

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return NoEmission


def _disc(x: float, y: float, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addEllipse(QRectF(x - radius, y - radius, radius * 2, radius * 2))
    return path


def _ring(x: float, y: float, outer: float, inner: float) -> QPainterPath:
    """An annulus — a cage's guard, or one of the wires inside it."""
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)  # outer minus inner
    path.addEllipse(QRectF(x - outer, y - outer, outer * 2, outer * 2))
    path.addEllipse(QRectF(x - inner, y - inner, inner * 2, inner * 2))
    return path
