"""
Timeline widgets module.

Contains reusable UI components for the timeline system including tooltips,
dialogs, custom views, and coordinate utilities.
"""

from .tooltip import CustomTooltip
from .content_view import ContentView
from .start_event_edit import StartEventEditDialog
from .resize_handle import TrackResizeHandle

# clip_edit is NOT re-exported here: it pulls in the stage render engine
# (ui.fixture_console -> stage), and this package is imported by the tracks
# themselves. Import it directly from .widgets.clip_edit.

__all__ = [
    'CustomTooltip',
    'ContentView',
    'StartEventEditDialog',
    'TrackResizeHandle'
]