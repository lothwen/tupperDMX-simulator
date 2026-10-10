# ABOUTME: PlaylistController — the widget half of the playlist: timeline/
# ABOUTME: toolbar refresh per song load, missing-media relink dialogs,
# ABOUTME: navigation delegation to app.playlist.PlaylistCore, and the
# ABOUTME: playlist manager's route to it.

import os

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QFileDialog, QMessageBox

from core.paths import default_shows_dir, is_audio_path
from core.show import song_import, store
from core.show.model import song_status
from core.show.relink import find_by_basename
from tupperlogger import get_logger
from ui.error_dialogs import ErrorDialogManager
from ui.playlist_dialog import PlaylistDialog
from ui.widgets import icons


class PlaylistController:
    """The UI layer over PlaylistCore (window.playlist_core).

    Subscribes to the core's signals to refresh the timeline/toolbar per
    song, owns the missing-media recovery dialogs, and forwards navigation
    so existing call sites (toolbar buttons, web remote) keep working.
    """

    def __init__(self, window):
        self.window = window
        self.logger = get_logger("ui.playlist")
        # Last folder the user relinked from: moved guide folders usually move
        # together, so it seeds the auto-relink search for the other songs.
        self._last_relink_dir = None

        core = window.playlist_core
        core.song_loaded.connect(self._on_song_loaded)
        core.audio_missing.connect(self._on_audio_missing)
        core.setlist_changed.connect(self.update_song_counter)

    # ------------------------------------------------------------------
    # Delegation (toolbar buttons, web remote, MainWindow call sites)
    # ------------------------------------------------------------------
    def apply_song_config(self, song):
        self.window.playlist_core.load_song(song)

    def next_song(self):
        self.window.playlist_core.next_song()

    def prev_song(self):
        self.window.playlist_core.prev_song()

    def go_to_song(self, index):
        self.window.playlist_core.go_to_song(index)

    def add_empty_song(self):
        """Append an empty song and switch to it (SETLIST ▸ +)."""
        self.window.playlist_core.add_empty_song()

    # ------------------------------------------------------------------
    # Playlist manager (ui/playlist_dialog.py): every song, not only the
    # current one. The current song keeps its own routes (they reload the
    # engine and refresh the toolbar); any other song is plain data.
    # ------------------------------------------------------------------
    def open_manager(self):
        """The playlist manager: modal, applying every edit as it happens. A
        show opened meanwhile (the web remote can) closes it — its rows would
        be the old show's songs, and its index-based edits would land in the
        new one."""
        core = self.window.app_core
        dialog = PlaylistDialog(self, self.window)
        core.show_reloaded.connect(dialog.reject)
        dialog.exec()
        core.show_reloaded.disconnect(dialog.reject)
        dialog.deleteLater()

    def songs(self):
        return self.window.config.playlist.songs

    def current_song(self):
        return self.window.config.get_current_song()

    def show_name(self):
        show = self.window.show_manager.show
        return show.name if show is not None else ""

    def other_shows(self):
        """Show folders beside this one in the shows folder, to import from."""
        show = self.window.show_manager.show
        return song_import.other_shows(
            default_shows_dir(), show.root if show is not None else None)

    def patch(self):
        return self.window.config.fixtures

    def song_bpm(self, song):
        return self.window.playlist_core.song_bpm(song)

    def new_song(self):
        """Append an empty song WITHOUT switching to it; returns its index."""
        return self.window.playlist_core.add_empty_song(switch=False)

    def move_song(self, src, dst):
        self.window.playlist_core.move_song(src, dst)

    def song_owned_files(self, song):
        """The song's files deleting it with its files would remove."""
        return store.song_owned_files(self.window.show_manager.show, song)

    def remove_song(self, index, delete_files=False):
        """Returns the files that could not be deleted."""
        return self.window.playlist_core.remove_song(index, delete_files)

    def set_song_enabled(self, song, enabled):
        self.window.playlist_core.set_song_enabled(song, enabled)

    def set_song_title(self, song, title):
        if self.window.app_core.set_song_title(title, song=song):
            self.update_song_counter()
            if song is self.current_song():
                self._refresh_song_info(song)

    def set_song_key(self, song, key):
        self.window.app_core.set_song_key(key, song=song)
        if song is self.current_song():
            self._refresh_song_info(song)

    def set_song_precount(self, song, enabled):
        self.window.app_core.on_song_precount_toggled(enabled, song=song)

    def set_song_bpm(self, song, bpm):
        self.window.playlist_core.set_song_bpm(song, bpm)
        if song is self.current_song():
            self._refresh_song_info(song)   # a first .mid turns the chip

    def set_song_track(self, song, kind, path):
        """Set (path) or clear (None) a song's backing ('audio') or guide."""
        w = self.window
        if song is self.current_song():
            if path:
                w.load_media_file(kind, path)
            elif kind == "audio":
                w.clear_backing_track()
            else:
                w.clear_guide_track()
            return
        if path and not os.path.exists(path):
            ErrorDialogManager.show_media_file_error(w, path, "File not found")
            return
        if path and not is_audio_path(path):
            ErrorDialogManager.show_media_file_error(
                w, path, "Unsupported audio format. Please select an MP3 or "
                         "WAV file.")
            return
        w.playlist_core.set_song_track(song, kind, path)

    def import_songs(self, songs, copy_audio):
        """Append copies of another show's songs; returns (new, warnings)."""
        return self.window.playlist_core.import_songs(songs, copy_audio)

    def _refresh_song_info(self, song):
        """The SETLIST footer for the current song."""
        w = self.window
        w.toolbar_manager.update_song_info(
            song.title, song_status(song),
            bpm=w.bpm_state.get_bpm(), offset=song.offset,
            key=song.key)

    # ------------------------------------------------------------------
    # Widget refresh on song load (PlaylistCore.song_loaded)
    # ------------------------------------------------------------------
    def _on_song_loaded(self, song, midi_ok):
        """Reflect a (re)loaded song — or the empty state — on the widgets."""
        w = self.window

        if song is None:
            # No songs left / empty playlist
            w.timeline.refresh_from_manager()
            w.toolbar_manager.update_midi_file_display("")
            w.toolbar_manager.update_audio_file_display("")
            w.toolbar_manager.update_guide_file_display("")
            w.toolbar_manager.update_song_info("", "")
            w.timeline_toolbar.click_popup.set_precount(False, has_song=False)
            w.timeline_toolbar.set_monitor_state(w.media_controller.monitor, False)
            w.timeline.set_song_tracks(())
            w.timeline.set_files(None)
            w.timeline.set_voice_tts([])
            w.timeline.set_control_markers([])
            w.update_window_title()
            return

        # Guide row: display + one-shot silent relink (no dialog — the
        # interactive recovery flow stays backing-only).
        self._refresh_guide_display(song)

        # Audio row: path display (missing media keeps its path — the core
        # already emitted audio_missing for the recovery flow).
        if song.audio and not os.path.exists(song.audio):
            w.toolbar_manager.update_audio_file_display(
                f'{icons.img_tag("warning", "#e24a4a", 11)} '
                f"Missing audio: {song.audio}")
        else:
            w.toolbar_manager.update_audio_file_display(song.audio or "")

        # Timeline extent: the song's tracks, whichever is monitored — the
        # scene spans the LONGEST of them (and of the MIDI), so a short guide
        # can't trim a full-length song when the monitor toggles.
        w.timeline.set_song_tracks((song.audio, song.guide))

        # Waveform: the MONITORED track (backing or guide), which the core
        # resolved while loading the song — so a song carrying only a guide
        # still draws one.
        active = w.media_controller.active_file()
        if active:
            w.timeline.set_files(active)
            # Use QTimer to ensure waveform is drawn before setting offset
            QTimer.singleShot(
                100,
                lambda offset=w.media_controller.active_offset():
                    w.timeline.set_waveform_offset(offset),
            )
        else:
            w.timeline.set_files(None)

        # Marker track: ruler markers for the cues AND the control markers
        # (the PCM render and the transport half ride the core)
        w.refresh_timeline_markers(song)

        # MIDI: redraw tracks (BPM detection already ran in the core)
        w.timeline.refresh_from_manager()
        if midi_ok:
            w.toolbar_manager.set_export_midi_enabled(True)
            w.toolbar_manager.update_midi_file_display(song.midi)
        else:
            w.toolbar_manager.update_midi_file_display("")

        self._refresh_song_info(song)
        w.timeline_toolbar.click_popup.set_precount(
            song.precount, has_song=bool(song.title))
        w.timeline_toolbar.set_monitor_state(
            w.media_controller.monitor,
            bool(song.guide and os.path.exists(song.guide)))
        w.update_window_title()

    def _refresh_guide_display(self, song):
        """Guide-row state; a moved guide relinks silently when its basename
        shows up in exactly one known-good root."""
        w = self.window
        if song.guide and not os.path.exists(song.guide):
            candidates = find_by_basename(os.path.basename(song.guide),
                                          self._relink_roots())
            if len(candidates) == 1:
                song.guide = candidates[0]
                w.save_config()
                self.logger.info(f"Relinked guide automatically to: {candidates[0]}")
                w.media_controller.set_guide(song.guide, song.guide_offset)
                # The song HAS a guide after all — re-pick what's monitored.
                w.media_controller.auto_select_monitor()
            else:
                w.toolbar_manager.update_guide_file_display(
                    f'{icons.img_tag("warning", "#e24a4a", 11)} '
                    f"Missing guide: {song.guide}")
                return
        w.toolbar_manager.update_guide_file_display(song.guide or "")

    # ------------------------------------------------------------------
    # Missing-media recovery
    # ------------------------------------------------------------------
    def _on_audio_missing(self, song):
        # Try to recover once the event loop breathes.
        QTimer.singleShot(0, lambda s=song: self._offer_relink(s))

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
        w.playlist_core.load_song(song)
        w.update_window_title()

    # ------------------------------------------------------------------
    # Toolbar setlist state
    # ------------------------------------------------------------------
    def update_song_counter(self):
        """Refresh the toolbar's setlist state: counter, nav buttons, the
        song-selector combobox and the show stats."""
        w = self.window
        songs = w.config.playlist.songs
        total = len(songs)
        index = w.config.playlist.current_song_index
        current = index + 1 if total > 0 else 0
        has_prev = w.config.playlist.neighbour(-1) is not None
        has_next = w.config.playlist.neighbour(+1) is not None
        w.toolbar_manager.update_song_counter(current, total)
        w.toolbar_manager.set_setlist([s.title for s in songs], index,
                                      [s.enabled for s in songs])
        w.toolbar_manager.set_navigation_enabled(has_prev, has_next)
        w.toolbar_manager.update_show_stats()
