# ABOUTME: The Qt chip widget, plus the one FlowLayout every wrapping row in the
# ABOUTME: app uses. The channel family a chip displays comes from
# ABOUTME: core/channel_families.py.
"""Chip rendering, and the flow layout chips need.

Only the Qt half lives here: what a channel *is* — its family label and colour —
is decided from the OFL capabilities in `core.channel_families`, which the
timeline reads too and which therefore has to stay Qt-free.
"""

from typing import List

from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QLabel, QLayout, QWidget


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


class FlowLayout(QLayout):
    """Left-to-right layout wrapping onto a new line when it runs out of width.

    The standard Qt flow-layout recipe. Chips are text-sized and so
    unpredictably wide that no grid fits them; the clip library's pill bar and
    card grid are fixed-size but want the same reflow-with-the-panel behaviour,
    so both read this one. Margins are always zero — set them on the parent.
    """

    def __init__(self, parent=None, spacing: int = 5):
        super().__init__(parent)
        self._items: List = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect: QRect, apply: bool) -> int:
        x, y, line_height = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x > rect.x() and x + hint.width() > rect.right():
                x, y = rect.x(), y + line_height + self.spacing()
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.spacing()
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


def flow_holder(spacing: int = 5) -> QWidget:
    """A widget carrying a `FlowLayout`, sized by the height that layout needs.

    Without the height-for-width policy the widget reports one row's height
    whatever it wraps to, and a second line is drawn over whatever sits below.
    """
    holder = QWidget()
    FlowLayout(holder, spacing)
    policy = holder.sizePolicy()
    policy.setHeightForWidth(True)
    holder.setSizePolicy(policy)
    return holder
