# ABOUTME: Pure (Qt-free) I/O routing logic: MIDI routing derivation, quick-mode
# ABOUTME: presets, validation rules, the "resulting flow" text and the live
# ABOUTME: signal-flow graph state for IOConfig.
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

from core.config import IOConfig


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


# --- signal-flow graph state -------------------------------------------------

# Per-node / per-edge render status. A node/edge is ACTIVE when it carries live
# data (colored, animated), DIM when it's an unused route (grey), or DEAD when
# it's configured but broken (red — e.g. MIDI into a disabled controller, or a
# stage pointed at a dark source).
STATUS_ACTIVE = "active"
STATUS_DIM = "dim"
STATUS_DEAD = "dead"

NODE_SEQ = "seq"
NODE_MIDI_PORT = "midi_port"
NODE_CTRL = "ctrl"
NODE_ARTNET = "artnet"
NODE_USB = "usb"
NODE_STAGE = "stage"

EDGE_SEQ_CTRL = (NODE_SEQ, NODE_CTRL)          # MIDI
EDGE_SEQ_PORT = (NODE_SEQ, NODE_MIDI_PORT)     # MIDI
EDGE_CTRL_STAGE = (NODE_CTRL, NODE_STAGE)      # DMX
EDGE_CTRL_ARTNET = (NODE_CTRL, NODE_ARTNET)    # DMX
EDGE_CTRL_USB = (NODE_CTRL, NODE_USB)          # DMX
EDGE_ARTNET_STAGE = (NODE_ARTNET, NODE_STAGE)  # DMX return path
EDGE_USB_STAGE = (NODE_USB, NODE_STAGE)        # DMX return path

# Edges that carry MIDI (the widget picks the MIDI vs DMX pen colour from this).
MIDI_EDGES = (EDGE_SEQ_CTRL, EDGE_SEQ_PORT)


@dataclass(frozen=True)
class FlowState:
    """Render-ready state of the I/O signal-flow graph: a status per node and
    per edge, plus the dynamic subtitle line of every node. Pure data — the
    widget only draws it."""
    nodes: Dict[str, str]              # node id -> STATUS_*
    edges: Dict[Tuple[str, str], str]  # (src, dst) -> STATUS_*
    subtitles: Dict[str, str]          # node id -> dynamic second line


def _device_label(device: Optional[str], fallback: str = "no device") -> str:
    """Short label for a "FTDI:<desc>:<serial>" device id — the middle part
    (the human description) when present, else the raw string or a fallback."""
    if not device:
        return fallback
    parts = device.split(":")
    if len(parts) >= 3:
        return parts[1]
    return device


def flow_state(io: IOConfig, artnet_ip: str = "", artnet_universe: int = 0) -> FlowState:
    """Derive the signal-flow graph state from an IOConfig (plus the live
    Art-Net target, which lives in ArtNetConfig, not IOConfig). The dead-route
    predicates mirror validate()'s hard error / stage-dark warning."""
    # Predicates shared with validate().
    usb_conflict = (io.dmx_to_usb and io.stage_source == "usb"
                    and io.usb_out_device
                    and io.usb_out_device == io.stage_usb_device)
    stage_dark = (io.stage_source == "internal"
                  and not (io.controller_enabled and io.dmx_to_stage))

    edges: Dict[Tuple[str, str], str] = {}

    # Sequencer MIDI out.
    if io.midi_to_controller and not io.controller_enabled:
        edges[EDGE_SEQ_CTRL] = STATUS_DEAD
    elif io.midi_to_controller:
        edges[EDGE_SEQ_CTRL] = STATUS_ACTIVE
    else:
        edges[EDGE_SEQ_CTRL] = STATUS_DIM
    edges[EDGE_SEQ_PORT] = STATUS_ACTIVE if io.midi_to_port else STATUS_DIM

    # Controller DMX out. The stage edge is only wired in-process when the
    # stage input *is* the internal controller; other sources leave it idle.
    if io.stage_source == "internal":
        edges[EDGE_CTRL_STAGE] = (STATUS_ACTIVE
                                  if io.controller_enabled and io.dmx_to_stage
                                  else STATUS_DEAD)
    else:
        edges[EDGE_CTRL_STAGE] = STATUS_DIM
    edges[EDGE_CTRL_ARTNET] = (STATUS_ACTIVE
                               if io.controller_enabled and io.dmx_to_artnet
                               else STATUS_DIM)
    edges[EDGE_CTRL_USB] = (STATUS_ACTIVE
                            if io.controller_enabled and io.dmx_to_usb
                            else STATUS_DIM)

    # Stage input return paths.
    edges[EDGE_ARTNET_STAGE] = (STATUS_ACTIVE if io.stage_source == "artnet"
                                else STATUS_DIM)
    if usb_conflict:
        edges[EDGE_USB_STAGE] = STATUS_DEAD
    else:
        edges[EDGE_USB_STAGE] = (STATUS_ACTIVE if io.stage_source == "usb"
                                 else STATUS_DIM)

    nodes: Dict[str, str] = {}
    nodes[NODE_SEQ] = STATUS_ACTIVE
    nodes[NODE_MIDI_PORT] = STATUS_ACTIVE if io.midi_to_port else STATUS_DIM
    if io.midi_to_controller and not io.controller_enabled:
        nodes[NODE_CTRL] = STATUS_DEAD
    elif io.controller_enabled:
        nodes[NODE_CTRL] = STATUS_ACTIVE
    else:
        nodes[NODE_CTRL] = STATUS_DIM
    artnet_active = ((io.controller_enabled and io.dmx_to_artnet)
                     or io.stage_source == "artnet")
    nodes[NODE_ARTNET] = STATUS_ACTIVE if artnet_active else STATUS_DIM
    if usb_conflict:
        nodes[NODE_USB] = STATUS_DEAD
    else:
        usb_active = ((io.controller_enabled and io.dmx_to_usb)
                      or io.stage_source == "usb")
        nodes[NODE_USB] = STATUS_ACTIVE if usb_active else STATUS_DIM
    nodes[NODE_STAGE] = STATUS_DEAD if stage_dark else STATUS_ACTIVE

    stage_input = {
        "internal": "input: internal ctrl",
        "artnet": "input: Art-Net",
        "usb": "input: USB DMX",
    }.get(io.stage_source, "input: internal ctrl")
    subtitles = {
        NODE_SEQ: "timeline",
        NODE_MIDI_PORT: io.midi_port or "no port selected",
        NODE_CTRL: "MIDI → DMX" if io.controller_enabled else "disabled",
        NODE_ARTNET: f"{artnet_ip or '—'} · U{artnet_universe}",
        NODE_USB: _device_label(
            io.stage_usb_device if io.stage_source == "usb" else io.usb_out_device),
        NODE_STAGE: "no signal" if stage_dark else stage_input,
    }

    return FlowState(nodes=nodes, edges=edges, subtitles=subtitles)
