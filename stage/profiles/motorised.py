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

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainterPath

from core.fixture_library import FixtureDef
from stage.body import BodyFixture
from stage.emission.cone import BeamFan, emission_for_kind
from stage.emission.volumetric import LaserFan
from stage.frame import Frame
from stage.geometry import Spinner
from stage.optics import BeamKind
from stage.profiles import chassis
from stage.registry import register_renderer

#: A mirror is the one part of a rig allowed to be brighter than the room: it
#: is not lit, it is *reflecting*, so it stays cool and metallic where every
#: other surface here is warm and dark.
MIRROR_RIM = QColor(178, 186, 200)
MIRROR_TOP = QColor(120, 128, 143)
MIRROR_BOTTOM = QColor(46, 51, 61)

#: How much of its height a moving head's shell keeps when its face is fully
#: foreshortened. The housing is as deep as it is wide, so it never collapses.
SHELL_SQUASH_FLOOR = 0.62

#: And how much of its width the yoke keeps at a quarter turn of pan, where the
#: two arms line up behind each other. A yoke is a real object seen edge-on, not
#: a line, and one that vanished took the head's pivot with it.
YOKE_NARROW_FLOOR = 0.34


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
    # And the housing draws that lens — bezel and glass — so a blacked-out head
    # shows its own dark optic instead of the lamp's stand-in disc, which was
    # sized to the whole body and lighter than the chassis holding it.
    draws_own_aperture = True

    def build_housing(self) -> None:
        """Base, yoke and head, in the order they overlap.

        The yoke is a **U** — two arms and a bridge — and not the filled slab it
        used to be. That mattered more than any amount of shading: a solid
        rectangle spanning the whole body has nothing a yoke has, so panning it
        swung a tilted card across the stage instead of turning a fixture.

        Everything in the yoke and the head is drawn symmetrically about the
        pivot, which is also `aim_origin` — so the lens the emission flares sits
        exactly where this draws glass, and the tilt squash stays centred.
        """
        width, height = self.width, self.height
        radius = min(width, height) * 0.07
        pivot_x, pivot_y = self.aim_origin()

        self.base = chassis.ChassisPart(
            chassis.rounded(width * 0.13, height * 0.70,
                            width * 0.74, height * 0.17, radius), self)
        self.base_plate = chassis.ChassisPart(
            chassis.rounded(width * 0.02, height * 0.85,
                            width * 0.96, height * 0.15, radius), self,
            top=chassis.DARK_TOP, bottom=chassis.DARK_BOTTOM, rim=chassis.DARK_RIM)
        self.base_plate.add_detail(
            chassis.rounded(width * 0.33, height * 0.885,
                            width * 0.34, height * 0.08, radius * 0.5),
            chassis.LENS_BOTTOM)

        # The yoke rotates about the pan axis, so its own origin has to sit
        # there rather than at the item's corner.
        self.yoke = chassis.ChassisPart(self._yoke_path(), self)
        self.yoke.setPos(pivot_x, pivot_y)
        for side in (-1, 1):
            self.yoke.add_detail(
                _disc(side * width * 0.375, 0.0, min(width, height) * 0.05),
                chassis.DARK_BOTTOM)

        self.head = chassis.ChassisPart(
            chassis.rounded(-width * 0.33, -height * 0.33,
                            width * 0.66, height * 0.66,
                            min(width, height) * 0.22), self,
            barrel=0.45)
        self.head.setPos(pivot_x, pivot_y)
        for side in (-1, 1):
            for row in (-1, 0, 1):
                self.head.add_detail(
                    chassis.rounded(side * width * 0.30 - width * 0.023,
                                    row * height * 0.15 - height * 0.037,
                                    width * 0.046, height * 0.075, radius * 0.4),
                    chassis.DARK_BOTTOM)

        # Bezel and glass ride the head, so they turn and squash with it. Both
        # are round off the *smaller* dimension: a lens is a circle whatever
        # shape the housing round it is, and scaling it per-axis drew a tall
        # oval on every fixture taller than it is wide — which is most of them.
        aperture = min(width, height)
        self.bezel = chassis.ChassisPart(
            _ellipse(aperture * 0.26), self.head,
            top=chassis.DARK_TOP, bottom=chassis.DARK_BOTTOM, rim=chassis.DARK_RIM)
        self.lens = chassis.ChassisPart(
            _ellipse(aperture * 0.195), self.bezel,
            top=chassis.LENS_TOP, bottom=chassis.LENS_BOTTOM, rim=chassis.LENS_RIM,
            contour=False)

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return emission_for_kind(cls.beam_kind(fdef))

    def aim_origin(self):
        return self.width / 2.0, self.height * 0.4

    def _yoke_path(self) -> QPainterPath:
        """Two arms and the bridge between them, as one path about the pivot."""
        width, height = self.width, self.height
        radius = min(width, height) * 0.06
        arm = width * 0.15
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)  # or the overlaps cancel out
        for left in (-width * 0.45, width * 0.45 - arm):
            path.addPath(chassis.rounded(left, -height * 0.32,
                                         arm, height * 0.66, radius))
        path.addPath(chassis.rounded(-width * 0.45, height * 0.24,
                                     width * 0.90, height * 0.10, radius))
        return path

    def paint_housing(self, frame: Frame) -> None:
        # The yoke turns about the vertical axis, and a vertical axis is the one
        # thing a front elevation cannot show turning: what the audience sees is
        # the two arms closing on each other. Rotating it instead drew a fixture
        # that had swung in the picture while its beam — which pans into depth,
        # not across — had not moved at all.
        face_on = abs(math.cos(math.radians(self.aim.pan_offset())))
        self.yoke.setTransform(
            _narrow(YOKE_NARROW_FLOOR + (1.0 - YOKE_NARROW_FLOOR) * face_on))

        # The head's face IS the beam's aperture, so it turns with the beam and
        # foreshortens by exactly what shortens it — one quantity, not a second
        # guess at the same tilt. But it is the *face* that foreshortens: a
        # barrel seen end-on is still as deep as it is wide, so squashing the
        # whole head collapsed it to a sliver between two arms that had not
        # moved. The shell gives a little, the aperture all of it.
        squash = max(0.25, frame.beam.foreshorten)
        shell = SHELL_SQUASH_FLOOR + (1.0 - SHELL_SQUASH_FLOOR) * squash
        self.head.setTransform(_turned(self.aim.turn(), shell))
        self.bezel.setTransform(_squash(squash / shell))

        # A lit head is lit from its own lamp: the chassis catches spill, the
        # bezel more of it and the glass most. The flare itself is the
        # emission's, drawn over this.
        rgb, level = frame.beam.rgb, frame.beam.level
        self.lens.set_tint(rgb, level)
        self.bezel.set_tint(rgb, level * 0.7)
        self.head.set_tint(rgb, level * 0.45)
        self.yoke.set_tint(rgb, level * 0.2)


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
        """A lamp body, a neck, and the mirror slung under it.

        The lens recess is placed where the emission actually puts its lamp —
        the body's centre, not the aim origin — so the flare it draws when lit
        lands in an optic rather than on bare casing. The beam still leaves from
        the mirror below, which is what a scanner does.
        """
        width, height = self.width, self.height
        radius = min(width, height) * 0.09

        self.bracket = chassis.ChassisPart(
            chassis.bracket(width, height * 0.80), self,
            top=chassis.DARK_TOP, bottom=chassis.DARK_BOTTOM, rim=chassis.DARK_RIM)
        self.chassis = chassis.ChassisPart(
            chassis.rounded(width * 0.09, 0, width * 0.82, height * 0.66,
                            radius), self, barrel=0.35)
        self.lens = chassis.ChassisPart(
            _disc(width / 2.0, height / 2.0, min(width, height) * 0.38), self,
            top=chassis.LENS_TOP, bottom=chassis.LENS_BOTTOM,
            rim=chassis.DARK_RIM)
        self.neck = chassis.ChassisPart(
            chassis.rounded(width * 0.40, height * 0.60,
                            width * 0.20, height * 0.20, radius * 0.5), self,
            top=chassis.DARK_TOP, bottom=chassis.DARK_BOTTOM, rim=chassis.DARK_RIM)

        # Same rect the mirror has always had, so the swing reads identically.
        self.mirror = chassis.ChassisPart(
            chassis.rounded(-width * 0.3, -height * 0.06,
                            width * 0.6, height * 0.12, radius * 0.35), self,
            top=MIRROR_TOP, bottom=MIRROR_BOTTOM, rim=MIRROR_RIM)
        self.mirror.setPos(width / 2, height * 0.78)

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return emission_for_kind(cls.beam_kind(fdef))

    def aim_origin(self):
        return self.width / 2.0, self.height * 0.78

    def paint_housing(self, frame: Frame) -> None:
        # A reflection moves at twice the angle of the surface making it, so the
        # mirror turns half as far as the beam it throws. It is the *beam* it
        # halves and not the pan channel: how far a scanner's light swings in
        # the picture depends on where the whole fixture is pointing, so reading
        # pan on its own turned a mirror that was deflecting nowhere.
        self.mirror.setRotation(self.aim.turn() / 2.0)
        self.mirror.setTransform(_squash(max(0.2, frame.beam.foreshorten)))

        # A mirror takes the colour of whatever it is deflecting, and takes far
        # more of it than a casing does — that is the whole job of the part.
        rgb, level = frame.beam.rgb, frame.beam.level
        self.mirror.set_tint(rgb, level)
        self.lens.set_tint(rgb, level * 0.8)
        self.chassis.set_tint(rgb, level * 0.25)


