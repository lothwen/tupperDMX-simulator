# ABOUTME: PlaylistCore — widget-free playlist navigation + per-song load
# ABOUTME: logic, shared by the desktop UI and the headless runner. The UI
# ABOUTME: layer (ui/playlist_controller.py) listens to its signals.
import os

from PyQt6.QtCore import QObject, pyqtSignal

from core.config import SongConfig
from core.show import song_import, store
from core.tempo_map import clamp_bpm
from tupperlogger import get_logger
from webremote.state import probe_bpm

# What a .mid without a set_tempo plays at (MIDI's default of 500000 µs/beat).
DEFAULT_BPM = 120


class PlaylistCore(QObject):
    """Playlist navigation and applying a song's config to the core.

    Owns the logic half of what ui.PlaylistController used to do: index
    math, the stop → flush("song-switch") → persist ordering, and feeding
    media/midi/BPM/count-in/voice-cues on every song load. Widget updates
    (timeline, toolbar, relink dialogs) ride the signals below.

    `core` is the façade the web remote also consumes (MainWindow today,
    AppCore headless): config, show_manager, media_controller, midi_manager,
    bpm_state, stop_playback(), save_config(), apply_voice_tts(song),
    apply_song_markers(song).
    """

    song_loaded = pyqtSignal(object, bool)  # SongConfig|None, midi_ok
    audio_missing = pyqtSignal(object)      # SongConfig (path kept: relink key)
    setlist_changed = pyqtSignal()          # order, length, current index or a song's details

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
        core.media_controller.set_voice_tts([])
        core.apply_song_markers(SongConfig())  # clear the previous song's
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
        # Re-pick the monitor for THIS song (guide when it has one) before the
        # backing lands, so only the monitored file is ever decoded. A no-op
        # unless the desktop enabled it — headless stays on the backing.
        core.media_controller.auto_select_monitor()

        # Apply audio file
        if song.audio and not os.path.exists(song.audio):
            # Missing media: keep the path on the SONG (it's the relink key)
            # but clear it on the controller, or the previous song's backing
            # would still be loaded and play under this one. Surface the
            # state: the UI layer offers recovery, headless just logs.
            self.logger.warning(f"Missing audio for '{song.title}': {song.audio}")
            core.media_controller.set_files(None)
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
        # the UI layer's half of apply_voice_tts' listeners)
        core.apply_voice_tts(song)

        # Control markers + segue. AFTER the BPM detection above: the tempo map
        # is built from the base tempo plus these, so a stale base would put the
        # markers' segments at the wrong times.
        core.apply_song_markers(song)

        return midi_ok

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def next_song(self):
        """Switch to the next enabled song in the playlist."""
        core = self.core
        index = core.config.playlist.neighbour(+1)
        if index is not None:
            # Stop playback and save pending edits before switching
            core.stop_playback()
            core.show_manager.flush("song-switch")

            core.config.set_current_song_index(index)
            self.load_current_song()
            self.setlist_changed.emit()
            core.save_config()
            total = len(core.config.playlist.songs)
            self.logger.info(f"Switched to song {index + 1}/{total}")

    def prev_song(self):
        """Switch to the previous enabled song in the playlist."""
        core = self.core
        index = core.config.playlist.neighbour(-1)
        if index is not None:
            # Stop playback and save pending edits before switching
            core.stop_playback()
            core.show_manager.flush("song-switch")

            core.config.set_current_song_index(index)
            self.load_current_song()
            self.setlist_changed.emit()
            core.save_config()
            total = len(core.config.playlist.songs)
            self.logger.info(f"Switched to song {index + 1}/{total}")

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

    def add_empty_song(self, switch=True):
        """Append an empty song (no backing, no guide, empty MIDI track) and
        make it current — or, with switch=False (the playlist manager), just
        append it, leaving the current song and its playback alone. An empty
        setlist switches either way: there is no song loaded to leave alone.

        Loading a file only ever edits the current song, so a song exists
        first and collects its tracks after (importing from another show is
        the other way the setlist grows)."""
        core = self.core
        if not switch and core.config.playlist.songs:
            index = core.config.add_song()
            core.save_config()
            self.setlist_changed.emit()
            self.logger.info(f"Added empty song at index {index}")
            return index

        # Stop playback and save pending edits before switching away
        core.stop_playback()
        core.show_manager.flush("song-new")

        index = core.config.add_song()
        core.config.set_current_song_index(index)
        self.load_current_song()
        self.setlist_changed.emit()
        core.save_config()
        self.logger.info(f"Added empty song at index {index}")
        return index

    def move_song(self, src, dst):
        """Move a song to another place in the setlist. Nothing stops or
        flushes: the current song stays the same object (Config.move_song
        makes the index follow it), so pending MIDI edits keep their home."""
        if self.core.config.move_song(src, dst):
            self.core.save_config()
            self.setlist_changed.emit()

    def remove_song(self, index, delete_files=False):
        """Remove any song from the setlist. Its files stay on disk unless
        `delete_files`, which deletes the ones only this song of this show
        uses (store.song_owned_files) — after the song is gone, so the current
        song's pending MIDI edits are flushed first rather than written back
        into a deleted file. Only removing the CURRENT song stops playback and
        loads another. Returns the files that could not be deleted."""
        core = self.core
        songs = core.config.playlist.songs
        if not 0 <= index < len(songs):
            return []
        doomed = (store.song_owned_files(core.show_manager.show, songs[index])
                  if delete_files else [])
        if index == core.config.playlist.current_song_index:
            self.remove_current_song()
        elif core.config.remove_song(index):
            core.save_config()
            self.setlist_changed.emit()
            self.logger.info(
                f"Removed song {index + 1} from playlist. "
                f"{len(core.config.playlist.songs)} songs remaining.")
        failed = []
        for path in doomed:
            try:
                os.remove(path)
                self.logger.info(f"Deleted {path}")
            except OSError as e:
                self.logger.warning(f"Could not delete {path}: {e}")
                failed.append(path)
        return failed

    def remove_current_song(self):
        """Remove the current song from the playlist (the entry — and its
        owned .mid — stays on disk; only the setlist forgets it). The UI's
        confirmation lives in the playlist manager."""
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

    # ------------------------------------------------------------------
    # Any song's details (the playlist manager)
    # ------------------------------------------------------------------
    def set_song_enabled(self, song, enabled):
        """Take a song out of the running order (or put it back): it stays in
        the setlist, but next/prev, the segue and the web remote skip it."""
        if song.enabled == bool(enabled):
            return
        song.enabled = bool(enabled)
        self.core.save_config()
        self.setlist_changed.emit()

    def set_song_track(self, song, kind, path):
        """Set (path) or clear (None) a NOT-current song's backing ('audio')
        or guide. A different file's sync offset is not the old one's, so the
        offset resets. The current song goes through the window's media
        loading instead, which reloads the engine."""
        if kind == "audio":
            song.audio, song.offset = path or None, 0.0
        else:
            song.guide, song.guide_offset = path or None, 0.0
        self.core.save_config()
        self.setlist_changed.emit()

    def song_bpm(self, song) -> int:
        """A song's base tempo. The .mid is its home, so a song that is not
        loaded is read from there; the manifest cache is only a fallback."""
        if song is self.core.config.get_current_song():
            return self.core.bpm_state.get_bpm()
        return (probe_bpm(song.midi) or song.cache.get("bpm")
                or DEFAULT_BPM)

    def set_song_bpm(self, song, bpm):
        """Set any song's base tempo. The current song's is bpm_state's (the
        timeline and the autosave follow it); a song that is not loaded gets
        its .mid rewritten. Either way a song with no .mid gets a tempo-only
        one — the autosave never writes an event-less song, so without it the
        tempo would have nowhere to live."""
        core = self.core
        bpm = clamp_bpm(bpm)
        if song is core.config.get_current_song():
            if not song.midi:
                core.show_manager.allocate_song_midi(song)
                core.show_manager.write_song_tempo(song, bpm)
            core.bpm_state.set_bpm(bpm)
        else:
            core.show_manager.write_song_tempo(song, bpm)
        self.setlist_changed.emit()

    def import_songs(self, songs, copy_audio):
        """Append copies of another show's songs (read with
        song_import.peek_show) to this setlist. Returns (new songs,
        warnings). An empty setlist loads the first one, like a new song."""
        core = self.core
        was_empty = not core.config.playlist.songs
        if was_empty:
            core.stop_playback()
            core.show_manager.flush("song-import")
        new_songs, warnings = song_import.import_songs(
            core.show_manager.show, songs, copy_audio=copy_audio)
        core.config.playlist.songs.extend(new_songs)
        if was_empty and new_songs:
            core.config.set_current_song_index(0)
            self.load_current_song()
        core.save_config()
        self.setlist_changed.emit()
        self.logger.info(f"Imported {len(new_songs)} song(s) "
                         f"({'copied' if copy_audio else 'referenced'} audio)")
        return new_songs, warnings
