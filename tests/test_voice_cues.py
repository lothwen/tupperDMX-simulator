"""
Characterization test: voice cues (Fase 4 TTS control track).

Covers the VoiceCueConfig model parse/validation, show-manifest round-trip,
and MediaController's cue-firing pointer (crossing, no double-fire, seek
behaviour) with a fake engine — no audio device, no TTS.

Run with:
    uv run python tests/test_voice_cues.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
from pathlib import Path

import numpy as np
from PyQt6.QtWidgets import QApplication

from core.config import Config, PlaylistConfig, SongConfig, VoiceCueConfig
from core.show.model import Show
from core.show.manifest import show_to_manifest, show_from_manifest

from test_count_in import FakeEngine, FakeMidiManager


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_parse_voice_cues():
    print("test_parse_voice_cues")
    cues = Config._parse_voice_cues([
        {"time": 30.0, "text": "entra batería"},
        {"time": 5.0, "text": "  siguiente: estribillo  "},
        {"time": 1.0, "text": ""},            # empty text dropped
        {"time": 2.0},                          # no text dropped
        "not a dict",                           # malformed dropped
        {"time": -3.0, "text": "clamped"},     # negative time clamps to 0
    ])
    _assert([c.text for c in cues] == ["clamped", "siguiente: estribillo",
                                       "entra batería"],
            "cues parse, trim, drop malformed and sort by time")
    _assert(cues[0].time == 0.0, "negative time clamps to 0")
    _assert(Config._parse_voice_cues(None) == [], "non-list parses to empty")


def test_manifest_roundtrip():
    print("test_manifest_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Demo.show"
        root.mkdir()
        song = SongConfig(id="abc", title="Marea",
                          voice_cues=[VoiceCueConfig(12.5, "entra batería"),
                                      VoiceCueConfig(60.0, "último estribillo")])
        show = Show(root=root, name="Demo",
                    playlist=PlaylistConfig(songs=[song]))

        data = show_to_manifest(show)
        _assert(data["songs"][0]["voice_cues"] == [
            {"time": 12.5, "text": "entra batería"},
            {"time": 60.0, "text": "último estribillo"}],
            "voice cues serialize as plain time/text dicts")

        show2 = show_from_manifest(root, data)
        cues = show2.playlist.songs[0].voice_cues
        _assert([(c.time, c.text) for c in cues]
                == [(12.5, "entra batería"), (60.0, "último estribillo")],
                "voice cues round-trip through the manifest")

        # A pre-Fase-4 manifest (no voice_cues key) loads with an empty list.
        del data["songs"][0]["voice_cues"]
        show3 = show_from_manifest(root, data)
        _assert(show3.playlist.songs[0].voice_cues == [],
                "older manifests load with no cues")


def test_cue_firing():
    print("test_cue_firing")
    from app.media_controller import MediaController
    engine = FakeEngine()
    controller = MediaController(FakeMidiManager(), engine)

    s1 = np.full(10, 0.1, dtype=np.float32)
    s2 = np.full(10, 0.2, dtype=np.float32)
    controller.set_voice_cues([(10.0, s2), (5.0, s1)])  # unsorted on purpose

    controller.current_timeline_time = 4.9
    controller._reset_cue_pointer(0.0)
    controller._fire_voice_cues()
    _assert(engine.voices == [], "no cue before its time")

    controller.current_timeline_time = 5.05
    controller._fire_voice_cues()
    _assert(len(engine.voices) == 1 and engine.voices[0] is s1,
            "cue fires when the timeline crosses its time")

    controller._fire_voice_cues()
    _assert(len(engine.voices) == 1, "a cue never fires twice")

    controller.current_timeline_time = 11.0
    controller._fire_voice_cues()
    _assert(len(engine.voices) == 2 and engine.voices[1] is s2,
            "later cues fire in order")

    # Seeking back re-arms cues at/after the seek target…
    engine.voices.clear()
    controller._reset_cue_pointer(5.0)
    controller.current_timeline_time = 5.0
    controller._fire_voice_cues()
    _assert(len(engine.voices) == 1 and engine.voices[0] is s1,
            "seek back re-arms cues (>= target)")

    # …and seeking forward skips the ones behind the target.
    engine.voices.clear()
    controller._reset_cue_pointer(7.0)
    controller.current_timeline_time = 12.0
    controller._fire_voice_cues()
    _assert(engine.voices == [s2], "seek forward skips cues behind the target")


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    test_parse_voice_cues()
    test_manifest_roundtrip()
    test_cue_firing()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
