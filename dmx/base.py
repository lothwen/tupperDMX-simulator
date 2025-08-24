from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional, Callable
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.sip import wrappertype


class DMXConnectionStatus(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class QABCMeta(wrappertype, type(ABC)):
    """Metaclass that combines QObject and ABC metaclasses."""
    pass


class DMXWorkerBase(QObject, ABC, metaclass=QABCMeta):
    """Abstract base class for all DMX workers."""
    
    # Signals
    dmx_data_received = pyqtSignal(bytearray)
    connection_status_changed = pyqtSignal(str)  # DMXConnectionStatus value
    error_occurred = pyqtSignal(str)  # Error message
    
    def __init__(self, source_name: str):
        super().__init__()
        self.source_name = source_name
        self.status = DMXConnectionStatus.DISCONNECTED
        self._is_running = False
        
    @abstractmethod
    def run(self) -> None:
        """Start the DMX worker. Should be called in a separate thread."""
        pass
        
    @abstractmethod
    def stop(self) -> None:
        """Stop the DMX worker gracefully."""
        pass
        
    def is_running(self) -> bool:
        """Check if the worker is currently running."""
        return self._is_running
        
    def get_status(self) -> DMXConnectionStatus:
        """Get the current connection status."""
        return self.status
        
    def get_source_name(self) -> str:
        """Get the source name/identifier."""
        return self.source_name
        
    def _set_status(self, status: DMXConnectionStatus) -> None:
        """Update connection status and emit signal."""
        if self.status != status:
            self.status = status
            self.connection_status_changed.emit(status.value)
            
    def _emit_error(self, message: str) -> None:
        """Emit error signal and set status to error."""
        self.error_occurred.emit(message)
        self._set_status(DMXConnectionStatus.ERROR)