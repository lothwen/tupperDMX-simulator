# ABOUTME: Pure (Qt-free) web-remote command parsing: whitelist of remote
# ABOUTME: actions and normalization of raw client messages into command dicts.
import json
import math
from typing import Any, Optional

ACTIONS = frozenset({
    "play_pause", "stop", "next", "prev", "seek", "cue", "select_song",
    "blackout", "panic", "set_precount", "set_countin_bars",
})


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

    if action == "set_precount":
        on = raw.get("on")
        if not isinstance(on, bool):
            return None
        cmd["on"] = on

    if action == "set_countin_bars":
        bars = raw.get("bars")
        if isinstance(bars, bool) or not isinstance(bars, int) or bars < 1:
            return None
        cmd["bars"] = bars  # upper clamp happens at execution time

    return cmd
