# ABOUTME: Profiles for fixtures that move or project — moving heads, scanners,
# ABOUTME: flower effects and lasers.
"""Fixtures that point somewhere.

The stage is a flat front elevation, so none of these can aim into a world that
has depth. What they do instead is aim *in the picture*: the beam swings across
it and foreshortens as the fixture turns toward the audience, and the body
visibly turns with it — a yoke for a head, a mirror for a scanner. See
`stage/geometry.py` for the mapping.

Which emission a moving head gets is decided by what its optics say it is, not
by its name: a head with a gobo wheel, a prism or an iris — or a lens narrow
even wide open — is a spot and gets a hard-edged beam; one with none of those is
a wash and gets a soft cone. `stage/optics.py` makes that call for every
fixture on the stage, so it needs no per-fixture code here.

What these profiles do add is a floor. A fixture that aims has to emit
*something* — a yoke swinging a beam that was never drawn is just a moving box —
so where a lamp body with no declared optics stays dark, a head falls back to a
wash and a scanner, which is a mirror on the front of a beam, to a hard one.
"""

import math

from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import QGraphicsEllipseItem, QGraphicsRectItem

from core.fixture_library import FixtureDef
from stage.body import BodyFixture
from stage.emission.cone import BeamFan, emission_for_kind
from stage.emission.volumetric import LaserFan
from stage.frame import Frame
from stage.geometry import Spinner
from stage.optics import BeamKind
from stage.registry import register_renderer

CHASSIS_COLOR = QColor(28, 28, 32)
YOKE_COLOR = QColor(45, 45, 52)
MIRROR_COLOR = QColor(150, 160, 175)


@register_renderer("Moving Head")
class MovingHeadFixture(BodyFixture):
    """A base, a yoke that pans and a head that tilts inside it."""

    # A head aims even if it only declares one of the two axes.
    forced_features = ("PanTiltFeature",)
    # A head with no optics declared at all still throws something.
    beam_fallback = BeamKind.WASH
    # And what it throws is the beam, all of it. The lens is where the light
    # leaves, not a second light of its own: a head that also wore a halo read
    # as a spot with a wash bolted to it, which is not a fixture.
    lamp_is_lens = True

    def build_housing(self) -> None:
        width, height = self.width, self.height
        self.base = QGraphicsRectItem(0, height * 0.72, width, height * 0.28, self)
        self.base.setBrush(QBrush(CHASSIS_COLOR))
        # The yoke rotates about the pan axis, so its own origin has to sit
        # there rather than at the item's corner.
        self.yoke = QGraphicsRectItem(-width * 0.42, -height * 0.36,
                                      width * 0.84, height * 0.72, self)
        self.yoke.setBrush(QBrush(YOKE_COLOR))
        self.yoke.setPos(width / 2, height * 0.4)
        self.head = QGraphicsEllipseItem(-width * 0.3, -height * 0.26,
                                         width * 0.6, height * 0.52, self)
        self.head.setBrush(QBrush(CHASSIS_COLOR))
        self.head.setPos(width / 2, height * 0.4)

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return emission_for_kind(cls.beam_kind(fdef))

    def aim_origin(self):
        return self.width / 2.0, self.height * 0.4

    def paint_housing(self, frame: Frame) -> None:
        motion = frame.state.motion
        swing = self.aim.pan_swing(motion)
        self.yoke.setRotation(swing)
        self.head.setRotation(swing)
        # Tilting toward or away from the audience foreshortens the head the
        # same way it foreshortens the beam.
        squash = max(0.25, abs(math.cos(math.radians(self.aim.tilt_offset(motion)))))
        self.head.setTransform(_squash(squash))


@register_renderer("Scanner", "Barrel Scanner")
class ScannerFixture(BodyFixture):
    """A fixed body with a mirror on the front that deflects the beam.

    The mirror turns half as far as the beam does — a reflection moves at twice
    the angle of the surface making it — which is why a scanner can cross a room
    faster than a moving head.
    """

    forced_features = ("PanTiltFeature",)
    # There is no such thing as a scanner that washes: the mirror is there to
    # deflect a beam, so one that declares no optics still gets a hard one.
    beam_fallback = BeamKind.HARD

    def build_housing(self) -> None:
        width, height = self.width, self.height
        self.chassis = QGraphicsRectItem(0, 0, width, height * 0.7, self)
        self.chassis.setBrush(QBrush(CHASSIS_COLOR))
        self.mirror = QGraphicsRectItem(-width * 0.3, -height * 0.06,
                                        width * 0.6, height * 0.12, self)
        self.mirror.setBrush(QBrush(MIRROR_COLOR))
        self.mirror.setPos(width / 2, height * 0.78)

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return emission_for_kind(cls.beam_kind(fdef))

    def aim_origin(self):
        return self.width / 2.0, self.height * 0.78

    def paint_housing(self, frame: Frame) -> None:
        motion = frame.state.motion
        self.mirror.setRotation(self.aim.pan_swing(motion) / 2.0)
        squash = max(0.2, abs(math.cos(math.radians(self.aim.tilt_offset(motion)))))
        self.mirror.setTransform(_squash(squash))


@register_renderer("Flower", "Effect")
class FlowerFixture(BodyFixture):
    """A moonflower or derby: one lamp, many narrow beams, spinning."""

    # The rosette IS the output. Its beams all leave through the one front
    # optic, so that optic is a lens like a head's — a flower does not also
    # wash the room, and drawing it with a full halo made it look like a par
    # someone had drawn beams on.
    lamp_is_lens = True

    def build_housing(self) -> None:
        width, height = self.width, self.height
        self.chassis = QGraphicsEllipseItem(0, 0, width, height, self)
        self.chassis.setBrush(QBrush(CHASSIS_COLOR))

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return BeamFan

    def aim_origin(self):
        return self.width / 2.0, self.height * 0.75

    def rosette_angle(self, frame: Frame) -> float:
        """How far round the rosette of beams has turned.

        A flower's rotation channel spins the whole optic; a fixture with no
        rotation channel simply holds still.
        """
        if not hasattr(self, "_spinner"):
            self._spinner = Spinner()
        motion = frame.state.motion
        if motion.rotation_deg is not None:
            return motion.rotation_deg
        return self._spinner.advance(motion.rotation_hz, frame.t)


@register_renderer("Laser")
class LaserFixture(BodyFixture):
    """A projector box throwing a fan of thin beams."""

    def build_housing(self) -> None:
        width, height = self.width, self.height
        self.chassis = QGraphicsRectItem(0, 0, width, height * 0.8, self)
        self.chassis.setBrush(QBrush(CHASSIS_COLOR))
        aperture = min(width, height) * 0.18
        self.aperture = QGraphicsEllipseItem((width - aperture) / 2,
                                             height * 0.8 - aperture / 2,
                                             aperture, aperture, self)
        self.aperture.setBrush(QBrush(QColor(70, 70, 80)))

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return LaserFan

    def aim_origin(self):
        return self.width / 2.0, self.height * 0.8


def _squash(factor: float):
    """A vertical squash about the item's own origin."""
    from PyQt6.QtGui import QTransform
    transform = QTransform()
    transform.scale(1.0, factor)
    return transform
