from typing import Optional, Callable
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from config import Config
from tupperlogger import get_logger
from .base import DMXWorkerBase, DMXConnectionStatus
from .factory import DMXWorkerFactory


class DMXManager(QObject):
    """
    Manages DMX workers and their lifecycle.
    Provides a high-level interface for DMX operations.
    """
    
    # Signals
    dmx_data_received = pyqtSignal(bytearray)
    connection_status_changed = pyqtSignal(str, str)  # source, status
    error_occurred = pyqtSignal(str, str)  # source, error message
    source_changed = pyqtSignal(str)  # new source
    software_controller_changed = pyqtSignal(object) # software controller instance
    
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.current_worker: Optional[DMXWorkerBase] = None
        self.current_thread: Optional[QThread] = None
        self.current_source: Optional[str] = None
        self.logger = get_logger("dmx.manager")
        
    def start_worker(self, source: str) -> bool:
        """
        Start a DMX worker for the specified source.
        
        Args:
            source: DMX source identifier
            
        Returns:
            True if worker started successfully
        """
        # Stop any existing worker
        self.stop_worker()
        
        # Validate source
        if not DMXWorkerFactory.validate_source(source, self.config):
            self.logger.error(f"Invalid or unavailable DMX source: {source}")
            self.error_occurred.emit(source, f"Invalid or unavailable DMX source: {source}")
            return False
        
        # Create new worker
        worker = DMXWorkerFactory.create_worker(source, self.config)
        if not worker:
            self.logger.error(f"Failed to create worker for source: {source}")
            self.error_occurred.emit(source, f"Failed to create worker for source: {source}")
            return False
        
        # Create thread and move worker to it
        thread = QThread()
        worker.moveToThread(thread)
        
        # Connect signals
        worker.dmx_data_received.connect(self.dmx_data_received.emit)
        worker.connection_status_changed.connect(
            lambda status: self.connection_status_changed.emit(source, status)
        )
        worker.error_occurred.connect(
            lambda error: self.error_occurred.emit(source, error)
        )
        
        # Connect software controller signal if available
        if hasattr(worker, 'software_controller_changed'):
            worker.software_controller_changed.connect(self.software_controller_changed.emit)
        
        # Connect thread lifecycle
        thread.started.connect(worker.run)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        
        # Store references
        self.current_worker = worker
        self.current_thread = thread
        self.current_source = source
        
        # Start the thread
        thread.start()
        
        # Update config and emit signal
        self.config.last_dmx_source = source
        self.source_changed.emit(source)
        
        self.logger.info(f"Started DMX worker for {source}")
        return True
    
    def stop_worker(self) -> None:
        """Stop the current DMX worker."""
        if self.current_worker and self.current_thread:
            self.logger.info(f"Stopping DMX worker for {self.current_source}")
            
            # Stop the worker
            self.current_worker.stop()
            
            # Wait for thread to finish (with timeout)
            if self.current_thread.isRunning():
                self.current_thread.quit()
                self.current_thread.wait(5000)  # 5 second timeout
                
                if self.current_thread.isRunning():
                    self.logger.warning("DMX worker thread did not stop gracefully, terminating")
                    self.current_thread.terminate()
                    self.current_thread.wait(1000)
            
            # Clean up references
            self.current_worker = None
            self.current_thread = None
            self.current_source = None
            
            self.logger.info("DMX worker stopped")
    
    def switch_source(self, new_source: str) -> bool:
        """
        Switch to a different DMX source.
        
        Args:
            new_source: New DMX source identifier
            
        Returns:
            True if switch was successful
        """
        if self.current_source == new_source:
            return True  # Already using this source
        
        return self.start_worker(new_source)
    
    def get_current_source(self) -> Optional[str]:
        """Get the current DMX source identifier."""
        return self.current_source
    
    def get_current_status(self) -> Optional[DMXConnectionStatus]:
        """Get the current connection status."""
        if self.current_worker:
            return self.current_worker.get_status()
        return None
    
    def is_connected(self) -> bool:
        """Check if currently connected to a DMX source."""
        status = self.get_current_status()
        return status == DMXConnectionStatus.CONNECTED if status else False
    
    def get_available_sources(self) -> list[str]:
        """Get list of all available DMX sources."""
        return DMXWorkerFactory.get_available_sources(self.config)
    
    def get_connection_info(self) -> Optional[dict]:
        """Get detailed connection information for current worker."""
        if self.current_worker and hasattr(self.current_worker, 'get_connection_info'):
            return self.current_worker.get_connection_info()
        return None
    
    def cleanup(self) -> None:
        """Clean up resources when shutting down."""
        self.stop_worker()