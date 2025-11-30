from typing import Optional, List, Dict, Any, Callable
from PyQt6.QtCore import QObject, pyqtSignal
import mido
import uuid
import copy

from tupperlogger import get_logger
from .worker import MidiWorker, MidiConnectionStatus


class MidiManager(QObject):
    """
    Manages MIDI worker lifecycle and provides a high-level interface.
    """
    
    # Signals
    connection_status_changed = pyqtSignal(str, str)  # port_name, status
    error_occurred = pyqtSignal(str, str)  # port_name, error message
    playback_position_changed = pyqtSignal(float)  # current time
    worker_finished = pyqtSignal()
    data_changed = pyqtSignal()  # Emitted when MIDI data is modified
    
    def __init__(self, midi_note_map: Optional[Dict] = None):
        super().__init__()
        self.current_worker: Optional[MidiWorker] = None
        self.current_filename: Optional[str] = None
        self.current_port_name: Optional[str] = None
        
        # MIDI Data Storage - Single Source of Truth
        self.midi_data: Optional[mido.MidiFile] = None
        self.midi_events: List[Dict[str, Any]] = []  # Events for UI display with unique IDs
        self.midi_notes: List[Dict[str, Any]] = []   # Notes for piano roll with unique IDs
        self.midi_note_map = midi_note_map or {}
        
        # Track current playback position for in-place updates
        self._current_playback_time: float = 0.0
        
        self.logger = get_logger("midi.manager")
        
        # Software controller reference
        self.software_controller = None

    def set_software_controller(self, controller):
        """Set the software controller instance."""
        self.software_controller = controller
        if self.current_worker:
            self.current_worker.set_software_controller(controller)

    # MIDI Data Management Methods  
    def load_from_file(self, file_path: str) -> bool:
        """Load MIDI data from file and parse it for UI display."""
        try:
            self.midi_data = mido.MidiFile(file_path)
            self.current_filename = file_path
            self._parse_midi_data()
            self.logger.info(f"Loaded MIDI file: {file_path}")
            self.data_changed.emit()
            return True
        except Exception as e:
            self.logger.error(f"Failed to load MIDI file {file_path}: {e}")
            return False
    
    def create_empty_track(self) -> None:
        """Create an empty MIDI track for new compositions."""
        self.midi_data = mido.MidiFile(type=1, ticks_per_beat=480)
        self.midi_data.tracks.append(mido.MidiTrack())
        self.midi_events.clear()
        self.midi_notes.clear()
        self.current_filename = None
        self.logger.info("Created empty MIDI track")
        self.data_changed.emit()
        
    def set_midi_note_map(self, midi_note_map: Dict) -> None:
        """Update the MIDI note mapping and reparse data if available."""
        self.midi_note_map = midi_note_map
        if self.midi_data:
            self._parse_midi_data()
            self.data_changed.emit()
    
    def _parse_midi_data(self) -> None:
        """Parse the loaded MIDI data into events and notes for UI display."""
        if not self.midi_data:
            return
            
        self.midi_events.clear()
        self.midi_notes.clear()
        
        # Parse for MIDI events (using note map)
        current_time = 0
        for msg in self.midi_data:
            current_time += msg.time
            if msg.type == 'note_on' and msg.velocity > 0:
                # Check if this note is in our note map (for events track)
                note_info = self.midi_note_map.get(str(msg.note))
                if note_info:
                    event_id = str(uuid.uuid4())
                    self.midi_events.append({
                        "id": event_id,
                        "note": msg.note,
                        "time": current_time,
                        "velocity": msg.velocity,
                        "info": note_info
                    })
        
        # Parse for piano roll notes
        current_time = 0
        active_notes = {}
        for msg in self.midi_data:
            current_time += msg.time
            if msg.type == 'note_on' and msg.velocity > 0:
                active_notes[msg.note] = {"time": current_time, "velocity": msg.velocity}
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                if msg.note in active_notes:
                    note_id = str(uuid.uuid4())
                    start_time = active_notes[msg.note]["time"]
                    self.midi_notes.append({
                        "id": note_id,
                        "note": msg.note,
                        "time": start_time,
                        "duration": current_time - start_time,
                        "velocity": active_notes[msg.note]["velocity"]
                    })
                    del active_notes[msg.note]
                    
    def get_events_for_ui(self) -> List[Dict[str, Any]]:
        """Get MIDI events formatted for Timeline display."""
        return copy.deepcopy(self.midi_events)
    
    def get_notes_for_ui(self) -> List[Dict[str, Any]]:
        """Get MIDI notes formatted for piano roll display.""" 
        return copy.deepcopy(self.midi_notes)
    
    def get_duration(self) -> float:
        """Get the total duration of the MIDI data."""
        if self.midi_data:
            return self.midi_data.length
        return 0.0

    # MIDI Modification Methods
    
    def add_event(self, note: int, time: float, velocity: int) -> str:
        """Add a new MIDI event and return its ID."""
        if str(note) not in self.midi_note_map:
            self.logger.warning(f"Note {note} not in MIDI note map")
            return ""
            
        event_id = str(uuid.uuid4())
        new_event = {
            "id": event_id,
            "note": note,
            "time": time,
            "velocity": velocity,
            "info": self.midi_note_map[str(note)]
        }
        
        # Insert in chronological order
        inserted = False
        for i, event in enumerate(self.midi_events):
            if event['time'] > time:
                self.midi_events.insert(i, new_event)
                inserted = True
                break
        if not inserted:
            self.midi_events.append(new_event)
        
        # Also add to piano roll
        note_id = str(uuid.uuid4())
        new_note = {
            "id": note_id,
            "note": note,
            "time": time,
            "velocity": velocity,
            "duration": 0.1  # Default duration
        }
        
        inserted = False
        for i, note_data in enumerate(self.midi_notes):
            if note_data['time'] > time:
                self.midi_notes.insert(i, new_note)
                inserted = True
                break
        if not inserted:
            self.midi_notes.append(new_note)
            
        self._rebuild_midi_data()
        self._auto_update_worker()
        self.data_changed.emit()
        self.logger.info(f"Added MIDI event: note={note}, time={time:.3f}s")
        return event_id
    
    def update_event_timing(self, event_id: str, new_time: float) -> bool:
        """Update the timing of a MIDI event."""
        # Find and update event
        event = None
        for e in self.midi_events:
            if e['id'] == event_id:
                old_time = e['time']
                e['time'] = new_time
                event = e
                break
        
        if not event:
            return False
            
        # Find and update corresponding note
        for note in self.midi_notes:
            if (note['note'] == event['note'] and abs(note['time'] - old_time) < 0.01):
                note['time'] = new_time
                break
        
        # Re-sort events by time
        self.midi_events.sort(key=lambda x: x['time'])
        self.midi_notes.sort(key=lambda x: x['time'])
        
        self._rebuild_midi_data()
        self._auto_update_worker()
        self.data_changed.emit()
        self.logger.info(f"Updated event timing: {event_id} to {new_time:.3f}s")
        return True
    
    def edit_event(self, event_id: str, **kwargs) -> bool:
        """Edit a MIDI event's properties."""
        # Find event
        event = None
        for e in self.midi_events:
            if e['id'] == event_id:
                event = e
                break
                
        if not event:
            return False
        
        old_note = event['note']
        old_time = event['time']
        
        # Update event properties
        if 'note' in kwargs and str(kwargs['note']) in self.midi_note_map:
            event['note'] = kwargs['note']
            event['info'] = self.midi_note_map[str(kwargs['note'])]
        if 'time' in kwargs:
            event['time'] = kwargs['time']
        if 'velocity' in kwargs:
            event['velocity'] = kwargs['velocity']
            
        # Find and update corresponding note
        for note in self.midi_notes:
            if (note['note'] == old_note and abs(note['time'] - old_time) < 0.01):
                if 'note' in kwargs:
                    note['note'] = kwargs['note']
                if 'time' in kwargs:
                    note['time'] = kwargs['time']
                if 'velocity' in kwargs:
                    note['velocity'] = kwargs['velocity']
                break
                
        # Re-sort by time
        self.midi_events.sort(key=lambda x: x['time'])
        self.midi_notes.sort(key=lambda x: x['time'])
        
        self._rebuild_midi_data()
        self._auto_update_worker()
        self.data_changed.emit()
        self.logger.info(f"Edited event: {event_id}")
        return True
    
    def delete_event(self, event_id: str) -> bool:
        """Delete a MIDI event."""
        # Find and remove event
        event = None
        for i, e in enumerate(self.midi_events):
            if e['id'] == event_id:
                event = self.midi_events.pop(i)
                break
                
        if not event:
            return False
            
        # Find and remove corresponding note
        note_to_remove = None
        for i, note in enumerate(self.midi_notes):
            if (note['note'] == event['note'] and abs(note['time'] - event['time']) < 0.01):
                note_to_remove = self.midi_notes.pop(i)
                break
                
        self._rebuild_midi_data()
        self._auto_update_worker()
        self.data_changed.emit()
        self.logger.info(f"Deleted event: {event_id}")
        return True
    
    def _rebuild_midi_data(self) -> None:
        """Rebuild the MIDI file from current event and note data."""
        if not self.midi_data:
            return
            
        try:
            # Create new MIDI file with current structure
            new_mid = mido.MidiFile(type=self.midi_data.type, ticks_per_beat=self.midi_data.ticks_per_beat)
            track = mido.MidiTrack()
            new_mid.tracks.append(track)
            
            # Add tempo and meta messages from original
            tempo_microseconds = 500000  # Default tempo (120 BPM)
            for msg in self.midi_data.merged_track:
                if msg.is_meta:
                    if msg.type == 'set_tempo':
                        tempo_microseconds = msg.tempo
                    track.append(msg.copy())
            
            # Build all MIDI events from notes data
            all_events = []
            for note_data in self.midi_notes:
                # Note on
                all_events.append({
                    'time': note_data['time'],
                    'type': 'note_on',
                    'note': note_data['note'],
                    'velocity': note_data['velocity'],
                    'channel': 0
                })
                # Note off
                all_events.append({
                    'time': note_data['time'] + note_data['duration'],
                    'type': 'note_off',
                    'note': note_data['note'],
                    'velocity': 0,
                    'channel': 0
                })
            
            # Sort by time and convert to MIDI messages
            all_events.sort(key=lambda x: x['time'])
            current_time_seconds = 0
            
            for event in all_events:
                delta_time_seconds = event['time'] - current_time_seconds
                delta_ticks = int(mido.second2tick(delta_time_seconds,
                                                 ticks_per_beat=new_mid.ticks_per_beat,
                                                 tempo=tempo_microseconds))
                
                msg = mido.Message(
                    event['type'],
                    channel=event['channel'],
                    note=event['note'],
                    velocity=event['velocity'],
                    time=delta_ticks
                )
                track.append(msg)
                current_time_seconds = event['time']
            
            self.midi_data = new_mid
            
        except Exception as e:
            self.logger.error(f"Error rebuilding MIDI data: {e}")
    
    def _auto_update_worker(self) -> None:
        """Automatically update the worker with current MIDI data if running."""
        if self.current_worker and self.current_port_name and self.midi_data:
            # Try to update worker in-place to preserve playback position
            if self._update_worker_in_place():
                self.logger.debug("Updated worker MIDI data in-place")
            else:
                # Fallback to restarting if in-place update fails
                self.logger.debug("In-place update failed, restarting worker with modified MIDI data")
                self.start_worker(self.current_filename or "modified", self.current_port_name, self.midi_data)

    def _update_worker_in_place(self) -> bool:
        """Update the worker's MIDI data without restarting, preserving playback position."""
        if not self.current_worker or not self.current_worker.isRunning():
            return False
            
        # Get current playback position
        current_time = self._get_current_playback_time()
        
        # Safety check: if MIDI data is None or empty, we can't do in-place update
        if not self.midi_data:
            self.logger.warning("Cannot update worker in-place: no MIDI data available")
            return False
        
        # Update the worker's MIDI data
        success = self.current_worker.update_midi_data(self.midi_data, current_time)
        
        if success:
            self.logger.debug(f"Successfully updated worker MIDI data in-place at position {current_time:.2f}s")
        else:
            self.logger.warning("In-place MIDI data update failed, will need to restart worker")
            
        return success

    def _get_current_playback_time(self) -> float:
        """Get the current playback time from the MediaController."""
        # We need a way to get the current playback position
        # This will be set by the MediaController when it calls play_until()
        return getattr(self, '_current_playback_time', 0.0)

    def set_current_playback_time(self, time_seconds: float) -> None:
        """Set the current playback time (called by MediaController)."""
        self._current_playback_time = time_seconds
    
    def export_to_file(self, file_path: str) -> bool:
        """Export current MIDI data to file using safe temporary file approach."""
        if not self.midi_data:
            self.logger.error("No MIDI data to export")
            return False
        
        import os
        import tempfile
        import shutil
        
        # Create temporary file in the same directory as target
        target_dir = os.path.dirname(os.path.abspath(file_path))
        temp_path = None
        
        try:
            # Create temporary file with .tmp extension in target directory
            temp_fd, temp_path = tempfile.mkstemp(suffix='.tmp', dir=target_dir, prefix='midi_export_')
            os.close(temp_fd)  # Close file descriptor, we'll use the path
            
            # Export MIDI data to temporary file first
            self.midi_data.save(temp_path)
            self.logger.debug(f"MIDI data written to temporary file: {temp_path}")
            
            # Verify the temporary file was created and has content
            if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
                raise Exception("Temporary MIDI file is empty or was not created")
            
            # Atomic move from temporary file to final destination
            shutil.move(temp_path, file_path)
            self.logger.info(f"Exported MIDI data to: {file_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to export MIDI file {file_path}: {e}")
            
            # Clean up temporary file if it still exists
            try:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
                    self.logger.debug(f"Cleaned up temporary file: {temp_path}")
            except Exception as cleanup_error:
                self.logger.warning(f"Failed to clean up temporary file {temp_path}: {cleanup_error}")
            
            return False

    # Enhanced playback methods
    
    def start_playback(self, port_name: str) -> bool:
        """Start MIDI playback with current data."""
        if not self.midi_data:
            self.logger.warning("No MIDI data available for playback")
            return False
            
        return self.start_worker(self.current_filename or "current", port_name, self.midi_data)
            
    def start_worker(self, filename: str, port_name: str, midi_data: Optional[mido.MidiFile] = None) -> bool:
        """
        Start a MIDI worker with the specified file and port.
        
        Args:
            filename: Path to MIDI file (used for identification)
            port_name: MIDI output port name
            midi_data: Optional MidiFile object to use instead of loading from file
            
        Returns:
            True if worker started successfully
        """
        # Stop any existing worker
        self.stop_worker()
        
        # Use current MIDI data if available and no specific data provided
        if not midi_data and self.midi_data:
            midi_data = self.midi_data
        
        # Validate inputs
        if not filename and not midi_data:
            self.error_occurred.emit(port_name or "Unknown", "No MIDI file or data specified")
            return False
        
        if not port_name:
            self.error_occurred.emit("Unknown", "No MIDI port specified")
            return False
        
        # Validate port availability
        available_ports = MidiWorker.get_available_ports()
        if port_name not in available_ports:
            self.error_occurred.emit(port_name, f"MIDI port not available. Available ports: {available_ports}")
            return False
        
        # Create and start worker
        try:
            worker = MidiWorker(filename, port_name, midi_data)
            if self.software_controller:
                worker.set_software_controller(self.software_controller)
            
            # Connect signals
            worker.connection_status_changed.connect(
                lambda status: self.connection_status_changed.emit(port_name, status)
            )
            worker.error_occurred.connect(
                lambda error: self.error_occurred.emit(port_name, error)
            )
            worker.playback_position_changed.connect(self.playback_position_changed.emit)
            worker.finished.connect(self.worker_finished.emit)
            worker.finished.connect(worker.deleteLater)
            
            # Store references
            self.current_worker = worker
            self.current_filename = filename
            self.current_port_name = port_name
            
            # Reset playback time tracking when starting new worker
            self._current_playback_time = 0.0
            
            # Start the worker thread
            worker.start()
            
            self.logger.info(f"Started MIDI worker for {filename} on port {port_name}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to start MIDI worker: {e}")
            self.error_occurred.emit(port_name, f"Failed to start MIDI worker: {e}")
            return False
    
    def stop_worker(self) -> None:
        """Stop the current MIDI worker."""
        if self.current_worker:
            self.logger.info(f"Stopping MIDI worker for {self.current_filename}")
            
            # Stop the worker
            self.current_worker.stop()
            
            # Wait for thread to finish (with timeout)
            if self.current_worker.isRunning():
                self.current_worker.wait(3000)  # 3 second timeout
                
                if self.current_worker.isRunning():
                    self.logger.warning("MIDI worker thread did not stop gracefully, terminating")
                    self.current_worker.terminate()
                    self.current_worker.wait(1000)
            
            # Clean up references
            self.current_worker = None
            self.current_filename = None
            self.current_port_name = None
            
            self.logger.info("MIDI worker stopped")
    
    def play_until(self, timeline_time_sec: float) -> None:
        """Request playback up to the specified timeline position."""
        if self.current_worker:
            # Track the current playback time for in-place updates
            self._current_playback_time = timeline_time_sec
            self.current_worker.play_until(timeline_time_sec)
    
    def seek(self, time_seconds: float) -> None:
        """Seek to a specific time position."""
        if self.current_worker:
            # Track the current playback time for in-place updates
            self._current_playback_time = time_seconds
            self.current_worker.seek(time_seconds)
    
    def get_current_filename(self) -> Optional[str]:
        """Get the current MIDI filename."""
        return self.current_filename
    
    def get_current_port_name(self) -> Optional[str]:
        """Get the current MIDI port name."""
        return self.current_port_name
    
    def get_current_status(self) -> Optional[MidiConnectionStatus]:
        """Get the current connection status."""
        if self.current_worker:
            return self.current_worker.get_status()
        return None
    
    def is_running(self) -> bool:
        """Check if a MIDI worker is currently running."""
        return self.current_worker is not None and self.current_worker.isRunning()
    
    def is_connected(self) -> bool:
        """Check if currently connected to a MIDI port."""
        status = self.get_current_status()
        return status == MidiConnectionStatus.CONNECTED if status else False
    
    def get_available_ports(self) -> list[str]:
        """Get list of all available MIDI output ports."""
        return MidiWorker.get_available_ports()
    
    def get_connection_info(self) -> Optional[dict]:
        """Get detailed connection information."""
        if self.current_worker:
            return {
                'type': 'MIDI',
                'filename': self.current_filename,
                'port_name': self.current_port_name,
                'status': self.current_worker.get_status().value,
                'message_count': self.current_worker.get_message_count(),
                'duration': self.current_worker.get_duration()
            }
        return None
    
    def reload_file(self, filename: Optional[str] = None) -> bool:
        """
        Reload the current MIDI file or load a new one.
        
        Args:
            filename: New filename, or None to reload current file
            
        Returns:
            True if reload was successful
        """
        if not self.current_port_name:
            return False
        
        if filename:
            # Loading a new file
            return self.load_from_file(filename) and self.start_playback(self.current_port_name)
        else:
            # Reloading current data
            return self.start_playback(self.current_port_name)
    
    def switch_port(self, new_port_name: str) -> bool:
        """
        Switch to a different MIDI port while keeping current data.
        
        Args:
            new_port_name: New MIDI port name
            
        Returns:
            True if switch was successful
        """
        if not self.midi_data:
            return False
        
        return self.start_playback(new_port_name)
    
    def cleanup(self) -> None:
        """Clean up resources when shutting down."""
        self.stop_worker()