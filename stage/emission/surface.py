# ABOUTME: Emissions that ARE a lit surface rather than a beam — matrix cells,
# ABOUTME: blinder bulbs, strobe panels.
"""Surfaces.

Some fixtures don't throw light anywhere in particular; you look straight at
them. A pixel bar is a grid of cells, a blinder is a row of bulbs, a strobe is
one flat sheet. All three are drawn as what they are, with a glow so they read
as light rather than as coloured rectangles.
"""

import glob
import os
from typing import Dict, Optional, Tuple

from PyQt6.QtGui import QBrush, QColor, QMovie
from PyQt6.QtWidgets import QGraphicsDropShadowEffect, QGraphicsRectItem

from core.paths import asset_path
from stage.glow import GlowingEllipse
from stage.metrics import body_count
from stage.emission.base import Emission
from stage.frame import Frame
from stage.light import LightList, linear_rgb

MATRIX_GLOW_BLUR = 100
PANEL_GLOW_BLUR = 60
BULB_MARGIN = 5          # gap above the first bulb and below the last

# Above this many cells per body the grid is merged: a 64x64 matrix would
# otherwise put four thousand items on the scene for one fixture.
MAX_CELLS = 256

# Two independent knobs, and they have to stay independent: the light a pixel
# throws is the product of how FAR it carries and how BRIGHT it is on the way.
# Tying them together is why a bar could only ever be dim-and-tight or
# far-and-blinding, with nothing in between.

# HOW FAR one lit pixel throws light past its own face, as a multiple of the
# cell. Can be generous — reaching further costs no extra brightness now.
# Each cell carries its OWN colour, so a bar running an animation spills the
# pattern rather than an averaged wash, which is the point of a pixel bar.
CELL_HALO = 50.0

# HOW BRIGHT that spill is, against the pixel's own face. Small on purpose:
# the face itself is drawn by the scene underneath, so this is only what
# reaches the air, and forty stacked cells all contribute to the same patch of
# it. Raise it for a hazier room, not to make the bar reach further.
CELL_GLOW = 0.0004

# A panel is one big soft source rather than a grid of small ones, so nothing
# stacks and it can afford to be a little stronger per unit of face.
PANEL_HALO = 2.0
PANEL_GLOW = 0.22


