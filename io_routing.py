# ABOUTME: Pure (Qt-free) I/O routing logic: MIDI routing derivation, quick-mode
# ABOUTME: presets, validation rules and the "resulting flow" text for IOConfig.
from dataclasses import dataclass, replace
from typing import List, Optional, Tuple

from config import IOConfig


@dataclass(frozen=True)
class MidiRouting:
    """Where the sequencer's MIDI stream goes: the internal software controller
    and/or one external mido output port. Fan-out is safe (each receiver keeps
    its own state machine); fan-in is deliberately impossible."""
    to_controller: bool
    port: Optional[str]

    def has_output(self) -> bool:
        return self.to_controller or self.port is not None


def midi_routing_from_io(io: IOConfig) -> MidiRouting:
    port = io.midi_port if (io.midi_to_port and io.midi_port) else None
    return MidiRouting(to_controller=io.midi_to_controller, port=port)


# --- quick-mode presets ------------------------------------------------------

# Routing fields a preset pins down; device/port picks are preserved so
# flipping presets doesn't wipe the user's selections.
_PRESETS = {
    "All software": dict(
        midi_to_controller=True, midi_to_port=False, controller_enabled=True,
        dmx_to_stage=True, dmx_to_artnet=False, dmx_to_usb=False,
        stage_source="internal"),
    "Software + real lights": dict(
        midi_to_controller=True, midi_to_port=False, controller_enabled=True,
        dmx_to_stage=True, dmx_to_artnet=True, dmx_to_usb=False,
        stage_source="internal"),
    "External rig monitor": dict(
        midi_to_controller=False, midi_to_port=False, controller_enabled=False,
        dmx_to_stage=False, dmx_to_artnet=False, dmx_to_usb=False,
        stage_source="artnet"),
    "Sequencer for HW rig": dict(
        midi_to_controller=True, midi_to_port=True, controller_enabled=True,
        dmx_to_stage=True, dmx_to_artnet=False, dmx_to_usb=False,
        stage_source="internal"),
}

PRESET_CUSTOM = "Custom"
PRESET_NAMES = list(_PRESETS) + [PRESET_CUSTOM]


def apply_preset(name: str, io: IOConfig) -> IOConfig:
    """New IOConfig with the preset's routing applied. Device/port picks are
    kept; "Custom" (or an unknown name) returns an unchanged copy."""
    values = _PRESETS.get(name)
    if values is None:
        return replace(io)
    return replace(io, **values)


def match_preset(io: IOConfig) -> str:
    """Name of the preset this routing matches, or "Custom"."""
    for name, values in _PRESETS.items():
        if all(getattr(io, f) == v for f, v in values.items()):
            return name
    return PRESET_CUSTOM


# --- validation --------------------------------------------------------------

def validate(io: IOConfig) -> Tuple[List[str], List[str]]:
    """Routing sanity check → (hard_errors, soft_warnings).

    Hard errors block accepting the settings; soft warnings describe legal but
    probably unintended configurations."""
    errors: List[str] = []
    warnings: List[str] = []

    if (io.dmx_to_usb and io.stage_source == "usb"
            and io.usb_out_device and io.usb_out_device == io.stage_usb_device):
        errors.append(
            "The same USB DMX device cannot be both the controller output "
            "and the stage input.")

    if io.stage_source == "internal" and not (io.controller_enabled and io.dmx_to_stage):
        warnings.append(
            "The stage will stay dark: its input is the internal controller, "
            "but the controller is disabled or not routed to the stage.")

    if not io.midi_to_controller and not io.midi_to_port:
        warnings.append(
            "No MIDI outputs selected: playback will be audio-only.")

    if io.controller_enabled and not (io.dmx_to_stage or io.dmx_to_artnet or io.dmx_to_usb):
        warnings.append(
            "The internal controller has no DMX outputs: its output goes nowhere.")

    return errors, warnings


# --- flow rendering ----------------------------------------------------------

def flow_text(io: IOConfig) -> str:
    """Human-readable rendering of the active signal chain, one hop per line."""
    lines: List[str] = []

    midi_targets = []
    if io.midi_to_controller:
        midi_targets.append("Internal controller")
    if io.midi_to_port:
        midi_targets.append(io.midi_port or "MIDI port")
    lines.append("Timeline ─MIDI→ " + (" + ".join(midi_targets) if midi_targets else "(nothing)"))

    if io.controller_enabled:
        dmx_targets = []
        if io.dmx_to_stage:
            dmx_targets.append("Stage")
        if io.dmx_to_artnet:
            dmx_targets.append("Art-Net")
        if io.dmx_to_usb:
            dmx_targets.append("USB DMX")
        lines.append("Internal controller ─DMX→ "
                     + (" + ".join(dmx_targets) if dmx_targets else "(nothing)"))

    if io.stage_source == "artnet":
        lines.append("Art-Net listen ─DMX→ Stage")
    elif io.stage_source == "usb":
        lines.append("USB DMX read ─DMX→ Stage")

    return "\n".join(lines)
