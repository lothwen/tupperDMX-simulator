"""
Characterization test: PlaylistCore navigation + song-load logic.

Drives app.playlist.PlaylistCore directly against a FakeCore (recording fakes
for show_manager/media_controller/transport, a REAL MidiManager and a REAL
BpmState) — no widgets, no timers. Covers nav index math and the
stop → flush("song-switch") → save call order, load_song's audio/midi
branches (missing-audio emission, bad-midi fallback to an empty track), BPM
detection from the .mid's set_tempo (and BPM preservation when the file has
none), count-in forwarding and the empty-state path.

Run with:
    uv run python tests/test_playlist_core.py
"""

import sys
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import mido

from app.bpm import BpmState
from app.playlist import PlaylistCore
from core import markers as core_markers
from core import note_layout
from core.config import Config, SongConfig
from midi import MidiManager


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


from dataclasses import dataclass


@dataclass
class FakeFixture:
    type: str


PATCH = [FakeFixture("ledbar"), FakeFixture("blinder")]
BLINDER_CLIP = 26  # standalone blinder verb; never scene-clustered


class FakeShowManager:
    def __init__(self, log):
        self.log = log

    def begin_song_load(self):
        self.log.append("begin_song_load")

    def end_song_load(self):
        self.log.append("end_song_load")

    def flush(self, reason=""):
        self.log.append(("flush", reason))


class FakeMediaController:
    def __init__(self, log):
        self.log = log

    def set_files(self, audio):
        self.log.append(("set_files", audio))

    def set_guide(self, path, offset_sec=0.0):
        self.log.append(("set_guide", path, offset_sec))

    def auto_select_monitor(self):
        self.log.append("auto_select_monitor")

    def set_audio_offset(self, offset):
        self.log.append(("set_audio_offset", offset))

    def set_count_in(self, enabled, bars):
        self.log.append(("set_count_in", bool(enabled), bars))

    def set_voice_tts(self, cues):
        self.log.append(("set_voice_tts", list(cues)))

    def set_control_markers(self, markers):
        self.log.append(("set_control_markers", list(markers)))

    def set_segue(self, enabled, time_sec):
        self.log.append(("set_segue", bool(enabled), time_sec))


class FakeCore:
    """The façade surface PlaylistCore consumes (MainWindow / AppCore)."""

    def __init__(self):
        self.log = []
        self.config = Config()
        self.show_manager = FakeShowManager(self.log)
        self.media_controller = FakeMediaController(self.log)
        self.midi_manager = MidiManager(note_layout.build_note_map(),
                                        fixtures=PATCH)
        self.bpm_state = BpmState()

    def stop_playback(self):
        self.log.append("stop_playback")

    def save_config(self):
        self.log.append("save_config")

    def apply_voice_tts(self, song):
        self.log.append(("apply_voice_tts", song.id))

    def apply_song_markers(self, song):
        self.log.append(("apply_song_markers", song.id))
        self.bpm_state.set_tempo_changes(
            core_markers.tempo_changes(song.control_markers))
        self.media_controller.set_control_markers(
            [(m.time, m.kind, m.bpm) for m in song.control_markers])
        self.media_controller.set_segue(song.segue, song.segue_time)


def _write_midi(path, bpm=None):
    """A minimal real .mid: one blinder clip, optionally with a set_tempo."""
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    track = mido.MidiTrack()
    if bpm is not None:
        track.append(mido.MetaMessage(
            'set_tempo', tempo=mido.bpm2tempo(bpm), time=0))
    track.append(mido.Message(
        'note_on', channel=1, note=BLINDER_CLIP, velocity=100, time=0))
    track.append(mido.Message(
        'note_off', channel=1, note=BLINDER_CLIP, velocity=0, time=480))
    mid.tracks.append(track)
    mid.save(path)


def _make(tmp, *, songs):
    core = FakeCore()
    core.config.playlist.songs = list(songs)
    core.config.playlist.current_song_index = 0
    playlist = PlaylistCore(core)
    loaded = []
    missing = []
    setlist_events = []
    playlist.song_loaded.connect(lambda s, ok: loaded.append((s, ok)))
    playlist.audio_missing.connect(missing.append)
    playlist.setlist_changed.connect(lambda: setlist_events.append(True))
    return core, playlist, loaded, missing, setlist_events


