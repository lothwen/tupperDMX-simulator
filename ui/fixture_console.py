# ABOUTME: The fixture console — a strip of channel faders over a live preview
# ABOUTME: drawn by the real stage render engine, shared by every scene editor.
"""One fixture's channels, as a console.

Two widgets and the arithmetic between them, extracted from the fixture
browser's Scenes tab so the timeline's clip editor renders a fixture exactly the
way the library editor does — same faders, same colours, same preview:

    ┌ SIMULATED OUTPUT ─────────────────────┐
    │ intensity 100 %          <the real    │   OutputCard
    │ colour    #ff7a20         renderer>   │
    └───────────────────────────────────────┘
    ┌ CHANNEL FADERS ───────────────────────┐
    │ ▮ ▮ ▮ ▮ ▮ ▮ ▮ ▮ ▮ ▮                   │   FaderStrip
    └───────────────────────────────────────┘

Nothing here is simulated twice: `ScenePreview` builds the same renderer the
stage would (`stage.registry.renderer_for`), one body per `body_count()`, and
feeds it raw DMX — so a look previews here exactly as it will appear on stage,
animations, beam cone and strobe included.

Qt-facing only: what a channel *means* comes from `core.channel_families` and
`core.fixture_state`, never from a table here.
"""

from typing import Dict, List, Optional, Sequence, Set, Tuple

from PyQt6.QtCore import QPointF, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QLinearGradient, QPainter
from PyQt6.QtWidgets import (
    QFrame, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QScrollArea,
    QSizePolicy, QSlider, QStyle, QStyleOptionSlider, QVBoxLayout, QWidget,
)

from core import ofl
from core.channel_families import channel_family
from core.fixture_library import FixtureDef
from core.fixture_state import FixtureState, capability_at, resolve_state
from ui.fixture_chips import rgba
from stage.metrics import body_count, body_size, body_spacing
from stage.emission.base import emission_reach
from stage.registry import renderer_for

# Height of the faders scroll area — one 96 px slider plus its labels.
FADERS_HEIGHT = 196


# --- preview ------------------------------------------------------------------

