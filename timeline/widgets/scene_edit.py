# ABOUTME: Edit dialog for LED scene clips — 7 channel controls + scene name.
# ABOUTME: Returns the modified channels dict and name on accept.

import math
from pathlib import Path
from typing import Dict, List, Optional

from core.paths import asset_path

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QLineEdit, QSpinBox, QSlider, QCheckBox,
                             QDialogButtonBox, QFormLayout, QFrame, QWidget,
                             QSizePolicy, QGridLayout)
from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QSize
from PyQt6.QtGui import (QColor, QPalette, QPainter, QPixmap, QBrush, QPen,
                         QConicalGradient, QRadialGradient, QMouseEvent,
                         QMovie)


def _animations_dir() -> Path:
    """Resolve assets/animations/ both in dev and under PyInstaller (_MEIPASS)."""
    return Path(asset_path("animations"))


class _ColorWheel(QWidget):
    """Embedded HSV hue-saturation wheel.

    Click or drag inside the disk to pick a color. Angle = hue, radius =
    saturation. Always emits a fully-saturated color (V=1.0); value comes
    from elsewhere (R/G/B spinboxes + brightness slider).
    """

    color_changed = pyqtSignal(QColor)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(180, 180)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Preferred)
        self._hue = 0.0
        self._sat = 0.0
        self._wheel_pixmap: Optional[QPixmap] = None

    def set_rgb(self, r: int, g: int, b: int):
        """Move selector to match an RGB triplet (V channel ignored)."""
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


class _AnimationTile(QFrame):
    """One animation thumbnail in the grid. Plays its GIF; emits `clicked(index)`."""

    clicked = pyqtSignal(int)

    TILE_W = 48
    TILE_H = 72

    def __init__(self, gif_path: Path, index: int, parent=None):
        super().__init__(parent)
        self._index = index
        self.setObjectName("AnimationTile")
        self.setFixedSize(self.TILE_W, self.TILE_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{index + 1}. {gif_path.stem}\nvalue: {index * _AnimationGrid.STEP}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        self._gif_label = QLabel()
        self._gif_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Scale the GIF to fit while preserving aspect (source is 8×40 = 1:5).
        movie = QMovie(str(gif_path))
        src = movie.frameRect().size() if movie.isValid() else QSize(8, 40)
        if src.width() <= 0 or src.height() <= 0:
            src = QSize(8, 40)
        avail_w = self.TILE_W - 4
        avail_h = self.TILE_H - 4
        scale = min(avail_w / src.width(), avail_h / src.height())
        movie.setScaledSize(QSize(max(1, int(src.width() * scale)),
                                  max(1, int(src.height() * scale))))
        self._gif_label.setMovie(movie)
        movie.start()
        self._movie = movie  # keep reference alive

        layout.addWidget(self._gif_label)

    @property
    def index(self) -> int:
        return self._index

    def set_selected(self, selected: bool):
        # Use string property values so QSS [selected="true"] matches reliably.
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._index)