def _song(tmp, name, *, bpm=None, audio=True, midi=True, offset=0.0,
          precount=True):
    audio_path = None
    if audio:
        audio_path = os.path.join(tmp, f"{name}.mp3")
        with open(audio_path, "wb") as f:
            f.write(b"a")
    midi_path = None
    if midi:
        midi_path = os.path.join(tmp, f"{name}.mid")
        _write_midi(midi_path, bpm=bpm)
    return SongConfig(id=name, title=name, audio=audio_path, midi=midi_path,
                      offset=offset, precount=precount)


def test_load_song_full():
    print("test_load_song_full")
    with tempfile.TemporaryDirectory() as tmp:
        song = _song(tmp, "uno", bpm=142, offset=0.25)
        core, playlist, loaded, missing, _ = _make(tmp, songs=[song])

        playlist.load_song(song)

        _assert(("set_files", song.audio) in core.log, "backing handed to media")
        _assert(("set_audio_offset", 0.25) in core.log, "saved offset applied")
        _assert(core.bpm_state.get_bpm() == 142, "BPM detected from set_tempo")
        _assert(("set_count_in", True, core.config.audio.count_in_bars)
                in core.log, "count-in fed from song.precount + global bars")
        _assert(("apply_voice_tts", "uno") in core.log,
                "voice cues (re)rendered on load")
        _assert(loaded == [(song, True)], "song_loaded(song, midi_ok=True)")
        _assert(missing == [], "no missing-audio emission")
        _assert(core.log.index("begin_song_load") < core.log.index("end_song_load"),
                "load wrapped in the autosave gate")


def test_load_song_without_tempo_keeps_bpm():
    print("test_load_song_without_tempo_keeps_bpm")
    with tempfile.TemporaryDirectory() as tmp:
        song = _song(tmp, "dos", bpm=None)
        core, playlist, loaded, _, _ = _make(tmp, songs=[song])
        core.bpm_state.set_bpm(97)

        playlist.load_song(song)

        _assert(core.bpm_state.get_bpm() == 97,
                "no set_tempo -> manually-set BPM survives")
        _assert(loaded == [(song, True)], "midi still loads fine")


def test_missing_audio_is_surfaced_not_loaded():
    print("test_missing_audio_is_surfaced_not_loaded")
    with tempfile.TemporaryDirectory() as tmp:
        song = _song(tmp, "tres")
        os.remove(song.audio)
        core, playlist, loaded, missing, _ = _make(tmp, songs=[song])

        playlist.load_song(song)

        _assert(missing == [song], "audio_missing emitted with the song")
        _assert(("set_files", None) in core.log,
                "the media controller is CLEARED, never fed the missing path "
                "(and never left holding the previous song's backing)")
        _assert(all(c[1] != song.audio for c in core.log
                    if isinstance(c, tuple) and c[0] == "set_files"),
                "the missing path is never handed to media")
        _assert(song.audio and not os.path.exists(song.audio),
                "the broken path is preserved (it's the relink key)")
        _assert(loaded == [(song, True)], "the rest of the song still loads")


def test_bad_midi_falls_back_to_empty_track():
    print("test_bad_midi_falls_back_to_empty_track")
    with tempfile.TemporaryDirectory() as tmp:
        song = _song(tmp, "cuatro", midi=False)
        song.midi = os.path.join(tmp, "no-such.mid")
        core, playlist, loaded, _, _ = _make(tmp, songs=[song])

        playlist.load_song(song)

        _assert(loaded == [(song, False)], "song_loaded reports midi_ok=False")
        _assert(core.midi_manager.midi_data is not None,
                "an empty track replaces the unloadable file")
        _assert(core.midi_manager.midi_events == [], "and it has no events")