class ScenePreview(QGraphicsView):
    """The stage's own renderer for a fixture, fed a scene instead of live DMX.

    Builds one body per `body_count()` exactly as the stage does, so a
    multi-body fixture (the LED bar's eight bars) previews as the row it really
    is rather than as a single strip.

    The backdrop is the card's own gradient, repainted here: the view is a child
    widget, so it has to draw what the card behind it would have drawn.
    """

    # Room around the bodies for the glow, as a fraction of ONE body — scaling
    # it off the whole row instead would shrink a wide fixture (the LED bar's
    # eight bars) into a sliver.
    _PAD_X = 0.8
    _PAD_Y = 0.3

    # Keep in step with QFrame#AddFxOutputCard's gradient, or the preview shows
    # as a block against the card it sits on.
    _BACKDROP_TOP = QColor("#1a1a1a")
    _BACKDROP_BOTTOM = QColor("#0e0e0e")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        # Same reason as the stage: this scene is torn down and rebuilt on every
        # fixture change, and the BSP index keeps dangling child-item pointers.
        self._scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)
        self.setScene(self._scene)
        self.setObjectName("AddFxPreview")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setRenderHints(QPainter.RenderHint.Antialiasing
                            | QPainter.RenderHint.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._bodies: List = []
        self._light_layer = self._attach_light_layer()

    def _attach_light_layer(self):
        """Preview the light the same way the stage draws it, when it can.

        A look is meant to preview exactly as it will appear on stage, and once
        the stage is accumulating light in HDR this is the only way that stays
        true. Gated on the stage having *succeeded*, never on the preference —
        a preview must not be the thing that discovers the driver is broken.
        """
        try:
            from stage.gpu.layer import LightLayer, stage_gpu_active
            if not stage_gpu_active():
                return None
            from PyQt6.QtOpenGLWidgets import QOpenGLWidget
            self.setViewport(QOpenGLWidget())
            return LightLayer(lambda: self._bodies)
        except Exception:
            return None

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        if self._light_layer is None:
            return
        painter.beginNativePainting()
        try:
            ratio = self.viewport().devicePixelRatio()
            width = int(self.viewport().width() * ratio)
            height = int(self.viewport().height() * ratio)
            # The preview fills its own card, so the whole viewport is fair
            # game — but it still has to be clipped to it, or the halo spills
            # over the widgets around it.
            clip = QRect(0, 0, width, height)
            if not self._light_layer.render(self._scene_to_clip(), width,
                                            height, clip=clip):
                self._light_layer = None
        finally:
            painter.endNativePainting()

    def _scene_to_clip(self):
        """Scene units to clip space, mirroring what `fitInView` set up."""
        from PyQt6.QtGui import QMatrix4x4
        view = self.viewportTransform()
        width = max(1, self.viewport().width())
        height = max(1, self.viewport().height())
        matrix = QMatrix4x4()
        matrix.translate(-1.0, 1.0)
        matrix.scale(2.0 / width, -2.0 / height)
        matrix.translate(view.dx(), view.dy())
        matrix.scale(view.m11(), view.m22())
        return matrix

    def drawBackground(self, painter, rect):
        # Anchored to the widget rather than the scene, so the fall stays the
        # same whatever the fixture on show zooms the view to.
        top = self.mapToScene(0, 0).y()
        bottom = self.mapToScene(0, self.viewport().height()).y()
        gradient = QLinearGradient(QPointF(0, top), QPointF(0, bottom))
        gradient.setColorAt(0, self._BACKDROP_TOP)
        gradient.setColorAt(1, self._BACKDROP_BOTTOM)
        painter.fillRect(rect, gradient)

    def set_fixture(self, fdef: Optional[FixtureDef]) -> None:
        """Rebuild the preview for a fixture definition (None clears it)."""
        self.clear()
        if fdef is None:
            return

        renderer = renderer_for(fdef)
        count = body_count(fdef)
        spacing = body_spacing(fdef)
        width, height = body_size(fdef)
        for index in range(count):
            body = renderer(fdef, index * spacing, 0.0, body_index=index)
            if self._light_layer is not None:
                body.set_light_on_gpu(True)
                body._light_sink = self.viewport().update
            self._scene.addItem(body)
            self._bodies.append(body)

        span = spacing * (count - 1) + width
        pad_x = width * self._PAD_X
        pad_y = height * self._PAD_Y
        # A fixture with optics throws its cone downstage, well past its body.
        below = pad_y + emission_reach(fdef)
        self._scene.setSceneRect(-pad_x, -pad_y,
                                 span + 2 * pad_x, height + pad_y + below)
        self._fit()

    def set_values(self, values: List[int]) -> None:
        """Feed every body the same DMX slice, as the stage does."""
        for body in self._bodies:
            body.update_dmx(values)

    def clear(self) -> None:
        """Stop and drop the current bodies (timers, animations and all)."""
        for body in self._bodies:
            body.cleanup()
            self._scene.removeItem(body)
        self._bodies = []
        if self._light_layer is not None:
            # The bodies are gone, so the next paint has nothing to pack; make
            # sure it repaints rather than leaving the last frame's light up.
            self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        if self._bodies:
            self.fitInView(self._scene.sceneRect(),
                           Qt.AspectRatioMode.KeepAspectRatio)


# --- faders -------------------------------------------------------------------

class FaderSlider(QSlider):
    """A vertical slider that behaves like a console fader.

    Qt's default is to page-step when the groove is clicked, which on a 96 px
    column means several clicks to cross the range. Here the whole bar is the
    handle: press anywhere and the value jumps there, then follows the drag.
    Right-click is the flash button — full, or back to zero if already full.
    """

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Vertical, parent)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        # Right-click is ours; don't let it bubble up as a context menu.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.setValue(0 if self.value() >= self.maximum() else self.maximum())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self._set_from_y(event.position().y())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self._set_from_y(event.position().y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _set_from_y(self, y: float) -> None:
        """Set the value from a y inside the widget — top is the maximum.

        The handle is centred on the cursor, so the travel is the groove minus
        one handle, exactly as a drag of the handle itself would be.
        """
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option,
            QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option,
            QStyle.SubControl.SC_SliderHandle, self)
        position = y - groove.top() - handle.height() / 2
        self.setValue(QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), int(position),
            groove.height() - handle.height(), True))


