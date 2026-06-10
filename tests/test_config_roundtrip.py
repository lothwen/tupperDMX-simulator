"""
Characterization test: Config load/save and the two-file split.

Covers first-launch creation of userprefs.json, full round-trip fidelity of the
user-editable sections, that save() writes ONLY userprefs (never the shipped
read-only config.json), validation/clamping of out-of-range values, graceful
fallback on a corrupted userprefs file, and auto-registration of the LED-bar
channel-control notes (1-7) in the MIDI note map.

Paths are redirected to a temp dir (Config.load/save call the module-level
userprefs_path()/shipped_config_path() by name, so monkeypatching those on the
config module isolates the test from the real userprefs.json).

Run with:
    uv run python tests/test_config_roundtrip.py
"""

import sys
import os
import json
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from config import Config


SHIPPED = {
    "fixtures": {"led_bar_controllers": [], "blinders": []},
    "midi_note_map": {
        "26": {"part": "Blinder Flash", "color": "#ffffff", "fixture": "blinder"},
    },
}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _patch_paths(tmp):
    """Redirect shipped + userprefs paths into the temp dir."""
    shipped = Path(tmp) / "config.json"
    prefs = Path(tmp) / "userprefs.json"
    config.shipped_config_path = lambda: str(shipped)
    config.userprefs_path = lambda: prefs
    return shipped, prefs


def test_first_launch_creates_userprefs():
    print("test_first_launch_creates_userprefs")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        _assert(not prefs.exists(), "no userprefs before first launch")
        cfg = Config.load()
        _assert(prefs.exists(), "first launch creates userprefs.json")
        _assert(cfg.artnet.ip == "127.0.0.1", "default Art-Net ip")
        _assert(cfg.volume.audio == 0.5, "default audio volume")
        _assert("26" in cfg.midi_note_map, "shipped midi_note_map was applied")


def test_userprefs_roundtrip():
    print("test_userprefs_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        audio = Path(tmp) / "song.mp3"
        audio.write_bytes(b"x")  # add_song validates the path exists

        cfg = Config.load()
        cfg.artnet.ip = "10.0.0.9"
        cfg.artnet.port = 6460
        cfg.artnet.universe = 5
        cfg.volume.audio = 0.8
        cfg.volume.metronome = 0.2
        cfg.logging.level = "DEBUG"
        cfg.window.width = 1600
        cfg.window.height = 900
        cfg.last_dmx_source = "Art-Net"
        cfg.last_midi_port = "Software emulated"
        cfg.add_song(audio=str(audio), offset=1.5)
        _assert(cfg.save() is True, "save() reported success")

        cfg2 = Config.load()
        _assert(cfg2.artnet.ip == "10.0.0.9", "artnet ip round-tripped")
        _assert(cfg2.artnet.port == 6460, "artnet port round-tripped")
        _assert(cfg2.artnet.universe == 5, "artnet universe round-tripped")
        _assert(abs(cfg2.volume.audio - 0.8) < 1e-9, "audio volume round-tripped")
        _assert(abs(cfg2.volume.metronome - 0.2) < 1e-9, "metronome volume round-tripped")
        _assert(cfg2.logging.level == "DEBUG", "log level round-tripped")
        _assert(cfg2.window.width == 1600 and cfg2.window.height == 900,
                "window size round-tripped")
        _assert(cfg2.last_dmx_source == "Art-Net", "last_dmx_source round-tripped")
        _assert(cfg2.last_midi_port == "Software emulated", "last_midi_port round-tripped")
        _assert(len(cfg2.playlist.songs) == 1, "playlist song round-tripped")
        _assert(cfg2.playlist.songs[0].audio == str(audio), "song audio path round-tripped")
        _assert(abs(cfg2.playlist.songs[0].offset - 1.5) < 1e-9, "song offset round-tripped")


def test_save_writes_only_userprefs():
    print("test_save_writes_only_userprefs")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        shipped_before = shipped.read_text()

        cfg = Config.load()
        cfg.artnet.port = 7000
        cfg.save()

        _assert(shipped.read_text() == shipped_before,
                "save() leaves the shipped config.json untouched")
        data = json.loads(prefs.read_text())
        _assert("fixtures" not in data and "midi_note_map" not in data,
                "userprefs.json carries no read-only shipped sections")
        _assert("artnet" in data and "volume" in data and "logging" in data,
                "userprefs.json carries the user-editable sections")


def test_validation_clamps_out_of_range():
    print("test_validation_clamps_out_of_range")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        prefs.write_text(json.dumps({
            "artnet": {"ip": "999.1.1.1", "port": 99999, "universe": -5},
            "volume": {"audio": 5.0, "metronome": -1.0},
            "logging": {"level": "BOGUS"},
            "window": {"width": 10, "height": 999999},
        }))
        cfg = Config.load()
        _assert(cfg.artnet.ip == "127.0.0.1", "invalid IP falls back to default")
        _assert(cfg.artnet.port == 65535, "port clamped to max")
        _assert(cfg.artnet.universe == 0, "universe clamped to min")
        _assert(cfg.volume.audio == 1.0, "audio volume clamped to 1.0")
        _assert(cfg.volume.metronome == 0.0, "metronome volume clamped to 0.0")
        _assert(cfg.logging.level == "INFO", "invalid log level falls back to INFO")
        _assert(cfg.window.width == 400, "window width clamped to min")
        _assert(cfg.window.height == 3000, "window height clamped to max")


def test_corrupted_userprefs_falls_back():
    print("test_corrupted_userprefs_falls_back")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        prefs.write_text("{ this is not valid json ")
        cfg = Config.load()  # must not raise
        _assert(cfg.artnet.ip == "127.0.0.1", "corrupted prefs -> default artnet")
        _assert(cfg.volume.audio == 0.5, "corrupted prefs -> default volume")


def test_ledbar_channel_notes_autoregister():
    print("test_ledbar_channel_notes_autoregister")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        # Shipped map deliberately omits the channel-control notes 1-7.
        shipped.write_text(json.dumps({
            "midi_note_map": {
                "26": {"part": "Blinder Flash", "color": "#ffffff", "fixture": "blinder"},
            }
        }))
        cfg = Config.load()
        for n in "1234567":
            _assert(n in cfg.midi_note_map, f"LED channel note {n} auto-registered")
        _assert(cfg.midi_note_map["1"].fixture == "ledbar", "channel note maps to ledbar")


def main():
    test_first_launch_creates_userprefs()
    test_userprefs_roundtrip()
    test_save_writes_only_userprefs()
    test_validation_clamps_out_of_range()
    test_corrupted_userprefs_falls_back()
    test_ledbar_channel_notes_autoregister()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
