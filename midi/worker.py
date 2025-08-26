import queue
import mido
from typing import Optional, List, Tuple
from enum import Enum
from PyQt6.QtCore import QThread, pyqtSignal

from tupperlogger import get_logger


class MidiConnectionStatus(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class MidiWorker(QThread):
    """
    Worker thread for MIDI playback synchronized with timeline.
    Handles loading MIDI files and sending MIDI messages to output ports.
    """
    
    # Signals
    finished = pyqtSignal()
    connection_status_changed = pyqtSignal(str)  # MidiConnectionStatus value
    error_occurred = pyqtSignal(str)  # Error message
    playback_position_changed = pyqtSignal(float)  # Current playback time
    
    def __init__(self, filename: str, port_name: str, midi_data: Optional[mido.MidiFile] = None):
        super().__init__()
        self.filename = filename
        self.port_name = port_name
        self.messages: List[Tuple[float, mido.Message]] = []
        self.last_played_index = 0
        self._is_running = True
        self.time_queue = queue.Queue()
        self.port: Optional[mido.ports.BaseOutput] = None
        self.status = MidiConnectionStatus.DISCONNECTED
        self.logger = get_logger("midi.worker")
        
        # Load MIDI file or use provided data
        if midi_data:
            self._load_midi_from_data(midi_data)
        else:
            self._load_midi_file()

    def _load_midi_file(self) -> None:
        """Load and parse the MIDI file."""
        try:
            self._set_status(MidiConnectionStatus.CONNECTING)
            
            if not self.filename:
                raise ValueError("No MIDI file specified")
            
            mid = mido.MidiFile(self.filename)
            self._load_midi_from_data(mid)
            
        except Exception as e:
            self.logger.error(f"Failed to load MIDI file: {e}")
            self._emit_error(f"Failed to load MIDI file: {e}")

    def _load_midi_from_data(self, mid: mido.MidiFile) -> None:
        """Load and parse MIDI data from a MidiFile object."""
        try:
            self._set_status(MidiConnectionStatus.CONNECTING)
            
            current_time = 0
            self.messages.clear()
            
            for msg in mid:
                current_time += msg.time
                if not msg.is_meta:
                    self.messages.append((current_time, msg))
            
            source = self.filename if self.filename else "modified MIDI data"
            self.logger.info(f"Loaded {len(self.messages)} MIDI messages from {source}")
            
        except Exception as e:
            self.logger.error(f"Failed to load MIDI data: {e}")
            self._emit_error(f"Failed to load MIDI data: {e}")

    def run(self) -> None:
        """Main worker thread execution."""
        if not self._connect_midi_port():
            return
        
        self._set_status(MidiConnectionStatus.CONNECTED)
        self._playback_loop()
        self._cleanup()

    def _connect_midi_port(self) -> bool:
        """Connect to the specified MIDI output port."""
        try:
            if not self.port_name:
                raise ValueError("No MIDI port specified")
            
            self.port = mido.open_output(self.port_name)
            self.logger.info(f"Connected to MIDI port: {self.port_name}")
            return True
            
        except Exception as e:
            self.logger.error(f"Could not open MIDI port {self.port_name}: {e}")
            self._emit_error(f"Could not open MIDI port {self.port_name}: {e}")
            return False

    def _playback_loop(self) -> None:
        """Main playback loop that processes timeline updates."""
        while self._is_running:
            try:
                timeline_time_sec = self.time_queue.get(timeout=0.1)
                
                if timeline_time_sec is None:  # Sentinel value to stop
                    break
                
                self._play_messages_until(timeline_time_sec)
                self.playback_position_changed.emit(timeline_time_sec)
                
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(f"Playback error: {e}")
                self._emit_error(f"Playback error: {e}")
                break

    def _play_messages_until(self, timeline_time_sec: float) -> None:
        """Play all MIDI messages up to the specified timeline position."""
        while self.last_played_index < len(self.messages):
            msg_time, msg = self.messages[self.last_played_index]
            
            if msg_time <= timeline_time_sec:
                # Send all MIDI messages, not just note_on
                if self.port and msg.type in ('note_on', 'note_off', 'control_change', 'program_change'):
                    try:
                        self.port.send(msg)
                        self.logger.debug(f"Sent MIDI message: {msg}")
                    except Exception as e:
                        self.logger.warning(f"Failed to send MIDI message: {e}")
                
                self.last_played_index += 1
            else:
                break

    def _cleanup(self) -> None:
        """Clean up resources."""
        if self.port:
            try:
                self.port.close()
                self.logger.debug("MIDI port closed")
            except Exception as e:
                self.logger.warning(f"Error closing MIDI port: {e}")
            finally:
                self.port = None
        
        self._set_status(MidiConnectionStatus.DISCONNECTED)
        self.finished.emit()

    def play_until(self, timeline_time_sec: float) -> None:
        """Request playback up to the specified timeline position."""
        if self._is_running:
            self.time_queue.put(timeline_time_sec)

    def seek(self, time_seconds: float) -> None:
        """Seek to a specific time position in the MIDI sequence."""
        # Send all notes off first to prevent hanging notes
        if self.port:
            try:
                # Send all notes off on all channels
                for channel in range(16):
                    all_notes_off = mido.Message('control_change', channel=channel, control=123, value=0)
                    self.port.send(all_notes_off)
                self.logger.debug("Sent all notes off before seek")
            except Exception as e:
                self.logger.warning(f"Failed to send all notes off: {e}")
        
        # Find the correct message index for the seek position
        self.last_played_index = 0
        
        for i, (msg_time, _) in enumerate(self.messages):
            if msg_time >= time_seconds:
                self.last_played_index = i
                break
        
        self.logger.debug(f"MIDI seeked to {time_seconds:.2f}s (message index: {self.last_played_index})")

    def stop(self) -> None:
        """Stop the MIDI worker gracefully."""
        self.logger.debug("Stopping MIDI worker")
        
        # Send all notes off before stopping
        if self.port:
            try:
                # Send all notes off on all channels
                for channel in range(16):
                    all_notes_off = mido.Message('control_change', channel=channel, control=123, value=0)
                    self.port.send(all_notes_off)
                self.logger.debug("Sent all notes off before stop")
            except Exception as e:
                self.logger.warning(f"Failed to send all notes off: {e}")
        
        self._is_running = False
        self.time_queue.put(None)  # Sentinel to break the loop

    def get_status(self) -> MidiConnectionStatus:
        """Get the current connection status."""
        return self.status

    def get_port_name(self) -> str:
        """Get the MIDI port name."""
        return self.port_name

    def get_filename(self) -> str:
        """Get the MIDI filename."""
        return self.filename

    def get_message_count(self) -> int:
        """Get the number of loaded MIDI messages."""
        return len(self.messages)

    def get_duration(self) -> float:
        """Get the total duration of the MIDI sequence."""
        if not self.messages:
            return 0.0
        return self.messages[-1][0]  # Time of last message

    def _set_status(self, status: MidiConnectionStatus) -> None:
        """Update connection status and emit signal."""
        if self.status != status:
            self.status = status
            self.connection_status_changed.emit(status.value)

    def _emit_error(self, message: str) -> None:
        """Emit error signal and set status to error."""
        self.error_occurred.emit(message)
        self._set_status(MidiConnectionStatus.ERROR)

    @staticmethod
    def get_available_ports() -> List[str]:
        """Get list of available MIDI output ports."""
        try:
            return mido.get_output_names()
        except Exception as e:
            logger = get_logger("midi.worker")
            logger.error(f"Error getting MIDI ports: {e}")
            return []