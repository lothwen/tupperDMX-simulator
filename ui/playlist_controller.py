# ABOUTME: PlaylistController — playlist navigation + per-song load orchestration.
# ABOUTME: Extracted from MainWindow; drives media/timeline/midi/toolbar per song.

import os

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QFileDialog, QMessageBox

from core.config import SongConfig
from core.show.model import song_status
from core.show.relink import find_by_basename
from tupperlogger import get_logger


class PlaylistController:
    """Playlist navigation and applying a song's config to the app.

    Orchestrates the MainWindow's collaborators (media / timeline / midi /
    toolbar) whenever the current song changes. Holds a back-reference to the
    window for its shared services (config plus stop_playback / save_config /
    update_window_title), which it reaches lazily so construction order with
    the window's components doesn't matter.
    """

    def __init__(self, window):
        self.window = window
        self.logger = get_logger("ui.playlist")
        # Last folder the user relinked from: moved guide folders usually move
        # together, so it seeds the auto-relink search for the other songs.
        self._last_relink_dir = None

    def apply_song_config(self, song):
        """Apply song configuration to all components.

        Wrapped in begin/end_song_load so the data_changed/bpm_changed bursts
        that loading emits never arm the show autosave — callers that switch
        songs flush pending edits BEFORE moving the playlist index."""
        w = self.window
        if not isinstance(song, SongConfig):
            return
        w.show_manager.begin_song_load()
        try:
            self._apply_song_config(song)
        finally:
            w.show_manager.end_song_load()

    def _apply_song_config(self, song):
        w = self.window

        # Apply audio file
        if song.audio and not os.path.exists(song.audio):
            # Missing media: keep the path (it's the relink key), surface the
            # state, and try to recover once the event loop breathes.
            w.toolbar_manager.update_audio_file_display(
                f"⚠ Missing audio: {song.audio}")
            w.timeline.set_files(None)
            QTimer.singleShot(0, lambda s=song: self._offer_relink(s))
        elif song.audio:
            w.media_controller.set_files(song.audio)
            w.timeline.set_files(song.audio)
            w.toolbar_manager.update_audio_file_display(song.audio)

            # Apply saved audio offset
            if song.offset != 0.0:
                w.media_controller.set_audio_offset(song.offset)
                # Use QTimer to ensure waveform is drawn before setting offset
                QTimer.singleShot(
                    100,
                    lambda offset=song.offset: w.timeline.set_waveform_offset(offset),
                )
                self.logger.info(f"Applied saved audio offset: {song.offset:.3f}s")
            else:
                w.media_controller.set_audio_offset(0.0)
                QTimer.singleShot(100, lambda: w.timeline.set_waveform_offset(0.0))
        else:
            w.toolbar_manager.update_audio_file_display("")

        # Load MIDI file via MidiManager
        if song.midi:
            if w.midi_manager.load_from_file(song.midi):
                w.timeline.refresh_from_manager(detect_bpm=True)
                w.toolbar_manager.set_export_midi_enabled(True)
                w.toolbar_manager.update_midi_file_display(song.midi)
            else:
                self.logger.error(f"Failed to load MIDI file: {song.midi}")
                w.midi_manager.create_empty_track()
                w.timeline.refresh_from_manager()
                w.toolbar_manager.update_midi_file_display("")
        else:
            # Create empty MIDI track if no MIDI file
            w.midi_manager.create_empty_track()
            w.timeline.refresh_from_manager()
            w.toolbar_manager.update_midi_file_display("")

        w.toolbar_manager.update_song_info(
            song.title, song_status(song),
            bpm=w.timeline_toolbar.get_bpm(), offset=song.offset)
        w.update_window_title()

    # ------------------------------------------------------------------
    # Missing-media recovery
    # ------------------------------------------------------------------
    def _relink_roots(self):
        """Known-good folders to search a moved file in: the last manual
        relink dir, the show's audio/ and every folder that still holds some
        song's audio."""
        w = self.window
        roots = []
        if self._last_relink_dir:
            roots.append(self._last_relink_dir)
        show = w.show_manager.show
        if show is not None:
            roots.append(str(show.audio_dir))
        for s in w.config.playlist.songs:
            if s.audio and os.path.exists(s.audio):
                d = os.path.dirname(s.audio)
                if d not in roots:
                    roots.append(d)
        return roots

    def _offer_relink(self, song):
        """Recover a missing audio file: silent relink when the basename shows
        up in exactly one known-good root, else ask the user to locate it
        (their pick seeds the search roots for the remaining songs)."""
        w = self.window
        if song is not w.config.get_current_song():
            return  # the user already moved on to another song
        if not song.audio or os.path.exists(song.audio):
            return
        basename = os.path.basename(song.audio)
        candidates = find_by_basename(basename, self._relink_roots())
        if len(candidates) == 1:
            self._relink(song, candidates[0], auto=True)
            return

        reply = QMessageBox.question(
            w, "Missing audio",
            f"The audio file for '{song.title or basename}' is missing:\n"
            f"{song.audio}\n\nLocate it now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        start_dir = os.path.dirname(candidates[0]) if candidates \
            else os.path.dirname(song.audio)
        path, _ = QFileDialog.getOpenFileName(
            w, f"Locate {basename}", start_dir,
            "Audio files (*.mp3 *.wav);;All files (*)")
        if path:
            self._last_relink_dir = os.path.dirname(path)
            self._relink(song, path)

    def _relink(self, song, path, auto=False):
        w = self.window
        song.audio = path
        w.save_config()
        self.logger.info(
            f"Relinked audio {'automatically ' if auto else ''}to: {path}")
        self.apply_song_config(song)
        w.update_window_title()

    def update_song_counter(self):
        """Refresh the toolbar's setlist state: counter, nav buttons, the
        song-selector combobox and the show stats."""
        w = self.window
        songs = w.config.playlist.songs
        total = len(songs)
        index = w.config.playlist.current_song_index
        current = index + 1 if total > 0 else 0
        has_prev = current > 1
        has_next = current < total
        w.toolbar_manager.update_song_counter(current, total)
        w.toolbar_manager.set_setlist([s.title for s in songs], index)
        w.toolbar_manager.set_navigation_enabled(has_prev, has_next)
        w.toolbar_manager.update_show_stats()

    def next_song(self):
        """Switch to the next song in the playlist."""
        w = self.window
        total = len(w.config.playlist.songs)
        current = w.config.playlist.current_song_index
        if current < total - 1:
            # Stop playback and save pending edits before switching
            w.stop_playback()
            w.show_manager.flush("song-switch")

            w.config.set_current_song_index(current + 1)
            song = w.config.get_current_song()
            if song:
                self.apply_song_config(song)
            self.update_song_counter()
            w.save_config()
            self.logger.info(f"Switched to song {current + 2}/{total}")

    def prev_song(self):
        """Switch to the previous song in the playlist."""
        w = self.window
        current = w.config.playlist.current_song_index
        if current > 0:
            # Stop playback and save pending edits before switching
            w.stop_playback()
            w.show_manager.flush("song-switch")

            w.config.set_current_song_index(current - 1)
            song = w.config.get_current_song()
            if song:
                self.apply_song_config(song)
            self.update_song_counter()
            w.save_config()
            total = len(w.config.playlist.songs)
            self.logger.info(f"Switched to song {current}/{total}")

    def go_to_song(self, index):
        """Switch to a specific song by playlist index (e.g. tapping the web
        remote's setlist). No-op if the index is out of range or already
        current, so re-tapping the playing song never interrupts it."""
        w = self.window
        total = len(w.config.playlist.songs)
        current = w.config.playlist.current_song_index
        if not (0 <= index < total) or index == current:
            return

        # Stop playback and save pending edits before switching
        w.stop_playback()
        w.show_manager.flush("song-switch")

        w.config.set_current_song_index(index)
        song = w.config.get_current_song()
        if song:
            self.apply_song_config(song)
        self.update_song_counter()
        w.save_config()
        self.logger.info(f"Switched to song {index + 1}/{total}")

    def remove_current_song(self):
        """Remove the current song from the playlist."""
        w = self.window
        current = w.config.playlist.current_song_index
        total = len(w.config.playlist.songs)

        if total == 0:
            return

        # Get current song info for confirmation message
        current_song = w.config.get_current_song()
        song_name = (os.path.basename(current_song.audio)
                     if current_song and current_song.audio else "this song")

        # Show confirmation dialog
        reply = QMessageBox.question(
            w,
            "Remove Song",
            f"Are you sure you want to remove '{song_name}' from the playlist?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        # Stop playback and save pending edits before removing (the entry —
        # and its owned .mid — stays on disk; only the setlist forgets it)
        w.stop_playback()
        w.show_manager.flush("song-remove")

        w.config.remove_song(current)
        w.save_config()

        # Load the new current song (or show empty state)
        song = w.config.get_current_song()
        if song:
            self.apply_song_config(song)
        else:
            # No songs left - show empty state
            w.show_manager.begin_song_load()
            try:
                w.midi_manager.create_empty_track()
            finally:
                w.show_manager.end_song_load()
            w.timeline.refresh_from_manager()
            w.toolbar_manager.update_midi_file_display("")
            w.toolbar_manager.update_audio_file_display("")
            w.toolbar_manager.update_song_info("", "")
            w.timeline.set_files(None)

        self.update_song_counter()
        w.update_window_title()
        self.logger.info(
            f"Removed song from playlist. "
            f"{len(w.config.playlist.songs)} songs remaining."
        )
