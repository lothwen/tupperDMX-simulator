"""
Characterization test: voice TTS (Fase 4 spoken control track).

Covers the VoiceTTSConfig model parse/validation, show-manifest round-trip,
and MediaController's firing pointer (crossing, no double-fire, seek
behaviour) with a fake engine — no audio device, no TTS.

Run with:
    uv run python tests/test_voice_tts.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
from pathlib import Path

import numpy as np
from PyQt6.QtWidgets import QApplication

from core.config import Config, PlaylistConfig, SongConfig, VoiceTTSConfig
from core.show.model import Show
from core.show.manifest import show_to_manifest, show_from_manifest

from test_count_in import FakeEngine, FakeMidiManager


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_parse_voice_tts():
    print("test_parse_voice_tts")
    lines = Config._parse_voice_tts([
        {"time": 30.0, "text": "entra batería"},
        {"time": 5.0, "text": "  siguiente: estribillo  "},
        {"time": 1.0, "text": ""},            # empty text dropped
        {"time": 2.0},                          # no text dropped
        "not a dict",                           # malformed dropped
        {"time": -3.0, "text": "clamped"},     # negative time clamps to 0
    ])
    _assert([line.text for line in lines] == ["clamped", "siguiente: estribillo",
                                              "entra batería"],
            "lines parse, trim, drop malformed and sort by time")
    _assert(lines[0].time == 0.0, "negative time clamps to 0")
    _assert(Config._parse_voice_tts(None) == [], "non-list parses to empty")


def test_manifest_roundtrip():
    print("test_manifest_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Demo.show"
        root.mkdir()
        song = SongConfig(id="abc", title="Marea",
                          voice_tts=[VoiceTTSConfig(12.5, "entra batería"),
                                      VoiceTTSConfig(60.0, "último estribillo")])
        show = Show(root=root, name="Demo",
                    playlist=PlaylistConfig(songs=[song]))

        data = show_to_manifest(show)
        _assert(data["songs"][0]["voice_tts"] == [
            {"time": 12.5, "text": "entra batería"},
            {"time": 60.0, "text": "último estribillo"}],
            "voice-TTS lines serialize as plain time/text dicts")

        show2 = show_from_manifest(root, data)
        lines = show2.playlist.songs[0].voice_tts
        _assert([(line.time, line.text) for line in lines]
                == [(12.5, "entra batería"), (60.0, "último estribillo")],
                "voice-TTS lines round-trip through the manifest")

        # A show written before the rename says `voice_cues`, and still opens
        # with its spoken markers — the old key is read, never written.
        legacy = dict(data)
        legacy["songs"] = [dict(data["songs"][0])]
        legacy["songs"][0]["voice_cues"] = legacy["songs"][0].pop("voice_tts")
        show_old = show_from_manifest(root, legacy)
        _assert([line.text for line in show_old.playlist.songs[0].voice_tts]
                == ["entra batería", "último estribillo"],
                "a pre-rename manifest still loads its spoken markers")
        _assert("voice_cues" not in show_to_manifest(show_old)["songs"][0],
                "and writing it back leaves only the new key behind")

        # A pre-Fase-4 manifest (neither key) loads with an empty list.
        del data["songs"][0]["voice_tts"]
        show3 = show_from_manifest(root, data)
        _assert(show3.playlist.songs[0].voice_tts == [],
                "older manifests load with none")


def test_voice_firing():
    print("test_voice_firing")
    from app.media_controller import MediaController
    engine = FakeEngine()
    controller = MediaController(FakeMidiManager(), engine)

    s1 = np.full(10, 0.1, dtype=np.float32)
    s2 = np.full(10, 0.2, dtype=np.float32)
    controller.set_voice_tts([(10.0, s2), (5.0, s1)])  # unsorted on purpose

    controller.current_timeline_time = 4.9
    controller._reset_voice_pointer(0.0)
    controller._fire_voice_tts()
    _assert(engine.voices == [], "nothing speaks before its time")

    controller.current_timeline_time = 5.05
    controller._fire_voice_tts()
    _assert(len(engine.voices) == 1 and engine.voices[0] is s1,
            "a line fires when the timeline crosses its time")

    controller._fire_voice_tts()
    _assert(len(engine.voices) == 1, "a line never fires twice")

    controller.current_timeline_time = 11.0
    controller._fire_voice_tts()
    _assert(len(engine.voices) == 2 and engine.voices[1] is s2,
            "later lines fire in order")

    # Seeking back re-arms lines at/after the seek target…
    engine.voices.clear()
    controller._reset_voice_pointer(5.0)
    controller.current_timeline_time = 5.0
    controller._fire_voice_tts()
    _assert(len(engine.voices) == 1 and engine.voices[0] is s1,
            "seek back re-arms lines (>= target)")

    # …and seeking forward skips the ones behind the target.
    engine.voices.clear()
    controller._reset_voice_pointer(7.0)
    controller.current_timeline_time = 12.0
    controller._fire_voice_tts()
    _assert(engine.voices == [s2], "seek forward skips the lines behind the target")


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    test_parse_voice_tts()
    test_manifest_roundtrip()
    test_voice_firing()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
