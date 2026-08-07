# ABOUTME: Shared Qt building blocks for stage fixtures — the designer mixin,
# ABOUTME: the constant-size label, the glow primitive and the renderer base.
"""Pieces every stage renderer is built from.

`FixtureLabel`, `DesignableFixture` and `GlowingEllipse` moved here unchanged
when rendering became category-driven; `BodyFixture` is the new base that turns
a DMX slice into a `FixtureState` so subclasses only decide how to draw it.
"""

import math
from typing import Tuple

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtWidgets import (
    QGraphicsEllipseItem, QGraphicsItem, QGraphicsObject,
)
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QPainter, QPen, QRadialGradient,
)

from core.fixture_library import FixtureDef, all_capability_types
from core.fixture_state import FixtureState, resolve_state
from stage.atmosphere import DEFAULT_ATMOSPHERE
from stage.clock import CLOCK
from stage.features.base import build_features
from stage.frame import Frame
from stage.geometry import Aim
from stage.light import LightList
from stage.optics import BeamKind, beam_kind, resting_half_angle

# Stage coordinates are background-image pixels (see stage/controller.py), so
# a real-world size needs a scale to become one. Anchored on the LED bar: 1000mm
# tall renders 400px, the height its bespoke renderer used.
MM_PER_STAGE_PX = 2.5
DEFAULT_BODY_MM = (200.0, 200.0, 200.0)   # for a fixture that declares no size
MIN_BODY_PX, MAX_BODY_PX = 12.0, 640.0

# Capability types that put something in the air.
_FOG_CAPABILITIES = frozenset({"Fog", "FogOutput", "FogType"})
DEFAULT_BODY_SPACING_PX = 175             # between bodies of one multi-body fixture

# Pixels spaced this much further apart than they are wide aren't a row of
# cells behind one lens; they are separate housings on one DMX slice.
SEPARATE_BODY_RATIO = 1.5


def body_size(fdef: FixtureDef) -> Tuple[float, float]:
    """On-stage size of one body, in scene units.

    A `stage.size_px` hint in the sidecar wins outright; otherwise the fixture's
    real dimensions are scaled down and clamped to something visible.
    """
    override = fdef.stage.get("size_px")
    if isinstance(override, (list, tuple)) and len(override) == 2:
        return float(override[0]), float(override[1])
    dimensions = fdef.physical.dimensions_mm or DEFAULT_BODY_MM
    return tuple(max(MIN_BODY_PX, min(MAX_BODY_PX, mm / MM_PER_STAGE_PX))
                 for mm in dimensions[:2])


def body_count(fdef: FixtureDef) -> int:
    """How many physical bodies share this fixture's DMX slice.

    Almost always one. A matrix is a grid of cells *inside* a housing — OFL's
    X axis is where a pixel sits across the fixture's face, not a fixture of
    its own — so a 24-pixel bar is one bar, and a 5x5 flower is one flower.

    The exception is a set sold as several units on a single slice (the LED
    bar's eight bars). That shows up in the fixture's own physical data, as
    pixels spaced far wider apart than a pixel is wide, so it is read from
    there rather than assumed of every matrix.
    """
    override = fdef.stage.get("bodies")
    if isinstance(override, int) and override > 0:
        return override
    if not fdef.matrix:
        return 1
    spacing = fdef.physical.pixel_spacing_mm
    pixel = fdef.physical.pixel_dimensions_mm
    if (spacing and pixel and pixel[0] > 0
            and spacing[0] > pixel[0] * SEPARATE_BODY_RATIO):
        return max(1, fdef.matrix.pixel_count[0])
    return 1


def body_spacing(fdef: FixtureDef) -> int:
    """Gap between the bodies of a multi-body fixture, in scene units."""
    override = fdef.stage.get("body_spacing")
    if isinstance(override, (int, float)) and override > 0:
        return int(override)
    spacing = fdef.physical.pixel_spacing_mm
    if spacing and spacing[0] > 0:
        return int(spacing[0] / MM_PER_STAGE_PX)
    return DEFAULT_BODY_SPACING_PX


