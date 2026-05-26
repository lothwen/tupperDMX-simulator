# ABOUTME: Edit dialog for the special START MIDI event (note 40).
# ABOUTME: Encodes a hue choice (12 fixed hues + random) and palette mode
# ABOUTME: into the event's velocity as: hue_index * 6 + palette_mode_index.

import colorsys

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QLabel, QPushButton, QButtonGroup, QFrame,
                             QDialogButtonBox, QWidget)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPainter, QColor


# Mirrors software_controller.main constants so the preview matches what the
# controller will actually render at runtime.
RAINBOW_HUE_12 = [0.0, 0.083, 0.166, 0.25, 0.333, 0.416,
                  0.5, 0.583, 0.666, 0.75, 0.833, 0.916]
DEFAULT_HSL_S = 1.0
DEFAULT_HSL_L = 0.5
COLOR32_WHITE = (255, 255, 230)

PALETTE_MODES = ['COMPLEMENTARY', 'ANALOGOUS', 'TRIADIC', 'MONOCHROME']

RANDOM_HUE_INDEX = 12


def _hsl_to_rgb(h, s, l):
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return round(r * 255), round(g * 255), round(b * 255)


def _generate_palette(mode, hue_index):
    """Preview-equivalent of DMXController.generate_song_colors().

    Uses midpoint values where the controller draws random offsets, so the
    preview is deterministic. Returns None for the random hue (index 12).
    """
    if hue_index >= RANDOM_HUE_INDEX:
        return None
    hue = RAINBOW_HUE_12[hue_index % 12] * 360

    if mode == 'COMPLEMENTARY':
        # Controller uses random.randint(15, 25)/100 for the 2nd entry;
        # use the midpoint (0.20) here.
        return [
            _hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            _hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, min(1.0, DEFAULT_HSL_L + 0.20)),
            _hsl_to_rgb(((hue + 180) % 360) / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
        ]
    if mode == 'ANALOGOUS':
        return [
            _hsl_to_rgb(((hue - 30) % 360) / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            _hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            _hsl_to_rgb(((hue + 30) % 360) / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
        ]
    if mode == 'TRIADIC':
        return [
            _hsl_to_rgb(((hue - 120) % 360) / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            _hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            _hsl_to_rgb(((hue + 120) % 360) / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
        ]
    if mode == 'MONOCHROME':
        return [
            COLOR32_WHITE,
            _hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
        ]
    return []


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


class StartEventEditDialog(QDialog):
    """Edit dialog for the START song-part MIDI event.

    Encodes the user's choices into the event's velocity as:
        velocity = hue_index * 6 + palette_mode_index
    where hue_index 0-11 maps to RAINBOW_HUE_12 and 12 means random.
    """

    SWATCH_SIZE = 44

    def __init__(self, event_data, parent=None):
        super().__init__(parent)
        self.event_data = event_data
        self.setWindowTitle("Edit START Event")
        self.setModal(True)

        velocity = int(event_data.get('velocity', 0))
        self.hue_index = velocity // 6
        self.palette_mode_index = velocity % 6
        if self.hue_index > RANDOM_HUE_INDEX:
            self.hue_index = 0
        if self.palette_mode_index >= len(PALETTE_MODES):
            self.palette_mode_index = 0

        self.color_buttons = []
        self.mode_buttons = []
        self.preview_widgets = []

        self._setup_ui()
        self._apply_initial_selection()
        self._refresh_previews()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        color_header = QLabel("Base Color")
        color_header.setStyleSheet("font-weight: bold;")
        layout.addWidget(color_header)

        self.color_group = QButtonGroup(self)
        self.color_group.setExclusive(True)

        grid_row = QHBoxLayout()
        grid_row.addStretch()
        grid = QGridLayout()
        grid.setSpacing(6)

        for i in range(12):
            r, g, b = _hsl_to_rgb(RAINBOW_HUE_12[i], DEFAULT_HSL_S, DEFAULT_HSL_L)
            btn = self._make_swatch(f"rgb({r}, {g}, {b})")
            row, col = divmod(i, 4)
            grid.addWidget(btn, row, col)
            self.color_group.addButton(btn, i)
            self.color_buttons.append(btn)

        self.random_button = QPushButton("?")
        self.random_button.setCheckable(True)
        self.random_button.setFixedSize(self.SWATCH_SIZE, self.SWATCH_SIZE)
        self.random_button.setStyleSheet(
            "QPushButton {"
            "  background-color: #2a2a2a;"
            "  color: #dddddd;"
            "  border: 2px solid #555;"
            "  border-radius: 4px;"
            "  font-size: 22px;"
            "  font-weight: bold;"
            "}"
            "QPushButton:checked {"
            "  border: 3px solid white;"
            "}"
        )
        self.color_group.addButton(self.random_button, RANDOM_HUE_INDEX)

        random_box = QVBoxLayout()
        random_box.setSpacing(2)
        random_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        random_box.addWidget(self.random_button, alignment=Qt.AlignmentFlag.AlignCenter)
        random_label = QLabel("Random")
        random_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        random_label.setStyleSheet("color: #888; font-size: 10px;")
        random_box.addWidget(random_label)

        random_wrap = QWidget()
        random_wrap.setLayout(random_box)
        grid.addWidget(random_wrap, 0, 4, 3, 1, Qt.AlignmentFlag.AlignCenter)

        grid_row.addLayout(grid)
        grid_row.addStretch()
        layout.addLayout(grid_row)

        mode_header = QLabel("Palette Mode")
        mode_header.setStyleSheet("font-weight: bold; margin-top: 8px;")
        layout.addWidget(mode_header)

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)

        for idx, name in enumerate(PALETTE_MODES):
            row = QHBoxLayout()
            mode_btn = QPushButton(name)
            mode_btn.setCheckable(True)
            mode_btn.setFixedWidth(150)
            mode_btn.setStyleSheet(
                "QPushButton {"
                "  padding: 8px;"
                "  border: 2px solid #555;"
                "  border-radius: 4px;"
                "}"
                "QPushButton:checked {"
                "  border: 3px solid white;"
                "  background-color: #444;"
                "}"
            )
            self.mode_group.addButton(mode_btn, idx)
            self.mode_buttons.append(mode_btn)
            row.addWidget(mode_btn)

            preview = _PalettePreview()
            self.preview_widgets.append(preview)
            row.addWidget(preview, 1)
            layout.addLayout(row)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self.color_group.idClicked.connect(self._on_color_changed)
        self.mode_group.idClicked.connect(self._on_mode_changed)

    def _make_swatch(self, css_color):
        btn = QPushButton()
        btn.setCheckable(True)
        btn.setFixedSize(self.SWATCH_SIZE, self.SWATCH_SIZE)
        btn.setStyleSheet(
            "QPushButton {"
            f"  background-color: {css_color};"
            "  border: 2px solid #555;"
            "  border-radius: 4px;"
            "}"
            "QPushButton:checked {"
            "  border: 3px solid white;"
            "}"
        )
        return btn

    def _apply_initial_selection(self):
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
            colors = _generate_palette(name, self.hue_index)
            self.preview_widgets[idx].set_colors(colors)

    def get_values(self):
        velocity = self.hue_index * 6 + self.palette_mode_index
        return {
            'note': self.event_data.get('note'),
            'time': self.event_data.get('time'),
            'velocity': velocity,
        }