def _fit_grid(columns: int, rows: int) -> Tuple[int, int]:
    """Halve the longer axis until the grid fits MAX_CELLS.

    Coarser than the real matrix, but a 64x64 wall reads the same at stage
    scale and costs sixteen times fewer scene items.
    """
    columns, rows = max(1, columns), max(1, rows)
    while columns * rows > MAX_CELLS:
        if columns >= rows:
            columns = max(1, columns // 2)
        else:
            rows = max(1, rows // 2)
    return columns, rows


# One QMovie per animation file for the whole stage. An eight-body LED bar used
# to decode eight copies of the same GIF, and every one of them ran its own
# frame timer.
_MOVIES: Dict[str, QMovie] = {}
_MOVIE_USERS: Dict[str, int] = {}


class MatrixCells(Emission):
    """A strip or grid of individually-lit cells.

    The grid is the matrix's own X by Y extent — a 24-pixel bar is 24 cells
    across, a 5x5 flower is a 5x5 grid — divided between the fixture's bodies
    when it has several (the LED bar's eight bars take one column each).

    Each cell takes its own emitter's colour when the fixture drives its pixels
    separately, and the fixture's colour when it doesn't — which is what an LED
    bar with one set of RGB channels for all its pixels does.

    When an effect is running, the animation supplies the *pattern* and the DMX
    supplies the *colour*: `column` is the animation column this body starts at,
    so the eight bars of one bar-set differ.
    """

    animates = True

    def build(self) -> None:
        body = self.body
        fdef = body.fdef
        matrix = fdef.matrix
        bodies = body_count(fdef)
        # A fixture split into several bodies shares its columns out between
        # them; every other matrix keeps all of them.
        columns = (max(1, matrix.pixel_count[0]) // bodies) if matrix else 1
        rows = max(1, matrix.pixel_count[1]) if matrix else 1
        self.columns, self.rows = _fit_grid(columns, rows)
        self.column = body.body_index * self.columns

        # Which emitter feeds which cell. Emitters arrive in the matrix's own
        # reading order (X fastest), and a body only shows its own columns.
        stride = self.columns * bodies
        self.emitter_index = [row * stride + self.column + col
                              for row in range(self.rows)
                              for col in range(self.columns)]

        cell_width = body.width / self.columns
        cell_height = body.height / self.rows
        self.cells = [
            QGraphicsRectItem(col * cell_width, row * cell_height,
                              cell_width, cell_height, body)
            for row in range(self.rows)
            for col in range(self.columns)
        ]
        for cell in self.cells:
            cell.setBrush(QColor("darkgrey"))

        glow = QGraphicsDropShadowEffect()
        glow.setBlurRadius(MATRIX_GLOW_BLUR)
        glow.setOffset(0)
        body.setGraphicsEffect(glow)

        self._movie: Optional[QMovie] = None
        self._asset: Optional[str] = None
        # What each cell resolved to this frame, so `emit_lights` doesn't have
        # to sample the animation a second time — 256 cells per body at 30 Hz
        # is the one place in the engine where that would actually show.
        self._lit: list = [((0.0, 0.0, 0.0), 0.0)] * len(self.cells)
        self._raster_light = True

    def render(self, frame: Frame) -> None:
        self._set_animation(self._asset_for(frame.state.program.name),
                            frame.state.program.speed_hz)
        if self._movie is not None and self._movie.isValid():
            self._paint_animation(frame)
        else:
            self._paint_flat(frame)

    def emit_lights(self, frame: Frame, out: LightList) -> None:
        for cell, (rgb, level) in zip(self.cells, self._lit):
            # An animation leaves most of the grid dark, and a black cell
            # contributes nothing to an additive buffer — so it is pure fill
            # cost, over a halo several times its own size, per body, per
            # frame. A pixel bar is where that adds up.
            if level <= 0.0 or not any(rgb):
                continue
            rect = cell.rect()
            out.add_quad(rect.x(), rect.y(), rect.width(), rect.height(),
                         linear_rgb(rgb), level * CELL_GLOW,
                         halo=max(rect.width(), rect.height()) * CELL_HALO)

    # -- painting ------------------------------------------------------
    def _paint_flat(self, frame: Frame) -> None:
        """No animation: every cell takes its own colour, or the fixture's."""
        beam = frame.beam
        emitters = frame.state.emitters
        # Per-cell only when the state drives every cell this body shows;
        # `emitter_index` rises with the cells, so its last entry is the
        # highest emitter asked for.
        per_cell = len(self.cells) > 1 and len(emitters) > self.emitter_index[-1]

        for index, cell in enumerate(self.cells):
            if per_cell:
                emitter = emitters[self.emitter_index[index]]
                rgb, level = emitter.rgb, emitter.intensity * beam.gate
            else:
                rgb, level = beam.rgb, beam.level
            # The cells stay lit whoever owns the light: they are the bar's
            # face. Only its bloom (the drop shadow) moves to the GPU.
            cell.setBrush(QBrush(self.body.clamped_color(rgb, level)))
            self._lit[index] = (rgb, level)
        # The glow keeps the unscaled hue, so a dimmed bar still reads as its
        # colour rather than fading to black.
        self._set_glow(beam.rgb)

    def _paint_animation(self, frame: Frame) -> None:
        """Tint this body's slice of the animation with the fixture's colour."""
        image = self._movie.currentImage()
        if image.isNull() or image.width() <= self.column:
            return

        beam = frame.beam
        red, green, blue = beam.rgb
        totals = [0, 0, 0]
        lit = 0
        for index, cell in enumerate(self.cells):
            column = self.column + index % self.columns
            row = index // self.columns
            # A grid wider or taller than the animation keeps its last colour
            # there rather than going black.
            if column >= image.width() or row >= image.height():
                continue
            pixel = image.pixelColor(column, row)
            # The animation supplies the pattern, the DMX supplies the colour.
            mixed = (pixel.red() / 255.0 * red,
                     pixel.green() / 255.0 * green,
                     pixel.blue() / 255.0 * blue)
            for component in range(3):
                totals[component] += mixed[component]
            lit += 1
            cell.setBrush(self.body.clamped_color(mixed, beam.level))
            self._lit[index] = (mixed, beam.level)

        if lit:
            self._set_glow(tuple(int(total / lit) for total in totals))

    def set_raster_light_visible(self, visible: bool) -> None:
        self._raster_light = visible
        effect = self.body.graphicsEffect()
        if effect is not None:
            # The blur is the matrix's bloom; the shader draws that now.
            effect.setEnabled(visible)

    def _set_glow(self, rgb) -> None:
        effect = self.body.graphicsEffect()
        if effect is not None:
            effect.setColor(QColor(*(max(0, min(255, int(c))) for c in rgb), 255))

    # -- animation assets ----------------------------------------------
    def _asset_for(self, effect: str) -> Optional[str]:
        """The animation file the running effect maps to, if any."""
        if not effect:
            return None
        return self.body.fdef.effects.get(effect, {}).get("gif")

    def _set_animation(self, asset: Optional[str], speed_hz: Optional[float]) -> None:
        if asset != self._asset:
            self._release_movie()
            self._asset = asset
            if asset is not None:
                self._movie = _acquire_movie(asset)
        if self._movie is not None and speed_hz:
            # The fixture's speed channel finally does something: 1 Hz is the
            # rate the GIFs were authored at, so that is 100%.
            self._movie.setSpeed(max(10, min(400, int(speed_hz * 100))))

    def _release_movie(self) -> None:
        if self._asset is not None:
            _release_movie(self._asset)
        self._movie = None

    def cleanup(self) -> None:
        self._release_movie()
        self._asset = None

    @property
    def current_asset(self) -> Optional[str]:
        """The animation file playing, or None when the bar is a flat colour."""
        return self._asset

    @property
    def current_movie(self) -> Optional[QMovie]:
        return self._movie


def _acquire_movie(asset: str) -> Optional[QMovie]:
    """The shared QMovie for an animation file, started on first use."""
    movie = _MOVIES.get(asset)
    if movie is None:
        matches = glob.glob(os.path.join(asset_path("animations"), asset))
        if not matches:
            return None
        movie = QMovie(matches[0])
        _MOVIES[asset] = movie
        _MOVIE_USERS[asset] = 0
    _MOVIE_USERS[asset] += 1
    if movie.state() != QMovie.MovieState.Running:
        movie.start()
    return movie


def _release_movie(asset: str) -> None:
    """Drop one user; the last one out stops the movie, but leaves it cached.

    Deliberately never destroyed: freeing a QMovie whose decoder is mid-frame
    aborts the process, and there are only thirty-odd animation files, so
    keeping the decoded ones around costs far less than the crash.
    """
    if asset not in _MOVIE_USERS:
        return
    _MOVIE_USERS[asset] = max(0, _MOVIE_USERS[asset] - 1)
    if _MOVIE_USERS[asset] == 0:
        movie = _MOVIES.get(asset)
        if movie is not None:
            movie.stop()


class BulbArray(Emission):
    """One glowing bulb per emitter, stacked in the housing.

    How many, and which channel feeds which, comes from the resolved state's
    emitters — the n-th channel of a colour is the n-th cell — so a two-cell
    warm/cold blinder and a four-cell one both work without special-casing.
    """

    def build(self) -> None:
        body = self.body
        count = self.body.cell_count()
        # Share the height between the bulbs, keeping them square and centred.
        slot = (body.height - 2 * BULB_MARGIN) / count
        size = min(body.width * 0.75, slot)
        self.bulbs = []
        for index in range(count):
            top = BULB_MARGIN + index * slot + (slot - size) / 2
            self.bulbs.append(GlowingEllipse((body.width - size) / 2, top,
                                             size, size, body))

    def render(self, frame: Frame) -> None:
        beam = frame.beam
        emitters = frame.state.emitters
        for index, bulb in enumerate(self.bulbs):
            # More bulbs than the state drives: the extras stay dark.
            rgb = emitters[index].rgb if index < len(emitters) else (0, 0, 0)
            bulb.setGlowColor(self.body.clamped_color(rgb, beam.level),
                              min(1.0, beam.level))

    def set_raster_light_visible(self, visible: bool) -> None:
        for bulb in self.bulbs:
            bulb.draw_glow = visible
            bulb.update()

    def emit_lights(self, frame: Frame, out: LightList) -> None:
        """The blinder's glare, at the intensity it actually asks for.

        Unlike the raster path, nothing here is clipped on the way out: two
        blinders lighting the same patch of air add, and the stage exposure
        decides how far past full that reads.
        """
        beam = frame.beam
        emitters = frame.state.emitters
        for index, bulb in enumerate(self.bulbs):
            rgb = emitters[index].rgb if index < len(emitters) else (0, 0, 0)
            level = beam.level
            if level <= 0.0:
                continue
            rect = bulb.rect()
            out.add_point(rect.center().x(), rect.center().y(),
                          linear_rgb(rgb), level, rect.width() / 2.0)


class FlatPanel(Emission):
    """One flat surface that lights up — a strobe, a wall of LED."""

    def build(self) -> None:
        body = self.body
        self.panel = QGraphicsRectItem(0, 0, body.width, body.height, body)
        self.panel.setBrush(QColor(20, 20, 20))
        self._raster_light = True

        glow = QGraphicsDropShadowEffect()
        glow.setBlurRadius(PANEL_GLOW_BLUR)
        glow.setOffset(0)
        body.setGraphicsEffect(glow)

    def render(self, frame: Frame) -> None:
        beam = frame.beam
        self.panel.setBrush(QBrush(self.body.clamped_color(beam.rgb, beam.level)))
        effect = self.body.graphicsEffect()
        if effect is not None:
            effect.setColor(QColor(*beam.rgb, 255))

    def set_raster_light_visible(self, visible: bool) -> None:
        self._raster_light = visible
        effect = self.body.graphicsEffect()
        if effect is not None:
            effect.setEnabled(visible)

    def emit_lights(self, frame: Frame, out: LightList) -> None:
        beam = frame.beam
        if beam.level <= 0.0:
            return
        rect = self.panel.rect()
        out.add_quad(rect.x(), rect.y(), rect.width(), rect.height(),
                     linear_rgb(beam.rgb), beam.level * PANEL_GLOW,
                     halo=max(rect.width(), rect.height()) * PANEL_HALO)