class FixtureLabel(QGraphicsItem):
    """Constant-size name + DMX chip shown under a fixture in edit mode.

    Uses ItemIgnoresTransformations so it stays a fixed on-screen size (and
    legible) regardless of the stage's fit scale. Lives as a top-level scene
    item rather than a child of the fixture, so the LED bar's drop-shadow glow
    effect doesn't blur it; its position is kept in sync by the fixture's
    itemChange hook."""

    _PAD_X = 6
    _PAD_Y = 3
    _GAP = 6  # gap below the fixture's bottom edge, in device pixels

    def __init__(self):
        super().__init__()
        self._text = ""
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setZValue(100)
        self._font = QFont()
        self._font.setPixelSize(11)

    def set_text(self, text: str):
        if text != self._text:
            self.prepareGeometryChange()
            self._text = text
            self.update()

    def _box(self) -> QRectF:
        fm = QFontMetrics(self._font)
        w = fm.horizontalAdvance(self._text) + 2 * self._PAD_X
        h = fm.height() + 2 * self._PAD_Y
        return QRectF(-w / 2, self._GAP, w, h)  # centred under the anchor, below it

    def boundingRect(self) -> QRectF:
        return self._box()

    def paint(self, painter, option, widget):
        if not self._text:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = self._box()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(25, 25, 25, 230))
        painter.drawRoundedRect(box, 4, 4)
        painter.setFont(self._font)
        painter.setPen(QColor(235, 235, 235))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self._text)


class DesignableFixture:
    """Mixin adding scenario-designer behaviour to a fixture render object:
    optional move/select (gated by design mode), a position write-back on
    drag-release, an edit-mode outline, and a name/DMX label. Mixed in BEFORE
    QGraphicsObject so super() reaches Qt.

    Persistence follows the timeline's convention: position changes are visual
    during the drag and only committed on mouseRelease (via the move callback).
    """

    # Outline colours (RGB): every fixture gets a thin edit-mode border; the
    # selected one a thicker, lighter-blue border.
    _EDIT_OUTLINE = (60, 130, 230)
    _SELECT_OUTLINE = (120, 200, 255)

    def init_design(self):
        # Called from each fixture __init__ after the Qt object is constructed.
        self._instance = None          # config.FixtureInstance back-reference
        self._bar_index = None         # set for the bars of a multi-bar ledbar
        self._move_callback = None     # callable(render_obj) invoked on release
        self._design_mode = False
        self._label = None             # FixtureLabel (top-level scene item)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)

    def set_design_mode(self, enabled: bool):
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, enabled)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, enabled)
        self._design_mode = enabled
        if self._label is not None:
            self.refresh_label()
            self._label.setVisible(enabled and self._is_label_owner())
        self.update()

    # -- name/DMX label -------------------------------------------------
    def _is_label_owner(self) -> bool:
        """Only the first bar of a multi-bar ledbar carries a label (the others
        share the same instance, so they'd be duplicates)."""
        return self._bar_index in (None, 0)

    def attach_label(self, scene):
        """Create the fixture's label as a top-level scene item."""
        self._label = FixtureLabel()
        scene.addItem(self._label)
        self._update_label_pos()
        self.refresh_label()
        self._label.setVisible(self._design_mode and self._is_label_owner())

    def remove_label(self, scene):
        if self._label is not None:
            scene.removeItem(self._label)
            self._label = None

    def refresh_label(self):
        """Sync the label text with the fixture's instance (name + DMX address)."""
        if self._label is None:
            return
        inst = self._instance
        if inst is None or not self._is_label_owner():
            self._label.set_text("")
            return
        name = inst.name or inst.type
        self._label.set_text(f"{name} · {inst.dmx_start}")

    def _update_label_pos(self):
        if self._label is None:
            return
        br = self.boundingRect()
        self._label.setPos(self.pos().x() + br.center().x(),
                           self.pos().y() + br.bottom())

    def itemChange(self, change, value):
        if (change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged
                and self._label is not None):
            self._update_label_pos()
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if (self._move_callback is not None
                and self.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable):
            self._move_callback(self)

    def paint_selection(self, painter):
        """Outline the fixture: a thin edit-mode border on every fixture, a
        thicker selection border on the selected one. Called from each fixture's
        paint() (their own paint is otherwise a no-op)."""
        if self.isSelected():
            rgb, width = self._SELECT_OUTLINE, 8
        elif self._design_mode:
            rgb, width = self._EDIT_OUTLINE, 4
        else:
            return
        pen = QPen(QColor(*rgb), width)
        pen.setCosmetic(True)  # constant on-screen width regardless of fit scale
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.boundingRect().adjusted(1, 1, -1, -1))


#: Peak alpha at the centre of a halo, before the lamp's own intensity.
GLOW_PEAK_ALPHA = 180

#: The classic falloff, as (position, alpha): every glowing thing on this stage
#: has faded this way since before there were beams — a wide flat core out to an
#: eighth, then roughly (1-t)² to nothing at the rim. The numbers are the ones
#: that were written out stop by stop, so nothing on the stage moves.
SHARP_GLOW = ((0.0, 180), (0.13, 130), (0.25, 90), (0.5, 40), (1.0, 0))


