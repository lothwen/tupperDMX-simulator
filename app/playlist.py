# ABOUTME: PlaylistCore — widget-free playlist navigation + per-song load
# ABOUTME: logic, shared by the desktop UI and the headless runner. The UI
# ABOUTME: layer (ui/playlist_controller.py) listens to its signals.
import os

from PyQt6.QtCore import QObject, pyqtSignal

from core.config import SongConfig
from tupperlogger import get_logger


class PlaylistCore(QObject):
    """Playlist navigation and applying a song's config to the core.

    Owns the logic half of what ui.PlaylistController used to do: index
    math, the stop → flush("song-switch") → persist ordering, and feeding
    media/midi/BPM/count-in/voice-cues on every song load. Widget updates
    (timeline, toolbar, relink dialogs) ride the signals below.

    `core` is the façade the web remote also consumes (MainWindow today,
    AppCore headless): config, show_manager, media_controller, midi_manager,
    bpm_state, stop_playback(), save_config(), apply_voice_cues(song).
    """

    song_loaded = pyqtSignal(object, bool)  # SongConfig|None, midi_ok
    audio_missing = pyqtSignal(object)      # SongConfig (path kept: relink key)
    setlist_changed = pyqtSignal()          # length/current index changed

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.logger = get_logger("app.playlist")

    # ------------------------------------------------------------------
    # Song loading
    # ------------------------------------------------------------------
    def load_current_song(self) -> None:
        """Load config's current song, or set up the empty state (one path
        for startup, show reload and removing the last song)."""
        core = self.core
        song = core.config.get_current_song()
        if song is not None:
            self.load_song(song)
            return
        core.show_manager.begin_song_load()
        try:
            core.midi_manager.create_empty_track()
        finally:
            core.show_manager.end_song_load()
        core.media_controller.set_voice_cues([])
        self.song_loaded.emit(None, False)

    def load_song(self, song) -> None:
        """Apply song configuration to the core components.

        Wrapped in begin/end_song_load so the data_changed/bpm_changed bursts
        that loading emits never arm the show autosave — callers that switch
        songs flush pending edits BEFORE moving the playlist index."""
        core = self.core
        if not isinstance(song, SongConfig):
            return
        core.show_manager.begin_song_load()
        try:
            midi_ok = self._load_song(song)
            self.song_loaded.emit(song, midi_ok)
        finally:
            core.show_manager.end_song_load()

    def _load_song(self, song) -> bool:
        core = self.core

        # Guide track (editor monitoring only) — fed BEFORE the backing so
        # set_files resolves the monitored file against this song. A missing
        # guide just logs: the audio_missing relink flow stays backing-only.
        guide = song.guide if (song.guide and os.path.exists(song.guide)) else None
        if song.guide and guide is None:
            self.logger.warning(f"Missing guide for '{song.title}': {song.guide}")
        core.media_controller.set_guide(guide, song.guide_offset)

        # Apply audio file
        if song.audio and not os.path.exists(song.audio):
            # Missing media: keep the path (it's the relink key), surface the
            # state; the UI layer offers recovery, headless just logs.
            self.logger.warning(f"Missing audio for '{song.title}': {song.audio}")
            self.audio_missing.emit(song)
        elif song.audio:
            core.media_controller.set_files(song.audio)
            core.media_controller.set_audio_offset(song.offset)
            if song.offset != 0.0:
                self.logger.info(f"Applied saved audio offset: {song.offset:.3f}s")
        else:
            # No backing: clear it (a guide-only song stays playable while
            # the guide is monitored).
            core.media_controller.set_files(None)

        # Load MIDI file via MidiManager
        midi_ok = False
        if song.midi:
            if core.midi_manager.load_from_file(song.midi):
                midi_ok = True
            else:
                self.logger.error(f"Failed to load MIDI file: {song.midi}")
                core.midi_manager.create_empty_track()
        else:
            # Create empty MIDI track if no MIDI file
            core.midi_manager.create_empty_track()

        # BPM: canonical in the .mid (set_tempo); a file without one leaves
        # the session BPM in effect.
        if midi_ok:
            detected = core.midi_manager.detect_bpm()
            if detected is not None:
                core.bpm_state.set_bpm(detected)

        # Count-in: per-song on/off + machine-level bar count
        core.media_controller.set_count_in(
            song.precount, core.config.audio.count_in_bars)

        # Voice cues: rendered PCM installs asynchronously (ruler markers are
        # the UI layer's half of apply_voice_cues' listeners)
        core.apply_voice_cues(song)

        return midi_ok

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def next_song(self):
        """Switch to the next song in the playlist."""
        core = self.core
        total = len(core.config.playlist.songs)
        current = core.config.playlist.current_song_index
        if current < total - 1:
            # Stop playback and save pending edits before switching
            core.stop_playback()
            core.show_manager.flush("song-switch")

            core.config.set_current_song_index(current + 1)
            self.load_current_song()
            self.setlist_changed.emit()
            core.save_config()
            self.logger.info(f"Switched to song {current + 2}/{total}")

    def prev_song(self):
        """Switch to the previous song in the playlist."""
        core = self.core
        current = core.config.playlist.current_song_index
        if current > 0:
            # Stop playback and save pending edits before switching
            core.stop_playback()
            core.show_manager.flush("song-switch")

            core.config.set_current_song_index(current - 1)
            self.load_current_song()
            self.setlist_changed.emit()
            core.save_config()
            total = len(core.config.playlist.songs)
            self.logger.info(f"Switched to song {current}/{total}")

    def go_to_song(self, index):
        """Switch to a specific song by playlist index (e.g. tapping the web
        remote's setlist). No-op if the index is out of range or already
        current, so re-tapping the playing song never interrupts it."""
        core = self.core
        total = len(core.config.playlist.songs)
        current = core.config.playlist.current_song_index
        if not (0 <= index < total) or index == current:
            return

        # Stop playback and save pending edits before switching
        core.stop_playback()
        core.show_manager.flush("song-switch")

        core.config.set_current_song_index(index)
        self.load_current_song()
        self.setlist_changed.emit()
        core.save_config()
        self.logger.info(f"Switched to song {index + 1}/{total}")

    def remove_current_song(self):
        """Remove the current song from the playlist (the entry — and its
        owned .mid — stays on disk; only the setlist forgets it). The UI's
        confirmation dialog lives in ui.PlaylistController."""
        core = self.core
        current = core.config.playlist.current_song_index
        if len(core.config.playlist.songs) == 0:
            return

        # Stop playback and save pending edits before removing
        core.stop_playback()
        core.show_manager.flush("song-remove")

        core.config.remove_song(current)
        core.save_config()

        # Load the new current song (or the empty state)
        self.load_current_song()
        self.setlist_changed.emit()
        self.logger.info(
            f"Removed song from playlist. "
            f"{len(core.config.playlist.songs)} songs remaining."
        )
