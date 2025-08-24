import logging
import logging.handlers
import sys
from typing import Dict, Optional
from enum import Enum


class LogColor(Enum):
    """ANSI color codes for console logging."""
    RESET = '\033[0m'
    BOLD = '\033[1m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    GRAY = '\033[90m'


class ColoredConsoleHandler(logging.StreamHandler):
    """Console handler with colored output based on log level."""
    
    LEVEL_COLORS: Dict[int, LogColor] = {
        logging.DEBUG: LogColor.GRAY,
        logging.INFO: LogColor.CYAN,
        logging.WARNING: LogColor.YELLOW,
        logging.ERROR: LogColor.RED,
        logging.CRITICAL: LogColor.MAGENTA,
    }
    
    def __init__(self, stream=None):
        super().__init__(stream or sys.stdout)
        
    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record with colored output."""
        try:
            message = self.format(record)
            
            # Only add colors if the stream supports it (e.g., terminal)
            if self.stream.isatty():
                color = self.LEVEL_COLORS.get(record.levelno, LogColor.WHITE)
                colored_message = f"{color.value}{message}{LogColor.RESET.value}"
            else:
                colored_message = message
            
            self.stream.write(colored_message + self.terminator)
            self.flush()
            
        except Exception:
            self.handleError(record)


class FileRotatingHandler(logging.handlers.RotatingFileHandler):
    """File handler with automatic rotation and size management."""
    
    def __init__(self, 
                 filename: str,
                 max_bytes: int = 10 * 1024 * 1024,  # 10MB default
                 backup_count: int = 5,
                 encoding: Optional[str] = 'utf-8',
                 delay: bool = False):
        """
        Initialize the rotating file handler.
        
        Args:
            filename: Path to the log file
            max_bytes: Maximum file size before rotation
            backup_count: Number of backup files to keep
            encoding: File encoding
            delay: If True, file opening is deferred until first emit
        """
        super().__init__(
            filename=filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding=encoding,
            delay=delay
        )


class ComponentFilter(logging.Filter):
    """Filter that adds component information to log records."""
    
    def __init__(self, component_name: str):
        super().__init__()
        self.component_name = component_name
        
    def filter(self, record: logging.LogRecord) -> bool:
        """Add component name to the log record."""
        record.component = self.component_name
        return True


class PerformanceFilter(logging.Filter):
    """Filter that can suppress high-frequency log messages."""
    
    def __init__(self, max_per_second: float = 10.0):
        super().__init__()
        self.max_per_second = max_per_second
        self.last_time = {}
        self.message_counts = {}
        
    def filter(self, record: logging.LogRecord) -> bool:
        """Rate limit log messages to prevent spam."""
        import time
        
        # Create a key based on logger name and message
        key = f"{record.name}:{record.getMessage()[:50]}"  # First 50 chars
        current_time = time.time()
        
        if key in self.last_time:
            time_diff = current_time - self.last_time[key]
            if time_diff < (1.0 / self.max_per_second):
                # Too frequent, suppress this message
                self.message_counts[key] = self.message_counts.get(key, 0) + 1
                return False
            else:
                # Reset counter and allow message
                if key in self.message_counts and self.message_counts[key] > 0:
                    suppressed = self.message_counts[key]
                    record.msg = f"{record.msg} (suppressed {suppressed} similar messages)"
                    self.message_counts[key] = 0
        
        self.last_time[key] = current_time
        return True