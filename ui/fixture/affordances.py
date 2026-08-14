# ABOUTME: Controls that beat a plain fader for particular channels — a colour
# ABOUTME: wheel, a picker of a channel's own discrete capabilities.
"""Better controls for channels a fader describes badly.

Two of them, both chosen from what the fixture's OFL capabilities say rather
than from its name:

**ColorWheel** — dialling a colour on three or four separate faders is guesswork.
The wheel emits an RGB triplet and `core.fixture_state.solve_color` turns that
into whichever emitters the fixture actually mixes: straight through for RGB,
a greedy fit for CMY or RGBA-without-blue, `{}` for a whites-only blinder that
has no hue to set. No per-fixture code, and it inverts the same additive model
the stage renders with, so what is dialled here is what will be emitted.

**CapabilityGrid** — a channel carrying a list of discrete `Effect` /
`ColorPreset` / `WheelSlot` capabilities is a menu, not a continuum: dragging a
fader to find "Ping Pong" is absurd when the definition names it. So the channel
is laid out as a grid of tiles instead, one per capability, and **every tile
animates at once** — the whole menu is on screen playing simultaneously, and you
pick the look by eye rather than nudging a value and waiting to see what it was.
Clicking a tile writes the start of that capability's `dmxRange`.

    ┌ Animation ─────────────────────────┐
    │ ▐▌  ▐▌  ▐▌  ▐▌  ▐▌  ▐▌  ▐▌         │   each cell a live GIF,
    │ ▐▌  ▐▌  ▐▌  ▐▌  ▐▌  ▐▌  ▐▌         │   all playing together
    │ ▐▌  ▐▌  ▐▌  ▐▌ [▐▌] ▐▌  ▐▌         │   the selected one framed
    └────────────────────────────────────┘

The LED bar's `animation` channel is the case this was built for, and it used to
be hardcoded: it declares 35 `Effect` capabilities at `dmxRange` [0,3] [4,7]
[8,11] … plus a `NoFunction` tail at [140,255], which is exactly what the old
grid's `value = index * 4` over a directory listing re-derived. Its 35 shipped
GIFs still illustrate the tiles, but they are now decoration ON a data-driven
picker rather than the thing driving it. So a fixture that declares different
effects gets its own tiles, falling back to the capability's name on a plain
coloured tile where it names no artwork.

**Artwork is looked up per fixture, not by name across all of them** — through
`fixture_library.effect_asset`, which reads the sidecar's `effects` map and
resolves it against that fixture's own `<stem>.effects/` folder. The grid used
to slug-match a capability's name against one global `assets/animations/`
index, which meant any fixture declaring an effect called "Fade" silently
inherited the LED bar's GIF. That is why the grid needs the `FixtureDef` and
not just the channel spec.
"""

import math
from typing import List, Optional, Sequence, Tuple

from PyQt6.QtCore import QPointF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QMouseEvent, QMovie, QPainter, QPen,
    QPixmap, QRadialGradient,
)
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

from core import ofl
from core.channel_families import channel_family
from core.fixture_library import (
    FixtureDef, color_slots, dominant_capability, effect_asset,
)
from core.fixture_state import resolve_state, solve_color

# Capability types that name discrete choices rather than a continuum.
DISCRETE_TYPES = frozenset({"Effect", "ColorPreset", "WheelSlot",
                            "Maintenance"})
# Below this many, a fader and a read-out say it just as well.
MIN_DISCRETE = 4


# --- colour -------------------------------------------------------------------

