"""
Characterization test: web-remote command parsing.

Covers the parse_command whitelist/validation (every action, malformed JSON,
wrong type tags, bad seek times, bad cue indices) and WebRemoteWorker's
_handle_text delegation on a constructed-but-never-run worker (no thread, no
socket — same approach as test_midi_fanout.py).

Run with:
    uv run python tests/test_webremote_commands.py
"""

import sys
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from webremote.commands import parse_command, ACTIONS
from webremote.server import WebRemoteWorker


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_simple_actions():
    print("test_simple_actions")
    for action in ("play_pause", "stop", "next", "prev", "blackout", "panic"):
        cmd = parse_command({"type": "cmd", "action": action})
        _assert(cmd == {"action": action}, f"accepts '{action}'")


def test_json_text_input():
    print("test_json_text_input")
    cmd = parse_command('{"type": "cmd", "action": "stop"}')
    _assert(cmd == {"action": "stop"}, "JSON text frames are parsed")
    _assert(parse_command("not json {") is None, "malformed JSON is rejected")
    _assert(parse_command('"just a string"') is None,
            "non-object JSON is rejected")


def test_rejects_bad_envelopes():
    print("test_rejects_bad_envelopes")
    _assert(parse_command({"action": "stop"}) is None,
            "missing type tag is rejected")
    _assert(parse_command({"type": "state", "action": "stop"}) is None,
            "wrong type tag is rejected")
    _assert(parse_command({"type": "cmd", "action": "reboot"}) is None,
            "unknown action is rejected")
    _assert(parse_command({"type": "cmd"}) is None,
            "missing action is rejected")


def test_seek_validation():
    print("test_seek_validation")
    cmd = parse_command({"type": "cmd", "action": "seek", "time": 93.2})
    _assert(cmd == {"action": "seek", "time": 93.2}, "seek carries its time")
    cmd = parse_command({"type": "cmd", "action": "seek", "time": 0})
    _assert(cmd == {"action": "seek", "time": 0.0},
            "integer time is normalized to float")

    for bad in (None, "12", -1.0, float("nan"), float("inf"), True):
        raw = {"type": "cmd", "action": "seek", "time": bad}
        _assert(parse_command(raw) is None, f"seek time {bad!r} is rejected")
    _assert(parse_command({"type": "cmd", "action": "seek"}) is None,
            "seek without a time is rejected")


def test_indexed_validation():
    print("test_indexed_validation")
    for action in ("cue", "select_song"):
        cmd = parse_command({"type": "cmd", "action": action, "index": 2})
        _assert(cmd == {"action": action, "index": 2},
                f"{action} carries its index")

        for bad in (None, "2", -1, 1.5, True):
            raw = {"type": "cmd", "action": action, "index": bad}
            _assert(parse_command(raw) is None,
                    f"{action} index {bad!r} is rejected")
        _assert(parse_command({"type": "cmd", "action": action}) is None,
                f"{action} without an index is rejected")


def test_free_run_command_validation():
    print("test_free_run_command_validation")
    cmd = parse_command({"type": "cmd", "action": "free_run",
                         "bpm": 96, "meter": "6/8"})
    _assert(cmd == {"action": "free_run", "bpm": 96, "meter": "6/8"},
            "free_run carries its tempo and meter")
    for bad in (None, "96", 96.5, True, 39, 301, 10 ** 400):
        raw = {"type": "cmd", "action": "free_run", "bpm": bad, "meter": "4/4"}
        _assert(parse_command(raw) is None,
                f"free_run bpm={str(bad)[:12]!r} is rejected")
    for bad in (None, "4/3", "banana", ["4/4"], 4):
        raw = {"type": "cmd", "action": "free_run", "bpm": 120, "meter": bad}
        _assert(parse_command(raw) is None,
                f"free_run meter={bad!r} is rejected")

    cmd = parse_command({"type": "cmd", "action": "free_run_stop"})
    _assert(cmd == {"action": "free_run_stop"}, "free_run_stop takes nothing")


def test_open_show_validation():
    print("test_open_show_validation")
    cmd = parse_command({"type": "cmd", "action": "open_show", "name": "Gira 2026"})
    _assert(cmd == {"action": "open_show", "name": "Gira 2026"},
            "open_show carries its show name")
    for bad in (None, "", 3, True, ["Gira"], "x" * 201):
        raw = {"type": "cmd", "action": "open_show", "name": bad}
        _assert(parse_command(raw) is None,
                f"open_show name={bad!r} is rejected")
    _assert(parse_command({"type": "cmd", "action": "open_show"}) is None,
            "open_show without a name is rejected")


def test_worker_handle_text_delegates():
    print("test_worker_handle_text_delegates")
    worker = WebRemoteWorker(port=8765, static_dir=".")

    cmd = worker._handle_text('{"type": "cmd", "action": "play_pause"}')
    _assert(cmd == {"action": "play_pause"},
            "worker validates inbound frames through parse_command")
    _assert(worker._handle_text("garbage") is None,
            "worker drops invalid frames")
    _assert(worker._latest_state is None and not worker._clients,
            "un-run worker holds no state and no clients")


def test_actions_whitelist_is_closed():
    print("test_actions_whitelist_is_closed")
    _assert(ACTIONS == {"play_pause", "stop", "next", "prev", "seek", "cue",
                        "select_song", "blackout", "panic",
                        "free_run", "free_run_stop", "open_show"},
            "the remote action whitelist is exactly the frozen protocol set")


def main():
    test_simple_actions()
    test_json_text_input()
    test_rejects_bad_envelopes()
    test_seek_validation()
    test_indexed_validation()
    test_free_run_command_validation()
    test_open_show_validation()
    test_worker_handle_text_delegates()
    test_actions_whitelist_is_closed()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
