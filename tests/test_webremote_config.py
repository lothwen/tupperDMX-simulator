"""
Characterization test: webremote config section (core/config.py).

Drives Config._apply_userprefs / _userprefs_to_dict directly (no disk):
defaults when the key is absent, port clamping, defensive cue parsing
(malformed entries and messages dropped, MIDI ranges clamped) and the
save/load round-trip.

Run with:
    uv run python tests/test_webremote_config.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dataclasses import asdict

from core.config import Config, WebCueConfig, default_webremote_cues


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_defaults():
    print("test_defaults")
    config = Config()
    _assert(config.webremote.enabled is False, "server is off by default")
    _assert(config.webremote.port == 8765, "default port is 8765")
    _assert(len(config.webremote.cues) == 6, "six default cues ship")
    _assert([c.label for c in config.webremote.cues]
            == ["Warm", "Cool Blue", "Strobe", "Full flash",
                "Magenta", "Blackout"],
            "default cue labels match the remote's grid")
    _assert(config.webremote.cues[5].messages == [[0, 51, 0]],
            "the Blackout cue sends the note-51 toggle")


def test_absent_key_keeps_defaults():
    print("test_absent_key_keeps_defaults")
    config = Config()
    Config._apply_userprefs(config, {})
    _assert(asdict(config.webremote)["cues"]
            == [asdict(c) for c in default_webremote_cues()],
            "prefs without a webremote key keep the shipped defaults")


def test_port_clamping():
    print("test_port_clamping")
    config = Config()
    Config._apply_userprefs(config, {"webremote": {"port": 80}})
    _assert(config.webremote.port == 1024, "ports below 1024 clamp up")
    Config._apply_userprefs(config, {"webremote": {"port": 99999}})
    _assert(config.webremote.port == 65535, "ports above 65535 clamp down")
    Config._apply_userprefs(config, {"webremote": {"port": "nope"}})
    _assert(config.webremote.port == 1024, "non-numeric port falls to the floor")


def test_cue_parsing_is_defensive():
    print("test_cue_parsing_is_defensive")
    config = Config()
    Config._apply_userprefs(config, {"webremote": {"cues": [
        {"label": "Ok", "color": "#123456", "messages": [[0, 73, 127]]},
        "not a dict",
        {"color": "#fff", "messages": [[0, 51, 0]]},            # no label
        {"label": "Clamped", "messages": [[99, 300, -5]]},      # out of range
        {"label": "Mixed", "messages": [[0, 51], "bad", [1, 2, 3]]},
        {"label": "Bare"},                                      # no messages
    ]}})
    cues = config.webremote.cues
    _assert([c.label for c in cues] == ["Ok", "Clamped", "Mixed", "Bare"],
            "entries without a label (or not dicts) are dropped")
    _assert(cues[0].messages == [[0, 73, 127]] and cues[0].color == "#123456",
            "well-formed cues pass through untouched")
    _assert(cues[1].messages == [[15, 127, 0]],
            "channel/note/velocity are clamped to MIDI ranges")
    _assert(cues[2].messages == [[1, 2, 3]],
            "malformed messages are dropped, valid ones kept")
    _assert(cues[3].messages == [] and cues[3].color == "#888888",
            "message-less cues survive with the default color")

    Config._apply_userprefs(config, {"webremote": {"cues": []}})
    _assert(config.webremote.cues == [],
            "an explicit empty cue list is respected (not reset to defaults)")


def test_roundtrip():
    print("test_roundtrip")
    config = Config()
    config.webremote.enabled = True
    config.webremote.port = 9000
    config.webremote.cues = [
        WebCueConfig("Solo blinders", "#abcdef", [[1, 72, 127], [2, 72, 127]])]

    data = config._userprefs_to_dict()
    _assert(data["webremote"]["enabled"] is True,
            "userprefs dict carries the webremote section")

    restored = Config()
    Config._apply_userprefs(restored, data)
    _assert(asdict(restored.webremote) == asdict(config.webremote),
            "webremote section round-trips through save/load")


def main():
    test_defaults()
    test_absent_key_keeps_defaults()
    test_port_clamping()
    test_cue_parsing_is_defensive()
    test_roundtrip()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