class _FaderName(QLabel):
    """The name under a fader.

    Inert by default. A channel whose capabilities name discrete choices is made
    *pickable* — and then it has to LOOK it, or the picker behind it may as well
    not exist: it gets a ▾ marker, a hand cursor, a tooltip that says what a
    click does, and a `pickable` property the stylesheet underlines.
    """

    clicked = pyqtSignal()

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._base_text = text
        self._pickable = False
        self.setProperty("pickable", "false")

    def set_pickable(self, pickable: bool, hint: str = "") -> None:
        self._pickable = pickable
        self.setText(f"{self._base_text} ▾" if pickable else self._base_text)
        self.setCursor(Qt.CursorShape.PointingHandCursor if pickable
                       else Qt.CursorShape.ArrowCursor)
        self.setToolTip(hint or self._base_text)
        self.setProperty("pickable", "true" if pickable else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event):
        if self._pickable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class ChannelFader(QWidget):
    """One channel's column: value, slider, DMX address and name.

    Coloured by the channel's family, so a strip of faders reads the same way
    the Channels tab's chips do.
    """

    valueChanged = pyqtSignal(int, int)      # channel index, value
    nameClicked = pyqtSignal(int)            # channel index

    def __init__(self, index: int, spec, address: int, parent=None):
        super().__init__(parent)
        self.index = index
        self.spec = spec
        _label, color = channel_family(spec)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.value_label = QLabel("000")
        self.value_label.setObjectName("AddFxFaderValue")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.value_label)

        self.slider = FaderSlider()
        self.slider.setObjectName("AddFxFader")
        self.slider.setRange(0, 255)
        self.slider.setFixedHeight(96)
        self.slider.setToolTip("Drag anywhere on the bar · "
                               "right-click for full, again for zero")
        # A vertical slider's add-page is the part BELOW the handle, so that is
        # what carries the fill. It is a gradient rather than a flat block so
        # the bar reads as a lit column: brightest just under the handle, the
        # family colour proper below it, deepening toward the base.
        tint = QColor(color)
        fill = (f"qlineargradient(x1:0, y1:0, x2:0, y2:1,"
                f" stop:0 {tint.lighter(118).name()},"
                f" stop:0.35 {color},"
                f" stop:1 {tint.darker(128).name()})")
        self.slider.setStyleSheet(
            f"QSlider#AddFxFader::add-page:vertical {{ background: {fill}; }}"
            f"QSlider#AddFxFader::handle:vertical {{ background: {rgba(color, 235)}; }}")
        self.slider.valueChanged.connect(self._on_changed)
        layout.addWidget(self.slider, 0, Qt.AlignmentFlag.AlignHCenter)

        self.address_label = QLabel(str(address))
        self.address_label.setObjectName("AddFxFaderAddress")
        self.address_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.address_label)

        # A fine slot carries its coarse channel's label, so name it by its own
        # key ("Pan fine") — the same way the Channels tab does.
        text = spec.field if spec.fine_of >= 0 else (spec.label or spec.field)
        self.name_label = _FaderName(text)
        self.name_label.setObjectName("AddFxFaderName")
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignHCenter
                                     | Qt.AlignmentFlag.AlignTop)
        self.name_label.setWordWrap(True)
        self.name_label.setFixedWidth(56)
        self.name_label.setToolTip(text)
        self.name_label.clicked.connect(
            lambda: self.nameClicked.emit(self.index))
        layout.addWidget(self.name_label, 1)

    def set_address(self, address: int) -> None:
        self.address_label.setText(str(address))

    def set_pickable(self, pickable: bool, hint: str = "") -> None:
        """Offer a picker on this channel's name (and show that it is there)."""
        self.name_label.set_pickable(pickable, hint)

    def set_value(self, value: int) -> None:
        """Set the fader without reporting it back as an edit."""
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)
        self.value_label.setText(f"{value:03d}")

    def value(self) -> int:
        return self.slider.value()

    def _on_changed(self, value: int) -> None:
        self.value_label.setText(f"{value:03d}")
        self.valueChanged.emit(self.index, value)


