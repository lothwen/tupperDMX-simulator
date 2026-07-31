# ABOUTME: Shared Qt building blocks for stage fixtures — the designer mixin,
# ABOUTME: the constant-size label, the glow primitive and the renderer base.
"""Pieces every stage renderer is built from.

`FixtureLabel`, `DesignableFixture` and `GlowingEllipse` moved here unchanged
when rendering became category-driven; `BodyFixture` is the new base that turns
a DMX slice into a `FixtureState` so subclasses only decide how to draw it.
"""

from typing import Tuple

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtWidgets import (
    QGraphicsEllipseItem, QGraphicsItem, QGraphicsObject,
)
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QPainter, QPen, QRadialGradient,
)

from core.fixture_library import FixtureDef
from core.fixture_state import FixtureState, resolve_state

# Stage coordinates are background-image pixels (see ui/stage_controller.py), so
# a real-world size needs a scale to become one. Anchored on the LED bar: 1000mm
# tall renders 400px, the height its bespoke renderer used.
MM_PER_STAGE_PX = 2.5
DEFAULT_BODY_MM = (200.0, 200.0, 200.0)   # for a fixture that declares no size
MIN_BODY_PX, MAX_BODY_PX = 12.0, 640.0
DEFAULT_BODY_SPACING_PX = 175             # between bodies of one multi-body fixture


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

    A matrix's X extent: the LED bar's eight bars all mirror the same seven
    channels and differ only in which column of an effect they show.
    """
    return max(1, fdef.matrix.pixel_count[0]) if fdef.matrix else 1


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


class GlowingEllipse(QGraphicsEllipseItem):
    """Custom QGraphicsEllipseItem with shader-like radial gradient glow effect"""
    
    def __init__(self, x, y, width, height, parent=None):
        super().__init__(x, y, width, height, parent)
        self.glow_color = QColor("darkgrey")
        self.glow_intensity = 0.0
        self.glow_radius_multiplier = 20.0  # How far the glow extends beyond the ellipse
        
    def setGlowColor(self, color, intensity=1.0):
        """Set the glow color and intensity (0.0-1.0)"""
        self.glow_color = color
        self.glow_intensity = max(0.0, min(1.0, intensity))
        self.update()  # Trigger repaint
        
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
        
        if self.glow_intensity > 0:
            
            # Create smooth radial gradient for seamless glow
            gradient = QRadialGradient(center_x, center_y, glow_radius)
            
            # Core - full intensity
            core_color = QColor(self.glow_color)
            core_color.setAlpha(int(220 * self.glow_intensity))
            gradient.setColorAt(0.0, core_color)
            
            # Inner bright area
            inner_color = QColor(self.glow_color)
            inner_color.setAlpha(int(180 * self.glow_intensity))
            gradient.setColorAt(0.0, inner_color)
            
            # Mid glow
            mid_color = QColor(self.glow_color)
            mid_color.setAlpha(int(130 * self.glow_intensity))
            gradient.setColorAt(0.13, mid_color)
            
            # Outer glow
            outer_color = QColor(self.glow_color)
            outer_color.setAlpha(int(90 * self.glow_intensity))
            gradient.setColorAt(0.25, outer_color)
            
            # Far glow
            far_color = QColor(self.glow_color)
            far_color.setAlpha(int(40 * self.glow_intensity))
            gradient.setColorAt(0.5, far_color)
            
            # Edge - fade to transparent
            edge_color = QColor(self.glow_color)
            edge_color.setAlpha(0)
            gradient.setColorAt(1.0, edge_color)
            
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


class BodyFixture(DesignableFixture, QGraphicsObject):
    """Base for every stage renderer: one drawable body of one patched fixture.

    Owns the geometry, the DMX -> FixtureState step, the selection border and
    the designer label. Subclasses build their child items in `build_body()` and
    repaint them in `apply_state()`; nothing below this line knows what kind of
    light it is.

    A fixture with several bodies (an LED bar's bars) gets one instance per
    body, all fed the same DMX slice — they mirror the same channels and differ
    only in `body_index`.
    """

    def __init__(self, fdef: FixtureDef, x: float, y: float, *,
                 body_index: int = 0, instance=None):
        super().__init__()
        self.setPos(x, y)
        self.fdef = fdef
        self.body_index = body_index
        self.instance = instance
        self.dmx_channel_count = fdef.channel_count
        self.width, self.height = body_size(fdef)
        self.state = FixtureState()
        self.init_design()
        self.build_body()

    # -- geometry ------------------------------------------------------
    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.width, self.height)

    def paint(self, painter, option, widget):
        # Child items draw the fixture itself; we only add the edit-mode border.
        self.paint_selection(painter)

    # -- subclass hooks ------------------------------------------------
    def build_body(self) -> None:
        """Create the child items. Called once, at construction."""

    def apply_state(self, state: FixtureState) -> None:
        """Repaint the child items for a new state."""
        raise NotImplementedError

    def cleanup(self) -> None:
        """Release anything that keeps ticking (timers, movies) before removal."""

    # -- DMX -----------------------------------------------------------
    def update_dmx(self, data) -> None:
        """Feed this body a DMX slice. Unchanged contract from the stage."""
        if len(data) < self.dmx_channel_count:
            return
        self.state = resolve_state(self.fdef, data)
        self.apply_state(self.state)

    # -- helpers for subclasses ----------------------------------------
    def intensity_factor(self) -> float:
        """How brightly to draw this body, 0..1 before any renderer overdrive."""
        return self.state.intensity

    @staticmethod
    def clamped_color(rgb, factor: float) -> QColor:
        """`rgb` scaled by `factor`, clamped per channel.

        QColor rejects out-of-range components and renders as nothing, so an
        overdriven colour must be clamped rather than passed through.
        """
        return QColor(*(max(0, min(255, int(c * factor))) for c in rgb))


class StrobeGate:
    """Flashes a fixture on and off while its shutter is strobing.

    Drives a plain on/off flag from a timer rather than animating opacity: a
    strobe reads as hard flashes, and the renderer just multiplies its intensity
    by `factor()`. Owned by a renderer, which must call `stop()` from cleanup so
    a removed fixture stops ticking.
    """

    MIN_HZ, MAX_HZ = 0.5, 25.0

    def __init__(self, on_toggle):
        from PyQt6.QtCore import QTimer
        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)
        self._on_toggle = on_toggle
        self._lit = True
        self._hz = None

    def apply_state(self, state: FixtureState) -> None:
        strobing = state.shutter == "Strobe" and bool(state.strobe_hz)
        hz = max(self.MIN_HZ, min(self.MAX_HZ, state.strobe_hz)) if strobing else None
        if hz == self._hz:
            return
        self._hz = hz
        if hz is None:
            self._timer.stop()
            self._lit = True
            return
        # Two timer ticks per flash cycle: one on, one off.
        self._timer.start(max(1, int(500.0 / hz)))

    def factor(self) -> float:
        return 1.0 if self._lit else 0.0

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        self._lit = not self._lit
        self._on_toggle()
