# ABOUTME: AutoProgramDialog — ☰ ▸ Auto program…: pick a song, its audio and
# ABOUTME: palette, read its analysis (song map, sections) and the role each
# ABOUTME: fixture got, then apply the take to the song or export it to a .mid.
"""Generate a song's light show from its audio, look at it, then apply it.

Modal, like the playlist manager. The analysis starts as the window opens and
runs again whenever the song, its audio or an ANALYSIS value changes; the
palette and the GENERATION values only program again from the same analysis
(AutoProgrammer keeps it), which takes a fraction of a second. Nothing touches
the show until Apply — behind a confirmation and a pinned snapshot — and the
take on show is always the one the inputs describe: a take asked for under
other inputs is ignored when it arrives.

The colours painted here (the song map, role chips, step and notice icons)
are the theme's own: QSS cannot reach a QPainter.
"""
import html
import math
import os
import shutil
import dataclasses
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QElapsedTimer, QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QIcon, QPainter, QPalette,
                         QPen, QPixmap, QPolygonF)
from PyQt6.QtWidgets import (QAbstractItemView, QButtonGroup, QColorDialog, QComboBox,
                             QDialog, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                             QHeaderView, QLabel, QListWidget, QListWidgetItem, QProgressBar,
                             QPushButton, QSizePolicy, QSlider, QSpinBox, QStackedWidget, QStyle,
                             QStyledItemDelegate, QStyleOptionComboBox, QStyleOptionViewItem,
                             QStylePainter, QTableWidget, QTableWidgetItem, QToolButton,
                             QToolTip, QVBoxLayout, QWidget)

from app.autoprogram import snapshot_name
from autoprogram import analysis, colours
from autoprogram.director import (DEFAULT_FEEL, DEFAULT_PACE, FEELS, PACE_STEPS, Feel, Style,
                                  look_beats)
from autoprogram.looks import PEAK
from autoprogram.take import ANALYSE, BACKING, DECODE, GUIDE, PROGRAM, Options, tracks
from core import note_layout
from core.tempo_map import valid_meter
from ui import theme
from ui.widgets.sizing import fit_to_screen
from ui.widgets.slider_ticks import SliderTickLabels

MONO = ["JetBrains Mono", "DejaVu Sans Mono", "Menlo", "Consolas", "monospace"]

# Painted colours, the theme's.
MAP_BG, AREA_BG = "#1a1a1a", "#202020"
LEVEL_COLOURS = {analysis.SILENT: "#333333", analysis.QUIET: "#56616b",
                 analysis.MID: "#8e99a2", analysis.HIGH: "#d4d8db"}
ACCENT, WARN, ERROR = "#79cdf0", "#d9a13b", "#e05252"
DIM = "#7a7a7a"

ROLE_COLOURS = {"mover": "#c49cf2", "wash": "#79cdf0", "pixel": "#7fd69a",
                "blinder": "#eef3ff", "fog": "#a8a8a8"}
# What each role does in a generated show (director.py), for the chip's tooltip.
ROLE_TIPS = {
    "mover": "Pans and tilts: the library's looks, else beams swung either side of the "
             "floor in the palette's colours, moving at every look change. Opens wide white "
             "on impacts.",
    "wash": "Mixes colour: the part's two palette colours, alternating at every look change "
            "— once a section at pace 0, every few beats at a busy climax.",
    "pixel": "LED bar with built-in animations: plays the fixture library's scenes tagged "
             "for each song part.",
    "blinder": "White or dimmer only: stays dark, firing on the song's own hits and the "
               "landing after a build; from the last chorus on, short strobe bursts into the "
               "climax's phrases too.",
    "fog": "Haze at a quiet opening, and a puff into every build, every break, every chorus "
           "and the climax.",
}
TRACK_NAMES = {GUIDE: "Guide", BACKING: "Backing"}
# A custom palette's three colours, as their swatches say them.
CUSTOM_TIPS = ("Primary: what most of the song wears",
               "Partner: worn with the primary",
               "Contrast: kept for choruses, bridges and accents")
SWATCH_W, SWATCH_H = 24, 12     # a palette entry's icon: its colours side by side
NO_FIXTURES = ("No programmable fixtures in the patch: there is nothing to apply. Patch a "
               "mover, wash, pixel bar, blinder or fog machine, then run again.")
DEBOUNCE_MS = 300
NOTICE_ROWS = 4         # notices shown before the list scrolls

# The song map's geometry, in its own 128-high units (scaled with the UI).
MAP_H, BAND_H = 128, 18
LOUD_Y, LOUD_H, LOUD_TOP = 20, 68, 1.6
GRID_Y, GRID_H = 91, 6
MARK_Y, MARK_H = 100, 12
AXIS_Y = 124


def _px(value: float) -> int:
    """`value` design pixels at the UI scale (theme.py scales only the QSS)."""
    return round(value * theme.applied_scale())


def _signed(value: float, decimals: int) -> str:
    """`value` with a true minus sign."""
    return f"{value:.{decimals}f}".replace("-", "−")


