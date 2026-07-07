"""
Characterization test: io_routing pure logic.

Covers the quick-mode presets (apply/match round-trip, Custom detection,
device-pick preservation), the validation rules (one hard, three soft), the
resulting-flow text for the canonical usage modes, and MidiRouting derivation.

Run with:
    uv run python tests/test_io_routing.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import io_routing
from config import IOConfig


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_presets_roundtrip():
    print("test_presets_roundtrip")
    for name in io_routing.PRESET_NAMES:
        if name == io_routing.PRESET_CUSTOM:
            continue
        io = io_routing.apply_preset(name, IOConfig())
        _assert(io_routing.match_preset(io) == name,
                f"preset '{name}' matches itself after apply")

    _assert(io_routing.match_preset(IOConfig()) == "All software",
            "IOConfig defaults are the All software preset")


def test_custom_detection():
    print("test_custom_detection")
    io = io_routing.apply_preset("All software", IOConfig())
    io.dmx_to_usb = True
    _assert(io_routing.match_preset(io) == io_routing.PRESET_CUSTOM,
            "manual divergence from a preset reads as Custom")
    _assert(io_routing.apply_preset(io_routing.PRESET_CUSTOM, io) == io,
            "applying Custom leaves the routing unchanged")


def test_apply_preset_preserves_picks():
    print("test_apply_preset_preserves_picks")
    io = IOConfig(midi_port="Arduino Leonardo",
                  usb_out_device="FTDI:FT232R:OUT1",
                  stage_usb_device="FTDI:FT232R:IN1")
    out = io_routing.apply_preset("External rig monitor", io)
    _assert(out.midi_port == "Arduino Leonardo", "midi port pick preserved")
    _assert(out.usb_out_device == "FTDI:FT232R:OUT1", "usb out pick preserved")
    _assert(out.stage_usb_device == "FTDI:FT232R:IN1", "stage usb pick preserved")
    _assert(out.stage_source == "artnet", "preset routing applied")


def test_validation_rules():
    print("test_validation_rules")
    # Hard: same FTDI device as controller output and stage input.
    io = IOConfig(dmx_to_usb=True, usb_out_device="FTDI:X:1",
                  stage_source="usb", stage_usb_device="FTDI:X:1")
    errors, _ = io_routing.validate(io)
    _assert(len(errors) == 1, "same-device in/out is a hard error")

    io.stage_usb_device = "FTDI:X:2"
    errors, _ = io_routing.validate(io)
    _assert(not errors, "different devices are fine")

    # Soft: stage dark (controller disabled).
    _, warnings = io_routing.validate(IOConfig(controller_enabled=False))
    _assert(any("dark" in w for w in warnings),
            "stage-input internal with controller off warns stage-dark")

    # Soft: stage dark (controller not routed to stage).
    _, warnings = io_routing.validate(IOConfig(dmx_to_stage=False))
    _assert(any("dark" in w for w in warnings),
            "stage-input internal without dmx_to_stage warns stage-dark")

    # Soft: audio-only.
    _, warnings = io_routing.validate(
        IOConfig(midi_to_controller=False, midi_to_port=False))
    _assert(any("audio-only" in w for w in warnings),
            "no MIDI outputs warns audio-only")

    # Soft: controller output goes nowhere.
    _, warnings = io_routing.validate(
        IOConfig(dmx_to_stage=False, stage_source="artnet"))
    _assert(any("nowhere" in w for w in warnings),
            "controller with no DMX outputs warns")

    # The canonical presets validate clean — except External rig monitor,
    # where the audio-only note is informative (an external player drives
    # the rig, so app playback deliberately has no MIDI outputs).
    for name in io_routing.PRESET_NAMES:
        if name == io_routing.PRESET_CUSTOM:
            continue
        io = io_routing.apply_preset(name, IOConfig(midi_port="P"))
        errors, warnings = io_routing.validate(io)
        _assert(not errors, f"preset '{name}' produces no hard errors")
        if name == "External rig monitor":
            _assert(warnings == ["No MIDI outputs selected: playback will be audio-only."],
                    "monitor preset carries only the audio-only note")
        else:
            _assert(not warnings, f"preset '{name}' produces no warnings")


def test_flow_text_modes():
    print("test_flow_text_modes")
    text = io_routing.flow_text(IOConfig())
    _assert("Timeline" in text and "Internal controller" in text
            and "Stage" in text, "all-software flow names the full chain")

    text = io_routing.flow_text(
        io_routing.apply_preset("External rig monitor", IOConfig()))
    _assert("Art-Net listen" in text, "monitor flow shows the Art-Net listener")
    _assert("Internal controller ─DMX" not in text,
            "monitor flow hides the disabled controller")

    io = io_routing.apply_preset("Sequencer for HW rig",
                                 IOConfig(midi_port="Arduino Leonardo"))
    text = io_routing.flow_text(io)
    _assert("Arduino Leonardo" in text, "dual MIDI flow names the external port")

    text = io_routing.flow_text(
        IOConfig(midi_to_controller=False, midi_to_port=False))
    _assert("(nothing)" in text, "no MIDI outputs render as (nothing)")


def test_midi_routing_from_io():
    print("test_midi_routing_from_io")
    r = io_routing.midi_routing_from_io(IOConfig())
    _assert(r.to_controller and r.port is None and r.has_output(),
            "defaults route to the controller only")

    r = io_routing.midi_routing_from_io(
        IOConfig(midi_to_port=True, midi_port="P"))
    _assert(r.port == "P", "checked port with a pick routes to it")

    r = io_routing.midi_routing_from_io(
        IOConfig(midi_to_port=True, midi_port=None))
    _assert(r.port is None, "checked port without a pick routes nowhere")

    r = io_routing.midi_routing_from_io(
        IOConfig(midi_to_controller=False, midi_to_port=False, midi_port="P"))
    _assert(not r.has_output(), "unchecked outputs -> no output")


def main():
    test_presets_roundtrip()
    test_custom_detection()
    test_apply_preset_preserves_picks()
    test_validation_rules()
    test_flow_text_modes()
    test_midi_routing_from_io()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