@register_renderer("Flower", "Effect")
class FlowerFixture(BodyFixture):
    """A moonflower or derby: one lamp, many narrow beams, spinning."""

    # The rosette IS the output. Its beams all leave through the one front
    # optic, so that optic is a lens like a head's — a flower does not also
    # wash the room, and drawing it with a full halo made it look like a par
    # someone had drawn beams on.
    lamp_is_lens = True
    # And the optic is a cluster of little lenses, which the housing draws — so
    # the lamp leaves its stand-in disc off and lets them show.
    draws_own_aperture = True

    #: Lenses in the rosette. `BeamFan` throws one beam per lens, so this is
    #: its `BEAMS` and not a number of its own.
    OPTICS = BeamFan.BEAMS

    def build_housing(self) -> None:
        """A round can whose front is a ring of small lenses, and which spins.

        A derby's beams come out of its optic, and the optic turns — so the
        lenses turn with the rosette rather than sitting still behind it. Both
        read the same `rosette_angle`, and the emission has already advanced it
        by the time this runs, so the two cannot drift apart.
        """
        width, height = self.width, self.height
        span = min(width, height)

        self.bracket = chassis.ChassisPart(
            chassis.bracket(width, height), self,
            top=chassis.DARK_TOP, bottom=chassis.DARK_BOTTOM, rim=chassis.DARK_RIM)
        self.chassis = chassis.ChassisPart(
            _oval(width * 0.06, height * 0.04, width * 0.88, height * 0.92),
            self, barrel=0.35)

        self.optic = chassis.ChassisPart(
            _disc(width / 2.0, height / 2.0, span * 0.36), self,
            top=chassis.DARK_TOP, bottom=chassis.DARK_BOTTOM,
            rim=chassis.DARK_RIM)
        self.optic.setTransformOriginPoint(width / 2.0, height / 2.0)
        for index in range(self.OPTICS):
            angle = 2.0 * math.pi * index / self.OPTICS
            self.optic.add_detail(
                _disc(width / 2.0 + math.cos(angle) * span * 0.21,
                      height / 2.0 + math.sin(angle) * span * 0.21,
                      span * 0.085),
                chassis.LENS_BOTTOM)
        self.optic.add_detail(_disc(width / 2.0, height / 2.0, span * 0.07),
                              chassis.LENS_BOTTOM)

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return BeamFan

    def paint_housing(self, frame: Frame) -> None:
        self.optic.setRotation(self.rosette_angle(frame))
        rgb, level = frame.beam.rgb, frame.beam.level
        self.optic.set_tint(rgb, level * 0.8)
        self.chassis.set_tint(rgb, level * 0.25)

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
        """A projector box with an emitter window low on its front.

        `LaserFan` builds no lamp at all — the beams are the whole output — so
        unlike every other fixture here there is nothing drawn over this. The
        window is the only thing that says where the light comes out, which is
        why it is a recess rather than the flat disc it used to be.
        """
        width, height = self.width, self.height
        radius = min(width, height) * 0.10
        origin_x, origin_y = self.aim_origin()

        self.bracket = chassis.ChassisPart(
            chassis.bracket(width, height * 0.92), self,
            top=chassis.DARK_TOP, bottom=chassis.DARK_BOTTOM, rim=chassis.DARK_RIM)
        self.chassis = chassis.ChassisPart(
            chassis.rounded(width * 0.09, height * 0.03,
                            width * 0.82, height * 0.83, radius), self,
            barrel=0.35)
        aperture = min(width, height) * 0.30
        self.aperture = chassis.ChassisPart(
            chassis.rounded(origin_x - aperture, origin_y - aperture * 0.45,
                            aperture * 2, aperture * 0.9, radius * 0.4), self,
            top=chassis.LENS_TOP, bottom=chassis.LENS_BOTTOM,
            rim=chassis.DARK_RIM)
        for side in (-1, 1):
            self.chassis.add_detail(
                _disc(origin_x + side * width * 0.30, height * 0.22,
                      min(width, height) * 0.05),
                chassis.DARK_BOTTOM)

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return LaserFan

    def aim_origin(self):
        return self.width / 2.0, self.height * 0.8

    def paint_housing(self, frame: Frame) -> None:
        # The window is the only lit thing on the body, so it takes the colour
        # straight — a laser reads as its colour more than any other fixture.
        rgb, level = frame.beam.rgb, frame.beam.level
        self.aperture.set_tint(rgb, level)
        self.chassis.set_tint(rgb, level * 0.2)


