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
from pathlib import Path

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
from core.show.model import Show
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
        self.show = None

    def begin_song_load(self):
        self.log.append("begin_song_load")

    def end_song_load(self):
        self.log.append("end_song_load")

    def flush(self, reason=""):
        self.log.append(("flush", reason))

    def allocate_song_midi(self, song):
        self.log.append(("allocate_song_midi", song.id))
        song.midi = f"/allocated/{song.id}.mid"
        return song.midi

    def write_song_tempo(self, song, bpm):
        self.log.append(("write_song_tempo", song.id, bpm))
        return True


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


def test_move_song_keeps_the_current_song():
    print("test_move_song_keeps_the_current_song")
    with tempfile.TemporaryDirectory() as tmp:
        a, b, c = (_song(tmp, n) for n in "abc")
        core, playlist, loaded, _, setlist_events = _make(tmp, songs=[a, b, c])
        core.config.playlist.current_song_index = 1

        playlist.move_song(0, 2)
        _assert(core.config.playlist.songs == [b, c, a], "a moved to the end")
        _assert(core.config.get_current_song() is b,
                "the index followed the current song")
        _assert("stop_playback" not in core.log and loaded == []
                and not any(e[0] == "flush" for e in core.log if isinstance(e, tuple)),
                "no stop, no flush, no reload: the current song is unchanged")
        _assert("save_config" in core.log and setlist_events,
                "saved and announced")

        playlist.move_song(0, 1)
        _assert(core.config.playlist.songs == [c, b, a]
                and core.config.playlist.current_song_index == 1,
                "moving the current song itself carries the index along")

        core.log.clear()
        playlist.move_song(0, 9)
        _assert(core.log == [], "an out-of-range move is a no-op")


def test_remove_song_that_is_not_current():
    print("test_remove_song_that_is_not_current")
    with tempfile.TemporaryDirectory() as tmp:
        a, b, c = (_song(tmp, n) for n in "abc")
        core, playlist, loaded, _, setlist_events = _make(tmp, songs=[a, b, c])
        core.config.playlist.current_song_index = 2

        playlist.remove_song(0)
        _assert(core.config.playlist.songs == [b, c], "the first song is gone")
        _assert(core.config.get_current_song() is c, "c is still current")
        _assert("stop_playback" not in core.log and loaded == [],
                "the loaded song plays on")
        _assert(setlist_events and "save_config" in core.log, "saved and announced")

        playlist.remove_song(1)
        _assert(("flush", "song-remove") in core.log and loaded[-1][0] is b,
                "removing the current song takes the stop/flush/load route")


def test_remove_song_with_its_files():
    print("test_remove_song_with_its_files")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Gira.show"
        (root / "midis").mkdir(parents=True)
        a, b, c = (_song(str(root / "midis"), n) for n in "abc")
        external = _song(tmp, "ext")          # audio outside the show folder
        c.audio = external.audio
        core, playlist, loaded, _, _ = _make(tmp, songs=[a, b, c])
        core.show_manager.show = Show(root=root, name="Gira",
                                      playlist=core.config.playlist)

        failed = playlist.remove_song(2, delete_files=True)
        _assert(failed == [] and core.config.playlist.songs == [a, b],
                "the song is gone")
        _assert(not os.path.exists(c.midi), "its in-show .mid is deleted")
        _assert(os.path.exists(external.audio),
                "audio referenced from outside the show is kept")

        playlist.remove_song(0, delete_files=True)   # the current song
        _assert(("flush", "song-remove") in core.log
                and not os.path.exists(a.midi) and not os.path.exists(a.audio),
                "the current song flushes, goes, then its files go")
        _assert(loaded[-1][0] is b, "the next song loads")

        playlist.remove_song(0)
        _assert(os.path.exists(b.midi) and os.path.exists(b.audio),
                "without delete_files every file stays")


def test_add_empty_song_without_switching():
    print("test_add_empty_song_without_switching")
    with tempfile.TemporaryDirectory() as tmp:
        first = _song(tmp, "uno")
        core, playlist, loaded, _, setlist_events = _make(tmp, songs=[first])

        index = playlist.add_empty_song(switch=False)
        _assert(index == 1 and len(core.config.playlist.songs) == 2, "appended")
        _assert(core.config.get_current_song() is first, "current song unchanged")
        _assert("stop_playback" not in core.log and loaded == [],
                "playback and the loaded song left alone")
        _assert(setlist_events and "save_config" in core.log, "saved and announced")

        core2, playlist2, loaded2, _, _ = _make(tmp, songs=[])
        playlist2.add_empty_song(switch=False)
        _assert(loaded2 and loaded2[-1][0] is core2.config.playlist.songs[0],
                "an empty setlist still loads its first song")