class _AnimationGrid(QWidget):
    """Selectable grid of animation GIFs.

    Each tile maps directly to an animation channel value:
        index N  ↔  value = N * STEP
    Tiles are populated from `assets/animations/*.gif` sorted by filename, so
    the leading number prefix (01-, 02-, …) drives the value mapping.
    """

    STEP = 4
    COLS = 7

    value_changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tiles: List[_AnimationTile] = []
        self._selected_index = -1

        layout = QGridLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)

        gif_dir = _animations_dir()
        if gif_dir.is_dir():
            gifs = sorted(p for p in gif_dir.iterdir()
                          if p.suffix.lower() == ".gif")
        else:
            gifs = []

        for i, path in enumerate(gifs):
            tile = _AnimationTile(path, i)
            tile.clicked.connect(self._on_tile_clicked)
            self._tiles.append(tile)
            layout.addWidget(tile, i // self.COLS, i % self.COLS)

    def set_value(self, value: int):
        """Highlight the tile that corresponds to a raw channel value."""
        if not self._tiles:
            return
        index = max(0, min(len(self._tiles) - 1, int(value) // self.STEP))
        self._set_selected(index)

    def value(self) -> int:
        if self._selected_index < 0:
            return 0
        return self._selected_index * self.STEP

    def _set_selected(self, index: int):
        if index == self._selected_index:
            return
        if 0 <= self._selected_index < len(self._tiles):
            self._tiles[self._selected_index].set_selected(False)
        self._selected_index = index
        if 0 <= index < len(self._tiles):
            self._tiles[index].set_selected(True)

    def _on_tile_clicked(self, index: int):
        if index == self._selected_index:
            return
        self._set_selected(index)
        self.value_changed.emit(self.value())


class SceneEditDialog(QDialog):
    """Edit the 7 channel values of a scene group (and its display name)."""

    def __init__(self, name: str, channels: Dict[str, int], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit Scene — {name}")
        self.setModal(True)
        self.resize(560, 820)

        self._setup_ui(name, channels)

    def _setup_ui(self, name: str, channels: Dict[str, int]):
        layout = QVBoxLayout(self)

        # Name
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit(name)
        name_row.addWidget(self.name_edit)
        layout.addLayout(name_row)

        form = QFormLayout()

        # Brightness — slider with black→white gradient (in dark-theme.qss).
        self.brightness_slider = QSlider(Qt.Orientation.Horizontal)
        self.brightness_slider.setObjectName("BrightnessSlider")
        self.brightness_slider.setRange(0, 255)
        self.brightness_slider.setValue(int(channels.get("brightness", 0)))
        self.brightness_value = QLabel(str(self.brightness_slider.value()))
        self.brightness_value.setMinimumWidth(32)
        self.brightness_value.setAlignment(Qt.AlignmentFlag.AlignRight
                                           | Qt.AlignmentFlag.AlignVCenter)
        self.brightness_slider.valueChanged.connect(
            lambda v: self.brightness_value.setText(str(v))
        )
        self.brightness_slider.valueChanged.connect(self._refresh_preview)
        b_row = QHBoxLayout()
        b_row.addWidget(self.brightness_slider)
        b_row.addWidget(self.brightness_value)
        form.addRow("Brightness:", b_row)

        # Color — embedded wheel + R/G/B spinboxes + small swatch.
        self.color_wheel = _ColorWheel()
        self.color_wheel.color_changed.connect(self._on_wheel_changed)

        self.r_spin = QSpinBox()
        self.g_spin = QSpinBox()
        self.b_spin = QSpinBox()
        for spin, key in ((self.r_spin, "red"),
                          (self.g_spin, "green"),
                          (self.b_spin, "blue")):
            spin.setRange(0, 255)
            spin.setValue(int(channels.get(key, 0)))
            spin.valueChanged.connect(self._on_rgb_spinbox_changed)

        self.color_swatch = QFrame()
        self.color_swatch.setFrameShape(QFrame.Shape.Box)
        self.color_swatch.setMinimumHeight(24)
        self.color_swatch.setAutoFillBackground(True)

        rgb_col = QVBoxLayout()
        for label_text, spin in (("R:", self.r_spin),
                                 ("G:", self.g_spin),
                                 ("B:", self.b_spin)):
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setMinimumWidth(20)
            row.addWidget(lbl)
            row.addWidget(spin)
            rgb_col.addLayout(row)
        rgb_col.addSpacing(4)
        rgb_col.addWidget(self.color_swatch)
        rgb_col.addStretch(1)

        color_row = QHBoxLayout()
        color_row.addWidget(self.color_wheel, 1)
        color_row.addLayout(rgb_col)
        form.addRow("Color:", color_row)

        # Animation — grid of GIF thumbnails (one per animation, value = index * 4).
        self.animation_grid = _AnimationGrid()
        self.animation_grid.set_value(int(channels.get("animation", 0)))
        form.addRow("Animation:", self.animation_grid)

        # Speed — plain slider with value readout.
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(0, 255)
        self.speed_slider.setValue(int(channels.get("speed", 0)))
        self.speed_value = QLabel(str(self.speed_slider.value()))
        self.speed_value.setMinimumWidth(32)
        self.speed_value.setAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
        self.speed_slider.valueChanged.connect(
            lambda v: self.speed_value.setText(str(v))
        )
        s_row = QHBoxLayout()
        s_row.addWidget(self.speed_slider)
        s_row.addWidget(self.speed_value)
        form.addRow("Speed:", s_row)

        # Direction — checkbox; unchecked → 1, checked → 255.
        self.direction_check = QCheckBox("Reverse direction")
        self.direction_check.setChecked(int(channels.get("direction", 1)) >= 128)
        form.addRow("Direction:", self.direction_check)

        layout.addLayout(form)

        # Color preview swatch (RGB scaled by brightness).
        self.preview = QFrame()
        self.preview.setFrameShape(QFrame.Shape.Box)
        self.preview.setMinimumHeight(40)
        self.preview.setAutoFillBackground(True)
        layout.addWidget(QLabel("Preview:"))
        layout.addWidget(self.preview)

        # Initial sync — drive the wheel selector and swatches off the loaded RGB.
        self.color_wheel.set_rgb(self.r_spin.value(),
                                 self.g_spin.value(),
                                 self.b_spin.value())
        self._refresh_swatch()
        self._refresh_preview()

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_wheel_changed(self, color: QColor):
        """Wheel selector moved → push RGB into spinboxes (signals blocked)."""
        for spin, value in ((self.r_spin, color.red()),
                            (self.g_spin, color.green()),
                            (self.b_spin, color.blue())):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self._refresh_swatch()
        self._refresh_preview()

    def _on_rgb_spinbox_changed(self):
        """R/G/B spinbox edited → sync swatch, preview, and wheel selector."""
        r, g, b = self.r_spin.value(), self.g_spin.value(), self.b_spin.value()
        # set_rgb does not emit color_changed, so no feedback loop.
        self.color_wheel.set_rgb(r, g, b)
        self._refresh_swatch()
        self._refresh_preview()

    def _refresh_swatch(self):
        """Paint the color swatch with the current pure RGB."""
        color = QColor(self.r_spin.value(),
                       self.g_spin.value(),
                       self.b_spin.value())
        palette = self.color_swatch.palette()
        palette.setColor(QPalette.ColorRole.Window, color)
        self.color_swatch.setPalette(palette)

    def _refresh_preview(self):
        """Update the preview swatch (RGB scaled by brightness)."""
        red = self.r_spin.value()
        green = self.g_spin.value()
        blue = self.b_spin.value()
        brightness = self.brightness_slider.value()
        scale = max(brightness / 255.0, 0.18)
        color = QColor(int(red * scale), int(green * scale), int(blue * scale))
        palette = self.preview.palette()
        palette.setColor(QPalette.ColorRole.Window, color)
        self.preview.setPalette(palette)

    def get_values(self) -> Optional[Dict]:
        """Return {'name': str, 'channels': {...}} as currently entered."""
        return {
            "name": self.name_edit.text().strip() or "Scene",
            "channels": {
                "brightness": self.brightness_slider.value(),
                "blue": self.b_spin.value(),
                "green": self.g_spin.value(),
                "red": self.r_spin.value(),
                "animation": self.animation_grid.value(),
                "speed": self.speed_slider.value(),
                "direction": 255 if self.direction_check.isChecked() else 1,
            },
        }
