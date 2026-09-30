# ABOUTME: ShowManager — Qt-side coordinator of the open show project: aliases
# ABOUTME: Config onto the show's playlist/patch, debounced autosave of the
# ABOUTME: active song's .mid, and the manifest save funnel (flush).
import json
import os
import shutil
import time
import uuid
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from core import clips, fixture_library
from core.config import SongConfig
from core.show import store, versions
from core.show.manifest import show_to_manifest
from core.show.migrate import remember_show
from core.show.model import Show
from midi.codec import MidiCodec
from tupperlogger import get_logger
from webremote import state as remote_state

AUTOSAVE_DEBOUNCE_MS = 3000


class ShowManager(QObject):
    """Owns the open Show on the Qt side (WebRemoteManager pattern).

    attach() aliases config.playlist/config.fixtures to the show's own
    objects, so every existing reader/mutator (PlaylistController,
    StageController, DMXManager, clip library, web remote, ...) edits the
    show directly without changing a line.

    Persistence funnels through flush(): the active song's .mid is exported
    (atomic, via the codec) whenever timeline edits marked it dirty, and the
    manifest is saved iff its serialized content changed — so config.save()
    call sites that only touch machine prefs never rewrite show.json. Timeline
    edits debounce an autosave (~3 s after the last edit); song switches and
    app close flush unconditionally."""

    show_changed = pyqtSignal()
    autosave_state_changed = pyqtSignal(str)  # "saved" | "pending" | "error"
    versions_changed = pyqtSignal()           # the snapshot list gained/lost an entry
    midi_path_allocated = pyqtSignal(str)     # a song got its owned .mid on first save
    setlist_changed = pyqtSignal()            # flush created an untitled song
    favorites_changed = pyqtSignal()          # a library clip was starred/unstarred

    def __init__(self, config, core=None, snapshot_on_open=True):
        super().__init__()
        self.config = config
        self.core = core
        self.snapshot_on_open = snapshot_on_open
        self.logger = get_logger("show.manager")
        self.show: Optional[Show] = None
        self._last_manifest_json: Optional[str] = None
        self._midi_dirty = False
        self._loading = False
        self._last_snapshot_monotonic = 0.0

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(AUTOSAVE_DEBOUNCE_MS)
        self._autosave_timer.timeout.connect(self._on_autosave_timeout)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def attach(self, show: Show) -> None:
        """Adopt `show` as the open project. The show-local fixture-defs
        layer is already active (store.load_show/create_show set it). Takes
        the opening safety snapshot (crash-recovery baseline) unless
        `snapshot_on_open` is off."""
        self.show = show
        self.config.playlist = show.playlist
        self.config.fixtures = show.fixtures
        self._midi_dirty = False
        self._last_manifest_json = self._serialized()
        if self.snapshot_on_open:
            try:
                versions.take_snapshot(show)
                versions.prune(show)
            except Exception as e:
                self.logger.error(f"Opening snapshot failed: {e}")
        # Set either way: it paces _maybe_snapshot, so leaving it at 0 would
        # let the first real change checkpoint immediately.
        self._last_snapshot_monotonic = time.monotonic()
        self.show_changed.emit()
        self.versions_changed.emit()

    def restore_version(self, version_id: str) -> Show:
        """Roll show.json + midis/ back to a snapshot (the pre-restore state
        is snapshotted first) and re-open the show from disk."""
        if self.show is None:
            raise RuntimeError("No show attached")
        versions.restore(self.show, version_id)
        show = store.load_show(self.show.root)
        self.attach(show)
        if self.core is not None:
            self.core.reload_from_show()
        self.logger.info(f"Restored version {version_id}")
        return show

    def open_show(self, path) -> Show:
        """Flush the current show, load the one at `path`, adopt it and
        rebuild the window. Raises ShowError when it can't be loaded — the
        current show stays attached (and its fixture-defs layer restored)."""
        if self.show is not None:
            self.flush("switch-show")
        try:
            show = store.load_show(path)
        except Exception:
            if self.show is not None:
                fixture_library.set_show_fixtures_dir(self.show.fixtures_dir)
            raise
        self.attach(show)
        remember_show(self.config, show.root)
        self.config.save()
        if self.core is not None:
            self.core.reload_from_show()
        self.logger.info(f"Opened show: {show.root}")
        return show

    def begin_song_load(self) -> None:
        """Suppress dirty-marking while a song is being (re)loaded — loading
        emits the same data_changed/bpm_changed signals as editing."""
        self._loading = True

    def end_song_load(self) -> None:
        self._loading = False

    # ------------------------------------------------------------------
    # Edit tracking (wired to midi_manager.data_changed / toolbar bpm_changed)
    # ------------------------------------------------------------------
    def on_midi_data_changed(self) -> None:
        if self._loading or self.show is None:
            return
        self._midi_dirty = True
        self.autosave_state_changed.emit("pending")
        self._autosave_timer.start()

    def on_bpm_changed(self, _bpm: int) -> None:
        # The BPM is canonical in the .mid (set_tempo), so it rides the same
        # dirty flag and re-export path as clip edits.
        self.on_midi_data_changed()

    def _on_autosave_timeout(self) -> None:
        # Don't export mid-performance: retry after the next debounce window
        # (a pause/stop lands within one interval).
        mc = getattr(self.core, "media_controller", None)
        if mc is not None and mc.get_playback_state():
            self._autosave_timer.start()
            return
        self.flush("autosave")

    # ------------------------------------------------------------------
    # Persistence funnel
    # ------------------------------------------------------------------
    def notify_changed(self) -> None:
        """Funnel from MainWindow.save_config(): persist the manifest iff
        its content actually changed."""
        try:
            self._save_manifest_if_changed()
        except Exception as e:
            self.logger.error(f"Failed to save show manifest: {e}")
            self.autosave_state_changed.emit("error")

    def flush(self, reason: str = "") -> None:
        """Guaranteed save point (debounced autosave, song switch, app close,
        manual Save): active song's .mid first, then the manifest."""
        if self.show is None:
            return
        self._autosave_timer.stop()
        try:
            wrote = self._flush_midi()
            wrote |= self._save_manifest_if_changed()
        except Exception as e:
            self.logger.error(f"Show flush failed: {e}")
            self.autosave_state_changed.emit("error")
            return
        self.autosave_state_changed.emit("saved")
        if wrote:
            self._maybe_snapshot()
        if reason:
            self.logger.debug(f"Show flushed ({reason})")

    def _maybe_snapshot(self) -> None:
        """Coarse history checkpoints: at most one automatic snapshot per
        SNAPSHOT_MIN_INTERVAL_S of actual writes, pruned to the retention."""
        if time.monotonic() - self._last_snapshot_monotonic \
                < versions.SNAPSHOT_MIN_INTERVAL_S:
            return
        try:
            versions.take_snapshot(self.show)
            versions.prune(self.show)
        except Exception as e:
            self.logger.error(f"Automatic snapshot failed: {e}")
            return
        self._last_snapshot_monotonic = time.monotonic()
        self.versions_changed.emit()

    def save_manifest_if_changed(self) -> bool:
        """notify_changed() semantics, but propagating the result (tests)."""
        try:
            return self._save_manifest_if_changed()
        except Exception as e:
            self.logger.error(f"Failed to save show manifest: {e}")
            self.autosave_state_changed.emit("error")
            return False

    def _save_manifest_if_changed(self) -> bool:
        if self.show is None:
            return False
        blob = self._serialized()
        if blob == self._last_manifest_json:
            return False
        store.save_manifest(self.show)
        self._last_manifest_json = blob
        return True

    def _flush_midi(self) -> bool:
        """Export the in-memory MIDI document onto the current song's owned
        .mid (allocating midis/<slug>.mid on first save). Returns True when a
        file was written."""
        if not self._midi_dirty or self.core is None:
            return False
        mm = self.core.midi_manager
        if mm.midi_data is None:
            self._midi_dirty = False
            return False

        song = self.config.get_current_song()
        has_events = bool(mm.midi_events)
        if song is None:
            if not has_events:
                self._midi_dirty = False
                return False
            # Edits with an empty setlist still deserve a home: they become
            # an audio-less song the user can attach a guide to later.
            song = self._create_untitled_song()
        if not song.midi:
            if not has_events:
                self._midi_dirty = False
                return False
            self.allocate_song_midi(song)

        bpm = self.core.bpm_state.get_bpm()
        if not mm.export_to_file(song.midi, bpm):
            raise RuntimeError(f"export_to_file('{song.midi}') returned False")
        self._update_song_cache(song, bpm)
        self._midi_dirty = False
        return True

    def allocate_song_midi(self, song: SongConfig) -> str:
        """Give the song its owned midis/<slug>.mid path (not yet written)."""
        song.midi = store.allocate_midi_path(self.show, song.title or "song")
        self.midi_path_allocated.emit(song.midi)
        return song.midi

    def write_song_tempo(self, song: SongConfig, bpm: int) -> bool:
        """Set a song's tempo in its .mid on disk, creating a tempo-only one
        when there is none. For a song whose .mid is not loaded: the current
        song's tempo is bpm_state's, flushed from memory over this file (the
        one exception is giving an event-less current song its first file)."""
        if self.show is None:
            return False
        if not song.midi:
            # Not allocate_song_midi: its signal speaks for the CURRENT song.
            song.midi = store.allocate_midi_path(self.show, song.title or "song")
        if not MidiCodec().write_file_tempo(song.midi, bpm):
            return False
        self._update_song_cache(song, bpm)
        self.notify_changed()
        return True

    def _create_untitled_song(self) -> SongConfig:
        song = SongConfig(id=str(uuid.uuid4()), title="Untitled")
        self.config.playlist.songs.append(song)
        self.config.playlist.current_song_index = len(self.config.playlist.songs) - 1
        self.setlist_changed.emit()
        return song

    def _update_song_cache(self, song: SongConfig, bpm: int) -> None:
        """Refresh the derived (never hand-edited) manifest cache for fast
        setlist display: canonical values stay in the .mid / audio file."""
        mtime = None
        if song.audio and os.path.exists(song.audio):
            mtime = os.path.getmtime(song.audio)
        song.cache = {
            "bpm": int(bpm),
            "duration": remote_state.probe_duration(song.audio),
            "audio_mtime": mtime,
        }

    # ------------------------------------------------------------------
    # Song MIDI import (the show owns its sequences)
    # ------------------------------------------------------------------
    def import_midi_copy(self, path: str) -> Optional[str]:
        """Copy an external .mid into midis/ and return the owned copy's
        path (the user's original stays untouched)."""
        if self.show is None:
            return None
        title = os.path.splitext(os.path.basename(path))[0]
        dest = store.allocate_midi_path(self.show, title)
        shutil.copy2(path, dest)
        return dest

    # ------------------------------------------------------------------
    # Favourites (the starred library clips — see core/clips.py)
    # ------------------------------------------------------------------
    def favorites(self) -> List[Dict]:
        """The show's starred clip refs, in grid order. Empty without a show:
        favourites are show content, so there is no machine-level fallback."""
        return self.show.favorites if self.show is not None else []

    def is_favorite(self, ref: Dict) -> bool:
        key = clips.ref_key(ref)
        return any(clips.ref_key(f) == key for f in self.favorites())

    def toggle_favorite(self, ref: Dict) -> bool:
        """Star/unstar one clip; returns the new state. Starring appends, so
        the grid order is the order they were starred in."""
        if self.show is None:
            return False
        key = clips.ref_key(ref)
        existing = [f for f in self.show.favorites if clips.ref_key(f) == key]
        if existing:
            self.show.favorites = [f for f in self.show.favorites
                                   if clips.ref_key(f) != key]
            starred = False
        else:
            self.show.favorites.append(dict(ref))
            starred = True
        self.favorites_changed.emit()
        self.notify_changed()
        return starred

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    def _serialized(self) -> str:
        return json.dumps(show_to_manifest(self.show), sort_keys=True)
