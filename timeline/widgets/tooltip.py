"""
Custom tooltip widget for timeline components.

Provides persistent tooltips that can be positioned and managed independently
of the standard Qt tooltip system.
"""

from PyQt6.QtWidgets import QLabel
from PyQt6.QtCore import Qt


class CustomTooltip(QLabel):
    """Custom persistent tooltip widget."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("TimelineTooltip")
        self.hide()
        
    def show_at(self, global_pos, text):
        """Show tooltip at global position with given text."""
        self.setText(text)
        self.adjustSize()
        self.move(global_pos)
        self.show()
        self.raise_()
        
    def hide_tooltip(self):
        """Hide the tooltip."""
        self.hide()