def test_navigation_skips_disabled_songs():
    print("test_navigation_skips_disabled_songs")
    with tempfile.TemporaryDirectory() as tmp:
        a, b, c = (_song(tmp, n) for n in "abc")
        b.enabled = False
        core, playlist, loaded, _, _ = _make(tmp, songs=[a, b, c])

        playlist.next_song()
        _assert(core.config.get_current_song() is c, "next skips the disabled b")
        playlist.prev_song()
        _assert(core.config.get_current_song() is a, "prev skips it too")

        c.enabled = False
        core.log.clear()
        playlist.next_song()
        _assert(core.log == [] and core.config.get_current_song() is a,
                "no enabled song ahead: next is a no-op")

        core.config.playlist.current_song_index = 1  # opened directly
        _assert(core.config.playlist.neighbour(-1) == 0
                and core.config.playlist.neighbour(+1) is None,
                "a disabled current song still has enabled neighbours")


def test_set_song_bpm_routes_by_song():
    print("test_set_song_bpm_routes_by_song")
    with tempfile.TemporaryDirectory() as tmp:
        a, b = _song(tmp, "a", bpm=100), _song(tmp, "b", bpm=133)
        empty = SongConfig(id="e", title="e")
        core, playlist, _, _, setlist_events = _make(tmp, songs=[a, b, empty])
        core.bpm_state.set_bpm(100)

        _assert(playlist.song_bpm(a) == 100, "current song reads bpm_state")
        _assert(playlist.song_bpm(b) == 133, "other song reads its .mid")
        empty.cache = {"bpm": 90}
        _assert(playlist.song_bpm(empty) == 90, "no .mid: the manifest cache")
        empty.cache = {}
        _assert(playlist.song_bpm(empty) == 120, "nothing known: MIDI's 120")

        playlist.set_song_bpm(b, 140)
        _assert(("write_song_tempo", "b", 140) in core.log
                and core.bpm_state.get_bpm() == 100,
                "a song that is not loaded gets its file rewritten, "
                "bpm_state untouched")

        playlist.set_song_bpm(a, 150)
        _assert(core.bpm_state.get_bpm() == 150
                and not any(e[:2] == ("write_song_tempo", "a")
                            for e in core.log if isinstance(e, tuple)),
                "the current song's tempo goes through bpm_state only")

        core.config.playlist.current_song_index = 2
        playlist.set_song_bpm(empty, 999)
        _assert(("allocate_song_midi", "e") in core.log
                and ("write_song_tempo", "e", 300) in core.log
                and core.bpm_state.get_bpm() == 300,
                "a current song with no .mid gets a tempo-only file "
                "(and the BPM is clamped)")
        _assert(setlist_events, "setlist_changed emitted")


def test_set_song_enabled_and_track():
    print("test_set_song_enabled_and_track")
    with tempfile.TemporaryDirectory() as tmp:
        a, b = _song(tmp, "a"), _song(tmp, "b", offset=1.5)
        b.guide, b.guide_offset = "/g.mp3", 0.7
        core, playlist, _, _, setlist_events = _make(tmp, songs=[a, b])

        playlist.set_song_enabled(b, False)
        _assert(b.enabled is False and "save_config" in core.log
                and setlist_events, "disabled, saved, announced")
        core.log.clear()
        playlist.set_song_enabled(b, False)
        _assert(core.log == [], "an unchanged flag writes nothing")

        playlist.set_song_track(b, "audio", "/new.wav")
        _assert(b.audio == "/new.wav" and b.offset == 0.0,
                "new backing, its offset reset")
        playlist.set_song_track(b, "guide", None)
        _assert(b.guide is None and b.guide_offset == 0.0, "guide cleared")


def main():
    test_load_song_full()
    test_load_song_without_tempo_keeps_bpm()
    test_missing_audio_is_surfaced_not_loaded()
    test_bad_midi_falls_back_to_empty_track()
    test_empty_state()
    test_navigation_bounds_and_order()
    test_add_empty_song()
    test_remove_current_song()
    test_move_song_keeps_the_current_song()
    test_remove_song_that_is_not_current()
    test_remove_song_with_its_files()
    test_add_empty_song_without_switching()
    test_navigation_skips_disabled_songs()
    test_set_song_bpm_routes_by_song()
    test_set_song_enabled_and_track()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