def _mmss(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _lane_note(lane) -> str:
    """The grey note after a programmed fixture's name: its twins on the same
    DMX address, and the fixture it mirrors across the stage."""
    notes = [f"also drives ch {', '.join(map(str, lane.twins))}"] if lane.twins else []
    if lane.mirror is not None:
        notes.append(f"mirrors ch {lane.mirror}")
    return " · ".join(notes)


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def part_colour(part: str) -> QColor:
    """A song part's colour, as the timeline's arrangement band draws it."""
    info = note_layout.describe_note(note_layout.SONG_PART_BASE
                                     + note_layout.SONG_PARTS.index(part))
    return QColor(info.color)


def _ink(fill: QColor) -> QColor:
    """Dark text on a light song-part colour, light text on a dark one."""
    luma = 0.2126 * fill.red() + 0.7152 * fill.green() + 0.0722 * fill.blue()
    return QColor("#1f1f1f" if luma >= 100 else "#dcdcdc")


def _restyle(widget: QWidget, **properties) -> None:
    """Set dynamic properties the stylesheet selects on, and re-polish."""
    for name, value in properties.items():
        widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _plain(*labels: QLabel) -> None:
    """Labels that show a song's title or a file's name: as text, never
    read as rich text (a title may hold "<" or "&")."""
    for label in labels:
        label.setTextFormat(Qt.TextFormat.PlainText)


def _spaced(widget: QWidget, spacing: float) -> None:
    """Letter spacing, which QSS has no property for."""
    font = widget.font()
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    widget.setFont(font)


def _paint_font(widget: QWidget, size: float, bold: bool = False, mono: bool = False,
                spacing: float = 0.0) -> QFont:
    """A font of `size` pixels (fractions too) for painting on `widget`."""
    font = QFont(widget.font())
    if mono:
        font.setFamilies(MONO)
    font.setPointSizeF(size * 72.0 / widget.logicalDpiY())
    font.setBold(bold)
    if spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return font


# ---------------------------------------------------------------------------
# The Advanced values: the analysis's Tuning and the director's Style
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Field:
    """One Advanced value: `key` is its Tuning / Style attribute (or
    "pace" / "take"), shown as `per` engine units a step."""
    key: str
    label: str
    tip: str
    minimum: float
    maximum: float
    step: float = 1
    decimals: int = 0           # 0: a whole number
    suffix: str = ""
    per: float = 1.0


ANALYSIS_FIELDS = (
    Field("grid_search", "Tempo search ±",
          "How far from the song's own BPM — and from the live tracker's estimate — the "
          "beat grid is searched.", 1, 10, suffix=" %", per=0.01),
    Field("quiet_max", "Quiet below",
          "Bars quieter than this, relative to the song's loud level, are quiet.",
          0.1, 1.0, 0.01, 2),
    Field("high_min", "High above",
          "Bars at least this loud, relative to the song's loud level, are high.",
          0.2, 1.5, 0.01, 2),
    Field("build_rise", "Build rise",
          "How much louder a bar's last beat must be than its first, landing on a louder "
          "downbeat, to count as a build.", 1.05, 5.0, 0.05, 2, "×"),
    Field("segment_cost", "Phrase cost",
          "What each extra phrase costs when the song is cut up: higher makes fewer, longer "
          "sections.", 0.5, 8.0, 0.25, 2),
    Field("climax_share", "Climax share",
          "The climax is the last loud section with at least this share of the strongest "
          "one's energy.", 50, 100, suffix=" %", per=0.01),
)
GENERATION_FIELDS = (
    Field("pace", "Pace",
          "How often the looks change and the fixtures take turns, each step about twice as "
          "often: at 0 a section holds one look, at 4 a loud chorus changes every 2 bars, at "
          "the top every beat — and the climax twice as often as a chorus. Sections, hits and "
          "builds land at any pace.", 0, PACE_STEPS),
    Field("take", "Take",
          "Another take at the same settings: other lead roles, look orders and "
          "choreographies. Take 1 is the song's own.", 1, 999),
    Field("glow_scale", "Glow level",
          "How much of its look the middle fixture (or mirrored pair) lighting the quietest "
          "sections gives.",
          10, 100, suffix=" %", per=0.01),
    Field("glow_energy", "Glow below",
          "A quiet section with less energy than this (0–1, against the rest of the song) "
          "is lit by its lead role's middle fixture (or mirrored pair) glowing rather than by "
          "the whole lead role.", 0.0, 1.0, 0.05, 2),
    Field("cycle", "Looks per part",
          "The most looks one fixture steps through in a part of the song. A part looks "
          "alike each time it comes back.", 1, 8),
)


def _default(field: Field):
    """A field's default as shown: the engine's own value."""
    if field.key == "pace":
        return DEFAULT_PACE
    if field.key == "take":
        return 1
    source = analysis.Tuning() if field in ANALYSIS_FIELDS else Style()
    return _as_shown(field, getattr(source, field.key))


def _as_shown(field: Field, value):
    """An engine value as `field` shows it."""
    value = round(value / field.per, field.decimals)
    return value if field.decimals else int(value)


# The fields a feel sets: the pace and the director's Style.
FEEL_KEYS = ("pace",) + tuple(f.name for f in dataclasses.fields(Style))


def _feel_value(feel: Feel, field: Field):
    """What `feel` sets `field` to, as shown (`field` is one of FEEL_KEYS)."""
    if field.key == "pace":
        return feel.pace
    return _as_shown(field, getattr(feel.style, field.key))


def _every(beats: float, beats_per_bar: int) -> str:
    """A look interval as the readout says it: in bars when it is whole bars."""
    if beats >= beats_per_bar and beats % beats_per_bar == 0:
        bars = int(beats // beats_per_bar)
        return "bar" if bars == 1 else f"{bars} bars"
    return {0.5: "half beat", 1: "beat"}.get(beats, f"{beats:g} beats")


def pace_readout(pace: int, meter: Optional[str]) -> str:
    """What `pace` does to a song in `meter`, in the engine's own numbers:
    how often a loud chorus and the climax change look."""
    beats_per_bar = int(valid_meter(meter).split("/")[0])
    x = pace / PACE_STEPS
    chorus = look_beats(PEAK, x, beats_per_bar)
    if chorus is None:
        return "one look per section"
    climax = look_beats(PEAK, x, beats_per_bar, climax=True)
    return (f"chorus every {_every(chorus, beats_per_bar)} · "
            f"climax every {_every(climax, beats_per_bar)}")


class _EnterKeeps:
    """Enter takes the typed value instead of reaching the dialog's default
    button (which would open the Apply confirmation)."""

    def keyPressEvent(self, event):
        super().keyPressEvent(event)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()


class _Spin(_EnterKeeps, QSpinBox):
    pass


class _DoubleSpin(_EnterKeeps, QDoubleSpinBox):
    pass


# ---------------------------------------------------------------------------
# Painted pieces
# ---------------------------------------------------------------------------
class SongMapWidget(QWidget):
    """The song's analysis in one strip: its sections, the loudness of every
    bar shaded by level (builds in cyan), the beat grid (amber where it does
    not fit), builds, impacts and accents, and a time axis. Hovering a bar
    tells its number, start, loudness and level."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("AutoProgramMap")
        self.setFixedHeight(_px(MAP_H))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.song_map: Optional[analysis.SongMap] = None

    def set_map(self, song_map: Optional[analysis.SongMap]) -> None:
        self.song_map = song_map
        self.update()

    def _units(self) -> Tuple[float, float]:
        """(scale, width) of the map's own units."""
        scale = self.height() / MAP_H
        return scale, self.width() / scale

    def paintEvent(self, _event):
        painter = QPainter(self)
        scale, width = self._units()
        painter.scale(scale, scale)
        painter.fillRect(QRectF(0, 0, width, MAP_H), QColor(MAP_BG))
        song = self.song_map
        if song is None or not song.bars or song.duration <= 0:
            return

        def x(t):
            return max(0.0, min(width, t / song.duration * width))

        def y(loud):
            return LOUD_Y + LOUD_H - min(loud, LOUD_TOP) / LOUD_TOP * LOUD_H

        bar_len = song.bar_length
        label = _paint_font(self, 10)

        # sections, END running to the right edge
        painter.fillRect(QRectF(0, 0, width, BAND_H), QColor(AREA_BG))
        painter.setFont(_paint_font(self, 10, bold=True, spacing=0.5))
        blocks = [(part, s.start, x(s.end)) for part, s in song.parts() if part != "END"]
        blocks.append(("END", song.end_start, width))
        for part, start, x1 in blocks:
            x0, fill = x(start), part_colour(part)
            painter.fillRect(QRectF(x0, 0, max(1.0, x1 - x0 - 1), BAND_H), fill)
            if x1 - x0 > 44:
                painter.setPen(_ink(fill))
                painter.drawText(QPointF(x0 + 5, 12.5), part)

        # loudness, one column a bar; the dashed line is the loud level
        painter.fillRect(QRectF(0, LOUD_Y, width, LOUD_H), QColor(AREA_BG))
        dashed = QPen(QColor("#3f3f3f"), 1)
        dashed.setDashPattern([3, 3])
        painter.setPen(dashed)
        painter.drawLine(QPointF(0, y(1) + 0.5), QPointF(width, y(1) + 0.5))
        weak = song.fit < analysis.STEADY_FIT
        for i, bar in enumerate(song.bars):
            x0, x1 = x(bar.start), x(bar.start + bar_len)
            if x1 <= 0 or x0 >= width:
                continue
            top = y(bar.loud)
            painter.fillRect(QRectF(x0, top, max(1.0, x1 - x0 - 1), LOUD_Y + LOUD_H - top),
                             QColor(ACCENT if bar.build else LEVEL_COLOURS[bar.level]))
            off = weak or any(a <= bar.number <= b for a, b in song.weak_grid)
            tall = i % 4 == 0
            painter.fillRect(QRectF(x0, GRID_Y + (0 if tall else 3), 1, GRID_H if tall else 3),
                             QColor(WARN if off else "#5a5a5a"))

        # where the grid does not fit
        for first, last in song.weak_grid:
            x0, x1 = x(song.time(first)), x(song.time(last + 1))
            painter.setPen(QPen(QColor(WARN), 1))
            painter.setBrush(QBrush(QColor(WARN), Qt.BrushStyle.BDiagPattern))
            painter.drawRect(QRectF(x0 + 0.5, LOUD_Y + 0.5, x1 - x0 - 1,
                                    GRID_Y + GRID_H - LOUD_Y - 1))
            painter.fillRect(QRectF(x0 + 1, LOUD_Y + 1, 112, 13), QColor(MAP_BG))
            painter.setFont(label)
            painter.drawText(QPointF(x0 + 5, LOUD_Y + 11), "grid does not fit")

        # the marker lane: builds, impacts, accents
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ACCENT))
        for bar in song.bars:
            if bar.build:
                x0, x1 = x(bar.start), x(bar.start + bar_len)
                painter.drawPolygon(QPolygonF([QPointF(x0, MARK_Y + MARK_H), QPointF(x1, MARK_Y),
                                               QPointF(x1, MARK_Y + MARK_H)]))
        white = QColor("#ffffff")
        for at, _strength in song.impacts:
            xi = round(x(at)) + 0.5
            painter.setPen(QPen(white, 1))
            painter.drawLine(QPointF(xi, BAND_H), QPointF(xi, MARK_Y + MARK_H))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(white)
            painter.drawPolygon(QPolygonF([QPointF(xi - 4, MARK_Y), QPointF(xi + 4, MARK_Y),
                                           QPointF(xi, MARK_Y + 7)]))
        painter.setPen(QPen(QColor("#dcdcdc"), 1.2))
        painter.setBrush(QColor(MAP_BG))
        for at, _strength in song.accents:
            painter.drawEllipse(QPointF(x(at), MARK_Y + MARK_H / 2), 3, 3)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        # the time axis
        painter.setPen(QPen(QColor("#333333"), 1))
        painter.drawLine(QPointF(0, MARK_Y + MARK_H + 1.5), QPointF(width, MARK_Y + MARK_H + 1.5))
        axis = _paint_font(self, 9.5, mono=True)
        metrics = QFontMetricsF(axis)
        painter.setFont(axis)
        step = 30 if song.duration > 120 else 10
        t = 0
        while t <= song.duration:
            xt = round(x(t)) + 0.5
            painter.setPen(QPen(QColor("#5a5a5a"), 1))
            painter.drawLine(QPointF(xt, MARK_Y + MARK_H + 2), QPointF(xt, MARK_Y + MARK_H + 5))
            text = _mmss(t)
            left = xt + 2 if t == 0 else xt - metrics.horizontalAdvance(text) / 2
            painter.setPen(QColor(DIM))
            painter.drawText(QPointF(left, AXIS_Y), text)
            t += step

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip:
            self._tooltip(event)
            return True
        return super().event(event)

    def _tooltip(self, event):
        song = self.song_map
        if song is None or not song.bars or song.duration <= 0:
            QToolTip.hideText()
            return
        scale, width = self._units()
        bar = song.bar_at(event.pos().x() / scale / width * song.duration)
        x0 = bar.start / song.duration * width * scale
        x1 = (bar.start + song.bar_length) / song.duration * width * scale
        level = analysis.LEVEL_NAMES[bar.level] + (", build" if bar.build else "")
        QToolTip.showText(event.globalPos(),
                          f"Bar {bar.number} · {_signed(bar.start, 2)} s\n"
                          f"Loudness {bar.loud:.2f} · {level}",
                          self, QRect(int(x0), 0, max(1, int(x1 - x0) + 1), self.height()))


