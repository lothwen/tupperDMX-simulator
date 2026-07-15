"""
Characterization test: WebRemoteManager command execution + state building.

Drives _execute() and _build_state() directly against a FakeWindow (recording
fakes for the transport API, playlist controller, midi manager, timeline
toolbar and software controller) — no server thread, no sockets, no timers
running. Covers the action → window-call mapping, seek clamping, cue message
pass-through, blackout-toggle semantics of panic, and snapshot shape.

Run with:
    uv run python tests/test_webremote_manager.py
"""

import sys
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from core.config import Config, SongConfig
from core.note_layout import NoteInfo
from midi.constants import SONG_PART_NOTES
from webremote.manager import WebRemoteManager


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


class FakeSignal:
    def connect(self, *args, **kwargs):
        pass


class FakeMediaController:
    playback_state_changed = FakeSignal()
    media_loaded = FakeSignal()

    def __init__(self):
        self.playing = False
        self.position = 0.0

    def get_playback_state(self):
        return self.playing

    def get_current_position(self):
        return self.position


class FakeMidiManager:
    data_changed = FakeSignal()

    def __init__(self):
        self.midi_events = []

    def song_end_time(self):
        return 100.0

    def is_song_part(self, event):
        return event.get("note") in SONG_PART_NOTES


class FakeBpmState:
    bpm_changed = FakeSignal()

    def get_bpm(self):
        return 128


class FakeAudioController:
    beat_occurred = FakeSignal()


class FakePlaylistController:
    def __init__(self, window):
        self.window = window

    def next_song(self):
        self.window.calls.append("next_song")

    def prev_song(self):
        self.window.calls.append("prev_song")

    def go_to_song(self, index):
        self.window.calls.append(("go_to_song", index))


class FakeController:
    def __init__(self):
        self.blackout_all_state = False


class FakeWindow:
    def __init__(self):
        self.calls = []
        self.media_controller = FakeMediaController()
        self.midi_manager = FakeMidiManager()
        self.bpm_state = FakeBpmState()
        self.audio_controller = FakeAudioController()
        self.playlist_controller = FakePlaylistController(self)
        self.software_controller = FakeController()

    def play_pause_playback(self):
        self.calls.append("play_pause")

    def stop_playback(self):
        self.calls.append("stop")

    def seek_playback(self, t):
        self.calls.append(("seek", t))

    def preview_clip(self, messages):
        self.calls.append(("preview", messages))

    def on_song_precount_toggled(self, enabled):
        self.calls.append(("precount", enabled))

    def set_count_in_bars(self, bars):
        self.calls.append(("countin_bars", bars))


def _make_manager():
    """Manager over fakes; no worker started, so publishing is a no-op."""
    config = Config()
    window = FakeWindow()
    return WebRemoteManager(config, window), config, window


def test_transport_actions():
    print("test_transport_actions")
    manager, _, window = _make_manager()
    manager._execute({"action": "play_pause"})
    manager._execute({"action": "stop"})
    manager._execute({"action": "next"})
    manager._execute({"action": "prev"})
    _assert(window.calls == ["play_pause", "stop", "next_song", "prev_song"],
            "transport actions map to the window/playlist API")


def test_select_song():
    print("test_select_song")
    manager, _, window = _make_manager()
    manager._execute({"action": "select_song", "index": 3})
    _assert(window.calls == [("go_to_song", 3)],
            "select_song forwards the index to playlist_controller.go_to_song")


def test_seek_clamps_to_duration():
    print("test_seek_clamps_to_duration")
    manager, _, window = _make_manager()
    manager._execute({"action": "seek", "time": 50.0})
    manager._execute({"action": "seek", "time": 500.0})
    _assert(window.calls == [("seek", 50.0), ("seek", 100.0)],
            "seek passes through in range and clamps to the song duration")


def test_cue_sends_config_messages():
    print("test_cue_sends_config_messages")
    manager, config, window = _make_manager()
    manager._execute({"action": "cue", "index": 0})
    expected = [tuple(m) for m in config.webremote.cues[0].messages]
    _assert(window.calls == [("preview", expected)],
            "cue fires its config messages through preview_clip")

    window.calls.clear()
    manager._execute({"action": "cue", "index": 99})
    _assert(window.calls == [], "out-of-range cue index is ignored")


