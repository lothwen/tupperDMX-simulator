# ABOUTME: A two-pane splitter that opens at a RATIO of its own width instead
# ABOUTME: of at a fixed pixel size, and stays draggable afterwards.
"""Proportional splitting.

Qt has no "30% wide" for a splitter: `setSizes` takes pixels, and a widget that
has not been laid out yet has none to take a share of. So the ratio is applied
on the first resize that hands the splitter a real width, and never again —
re-applying it would undo every drag the user had made.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QSplitter


class RatioSplitter(QSplitter):
    """Two panes side by side, opening at `ratio` of the width and draggable.

    `ratio` is the share the FIRST pane gets. It is a starting point rather
    than a promise: both panes keep their own minimum width, so a narrow
    window gives the minimums instead.
    """

    def __init__(self, first, second, ratio: float = 0.3, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setObjectName("RatioSplitter")
        self._ratio = float(ratio)
        self._applied = False
        self.addWidget(first)
        self.addWidget(second)
        # Neither pane may be dragged out of existence: a list collapsed to
        # nothing reads as a bug, and there is no affordance to bring it back.
        self.setChildrenCollapsible(False)
        # Width gained from a wider window is split in the same proportion, so
        # the panes keep looking the way they were left rather than the second
        # one swallowing everything.
        self.setStretchFactor(0, max(1, round(self._ratio * 100)))
        self.setStretchFactor(1, max(1, round((1.0 - self._ratio) * 100)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._applied and self.width() > 0:
            self._applied = True
            first = int(self.width() * self._ratio)
            self.setSizes([first, self.width() - first])
