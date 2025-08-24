from typing import Optional
from PyQt6.QtCore import QObject, pyqtSignal

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
    
    def __init__(self):
        super().__init__()
        self.current_worker: Optional[MidiWorker] = None
        self.current_filename: Optional[str] = None
        self.current_port_name: Optional[str] = None
        self.logger = get_logger("midi.manager")
        
    def start_worker(self, filename: str, port_name: str) -> bool:
        """
        Start a MIDI worker with the specified file and port.
        
        Args:
            filename: Path to MIDI file
            port_name: MIDI output port name
            
        Returns:
            True if worker started successfully
        """
        # Stop any existing worker
        self.stop_worker()
        
        # Validate inputs
        if not filename:
            self.error_occurred.emit(port_name or "Unknown", "No MIDI file specified")
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
            worker = MidiWorker(filename, port_name)
            
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
            self.current_worker.play_until(timeline_time_sec)
    
    def seek(self, time_seconds: float) -> None:
        """Seek to a specific time position."""
        if self.current_worker:
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
        
        target_filename = filename or self.current_filename
        if not target_filename:
            return False
        
        return self.start_worker(target_filename, self.current_port_name)
    
    def switch_port(self, new_port_name: str) -> bool:
        """
        Switch to a different MIDI port while keeping the same file.
        
        Args:
            new_port_name: New MIDI port name
            
        Returns:
            True if switch was successful
        """
        if not self.current_filename:
            return False
        
        return self.start_worker(self.current_filename, new_port_name)
    
    def cleanup(self) -> None:
        """Clean up resources when shutting down."""
        self.stop_worker()