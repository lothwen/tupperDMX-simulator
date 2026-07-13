"""
Characterization test: show autosave (ui/show_controller.py flush pipeline).

Drives a real MidiManager (as the production wiring does) against a temp show:
edits mark the .mid dirty and arm the debounce timer; flush() materializes
midis/<slug>.mid for songs without one, exports edits atomically, stamps the
song's derived cache, and is a no-op when clean; loads gated by
begin/end_song_load never dirty; BPM changes re-export the set_tempo; edits on
an empty setlist create an Untitled song; playback defers the debounced
autosave.

Run with:
    uv run python tests/test_show_autosave.py
"""

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import pytest

from core import fixture_library, note_layout
from core.config import Config, SongConfig
from core.show import store
from midi.manager import MidiManager
from ui.show_controller import AUTOSAVE_DEBOUNCE_MS, ShowManager
from webremote.state import probe_bpm


@pytest.fixture(autouse=True)
def _reset_fixture_library():
    yield
    fixture_library.set_show_fixtures_dir(None)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


@dataclass
class FakeFixture:
    type: str


PATCH = [FakeFixture("ledbar"), FakeFixture("blinder")]
BLINDER_CLIP = 26  # standalone blinder verb; never scene-clustered


class FakeTimelineToolbar:
    def __init__(self):
        self.bpm = 128

    def get_bpm(self):
        return self.bpm


class FakeMediaController:
    def __init__(self):
        self.playing = False

    def get_playback_state(self):
        return self.playing


class FakeToolbarManager:
    def __init__(self):
        self.midi_display = None

    def update_midi_file_display(self, path):
        self.midi_display = path


class FakePlaylistController:
    def update_song_counter(self):
        pass


class FakeWindow:
    def __init__(self, midi_manager):
        self.midi_manager = midi_manager
        self.timeline_toolbar = FakeTimelineToolbar()
        self.media_controller = FakeMediaController()
        self.toolbar_manager = FakeToolbarManager()
        self.playlist_controller = FakePlaylistController()


def _make(tmp, *, with_song=True):
    """Show + config + ShowManager over a real MidiManager, wired like
    MainWindow does (data_changed -> on_midi_data_changed)."""
    show = store.create_show(tmp, "Gira")
    config = Config()
    mm = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
    window = FakeWindow(mm)
    manager = ShowManager(config, window)
    if with_song:
        audio = Path(tmp) / "guia.mp3"
        audio.write_bytes(b"a")
        show.playlist.songs.append(
            SongConfig(audio=str(audio), id="s1", title="Mi Tema"))
        show.playlist.current_song_index = 0
    manager.attach(show)
    mm.data_changed.connect(manager.on_midi_data_changed)

    # Simulate the song load exactly like PlaylistController does: gated.
    manager.begin_song_load()
    mm.create_empty_track()
    mm.set_song_duration(240.0)
    manager.end_song_load()
    return manager, show, mm, window


def test_edit_flush_materializes_and_reloads():
    print("test_edit_flush_materializes_and_reloads")
    with tempfile.TemporaryDirectory() as tmp:
        manager, show, mm, window = _make(tmp)
        states = []
        manager.autosave_state_changed.connect(states.append)
        song = show.playlist.songs[0]
        _assert(song.midi is None, "song starts without a midi file")

        mm.add_event(BLINDER_CLIP, 5.0, 100, channel=1)
        _assert(states and states[0] == "pending", "an edit flips the chip to pending")
        _assert(manager._autosave_timer.isActive()
                and manager._autosave_timer.interval() == AUTOSAVE_DEBOUNCE_MS,
                "an edit arms the debounce timer")

        manager.flush("test")
        _assert(not manager._autosave_timer.isActive(), "flush disarms the timer")
        _assert(states[-1] == "saved", "flush flips the chip to saved")
        _assert(song.midi == str(show.midis_dir / "mi-tema.mid"),
                "first flush materializes midis/<slug>.mid")
        _assert(os.path.exists(song.midi), "the owned .mid exists on disk")
        _assert(window.toolbar_manager.midi_display == song.midi,
                "the toolbar shows the materialized path")

        _assert(song.cache.get("bpm") == 128, "derived cache stamped with the BPM")
        _assert("duration" in song.cache and "audio_mtime" in song.cache,
                "derived cache carries duration/audio_mtime slots")

        import json
        manifest = json.loads((show.root / "show.json").read_text())
        _assert(manifest["songs"][0]["midi"] == "midis/mi-tema.mid",
                "flush also persisted the manifest with the owned midi")

        # The edit survives a full reload — the actual point of autosave.
        mm2 = MidiManager(note_layout.build_note_map(), fixtures=PATCH)
        _assert(mm2.load_from_file(song.midi), "the exported .mid loads back")
        _assert(any(e["note"] == BLINDER_CLIP and abs(e["time"] - 5.0) < 0.01
                    for e in mm2.midi_events),
                "the edited clip is in the reloaded file")


