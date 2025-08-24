from typing import Optional, List
from .base import DMXWorkerBase
from .artnet_worker import ArtNetWorker
from .usb_worker import USBDMXWorker
from config import Config


class DMXWorkerFactory:
    """Factory for creating DMX workers based on source type."""
    
    @staticmethod
    def create_worker(source: str, config: Config) -> Optional[DMXWorkerBase]:
        """
        Create a DMX worker for the given source.
        
        Args:
            source: Source identifier (e.g., "Art-Net" or USB device ID)
            config: Application configuration
            
        Returns:
            DMXWorkerBase instance or None if source is invalid
        """
        if source == "Art-Net":
            return ArtNetWorker(config)
        elif ":" in source and source.startswith("FTDI"):
            # Extract device ID from FTDI device string
            try:
                device_id = source.split(":")[2]
                return USBDMXWorker(device_id)
            except (IndexError, ValueError):
                print(f"Invalid USB DMX device format: {source}")
                return None
        else:
            print(f"Unknown DMX source type: {source}")
            return None
    
    @staticmethod
    def get_available_sources(config: Config) -> List[str]:
        """
        Get list of all available DMX sources.
        
        Args:
            config: Application configuration
            
        Returns:
            List of source identifiers
        """
        sources = ["Art-Net"]
        
        # Add available USB devices
        usb_devices = USBDMXWorker.list_available_devices()
        sources.extend(usb_devices)
        
        return sources
    
    @staticmethod
    def validate_source(source: str, config: Config) -> bool:
        """
        Validate if a source is available and properly configured.
        
        Args:
            source: Source identifier
            config: Application configuration
            
        Returns:
            True if source is valid and available
        """
        available_sources = DMXWorkerFactory.get_available_sources(config)
        return source in available_sources