def _diffuse_glow(stops=14):
    """A tight core in a long, soft halo — the shape of a lens, not a ball.

    The flat core and steep shoulder above are what give a small halo a findable
    edge: past the shoulder there is almost nothing left, so the eye reads the
    shoulder as the rim of a disc. This is the GPU's own POINT falloff instead
    (`shaders.LIGHT_FRAG`: an inverse-square halo with a hot core, windowed out
    at the quad's edge), sampled into gradient stops — so the raster lens and
    the shader's agree on more than they used to, and the halo fades over its
    whole radius rather than ending somewhere inside it.
    """
    def alpha(t):
        halo = 1.0 / (1.0 + 24.0 * t * t)
        core = 0.6 * math.exp(-40.0 * t * t)
        window = 1.0 - _smoothstep(0.75, 1.0, t)
        # 1.6 is the value at t = 0, so the core is GLOW_PEAK_ALPHA like the
        # sharp profile's and only the shape around it differs.
        return GLOW_PEAK_ALPHA * (halo + core) * window / 1.6

    return tuple((index / stops, alpha(index / stops))
                 for index in range(stops + 1))


def _smoothstep(low, high, t):
    x = max(0.0, min(1.0, (t - low) / (high - low)))
    return x * x * (3.0 - 2.0 * x)


DIFFUSE_GLOW = _diffuse_glow()


class GlowingEllipse(QGraphicsEllipseItem):
    """Custom QGraphicsEllipseItem with shader-like radial gradient glow effect"""

    def __init__(self, x, y, width, height, parent=None):
        super().__init__(x, y, width, height, parent)
        self.glow_color = QColor("darkgrey")
        self.glow_intensity = 0.0
        self.glow_radius_multiplier = 20.0  # How far the glow extends beyond the ellipse

    #: False once the GPU layer owns the light: the bulb still draws, so an
    #: unlit fixture looks like itself, but the glow is left to the shader
    #: rather than painted twice — once alpha-stacked here, once added there.
    draw_glow = True

    #: Which falloff this lamp fades with. A lens sets it: its halo is small
    #: enough that the classic curve's shoulder reads as the edge of a ball.
    glow_profile = SHARP_GLOW

    def setGlowColor(self, color, intensity=1.0):
        """Set the glow color and intensity (0.0-1.0)"""
        self.glow_color = color
        self.glow_intensity = max(0.0, min(1.0, intensity))
        self.update()  # Trigger repaint

    def set_glow_radius(self, multiplier):
        """Resize the halo. The one path that may change it after construction.

        A moving head's lens blooms as its beam turns toward the viewer, so this
        moves between frames — and it is the bounding rect, which Qt caches.
        Without `prepareGeometryChange` a halo that shrank left its old self
        painted on the scene until something else happened to repaint that
        patch.
        """
        if abs(multiplier - self.glow_radius_multiplier) < 1e-3:
            return
        self.prepareGeometryChange()
        self.glow_radius_multiplier = multiplier
        self.update()


    def boundingRect(self):
        """Expand bounding rect to include glow area"""
        rect = super().boundingRect()
        glow_expansion = self.rect().width() * (self.glow_radius_multiplier - 1) / 2
        return rect.adjusted(-glow_expansion, -glow_expansion, glow_expansion, glow_expansion)
        
    def paint(self, painter, option, widget):
        """Custom paint method with radial gradient glow"""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        ellipse_rect = self.rect()
        center_x = ellipse_rect.center().x()
        center_y = ellipse_rect.center().y()
        bulb_radius = ellipse_rect.width() / 2
        glow_radius = bulb_radius * self.glow_radius_multiplier
        
        if self.glow_intensity > 0 and not self.draw_glow:
            # The GPU layer owns the halo, but this is still the lit face of
            # the lamp — drawing it dark would switch the fixture off.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(self.glow_color))
            painter.drawEllipse(ellipse_rect)
        elif self.glow_intensity > 0:

            # Create smooth radial gradient for seamless glow
            gradient = QRadialGradient(center_x, center_y, glow_radius)
            for position, alpha in self.glow_profile:
                colour = QColor(self.glow_color)
                colour.setAlpha(int(alpha * self.glow_intensity))
                gradient.setColorAt(position, colour)

            # Paint the entire glow with smooth gradient
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(gradient))
            glow_diameter = int(glow_radius * 2)
            painter.drawEllipse(
                int(center_x - glow_radius), int(center_y - glow_radius),
                glow_diameter, glow_diameter
            )
        else:
            # No glow - just dark grey bulb
            painter.setBrush(QBrush(QColor(40, 40, 40)))
            painter.setPen(self.pen())
            painter.drawEllipse(ellipse_rect)


