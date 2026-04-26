"""
Timeline widgets module.

Contains reusable UI components for the timeline system including tooltips,
dialogs, custom views, and coordinate utilities.
"""

from .tooltip import CustomTooltip
from .content_view import ContentView
from .midi_event_edit import MidiEventEditDialog
from .resize_handle import TrackResizeHandle

__all__ = [
    'CustomTooltip',
    'ContentView',
    'MidiEventEditDialog',
    'TrackResizeHandle'
]