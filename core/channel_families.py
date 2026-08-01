# ABOUTME: What a DMX channel is, in one word and one colour, derived entirely
# ABOUTME: from its OFL capabilities. Shared by the fixture browser and the timeline.
"""Channel families.

A channel's *family* is the one-word answer to "what does this do?" — Intensity,
Color, Pan, Gobo — derived from the capability covering most of its DMX range.
It reads as a chip in the fixture browser's Channels tab, as the fill colour of
that channel's fader in the Scenes tab, and as the label + swatch of a
channel-control clip in the timeline, so all three agree by construction.

Qt-free on purpose: `note_layout` reads it, and that is imported by the software
controller as well as the UI.
"""

from typing import Dict, Tuple

from core.fixture_library import dominant_capability

# OFL capability type -> (chip label, colour). The label is a *family*: a
# "Pan/Tilt speed" channel reads as Speed, "Gobo rotation" as Gobo. Anything
# absent falls back to GENERIC_FAMILY, so an unknown capability still gets a
# chip instead of an empty cell.
CHANNEL_FAMILIES: Dict[str, Tuple[str, str]] = {
    "Intensity": ("Intensity", "#d9a13b"),
    "ColorIntensity": ("Color", "#d95fa0"),
    "ColorPreset": ("Color", "#d95fa0"),
    "ColorTemperature": ("Color", "#d95fa0"),
    "Pan": ("Pan", "#5b8fd9"),
    "Tilt": ("Tilt", "#5b8fd9"),
    "PanTiltSpeed": ("Speed", "#8f8f8f"),
    "ShutterStrobe": ("Shutter", "#7fa8c9"),
    "StrobeSpeed": ("Shutter", "#7fa8c9"),
    "StrobeDuration": ("Shutter", "#7fa8c9"),
    "Effect": ("Effect", "#c08a4f"),
    "EffectParameter": ("Effect", "#c08a4f"),
    "EffectSpeed": ("Speed", "#8f8f8f"),
    "EffectDuration": ("Speed", "#8f8f8f"),
    "Speed": ("Speed", "#8f8f8f"),
    "Time": ("Speed", "#8f8f8f"),
    "Rotation": ("Speed", "#8f8f8f"),
    "SoundSensitivity": ("Sound", "#8f8f8f"),
    "Focus": ("Focus", "#5fae72"),
    "Zoom": ("Zoom", "#4fb0c0"),
    "Iris": ("Iris", "#4fb0c0"),
    "Frost": ("Frost", "#4fb0c0"),
    "BeamAngle": ("Beam", "#4fb0c0"),
    "BeamPosition": ("Beam", "#4fb0c0"),
    "Prism": ("Prism", "#9b7fd4"),
    "PrismRotation": ("Prism", "#9b7fd4"),
    "BladeInsertion": ("Blade", "#9b7fd4"),
    "BladeRotation": ("Blade", "#9b7fd4"),
    "BladeSystemRotation": ("Blade", "#9b7fd4"),
    "Fog": ("Fog", "#7f9ac0"),
    "FogOutput": ("Fog", "#7f9ac0"),
    "FogType": ("Fog", "#7f9ac0"),
    "Maintenance": ("Maintenance", "#c9713b"),
    "NoFunction": ("Unused", "#6a6a6a"),
}
GENERIC_FAMILY = ("Generic", "#8f8f8f")
UNUSED_FAMILY = ("—", "#5f5f5f")

# The four wheel capability types serve colour wheels and gobo wheels alike; OFL
# tells them apart only by the wheel's name (which defaults to the channel's).
_WHEEL_TYPES = frozenset({"WheelSlot", "WheelShake", "WheelSlotRotation",
                          "WheelRotation"})

# Swatch per OFL mixable colour, muted to sit on the timeline's dark track.
# Red/Green/Blue and the two whites are the exact hexes the role-based palette
# used, so existing timelines keep their familiar colours; the rest follow the
# same v -> v*119/255 + 51 muting of their REFERENCE_RGB.
COLOR_SWATCH: Dict[str, str] = {
    "Red": "#aa3333",
    "Green": "#33aa33",
    "Blue": "#3333aa",
    "Cyan": "#33aaaa",
    "Magenta": "#aa33aa",
    "Yellow": "#aaaa33",
    "Amber": "#aa6e33",
    "White": "#9fb6c7",
    "Warm White": "#c7a037",
    "Cold White": "#9fb6c7",
    "UV": "#5b339a",
    "Lime": "#8caa33",
    "Indigo": "#563370",
}


def channel_family(spec) -> Tuple[str, str]:
    """The (label, colour) family of one resolved channel."""
    cap = dominant_capability(spec.capabilities)
    if cap is None:
        return UNUSED_FAMILY
    if cap.type in _WHEEL_TYPES:
        wheel = str(cap.get("wheel") or spec.label).lower()
        if "gobo" in wheel:
            return ("Gobo", "#5fae72")
        if "color" in wheel or "colour" in wheel:
            return ("Color", "#d95fa0")
        return ("Wheel", "#8f8f8f")
    return CHANNEL_FAMILIES.get(cap.type, GENERIC_FAMILY)


def describe_channel(spec) -> Tuple[str, str]:
    """The (label, colour) to show for one resolved channel.

    A colour channel names its colour — "Red", "Amber", "Warm White" — since a
    strip of faders or clips that all read "Color" would be unusable; everything
    else reads as its family. A sidecar `label` wins over both, which is the one
    thing a fixture author can't express in OFL: `Generic` capabilities with a
    comment ("Forward"/"Reverse") describe a direction channel that no
    capability type names.
    """
    if spec.display_name:
        return spec.display_name, channel_family(spec)[1]

    label, color = channel_family(spec)
    cap = dominant_capability(spec.capabilities)
    if cap is not None and cap.type == "ColorIntensity":
        colour_name = str(cap.get("color", ""))
        if colour_name:
            label = colour_name
            color = COLOR_SWATCH.get(colour_name, color)

    # A fine byte carries the low bits of the slot at `fine_of`; saying so beats
    # showing a second channel with the same name as its coarse half.
    if spec.fine_of >= 0:
        label = f"{label} fine"
    return label, color
