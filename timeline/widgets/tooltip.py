"""
Custom tooltip widget for timeline components.

Provides persistent tooltips that can be positioned and managed independently
of the standard Qt tooltip system.
"""

from PyQt6.QtWidgets import QLabel
from PyQt6.QtCore import Qt


class CustomTooltip(QLabel):
    """Custom persistent tooltip, drawn as a CHILD of the window rather than
    as a `Qt.WindowType.ToolTip` one.

    A tooltip window is a native top-level window, and mapping one is a round
    trip to the compositor: measured at 111 ms, blocking the Qt thread, on the
    first hover over a clip. The section drag re-shows this on every mouse
    move, so that cost repeated for the length of a gesture. A child widget has
    no window of its own — showing it is a repaint.

    What it gives up is the ability to extend beyond the app window, and since
    it is clamped to stay inside it anyway, that never shows.

    It must not take mouse events: it appears directly under the cursor, and a
    child widget that accepted them would send the clip underneath a hover
    LEAVE, hiding the tooltip that hover just asked for — a flicker loop.
    """

    #: Gap kept between the tooltip and its parent's edge when clamping.
    MARGIN = 4
    #: How far above the anchor the tooltip sits when it has to flip. Both
    #: callers anchor it just BELOW the cursor, so clearing the cursor again
    #: means going back up by about that much.
    FLIP_GAP = 24

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TimelineTooltip")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def show_at(self, global_pos, text):
        """Show the tooltip at `global_pos` (both callers hold a global
        position), clamped so it stays wholly inside the parent."""
        parent = self.parentWidget()
        if parent is None:
            return
        if text != self.text():
            self.setText(text)
            self.adjustSize()

        pos = parent.mapFromGlobal(global_pos)

        # Below the cursor by preference; above it when there is no room, the
        # way a tooltip is expected to behave. Clamping straight down instead
        # would drop it onto the clip it is describing — the timeline lives at
        # the bottom of the window, so that is the common case, not the corner
        # case.
        y = pos.y()
        if y + self.height() + self.MARGIN > parent.height():
            y = pos.y() - self.height() - self.FLIP_GAP
        y = max(self.MARGIN,
                min(y, parent.height() - self.height() - self.MARGIN))
        x = max(self.MARGIN,
                min(pos.x(), parent.width() - self.width() - self.MARGIN))
        self.move(x, y)

        # Raising is only needed when it comes back from hidden: during a drag
        # it stays up, and re-raising per mouse move is work for nothing.
        if not self.isVisible():
            self.show()
            self.raise_()

    def hide_tooltip(self):
        """Hide the tooltip."""
        self.hide()
