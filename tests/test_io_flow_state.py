"""
Characterization test: io_routing.flow_state pure graph-state derivation.

Covers the all-software default, the two blackout scenarios (controller
disabled / not routed to stage), the External-rig-monitor listen path, the USB
same-device conflict, subtitle formatting, and the invariant that every canonical
preset produces a graph with no DEAD element.

Run with:
    uv run python tests/test_io_flow_state.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import io_routing as ior
from core.io_routing import (STATUS_ACTIVE, STATUS_DIM, STATUS_DEAD,
                        NODE_SEQ, NODE_MIDI_PORT, NODE_CTRL, NODE_ARTNET,
                        NODE_USB, NODE_STAGE, EDGE_SEQ_CTRL, EDGE_SEQ_PORT,
                        EDGE_CTRL_STAGE, EDGE_CTRL_ARTNET, EDGE_CTRL_USB,
                        EDGE_ARTNET_STAGE, EDGE_USB_STAGE)
from core.config import IOConfig


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_all_software_defaults():
    print("test_all_software_defaults")
    s = ior.flow_state(IOConfig())
    _assert(s.nodes[NODE_SEQ] == STATUS_ACTIVE, "seq node active")
    _assert(s.nodes[NODE_CTRL] == STATUS_ACTIVE, "ctrl node active")
    _assert(s.nodes[NODE_STAGE] == STATUS_ACTIVE, "stage node active")
    _assert(s.nodes[NODE_MIDI_PORT] == STATUS_DIM, "midi port node dim")
    _assert(s.nodes[NODE_ARTNET] == STATUS_DIM, "artnet node dim")
    _assert(s.nodes[NODE_USB] == STATUS_DIM, "usb node dim")
    _assert(s.edges[EDGE_SEQ_CTRL] == STATUS_ACTIVE, "seq->ctrl edge active")
    _assert(s.edges[EDGE_CTRL_STAGE] == STATUS_ACTIVE, "ctrl->stage edge active")
    _assert(s.edges[EDGE_SEQ_PORT] == STATUS_DIM, "seq->port edge dim")
    _assert(s.edges[EDGE_CTRL_ARTNET] == STATUS_DIM, "ctrl->artnet edge dim")
    _assert(s.edges[EDGE_CTRL_USB] == STATUS_DIM, "ctrl->usb edge dim")


def test_dead_midi_into_disabled_controller():
    print("test_dead_midi_into_disabled_controller")
    s = ior.flow_state(IOConfig(controller_enabled=False))
    _assert(s.edges[EDGE_SEQ_CTRL] == STATUS_DEAD, "midi into disabled ctrl is dead")
    _assert(s.nodes[NODE_CTRL] == STATUS_DEAD, "disabled ctrl receiving midi is dead")
    _assert(s.nodes[NODE_STAGE] == STATUS_DEAD, "stage from dark ctrl is dead")
    _assert(s.edges[EDGE_CTRL_STAGE] == STATUS_DEAD, "ctrl->stage edge is dead")
    _assert("no signal" in s.subtitles[NODE_STAGE], "stage subtitle is 'no signal'")
    _assert(s.subtitles[NODE_CTRL] == "disabled", "ctrl subtitle is 'disabled'")

    # Controller disabled but nobody routes MIDI to it -> not a dead route,
    # just an idle (dim) controller. Stage still dark though.
    s = ior.flow_state(IOConfig(controller_enabled=False, midi_to_controller=False))
    _assert(s.edges[EDGE_SEQ_CTRL] == STATUS_DIM, "no midi to disabled ctrl is dim")
    _assert(s.nodes[NODE_CTRL] == STATUS_DIM, "unfed disabled ctrl is dim")


def test_blackout_when_not_routed_to_stage():
    print("test_blackout_when_not_routed_to_stage")
    s = ior.flow_state(IOConfig(dmx_to_stage=False))
    _assert(s.nodes[NODE_STAGE] == STATUS_DEAD, "stage dark without dmx_to_stage")
    _assert(s.edges[EDGE_CTRL_STAGE] == STATUS_DEAD, "ctrl->stage dead without route")

    # Pointing the stage at Art-Net clears the blackout.
    s = ior.flow_state(IOConfig(dmx_to_stage=False, stage_source="artnet"))
    _assert(s.nodes[NODE_STAGE] == STATUS_ACTIVE, "artnet-fed stage is active")
    _assert(s.edges[EDGE_ARTNET_STAGE] == STATUS_ACTIVE, "artnet->stage active")
    _assert(s.edges[EDGE_CTRL_STAGE] == STATUS_DIM, "ctrl->stage idle for artnet input")


def test_monitor_preset():
    print("test_monitor_preset")
    io = ior.apply_preset("External rig monitor", IOConfig())
    s = ior.flow_state(io)
    _assert(s.nodes[NODE_ARTNET] == STATUS_ACTIVE, "artnet active via listen")
    _assert(s.edges[EDGE_ARTNET_STAGE] == STATUS_ACTIVE, "artnet->stage active")
    _assert(s.nodes[NODE_CTRL] == STATUS_DIM, "disabled ctrl is dim (no midi fed)")
    _assert(STATUS_DEAD not in s.nodes.values(), "monitor has no dead nodes")
    _assert(STATUS_DEAD not in s.edges.values(), "monitor has no dead edges")


def test_usb_conflict_is_dead():
    print("test_usb_conflict_is_dead")
    io = IOConfig(dmx_to_usb=True, usb_out_device="FTDI:X:1",
                  stage_source="usb", stage_usb_device="FTDI:X:1")
    s = ior.flow_state(io)
    _assert(s.nodes[NODE_USB] == STATUS_DEAD, "same-device usb node is dead")
    _assert(s.edges[EDGE_USB_STAGE] == STATUS_DEAD, "same-device usb->stage is dead")

    io.stage_usb_device = "FTDI:X:2"
    s = ior.flow_state(io)
    _assert(s.nodes[NODE_USB] == STATUS_ACTIVE, "different devices -> usb active")
    _assert(s.edges[EDGE_USB_STAGE] == STATUS_ACTIVE, "different devices -> edge active")


def test_subtitles():
    print("test_subtitles")
    s = ior.flow_state(IOConfig(midi_to_port=True, midi_port="Arduino Leonardo"))
    _assert(s.subtitles[NODE_MIDI_PORT] == "Arduino Leonardo", "port name passed through")

    s = ior.flow_state(IOConfig(midi_to_port=True))
    _assert(s.subtitles[NODE_MIDI_PORT] == "no port selected", "missing port fallback")

    s = ior.flow_state(IOConfig(), artnet_ip="192.168.1.50", artnet_universe=3)
    _assert(s.subtitles[NODE_ARTNET] == "192.168.1.50 · U3", "artnet subtitle formatting")

    s = ior.flow_state(IOConfig(dmx_to_usb=True, usb_out_device="FTDI:FT232R:X1"))
    _assert(s.subtitles[NODE_USB] == "FT232R", "FTDI device label shortened")

    s = ior.flow_state(IOConfig(dmx_to_usb=True))
    _assert(s.subtitles[NODE_USB] == "no device", "missing usb device fallback")


def test_presets_have_no_dead_elements():
    print("test_presets_have_no_dead_elements")
    for name in ior.PRESET_NAMES:
        if name == ior.PRESET_CUSTOM:
            continue
        io = ior.apply_preset(name, IOConfig(midi_port="P",
                                             usb_out_device="FTDI:X:1",
                                             stage_usb_device="FTDI:X:2"))
        s = ior.flow_state(io)
        _assert(STATUS_DEAD not in s.nodes.values(),
                f"preset '{name}' has no dead nodes")
        _assert(STATUS_DEAD not in s.edges.values(),
                f"preset '{name}' has no dead edges")


def main():
    test_all_software_defaults()
    test_dead_midi_into_disabled_controller()
    test_blackout_when_not_routed_to_stage()
    test_monitor_preset()
    test_usb_conflict_is_dead()
    test_subtitles()
    test_presets_have_no_dead_elements()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