def declared_cell_count(fdef: FixtureDef) -> int:
    """How many separately-lit cells this fixture's *definition* implies.

    The resolved state only knows once DMX has arrived, but a body has to be
    built before that — so this reads a declared cell count, else the most times
    any one colour repeats across the channels.
    """
    declared = fdef.stage.get("cells")
    if isinstance(declared, int) and declared > 0:
        return declared
    repeats: dict = {}
    for channel in fdef.channels:
        for capability in channel.capabilities:
            if capability.type == "ColorIntensity":
                colour = str(capability.get("color", ""))
                repeats[colour] = repeats.get(colour, 0) + 1
                break
    return max(repeats.values(), default=1)


class BodyFixture(DesignableFixture, QGraphicsObject):
    """Base for every stage renderer: one drawable body of one patched fixture.

    Owns the geometry, the DMX -> FixtureState step, the selection border and
    the designer label, and runs the two-stage pipeline every profile shares:

        reset the beam -> let each capability feature shape it -> hand it to the
        emission to draw -> repaint the chassis.

    A *profile* (the subclass) contributes only three things: the housing items,
    which emission model draws its light, and how the chassis itself moves. It
    never reads a capability — that is what the features are for, which is why a
    gobo works on any fixture that declares one and on no fixture that doesn't.

    A fixture with several bodies (an LED bar's bars) gets one instance per
    body, all fed the same DMX slice — they mirror the same channels and differ
    only in `body_index`.
    """

    #: Features to attach whether or not the fixture declares their capability
    #: types — a moving head aims even if it only has a Tilt channel.
    forced_features: Tuple[str, ...] = ()

    #: What this profile's fixtures throw when their definition declares no
    #: optics at all. The document has no answer, so the body shape is the only
    #: evidence left: a moving head that emits nothing is nonsense, while an
    #: unlensed lamp is far more likely to be a panel than a spot.
    beam_fallback: BeamKind = BeamKind.NONE

    #: True when the chassis itself moves between DMX frames — a fan's blades,
    #: a yoke still slewing. Emissions and features declare their own.
    profile_animates: bool = False

    #: True when this profile's lamp is the lens a beam leaves through rather
    #: than a light in its own right. A head puts everything it makes down its
    #: beam, so its lens only flares — it does not wash the stage — until the
    #: beam turns toward the viewer, when that flare blooms into what looks
    #: like one (see `emission/cone.lens_glow_scale`). Ignored on a body that
    #: throws no shaft, where the lamp IS the light.
    lamp_is_lens: bool = False

    #: What kind of fog this profile makes when the definition declares no OFL
    #: `fogType`. Empty for everything that is not a fog machine — and note the
    #: fallback is only reached when the fixture itself is silent, so a mover
    #: with a hazer built in still says what it is putting out.
    default_fog_kind: str = ""

    def __init__(self, fdef: FixtureDef, x: float, y: float, *,
                 body_index: int = 0):
        super().__init__()
        self.setPos(x, y)
        self.fdef = fdef
        self.body_index = body_index
        self.dmx_channel_count = fdef.channel_count
        self.width, self.height = body_size(fdef)
        self.state = FixtureState()
        self.atmosphere = DEFAULT_ATMOSPHERE
        self.aim = Aim(fdef)
        self.default_half_angle = resting_half_angle(
            fdef, type(self).beam_kind(fdef))
        self.init_design()

        self.build_housing()
        self.emission = self.emission_for(fdef)(self)
        self.features = build_features(fdef, self)
        self._frame = Frame(state=self.state, body=self)

        # The same frame, described for the GPU layer instead of drawn: linear
        # float, unbounded intensity, body-local. Refilled in place every
        # render; nothing reads it unless a light layer is attached.
        self.lights = LightList()
        self._light_sink = None

        # A fixture that fills the air changes what every OTHER fixture's beam
        # looks like, so it has to publish that however it is categorised.
        self._makes_fog = bool(all_capability_types(fdef) & _FOG_CAPABILITIES)

        # Only a body with something moving in it keeps the clock running.
        self._animates = (self.emission.animates or self.profile_animates
                          or any(f.animates for f in self.features))
        self._clock_token = (CLOCK.subscribe(self._on_tick)
                             if self._animates else None)

    # -- geometry ------------------------------------------------------
    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.width, self.height)

    def paint(self, painter, option, widget):
        # Child items draw the fixture itself; we only add the edit-mode border.
        self.paint_selection(painter)

    def aim_origin(self) -> Tuple[float, float]:
        """Where light leaves this body, in its own coordinates."""
        return self.width / 2.0, self.height

    def cell_count(self) -> int:
        return declared_cell_count(self.fdef)

    # -- profile hooks -------------------------------------------------
    def build_housing(self) -> None:
        """Create the chassis items. Called once, at construction."""

    @classmethod
    def beam_kind(cls, fdef: FixtureDef) -> BeamKind:
        """What kind of light this fixture throws — no shaft, a wash or a beam.

        The classifier reads the fixture; the profile only supplies what to
        assume when the fixture says nothing.
        """
        return beam_kind(fdef, cls.beam_fallback)

    @classmethod
    def emission_for(cls, fdef: FixtureDef):
        """Which emission model draws this fixture's light."""
        from stage.emission.base import NoEmission
        return NoEmission

    def paint_housing(self, frame: Frame) -> None:
        """Repaint the chassis — a yoke that turned, a mirror that tilted."""

    # -- the pipeline --------------------------------------------------
    def set_state(self, state: FixtureState) -> None:
        """Adopt a resolved state and redraw.

        Separate from `update_dmx` so the stage can resolve once per fixture and
        share the result across its bodies, which all see the same slice.
        """
        self.state = state
        self.render()

    def update_dmx(self, data) -> None:
        """Feed this body a DMX slice. Unchanged contract from the stage."""
        if len(data) < self.dmx_channel_count:
            return
        self.set_state(resolve_state(self.fdef, data))

    def render(self) -> None:
        """Run the feature chain and repaint."""
        frame = self._frame
        frame.state = self.state
        frame.haze = self.atmosphere.level
        frame.t = CLOCK.now()
        frame.beam.reset(self.default_half_angle)

        for feature in self.features:
            feature.apply(frame)
        frame.beam.visible = frame.beam.level > 0

        if self._makes_fog:
            self.atmosphere.contribute(
                id(self), self.state.atmosphere.output,
                self.state.atmosphere.kind or self.default_fog_kind)
        self.emission.render(frame)
        self.paint_housing(frame)

        self.lights.clear()
        self.emission.emit_lights(frame, self.lights)
        if self._light_sink is not None:
            self._light_sink()

    def apply_state(self, state: FixtureState) -> None:
        """Adopt a state and redraw. The pre-pipeline name for `set_state`."""
        self.set_state(state)

    def set_light_on_gpu(self, on_gpu: bool) -> None:
        """Hand the light over to the GPU layer, or take it back.

        Only the light moves. The chassis, the unlit bulbs and the dark cells
        keep being drawn here, because they are the fixture rather than what it
        emits — and because hit-testing, selection and the designer all still
        run on these items.
        """
        self.emission.set_raster_light_visible(not on_gpu)
        self.render()

    def set_atmosphere(self, atmosphere) -> None:
        """Join a stage's air. Bodies with no stage keep the shared default."""
        self.atmosphere = atmosphere

    def _on_tick(self) -> None:
        self.render()

    def cleanup(self) -> None:
        """Release everything this body holds, before it leaves the scene.

        The back-references matter as much as the timers. A body points at its
        emission and its features, and both point back at it — a cycle, which
        means the body would survive until a *cyclic* collection ran, at some
        arbitrary later moment. Freeing a QGraphicsObject whose children Qt has
        already deleted, in the middle of some unrelated Qt call, aborts the
        process. Dropping the back-references lets it go by refcount instead,
        the moment the scene lets go of it.
        """
        if self._clock_token is not None:
            CLOCK.unsubscribe(self._clock_token)
            self._clock_token = None
        self.atmosphere.release(id(self))

        # The sink points back at the stage, so it is a back-reference like the
        # ones below: left in place it would keep a removed body reachable.
        self._light_sink = None
        self.lights.clear()

        self.emission.cleanup()
        for feature in self.features:
            feature.cleanup()
            feature.body = None
        self.features = []
        self.emission.body = None
        self._frame.body = None
        self._frame.state = None

    # -- helpers for profiles and emissions ----------------------------
    def intensity_factor(self) -> float:
        """How brightly to draw this body, 0..1."""
        return self.state.intensity

    @staticmethod
    def clamped_color(rgb, factor: float) -> QColor:
        """`rgb` scaled by `factor`, clamped per channel.

        QColor rejects out-of-range components and renders as nothing, so a
        colour mixed past full must be clamped rather than passed through.
        """
        return QColor(*(max(0, min(255, int(c * factor))) for c in rgb))
