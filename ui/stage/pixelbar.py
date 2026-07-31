# ABOUTME: Renders Pixel Bar / Matrix fixtures as a strip of individually-lit
# ABOUTME: cells, playing the animation their effect channel selects.
"""Pixel bars and matrices.

A fixture with a pixel matrix draws as a column of cells rather than one lamp.
Its bodies (an LED bar's eight bars) all mirror the same DMX slice, so what
makes them differ is which column of the running effect they show —
`body_index` is that column.

Effects come from the fixture's own definition: an `Effect` capability names
what is running, and the sidecar maps that name to an animation asset. A value
with no effect behind it falls back to a flat colour fill, which is what an
un-animated bar looks like.
"""

import glob
import os
from typing import Optional

from PyQt6.QtGui import QBrush, QColor, QMovie
from PyQt6.QtWidgets import QGraphicsDropShadowEffect, QGraphicsRectItem

from core.fixture_state import FixtureState
from core.paths import asset_path
from ui.stage.base import BodyFixture, StrobeGate
from ui.stage.registry import register_renderer

GLOW_BLUR_RADIUS = 100


@register_renderer("Pixel Bar", "Matrix")
class PixelBarFixture(BodyFixture):
    """A strip of cells with a glow, driven per-cell by the running animation."""

    def build_body(self) -> None:
        matrix = self.fdef.matrix
        self.rows = max(1, matrix.pixel_count[1]) if matrix else 1
        # Which column of the animation this body shows.
        self.column = self.body_index

        cell_height = self.height / self.rows
        self.cells = [
            QGraphicsRectItem(0, row * cell_height, self.width, cell_height, self)
            for row in range(self.rows)
        ]
        for cell in self.cells:
            cell.setBrush(QColor("darkgrey"))

        glow = QGraphicsDropShadowEffect()
        glow.setBlurRadius(GLOW_BLUR_RADIUS)
        glow.setOffset(0)
        self.setGraphicsEffect(glow)

        self._movies = {}            # asset filename -> QMovie
        self._current_movie = None
        self._current_asset = None
        self._strobe = StrobeGate(self._repaint)

    # -- rendering -----------------------------------------------------
    def apply_state(self, state: FixtureState) -> None:
        self._strobe.apply_state(state)
        self._set_animation(self._asset_for(state.effect))
        self._repaint()

    def _repaint(self) -> None:
        if self._current_movie is not None and self._current_movie.isValid():
            self._paint_animation_frame()
        else:
            self._paint_flat()

    def _paint_flat(self) -> None:
        """No animation running: every cell takes the fixture's colour."""
        factor = self.intensity_factor() * self._strobe.factor()
        colour = self.clamped_color(self.state.rgb, factor)
        brush = QBrush(colour)
        for cell in self.cells:
            cell.setBrush(brush)
        # The glow keeps the unscaled hue, so a dimmed bar still reads as its
        # colour rather than fading to black.
        self._set_glow(self.state.rgb)

    def _paint_animation_frame(self) -> None:
        """Tint this body's column of the animation with the fixture's colour."""
        image = self._current_movie.currentImage()
        if image.isNull() or image.width() <= self.column:
            return

        factor = self.intensity_factor() * self._strobe.factor()
        red, green, blue = self.state.rgb
        totals = [0, 0, 0]
        lit = 0
        for row in range(min(self.rows, image.height())):
            pixel = image.pixelColor(self.column, row)
            # The animation supplies the pattern, the DMX supplies the colour.
            mixed = (pixel.red() / 255.0 * red,
                     pixel.green() / 255.0 * green,
                     pixel.blue() / 255.0 * blue)
            for component in range(3):
                totals[component] += mixed[component]
            lit += 1
            self.cells[row].setBrush(self.clamped_color(mixed, factor))

        if lit:
            self._set_glow(tuple(int(total / lit) for total in totals))

    def _set_glow(self, rgb) -> None:
        effect = self.graphicsEffect()
        if effect is not None:
            effect.setColor(QColor(*(max(0, min(255, int(c))) for c in rgb), 255))

    # -- animation assets ----------------------------------------------
    def _asset_for(self, effect: str) -> Optional[str]:
        """The animation file the running effect maps to, if any."""
        if not effect:
            return None
        return self.fdef.effects.get(effect, {}).get("gif")

    def _set_animation(self, asset: Optional[str]) -> None:
        if asset == self._current_asset:
            return
        self._current_asset = asset
        self._disconnect_movie()

        if asset is None:
            return
        movie = self._movies.get(asset)
        if movie is None:
            matches = glob.glob(os.path.join(asset_path("animations"), asset))
            if not matches:
                return
            movie = QMovie(matches[0])
            self._movies[asset] = movie
        self._current_movie = movie
        movie.frameChanged.connect(self._repaint)
        movie.start()

    def _disconnect_movie(self) -> None:
        if self._current_movie is None:
            return
        try:
            self._current_movie.frameChanged.disconnect(self._repaint)
        except TypeError:
            pass  # never connected
        self._current_movie.stop()
        self._current_movie = None

    def cleanup(self) -> None:
        """Stop everything that ticks before this body leaves the scene.

        Disconnecting also breaks the movie<->body reference cycle, so the body
        is freed by refcount instead of lingering until a cyclic collection —
        and its animation stops the moment it is removed.
        """
        self._strobe.stop()
        self._disconnect_movie()
        self._movies.clear()
