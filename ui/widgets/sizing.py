# ABOUTME: keeps a window's opening size inside the screen it opens on.
"""Screen-aware initial sizing.

A dialog that asks for 800px of height on a 1280x800 laptop opens with its
buttons under the taskbar, and on most window managers the user cannot drag it
back up. `fit_to_screen` is the preferred size clamped to what the screen
actually has, so the dialog opens smaller instead of opening unusable.
"""

from PyQt6.QtGui import QGuiApplication

# Room for the title bar and a shadow, so a clamped window is still grabbable.
_CHROME = 48


def fit_to_screen(widget, width: int, height: int) -> None:
    """Resize `widget` to `width` x `height`, or to the screen if that is
    smaller."""
    screen = widget.screen() or QGuiApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        width = min(width, available.width() - _CHROME)
        height = min(height, available.height() - _CHROME)
    widget.resize(width, height)
