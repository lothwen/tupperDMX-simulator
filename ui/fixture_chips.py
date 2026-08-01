# ABOUTME: The chip label and the OFL capability -> channel-family colour table
# ABOUTME: shared by the fixture browser's tabs.
"""Channel families, as a colour.

A channel's *family* is the one-word answer to "what does this do?" — Intensity,
Color, Pan, Gobo — derived from the capability covering most of its DMX range.
It reads as a chip in the Channels tab and as the fill colour of that channel's
fader in the Scenes tab, so both live here rather than in either tab.
"""

from typing import Dict, Tuple

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QLabel

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


def rgba(hex_color: str, alpha: int) -> str:
    """`hex_color` as a QSS rgba() string — QSS has no #RRGGBBAA literal."""
    color = QColor(hex_color)
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha})"


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


def chip(text: str, color: str) -> QLabel:
    """A small pill label in `color`."""
    label = QLabel(text)
    label.setObjectName("AddFxChip")
    label.setStyleSheet(
        f"color: {color};"
        f"border: 1px solid {rgba(color, 90)};"
        f"background-color: {rgba(color, 32)};")
    return label
