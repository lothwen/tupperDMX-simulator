# ABOUTME: The features that shape the beam — zoom, iris, frost, prism — and
# ABOUTME: the ones that aim it: pan, tilt and their continuous cousins.
"""Shaping and aiming.

Everything here modifies a beam that some emission is going to draw. A fixture
with no beam still gets the features (its channels are real), they just have
nothing visible to act on — which is the right outcome for, say, an LED bar
that inexplicably declares a zoom channel.
"""

from typing import Set

from stage.features.base import Feature, register_feature
from stage.frame import Frame
from stage.geometry import Slew, Spinner


@register_feature
class ZoomFeature(Feature):
    """A live zoom channel overrides the fixture's resting lens angle."""

    types = frozenset({"Zoom", "BeamAngle"})
    order = 40

    def apply(self, frame: Frame) -> None:
        zoom = frame.state.optics.zoom_deg
        if zoom is not None:
            # OFL states the full spread; the cone is drawn from its half-angle.
            frame.beam.half_angle_deg = max(1.0, zoom) / 2.0


@register_feature
class IrisFeature(Feature):
    """The iris pinches the beam down without changing where it points."""

    types = frozenset({"Iris", "IrisEffect"})
    order = 50
    animates = True

    def apply(self, frame: Frame) -> None:
        optics = frame.state.optics
        aperture = optics.iris
        name, speed = optics.iris_effect
        if name and speed:
            # A pulsing iris breathes between its setting and fully closed.
            import math
            aperture *= 0.5 + 0.5 * math.sin(2 * math.pi * speed * frame.t)
        frame.beam.aperture = max(0.02, min(1.0, aperture))


@register_feature
class FrostFeature(Feature):
    """Frost softens the edge and spreads the beam out."""

    types = frozenset({"Frost", "FrostEffect"})
    order = 55

    def apply(self, frame: Frame) -> None:
        frame.beam.softness = max(0.0, min(1.0, frame.state.optics.frost))


@register_feature
class PrismFeature(Feature):
    """A prism splits the beam into N copies arranged around its axis."""

    types = frozenset({"Prism", "PrismRotation"})
    order = 70
    animates = True

    # More than this and the copies overlap into a smear anyway.
    MAX_FACETS = 8

    def __init__(self, body):
        super().__init__(body)
        self._spinner = Spinner()

    def apply(self, frame: Frame) -> None:
        optics = frame.state.optics
        facets = min(self.MAX_FACETS, optics.prism_facets)
        if facets < 2:
            frame.beam.copies = (0.0,)
            return
        angle = (optics.prism_angle_deg if optics.prism_angle_deg is not None
                 else self._spinner.advance(optics.prism_spin_hz, frame.t))
        step = 360.0 / facets
        frame.beam.copies = tuple(angle + index * step for index in range(facets))


@register_feature
class BladeFeature(Feature):
    """Framing shutters cut the beam's edges off."""

    types = frozenset({"BladeInsertion", "BladeRotation", "BladeSystemRotation"})
    order = 80

    def apply(self, frame: Frame) -> None:
        frame.beam.blades = frame.state.optics.blades


@register_feature
class PanTiltFeature(Feature):
    """Where the fixture is pointing, in the plane of the stage image.

    Slewed rather than snapped: the DMX arrives at 20 Hz, and a head that
    teleported between frames would look like a strobe rather than a move. The
    fixture's own PanTiltSpeed sets the rate when it declares one.
    """

    types = frozenset({"Pan", "Tilt", "PanContinuous", "TiltContinuous",
                       "PanTiltSpeed", "BeamPosition"})
    order = 90
    animates = True

    def __init__(self, body):
        super().__init__(body)
        self._axis = Slew()
        self._foreshorten = Slew()
        self._pan_spin = Spinner()
        self._tilt_spin = Spinner()

    def apply(self, frame: Frame) -> None:
        motion = frame.state.motion
        axis, foreshorten = self.body.aim.solve(motion,
                                                frame.state.optics.beam_offset_deg)

        # A head in continuous rotation has no position to slew toward; it just
        # keeps turning.
        if motion.pan_spin_hz:
            axis += self._pan_spin.advance(motion.pan_spin_hz, frame.t)
        if motion.tilt_spin_hz:
            foreshorten = abs(_cos_deg(
                self._tilt_spin.advance(motion.tilt_spin_hz, frame.t)))
            foreshorten = max(0.15, foreshorten)

        tau = _move_tau(motion)
        frame.beam.axis_deg = self._axis.advance(axis, frame.t, tau)
        frame.beam.foreshorten = self._foreshorten.advance(foreshorten, frame.t, tau)


def _move_tau(motion):
    """How long this fixture takes to reach a new position, in seconds."""
    if motion.move_time_s:
        return motion.move_time_s
    if motion.move_speed_hz:
        return 1.0 / max(0.05, motion.move_speed_hz)
    return None


def _cos_deg(degrees: float) -> float:
    import math
    return math.cos(math.radians(degrees))
