"""
Timeline manager for TupperDMX Simulator.

Coordinates multiple timeline tracks and provides the main timeline interface.
"""

import os
import mido
import copy
from typing import Optional, List, Dict, Any

from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QGraphicsView, QGraphicsScene,
                             QSlider, QPushButton, QSizePolicy, QGraphicsLineItem,
                             QGraphicsItem, QDialog, QMessageBox, QGraphicsProxyWidget)
from PyQt6.QtGui import QColor, QPen
from PyQt6.QtCore import Qt, pyqtSignal, QTimer

from .widgets import ContentView, MidiEventEditDialog, TrackResizeHandle
from .tracks import WaveformTrack, MidiEventsTrack, PianoRollTrack, RulerTrack
from tupperlogger import get_logger


class Timeline(QWidget):
    """Main timeline widget coordinating multiple tracks."""
    
    # Signals
    seek_requested = pyqtSignal(float)
    bpm_detected = pyqtSignal(int)
    waveform_offset_changed = pyqtSignal(float)
    metronome_volume_changed = pyqtSignal(int)
    metronome_mute_toggled = pyqtSignal(bool)
    audio_volume_changed = pyqtSignal(int)
    audio_mute_toggled = pyqtSignal(bool)
    midi_data_changed = pyqtSignal()  # Emitted when MIDI data is modified
    piano_roll_visibility_changed = pyqtSignal(bool)  # Emitted when piano roll is hidden/shown
    track_heights_changed = pyqtSignal(int, int)  # waveform_height, midi_events_height (on commit)

    # Constants
    KEY_HEIGHT = 20
    PIANO_WIDTH = 100
    RULER_TRACK_HEIGHT = 30
    DEFAULT_WAVEFORM_TRACK_HEIGHT = 100
    DEFAULT_MIDI_EVENTS_TRACK_HEIGHT = 50
    MIN_TRACK_HEIGHT = 30
    TRACK_SPACING = 5
    PIANO_ROLL_TOGGLE_HEIGHT = 26  # Header strip hosting the piano-roll show/hide button
    SECONDS_TO_DISPLAY = 240
    PIXELS_PER_SECOND = 20

    # Preferred widget heights (px) for the timeline container, by piano-roll visibility
    HEIGHT_WITH_PIANO_ROLL = 300
    HEIGHT_WITHOUT_PIANO_ROLL = 230

    def __init__(self, midi_manager=None, audio_file=None, initial_metronome_volume=0.5,
                 initial_audio_volume=0.5, initial_piano_roll_visible=False,
                 initial_waveform_track_height=None, initial_midi_events_track_height=None,
                 parent=None):
        super().__init__(parent)
        self.audio_file = audio_file
        self.midi_manager = midi_manager

        self.min_note = 21
        self.max_note = 108
        self.bpm = 120
        self.piano_roll_visible = bool(initial_piano_roll_visible)

        # Resizable track heights (instance attributes so they can be changed at runtime)
        self.waveform_track_height = max(
            self.MIN_TRACK_HEIGHT,
            int(initial_waveform_track_height) if initial_waveform_track_height is not None
            else self.DEFAULT_WAVEFORM_TRACK_HEIGHT
        )
        self.midi_events_track_height = max(
            self.MIN_TRACK_HEIGHT,
            int(initial_midi_events_track_height) if initial_midi_events_track_height is not None
            else self.DEFAULT_MIDI_EVENTS_TRACK_HEIGHT
        )

        # Resize handle items (created in _init_tracks)
        self.waveform_resize_handle = None
        self.midi_events_resize_handle = None
        
        # Legacy compatibility - these will be populated by tracks
        self.note_items = []
        self.midi_event_items = []
        
        # Cache for current UI data (refreshed from MidiManager)
        self._cached_events = []
        self._cached_notes = []
        
        # Time grid items (managed centrally)
        self.time_grid_items = []
        
        # Logger for debug messages
        self.logger = get_logger("timeline.manager")

        # Create control widgets
        self._create_control_widgets(initial_metronome_volume, initial_audio_volume)
        
        # Initialize UI
        self.init_ui()
        self.setup_scenes()
        
        # Initialize tracks
        self._init_tracks()
        
    def _create_control_widgets(self, initial_metronome_volume, initial_audio_volume):
        """Create volume control widgets."""
        # Metronome controls
        self.metronome_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.metronome_volume_slider.setRange(0, 100)
        self.metronome_volume_slider.setValue(int(initial_metronome_volume * 100))
        self.metronome_volume_slider.setFixedWidth(60)
        self.metronome_volume_slider.valueChanged.connect(self.metronome_volume_changed.emit)

        self.metronome_mute_button = QPushButton("M")
        self.metronome_mute_button.setCheckable(True)
        self.metronome_mute_button.toggled.connect(self.on_mute_toggled)
        self.metronome_mute_button.setFixedSize(20, 20)
        self.metronome_mute_button.setObjectName("MetronomeMuteButton")

        self.metronome_controls_container = QWidget()
        self.metronome_controls_container.setFixedWidth(90)
        self.metronome_controls_container.setObjectName("MetronomeControlsContainer")
        controls_layout = QHBoxLayout(self.metronome_controls_container)
        controls_layout.setContentsMargins(2, 2, 2, 2)
        controls_layout.setSpacing(2)
        controls_layout.addWidget(self.metronome_mute_button)
        controls_layout.addWidget(self.metronome_volume_slider)

        # Audio controls
        self.audio_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.audio_volume_slider.setRange(0, 100)
        self.audio_volume_slider.setValue(int(initial_audio_volume * 100))
        self.audio_volume_slider.setFixedWidth(60)
        self.audio_volume_slider.valueChanged.connect(self.audio_volume_changed.emit)

        self.audio_mute_button = QPushButton("M")
        self.audio_mute_button.setCheckable(True)
        self.audio_mute_button.toggled.connect(self.on_audio_mute_toggled)
        self.audio_mute_button.setFixedSize(20, 20)
        self.audio_mute_button.setObjectName("AudioMuteButton")

        self.audio_controls_container = QWidget()
        self.audio_controls_container.setFixedWidth(90)
        self.audio_controls_container.setObjectName("AudioControlsContainer")
        audio_controls_layout = QHBoxLayout(self.audio_controls_container)
        audio_controls_layout.setContentsMargins(2, 2, 2, 2)
        audio_controls_layout.setSpacing(2)
        audio_controls_layout.addWidget(self.audio_mute_button)
        audio_controls_layout.addWidget(self.audio_volume_slider)

    def init_ui(self):
        """Initialize the UI layout."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header_view = QGraphicsView(self)
        self.header_scene = QGraphicsScene(self)
        self.header_view.setScene(self.header_scene)
        self.header_view.setFixedWidth(self.PIANO_WIDTH)
        self.header_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.header_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.header_view.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding) 
        
        self.content_view = ContentView(self, self)
        self.content_scene = QGraphicsScene(self)
        self.content_view.setScene(self.content_scene)

        layout.addWidget(self.header_view)
        layout.addWidget(self.content_view)

        # Sync scrollbars
        self.content_view.verticalScrollBar().valueChanged.connect(self.header_view.verticalScrollBar().setValue)
        self.header_view.verticalScrollBar().valueChanged.connect(self.content_view.verticalScrollBar().setValue)

    def setup_scenes(self):
        """Setup the graphics scenes."""
        self.content_width = self.PIXELS_PER_SECOND * self.SECONDS_TO_DISPLAY

        self.header_scene.setBackgroundBrush(QColor(30, 30, 30))
        self.content_scene.setBackgroundBrush(QColor(30, 30, 30))

        self._recompute_scene_height()
        self.header_scene.setSceneRect(0, 0, self.PIANO_WIDTH, self.scene_height)
        self.content_scene.setSceneRect(0, 0, self.content_width, self.scene_height)

        self.draw_playhead()
        self.draw_time_grid()

    def _waveform_y(self) -> int:
        """Y offset of the waveform track."""
        return self.RULER_TRACK_HEIGHT + self.TRACK_SPACING

    def _midi_events_y(self) -> int:
        """Y offset of the LedBar MIDI events track."""
        return self._waveform_y() + self.waveform_track_height + self.TRACK_SPACING

    def _midi_events_blinder_y(self) -> int:
        """Y offset of the Blinder MIDI events track (shares the same height)."""
        return self._midi_events_y() + self.midi_events_track_height + self.TRACK_SPACING

    def _piano_roll_toggle_y(self) -> int:
        """Y offset of the piano-roll toggle strip in the header scene."""
        return self._midi_events_blinder_y() + self.midi_events_track_height + self.TRACK_SPACING

    def _piano_roll_content_y(self) -> int:
        """Y offset where piano-roll keys/notes start (below the toggle strip)."""
        return self._piano_roll_toggle_y() + self.PIANO_ROLL_TOGGLE_HEIGHT

    def _recompute_scene_height(self):
        """Compute scene height based on current piano-roll visibility."""
        base = self._piano_roll_toggle_y() + self.PIANO_ROLL_TOGGLE_HEIGHT
        if self.piano_roll_visible:
            note_range = self.max_note - self.min_note
            base += self.KEY_HEIGHT * note_range
        self.scene_height = base

    def _init_tracks(self):
        """Initialize all timeline tracks."""
        self.ruler_track = RulerTrack(self, self.content_scene, self.header_scene)
        self.waveform_track = WaveformTrack(self, self.content_scene, self.header_scene)
        self.midi_events_track = MidiEventsTrack(self, self.content_scene, self.header_scene,
                                                 fixture_filter="ledbar")
        self.midi_events_blinder_track = MidiEventsTrack(self, self.content_scene, self.header_scene,
                                                         fixture_filter="blinder")
        self.piano_roll_track = PianoRollTrack(self, self.content_scene, self.header_scene)

        # Draw initial track layout
        self.ruler_track.draw_header()
        self.ruler_track.draw_ruler()

        self.waveform_track.draw_header(self._waveform_y())
        self.midi_events_track.draw_header(self._midi_events_y())
        self.midi_events_blinder_track.draw_header(self._midi_events_blinder_y())

        # Resize handles for the two resizable tracks
        self._create_resize_handles()

        # Piano-roll show/hide toggle button (always present)
        self._create_piano_roll_toggle()

        # Piano keys/grid only drawn when visible
        if self.piano_roll_visible:
            piano_y = self._piano_roll_content_y()
            self.piano_roll_track.draw_piano_keys(piano_y)
            self.piano_roll_track.draw_piano_roll_grid(piano_y)

        # Draw initial waveform if audio file provided
        if self.audio_file:
            self.waveform_track.draw_waveform(self.audio_file, self._waveform_y())

    def _create_resize_handles(self):
        """Create draggable resize handles at the bottom of the resizable tracks."""
        # Waveform handle
        self.waveform_resize_handle = TrackResizeHandle(
            self.PIANO_WIDTH, self.MIN_TRACK_HEIGHT,
            lambda: self.waveform_track_height
        )
        self.waveform_resize_handle.height_preview.connect(self._on_waveform_height_preview)
        self.waveform_resize_handle.height_committed.connect(self._on_waveform_height_committed)
        self.header_scene.addItem(self.waveform_resize_handle)

        # MIDI events handle
        self.midi_events_resize_handle = TrackResizeHandle(
            self.PIANO_WIDTH, self.MIN_TRACK_HEIGHT,
            lambda: self.midi_events_track_height
        )
        self.midi_events_resize_handle.height_preview.connect(self._on_midi_events_height_preview)
        self.midi_events_resize_handle.height_committed.connect(self._on_midi_events_height_committed)
        self.header_scene.addItem(self.midi_events_resize_handle)

        self._reposition_resize_handles()

    def _reposition_resize_handles(self):
        """Place each resize handle at the bottom border of its track header."""
        thickness = TrackResizeHandle.HANDLE_THICKNESS
        if self.waveform_resize_handle is not None:
            self.waveform_resize_handle.setPos(
                0, self._waveform_y() + self.waveform_track_height - thickness
            )
        if self.midi_events_resize_handle is not None:
            self.midi_events_resize_handle.setPos(
                0, self._midi_events_y() + self.midi_events_track_height - thickness
            )

    def _on_waveform_height_preview(self, new_height: int):
        """Live update during waveform resize drag (no expensive waveform redraw)."""
        if new_height == self.waveform_track_height:
            return
        self.waveform_track_height = max(self.MIN_TRACK_HEIGHT, int(new_height))
        self._reflow_layout(redraw_waveform=False)

    def _on_waveform_height_committed(self, new_height: int):
        """Drag finished — apply final height and trigger waveform redraw."""
        self.waveform_track_height = max(self.MIN_TRACK_HEIGHT, int(new_height))
        self._reflow_layout(redraw_waveform=True)
        self.logger.info(f"Waveform track height set to {self.waveform_track_height}px")
        self.track_heights_changed.emit(self.waveform_track_height, self.midi_events_track_height)

    def _on_midi_events_height_preview(self, new_height: int):
        """Live update during MIDI events resize drag."""
        if new_height == self.midi_events_track_height:
            return
        self.midi_events_track_height = max(self.MIN_TRACK_HEIGHT, int(new_height))
        self._reflow_layout(redraw_waveform=False)

    def _on_midi_events_height_committed(self, new_height: int):
        """Drag finished for MIDI events — persist and reflow."""
        self.midi_events_track_height = max(self.MIN_TRACK_HEIGHT, int(new_height))
        self._reflow_layout(redraw_waveform=False)
        self.logger.info(f"MIDI events track height set to {self.midi_events_track_height}px")
        self.track_heights_changed.emit(self.waveform_track_height, self.midi_events_track_height)

    def _reflow_layout(self, redraw_waveform: bool = False):
        """Reposition headers, content, and overlays after a height/visibility change."""
        # Recompute scene height and update both scene rects
        self._recompute_scene_height()
        self.header_scene.setSceneRect(0, 0, self.PIANO_WIDTH, self.scene_height)
        self.content_scene.setSceneRect(0, 0, self.content_width, self.scene_height)

        # Headers
        self.waveform_track.draw_header(self._waveform_y())
        self.midi_events_track.draw_header(self._midi_events_y())
        self.midi_events_blinder_track.draw_header(self._midi_events_blinder_y())

        # Piano-roll toggle proxy
        if hasattr(self, 'piano_roll_toggle_proxy') and self.piano_roll_toggle_proxy is not None:
            self.piano_roll_toggle_proxy.setPos(1, self._piano_roll_toggle_y())

        # Piano roll content (only redraw if currently visible)
        if self.piano_roll_visible:
            piano_y = self._piano_roll_content_y()
            self.piano_roll_track.draw_piano_keys(piano_y)
            self.piano_roll_track.draw_piano_roll_grid(piano_y)
            if self._cached_notes:
                self.piano_roll_track.redraw_notes(self._cached_notes, piano_y)

        # MIDI events content (cheap to redraw — keeps box heights in sync)
        if self._cached_events:
            self.midi_events_track.redraw_events(self._cached_events, self._midi_events_y())
            self.midi_events_blinder_track.redraw_events(self._cached_events, self._midi_events_blinder_y())

        # Waveform — only do the expensive pixmap rebuild on commit
        if redraw_waveform and self.audio_file:
            self.waveform_track.draw_waveform(self.audio_file, self._waveform_y())
        elif self.waveform_track.waveform_item is not None:
            # Keep existing pixmap visible at the new track top while dragging
            self.waveform_track.waveform_item.setYPos(self._waveform_y())

        # Playhead height + grid lines span the full scene
        if hasattr(self, 'playhead') and self.playhead is not None:
            self.playhead.setLine(0, 0, 0, self.scene_height)
        self.draw_time_grid()

        # Move resize handles to their new positions
        self._reposition_resize_handles()

    def _create_piano_roll_toggle(self):
        """Create the persistent show/hide button for the piano roll track."""
        self.piano_roll_toggle_button = QPushButton()
        self.piano_roll_toggle_button.setObjectName("PianoRollToggleButton")
        self.piano_roll_toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.piano_roll_toggle_button.setFlat(True)
        self.piano_roll_toggle_button.setFixedSize(self.PIANO_WIDTH - 2,
                                                   self.PIANO_ROLL_TOGGLE_HEIGHT - 2)
        self.piano_roll_toggle_button.clicked.connect(self._on_piano_roll_toggle_clicked)
        self._refresh_piano_roll_toggle_label()

        self.piano_roll_toggle_proxy = QGraphicsProxyWidget()
        self.piano_roll_toggle_proxy.setWidget(self.piano_roll_toggle_button)
        self.piano_roll_toggle_proxy.setPos(1, self._piano_roll_toggle_y())
        self.piano_roll_toggle_proxy.setZValue(5)
        self.header_scene.addItem(self.piano_roll_toggle_proxy)

    def _refresh_piano_roll_toggle_label(self):
        """Update toggle button text/tooltip to reflect current visibility."""
        if self.piano_roll_visible:
            self.piano_roll_toggle_button.setText("▼  Piano Roll")
            self.piano_roll_toggle_button.setToolTip("Hide piano roll track")
        else:
            self.piano_roll_toggle_button.setText("▶  Piano Roll")
            self.piano_roll_toggle_button.setToolTip("Show piano roll track")

    def _on_piano_roll_toggle_clicked(self):
        """Handle clicks on the piano-roll show/hide toggle."""
        self.set_piano_roll_visible(not self.piano_roll_visible)

    def set_piano_roll_visible(self, visible: bool):
        """Show or hide the piano roll track and reflow the timeline."""
        visible = bool(visible)
        if visible == self.piano_roll_visible:
            return

        self.piano_roll_visible = visible
        self._refresh_piano_roll_toggle_label()

        if visible:
            piano_y = self._piano_roll_content_y()
            self.piano_roll_track.draw_piano_keys(piano_y)
            self.piano_roll_track.draw_piano_roll_grid(piano_y)
            if self._cached_notes:
                self.piano_roll_track.redraw_notes(self._cached_notes, piano_y)
        else:
            self.piano_roll_track.clear_items()
            self.note_items.clear()

        self._recompute_scene_height()
        self.header_scene.setSceneRect(0, 0, self.PIANO_WIDTH, self.scene_height)
        self.content_scene.setSceneRect(0, 0, self.content_width, self.scene_height)

        # Playhead and grid lines span the full scene height; redraw them
        if hasattr(self, 'playhead') and self.playhead is not None:
            self.playhead.setLine(0, 0, 0, self.scene_height)
        self.draw_time_grid()

        # Adjust the timeline widget height so we don't leave dead space
        self.setFixedHeight(self.HEIGHT_WITH_PIANO_ROLL if visible
                            else self.HEIGHT_WITHOUT_PIANO_ROLL)

        self.logger.info(f"Piano roll {'shown' if visible else 'hidden'}")
        self.piano_roll_visibility_changed.emit(visible)

    def draw_playhead(self):
        """Draw the playhead line."""
        pen = QPen(QColor(255, 0, 0, 200), 2)
        self.playhead = QGraphicsLineItem(0, 0, 0, self.scene_height)
        self.playhead.setPen(pen)
        self.playhead.setZValue(10)
        self.content_scene.addItem(self.playhead)

    def update_playhead(self, time_seconds):
        """Update playhead position."""
        x_pos = time_seconds * self.PIXELS_PER_SECOND
        self.playhead.setX(x_pos)

    def draw_time_grid(self):
        """Draw the time grid lines."""
        for item in self.time_grid_items: 
            self.content_scene.removeItem(item)
        self.time_grid_items.clear()

        seconds_per_beat = 60.0 / self.bpm
        total_beats = int(self.content_width / (seconds_per_beat * self.PIXELS_PER_SECOND))
        
        for beat in range(total_beats):
            x_pos = beat * seconds_per_beat * self.PIXELS_PER_SECOND
            line = QGraphicsLineItem(x_pos, self.RULER_TRACK_HEIGHT, x_pos, self.scene_height)
            line.setPen(QPen(QColor(80, 80, 80), 1.5) if beat % 4 == 0 else QPen(QColor(60, 60, 60), 0.8))
            line.setZValue(-1)  # Keep grid lines in background
            self.content_scene.addItem(line)
            self.time_grid_items.append(line)

    def refresh_from_manager(self) -> None:
        """Refresh timeline display from MidiManager data."""
        if not self.midi_manager:
            return
            
        # Get current data from MidiManager
        self._cached_events = self.midi_manager.get_events_for_ui()
        self._cached_notes = self.midi_manager.get_notes_for_ui()
        
        # Redraw tracks with cached data
        midi_events_y = self._midi_events_y()
        piano_roll_y = self._piano_roll_content_y()

        self.midi_events_track.redraw_events(self._cached_events, midi_events_y)
        self.midi_events_blinder_track.redraw_events(self._cached_events, self._midi_events_blinder_y())
        if self.piano_roll_visible:
            self.piano_roll_track.redraw_notes(self._cached_notes, piano_roll_y)
        else:
            # Keep tooltip list consistent with what's drawn
            self.note_items.clear()
        
        # Detect BPM from first event if available
        if self._cached_events:
            # Try to get BPM from MIDI data
            duration = self.midi_manager.get_duration()
            if duration > 0:
                self._detect_bpm_from_data()

    def _detect_bpm_from_data(self) -> None:
        """Detect BPM from MIDI data structure."""
        if not self.midi_manager or not self.midi_manager.midi_data:
            return
            
        # Look for tempo meta messages
        for msg in self.midi_manager.midi_data:
            if msg.is_meta and msg.type == 'set_tempo':
                tempo = mido.tempo2bpm(msg.tempo)
                self.bpm_detected.emit(int(tempo))
                return
        
        # Look for velocity-encoded BPM (specific to this application)
        for msg in self.midi_manager.midi_data:
            if not msg.is_meta and msg.type == 'note_on' and msg.note == 40:
                tempo = msg.velocity * 2
                self.bpm_detected.emit(int(tempo))
                return

    def set_bpm(self, bpm):
        """Update BPM and redraw ruler and time grid to match new tempo."""
        if self.bpm != bpm:
            self.bpm = bpm
            self.ruler_track.redraw()
            self.draw_time_grid()
            self.logger.info(f"Timeline BPM updated to {bpm}")

    def update_ruler_labels(self):
        """Update ruler labels based on zoom level."""
        self.ruler_track.update_ruler_labels()

    def update_midi_text_truncation(self):
        """Update text truncation for all MIDI event labels based on current zoom level."""
        if not self._cached_events:
            return

        zoom_factor = self.content_view.transform().m11()
        font_metrics = self.content_view.fontMetrics()
        sorted_events = sorted(self._cached_events, key=lambda x: x['time'])
        song_length = self.midi_manager.get_duration() if self.midi_manager else 240
        extended_end = song_length * 1.05

        from PyQt6.QtWidgets import QGraphicsTextItem
        # Each track has its own filtered slice — so end-times must be
        # recomputed per track or the blinder text would inherit gaps from
        # ledbar neighbors (and vice-versa).
        for track in (self.midi_events_track, self.midi_events_blinder_track):
            if not track.items:
                continue
            track_events = [e for e in sorted_events if track.matches_filter(e)]
            id_to_end = {}
            for i, e in enumerate(track_events):
                end = track_events[i + 1]['time'] if i + 1 < len(track_events) else extended_end
                id_to_end[e['id']] = end

            for item in track.items:
                if not isinstance(item, QGraphicsTextItem):
                    continue
                full_text = item.data(0)
                if not full_text:
                    continue
                event_id = item.data(1)
                if event_id not in id_to_end:
                    continue
                event = next((e for e in track_events if e['id'] == event_id), None)
                if event is None:
                    continue
                duration = max(0.01, id_to_end[event_id] - event['time'])
                width = max(duration * self.PIXELS_PER_SECOND, 20)
                available_width = (width - 10) * zoom_factor
                item.setPlainText(self.get_truncated_text(full_text, available_width, font_metrics))

    def get_truncated_text(self, text, available_width, font_metrics):
        """Return text truncated to fit within available width, with ellipsis if needed."""
        if available_width <= 0:
            return ""
            
        # If the text fits entirely, return it as-is
        text_width = font_metrics.horizontalAdvance(text)
        if text_width <= available_width:
            return text
            
        # Calculate available width for ellipsis (3 dots)
        ellipsis = "..."
        ellipsis_width = font_metrics.horizontalAdvance(ellipsis)
        available_for_text = available_width - ellipsis_width
        
        if available_for_text <= 0:
            return ellipsis
            
        # Binary search to find the maximum characters that fit
        left, right = 0, len(text)
        best_length = 0
        
        while left <= right:
            mid = (left + right) // 2
            substring = text[:mid]
            substring_width = font_metrics.horizontalAdvance(substring)
            
            if substring_width <= available_for_text:
                best_length = mid
                left = mid + 1
            else:
                right = mid - 1
                
        return text[:best_length] + ellipsis if best_length > 0 else ellipsis

    # Event handlers
    def on_mute_toggled(self, checked):
        self.metronome_mute_toggled.emit(checked)

    def on_audio_mute_toggled(self, checked):
        self.audio_mute_toggled.emit(checked)

    # MIDI event management methods (event-id based)
    def _find_cached_event(self, event_id: str):
        """Look up a cached event by id."""
        for e in self._cached_events:
            if e.get('id') == event_id:
                return e
        return None

    def on_midi_event_moved(self, event_id: str, new_time: float):
        """Whole-clip MOVE drag committed."""
        if not self.midi_manager or not event_id:
            return
        if self.midi_manager.move_event(event_id, new_time):
            self.refresh_from_manager()

    def on_midi_event_left_edge(self, event_id: str, new_time: float):
        """Left-edge resize drag committed."""
        if not self.midi_manager or not event_id:
            return
        if self.midi_manager.set_event_left_edge(event_id, new_time):
            self.refresh_from_manager()

    def on_midi_event_right_edge(self, event_id: str, new_right_time: float):
        """Right-edge resize drag committed."""
        if not self.midi_manager or not event_id:
            return
        if self.midi_manager.set_event_right_edge(event_id, new_right_time):
            self.refresh_from_manager()

    def on_midi_event_edit_by_id(self, event_id: str):
        """Edit dialog request for the event with `event_id`."""
        if not self.midi_manager or not event_id:
            return
        event_data = self._find_cached_event(event_id)
        if not event_data:
            return

        dialog = MidiEventEditDialog(event_data, self.midi_manager.midi_note_map)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_values = dialog.get_values()
            if self.midi_manager.edit_event(event_id, **new_values):
                self.refresh_from_manager()
                self.logger.info(
                    f"MIDI event edited: note={new_values['note']}, "
                    f"time={new_values['time']:.3f}s, velocity={new_values['velocity']}"
                )

    def on_midi_event_delete_by_id(self, event_id: str):
        """Delete request for the event with `event_id`."""
        if not self.midi_manager or not event_id:
            return
        event_data = self._find_cached_event(event_id)
        if not event_data:
            return

        info = event_data.get('info', None)
        if info and hasattr(info, 'part'):
            event_name = info.part
        else:
            event_name = f"Note {event_data['note']}"

        reply = QMessageBox.question(
            None,
            "Delete MIDI Event",
            f"Are you sure you want to delete the MIDI event '{event_name}' at {event_data['time']:.2f}s?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            if self.midi_manager.delete_event(event_id):
                self.refresh_from_manager()
                self.logger.info(
                    f"MIDI event deleted: note={event_data['note']}, time={event_data['time']:.3f}s"
                )

    def fixture_at_y(self, scene_y: float):
        """Return the fixture filter owning the given scene Y, or None.

        Used by clip-library drops to validate that a dropped note targets
        the matching fixture's track. Returns the same string stored in
        each MidiEventsTrack.fixture_filter (e.g. "ledbar", "blinder").
        """
        height = self.midi_events_track_height
        ledbar_y = self._midi_events_y()
        blinder_y = self._midi_events_blinder_y()
        if ledbar_y <= scene_y < ledbar_y + height:
            return self.midi_events_track.fixture_filter
        if blinder_y <= scene_y < blinder_y + height:
            return self.midi_events_blinder_track.fixture_filter
        return None

    def can_accept_clip_drop(self, note: int, scene_y: float) -> bool:
        """Return True if a clip carrying `note` may land at `scene_y`."""
        if not self.midi_manager:
            return False
        note_info = self.midi_manager.midi_note_map.get(str(note))
        if note_info is None:
            return False
        target_fixture = self.fixture_at_y(scene_y)
        if target_fixture is None:
            return False
        return target_fixture == note_info.fixture

    def handle_clip_drop(self, note: int, velocity: int, scene_x: float, scene_y: float) -> bool:
        """Validate and insert a dropped clip. Returns True on success."""
        if not self.can_accept_clip_drop(note, scene_y):
            return False
        time_seconds = max(0.0, scene_x / self.PIXELS_PER_SECOND)
        clamped_velocity = max(1, min(127, int(velocity)))
        event_id = self.midi_manager.add_event(note, time_seconds, clamped_velocity)
        if not event_id:
            return False
        self.refresh_from_manager()
        self.logger.info(
            f"Clip dropped: note={note} at {time_seconds:.3f}s "
            f"on {self.fixture_at_y(scene_y)} track"
        )
        return True

    def on_add_event_requested(self, time_seconds):
        """Handle request to add a new MIDI event at the specified time."""
        if not self.midi_manager:
            return
            
        # Create event data with default values
        new_event_data = {
            'time': time_seconds,
            'velocity': 127,
            'note': None  # Will be set by dialog
        }
        
        dialog = MidiEventEditDialog(new_event_data, self.midi_manager.midi_note_map, is_new_event=True)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Get the new values from dialog
            new_values = dialog.get_values()
            
            # Add via MidiManager
            event_id = self.midi_manager.add_event(
                new_values['note'],
                new_values['time'], 
                new_values['velocity']
            )
            
            if event_id:
                # Refresh the timeline display
                self.refresh_from_manager()
                
                # Get part name for logging
                note_info = self.midi_manager.midi_note_map.get(str(new_values['note']))
                part_name = getattr(note_info, 'part', f'Note {new_values["note"]}') if note_info else f'Note {new_values["note"]}'
                self.logger.info(f"MIDI event added: {part_name} at {new_values['time']:.3f}s, velocity={new_values['velocity']}")
            else:
                self.logger.error(f"Failed to add MIDI event: note {new_values['note']} not in note map")

    # Legacy compatibility methods
    def set_files(self, audio_file, midi_file=None):
        """Set audio file. MIDI files should be loaded via MidiManager."""
        self.audio_file = audio_file

        # Clear existing items
        self.note_items.clear()
        self.midi_event_items.clear()
        
        # Redraw waveform if audio file provided
        if audio_file:
            self.waveform_track.draw_waveform(audio_file, self._waveform_y())
        
        QTimer.singleShot(0, self.fit_to_width)
    
    def set_waveform_offset(self, offset_sec: float) -> None:
        """Set the waveform offset position."""
        if self.waveform_track.waveform_item:
            x_pos = offset_sec * self.PIXELS_PER_SECOND
            self.waveform_track.waveform_item.setX(x_pos)
            self.logger.debug(f"Waveform offset set to {offset_sec:.3f}s")

    def fit_to_width(self):
        """Fit timeline to view width."""
        view_width = self.content_view.viewport().width()
        scene_width = self.content_scene.width()
        
        if scene_width <= 0 or view_width <= 0: 
            return

        min_scale = view_width / scene_width
        self.content_view.resetTransform()
        self.content_view.scale(min_scale, 1)
        self.update_ruler_labels()
        self.update_midi_text_truncation()

    def get_modified_midi_data(self):
        """Get the modified MIDI data from MidiManager."""
        if self.midi_manager and self.midi_manager.midi_data:
            return self.midi_manager.midi_data
        return None

    def get_time_from_pos(self, x_pos):
        """Convert X position to time in seconds."""
        scene_x = self.content_view.mapToScene(x_pos, 0).x()
        return scene_x / self.PIXELS_PER_SECOND if self.PIXELS_PER_SECOND > 0 else 0

    def mousePressEvent(self, event):
        """Handle mouse press for seeking."""
        if event.button() == Qt.MouseButton.LeftButton:
            view_pos = self.content_view.mapFrom(self, event.pos())
            if self.content_view.rect().contains(view_pos):
                time_seconds = self.get_time_from_pos(view_pos.x())
                self.update_playhead(time_seconds)
                self.seek_requested.emit(time_seconds)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """Handle mouse move events - delegated to ContentView for piano roll tooltips."""
        super().mouseMoveEvent(event)

    def cleanup(self):
        """Cleanup all tracks when timeline is destroyed."""
        if hasattr(self, 'ruler_track'):
            self.ruler_track.cleanup()
        if hasattr(self, 'waveform_track'):
            self.waveform_track.clear_items()
        if hasattr(self, 'midi_events_track'):
            self.midi_events_track.clear_items()
        if hasattr(self, 'midi_events_blinder_track'):
            self.midi_events_blinder_track.clear_items()
        if hasattr(self, 'piano_roll_track'):
            self.piano_roll_track.clear_items()