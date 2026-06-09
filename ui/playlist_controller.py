# ABOUTME: PlaylistController — playlist navigation + per-song load orchestration.
# ABOUTME: Extracted from MainWindow; drives media/timeline/midi/toolbar per song.

import os

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QMessageBox

from config import SongConfig
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

    def apply_song_config(self, song):
        """Apply song configuration to all components."""
        w = self.window
        if not isinstance(song, SongConfig):
            return

        # Apply audio file
        if song.audio:
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

        w.update_window_title()

    def update_song_counter(self):
        """Update the song counter display in toolbar."""
        w = self.window
        total = len(w.config.playlist.songs)
        current = w.config.playlist.current_song_index + 1 if total > 0 else 0
        has_prev = current > 1
        has_next = current < total
        w.toolbar_manager.update_song_counter(current, total)
        w.toolbar_manager.set_navigation_enabled(has_prev, has_next)

    def next_song(self):
        """Switch to the next song in the playlist."""
        w = self.window
        total = len(w.config.playlist.songs)
        current = w.config.playlist.current_song_index
        if current < total - 1:
            # Stop playback before switching
            w.stop_playback()

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
            # Stop playback before switching
            w.stop_playback()

            w.config.set_current_song_index(current - 1)
            song = w.config.get_current_song()
            if song:
                self.apply_song_config(song)
            self.update_song_counter()
            w.save_config()
            total = len(w.config.playlist.songs)
            self.logger.info(f"Switched to song {current}/{total}")

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

        # Stop playback before removing
        w.stop_playback()

        w.config.remove_song(current)
        w.save_config()

        # Load the new current song (or show empty state)
        song = w.config.get_current_song()
        if song:
            self.apply_song_config(song)
        else:
            # No songs left - show empty state
            w.midi_manager.create_empty_track()
            w.timeline.refresh_from_manager()
            w.toolbar_manager.update_midi_file_display("")
            w.toolbar_manager.update_audio_file_display("")
            w.timeline.set_files(None)

        self.update_song_counter()
        w.update_window_title()
        self.logger.info(
            f"Removed song from playlist. "
            f"{len(w.config.playlist.songs)} songs remaining."
        )
