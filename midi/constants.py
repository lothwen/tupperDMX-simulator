"""Shared constants and helpers for the MIDI manager subsystem.

These are imported (and re-exported) by ``midi/manager.py`` so existing
``from midi.manager import CHANNEL_NOTE_MAP`` / ``MIN_EVENT_DURATION`` /
``BLACKOUT_EPSILON`` imports keep resolving, and shared by the MidiDocument /
editor collaborators.
"""

from typing import Tuple

# Identification of BLACKOUT events is by part-name PREFIX (case-insensitive),
# not by note number — keeps it config-driven and lets each fixture have its
# own gap marker (BLACKOUT for ledbar, BLACKOUT_BLINDERS for blinders, ...).
BLACKOUT_PART_NAME = "BLACKOUT"

# Minimum non-BLACKOUT clip duration in seconds. Below this, an event is too
# short to remain a valid clip and gets clamped during edits.
MIN_EVENT_DURATION = 0.01

# A BLACKOUT whose effective duration (distance to its next event) is below
# this threshold gets removed by reconcile_blackouts().
BLACKOUT_EPSILON = 1e-4

# Tolerance used when matching a midi_notes entry to its midi_events parent
# by (note_number, time). Must be larger than BLACKOUT_EPSILON to survive
# tiny drift from float arithmetic during a series of edits.
NOTE_MATCH_TOLERANCE = 0.005

# Fade-generation constants (scene groups only). A "fade" is a chain of
# brightness (note 1) note_on events at FADE_STEP_SECONDS apart whose
# velocities ramp linearly between 0 and the group's brightness V.
# Fade notes live ONLY in midi_notes (never in midi_events) so they don't
# pollute scene clustering, timeline rendering, or per-clip mutation logic.
# They round-trip through the saved MIDI file and are detected on load by
# the consecutive-step-with-monotonic-velocity-and-zero-endpoint pattern.
FADE_STEP_SECONDS = 0.10
FADE_NOTE_DURATION = 0.05  # < step so consecutive note_ons don't overlap
FADE_MIN_DURATION = 0.01   # below this, treat as no fade
# Brightness channel-control note number — the fade's target channel.
FADE_BRIGHTNESS_NOTE = 1
# Velocity tolerance when matching a parsed midi_notes entry to a detected
# fade-run member; absorbs the /2 quantization round-trip in MIDI export.
FADE_VELOCITY_MATCH_TOLERANCE = 2

# LED-bar channel-control mapping used by scene clips. Each tuple is
# (json_channel_name, midi_note_number) and matches the cases in
# software_controller/main.py:handle_note_on (notes 1-7).
CHANNEL_NOTE_MAP: Tuple[Tuple[str, int], ...] = (
    ("brightness", 1),
    ("blue", 2),
    ("green", 3),
    ("red", 4),
    ("animation", 5),
    ("speed", 6),
    ("direction", 7),
)
CHANNEL_CONTROL_NOTES = frozenset(n for _, n in CHANNEL_NOTE_MAP)

# Notes whose velocity carries a value rather than gating a note on/off:
# the generalized note map's channel sets (1-23) and scene loaders (24-35).
# A velocity of 0 on these is a real value ("set channel to 0" / "instant
# blend"), so the codec must admit them instead of treating velocity 0 as a
# note_off. (Superset of CHANNEL_CONTROL_NOTES, which stays scoped to the 7
# LED scene channels used by the scene-clustering heuristic.)
VALUE_BEARING_NOTES = frozenset(range(1, 36))


def _channel_to_velocity(value: int) -> int:
    """Map a 0..255 DMX channel value to a 0..127 MIDI velocity (floor).

    The software controller doubles velocity to recover the channel value, so
    this conversion is intentionally lossy at odd inputs (255 → 127 → 254).
    """
    try:
        v = int(value) // 2
    except (TypeError, ValueError):
        v = 0
    return max(1, min(127, v))