class FaderStrip(QWidget):
    """The CHANNEL FADERS card: one `ChannelFader` per channel of a fixture.

    The strip owns the rebuild, so a consumer only ever says which fixture it is
    showing and what the values are. `set_addressable_limit` exists because the
    timeline can only reach a fixture's first 23 slots (notes 1-23 are the
    channel verbs; 24 up are the scene-load verbs) while the sidecar happily
    stores every one of them — so the same widget serves both, and only the
    timeline greys the tail.
    """

    value_changed = pyqtSignal(int, int)      # channel index, 0..255
    name_clicked = pyqtSignal(int)            # channel index

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fdef: Optional[FixtureDef] = None
        self._dmx_start = 1
        self._faders: List[ChannelFader] = []
        self._limit: Optional[int] = None
        self._present: Optional[Set[int]] = None
        self._pickable: Set[int] = set()
        self._editable = True

        card = QFrame(self)
        card.setObjectName("AddFxCard")
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(card)

        outer = QVBoxLayout(card)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 14, 0)
        self.title_label = QLabel("CHANNEL FADERS")
        self.title_label.setObjectName("AddFxCardTitle")
        header.addWidget(self.title_label, 1)
        self.hint_label = QLabel("drag · right-click for full, again for zero")
        self.hint_label.setObjectName("AddFxHint")
        header.addWidget(self.hint_label)
        outer.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("AddFxScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFixedHeight(FADERS_HEIGHT)
        holder = QWidget()
        self._layout = QHBoxLayout(holder)
        self._layout.setContentsMargins(12, 10, 12, 10)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        self.scroll.setWidget(holder)
        outer.addWidget(self.scroll)

    # -- population --------------------------------------------------------
    def set_fixture(self, fdef: Optional[FixtureDef], dmx_start: int = 1,
                    title: str = "") -> None:
        """Rebuild the strip for `fdef`. `title` overrides the card caption."""
        self._fdef = fdef
        self._dmx_start = dmx_start
        self._present = None
        # Which channels offer a picker is a property of the NEW fixture, so
        # the caller re-declares it; until then nothing looks clickable.
        self._pickable = set()
        self._refresh_hint()
        self.title_label.setText(
            title or (f"CHANNEL FADERS — {fdef.mode.upper()}"
                      if fdef and fdef.mode else "CHANNEL FADERS"))
        for fader in self._faders:
            fader.setParent(None)
        self._faders = []
        if fdef is None:
            return
        for index, spec in enumerate(fdef.channels):
            fader = ChannelFader(index, spec, dmx_start + index)
            fader.valueChanged.connect(self.value_changed)
            fader.nameClicked.connect(self.name_clicked)
            self._layout.insertWidget(self._layout.count() - 1, fader)
            self._faders.append(fader)
        self._apply_enabled()

    def set_dmx_start(self, dmx_start: int) -> None:
        """Re-stamp the fader addresses against a new DMX start."""
        self._dmx_start = dmx_start
        for fader in self._faders:
            fader.set_address(dmx_start + fader.index)

    def set_values(self, values: Sequence[int]) -> None:
        """Push a DMX slice onto the faders without reporting edits back."""
        for fader in self._faders:
            fader.set_value(values[fader.index]
                            if fader.index < len(values) else 0)

    def values(self) -> List[int]:
        return [fader.value() for fader in self._faders]

    def fader(self, index: int) -> Optional[ChannelFader]:
        return self._faders[index] if 0 <= index < len(self._faders) else None

    # The strip IS its faders: it replaced a plain list, and every caller that
    # walks or indexes one keeps working.
    def __len__(self) -> int:
        return len(self._faders)

    def __iter__(self):
        return iter(self._faders)

    def __getitem__(self, index) -> ChannelFader:
        return self._faders[index]

    # -- presentation ------------------------------------------------------
    def set_editable(self, editable: bool) -> None:
        self._editable = editable
        self._apply_enabled()

    def set_addressable_limit(self, limit: Optional[int]) -> None:
        """Disable every fader at or past `limit` (None lifts the cap)."""
        self._limit = limit
        self._apply_enabled()

    def set_present(self, present: Optional[Set[int]]) -> None:
        """Dim the channels NOT in `present` — the ones a scene does not carry.
        None means "no such distinction", which is the library editor's case."""
        self._present = present
        self._apply_enabled()

    def set_pickable(self, indices: Set[int], hint: str = "") -> None:
        """Mark the channels that offer a picker on their name.

        Only these get the ▾, the hand cursor and the tooltip — a plain fader's
        name must not look clickable, and a marked one must not be the only
        thing that hints the picker exists (see also the card's hint line)."""
        self._pickable = set(indices)
        for fader in self._faders:
            fader.set_pickable(fader.index in self._pickable, hint)
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        base = "drag · right-click for full, again for zero"
        self.hint_label.setText(
            f"{base} · ▾ names open a picker" if self._pickable else base)

    def unaddressable(self) -> List[int]:
        """Indices the cap put out of reach — what a caller warns about."""
        if self._limit is None:
            return []
        return [f.index for f in self._faders if f.index >= self._limit]

    def _apply_enabled(self) -> None:
        for fader in self._faders:
            reachable = self._limit is None or fader.index < self._limit
            fader.setEnabled(self._editable and reachable)
            if not reachable:
                fader.setToolTip(
                    f"DMX slot {fader.index + 1} is out of reach from the "
                    "timeline: the channel verbs are MIDI notes 1-23.")
            absent = self._present is not None and fader.index not in self._present
            fader.name_label.setProperty("absent", "true" if absent else "false")
            fader.name_label.style().unpolish(fader.name_label)
            fader.name_label.style().polish(fader.name_label)


# --- output card --------------------------------------------------------------

class OutputCard(QWidget):
    """The SIMULATED OUTPUT card: the resolved read-out beside the live preview.

    Its own card style: this one IS the dark surface the preview draws on,
    read-out and caption included, rather than a light card with a dark panel
    dropped into it.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fdef: Optional[FixtureDef] = None

        card = QFrame(self)
        card.setObjectName("AddFxOutputCard")
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(card)

        outer = QVBoxLayout(card)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(14, 10, 14, 0)
        header.addStretch(1)
        caption = QLabel("SIMULATED OUTPUT")
        caption.setObjectName("AddFxHint")
        header.addWidget(caption)
        outer.addLayout(header)

        body = QHBoxLayout()
        body.setContentsMargins(14, 4, 8, 8)
        body.setSpacing(12)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("AddFxSceneSummary")
        self.summary_label.setAlignment(Qt.AlignmentFlag.AlignLeft
                                        | Qt.AlignmentFlag.AlignTop)
        # Only as wide as the read-out needs: the rest of the card is the
        # preview, and a beam wants room around it more than this does.
        self.summary_label.setFixedWidth(168)
        body.addWidget(self.summary_label)
        self._preview = ScenePreview()
        self._preview.setMinimumHeight(150)
        self._preview.setSizePolicy(QSizePolicy.Policy.Expanding,
                                    QSizePolicy.Policy.Expanding)
        body.addWidget(self._preview, 1)
        outer.addLayout(body, 1)

    @property
    def preview(self) -> ScenePreview:
        return self._preview

    def set_fixture(self, fdef: Optional[FixtureDef]) -> None:
        self._fdef = fdef
        self._preview.set_fixture(fdef)

    def set_values(self, values: List[int]) -> None:
        self._preview.set_values(values)
        state = (resolve_state(self._fdef, values)
                 if self._fdef is not None and values else None)
        self.summary_label.setText(
            summary_html(self._fdef, state, values) if state else "")

    def clear(self) -> None:
        """Drop the preview bodies. MUST be called before the card goes away —
        each body owns animation timers on the shared 30 Hz clock."""
        self._preview.clear()
        self.summary_label.setText("")


# --- values -------------------------------------------------------------------

def clamp(value) -> int:
    try:
        return max(0, min(255, int(value)))
    except (TypeError, ValueError):
        return 0


def default_values(fdef: Optional[FixtureDef]) -> List[int]:
    """Every channel at the default its definition declares."""
    if fdef is None:
        return []
    return [clamp(spec.default_value) for spec in fdef.channels]


def scene_values(fdef: Optional[FixtureDef],
                 scene: Optional[dict]) -> List[int]:
    """A scene as a DMX slice, filling anything it omits with the channel's
    declared default — the same fallback `resolve_state` applies."""
    if fdef is None:
        return []
    if scene is None:
        return [0] * fdef.channel_count
    return [clamp(scene.get(spec.field, spec.default_value))
            for spec in fdef.channels]


def values_to_fields(fdef: Optional[FixtureDef],
                     values: Sequence[int]) -> Dict[str, int]:
    """A DMX slice keyed back by channel field."""
    if fdef is None:
        return {}
    return {spec.field: clamp(values[index]) if index < len(values) else 0
            for index, spec in enumerate(fdef.channels)}


def fields_to_values(fdef: Optional[FixtureDef],
                     mapping: Dict[str, int]) -> List[int]:
    """The inverse: a `{field: value}` dict as a DMX slice, defaults filled in."""
    return scene_values(fdef, dict(mapping) if mapping else None)


# --- formatting ---------------------------------------------------------------

def summary_html(fdef: Optional[FixtureDef], state: FixtureState,
                 values: List[int]) -> str:
    """The SIMULATED OUTPUT read-out: what the engine resolved, in words.

    Everything comes from the resolved state, except the rows OFL keeps on the
    channel rather than in `FixtureState` (position, fog), which are read back
    off the capability covering the current value.
    """
    rows: List[Tuple[str, str]] = [
        ("intensity", f"{state.intensity * 100:.0f} %"),
        ("colour", colour_text(state)),
    ]
    rows.extend(capability_rows(fdef, values))
    rows.append(("strobe", strobe_text(state)))
    if state.effect:
        speed = (f" · {state.effect_speed_hz:.1f} Hz"
                 if state.effect_speed_hz else "")
        rows.append(("effect", f"{state.effect}{speed}"))
    if state.zoom_deg is not None:
        rows.append(("zoom", f"{state.zoom_deg:.0f}°"))

    cells = "".join(
        f"<tr><td style='color:#7a7a7a;padding-right:14px'>{escape(name)}</td>"
        f"<td style='color:#d6d6d6'>{escape(value)}</td></tr>"
        for name, value in rows)
    return f"<table cellspacing='0' cellpadding='1'>{cells}</table>"


def escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def colour_text(state: FixtureState) -> str:
    if state.color_temp_k is not None:
        return f"{state.color_temp_k:.0f} K"
    if not state.has_color:
        return "fixed / white"
    red, green, blue = state.rgb
    cells = f" · {len(state.emitters)} cells" if len(state.emitters) > 1 else ""
    return f"#{red:02x}{green:02x}{blue:02x}{cells}"


def strobe_text(state: FixtureState) -> str:
    if state.strobe_hz:
        return f"{state.shutter.lower()} · {state.strobe_hz:.1f} Hz"
    return state.shutter.lower()


# Capability types worth a read-out that FixtureState doesn't carry, and the
# entity to read them as.
_EXTRA_ROWS = (
    ("Pan", "pan", "angle", "angle", "°"),
    ("Tilt", "tilt", "angle", "angle", "°"),
    ("Fog", "fog", "", "", ""),
    ("FogOutput", "fog", "fogOutput", "percent", ""),
)


def capability_rows(fdef: Optional[FixtureDef],
                    values: List[int]) -> List[Tuple[str, str]]:
    """Read-out rows for channels whose meaning lives on the capability."""
    if fdef is None:
        return []
    found: Dict[str, str] = {}
    for index, spec in enumerate(fdef.channels):
        if spec.fine_of >= 0 or index >= len(values):
            continue
        cap = capability_at(fdef, index, values)
        if cap is None:
            continue
        for cap_type, label, key, kind, unit in _EXTRA_ROWS:
            if cap.type != cap_type or label in found:
                continue
            amount = (ofl.capability_value(cap, values[index], key, kind)
                      if key else None)
            found[label] = (f"{amount:.0f}{unit}" if amount is not None
                            else str(cap.get("comment") or "—"))

    return list(found.items())
