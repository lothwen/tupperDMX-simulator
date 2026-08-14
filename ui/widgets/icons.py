# ABOUTME: Shared SVG icon loader — recolors the assets/icons/svg/ line icons
# ABOUTME: (drawn in #111) and serves them as QIcon / QPixmap / rich-text <img>.

import math
import os
import tempfile
from functools import lru_cache

from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import (QGuiApplication, QIcon, QIconEngine, QPainter,
                         QPixmap)
from PyQt6.QtSvg import QSvgRenderer

from core.paths import asset_path

# Every icon in assets/icons/svg/ is drawn with this stroke/fill color.
_SOURCE_COLOR = "#111"
# Default matches the theme's base text color (dark-theme.qss QWidget).
DEFAULT_COLOR = "#dcdcdc"

_DISABLED_OPACITY = 0.35

_img_dir: str | None = None  # session dir for materialized rich-text PNGs


@lru_cache(maxsize=None)
def _svg_bytes(name: str, color: str) -> bytes:
    with open(asset_path("icons", "svg", f"{name}.svg"), encoding="utf-8") as fh:
        return fh.read().replace(_SOURCE_COLOR, color).encode("utf-8")


@lru_cache(maxsize=None)
def _renderer(name: str, color: str) -> QSvgRenderer:
    return QSvgRenderer(QByteArray(_svg_bytes(name, color)))


@lru_cache(maxsize=None)
def _render(name: str, color: str, width: int, height: int,
            dpr: float, opacity: float) -> QPixmap:
    """Render one recolored SVG at exactly width×height DEVICE pixels.

    The vector is rasterized straight at the target resolution (no
    intermediate size that would need rescaling) with antialiasing on —
    that keeps the 2px strokes crisp at any icon size and screen scale.
    """
    result = QPixmap(max(1, width), max(1, height))
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if opacity < 1.0:
        painter.setOpacity(opacity)
    _renderer(name, color).render(painter, QRectF(0, 0, width, height))
    painter.end()
    result.setDevicePixelRatio(dpr)
    return result


def _screen_dpr() -> float:
    app = QGuiApplication.instance()
    return app.devicePixelRatio() if app else 1.0


class _SvgIconEngine(QIconEngine):
    """Renders the SVG on demand at whatever size/scale the style asks for.

    State.On swaps to on_name/on_color (checked buttons), Mode.Selected to
    selected_color (item views), Mode.Disabled fades the normal look.
    """

    def __init__(self, name: str, color: str, on_name: str | None,
                 on_color: str | None, selected_color: str | None):
        super().__init__()
        self._spec = (name, color, on_name, on_color, selected_color)

    def _variant(self, mode, state):
        name, color, on_name, on_color, selected_color = self._spec
        if state == QIcon.State.On:
            name, color = (on_name or name), (on_color or color)
        if mode == QIcon.Mode.Selected and selected_color:
            color = selected_color
        opacity = _DISABLED_OPACITY if mode == QIcon.Mode.Disabled else 1.0
        return name, color, opacity

    def pixmap(self, size, mode, state):
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size, mode, state, scale):
        # Qt 6.8 passes the LOGICAL size plus the scale separately (verified
        # against QIcon.pixmap under QT_SCALE_FACTOR=2) — render at
        # size×scale device pixels so hi-dpi screens get a native-res raster.
        name, color, opacity = self._variant(mode, state)
        return _render(name, color,
                       math.ceil(size.width() * scale),
                       math.ceil(size.height() * scale),
                       float(scale), opacity)

    def paint(self, painter, rect, mode, state):
        name, color, opacity = self._variant(mode, state)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if opacity < 1.0:
            painter.setOpacity(painter.opacity() * opacity)
        _renderer(name, color).render(painter, QRectF(rect))
        painter.restore()

    def clone(self):
        return _SvgIconEngine(*self._spec)


@lru_cache(maxsize=None)
def icon(name: str, color: str = DEFAULT_COLOR, on_name: str | None = None,
         on_color: str | None = None,
         selected_color: str | None = None) -> QIcon:
    """A QIcon of one recolored SVG, rasterized on demand at the exact
    requested size (see _SvgIconEngine for the mode/state variants)."""
    return QIcon(_SvgIconEngine(name, color, on_name, on_color,
                                selected_color))


def pixmap(name: str, size: int, color: str = DEFAULT_COLOR) -> QPixmap:
    """Render `assets/icons/svg/<name>.svg` recolored to `color` at `size`
    logical px, at the screen's device-pixel ratio."""
    dpr = _screen_dpr()
    device = math.ceil(size * dpr)
    return _render(name, color, device, device, dpr, 1.0)


@lru_cache(maxsize=None)
def _img_file(name: str, color: str, size: int) -> str:
    """Materialize a recolored icon as a PNG on disk (rendered at 2x for
    hi-dpi) so QLabel rich text can reference it by path."""
    global _img_dir
    if _img_dir is None:
        _img_dir = tempfile.mkdtemp(prefix="tupperdmx-icons-")
    path = os.path.join(_img_dir, f"{name}-{color.lstrip('#')}-{size}.png")
    if not os.path.exists(path):
        _render(name, color, size * 2, size * 2, 1.0, 1.0).save(path, "PNG")
    return path


def img_tag(name: str, color: str = DEFAULT_COLOR, size: int = 12) -> str:
    """An inline <img> tag for QLabel rich text, recolored like icon()."""
    return (f'<img src="{_img_file(name, color, size)}" width="{size}" '
            f'height="{size}" style="vertical-align: middle">')
