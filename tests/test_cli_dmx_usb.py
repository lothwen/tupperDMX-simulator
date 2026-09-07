"""
Characterization test: the --list-dmx-usb / --dmx-usb command line.

Headless has no Settings dialog, so the command line is the only way to point a
rack box at a USB DMX dongle. Covers what `--dmx-usb <spec>` resolves to, the
one benign miss (`auto` with nothing plugged in) against the one that is a
typo, that the pick reaches the routing without ever reaching userprefs.json,
and that the device list is ordered — an index is meaningless otherwise.

Run with:
    uv run python tests/test_cli_dmx_usb.py
"""

import sys
import os
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main as launcher
from core.config import Config
from tupperlogger import get_logger

DEVICES = ["FTDI:DMX USB PRO:EN123456", "FTDI:FT232R USB UART:A50285BI"]


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


@contextmanager
def _detected(devices):
    """Pretend these dongles are plugged in, for the length of the block."""
    original = launcher._usb_dmx_devices
    launcher._usb_dmx_devices = lambda: list(devices)
    try:
        yield
    finally:
        launcher._usb_dmx_devices = original


def test_resolve_picks_by_index():
    print("test_resolve_picks_by_index")
    for index, expected in enumerate(DEVICES):
        device, error = launcher._resolve_dmx_usb(str(index), DEVICES)
        _assert(error is None and device == expected,
                f"--dmx-usb {index} -> {expected}")

    device, error = launcher._resolve_dmx_usb("auto", DEVICES)
    _assert(error is None and device == DEVICES[0],
            "auto takes the first device listed")


def test_auto_without_hardware_is_not_an_error():
    print("test_auto_without_hardware_is_not_an_error")
    device, error = launcher._resolve_dmx_usb("auto", [])
    _assert(device is None and error is None,
            "auto with nothing plugged in is a benign miss, not a failure")


def test_a_spec_naming_nothing_is_an_error():
    print("test_a_spec_naming_nothing_is_an_error")
    for spec, devices in (("2", DEVICES), ("0", []), ("-1", DEVICES),
                          ("first", DEVICES), ("", DEVICES)):
        device, error = launcher._resolve_dmx_usb(spec, devices)
        _assert(device is None and error,
                f"--dmx-usb {spec!r} against {len(devices)} devices is refused")


def test_apply_drives_the_routing_but_not_the_prefs():
    print("test_apply_drives_the_routing_but_not_the_prefs")
    logger = get_logger("test.cli")
    config = Config()
    config.io.dmx_to_usb = False
    config.io.usb_out_device = "FTDI:Gone:OLD"   # what userprefs remembers

    with _detected(DEVICES):
        _assert(launcher._apply_dmx_usb(config, "1", logger) is True,
                "an index that exists applies")
    io = config.effective_io()
    _assert(io.dmx_to_usb and io.usb_out_device == DEVICES[1],
            "the routing that gets applied is the device the flag named")
    _assert(config.io.usb_out_device == "FTDI:Gone:OLD" and not config.io.dmx_to_usb,
            "config.io stays the preferences' view")
    _assert(config._userprefs_to_dict()["io"]["usb_out_device"] == "FTDI:Gone:OLD",
            "and a save writes the preference, never the run's pick")

    # An unrelated live prefs reload replaces config.io wholesale; the pick is
    # applied on top at every apply, so it cannot be dropped by one.
    config.io.midi_to_port = True
    _assert(config.effective_io().usb_out_device == DEVICES[1],
            "the pick survives config.io being rewritten underneath it")


def test_auto_owns_the_usb_leg():
    print("test_auto_owns_the_usb_leg")
    logger = get_logger("test.cli")
    config = Config()
    config.io.dmx_to_usb = True
    config.io.usb_out_device = "FTDI:Gone:OLD"

    with _detected([]):
        _assert(launcher._apply_dmx_usb(config, "auto", logger) is True,
                "auto with no hardware still boots the app")
    io = config.effective_io()
    _assert(not io.dmx_to_usb and io.usb_out_device is None,
            "and turns the USB output off rather than using a remembered device")

    with _detected([]):
        _assert(launcher._apply_dmx_usb(config, "3", logger) is False,
                "an explicit index with no hardware stops the launch")


def test_device_list_is_ordered():
    print("test_device_list_is_ordered")
    from dmx import usb_worker

    class _Port:
        def __init__(self, serial_number, product):
            self.vid = usb_worker.FTDI_VID
            self.serial_number = serial_number
            self.product = product
            self.description = product
            self.manufacturer = "FTDI"

    original = usb_worker.list_ports.comports
    # comports() globs /dev and returns it in directory order: the same two
    # dongles can come back either way round between boots.
    usb_worker.list_ports.comports = lambda: [
        _Port("A50285BI", "FT232R USB UART"), _Port("EN123456", "DMX USB PRO")]
    try:
        first = usb_worker.USBDMXWorker.list_available_devices()
        usb_worker.list_ports.comports = lambda: [
            _Port("EN123456", "DMX USB PRO"), _Port("A50285BI", "FT232R USB UART")]
        second = usb_worker.USBDMXWorker.list_available_devices()
    finally:
        usb_worker.list_ports.comports = original

    _assert(first == second == DEVICES,
            "the same devices list in the same order whatever /dev yields")


def main():
    test_resolve_picks_by_index()
    test_auto_without_hardware_is_not_an_error()
    test_a_spec_naming_nothing_is_an_error()
    test_apply_drives_the_routing_but_not_the_prefs()
    test_auto_owns_the_usb_leg()
    test_device_list_is_ordered()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
