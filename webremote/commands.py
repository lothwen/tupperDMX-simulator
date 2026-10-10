# ABOUTME: Pure (Qt-free) web-remote command parsing: whitelist of remote
# ABOUTME: actions and normalization of raw client messages into command dicts.
import json
import math
from typing import Any, Optional

from core.tempo_map import MAX_BPM, METERS, MIN_BPM

ACTIONS = frozenset({
    "play_pause", "stop", "next", "prev", "seek", "cue", "select_song",
    "blackout", "panic", "free_run", "free_run_stop", "open_show",
})

MAX_SHOW_NAME = 200


def parse_command(raw: Any) -> Optional[dict]:
    """Normalize a raw client message (JSON text or dict) into a command dict
    {'action': str, 'time': float?, 'index': int?}, or None if it is not a
    valid command. Seek clamping to song duration happens at execution time,
    where the duration is known."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(raw, dict) or raw.get("type") != "cmd":
        return None

    action = raw.get("action")
    if action not in ACTIONS:
        return None

    cmd = {"action": action}

    if action == "seek":
        time_value = raw.get("time")
        if isinstance(time_value, bool) or not isinstance(time_value, (int, float)):
            return None
        time_value = float(time_value)
        if not math.isfinite(time_value) or time_value < 0:
            return None
        cmd["time"] = time_value

    if action in ("cue", "select_song"):
        index = raw.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return None
        cmd["index"] = index

    if action == "free_run":
        # Start the click on its own, or retime the one running. The page
        # never leaves the app's tempo range, so anything outside it is not a
        # tempo to clamp but a message to drop.
        bpm = raw.get("bpm")
        if isinstance(bpm, bool) or not isinstance(bpm, int) \
                or not MIN_BPM <= bpm <= MAX_BPM:
            return None
        meter = raw.get("meter")
        if meter not in METERS:
            return None
        cmd["bpm"] = bpm
        cmd["meter"] = meter

    if action == "open_show":
        # A show's display name, never a path: it is looked up in the published
        # listing at execution time, so it can only ever name a folder the
        # remote was already shown.
        name = raw.get("name")
        if not isinstance(name, str) or not name or len(name) > MAX_SHOW_NAME:
            return None
        cmd["name"] = name

    return cmd
