"""
Characterization test: the redesigned SettingsDialog Input/Output page.

Covers the IOConfig round-trip through the rebuilt controls (incl. the
persisted-but-absent device "(not connected)" path), the internal-controller
power toggle greying its body, the two universe spinboxes staying in lockstep,
the live validation strip / flow-graph state, and that a hard routing error
blocks accept.

Offscreen Qt + a shared QApplication come from conftest.py (the `qapp` fixture).

Run with:
    uv run python tests/test_settings_dialog_ui.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.config import Config, IOConfig
from core import io_routing
from core.io_routing import NODE_STAGE, STATUS_ACTIVE, STATUS_DEAD


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _dialog(io=None):
    from ui.settings_dialog import SettingsDialog
    cfg = Config()
    if io is not None:
        cfg.io = io
    return SettingsDialog(cfg)


def test_io_roundtrip(qapp):
    print("test_io_roundtrip")
    # A non-default routing whose device picks are absent from the enumerated
    # lists exercises the "(not connected)" append path in _populate_combo.
    io = IOConfig(
        midi_to_controller=True, midi_to_port=True, midi_port="Ghost Port",
        controller_enabled=True, dmx_to_stage=True, dmx_to_artnet=True,
        dmx_to_usb=True, usb_out_device="FTDI:Ghost:OUT",
        stage_source="usb", stage_usb_device="FTDI:Ghost:IN")
    dlg = _dialog()
    dlg._widgets_from_io(io)
    _assert(dlg._io_from_widgets() == io, "widgets round-trip a non-default IOConfig")


def test_controller_toggle_greys_body(qapp):
    print("test_controller_toggle_greys_body")
    dlg = _dialog()
    _assert(dlg.controller_body.isEnabled(), "controller body starts enabled")

    dlg.controller_toggle.setChecked(False)
    _assert(not dlg.controller_body.isEnabled(), "toggle off greys the body")
    _assert(dlg._io_from_widgets().controller_enabled is False,
            "toggle off -> controller_enabled False")
    _assert(dlg.controller_state_label.text() == "Disabled", "state label reads Disabled")
    _assert(dlg.preset_combo.currentText() == io_routing.PRESET_CUSTOM,
            "manual toggle flips preset to Custom")


def test_universe_spins_stay_synced(qapp):
    print("test_universe_spins_stay_synced")
    dlg = _dialog()
    dlg.universe_spin.setValue(7)
    _assert(dlg.stage_universe_spin.value() == 7, "stage spin follows controller spin")
    dlg.stage_universe_spin.setValue(12)
    _assert(dlg.universe_spin.value() == 12, "controller spin follows stage spin")


def test_flow_and_validation_strip(qapp):
    print("test_flow_and_validation_strip")
    dlg = _dialog()
    _assert(dlg.validation_label.property("state") == "ok",
            "defaults validate clean (ok)")
    _assert(dlg.flow_widget._state.nodes[NODE_STAGE] == STATUS_ACTIVE,
            "defaults -> stage node active in the graph")

    # Blackout: disable the controller while the stage still points at it.
    dlg.controller_toggle.setChecked(False)
    _assert(dlg.validation_label.property("state") == "warn",
            "blackout config -> warn strip")
    _assert(dlg.flow_widget._state.nodes[NODE_STAGE] == STATUS_DEAD,
            "blackout config -> stage node dead in the graph")


def test_usb_conflict_is_error(qapp):
    print("test_usb_conflict_is_error")
    io = IOConfig(dmx_to_usb=True, usb_out_device="FTDI:X:1",
                  stage_source="usb", stage_usb_device="FTDI:X:1")
    dlg = _dialog(io)
    _assert(dlg.validation_label.property("state") == "error",
            "same-device USB in/out -> error strip")


def test_accept_blocked_on_hard_error(qapp, monkeypatch=None):
    print("test_accept_blocked_on_hard_error")
    from ui import settings_dialog
    io = IOConfig(dmx_to_usb=True, usb_out_device="FTDI:X:1",
                  stage_source="usb", stage_usb_device="FTDI:X:1")
    dlg = _dialog(io)

    calls = {"warn": 0}
    orig = settings_dialog.QMessageBox.warning
    settings_dialog.QMessageBox.warning = staticmethod(
        lambda *a, **k: calls.__setitem__("warn", calls["warn"] + 1))
    try:
        dlg._on_accept()
    finally:
        settings_dialog.QMessageBox.warning = orig
    _assert(calls["warn"] == 1, "hard error pops a warning box")
    _assert(dlg.result() == 0, "dialog is not accepted on a hard error")


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_io_roundtrip(app)
    test_controller_toggle_greys_body(app)
    test_universe_spins_stay_synced(app)
    test_flow_and_validation_strip(app)
    test_usb_conflict_is_error(app)
    test_accept_blocked_on_hard_error(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