class _StepIcon(QWidget):
    """A progress step's mark: a tick once done, a dot while running, a ring
    while it waits."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(_px(16), _px(16))
        self.state = "next"

    def set_state(self, state: str) -> None:
        self.state = state
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scale = self.width() / 16
        painter.scale(scale, scale)
        centre = QPointF(8, 8)
        if self.state == "done":
            painter.setPen(QPen(QColor("#5fb4d8"), 1))
            painter.setBrush(QColor("#3a5a67"))
            painter.drawEllipse(centre, 6.5, 6.5)
            painter.setPen(QColor("#dcdcdc"))
            painter.setFont(_paint_font(self, 9))
            painter.drawText(QRectF(1, 1, 14, 14), Qt.AlignmentFlag.AlignCenter, "✓")
        elif self.state == "now":
            painter.setPen(QPen(QColor(ACCENT), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(centre, 7.5, 7.5)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(ACCENT))
            painter.drawEllipse(centre, 4, 4)
        else:
            painter.setPen(QPen(QColor("#5a5a5a"), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(centre, 6.5, 6.5)


def _notice_icon(kind: str, ratio: float) -> QIcon:
    """A notice's mark: an amber square for a warning, a red disc for an
    error, a dark "!" on either."""
    size = 14
    pixmap = QPixmap(round(_px(size) * ratio), round(_px(size) * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    scale = _px(size) / size
    painter.scale(scale, scale)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ERROR if kind == "error" else WARN))
    if kind == "error":
        painter.drawEllipse(QRectF(0, 0, size, size))
    else:
        painter.drawRoundedRect(QRectF(0, 0, size, size), 2, 2)
    font = QFont()
    font.setPixelSize(10)
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#1f1f1f"))
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "!")
    painter.end()
    return QIcon(pixmap)


def _swatch_icon(fills: List[QColor], ratio: float) -> QIcon:
    """Colours side by side, as a combo entry's icon: a palette's primary,
    partner and contrast, or a hue's one."""
    width, height = _px(SWATCH_W), _px(SWATCH_H)
    pixmap = QPixmap(round(width * ratio), round(height * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    for i, fill in enumerate(fills):
        x0, x1 = round(width * i / len(fills)), round(width * (i + 1) / len(fills))
        painter.fillRect(QRect(x0, 0, x1 - x0, height), fill)
    painter.end()
    return QIcon(pixmap)


def _fills(spec: str) -> List[QColor]:
    """A palette spec's three colours, to paint."""
    resolved = colours.palette(spec)[1]
    return [QColor(*resolved[slot][:3]) for slot in colours.SLOTS]


NOTE_ROLE = Qt.ItemDataRole.UserRole + 1    # grey text after a fixture's name
CHIP_ROLE = Qt.ItemDataRole.UserRole + 2    # the role a ROLE cell draws as a chip


class _FixtureDelegate(QStyledItemDelegate):
    """The fixtures table's two drawn cells: a FIXTURE with its note after
    it in grey, and the ROLE as an outlined chip in the role's colour."""

    def paint(self, painter, option, index):
        note, role = index.data(NOTE_ROLE), index.data(CHIP_ROLE)
        if not note and not role:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text, opt.text = opt.text, ""
        widget = opt.widget
        style = widget.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        rect = QRectF(style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if role:
            self._chip(painter, widget, rect, role)
        else:
            self._named(painter, widget, opt, rect, text, note)
        painter.restore()

    @staticmethod
    def _chip(painter, widget, rect, role):
        colour = QColor(ROLE_COLOURS[role])
        scale = theme.applied_scale()
        font = _paint_font(widget, 10 * scale, bold=True, spacing=0.5)
        metrics = QFontMetricsF(font)
        text = role.upper()
        box = QRectF(rect.left(), 0, metrics.horizontalAdvance(text) + 12 * scale,
                     metrics.height() + 2 * scale)
        box.moveCenter(QPointF(box.center().x(), rect.center().y()))
        painter.setPen(QPen(colour, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 2, 2)
        painter.setFont(font)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    @staticmethod
    def _named(painter, widget, opt, rect, text, note):
        metrics = QFontMetricsF(opt.font)
        name = metrics.elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
        painter.setFont(opt.font)
        painter.setPen(opt.palette.color(QPalette.ColorRole.Text))
        painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
        room = rect.width() - metrics.horizontalAdvance(name + " ")
        if room <= 0:
            return
        small = _paint_font(widget, 11 * theme.applied_scale())
        small_metrics = QFontMetricsF(small)
        painter.setFont(small)
        painter.setPen(QColor(DIM))
        painter.drawText(rect.adjusted(rect.width() - room, 0, 0, 0),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         small_metrics.elidedText(note, Qt.TextElideMode.ElideRight, room))


class _PaletteCombo(QComboBox):
    """The palette: in Auto it also says which one the song's mood picked
    ("Auto · Neon"), the pick in grey."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.picked: Optional[str] = None

    def set_picked(self, name: Optional[str]) -> None:
        self.picked = name
        self.update()

    def paintEvent(self, event):
        if self.currentIndex() != 0 or not self.picked:
            super().paintEvent(event)
            return
        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        opt.currentText = ""
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        rect = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, opt,
                                           QStyle.SubControl.SC_ComboBoxEditField, self)
        auto = self.itemText(0)
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        painter.setPen(opt.palette.color(QPalette.ColorRole.ButtonText))
        painter.drawText(rect, align, auto)
        painter.setPen(QColor("#9a9a9a"))
        painter.drawText(rect.adjusted(self.fontMetrics().horizontalAdvance(auto), 0, 0, 0),
                         align, f" · {self.picked}")


class _HueCombo(QComboBox):
    """The hue a palette mode walks from: Random or one of the fixture
    palette's twelve. Closed it is just a swatch, a rainbow for Random; the
    names are in its list."""

    def paintEvent(self, event):
        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        opt.currentText, opt.currentIcon = "", QIcon()
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        rect = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, opt,
                                           QStyle.SubControl.SC_ComboBoxEditField, self)
        self.itemIcon(self.currentIndex()).paint(painter, rect)


class _Elided(QLabel):
    """A label that elides its text to the width it is given."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(1)
        self._elide()

    def setText(self, text: str) -> None:
        self._full = text
        self._elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight,
                                                      self.width()))


# ---------------------------------------------------------------------------
# The replace confirmation
# ---------------------------------------------------------------------------
class ReplaceDialog(QDialog):
    """Apply's second thoughts: what a take replaces, what the song keeps,
    and the version saved first. Cancel is the default."""

    def __init__(self, take, parent=None):
        super().__init__(parent)
        self.setObjectName("AutoProgramConfirm")
        self.setWindowTitle("Replace program")
        self.setModal(True)
        self.setFixedWidth(_px(480))
        title = take.song.title or "Untitled"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(_px(16), _px(16), _px(16), _px(16))
        column.setSpacing(_px(12))
        heading = QLabel(f"Replace the whole program of “{title}”?")
        heading.setObjectName("AutoProgramConfirmHeading")
        heading.setWordWrap(True)
        _plain(heading)
        column.addWidget(heading)

        lanes = len(take.lanes) + sum(len(lane.twins) for lane in take.lanes) + len(take.skipped)
        replaced = [f"Every clip, scene and fade on all {_plural(lanes, 'lane')}",
                    "The song sections on the arrangement band"]
        kept = ["Each lane’s colour-palette setup, mute and lock (automatic mode stays off)",
                "Audio, offsets, tempo markers and voice cues"]
        song_bpm, detected = take.analysis.bpm, take.analysis.map.bpm
        if song_bpm:
            kept.append(f"The song’s own {song_bpm} BPM, not the detected {detected:.2f}")
        else:
            replaced.append(f"The tempo: {take.bpm} BPM, the detected {detected:.2f} rounded")
        columns = QHBoxLayout()
        columns.setSpacing(_px(16))
        for caption, lines, state in (("REPLACED", replaced, "replaced"), ("KEPT", kept, "kept")):
            box = QVBoxLayout()
            box.setSpacing(_px(5))
            head = QLabel(caption)
            head.setObjectName("AutoProgramConfirmCaption")
            head.setProperty("state", state)
            _spaced(head, 1)
            box.addWidget(head)
            for line in lines:
                item = QLabel(line)
                item.setObjectName("AutoProgramConfirmItem")
                item.setProperty("state", state)
                item.setWordWrap(True)
                _plain(item)
                box.addWidget(item)
            box.addStretch(1)
            columns.addLayout(box, 1)
        column.addLayout(columns)
        info = QLabel(f"A version is saved first as <span style='color:#dcdcdc'>"
                      f"“{html.escape(snapshot_name(take.song))}”</span>. Restore it from the "
                      f"show’s versions.")
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setObjectName("AutoProgramConfirmInfo")
        info.setWordWrap(True)
        column.addWidget(info)
        layout.addWidget(body)

        footer = QFrame()
        footer.setObjectName("AutoProgramConfirmFooter")
        footer.setFixedHeight(_px(48))
        row = QHBoxLayout(footer)
        row.setContentsMargins(_px(16), 0, _px(16), 0)
        row.setSpacing(_px(8))
        row.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("AutoProgramSecondary")
        self.cancel_button.clicked.connect(self.reject)
        self.replace_button = QPushButton("Replace program")
        self.replace_button.setObjectName("AutoProgramDestructive")
        self.replace_button.setAutoDefault(False)
        self.replace_button.clicked.connect(self.accept)
        for button in (self.cancel_button, self.replace_button):
            button.setFixedHeight(_px(28))
            row.addWidget(button)
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()
        layout.addWidget(footer)


# ---------------------------------------------------------------------------
# The dialog
# ---------------------------------------------------------------------------
class AutoProgramDialog(QDialog):
    """`programmer` is AppCore's AutoProgrammer, `songs` the setlist and
    `current` the loaded song, which is picked first."""

    def __init__(self, programmer, songs, current, parent=None):
        super().__init__(parent)
        self.programmer = programmer
        self.setObjectName("AutoProgramDialog")
        self.setModal(True)
        self.setWindowTitle("Auto-program song")
        self._songs = list(songs)
        self._take = None             # the take on show, when it can be applied
        self._lanes_take = None       # the last take, whose lanes the table shows
        self._wanted = None           # (song, options) of the take asked for
        self._running = False         # the progress panel is up
        self._clock = QElapsedTimer()
        self._steps: Dict[str, Tuple[int, Optional[int]]] = {}   # step -> (started, ended) ms
        self._fields: Dict[str, Tuple[Field, QWidget, QLabel]] = {}
        self._feel = DEFAULT_FEEL     # the FEELS entry the slider is on
        self._file_name = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        body = QWidget()
        body.setObjectName("AutoProgramBody")
        column = QVBoxLayout(body)
        column.setContentsMargins(_px(16), _px(12), _px(16), _px(12))
        column.setSpacing(_px(10))
        column.addLayout(self._build_inputs())
        column.addLayout(self._build_feel())
        column.addWidget(self._build_advanced())
        column.addWidget(self._build_summary())
        column.addWidget(self._build_notices())
        column.addWidget(self._build_middle())
        column.addWidget(self._build_error(), 1)
        column.addWidget(self._build_tables(), 1)
        layout.addWidget(body, 1)
        layout.addWidget(self._build_footer())

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._request)
        self._ticker = QTimer(self)
        self._ticker.setInterval(100)
        self._ticker.timeout.connect(self._tick)

        programmer.stage.connect(self._on_stage)
        programmer.generated.connect(self._on_generated)
        programmer.failed.connect(self._on_failed)
        self.finished.connect(self._on_closed)

        self._on_advanced_toggled(False)
        fit_to_screen(self, _px(980), _px(700))
        if not self._songs:
            self._show_error("The setlist is empty",
                             "Add a song with a guide or backing track to the setlist, then "
                             "auto-program it.", note="Nothing to analyse.")
            return
        start = current if current in self._songs else self._songs[0]
        self.song_combo.blockSignals(True)
        self.song_combo.setCurrentIndex(self._songs.index(start))
        self.song_combo.blockSignals(False)
        self._on_song_changed()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    @staticmethod
    def _caption(text: str, object_name: str = "AutoProgramCaption") -> QLabel:
        label = QLabel(text.upper())
        label.setObjectName(object_name)
        _spaced(label, 1)
        return label

    def _captioned(self, text: str, field: QWidget) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(_px(4))
        box.addWidget(self._caption(text))
        box.addWidget(field)
        return box

    def _build_inputs(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(_px(16))
        self.song_combo = QComboBox()
        self.song_combo.setObjectName("AutoProgramSong")
        self.song_combo.setFixedSize(_px(188), _px(26))
        for song in self._songs:
            self.song_combo.addItem(song.title or "Untitled")
        self.song_combo.currentIndexChanged.connect(self._on_song_changed)
        row.addLayout(self._captioned("Song", self.song_combo))

        width, height = _px(168), _px(26)
        holder = QWidget()
        holder.setFixedSize(2 * width - 1, height)
        self._track_group = QButtonGroup(holder)
        self._track_group.setExclusive(True)
        self.track_buttons: Dict[str, QPushButton] = {}
        self._track_labels: Dict[str, Tuple[QLabel, _Elided]] = {}
        for i, track in enumerate((GUIDE, BACKING)):
            button = QPushButton(holder)
            button.setObjectName("AutoProgramTrack")
            button.setProperty("seg", "left" if i == 0 else "right")
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setGeometry(i * (width - 1), 0, width, height)
            inner = QHBoxLayout(button)
            inner.setContentsMargins(_px(10), 0, _px(10), 0)
            inner.setSpacing(_px(6))
            name = QLabel(TRACK_NAMES[track])
            name.setObjectName("AutoProgramTrackName")
            file = _Elided()
            file.setObjectName("AutoProgramTrackFile")
            _plain(file)
            for label in (name, file):
                label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            inner.addWidget(name)
            inner.addWidget(file, 1)
            self._track_group.addButton(button)
            self.track_buttons[track] = button
            self._track_labels[track] = (name, file)
            button.toggled.connect(self._restyle_tracks)
        self._track_group.buttonClicked.connect(lambda _button: self._request())
        row.addLayout(self._captioned("Audio to analyse", holder))

        self.palette_combo = _PaletteCombo()
        self.palette_combo.setObjectName("AutoProgramPalette")
        self.palette_combo.setFixedSize(_px(150), _px(26))
        self.palette_combo.setIconSize(QSize(_px(SWATCH_W), _px(SWATCH_H)))
        self.palette_combo.setMaxVisibleItems(24)
        self.palette_combo.addItem("Auto", None)
        self.palette_combo.insertSeparator(self.palette_combo.count())
        for name in sorted(colours.PALETTES):
            self.palette_combo.addItem(name.title(), name)
        self.palette_combo.insertSeparator(self.palette_combo.count())
        for mode in colours.MODES:
            self.palette_combo.addItem(mode.title(), mode)
            self.palette_combo.setItemData(
                self.palette_combo.count() - 1,
                f"The fixture Palette tab's {mode.title()}, from the hue beside it",
                Qt.ItemDataRole.ToolTipRole)
        self.palette_combo.insertSeparator(self.palette_combo.count())
        self.palette_combo.addItem("Custom", colours.CUSTOM)
        self.palette_combo.setToolTip(
            "Three hues and white that every fixture wears: a preset, a mode of the fixture "
            "palette walked from a hue, or three colours of your own; in Auto, chosen from "
            "the song's key and drive")

        self.hue_combo = _HueCombo()
        self.hue_combo.setObjectName("AutoProgramHue")
        self.hue_combo.setFixedSize(_px(56), _px(26))
        self.hue_combo.setIconSize(QSize(_px(SWATCH_W), _px(SWATCH_H)))
        self.hue_combo.setMaxVisibleItems(14)
        self.hue_combo.view().setMinimumWidth(_px(140))
        ratio = self.devicePixelRatioF()
        singles = [_fills(f"single:{name}")[0] for name in colours.HUES]
        self.hue_combo.addItem(_swatch_icon(singles, ratio), "Random", colours.RANDOM)
        self.hue_combo.insertSeparator(1)
        for name, fill in zip(colours.HUES, singles):
            self.hue_combo.addItem(_swatch_icon([fill], ratio), name.title(), name)

        self._custom: List[QColor] = []      # its three colours, once Custom is picked
        self._drawn: Optional[str] = None    # the hue the last Random take drew
        self.custom_buttons: List[QPushButton] = []
        for i, tip in enumerate(CUSTOM_TIPS):
            button = QPushButton()
            button.setObjectName("AutoProgramSwatch")
            button.setFixedSize(_px(26), _px(26))
            button.setAutoDefault(False)
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked, i=i: self._pick_custom(i))
            self.custom_buttons.append(button)

        holder = QWidget()
        line = QHBoxLayout(holder)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(_px(6))
        for widget in [self.palette_combo, self.hue_combo] + self.custom_buttons:
            line.addWidget(widget)
        self._refresh_palette()
        self.palette_combo.currentIndexChanged.connect(self._on_palette_changed)
        self.hue_combo.currentIndexChanged.connect(self._on_hue_changed)
        row.addLayout(self._captioned("Palette", holder))
        row.addStretch(1)
        row.setAlignment(Qt.AlignmentFlag.AlignBottom)
        return row

    def _build_feel(self) -> QGridLayout:
        """The Feel slider, the pace it comes to in the song's bars, and the
        Advanced button that opens the values it sets."""
        grid = QGridLayout()
        grid.setHorizontalSpacing(_px(16))
        grid.setVerticalSpacing(_px(4))
        self.feel_slider = QSlider(Qt.Orientation.Horizontal)
        self.feel_slider.setObjectName("AutoProgramFeel")
        self.feel_slider.setRange(0, len(FEELS) - 1)
        self.feel_slider.setPageStep(1)
        self.feel_slider.setTickInterval(1)
        self.feel_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.feel_slider.setValue(self._feel)
        self.feel_slider.setFixedWidth(_px(280))
        self.feel_slider.setToolTip(
            "How busy the show is: sets Pace, Looks per part, Glow below and Glow level in "
            "Advanced. Still holds one look a section, Frantic changes every beat. Edits in "
            "Advanced go on top of the feel; moving the slider puts its values back.")
        self.feel_slider.valueChanged.connect(self._on_feel_changed)
        self.feel_ticks = SliderTickLabels(self.feel_slider, [feel.name for feel in FEELS])
        self.pace_readout = QLabel()
        self.pace_readout.setObjectName("AutoProgramPaceReadout")
        self.advanced_button = QToolButton()
        self.advanced_button.setObjectName("AutoProgramAdvanced")
        self.advanced_button.setCheckable(True)
        self.advanced_button.setFixedHeight(_px(26))
        inner = QHBoxLayout(self.advanced_button)
        inner.setContentsMargins(_px(10), 0, _px(10), 0)
        inner.setSpacing(_px(8))
        self._advanced_text = QLabel("Advanced")
        self._advanced_text.setObjectName("AutoProgramAdvancedText")
        self._advanced_count = QLabel()
        self._advanced_count.setObjectName("AutoProgramAdvancedCount")
        self._advanced_arrow = QLabel("▼")
        self._advanced_arrow.setObjectName("AutoProgramAdvancedArrow")
        for label in (self._advanced_text, self._advanced_count, self._advanced_arrow):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            inner.addWidget(label)
        self.advanced_button.toggled.connect(self._on_advanced_toggled)
        grid.addWidget(self._caption("Feel"), 0, 0)
        grid.addWidget(self.feel_slider, 1, 0)
        grid.addWidget(self.pace_readout, 1, 1)
        grid.addWidget(self.feel_ticks, 2, 0)
        grid.addWidget(self.advanced_button, 1, 3)
        grid.setColumnStretch(2, 1)
        return grid

    def _build_advanced(self) -> QFrame:
        self.advanced_panel = QFrame()
        self.advanced_panel.setObjectName("AutoProgramAdvancedPanel")
        row = QHBoxLayout(self.advanced_panel)
        row.setContentsMargins(_px(10), _px(8), _px(10), _px(8))
        row.setSpacing(_px(16))
        self.reset_button = QPushButton("Reset to defaults")
        self.reset_button.setObjectName("AutoProgramReset")
        self.reset_button.setAutoDefault(False)
        self.reset_button.setFixedHeight(_px(20))
        self.reset_button.setToolTip("Every value back: the analysis's defaults, and the "
                                     "generation's as the Feel sets them")
        self.reset_button.clicked.connect(self._reset_fields)
        row.addLayout(self._field_group("Analysis", "changes re-run the analysis",
                                        ANALYSIS_FIELDS), 1)
        divider = QFrame()
        divider.setObjectName("AutoProgramDivider")
        divider.setFixedWidth(1)
        row.addWidget(divider)
        row.addLayout(self._field_group("Generation", "changes re-program only",
                                        GENERATION_FIELDS, self.reset_button), 1)
        self.advanced_panel.hide()
        return self.advanced_panel

    def _field_group(self, title, note, fields, corner=None) -> QVBoxLayout:
        group = QVBoxLayout()
        group.setSpacing(_px(6))
        head = QHBoxLayout()
        head.setSpacing(_px(8))
        head.addWidget(self._caption(title), 0, Qt.AlignmentFlag.AlignBaseline)
        hint = QLabel(note)
        hint.setObjectName("AutoProgramGroupNote")
        head.addWidget(hint, 0, Qt.AlignmentFlag.AlignBaseline)
        head.addStretch(1)
        if corner is not None:
            head.addWidget(corner)
        group.addLayout(head)
        grid = QGridLayout()
        grid.setHorizontalSpacing(_px(12))
        grid.setVerticalSpacing(_px(4))
        for i, field in enumerate(fields):
            label = QLabel(field.label)
            label.setObjectName("AutoProgramFieldLabel")
            label.setToolTip(field.tip)
            control = self._field_control(field)
            grid.addWidget(label, i // 2, 2 * (i % 2))
            grid.addWidget(control, i // 2, 2 * (i % 2) + 1)
            self._fields[field.key] = (field, control, label)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)
        group.addLayout(grid)
        return group

    def _field_control(self, field: Field) -> QWidget:
        spin = _DoubleSpin() if field.decimals else _Spin()
        if field.decimals:
            spin.setDecimals(field.decimals)
        spin.setObjectName("AutoProgramField")
        spin.setRange(field.minimum, field.maximum)
        spin.setSingleStep(field.step)
        spin.setSuffix(field.suffix)
        spin.setValue(self._base(field))
        spin.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        spin.setToolTip(field.tip)
        spin.setFixedSize(_px(96), _px(22))
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(self._on_field_changed)
        return spin

    def _build_summary(self) -> QFrame:
        strip = QFrame()
        strip.setObjectName("AutoProgramSummary")
        strip.setFixedHeight(_px(28))
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(_px(22))
        self._summary: Dict[str, Tuple[QWidget, QLabel, QLabel]] = {}
        for key, title in (("tempo", "Song tempo"), ("detected", "Detected"),
                           ("fit", "Grid fit"), ("bars", "Bars"),
                           ("downbeat", "First downbeat"), ("until", "Sound until")):
            item = QWidget()
            inner = QHBoxLayout(item)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.setSpacing(_px(6))
            name = QLabel(title)
            name.setObjectName("AutoProgramSummaryKey")
            value = QLabel()
            value.setObjectName("AutoProgramSummaryValue")
            sub = QLabel()
            sub.setObjectName("AutoProgramSummarySub")
            for label in (name, value, sub):
                inner.addWidget(label)
            row.addWidget(item)
            self._summary[key] = (item, value, sub)
        row.addStretch(1)
        return strip

    def _build_notices(self) -> QListWidget:
        self.notices = QListWidget()
        self.notices.setObjectName("AutoProgramNotices")
        self.notices.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.notices.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.notices.setIconSize(QSize(_px(14), _px(14)))
        self.notices.setUniformItemSizes(True)
        self.notices.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.notices.hide()
        return self.notices

    def _build_middle(self) -> QStackedWidget:
        """The song map, or in its place the progress of the analysis."""
        self.middle = QStackedWidget()
        self.middle.setFixedHeight(_px(MAP_H) + 2)
        frame = QFrame()
        frame.setObjectName("AutoProgramMapFrame")
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(1, 1, 1, 1)
        self.song_map = SongMapWidget()
        inner.addWidget(self.song_map)
        self.middle.addWidget(frame)
        self.middle.addWidget(self._build_progress())
        return self.middle

    def _build_progress(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("AutoProgramProgressPanel")
        row = QHBoxLayout(panel)
        row.setContentsMargins(_px(28), 0, _px(28), 0)
        row.setSpacing(_px(40))
        steps = QWidget()
        steps.setFixedWidth(_px(360))
        column = QVBoxLayout(steps)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(_px(8))
        column.addStretch(1)
        self._step_rows: Dict[str, Tuple[_StepIcon, QLabel, QLabel]] = {}
        for step in (DECODE, ANALYSE, PROGRAM):
            line = QHBoxLayout()
            line.setSpacing(_px(10))
            icon = _StepIcon()
            text = QLabel()
            text.setObjectName("AutoProgramStep")
            _plain(text)
            clock = QLabel()
            clock.setObjectName("AutoProgramStepTime")
            line.addWidget(icon)
            line.addWidget(text, 1)
            line.addWidget(clock)
            column.addLayout(line)
            self._step_rows[step] = (icon, text, clock)
        column.addStretch(1)
        row.addWidget(steps)

        right = QVBoxLayout()
        right.setSpacing(_px(8))
        right.addStretch(1)
        head = QHBoxLayout()
        self._progress_text = QLabel()
        self._progress_text.setObjectName("AutoProgramProgressText")
        _plain(self._progress_text)
        self._progress_clock = QLabel()
        self._progress_clock.setObjectName("AutoProgramStepTime")
        head.addWidget(self._progress_text, 1)
        head.addWidget(self._progress_clock)
        right.addLayout(head)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("AutoProgramProgress")
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 3)
        self.progress_bar.setFixedHeight(_px(12))
        right.addWidget(self.progress_bar)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel analysis")
        self.cancel_button.setObjectName("AutoProgramSecondary")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setFixedHeight(_px(26))
        self.cancel_button.clicked.connect(self._cancel)
        buttons.addWidget(self.cancel_button)
        right.addLayout(buttons)
        right.addStretch(1)
        row.addLayout(right, 1)
        return panel

    def _build_error(self) -> QFrame:
        """In place of the map and the tables when there is nothing to show:
        no audio, an analysis that failed or was cancelled."""
        self.error_panel = QFrame()
        self.error_panel.setObjectName("AutoProgramErrorPanel")
        column = QVBoxLayout(self.error_panel)
        column.setContentsMargins(_px(24), _px(24), _px(24), _px(24))
        column.setSpacing(_px(10))
        column.addStretch(1)
        mark = QLabel("!")
        mark.setObjectName("AutoProgramErrorMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(_px(36), _px(36))
        self._error_title = QLabel()
        self._error_title.setObjectName("AutoProgramErrorTitle")
        self._error_body = QLabel()
        self._error_body.setObjectName("AutoProgramErrorBody")
        self._error_body.setWordWrap(True)
        self._error_body.setFixedWidth(_px(500))
        self._error_detail = QLabel()
        self._error_detail.setObjectName("AutoProgramErrorDetail")
        self._error_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        _plain(self._error_title, self._error_body, self._error_detail)
        self._error_detail.setWordWrap(True)
        self.retry_button = QPushButton("Retry")
        self.retry_button.setObjectName("AutoProgramSecondary")
        self.retry_button.setAutoDefault(False)
        self.retry_button.setFixedHeight(_px(26))
        self.retry_button.clicked.connect(self._request)
        for widget in (mark, self._error_title, self._error_body, self._error_detail,
                       self.retry_button):
            if isinstance(widget, QLabel) and widget is not mark:
                widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            column.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addStretch(1)
        self.error_panel.hide()
        return self.error_panel

    def _table(self, name: str, headers, widths, stretch: int, right=()) -> QTableWidget:
        table = QTableWidget()
        # named before it has columns: a header polished before its table had
        # a name never matches the `QTableWidget#... QHeaderView` rules
        table.setObjectName(name)
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().hide()
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = table.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionsClickable(False)
        header.setFixedHeight(_px(24))
        _spaced(header, 1)
        for col, width in enumerate(widths):
            if col == stretch:
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
                table.setColumnWidth(col, _px(width))
            align = (Qt.AlignmentFlag.AlignRight if col in right else Qt.AlignmentFlag.AlignLeft)
            table.horizontalHeaderItem(col).setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
        rows = table.verticalHeader()
        rows.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        rows.setDefaultSectionSize(_px(22))
        return table

    def _table_box(self, title: str, table: QTableWidget) -> Tuple[QWidget, QLabel]:
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(_px(4))
        head = QHBoxLayout()
        head.setSpacing(_px(6))
        caption = QLabel(title)
        caption.setObjectName("AutoProgramTableCaption")
        count = QLabel()
        count.setObjectName("AutoProgramTableCount")
        head.addWidget(caption)
        head.addWidget(count)
        head.addStretch(1)
        column.addLayout(head)
        column.addWidget(table, 1)
        return box, count

    def _build_tables(self) -> QWidget:
        self.tables = QWidget()
        row = QHBoxLayout(self.tables)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(_px(12))
        self.sections_table = self._table("AutoProgramSections",
                                          ["BARS", "START", "PART", "LEVEL", "BUILDS"],
                                          [70, 72, 0, 58, 70], stretch=2)
        self.sections_table.setIconSize(QSize(_px(10), _px(10)))
        self._sections_empty = QLabel("Sections appear when the analysis finishes.",
                                      self.sections_table.viewport())
        self._sections_empty.setObjectName("AutoProgramTableEmpty")
        self._sections_empty.move(_px(8), _px(14))
        self._sections_empty.adjustSize()
        box, self._sections_count = self._table_box("Sections", self.sections_table)
        box.setFixedWidth(_px(410))
        row.addWidget(box)

        self.fixtures_table = self._table("AutoProgramFixtures",
                                          ["CH", "FIXTURE", "ROLE", "SCENES", "CLIPS"],
                                          [48, 0, 96, 58, 46], stretch=1, right=(3, 4))
        self.fixtures_table.setItemDelegate(_FixtureDelegate(self.fixtures_table))
        self._fixtures_empty = QLabel("Fixtures appear when the programming finishes.",
                                      self.fixtures_table.viewport())
        self._fixtures_empty.setObjectName("AutoProgramTableEmpty")
        self._fixtures_empty.move(_px(8), _px(14))
        self._fixtures_empty.adjustSize()
        box, self._fixtures_count = self._table_box("Fixtures", self.fixtures_table)
        row.addWidget(box, 1)
        return self.tables

    def _build_footer(self) -> QFrame:
        footer = QFrame()
        footer.setObjectName("AutoProgramFooter")
        footer.setFixedHeight(_px(54))
        row = QHBoxLayout(footer)
        row.setContentsMargins(_px(16), 0, _px(16), 0)
        row.setSpacing(_px(8))
        texts = QVBoxLayout()
        texts.setSpacing(_px(2))
        self.stats_label = QLabel()
        self.stats_label.setObjectName("AutoProgramStats")
        self.note_label = _Elided()
        self.note_label.setObjectName("AutoProgramNote")
        _plain(self.note_label)
        texts.addStretch(1)
        texts.addWidget(self.stats_label)
        texts.addWidget(self.note_label)
        texts.addStretch(1)
        row.addLayout(texts, 1)

        self.export_button = QPushButton("Export to .mid…")
        self.export_button.setObjectName("AutoProgramSecondary")
        self.export_button.setToolTip("Write this take to a .mid of its own; the show is "
                                      "left as it is")
        self.export_button.clicked.connect(self._export)
        divider = QFrame()
        divider.setObjectName("AutoProgramDivider")
        divider.setFixedSize(1, _px(28))
        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("AutoProgramSecondary")
        self.close_button.clicked.connect(self.reject)
        self.apply_button = QPushButton("Apply…")
        self.apply_button.setObjectName("AutoProgramPrimary")
        self.apply_button.clicked.connect(self._apply)
        for button in (self.export_button, self.close_button, self.apply_button):
            button.setFixedHeight(_px(28))
            button.setAutoDefault(False)
        row.addWidget(self.export_button)
        row.addSpacing(_px(4))
        row.addWidget(divider)
        row.addSpacing(_px(4))
        row.addWidget(self.close_button)
        row.addWidget(self.apply_button)
        self.apply_button.setDefault(True)
        return footer

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------
    def song(self):
        index = self.song_combo.currentIndex()
        return self._songs[index] if 0 <= index < len(self._songs) else None

    def track(self) -> Optional[str]:
        return next((t for t, b in self.track_buttons.items() if b.isChecked()), None)

    def _base(self, field: Field):
        """What `field` reads until it is edited by hand: the feel's value for
        the fields a feel sets, the engine's default for the rest."""
        if field.key in FEEL_KEYS:
            return _feel_value(FEELS[self._feel], field)
        return _default(field)

    def _shown(self, key: str) -> float:
        return self._fields[key][1].value()

    def _engine(self, key: str):
        field, control, _label = self._fields[key]
        if not field.decimals and field.per == 1.0:
            return int(control.value())
        return round(control.value() * field.per, 6)

    def palette(self) -> Optional[str]:
        """The palette spec the inputs say; None: Auto."""
        kind = self.palette_combo.currentData()
        if kind in colours.MODES:
            return f"{kind}:{self.hue_combo.currentData()}"
        if kind == colours.CUSTOM:
            return self._custom_spec()
        return kind

    def _custom_spec(self) -> str:
        return f"{colours.CUSTOM}:" + ",".join(c.name() for c in self._custom)

    def _on_palette_changed(self, _index):
        if self.palette_combo.currentData() == colours.CUSTOM and not self._custom:
            # start from the colours on show
            shown = self._lanes_take.palette if self._lanes_take else "ember"
            self._custom = _fills(shown)
        self._refresh_palette()
        self._request()

    def _on_hue_changed(self, _index):
        self._refresh_palette()
        self._request()

    def _pick_custom(self, i: int):
        picked = QColorDialog.getColor(self._custom[i], self, CUSTOM_TIPS[i].split(":")[0])
        if picked.isValid():
            self._set_custom(i, picked)

    def _set_custom(self, i: int, colour: QColor):
        self._custom[i] = colour
        self._refresh_palette()
        self._request()

    def _refresh_palette(self):
        """Show the hue beside a mode and the swatches beside Custom, and
        paint every entry's colours: a mode's from the hue picked — for
        Random, the one the last take drew."""
        kind = self.palette_combo.currentData()
        self.hue_combo.setVisible(kind in colours.MODES)
        for button in self.custom_buttons:
            button.setVisible(kind == colours.CUSTOM)
        hue = self.hue_combo.currentData()
        if hue == colours.RANDOM:
            self.hue_combo.setToolTip("Hue: random, drawn from the take"
                                      + (f" — this one drew {self._drawn.title()}"
                                         if self._drawn else ""))
            hue = self._drawn or colours.HUES[0]
        else:
            self.hue_combo.setToolTip(f"Hue: {hue.title()}")
        ratio = self.devicePixelRatioF()
        for i in range(self.palette_combo.count()):
            data = self.palette_combo.itemData(i)
            if data in colours.PALETTES:
                spec = data
            elif data in colours.MODES:
                spec = f"{data}:{hue}"
            elif data == colours.CUSTOM and self._custom:
                spec = self._custom_spec()
            else:
                continue
            self.palette_combo.setItemIcon(i, _swatch_icon(_fills(spec), ratio))
        for button, colour in zip(self.custom_buttons, self._custom):
            button.setStyleSheet(
                f"QPushButton#AutoProgramSwatch {{ background-color: {colour.name()}; }}")

    def options(self) -> Options:
        take = int(self._shown("take"))
        return Options(
            track=self.track(),
            pace=int(self._shown("pace")),
            palette=self.palette(),
            seed=None if take == 1 else take,
            tuning=analysis.Tuning(**{f.key: self._engine(f.key) for f in ANALYSIS_FIELDS}),
            style=Style(**{f.key: self._engine(f.key) for f in GENERATION_FIELDS
                           if f.key not in ("pace", "take")}))

    def _on_song_changed(self, *_args):
        song = self.song()
        self._show_pace()
        if song is None:
            return
        have = tracks(song)
        for track, button in self.track_buttons.items():
            path = song.guide if track == GUIDE else song.audio
            _name, file = self._track_labels[track]
            file.setText(os.path.basename(path) if path else "no file")
            button.setToolTip(path if track in have else
                              (f"Not found: {path}" if path else
                               f"The song has no {TRACK_NAMES[track].lower()} track"))
            button.setEnabled(track in have)
        self._track_group.setExclusive(False)       # so neither can be checked
        for track, button in self.track_buttons.items():
            button.setChecked(bool(have) and track == have[0])
        self._track_group.setExclusive(True)
        self._restyle_tracks()
        self._request()

    def _restyle_tracks(self, *_args):
        for track, button in self.track_buttons.items():
            state = "on" if button.isChecked() else "off" if button.isEnabled() else "disabled"
            for label in self._track_labels[track]:
                _restyle(label, state=state)
            if button.isChecked():
                button.raise_()                     # its border over the shared seam

    def _on_advanced_toggled(self, open_: bool):
        self.advanced_panel.setVisible(open_)
        self._advanced_arrow.setText("▲" if open_ else "▼")
        _restyle(self._advanced_text, state="open" if open_ else "")
        self._fit_advanced_button()

    def _fit_advanced_button(self):
        changed = sum(1 for field, control, _label in self._fields.values()
                      if control.value() != self._base(field))
        self._advanced_count.setText(f"{changed} changed")
        self._advanced_count.setVisible(changed > 0)
        for label in (self._advanced_text, self._advanced_count, self._advanced_arrow):
            label.ensurePolished()
        self.advanced_button.setFixedWidth(self.advanced_button.layout().sizeHint().width())

    def _on_field_changed(self, *_args):
        for field, control, label in self._fields.values():
            changed = control.value() != self._base(field)
            label.setText(field.label + (" •" if changed else ""))
            _restyle(label, changed=changed)
            _restyle(control, changed=changed)
        self._fit_advanced_button()
        self._show_pace()
        self._debounce.start()

    def _show_pace(self):
        """The pace in the selected song's bars and beats (its meter's)."""
        song = self.song()
        pace = int(self._shown("pace"))
        self.pace_readout.setText(
            f"Pace {pace}: {pace_readout(pace, song.meter if song is not None else None)}")

    def _on_feel_changed(self, index: int):
        """Put the feel's values into the fields it sets, over any edits there."""
        self._feel = index
        self.feel_ticks.set_active(index)
        for field, control, _label in self._fields.values():
            if field.key in FEEL_KEYS:
                control.blockSignals(True)
                control.setValue(self._base(field))
                control.blockSignals(False)
        self._on_field_changed()

    def _reset_fields(self):
        for field, control, _label in self._fields.values():
            control.blockSignals(True)
            control.setValue(self._base(field))
            control.blockSignals(False)
        self._on_field_changed()
        self._debounce.stop()
        self._request()

    # ------------------------------------------------------------------
    # Asking for a take
    # ------------------------------------------------------------------
    def _request(self, *_args):
        """Ask for the take the inputs describe: analysed and programmed, or
        only programmed when the programmer holds its analysis."""
        self._debounce.stop()
        song = self.song()
        if song is None:
            return
        if tracks(song):
            asked = (song, self.options())
            shown = self._take and (self._take.song, self._take.options)
            if asked in (self._wanted, shown):
                return
        self._take = None
        self._set_actions(False)
        if not tracks(song):
            self.programmer.cancel()
            self._wanted = None
            title = song.title or "Untitled"
            self._show_error(f"“{title}” has no audio",
                             "Auto-program reads the song’s guide or backing track, and this "
                             "song has neither. Add a track to it in the setlist, or pick "
                             "another song above.", note="Nothing to analyse for this song.")
            return
        options = self.options()
        self._wanted = (song, options)
        mapped = self.programmer.analysis_for(song, options)
        if mapped is None:
            self._show_running(song)
        else:
            self._show_programming(song, mapped)
        self.programmer.generate(song, options)

    def _show_running(self, song):
        """The analysis runs: its steps in place of the map, nothing else
        known yet."""
        self._running = True
        self._clock.start()
        self._steps = {DECODE: (0, None)}
        count = self.programmer.fixture_count()
        path = song.guide if self.track() == GUIDE else song.audio
        labels = {DECODE: f"Decoding {os.path.basename(path)}",
                  ANALYSE: "Analysing beats, bars and sections",
                  PROGRAM: f"Programming {_plural(count, 'fixture')}"}
        for step, (_icon, text, _clock) in self._step_rows.items():
            text.setText(labels[step])
        self._file_name = os.path.basename(path)
        self._show_panels("running")
        self.palette_combo.set_picked(None)
        self._fill_summary(song, None, "…")
        self._set_notices([])
        self._fill_sections(None)
        self._fill_fixtures(self._lanes_take, pending=True)
        self._set_footer("Decoding → Analysing → Programming",
                         "Apply is available when the analysis finishes.", "busy")
        self._tick()
        self._ticker.start()

    def _show_programming(self, song, mapped):
        """Only the programming runs: the analysis stays on show."""
        self._stop_running()
        self._show_panels("map")
        self.song_map.set_map(mapped.map)
        self._fill_summary(song, mapped, None)
        self._fill_sections(mapped.map)
        self._fill_fixtures(self._lanes_take, pending=True)
        count = self.programmer.fixture_count()
        self._set_footer(f"Programming {_plural(count, 'fixture')}…",
                         "Apply is available when the programming finishes.", "busy")

    def _stop_running(self):
        self._running = False
        self._ticker.stop()

    def _on_stage(self, step):
        if self._wanted is None or not self._running or step in self._steps:
            return
        now = self._clock.elapsed()
        for begun, (started, ended) in list(self._steps.items()):
            if ended is None:
                self._steps[begun] = (started, now)
        self._steps[step] = (now, None)
        self._tick()

    def _tick(self):
        """The progress panel, brought up to date."""
        now = self._clock.elapsed() if self._clock.isValid() else 0
        current = next((s for s, (_a, end) in self._steps.items() if end is None), DECODE)
        for step, (icon, text, clock) in self._step_rows.items():
            started, ended = self._steps.get(step, (None, None))
            if started is None:
                state, shown = "next", ""
            elif ended is None:
                state, shown = "now", f"{(now - started) / 1000:.1f} s"
            else:
                state, shown = "done", f"{(ended - started) / 1000:.1f} s"
            icon.set_state(state)
            _restyle(text, state=state)
            clock.setText(shown)
        done = sum(1 for _s, (_a, end) in self._steps.items() if end is not None)
        self.progress_bar.setValue(done)
        verb = {DECODE: "Decoding", ANALYSE: "Analysing", PROGRAM: "Programming"}[current]
        self._progress_text.setText(f"{verb} {self._file_name}" if current != PROGRAM
                                    else self._step_rows[PROGRAM][1].text())
        self._progress_clock.setText(f"{now / 1000:.1f} s")

    def _cancel(self):
        self.programmer.cancel()
        self._wanted = None
        self._stop_running()
        self._show_error("Analysis cancelled",
                         "Nothing was analysed or programmed. Run it again, or change the "
                         "song, its audio or the Advanced values above.",
                         action="Run again", note="Analysis cancelled.", note_state="busy")

    # ------------------------------------------------------------------
    # Answers
    # ------------------------------------------------------------------
    def _on_generated(self, take):
        if self._wanted is None or (take.song, take.options) != self._wanted:
            return                    # a take these inputs did not ask for
        self._wanted = None
        self._stop_running()
        self._take = take
        self._lanes_take = take
        song = take.song
        if take.options.palette is None:
            self.palette_combo.set_picked(colours.label(take.palette))
        if take.options.palette and take.options.palette.endswith(":" + colours.RANDOM):
            self._drawn = take.palette.partition(":")[2]
            self._refresh_palette()
        self._show_panels("map")
        self.song_map.set_map(take.analysis.map)
        self._fill_summary(song, take.analysis, None)
        self._fill_sections(take.analysis.map)
        self._fill_fixtures(take, pending=False)
        notices = [("warning", _capital(w)) for w in take.warnings]
        if not take.lanes:
            notices.append(("error", NO_FIXTURES))
        else:
            notices += [("warning", f"Skipped {s}") for s in take.skipped]
        self._set_notices(notices)
        stats = f"{_plural(take.events, 'event')} · {_plural(take.fades, 'fade')}"
        if not take.lanes:
            self._take = None
            self._set_footer(stats, "Nothing to apply.", "error")
            return
        title = song.title or "Untitled"
        note = f"Apply replaces the whole program of “{title}”. A version is saved first."
        if notices:
            note += " Warnings don’t block it."
        self._set_footer(stats, note, "")
        self._set_actions(True)

    def _on_failed(self, step, message):
        if self._wanted is None:
            return
        song, options = self._wanted
        self._wanted = None
        self._stop_running()
        mapped = self.programmer.analysis_for(song, options)
        if step == PROGRAM and mapped is not None:
            self._show_programming(song, mapped)
            self._fill_fixtures(None, pending=False)
            self._set_notices([("error", f"Could not program the lights: {message}")])
            self._set_footer("", "Nothing to apply.", "error")
            return
        path = song.guide if options.track == GUIDE else song.audio
        name = os.path.basename(path or "")
        if step == DECODE:
            self._show_error(f"Could not decode {name}",
                             "The file exists but its audio can’t be read. Re-export it as WAV "
                             "or MP3 and replace it in the song, then retry.",
                             detail=message, action="Retry",
                             note="The audio could not be read.")
        else:
            self._show_error(f"Could not analyse {name}",
                             "The analysis stopped on an error. Retry, or try the other track "
                             "or other Advanced values.", detail=message, action="Retry",
                             note="The audio could not be analysed.")

    # ------------------------------------------------------------------
    # Showing
    # ------------------------------------------------------------------
    def _show_panels(self, mode: str):
        """"map" or "running": the map (or the progress) over the tables;
        "error": the error panel instead of both."""
        error = mode == "error"
        self.middle.setVisible(not error)
        self.tables.setVisible(not error)
        self.error_panel.setVisible(error)
        if not error:
            self.middle.setCurrentIndex(1 if mode == "running" else 0)

    def _show_error(self, title, body, detail="", action="", note="", note_state="error"):
        self._stop_running()
        self._take = None
        self._set_actions(False)
        self._error_title.setText(title)
        self._error_body.setText(body)
        # centred in its column, a wrapped label is laid out at its size
        # hint's height, not the height its fixed width wraps to
        self._error_body.ensurePolished()
        self._error_body.setFixedHeight(self._error_body.heightForWidth(_px(500)))
        self._error_detail.setText(detail)
        self._error_detail.setVisible(bool(detail))
        if detail:
            # as wide as its text (padding and border included), wrapping past 600
            self._error_detail.ensurePolished()
            width = self._error_detail.fontMetrics().horizontalAdvance(detail) + _px(22)
            self._error_detail.setFixedWidth(min(width, _px(600)))
        self.retry_button.setText(action or "Retry")
        self.retry_button.setVisible(bool(action))
        self._show_panels("error")
        self.palette_combo.set_picked(None)
        self._fill_summary(self.song(), None, "—")
        self._set_notices([])
        self._set_footer("", note, note_state)

    def _fill_summary(self, song, mapped, placeholder: Optional[str]):
        """The song's tempo, then what the analysis found — or `placeholder`
        for each while it runs ("…") or when it could not ("—")."""
        meter = (song.meter if song is not None else None) or "4/4"
        bpm = self.programmer.song_tempo(song) if song is not None else None
        values = {"tempo": (f"{bpm} BPM · {meter}" if bpm else f"— · {meter}", "", "")}
        if mapped is None:
            values.update({k: (placeholder, "", "") for k in ("detected", "fit", "bars",
                                                              "downbeat", "until")})
        else:
            m = mapped.map
            steady = m.fit >= analysis.STEADY_FIT
            fit = "∞" if math.isinf(m.fit) else f"{m.fit:.2f}×"
            values.update({
                "detected": (f"{m.bpm:.2f} BPM",
                             f"live {m.tracker_bpm:.1f}" if m.tracker_bpm else "", ""),
                "fit": (fit, "steady" if steady else "no steady beat", "" if steady else "warn"),
                "bars": (str(len(m.bars)), "", ""),
                "downbeat": (f"{_signed(m.first_downbeat, 3)} s", "", ""),
                "until": (f"{m.end_of_sound:.2f} s", "", ""),
            })
        for key, (item, value, sub) in self._summary.items():
            text, note, state = values[key]
            value.setText(text)
            sub.setText(note)
            sub.setVisible(bool(note))
            _restyle(value, state=state)
            _restyle(sub, state=state)
            # with nothing found, only the tempo and what was looked for
            item.setVisible(not (placeholder == "—" and key in ("downbeat", "until")))

    def _fill_sections(self, song_map: Optional[analysis.SongMap]):
        table = self.sections_table
        table.setRowCount(0)
        self._sections_empty.setVisible(song_map is None)
        if song_map is None:
            self._sections_count.setText("")
            return
        rows = []
        for part, sec in song_map.parts():
            if part == "END":
                continue
            builds = [b.number for b in song_map.bars
                      if b.build and sec.first <= b.number <= sec.last]
            rows.append((sec.first, sec.last, sec.start, part,
                         analysis.LEVEL_NAMES[sec.level], builds))
        end = next((b.number for b in song_map.bars
                    if abs(b.start - song_map.end_start) < 1e-6), None)
        rows.append((end, end, song_map.end_start, "END", "—", []))
        mono = QFont(table.font())
        mono.setFamilies(MONO)
        mono.setPixelSize(_px(11))
        grey, cyan = QColor("#9a9a9a"), QColor(ACCENT)
        table.setRowCount(len(rows))
        for r, (first, last, start, part, level, builds) in enumerate(rows):
            span = "—" if first is None else str(first) if first == last else f"{first}–{last}"
            cells = [(span, mono, None), (f"{_signed(start, 2)} s", mono, grey),
                     (part, None, None), (level, None, grey),
                     (", ".join(map(str, builds)), mono, cyan)]
            for c, (text, font, colour) in enumerate(cells):
                item = QTableWidgetItem(text)
                if font is not None:
                    item.setFont(font)
                if colour is not None:
                    item.setForeground(colour)
                if c == 2:
                    item.setData(Qt.ItemDataRole.DecorationRole, part_colour(part))
                table.setItem(r, c, item)
        self._sections_count.setText(str(len(rows)))

    def _fill_fixtures(self, take, pending: bool):
        """One row per lane, then the skipped fixtures dimmed, by channel;
        `pending` while the take is being programmed."""
        table = self.fixtures_table
        table.setRowCount(0)
        self._fixtures_empty.setVisible(take is None)
        if take is None:
            self._fixtures_count.setText("")
            return
        rows = [(lane.channel, lane.fixture, lane.role, _lane_note(lane), lane.scenes, lane.clips)
                for lane in take.lanes]
        rows += [(s.channel, s.type, None, f"— {s.reason}", None, None) for s in take.skipped]
        rows.sort(key=lambda row: row[0])
        mono = QFont(table.font())
        mono.setFamilies(MONO)
        mono.setPixelSize(_px(11))
        dim = QColor(DIM)
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        table.setRowCount(len(rows))
        for r, (channel, name, role, note, scenes, clips) in enumerate(rows):
            items = [QTableWidgetItem(f"ch {channel}"), QTableWidgetItem(name),
                     QTableWidgetItem("" if role else "skipped")]
            for count in (scenes, clips):
                items.append(QTableWidgetItem("" if count is None else
                                              "…" if pending else str(count)))
            items[0].setFont(mono)
            items[1].setData(NOTE_ROLE, note)
            items[1].setToolTip(f"{name} {note}".strip())
            if role:
                items[2].setData(CHIP_ROLE, role)
                items[2].setToolTip(ROLE_TIPS[role])
            else:
                items[2].setForeground(QColor(WARN))
                for item in items[:2]:
                    item.setForeground(dim)
            for item in items[3:]:
                item.setFont(mono)
                item.setTextAlignment(right)
            for c, item in enumerate(items):
                table.setItem(r, c, item)
        skipped = len(take.skipped)
        self._fixtures_count.setText(f"{len(take.lanes)} programmed"
                                     + (f" · {skipped} skipped" if skipped else ""))

    def _set_notices(self, notices: List[Tuple[str, str]]):
        """(kind, text) rows, "warning" or "error"; up to NOTICE_ROWS shown."""
        self.notices.clear()
        ratio = self.devicePixelRatioF()
        height = _px(22)
        for kind, text in notices:
            item = QListWidgetItem(_notice_icon(kind, ratio), text)
            item.setToolTip(text)
            item.setForeground(QColor("#ff8a8a" if kind == "error" else "#dcdcdc"))
            item.setSizeHint(QSize(0, height))
            self.notices.addItem(item)
        rows = min(len(notices), NOTICE_ROWS)
        self.notices.setFixedHeight(rows * height + 2 * self.notices.frameWidth())
        self.notices.setVisible(bool(notices))

    def _set_footer(self, stats: str, note: str, state: str):
        self.stats_label.setText(stats)
        self.stats_label.setVisible(bool(stats))
        self.note_label.setText(note)
        _restyle(self.note_label, state=state)

    def _set_actions(self, enabled: bool):
        self.apply_button.setEnabled(enabled)
        self.export_button.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Applying, exporting
    # ------------------------------------------------------------------
    def _confirm(self, take) -> bool:
        return ReplaceDialog(take, self).exec() == QDialog.DialogCode.Accepted

    def _apply(self):
        take = self._take
        if take is None or not self._confirm(take):
            return
        try:
            self.programmer.apply(take)
        except Exception as e:
            self._set_footer(self.stats_label.text(), f"Could not apply: {e}", "error")
            return
        self._take = None
        self.accept()

    def _export(self):
        take = self._take
        if take is None:
            return
        name = f"{take.song.title or 'Untitled'} (auto-program).mid"
        path, _filter = QFileDialog.getSaveFileName(self, "Export to .mid", name,
                                                    "MIDI files (*.mid)")
        if not path:
            return
        if not path.lower().endswith((".mid", ".midi")):
            path += ".mid"
        try:
            shutil.copyfile(take.midi_path, path)
        except OSError as e:
            self._set_footer(self.stats_label.text(), f"Could not export: {e}", "error")
            return
        self._set_footer(self.stats_label.text(), f"Exported to {path}. The show is unchanged.",
                         "")

    def _on_closed(self, _result):
        """Closing drops a take still being made, and lets go of the
        programmer's signals."""
        self._debounce.stop()
        self._ticker.stop()
        self.programmer.cancel()
        self.programmer.stage.disconnect(self._on_stage)
        self.programmer.generated.disconnect(self._on_generated)
        self.programmer.failed.disconnect(self._on_failed)
