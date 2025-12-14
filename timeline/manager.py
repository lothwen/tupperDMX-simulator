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
                             QGraphicsItem, QDialog, QMessageBox)
from PyQt6.QtGui import QColor, QPen
from PyQt6.QtCore import Qt, pyqtSignal, QTimer

from .widgets import ContentView, MidiEventEditDialog
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
    
    # Constants
    KEY_HEIGHT = 20
    PIANO_WIDTH = 100
    RULER_TRACK_HEIGHT = 30
    WAVEFORM_TRACK_HEIGHT = 100
    MIDI_EVENTS_TRACK_HEIGHT = 50
    TRACK_SPACING = 5
    SECONDS_TO_DISPLAY = 240
    PIXELS_PER_SECOND = 20
    
    def __init__(self, midi_manager=None, audio_file=None, initial_metronome_volume=0.5, initial_audio_volume=0.5, parent=None):
        super().__init__(parent)
        self.audio_file = audio_file
        self.midi_manager = midi_manager
        
        self.min_note = 21
        self.max_note = 108
        self.bpm = 120
        
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
        note_range = self.max_note - self.min_note
        self.scene_height = (self.RULER_TRACK_HEIGHT + self.WAVEFORM_TRACK_HEIGHT + 
                           self.MIDI_EVENTS_TRACK_HEIGHT + (self.KEY_HEIGHT * note_range) + 
                           3 * self.TRACK_SPACING)

        self.header_scene.setBackgroundBrush(QColor(30, 30, 30))
        self.header_scene.setSceneRect(0, 0, self.PIANO_WIDTH, self.scene_height)
        
        self.content_scene.setBackgroundBrush(QColor(30, 30, 30))
        self.content_scene.setSceneRect(0, 0, self.content_width, self.scene_height)

        self.draw_playhead()
        self.draw_time_grid()
        
    def _init_tracks(self):
        """Initialize all timeline tracks."""
        self.ruler_track = RulerTrack(self, self.content_scene, self.header_scene)
        self.waveform_track = WaveformTrack(self, self.content_scene, self.header_scene)
        self.midi_events_track = MidiEventsTrack(self, self.content_scene, self.header_scene)
        self.piano_roll_track = PianoRollTrack(self, self.content_scene, self.header_scene)
        
        # Draw initial track layout
        self.ruler_track.draw_header()
        self.ruler_track.draw_ruler()
        
        y_offset = self.RULER_TRACK_HEIGHT + self.TRACK_SPACING
        self.waveform_track.draw_header(y_offset)
        
        y_offset += self.WAVEFORM_TRACK_HEIGHT + self.TRACK_SPACING
        self.midi_events_track.draw_header(y_offset)
        
        y_offset += self.MIDI_EVENTS_TRACK_HEIGHT + self.TRACK_SPACING
        self.piano_roll_track.draw_piano_keys(y_offset)
        self.piano_roll_track.draw_piano_roll_grid(y_offset)
        
        # Draw initial waveform if audio file provided
        if self.audio_file:
            waveform_y = self.RULER_TRACK_HEIGHT + self.TRACK_SPACING
            self.waveform_track.draw_waveform(self.audio_file, waveform_y)

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
        midi_events_y = self.RULER_TRACK_HEIGHT + self.WAVEFORM_TRACK_HEIGHT + 2 * self.TRACK_SPACING
        piano_roll_y = midi_events_y + self.MIDI_EVENTS_TRACK_HEIGHT + self.TRACK_SPACING
        
        self.midi_events_track.redraw_events(self._cached_events, midi_events_y)
        self.piano_roll_track.redraw_notes(self._cached_notes, piano_roll_y)
        
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
        if not self._cached_events or not self.midi_events_track.items:
            return
            
        zoom_factor = self.content_view.transform().m11()
        font_metrics = self.content_view.fontMetrics()
        
        # Find text items in midi event items
        from PyQt6.QtWidgets import QGraphicsTextItem
        text_items = [item for item in self.midi_events_track.items if isinstance(item, QGraphicsTextItem)]
        
        # Update each text item
        for i, text_item in enumerate(text_items):
            if i < len(self._cached_events):
                event_data = self._cached_events[i]
                
                # Calculate box width for this event
                if i < len(self._cached_events) - 1:
                    duration = self._cached_events[i+1]['time'] - event_data['time']
                else:
                    # For last event, use a reasonable default duration
                    song_length = self.midi_manager.get_duration() if self.midi_manager else 240
                    extended_duration = song_length * 1.05
                    duration = extended_duration - event_data['time']
                    if duration <= 0:
                        duration = song_length * 0.05
                
                width = max(duration * self.PIXELS_PER_SECOND, 20)
                available_width = (width - 10) * zoom_factor
                
                # Get full text and truncate appropriately
                full_text = text_item.data(0)
                if full_text:
                    text_to_display = self.get_truncated_text(full_text, available_width, font_metrics)
                    text_item.setPlainText(text_to_display)

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

    # MIDI event management methods
    def on_midi_event_timing_changed(self, event_index, new_start_time):
        """Handle when a MIDI event box left border is dragged, updating this event's start time."""
        if not self.midi_manager or event_index < 0 or event_index >= len(self._cached_events):
            return
        
        # Get the event ID from cached data
        event_data = self._cached_events[event_index]
        event_id = event_data.get('id')
        
        if not event_id:
            self.logger.error(f"No event ID found for event index {event_index}")
            return
        
        # Update via MidiManager
        if self.midi_manager.update_event_timing(event_id, new_start_time):
            # Refresh the timeline display
            self.refresh_from_manager()

    def on_midi_event_edit_requested(self, event_index):
        """Handle edit request for a MIDI event."""
        if not self.midi_manager or event_index < 0 or event_index >= len(self._cached_events):
            return
            
        event_data = self._cached_events[event_index]
        event_id = event_data.get('id')
        
        if not event_id:
            return
            
        # Use MidiManager's note map for the dialog
        dialog = MidiEventEditDialog(event_data, self.midi_manager.midi_note_map)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Get the new values
            new_values = dialog.get_values()
            
            # Update via MidiManager
            if self.midi_manager.edit_event(event_id, **new_values):
                # Refresh the timeline display
                self.refresh_from_manager()
                self.logger.info(f"MIDI event {event_index} edited: note={new_values['note']}, time={new_values['time']:.3f}s, velocity={new_values['velocity']}")

    def on_midi_event_delete_requested(self, event_index):
        """Handle delete request for a MIDI event."""
        if not self.midi_manager or event_index < 0 or event_index >= len(self._cached_events):
            return
            
        event_data = self._cached_events[event_index]
        event_id = event_data.get('id')
        
        if not event_id:
            return
            
        # Confirm deletion
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
            # Delete via MidiManager
            if self.midi_manager.delete_event(event_id):
                # Refresh the timeline display
                self.refresh_from_manager()
                self.logger.info(f"MIDI event {event_index} deleted: note={event_data['note']}, time={event_data['time']:.3f}s")

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
            waveform_y = self.RULER_TRACK_HEIGHT + self.TRACK_SPACING
            self.waveform_track.draw_waveform(audio_file, waveform_y)
        
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
        if hasattr(self, 'piano_roll_track'):
            self.piano_roll_track.clear_items()