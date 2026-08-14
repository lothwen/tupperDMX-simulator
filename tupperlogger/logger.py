import logging
import logging.config
import os
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime

from .handlers import ColoredConsoleHandler, FileRotatingHandler, ComponentFilter, DefaultComponentFilter, PerformanceFilter


class TupperDMXLogger:
    """Centralized logging configuration for TupperDMX simulator."""
    
    _instance: Optional['TupperDMXLogger'] = None
    _initialized: bool = False
    
    def __new__(cls) -> 'TupperDMXLogger':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not self._initialized:
            self.log_dir = Path("logs")
            self.loggers: Dict[str, logging.Logger] = {}
            self._initialized = True
    
    def setup_logging(self,
                     level: str = "INFO",
                     console_output: bool = True,
                     file_output: bool = True,
                     log_dir: Optional[str] = None,
                     max_file_size: int = 10 * 1024 * 1024,
                     backup_count: int = 5) -> None:
        """
        Set up the logging system with specified configuration.
        
        Args:
            level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            console_output: Enable console logging
            file_output: Enable file logging
            log_dir: Directory for log files (default: ./logs)
            max_file_size: Maximum size of log files before rotation
            backup_count: Number of backup files to keep
        """
        if log_dir:
            self.log_dir = Path(log_dir)
        
        # Create log directory if needed
        if file_output:
            self.log_dir.mkdir(exist_ok=True)
        
        # Configure root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, level.upper()))
        
        # Remove existing handlers
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # Set up formatters
        console_formatter = logging.Formatter(
            fmt='%(asctime)s | %(name)-20s | %(levelname)-8s | %(message)s',
            datefmt='%H:%M:%S'
        )
        
        file_formatter = logging.Formatter(
            fmt='%(asctime)s | %(name)-20s | %(levelname)-8s | %(component)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Console handler
        if console_output:
            console_handler = ColoredConsoleHandler()
            console_handler.setLevel(getattr(logging, level.upper()))
            console_handler.setFormatter(console_formatter)
            
            # Add performance filter to prevent spam
            perf_filter = PerformanceFilter(max_per_second=5.0)
            console_handler.addFilter(perf_filter)
            
            root_logger.addHandler(console_handler)
        
        # File handler
        if file_output:
            log_file = self.log_dir / f"tupperDMX_{datetime.now().strftime('%Y%m%d')}.log"
            file_handler = FileRotatingHandler(
                filename=str(log_file),
                max_bytes=max_file_size,
                backup_count=backup_count
            )
            file_handler.setLevel(logging.DEBUG)  # Always capture everything to file
            file_handler.setFormatter(file_formatter)
            file_handler.addFilter(DefaultComponentFilter())

            root_logger.addHandler(file_handler)
        
        # Set up specific logger levels
        self._configure_component_loggers()
        self._silence_noisy_third_party_loggers()
        
        # Log the startup
        startup_logger = self.get_logger("system.startup")
        startup_logger.info(f"TupperDMX logging system initialized (level: {level})")
        if file_output:
            startup_logger.info(f"Log files will be written to: {self.log_dir}")
    
    def _configure_component_loggers(self) -> None:
        """Configure specific loggers for different components."""
        
        # DMX-related loggers
        dmx_loggers = [
            "dmx.manager",
            "dmx.artnet",
            "dmx.usb",
            "dmx.worker"
        ]
        
        for logger_name in dmx_loggers:
            logger = logging.getLogger(logger_name)
            logger.addFilter(ComponentFilter("DMX"))
        
        # MIDI-related loggers
        midi_loggers = [
            "midi.manager",
            "midi.worker"
        ]
        
        for logger_name in midi_loggers:
            logger = logging.getLogger(logger_name)
            logger.addFilter(ComponentFilter("MIDI"))
        
        # UI-related loggers
        ui_loggers = [
            "ui.main",
            "ui.timeline",
            "ui.fixtures",
            "stage"
        ]
        
        for logger_name in ui_loggers:
            logger = logging.getLogger(logger_name)
            logger.addFilter(ComponentFilter("UI"))
        
        # System loggers
        system_loggers = [
            "system.startup",
            "system.config",
            "system.shutdown"
        ]
        
        for logger_name in system_loggers:
            logger = logging.getLogger(logger_name)
            logger.addFilter(ComponentFilter("SYSTEM"))
        
        # Audio/Media loggers
        media_loggers = [
            "media.player",
            "media.audio"
        ]
        
        for logger_name in media_loggers:
            logger = logging.getLogger(logger_name)
            logger.addFilter(ComponentFilter("MEDIA"))
    
    def _silence_noisy_third_party_loggers(self) -> None:
        """Cap third-party libraries at WARNING so their DEBUG/INFO chatter doesn't flood our handlers."""
        for name in ("numba", "matplotlib", "PIL", "librosa", "asyncio", "urllib3"):
            logging.getLogger(name).setLevel(logging.WARNING)

    def get_logger(self, name: str) -> logging.Logger:
        """
        Get a logger instance for the specified component.
        
        Args:
            name: Logger name (e.g., 'dmx.manager', 'ui.main')
            
        Returns:
            Configured logger instance
        """
        if name not in self.loggers:
            logger = logging.getLogger(name)
            # Add component filter to ensure 'component' field is available
            component_filter = ComponentFilter(name)
            logger.addFilter(component_filter)
            self.loggers[name] = logger
        
        return self.loggers[name]
    
    def set_level(self, level: str, component: Optional[str] = None) -> None:
        """
        Change logging level at runtime.
        
        Args:
            level: New logging level
            component: Specific component to change (None for root logger)
        """
        log_level = getattr(logging, level.upper())
        
        if component:
            logger = self.get_logger(component)
            logger.setLevel(log_level)
            self.get_logger("system.config").info(f"Changed {component} log level to {level}")
        else:
            logging.getLogger().setLevel(log_level)
            self.get_logger("system.config").info(f"Changed root log level to {level}")
    
    def get_log_stats(self) -> Dict[str, Any]:
        """Get logging statistics and information."""
        stats = {
            "log_directory": str(self.log_dir),
            "active_loggers": list(self.loggers.keys()),
            "root_level": logging.getLogger().level,
            "handlers": []
        }
        
        for handler in logging.getLogger().handlers:
            handler_info = {
                "type": type(handler).__name__,
                "level": handler.level,
                "formatter": type(handler.formatter).__name__ if handler.formatter else None
            }
            if hasattr(handler, 'baseFilename'):
                handler_info["file"] = handler.baseFilename
            stats["handlers"].append(handler_info)
        
        return stats


# Global instance
_logger_instance = TupperDMXLogger()


def setup_logging(**kwargs) -> None:
    """Set up the global logging system."""
    _logger_instance.setup_logging(**kwargs)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance."""
    return _logger_instance.get_logger(name)


def set_log_level(level: str, component: Optional[str] = None) -> None:
    """Change logging level at runtime."""
    _logger_instance.set_level(level, component)


def get_log_stats() -> Dict[str, Any]:
    """Get logging statistics."""
    return _logger_instance.get_log_stats()