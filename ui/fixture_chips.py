# ABOUTME: The Qt chip widget shared by the fixture browser's tabs. The channel
# ABOUTME: family it displays comes from core/channel_families.py.
"""Chip rendering.

Only the Qt half lives here: what a channel *is* — its family label and colour —
is decided from the OFL capabilities in `core.channel_families`, which the
timeline reads too and which therefore has to stay Qt-free.
"""

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QLabel


def rgba(hex_color: str, alpha: int) -> str:
    """`hex_color` as a QSS rgba() string — QSS has no #RRGGBBAA literal."""
    color = QColor(hex_color)
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha})"


def chip(text: str, color: str) -> QLabel:
    """A small pill label in `color`."""
    label = QLabel(text)
    label.setObjectName("AddFxChip")
    label.setStyleSheet(
        f"color: {color};"
        f"border: 1px solid {rgba(color, 90)};"
        f"background-color: {rgba(color, 32)};")
    return label
