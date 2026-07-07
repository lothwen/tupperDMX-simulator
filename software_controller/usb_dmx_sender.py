# ABOUTME: USBDMXSender — DMX512 transmitter over a plain FTDI dongle (Enttec
# ABOUTME: Open DMX style). Sink-shaped like ArtNetSender: send_dmx(data)/close().
import time

from tupperlogger import get_logger

# libftdi enum values for ftdi_set_line_property2
_BITS_8 = 8
_STOP_BIT_2 = 2
_PARITY_NONE = 0
_BREAK_ON = 1
_BREAK_OFF = 0


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
            from pylibftdi import Device
            self.device = Device(device_id, mode='b')
            self.device.baudrate = 250000
            self.device.line_property = (8, 2, 0)  # 8 data bits, 2 stop, no parity
            self.logger.info(f"USB DMX sender opened: {device_string}")
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
            set_line = self.device.ftdi_fn.ftdi_set_line_property2
            # Break (>88us) then mark-after-break (>8us).
            set_line(_BITS_8, _STOP_BIT_2, _PARITY_NONE, _BREAK_ON)
            time.sleep(0.0001)
            set_line(_BITS_8, _STOP_BIT_2, _PARITY_NONE, _BREAK_OFF)
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