def test_song_load_never_dirties():
    print("test_song_load_never_dirties")
    with tempfile.TemporaryDirectory() as tmp:
        manager, show, mm, window = _make(tmp)
        song = show.playlist.songs[0]
        _assert(not manager._autosave_timer.isActive(),
                "the gated song load did not arm the autosave")
        manager.flush("test")
        _assert(song.midi is None,
                "flush after a clean load writes no midi (nothing dirty)")


def test_clean_flush_never_rewrites():
    print("test_clean_flush_never_rewrites")
    with tempfile.TemporaryDirectory() as tmp:
        manager, show, mm, window = _make(tmp)
        mm.add_event(BLINDER_CLIP, 5.0, 100, channel=1)
        manager.flush("test")
        song = show.playlist.songs[0]
        midi_mtime = os.stat(song.midi).st_mtime_ns
        manifest_mtime = (show.root / "show.json").stat().st_mtime_ns

        manager.flush("again")
        _assert(os.stat(song.midi).st_mtime_ns == midi_mtime,
                "a clean flush does not rewrite the .mid")
        _assert((show.root / "show.json").stat().st_mtime_ns == manifest_mtime,
                "a clean flush does not rewrite the manifest")


def test_bpm_change_reexports_set_tempo():
    print("test_bpm_change_reexports_set_tempo")
    with tempfile.TemporaryDirectory() as tmp:
        manager, show, mm, window = _make(tmp)
        mm.add_event(BLINDER_CLIP, 5.0, 100, channel=1)
        manager.flush("test")
        song = show.playlist.songs[0]
        _assert(probe_bpm(song.midi) == 128, "initial export carries the session BPM")

        window.timeline_toolbar.bpm = 142
        manager.on_bpm_changed(142)
        manager.flush("test")
        _assert(probe_bpm(song.midi) == 142,
                "a BPM change re-exports the .mid's set_tempo")
        _assert(song.cache.get("bpm") == 142, "cache follows the new BPM")


def test_edits_on_empty_setlist_create_untitled_song():
    print("test_edits_on_empty_setlist_create_untitled_song")
    with tempfile.TemporaryDirectory() as tmp:
        manager, show, mm, window = _make(tmp, with_song=False)
        mm.add_event(BLINDER_CLIP, 2.0, 90, channel=1)
        manager.flush("test")
        _assert(len(show.playlist.songs) == 1
                and show.playlist.songs[0].title == "Untitled",
                "edits with an empty setlist get an Untitled song")
        _assert(os.path.exists(show.playlist.songs[0].midi),
                "the Untitled song owns a real .mid")


def test_playback_defers_debounced_autosave():
    print("test_playback_defers_debounced_autosave")
    with tempfile.TemporaryDirectory() as tmp:
        manager, show, mm, window = _make(tmp)
        mm.add_event(BLINDER_CLIP, 5.0, 100, channel=1)
        window.media_controller.playing = True
        manager._on_autosave_timeout()
        song = show.playlist.songs[0]
        _assert(song.midi is None, "no export happens mid-performance")
        _assert(manager._autosave_timer.isActive(),
                "the autosave re-arms to retry after playback")

        window.media_controller.playing = False
        manager._on_autosave_timeout()
        _assert(song.midi is not None and os.path.exists(song.midi),
                "the deferred autosave lands once playback stops")


def main():
    try:
        test_edit_flush_materializes_and_reloads()
        test_song_load_never_dirties()
        test_clean_flush_never_rewrites()
        test_bpm_change_reexports_set_tempo()
        test_edits_on_empty_setlist_create_untitled_song()
        test_playback_defers_debounced_autosave()
    finally:
        fixture_library.set_show_fixtures_dir(None)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
