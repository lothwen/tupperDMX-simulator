"""
Characterization test: webremote config section (core/config.py).

Drives Config._apply_userprefs / _userprefs_to_dict directly (no disk):
defaults when the key is absent, port clamping, the save/load round-trip, and
that a legacy `cues` block is read by nobody and never written back — the
remote's buttons are the open show's starred clips (core/clips.py), which is
show content rather than a machine setting.

Run with:
    uv run python tests/test_webremote_config.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dataclasses import asdict

from core.config import Config


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_defaults():
    print("test_defaults")
    config = Config()
    _assert(config.webremote.enabled is False, "server is off by default")
    _assert(config.webremote.port == 8765, "default port is 8765")
    _assert(not hasattr(config.webremote, "cues"),
            "prefs carry no cue set — favourites live in the show")


def test_absent_key_keeps_defaults():
    print("test_absent_key_keeps_defaults")
    config = Config()
    Config._apply_userprefs(config, {})
    _assert(asdict(config.webremote) == {"enabled": False, "port": 8765},
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


def test_legacy_cues_are_ignored():
    print("test_legacy_cues_are_ignored")
    config = Config()
    # What every prefs file written before favourites still carries.
    Config._apply_userprefs(config, {"webremote": {"port": 9001, "cues": [
        {"label": "Warm", "color": "#ffb86b", "messages": [[0, 73, 127]]},
    ]}})
    _assert(config.webremote.port == 9001,
            "the rest of the section still parses around the legacy key")
    _assert(not hasattr(config.webremote, "cues"),
            "a legacy cue set loads without error and is not adopted")
    _assert("cues" not in config._userprefs_to_dict()["webremote"],
            "and it is not written back on the next save")


def test_roundtrip():
    print("test_roundtrip")
    config = Config()
    config.webremote.enabled = True
    config.webremote.port = 9000

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
    test_legacy_cues_are_ignored()
    test_roundtrip()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