class ColorWheel(QWidget):
    """Embedded HSV hue-saturation wheel.

    Click or drag inside the disk to pick a color. Angle = hue, radius =
    saturation. Always emits a fully-saturated color (V=1.0); value comes
    from elsewhere (the fixture's intensity channel).
    """

    color_changed = pyqtSignal(QColor)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(140, 140)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Preferred)
        self._hue = 0.0
        self._sat = 0.0
        self._wheel_pixmap: Optional[QPixmap] = None

    def set_rgb(self, r: int, g: int, b: int):
        """Move selector to match an RGB triplet (V channel ignored).

        Does NOT emit — callers use it to follow the faders, and emitting would
        feed straight back into them.
        """
        h, s, _, _ = QColor(r, g, b).getHsvF()
        if h < 0:  # achromatic — keep current hue
            self._sat = s
        else:
            self._hue, self._sat = h, s
        self.update()

    def color(self) -> QColor:
        return QColor.fromHsvF(self._hue, self._sat, 1.0)

    def resizeEvent(self, event):
        self._wheel_pixmap = None
        super().resizeEvent(event)

    def paintEvent(self, event):
        if self._wheel_pixmap is None:
            self._render_wheel()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._wheel_pixmap is not None:
            painter.drawPixmap(0, 0, self._wheel_pixmap)
        cx, cy, r = self._geometry()
        angle = self._hue * 2 * math.pi
        sx = cx + math.cos(angle) * self._sat * r
        sy = cy - math.sin(angle) * self._sat * r
        painter.setPen(QPen(Qt.GlobalColor.black, 2))
        painter.setBrush(QBrush(self.color()))
        painter.drawEllipse(QPointF(sx, sy), 6, 6)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._update_from_pos(event.position())

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._update_from_pos(event.position())

    def _geometry(self):
        cx = self.width() / 2
        cy = self.height() / 2
        r = min(self.width(), self.height()) / 2 - 6
        return cx, cy, r

    def _render_wheel(self):
        if self.width() <= 0 or self.height() <= 0:
            return
        pixmap = QPixmap(self.width(), self.height())
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy, r = self._geometry()

        # Hue: conical gradient (counterclockwise from 3 o'clock).
        hue_gradient = QConicalGradient(QPointF(cx, cy), 0.0)
        for i in range(7):
            stop = i / 6
            hue_gradient.setColorAt(stop, QColor.fromHsvF(stop % 1.0, 1.0, 1.0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(hue_gradient))
        painter.drawEllipse(QPointF(cx, cy), r, r)

        # Saturation: radial white-at-center → transparent-at-edge overlay.
        sat_gradient = QRadialGradient(QPointF(cx, cy), r)
        sat_gradient.setColorAt(0.0, QColor(255, 255, 255, 255))
        sat_gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(QBrush(sat_gradient))
        painter.drawEllipse(QPointF(cx, cy), r, r)

        # Subtle border so the disk reads against any background.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#555"), 1))
        painter.drawEllipse(QPointF(cx, cy), r, r)
        painter.end()
        self._wheel_pixmap = pixmap

    def _update_from_pos(self, pos: QPointF):
        cx, cy, r = self._geometry()
        if r <= 0:
            return
        dx = pos.x() - cx
        dy = cy - pos.y()
        hue = (math.atan2(dy, dx) / (2 * math.pi)) % 1.0
        sat = min(1.0, math.hypot(dx, dy) / r)
        self._hue, self._sat = hue, sat
        self.update()
        self.color_changed.emit(self.color())


def wheel_applies(fdef: Optional[FixtureDef]) -> bool:
    """Whether a colour wheel can drive this fixture at all.

    Answered by asking `solve_color` for a saturated hue and seeing whether it
    can place it anywhere — NOT by `color_slots`, which lists a blinder's Cold
    White and Warm White as colour slots (they are, for mixing) even though
    there is no hue to dial onto them. Offering a wheel that quietly does
    nothing is worse than not offering one.
    """
    if fdef is None or not color_slots(fdef):
        return False
    return bool(solve_color(fdef, (255, 120, 0)))


def apply_color(fdef: Optional[FixtureDef], values: Sequence[int],
                color: QColor) -> List[int]:
    """`values` with the fixture's colour emitters set to `color`.

    Everything data-driven happens in `solve_color`, which fits the requested
    light onto whatever the fixture mixes and zeroes the emitters it doesn't
    use — so a stale amber can't tint the result.
    """
    out = list(values)
    if fdef is None:
        return out
    for slot, value in solve_color(fdef,
                                   (color.red(), color.green(), color.blue())).items():
        if 0 <= slot < len(out):
            out[slot] = max(0, min(255, int(value)))
    return out


def current_color(fdef: Optional[FixtureDef],
                  values: Sequence[int]) -> Tuple[int, int, int]:
    """The hue the fixture is currently emitting, read back the way the stage
    resolves it — the inverse of `apply_color`."""
    if fdef is None or not values:
        return (0, 0, 0)
    return tuple(resolve_state(fdef, list(values)).rgb)


# --- discrete capabilities ----------------------------------------------------

class CapabilityTile(QFrame):
    """One choice on a discrete channel. Plays its GIF when one exists."""

    clicked = pyqtSignal(int)

    TILE_W = 48
    TILE_H = 72

    def __init__(self, index: int, value: int, label: str,
                 gif_path: Optional[str], color: str, parent=None):
        super().__init__(parent)
        self._index = index
        self._value = value
        self.setObjectName("AnimationTile")
        self.setFixedSize(self.TILE_W, self.TILE_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{label}\nvalue: {value}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        self._movie: Optional[QMovie] = None

        if gif_path is not None:
            visual = QLabel()
            visual.setAlignment(Qt.AlignmentFlag.AlignCenter)
            movie = QMovie(gif_path)
            src = movie.frameRect().size() if movie.isValid() else QSize(8, 40)
            if src.width() <= 0 or src.height() <= 0:
                src = QSize(8, 40)
            available_w = self.TILE_W - 4
            available_h = self.TILE_H - 4
            scale = min(available_w / src.width(), available_h / src.height())
            movie.setScaledSize(QSize(max(1, int(src.width() * scale)),
                                      max(1, int(src.height() * scale))))
            visual.setMovie(movie)
            movie.start()
            self._movie = movie          # keep the reference alive
        else:
            # No art for this one: the label IS the tile, tinted by the
            # channel's family so a strip of them still reads as one channel.
            visual = QLabel(label)
            visual.setAlignment(Qt.AlignmentFlag.AlignCenter)
            visual.setWordWrap(True)
            visual.setStyleSheet(f"color: {color}; font-size: 8px;")
        layout.addWidget(visual)

    @property
    def index(self) -> int:
        return self._index

    @property
    def value(self) -> int:
        return self._value

    def set_selected(self, selected: bool):
        # Use string property values so QSS [selected="true"] matches reliably.
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._index)


class CapabilityGrid(QWidget):
    """A picker over ONE channel's discrete capabilities.

    Tiles come from the channel's own definition — one per capability, valued
    from its `dmxRange` — so this works for any fixture that lists its effects,
    presets or wheel slots. Artwork is optional decoration, resolved from the
    fixture's own effects folder.
    """

    COLS = 7

    value_changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tiles: List[CapabilityTile] = []
        self._selected_index = -1
        self._layout = QGridLayout(self)
        self._layout.setSpacing(4)
        self._layout.setContentsMargins(0, 0, 0, 0)

    def set_channel(self, spec, fdef: Optional[FixtureDef] = None) -> None:
        """Rebuild for a channel spec. A channel with nothing discrete to show
        simply ends up empty, and the caller falls back to a fader.

        `fdef` is the fixture the channel belongs to — the only thing that can
        say which artwork illustrates its effects. Without it every tile is a
        plain label, which is also what a fixture shipping no artwork gets.
        """
        for tile in self._tiles:
            tile.setParent(None)
        self._tiles = []
        self._selected_index = -1
        if spec is None:
            return
        _family, color = channel_family(spec)
        for index, (value, label) in enumerate(capability_choices(spec)):
            art = effect_asset(fdef, label) if fdef is not None else None
            tile = CapabilityTile(index, value, label, art, color)
            tile.clicked.connect(self._on_tile_clicked)
            self._tiles.append(tile)
            self._layout.addWidget(tile, index // self.COLS, index % self.COLS)

    def __len__(self) -> int:
        return len(self._tiles)

    def set_value(self, value: int) -> None:
        """Highlight the tile whose capability covers `value`."""
        for tile in self._tiles:
            if tile.value <= int(value):
                candidate = tile.index
            else:
                break
        else:
            candidate = self._tiles[-1].index if self._tiles else -1
        if not self._tiles:
            return
        self._set_selected(max(0, candidate))

    def value(self) -> int:
        if 0 <= self._selected_index < len(self._tiles):
            return self._tiles[self._selected_index].value
        return 0

    def _set_selected(self, index: int) -> None:
        if index == self._selected_index:
            return
        if 0 <= self._selected_index < len(self._tiles):
            self._tiles[self._selected_index].set_selected(False)
        self._selected_index = index
        if 0 <= index < len(self._tiles):
            self._tiles[index].set_selected(True)

    def _on_tile_clicked(self, index: int) -> None:
        if index == self._selected_index:
            return
        self._set_selected(index)
        self.value_changed.emit(self.value())


def capability_choices(spec) -> List[Tuple[int, str]]:
    """`[(dmx value, label), ...]` for a channel's discrete capabilities.

    The value is the START of each capability's range — the lowest DMX that
    selects it, which is what an author wants written.
    """
    choices: List[Tuple[int, str]] = []
    for cap in getattr(spec, "capabilities", ()) or ():
        if cap.type == "NoFunction":
            continue
        low, _high = cap.dmx_range
        choices.append((int(low), _capability_label(cap)))
    return choices


def _capability_label(cap) -> str:
    for key in ("effectName", "effectPreset", "colorTemperature", "comment",
                "slotNumber", "maintenanceMode"):
        text = cap.get(key)
        if text:
            return str(text)
    return str(cap.type)


def grid_applies(spec) -> bool:
    """Whether a channel is better shown as a menu than as a fader."""
    if spec is None or getattr(spec, "fine_of", -1) >= 0:
        return False
    caps = [c for c in (getattr(spec, "capabilities", ()) or ())
            if c.type != "NoFunction"]
    if len(caps) < MIN_DISCRETE:
        return False
    dominant = dominant_capability(tuple(caps))
    return dominant is not None and dominant.type in DISCRETE_TYPES


def affordance_for(fdef: Optional[FixtureDef], index: int) -> str:
    """Which control suits the `index`-th channel: "grid" or "fader".

    Asked of the CHANNEL'S OWN capabilities, never of a sidecar hint or the
    fixture's name — the same rule the rest of the stack follows.
    """
    if fdef is None or not (0 <= index < fdef.channel_count):
        return "fader"
    return "grid" if grid_applies(fdef.channels[index]) else "fader"