def _squash(factor: float):
    """A vertical squash about the item's own origin."""
    from PyQt6.QtGui import QTransform
    transform = QTransform()
    transform.scale(1.0, factor)
    return transform


def _narrow(factor: float):
    """A horizontal squash about the item's own origin."""
    from PyQt6.QtGui import QTransform
    transform = QTransform()
    transform.scale(factor, 1.0)
    return transform


def _turned(degrees: float, factor: float):
    """A vertical squash carried round by a rotation, in that order.

    A round face aimed away from the audience projects to an ellipse whose short
    axis lies **along the beam**, so the squash has to happen while the item is
    still upright and then be turned with it. That is why it is one transform
    and not `setRotation` plus one: a QGraphicsItem applies its rotation first
    and its `transform` after, so a squash set that way lands *outside* the
    rotation — a face flattened against the picture instead of against its own
    aim. (QTransform itself composes the other way round from how it reads: the
    last call is the first thing done to the point.)
    """
    from PyQt6.QtGui import QTransform
    transform = QTransform()
    transform.rotate(degrees)
    transform.scale(1.0, factor)
    return transform


def _ellipse(radius: float) -> QPainterPath:
    """A circle on the origin — the pivot everything head-side shares."""
    return _disc(0.0, 0.0, radius)


def _disc(x: float, y: float, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addEllipse(QRectF(x - radius, y - radius, radius * 2, radius * 2))
    return path


def _oval(x: float, y: float, w: float, h: float) -> QPainterPath:
    path = QPainterPath()
    path.addEllipse(QRectF(x, y, w, h))
    return path
