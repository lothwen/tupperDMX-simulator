import threading
import time
from typing import Optional
from pylibftdi import Device

from tupperlogger import get_logger
from .base import DMXWorkerBase, DMXConnectionStatus


class USBDMXWorker(DMXWorkerBase):
    """Worker for receiving DMX data via USB FTDI interface."""
    
    def __init__(self, device_id: str):
        super().__init__(f"USB DMX ({device_id})")
        self.device_id = device_id
        self.stop_event = threading.Event()
        self.device: Optional[Device] = None
        self.logger = get_logger("dmx.usb")
        
        # USB DMX configuration
        self.baudrate = 250000
        self.line_property = (8, 2, 0)  # 8 data bits, 2 stop bits, no parity
        self.read_timeout = 0.1  # 100ms timeout for reads

    def run(self) -> None:
        """Start the USB DMX worker."""
        self._is_running = True
        self._set_status(DMXConnectionStatus.CONNECTING)
        
        try:
            self._connect_device()
            self._read_loop()
        except Exception as e:
            self.logger.error(f"USB DMX error: {e}")
            self._emit_error(f"USB DMX error: {e}")
        finally:
            self._disconnect_device()
            self._is_running = False
            self._set_status(DMXConnectionStatus.DISCONNECTED)
            self.logger.info("USB DMX worker stopped")

    def _connect_device(self) -> None:
        """Connect to the USB DMX device."""
        try:
            self.logger.info(f"Connecting to USB DMX device: {self.device_id}")
            self.device = Device(self.device_id, mode='b')
            self.device.baudrate = self.baudrate
            self.device.line_property = self.line_property
            self.device.flush_input()
            
            self._set_status(DMXConnectionStatus.CONNECTED)
            self.logger.info(f"USB DMX device connected: {self.device_id}")
            
        except Exception as e:
            self.logger.error(f"Failed to connect to USB DMX device {self.device_id}: {e}")
            raise ConnectionError(f"Failed to connect to USB DMX device {self.device_id}: {e}")

    def _read_loop(self) -> None:
        """Main data reading loop."""
        consecutive_errors = 0
        max_consecutive_errors = 10
        
        while not self.stop_event.is_set() and self.device:
            try:
                # Read data with timeout
                data = self.device.read(1024)
                
                if data and len(data) >= 96:  # Minimum DMX frame size
                    # Extract DMX data (skip first 2 bytes which are typically header)
                    dmx_frame = bytearray(data[2:])
                    self.dmx_data_received.emit(dmx_frame)
                    self.logger.debug(f"Received USB DMX data: {len(dmx_frame)} bytes")
                    consecutive_errors = 0
                else:
                    # No data or insufficient data, short pause
                    time.sleep(0.01)
                    
            except Exception as e:
                consecutive_errors += 1
                self.logger.warning(f"USB DMX read error {consecutive_errors}/{max_consecutive_errors}: {e}")
                if consecutive_errors >= max_consecutive_errors:
                    self.logger.error(f"Too many consecutive read errors, stopping worker")
                    raise ConnectionError(f"Too many consecutive read errors: {e}")
                else:
                    # Short pause before retry
                    time.sleep(0.05)

    def _disconnect_device(self) -> None:
        """Disconnect from the USB DMX device."""
        if self.device:
            try:
                self.device.close()
                self.logger.debug("USB DMX device closed")
            except Exception as e:
                self.logger.warning(f"Error closing USB DMX device: {e}")
            finally:
                self.device = None

    def stop(self) -> None:
        """Stop the USB DMX worker."""
        self.stop_event.set()
        
        # Close device to interrupt any blocking read operations
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass  # Ignore errors during shutdown

    def get_connection_info(self) -> dict:
        """Get detailed connection information."""
        return {
            'type': 'USB DMX',
            'device_id': self.device_id,
            'baudrate': self.baudrate,
            'line_property': self.line_property,
            'status': self.status.value
        }

    @staticmethod
    def list_available_devices() -> list[str]:
        """List all available FTDI devices."""
        try:
            from pylibftdi import Driver
            return [":".join(dev) for dev in Driver().list_devices()]
        except Exception as e:
            logger = get_logger("dmx.usb")
            logger.error(f"Error listing FTDI devices: {e}")
            return []