# ABOUTME: Fixture-dialog tab for the per-fixture colour palette (note 60).
# ABOUTME: A hue choice (12 fixed hues + random) and a palette mode, packed into
# ABOUTME: one MIDI velocity as hue_index * 6 + mode_index.
"""The palette a fixture cycles through, edited where the fixture is edited.

It used to be a dialog behind a draggable START clip on the timeline. START is
not a clip any more — it is the fixture's own state, one note 60 pinned at t=0
per MIDI channel (see `core/note_layout`) — so the editor lives here, beside the
fixture's scenes and chases.

Offered only for a **Color Changer**: a hazer or a plain dimmer has no colour to
cycle, so the tab would be a control over nothing. `AddFixtureDialog` asks
`applies_to` and hides the tab when the answer is no.

Song-scoped, like the auto/mute/lock flags note 71 carries: the value is read
from and written to the .mid of the song that is loaded right now, so each song
gets its own palette per fixture.
"""

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QLabel, QPushButton, QButtonGroup, QFrame)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPainter, QColor

from core import note_layout
from software_controller.main import (
    PALETTE_MODES, RAINBOW_HUE_12, DEFAULT_HSL_S, DEFAULT_HSL_L,
    generate_song_colors, hsl_to_rgb,
)

RANDOM_HUE_INDEX = note_layout.RANDOM_HUE_INDEX

# The OFL category that earns the tab: only a fixture that can change colour has
# a palette to walk.
COLOR_CHANGER = "Color Changer"


def applies_to(fdef) -> bool:
    """Whether `fdef` is a fixture the palette means anything for."""
    return bool(fdef) and COLOR_CHANGER in getattr(fdef, "categories", ())


class _PalettePreview(QFrame):
    """Renders a palette as equal-width color bands, or a 'random' placeholder."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._colors = None
        self.setMinimumHeight(36)
        self.setFrameShape(QFrame.Shape.StyledPanel)

    def set_colors(self, colors):
        self._colors = colors
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        rect = self.contentsRect()

        if self._colors is None:
            painter.fillRect(rect, QColor(40, 40, 40))
            painter.setPen(QColor(200, 200, 200))
            font = painter.font()
            font.setPointSize(10)
            font.setItalic(True)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "Random hue — preview unavailable")
            return

        if not self._colors:
            return

        n = len(self._colors)
        seg_w = rect.width() / n
        for i, (r, g, b) in enumerate(self._colors):
            x0 = rect.x() + int(round(i * seg_w))
            x1 = rect.x() + int(round((i + 1) * seg_w))
            painter.fillRect(x0, rect.y(), x1 - x0, rect.height(),
                             QColor(int(r), int(g), int(b)))


class FixturePaletteTab(QWidget):
    """Base colour + palette mode for one fixture, as one packed velocity.

    Holds an ANSWER, like the footer's address and the header's rigging: nothing
    is written until the dialog is accepted, and the host asks for
    ``velocity()`` then.
    """

    SWATCH_SIZE = 44

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FixturePaletteTab")
        self.hue_index = 0
        self.palette_mode_index = 0

        self.color_buttons = []
        self.mode_buttons = []
        self.preview_widgets = []

        self._setup_ui()
        self._apply_selection()
        self._refresh_previews()

    # ------------------------------------------------------------------
    # Value
    # ------------------------------------------------------------------
    def set_velocity(self, velocity: int) -> None:
        """Load a packed note-60 velocity into the controls."""
        self.hue_index, self.palette_mode_index = \
            note_layout.unpack_palette(velocity)
        if self.palette_mode_index >= len(PALETTE_MODES):
            self.palette_mode_index = 0
        self._apply_selection()
        self._refresh_previews()

    def velocity(self) -> int:
        """The packed note-60 velocity for the current choices."""
        return note_layout.pack_palette(self.hue_index, self.palette_mode_index)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _setup_ui(self):
        layout = QVBoxLayout(self)

        color_header = QLabel("Base Color")
        color_header.setObjectName("PaletteSectionHeader")
        layout.addWidget(color_header)

        self.color_group = QButtonGroup(self)
        self.color_group.setExclusive(True)

        grid_row = QHBoxLayout()
        grid_row.addStretch()
        grid = QGridLayout()
        grid.setSpacing(6)

        for i in range(len(RAINBOW_HUE_12)):
            r, g, b = hsl_to_rgb(RAINBOW_HUE_12[i], DEFAULT_HSL_S, DEFAULT_HSL_L)
            btn = self._make_swatch(f"rgb({r}, {g}, {b})")
            row, col = divmod(i, 4)
            grid.addWidget(btn, row, col)
            self.color_group.addButton(btn, i)
            self.color_buttons.append(btn)

        self.random_button = QPushButton("?")
        self.random_button.setObjectName("RandomHueButton")
        self.random_button.setCheckable(True)
        self.random_button.setFixedSize(self.SWATCH_SIZE, self.SWATCH_SIZE)
        self.color_group.addButton(self.random_button, RANDOM_HUE_INDEX)

        random_box = QVBoxLayout()
        random_box.setSpacing(2)
        random_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        random_box.addWidget(self.random_button, alignment=Qt.AlignmentFlag.AlignCenter)
        random_label = QLabel("Random")
        random_label.setObjectName("RandomHueLabel")
        random_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        random_box.addWidget(random_label)

        random_wrap = QWidget()
        random_wrap.setLayout(random_box)
        grid.addWidget(random_wrap, 0, 4, 3, 1, Qt.AlignmentFlag.AlignCenter)

        grid_row.addLayout(grid)
        grid_row.addStretch()
        layout.addLayout(grid_row)

        mode_header = QLabel("Palette Mode")
        mode_header.setObjectName("PaletteModeHeader")
        layout.addWidget(mode_header)

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)

        for idx, name in enumerate(PALETTE_MODES):
            row = QHBoxLayout()
            mode_btn = QPushButton(name)
            mode_btn.setObjectName("PaletteModeButton")
            mode_btn.setCheckable(True)
            mode_btn.setFixedWidth(150)
            self.mode_group.addButton(mode_btn, idx)
            self.mode_buttons.append(mode_btn)
            row.addWidget(mode_btn)

            preview = _PalettePreview()
            self.preview_widgets.append(preview)
            row.addWidget(preview, 1)
            layout.addLayout(row)

        layout.addStretch(1)

        self.color_group.idClicked.connect(self._on_color_changed)
        self.mode_group.idClicked.connect(self._on_mode_changed)

    def _make_swatch(self, css_color):
        btn = QPushButton()
        btn.setObjectName("ColorSwatch")
        btn.setCheckable(True)
        btn.setFixedSize(self.SWATCH_SIZE, self.SWATCH_SIZE)
        # Only the per-swatch fill is data-driven; border/checked styling lives
        # in the QSS theme under #ColorSwatch.
        btn.setStyleSheet(f"QPushButton#ColorSwatch {{ background-color: {css_color}; }}")
        return btn

    def _apply_selection(self):
        if self.hue_index >= RANDOM_HUE_INDEX:
            self.random_button.setChecked(True)
        else:
            self.color_buttons[self.hue_index].setChecked(True)
        self.mode_buttons[self.palette_mode_index].setChecked(True)

    def _on_color_changed(self, hue_index):
        self.hue_index = hue_index
        self._refresh_previews()

    def _on_mode_changed(self, mode_index):
        self.palette_mode_index = mode_index

    def _refresh_previews(self):
        for idx, name in enumerate(PALETTE_MODES):
            colors = generate_song_colors(name, self.hue_index, True)
            self.preview_widgets[idx].set_colors(colors)
