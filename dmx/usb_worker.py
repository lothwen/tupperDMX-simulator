import threading
import time
from typing import Optional

import serial
from serial.tools import list_ports

from tupperlogger import get_logger
from .base import DMXWorkerBase, DMXConnectionStatus

# FTDI USB vendor ID; all supported DMX dongles are FTDI-based.
FTDI_VID = 0x0403

# The DMX512 line rate. Deliberately NOT a POSIX standard baud rate, which is
# what makes open_dmx_port() below necessary.
DMX_BAUDRATE = 250000


def find_port_by_serial(serial_number: str) -> Optional[str]:
    """Resolve an FTDI serial number to its serial port (COMx / /dev/ttyUSB*)."""
    for port in list_ports.comports():
        if port.vid == FTDI_VID and port.serial_number == serial_number:
            return port.device
    return None


def open_dmx_port(port: str, **kwargs) -> serial.Serial:
    """Open `port` with DMX512 line settings: 250000 baud, 8 bits, 2 stop, no parity.

    250000 has no `termios.B*` constant, so on Linux pyserial takes its
    custom-baudrate path — and that path hands `BOTHER` to `termios.tcsetattr`
    *before* applying the rate through the `TCSETS2` ioctl that would have
    worked. The `TCSETS` ioctl behind `tcsetattr` rejects any non-standard
    CBAUD with EINVAL, so the open dies with "(22, 'Invalid argument')" and the
    ioctl is never reached. Open at a standard rate and drive that ioctl
    ourselves instead.

    The direct open is still tried first: it is what succeeds on Windows and on
    any platform whose pyserial/libc/kernel combination handles 250000, and it
    keeps this workaround dormant once one of them does.

    NB the returned port reports the standard rate in `.baudrate`, and assigning
    that (or anything else pyserial reconfigures the port through) re-runs the
    failing `tcsetattr`. Read and write the port; do not re-configure it.
    """
    settings = dict(
        bytesize=serial.EIGHTBITS,
        stopbits=serial.STOPBITS_TWO,
        parity=serial.PARITY_NONE,
        **kwargs,
    )
    try:
        return serial.Serial(port, baudrate=DMX_BAUDRATE, **settings)
    except Exception as direct_error:
        logger = get_logger("dmx.usb")
        try:
            device = serial.Serial(port, baudrate=115200, **settings)
        except Exception:
            # Not the baud rate at all — the port is busy, gone or forbidden.
            # Report why the rate we actually want failed, not the retry.
            raise direct_error
        # pyserial's own TCSETS2 helper, private but the only public-free way to
        # reach it; absent on backends that never needed it (e.g. Windows).
        set_special_baudrate = getattr(device, "_set_special_baudrate", None)
        if set_special_baudrate is None:
            device.close()
            raise direct_error
        try:
            set_special_baudrate(DMX_BAUDRATE)
        except Exception:
            device.close()
            raise
        logger.info(
            f"{port} opened at {DMX_BAUDRATE} baud via the custom-baudrate ioctl "
            f"(direct open failed: {direct_error})"
        )
        return device


class USBDMXWorker(DMXWorkerBase):
    """Worker for receiving DMX data via USB FTDI interface."""

    # Auto-reconnect backoff schedule in seconds; the last value is the cap.
    _RECONNECT_DELAYS = (0.5, 1.0, 2.0, 5.0)

    def __init__(self, device_id: str):
        super().__init__(f"USB DMX ({device_id})")
        self.device_id = device_id
        self.stop_event = threading.Event()
        self.device: Optional[serial.Serial] = None
        self.logger = get_logger("dmx.usb")

        # USB DMX configuration
        self.baudrate = DMX_BAUDRATE
        self.line_property = (8, 2, 0)  # 8 data bits, 2 stop bits, no parity
        self.read_timeout = 0.1  # 100ms timeout for reads

    def run(self) -> None:
        """Run the worker, auto-reconnecting to the device until stopped.

        A mid-show unplug (or a device that isn't ready yet at startup) no
        longer kills the worker: the handle is closed and we retry with a
        capped backoff until the device returns or stop() is called. Reconnect
        attempts are logged and reflected in the connection status, but
        deliberately do NOT raise the modal error dialog — that would flood the
        UI on every retry, which is the opposite of what you want mid-show.
        """
        self._is_running = True
        attempt = 0
        while not self.stop_event.is_set():
            self._set_status(DMXConnectionStatus.CONNECTING)
            try:
                self._connect_device()
                attempt = 0  # reset backoff once we're connected
                self._read_loop()
            except Exception as e:
                self.logger.warning(f"USB DMX connection lost: {e}")
            finally:
                self._disconnect_device()

            if self.stop_event.is_set():
                break

            delay = self._RECONNECT_DELAYS[min(attempt, len(self._RECONNECT_DELAYS) - 1)]
            attempt += 1
            self.logger.info(
                f"USB DMX reconnecting to {self.device_id} in {delay:.1f}s "
                f"(attempt {attempt})"
            )
            self.stop_event.wait(delay)

        self._is_running = False
        self._set_status(DMXConnectionStatus.DISCONNECTED)
        self.logger.info("USB DMX worker stopped")

    def _connect_device(self) -> None:
        """Connect to the USB DMX device."""
        try:
            self.logger.info(f"Connecting to USB DMX device: {self.device_id}")
            port = find_port_by_serial(self.device_id)
            if port is None:
                raise ConnectionError(f"No FTDI serial port found for {self.device_id}")
            self.device = open_dmx_port(port, timeout=self.read_timeout)
            self.device.reset_input_buffer()

            self._set_status(DMXConnectionStatus.CONNECTED)
            self.logger.info(f"USB DMX device connected: {self.device_id} ({port})")

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
                    dmx_frame = bytearray(data)
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
        """List all available FTDI devices as "manufacturer:description:serial"."""
        try:
            return [
                f"{p.manufacturer or 'FTDI'}:{p.product or p.description}:{p.serial_number}"
                for p in list_ports.comports()
                if p.vid == FTDI_VID and p.serial_number
            ]
        except Exception as e:
            logger = get_logger("dmx.usb")
            logger.error(f"Error listing FTDI devices: {e}")
            return []