def test_blackout_and_panic():
    print("test_blackout_and_panic")
    manager, _, window = _make_manager()
    manager._execute({"action": "blackout"})
    _assert(window.calls == [("preview", [(0, 51, 0)])],
            "blackout sends the note-51 toggle")

    window.calls.clear()
    manager._execute({"action": "panic"})
    _assert(window.calls == ["stop", ("preview", [(0, 51, 0)])],
            "panic stops playback then forces blackout ON")

    window.calls.clear()
    window.software_controller.blackout_all_state = True
    manager._execute({"action": "panic"})
    _assert(window.calls == ["stop"],
            "panic with blackout already active does not un-toggle it")


def test_countin_commands():
    print("test_countin_commands")
    manager, config, window = _make_manager()
    manager._execute({"action": "set_precount", "on": False})
    manager._execute({"action": "set_countin_bars", "bars": 4})
    _assert(window.calls == [("precount", False), ("countin_bars", 4)],
            "count-in commands map to the window's precount/bars API")


def test_build_state_snapshot():
    print("test_build_state_snapshot")
    manager, config, window = _make_manager()
    config.playlist.songs = [SongConfig(audio="/no/such/Marea.mp3")]
    config.playlist.current_song_index = 0
    window.media_controller.playing = True
    window.media_controller.position = 12.0
    window.midi_manager.midi_events = [
        {"note": 61, "time": 0.0, "info": NoteInfo("INTRO", "#c77f37")},
        {"note": 24, "time": 1.0, "info": NoteInfo("Scene", "#555555")},
        {"note": 62, "time": 30.0, "info": NoteInfo("VERSE", "#c6c737")},
    ]

    state = manager._build_state()
    _assert(state["playing"] is True and state["position"] == 12.0,
            "playback state comes from the media controller")
    _assert(state["bpm"] == 128, "bpm comes from the BpmState holder")
    _assert(state["duration"] == 100.0,
            "unreadable audio falls back to midi song_end_time")
    _assert([s["name"] for s in state["sections"]] == ["INTRO", "VERSE"],
            "sections derive from song-part events only")
    _assert(state["sections"][1]["end"] == 100.0,
            "last section extends to the song end")
    _assert(state["setlist"]["songs"][0]["title"] == "Marea",
            "setlist is built from the playlist")
    _assert(len(state["cues"]) == 6 and state["cues"][0]["label"] == "Warm",
            "cues expose index/label/color from config")
    _assert("messages" not in state["cues"][0],
            "cue MIDI payloads never leave the server")
    _assert(state["blackout"] is False, "blackout state read from controller")
    _assert(state["countin"] == {"enabled": True, "bars": 2},
            "countin reflects the current song's precount + global bars")

    config.playlist.songs[0].precount = False
    config.audio.count_in_bars = 4
    _assert(manager._build_state()["countin"] == {"enabled": False, "bars": 4},
            "countin follows song precount and config bars")

    window.software_controller.blackout_all_state = True
    _assert(manager._build_state()["blackout"] is True,
            "controller blackout toggle propagates into the snapshot")


def test_show_provides_cues_and_name():
    """With an open show (window.show_manager.show), the cue set and the
    show_name come from the show; config cues only serve showless windows."""
    print("test_show_provides_cues_and_name")
    import types
    from core.config import WebCueConfig

    manager, config, window = _make_manager()
    show = types.SimpleNamespace(
        name="Directo Billares 2026",
        cues=[WebCueConfig("Solo", "#ffffff", [[1, 24, 100]])])
    window.show_manager = types.SimpleNamespace(show=show)

    state = manager._build_state()
    _assert(state["show_name"] == "Directo Billares 2026",
            "show_name comes from the open show")
    _assert(len(state["cues"]) == 1 and state["cues"][0]["label"] == "Solo",
            "cues come from the open show, not config")

    manager._execute({"action": "cue", "index": 0})
    _assert(window.calls[-1] == ("preview", [(1, 24, 100)]),
            "cue actions fire the show's cue messages")

    window.show_manager = types.SimpleNamespace(show=None)
    state = manager._build_state()
    _assert(state["show_name"] is None and len(state["cues"]) == 6,
            "no open show -> config cues and null show_name")


def test_no_worker_is_safe():
    print("test_no_worker_is_safe")
    manager, _, window = _make_manager()
    manager._publish_full()
    manager._on_tick()
    manager._on_beat(1)
    manager.cleanup()
    _assert(window.calls == [], "publishing without a worker is a no-op")


def main():
    test_transport_actions()
    test_select_song()
    test_seek_clamps_to_duration()
    test_cue_sends_config_messages()
    test_blackout_and_panic()
    test_countin_commands()
    test_build_state_snapshot()
    test_show_provides_cues_and_name()
    test_no_worker_is_safe()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
