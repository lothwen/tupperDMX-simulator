# ABOUTME: USBDMXSender — DMX512 transmitter over a plain FTDI dongle (Enttec
# ABOUTME: Open DMX style). Sink-shaped like ArtNetSender: send_dmx(data)/close().
import time

from tupperlogger import get_logger


class USBDMXSender:
    """Transmit DMX frames over an FTDI USB device.

    Fully defensive: any failure (open or send) logs once and self-disables so
    the controller's output loop can never be taken down by the hardware. The
    device identity is the full "<manufacturer>:<description>:<serial>" string
    from USBDMXWorker.list_available_devices(); the serial opens the device.
    """

    def __init__(self, device_string: str):
        self.logger = get_logger("software_controller.usb_dmx")
        self.device_string = device_string
        self.device = None
        self._disabled = False

        parts = device_string.split(":")
        device_id = parts[2] if len(parts) >= 3 else device_string
        try:
            import serial
            from dmx.usb_worker import find_port_by_serial
            port = find_port_by_serial(device_id)
            if port is None:
                raise ConnectionError(f"No FTDI serial port found for {device_id}")
            self.device = serial.Serial(
                port,
                baudrate=250000,
                bytesize=serial.EIGHTBITS,
                stopbits=serial.STOPBITS_TWO,
                parity=serial.PARITY_NONE,
            )
            self.logger.info(f"USB DMX sender opened: {device_string} ({port})")
        except Exception as e:
            self.logger.error(f"USB DMX sender failed to open {device_string}: {e}")
            self._disable()

    def _disable(self) -> None:
        self._disabled = True
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None

    def send_dmx(self, data) -> None:
        """Send one DMX512 frame: break, mark-after-break, start code, slots."""
        if self._disabled or not self.device:
            return
        try:
            # Break (>88us) then mark-after-break (>8us).
            self.device.break_condition = True
            time.sleep(0.0001)
            self.device.break_condition = False
            time.sleep(0.000012)
            # Null start code + up to 512 slots.
            self.device.write(bytes([0x00]) + bytes(bytearray(data[:512])))
        except Exception as e:
            self.logger.error(
                f"USB DMX send on {self.device_string} failed, disabling output: {e}")
            self._disable()

    def close(self) -> None:
        if self.device:
            try:
                self.device.close()
                self.logger.info(f"USB DMX sender closed: {self.device_string}")
            except Exception as e:
                self.logger.warning(f"Error closing USB DMX sender: {e}")
            finally:
                self.device = None
        self._disabled = True

    def __repr__(self) -> str:
        return f"USBDMXSender({self.device_string})"
