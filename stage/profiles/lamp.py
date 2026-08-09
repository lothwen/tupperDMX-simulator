# ABOUTME: Profiles for fixtures that are a lamp in a housing — pars, washes,
# ABOUTME: plain dimmers, and the last-resort body for anything unrecognised.
"""Lamps.

The commonest shape on any stage: one housing, one lamp, and whatever that lamp
puts in the air. A colour changer takes its hue from the resolved state; a
dimmer has no colour to take, so it glows white at whatever level its intensity
channel says. Both are the same body — which of them a fixture is only changes
what its channels happen to do, and that is the features' business, not the
profile's.

The same is true of the light itself. This one body covers a 4° beam par, a 25°
wash and a 120° soft panel, and which of those it draws comes from the fixture's
optics rather than from anything declared here: no lens tight enough or gate
fitted means no shaft, and the lamp does the whole job.
"""

from PyQt6.QtGui import QPainterPath

from core.fixture_library import FixtureDef
from stage.body import BodyFixture
from stage.emission.cone import emission_for_kind
from stage.frame import Frame
from stage.profiles import chassis
from stage.registry import register_renderer

#: Wider than this and the fixture is a batten or a soft panel rather than a
#: can, so its face is a rectangle. 210 of the 296 fixtures on this profile sit
#: inside it; a round bezel on the 436x26 bars outside it would be a dot in a
#: plank.
PANEL_ASPECT = 1.8

#: Radius of the recess the lamp sits in, in units of the body's smaller side.
#: `cone.LAMP_INSET` leaves the lamp itself 0.35 of that, so this is cut a
#: little wider on purpose: the face has to frame the lamp, not hide under it.
FACE_RADIUS = 0.40


class LampFixture(BodyFixture):
    """A housing with a glowing lamp, and whatever shaft its optics justify.

    Nothing here moves: a par is bolted where it was hung, so the housing is
    built once and only ever re-tinted. What it has to do instead is read as a
    *fixture* at a glance — the old flat rectangle could have been anything, and
    at a hundred-odd on a rig that is most of what the stage looked like.

    Three pieces: the bracket it hangs in, the can or panel, and the recessed
    face the lamp glows out of. The face is drawn but never lit here — the
    emission's lamp sits on top of it and does that — so this only has to leave
    the right hole in the right place.
    """

    def build_housing(self) -> None:
        width, height = self.width, self.height
        radius = min(width, height) * 0.09
        panel = width > height * PANEL_ASPECT

        # The bracket first, so the can is drawn over it and only the arms show
        # either side — which is what you see of a yoke from the front.
        self.bracket = chassis.ChassisPart(
            chassis.bracket(width, height), self,
            top=chassis.DARK_TOP, bottom=chassis.DARK_BOTTOM, rim=chassis.DARK_RIM)

        self.housing = chassis.ChassisPart(
            chassis.rounded(width * 0.10, height * 0.05,
                            width * 0.80, height * 0.90,
                            radius if panel else min(width, height) * 0.18), self,
            barrel=0.0 if panel else 0.40)

        if panel:
            face = chassis.rounded(width * 0.15, height * 0.22,
                                   width * 0.70, height * 0.56, radius * 0.6)
        else:
            face = _disc(width / 2.0, height / 2.0,
                         min(width, height) * FACE_RADIUS)
        self.face = chassis.ChassisPart(
            face, self, top=chassis.LENS_TOP, bottom=chassis.LENS_BOTTOM,
            rim=chassis.DARK_RIM)

        if not panel:
            # Cooling ribs down the flanks, in the gap the round face leaves.
            for side in (0.135, 0.815):
                for row in (-1, 0, 1):
                    self.housing.add_detail(
                        chassis.rounded(width * side, height * (0.47 + row * 0.13),
                                        width * 0.05, height * 0.06, radius * 0.3),
                        chassis.DARK_BOTTOM)

    def paint_housing(self, frame: Frame) -> None:
        # A par's own output spills onto the can it leaves through. Weaker than
        # a moving head's, because here the lamp IS the light and already fills
        # the face — this is the rim of the can catching it, nothing more.
        rgb, level = frame.beam.rgb, frame.beam.level
        self.housing.set_tint(rgb, level * 0.35)
        self.bracket.set_tint(rgb, level * 0.15)

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        return emission_for_kind(cls.beam_kind(fdef))

    @property
    def lamp(self):
        """The glowing element. Kept on the body: the designer, the scene
        preview and the parity tests all reach for it by name."""
        return self.emission.lamp

    @property
    def beam(self):
        """The cone, or None on a fixture that throws no shaft."""
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


def _disc(x: float, y: float, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addEllipse(x - radius, y - radius, radius * 2, radius * 2)
    return path
