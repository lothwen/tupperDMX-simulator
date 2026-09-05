"""Shared constants and helpers for the MIDI manager subsystem.

These are imported (and re-exported) by ``midi/manager.py`` so existing
``from midi.manager import MIN_EVENT_DURATION`` / ``BLACKOUT_EPSILON`` imports
keep resolving, and shared by the MidiDocument / editor collaborators.
"""

from core import note_layout

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
# note_on events at FADE_STEP_SECONDS apart whose velocities ramp linearly
# between 0 and the group's level V, riding the fixture's OWN dimmer — there
# is deliberately no constant for the target note, since which note dims a
# fixture is the fixture's answer (`FadeEngine.fade_note`), not a fixed 1.
# Fade notes live ONLY in midi_notes (never in midi_events) so they don't
# pollute scene clustering, timeline rendering, or per-clip mutation logic.
# They round-trip through the saved MIDI file and are detected on load by
# the consecutive-step-with-monotonic-velocity-and-zero-endpoint pattern.
FADE_STEP_SECONDS = 0.10
FADE_NOTE_DURATION = 0.05  # < step so consecutive note_ons don't overlap
FADE_MIN_DURATION = 0.01   # below this, treat as no fade
# A fade-out's last note stops this far SHORT of the clip's right edge. The
# codec sorts ramp notes after originals sharing a timestamp — which is what
# lets a fade-in's zero beat its own clip's value at the very same moment — so
# a fade-out landing exactly ON the boundary would overwrite the value the NEXT
# clip just set and black the fixture out precisely when it should light.
FADE_OUT_END_GAP = 0.002
# Shortest run load-time detection will call a fade. Two notes 100 ms apart
# with a near-zero end is also what two ordinary channel clips look like, and
# detection CONSUMES what it matches, so the bar sits at three: our own files
# recover their durations from the tdmx1 metas either way, and only a foreign
# file's sub-200 ms fade goes unrecognised.
FADE_MIN_RUN_NOTES = 3
# Velocity tolerance when matching a parsed midi_notes entry to a detected
# fade-run member; absorbs the /2 quantization round-trip in MIDI export.
FADE_VELOCITY_MATCH_TOLERANCE = 2

# Fixture type whose def supplies the channel<->note mapping when a clip can't
# be resolved to a patched fixture (channel-less legacy/synthetic events, an
# out-of-range channel, or tests run without a patch). Its layout is read from
# the data-driven library def — there is no hardcoded channel list.
DEFAULT_FIXTURE_TYPE = "ledbar"

# Which notes carry a value in velocity is a property of the NOTE PROTOCOL, so
# it is defined once in core.note_layout and re-exported here for the codec and
# for `from midi.manager import ...` callers. The timeline asks note_layout
# directly — it never imports this subsystem.
VALUE_BEARING_NOTES = note_layout.VALUE_BEARING_NOTES

# Song-part marker notes (INTRO..END). These are GLOBAL verbs: the software
# controller dispatches them channel-agnostically (they reset every fixture —
# see software_controller/main.py handle_note_on, `61 <= pitch <= 69`), so the
# timeline shows them on every fixture track rather than scoping by channel.
SONG_PART_NOTES = frozenset(range(61, 70))


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