def test_empty_state():
    print("test_empty_state")
    with tempfile.TemporaryDirectory() as tmp:
        core, playlist, loaded, _, _ = _make(tmp, songs=[])
        core.config.playlist.current_song_index = -1

        playlist.load_current_song()

        _assert(loaded == [(None, False)], "song_loaded(None) for the empty state")
        _assert(("set_voice_tts", []) in core.log, "stale voice cues dropped")
        _assert(core.midi_manager.midi_data is not None, "empty track created")
        _assert("begin_song_load" in core.log and "end_song_load" in core.log,
                "empty-state creation is autosave-gated too")


def test_navigation_bounds_and_order():
    print("test_navigation_bounds_and_order")
    with tempfile.TemporaryDirectory() as tmp:
        songs = [_song(tmp, f"s{i}") for i in range(3)]
        core, playlist, loaded, _, setlist_events = _make(tmp, songs=songs)

        playlist.next_song()
        _assert(core.config.playlist.current_song_index == 1, "next -> index 1")
        stop_i = core.log.index("stop_playback")
        flush_i = core.log.index(("flush", "song-switch"))
        save_i = core.log.index("save_config")
        _assert(stop_i < flush_i < save_i,
                "stop, then flush pending edits, then persist the index")
        _assert(loaded and loaded[-1][0] is songs[1], "the new song was loaded")
        _assert(setlist_events, "setlist_changed emitted on nav")

        playlist.go_to_song(2)
        playlist.next_song()
        _assert(core.config.playlist.current_song_index == 2,
                "next at the end is a no-op")

        core.log.clear()
        playlist.go_to_song(2)
        _assert(core.log == [], "go_to_song(current) never interrupts playback")
        playlist.go_to_song(99)
        _assert(core.log == [], "out-of-range go_to_song is a no-op")

        playlist.prev_song()
        playlist.prev_song()
        _assert(core.config.playlist.current_song_index == 0, "prev twice -> 0")
        playlist.prev_song()
        _assert(core.config.playlist.current_song_index == 0,
                "prev at the start is a no-op")


def test_add_empty_song():
    print("test_add_empty_song")
    with tempfile.TemporaryDirectory() as tmp:
        first = _song(tmp, "uno")
        core, playlist, loaded, _, setlist_events = _make(tmp, songs=[first])

        index = playlist.add_empty_song()

        songs = core.config.playlist.songs
        _assert(index == 1 and len(songs) == 2, "appended after the current song")
        _assert(core.config.playlist.current_song_index == 1,
                "the new song becomes current")
        new = songs[1]
        _assert(new.audio is None and new.guide is None and not new.midi,
                "it starts with no tracks at all")
        _assert(new.id and new.id != first.id, "it gets its own stable id")
        _assert(loaded[-1][0] is new, "it is loaded (empty MIDI track and all)")
        _assert(("set_files", None) in core.log,
                "media cleared: the previous song's backing does not follow")
        _assert(setlist_events, "setlist_changed emitted (counter/combo refresh)")
        _assert("stop_playback" in core.log and ("flush", "song-new") in core.log,
                "playback stopped and pending edits flushed first")
        _assert("save_config" in core.log, "the show records the new song")


def test_remove_current_song():
    print("test_remove_current_song")
    with tempfile.TemporaryDirectory() as tmp:
        songs = [_song(tmp, "a"), _song(tmp, "b")]
        core, playlist, loaded, _, setlist_events = _make(tmp, songs=songs)

        playlist.remove_current_song()
        _assert(core.config.playlist.songs == [songs[1]], "first song removed")
        _assert(("flush", "song-remove") in core.log, "pending edits flushed first")
        _assert(loaded and loaded[-1][0] is songs[1],
                "the surviving song becomes current and loads")

        playlist.remove_current_song()
        _assert(core.config.playlist.songs == [], "last song removed")
        _assert(loaded[-1] == (None, False), "empty state after the last removal")
        _assert(setlist_events, "setlist_changed emitted")

        core.log.clear()
        playlist.remove_current_song()
        _assert(core.log == [], "removing from an empty playlist is a no-op")


def main():
    test_load_song_full()
    test_load_song_without_tempo_keeps_bpm()
    test_missing_audio_is_surfaced_not_loaded()
    test_bad_midi_falls_back_to_empty_track()
    test_empty_state()
    test_navigation_bounds_and_order()
    test_add_empty_song()
    test_remove_current_song()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
