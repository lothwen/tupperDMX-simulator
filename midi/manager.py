from typing import Optional, List, Dict, Any, Iterable, Tuple
from contextlib import contextmanager
import logging
from PyQt6.QtCore import QObject, pyqtSignal
import mido
import copy

from tupperlogger import get_logger
from core.io_routing import MidiRouting
from .worker import MidiWorker, MidiConnectionStatus
from .document import MidiDocument
from .codec import MidiCodec, SIDECAR_MATCH_TOLERANCE
from .fade_engine import FadeEngine
from .clip_editor import ClipEditor
from .scene_editor import SceneEditor
from .selection_editor import SelectionEditor
from .constants import (
    # Re-exported for backward-compatible `from midi.manager import ...`
    # used by the UI modules and the test-suite; the manager body itself
    # no longer references these (they live in midi/constants.py).
    MIN_EVENT_DURATION,
    BLACKOUT_EPSILON,
    SONG_PART_NOTES,
    _channel_to_velocity,
)


class MidiManager(QObject):
    """
    Manages MIDI worker lifecycle and provides a high-level interface.
    """

    # Maximum number of undo/redo snapshots to retain.
    MAX_HISTORY = 50

    # Signals
    connection_status_changed = pyqtSignal(str, str)  # port_name, status
    error_occurred = pyqtSignal(str, str)  # port_name, error message
    playback_position_changed = pyqtSignal(float)  # current time
    worker_finished = pyqtSignal()
    data_changed = pyqtSignal()  # Emitted when MIDI data is modified

    def __init__(self, midi_note_map: Optional[Dict] = None, fixtures=None):
        super().__init__()
        self.current_worker: Optional[MidiWorker] = None
        self.current_filename: Optional[str] = None
        # Where playback sends MIDI (internal controller and/or external
        # port); set from config.io before playback starts.
        self.midi_routing = MidiRouting(to_controller=False, port=None)

        # Shared data model — the single source of truth (midi_data,
        # midi_events, midi_notes, midi_note_map) plus the low-level queries,
        # atomic mutators, and BLACKOUT reconciliation every editor builds on.
        # Manager code reaches the data through the proxy properties below.
        # The patch (config.fixtures) lets the doc route new events to the right
        # fixture instance and resolve per-fixture verbs against its def.
        self._doc = MidiDocument(midi_note_map, fixtures)

        # Serialization collaborator: mido.MidiFile <-> document lists.
        self._codec = MidiCodec()

        # Scene-fade collaborator: detect/generate ramps + fade UI setters.
        # Injected callbacks let it commit / notify without touching Qt.
        self._fades = FadeEngine(self._doc, commit=self._commit,
                                 notify_changed=self.data_changed.emit)

        # Editing collaborators (all share the document; commit via callback).
        self._clips = ClipEditor(self._doc, self._fades,
                                 commit=self._commit)
        self._scenes = SceneEditor(self._doc, self._fades, commit=self._commit)
        self._selection = SelectionEditor(self._doc, self._fades,
                                          commit=self._commit)

        # Track current playback position for in-place updates
        self._current_playback_time: float = 0.0

        self.logger = get_logger("midi.manager")

        # Software controller reference
        self.software_controller = None

        # Undo/redo snapshot history of the editable document state.
        # Seeded on load_from_file / create_empty_track.
        self._history: List[Dict[str, Any]] = []
        self._history_index: int = -1
        self._restoring: bool = False
        self._batching: bool = False
        self._batch_dirty: bool = False
        self._bulk: bool = False
        self._bulk_dirty: bool = False

        # In-memory clip clipboard (list of unit payloads). See copy_selection.
        self._clipboard: List[Dict[str, Any]] = []

    def set_software_controller(self, controller):
        """Set the software controller instance."""
        self.software_controller = controller
        if self.current_worker:
            self.current_worker.set_software_controller(controller)

    def set_midi_routing(self, routing: MidiRouting) -> None:
        """Set the playback MIDI routing. Takes effect on the next worker
        start; the MediaController restarts a live worker when playing."""
        self.midi_routing = routing
        self.logger.info(f"MIDI routing set: {routing}")

    # ------------------------------------------------------------------
    # Per-track auto mode (persisted in the .mid via note 71)
    # ------------------------------------------------------------------
    def auto_channels(self) -> set:
        """Channels whose persisted setup note has the auto flag (from the doc)."""
        return self._doc.auto_channels()

    def set_fixture_auto(self, channel, on: bool) -> bool:
        """Persist `channel`'s auto flag into the document (note 71) and mirror it
        onto the live controller, then commit (rebuild + worker + data_changed,
        which redraws the lane and lets it persist on the next Export).

        Distinct from DMXController.set_fixture_auto — that one is the runtime
        mirror; this is the durable, document-backed entry point."""
        if self.software_controller is not None:
            self.software_controller.set_fixture_auto(channel, on)
        if self._doc.set_fixture_auto_flag(channel, on):
            self._commit()
            return True
        return False

    def seed_controller_auto(self, controller=None) -> None:
        """Apply the document's persisted auto flags (note 71) onto `controller`
        (defaults to the attached one) — the runtime mirror of the durable
        per-track setup stored in the .mid."""
        controller = controller or self.software_controller
        if controller is not None:
            controller.apply_auto(self._doc.auto_channels())

    # ------------------------------------------------------------------
    # Per-track mute / lock (persisted alongside auto in the note-71 setup)
    # ------------------------------------------------------------------
    def mute_channels(self) -> set:
        """Channels whose persisted setup note has the mute flag (from the doc)."""
        return self._doc.mute_channels()

    def lock_channels(self) -> set:
        """Channels whose persisted setup note has the lock flag (from the doc)."""
        return self._doc.lock_channels()

    def is_fixture_muted(self, channel) -> bool:
        return channel in self._doc.mute_channels()

    def is_fixture_locked(self, channel) -> bool:
        return channel in self._doc.lock_channels()

    def set_fixture_mute(self, channel, on: bool) -> bool:
        """Persist `channel`'s mute flag (note 71) and suppress its live output:
        the worker skips messages on that MIDI channel. Commits so the lane
        redraws dimmed and the flag persists on the next Export. (_commit ->
        _auto_update_worker pushes the new muted set onto a running worker.)"""
        if self._doc.set_fixture_mute_flag(channel, on):
            self._commit()
            return True
        return False

    def set_fixture_lock(self, channel, on: bool) -> bool:
        """Persist `channel`'s lock flag (note 71) and commit so its lane
        redraws read-only (clips become non-interactive, drops are blocked)."""
        if self._doc.set_fixture_lock_flag(channel, on):
            self._commit()
            return True
        return False

    # ------------------------------------------------------------------
    # Per-fixture palette (persisted in the .mid via note 60 / START)
    # ------------------------------------------------------------------
    def fixture_palette(self, channel) -> int:
        """`channel`'s packed palette velocity (0 = the default palette)."""
        return self._doc.fixture_palette(channel)

    def set_fixture_palette(self, channel, velocity: int) -> bool:
        """Persist `channel`'s palette (note 60 at t=0) and commit, so the
        rebuilt .mid replays it at the top of the song and the show autosaves.

        Unlike the setup flags there is no live mirror to push: the palette only
        means anything once the note reaches the controller, and it does that on
        the next playback like every other note in the file."""
        if self._doc.set_fixture_palette(channel, velocity):
            self._commit()
            return True
        return False

    def _apply_muted_to_worker(self) -> None:
        """Push the current muted-channel set onto the running worker (if any),
        so muting takes effect mid-playback without a restart."""
        if self.current_worker is not None:
            self.current_worker.muted_channels = set(self._doc.mute_channels())

    # ------------------------------------------------------------------
    # MidiDocument bridge: data proxies + a few query delegations
    # ------------------------------------------------------------------
    # The mutable MIDI state lives on self._doc. These proxies keep the
    # old attribute surface (`midi_data`, `midi_events`, `midi_notes`,
    # `midi_note_map`) working for external consumers, and forward the
    # handful of queries (is_blackout / _event_fixture / song_end_time /
    # set_song_duration) the timeline and tests call on the manager.

    @property
    def midi_data(self):
        return self._doc.midi_data

    @midi_data.setter
    def midi_data(self, value):
        self._doc.midi_data = value

    @property
    def midi_events(self):
        return self._doc.midi_events

    @midi_events.setter
    def midi_events(self, value):
        self._doc.midi_events = value

    @property
    def midi_notes(self):
        return self._doc.midi_notes

    @midi_notes.setter
    def midi_notes(self, value):
        self._doc.midi_notes = value

    @property
    def midi_note_map(self):
        return self._doc.midi_note_map

    @midi_note_map.setter
    def midi_note_map(self, value):
        self._doc.midi_note_map = value

    def update_fixtures(self, fixtures) -> None:
        """Swap the patch at runtime so per-fixture verb resolution tracks
        designer edits (add/remove/channel/dmx)."""
        self._doc.update_fixtures(fixtures)

    def set_song_duration(self, duration: float) -> None:
        self._doc.set_song_duration(duration)

    def song_end_time(self) -> float:
        return self._doc.song_end_time()

    def detect_bpm(self) -> Optional[int]:
        """BPM from the loaded file's first set_tempo meta, or None when the
        file carries no tempo (the manually-set BPM then stays in effect)."""
        if not self.midi_data:
            return None
        for msg in self.midi_data:
            if msg.is_meta and msg.type == 'set_tempo':
                return round(mido.tempo2bpm(msg.tempo))
        return None

    def is_blackout(self, event: Dict[str, Any]) -> bool:
        return self._doc.is_blackout(event)

    def is_song_part(self, event: Dict[str, Any]) -> bool:
        """True if `event` is a global song-part marker (notes 61-69).

        Song parts reset every fixture, so the timeline shows them on every
        fixture track instead of scoping them to a single channel.
        """
        note = event.get('note') if event else None
        return note in SONG_PART_NOTES

    def _event_fixture(self, event: Dict[str, Any]):
        return self._doc._event_fixture(event)

    def channel_note_map(self, channel) -> Tuple[Tuple[str, int], ...]:
        """``((channel_field, note), ...)`` for the fixture at `channel`,
        falling back to the default fixture's def when unpatched."""
        return self._doc.channel_note_map(channel)

    # MIDI Data Management Methods  
    def load_from_file(self, file_path: str) -> bool:
        """Load MIDI data from file and parse it for UI display."""
        try:
            self.midi_data = self._codec.read(file_path)
            self.current_filename = file_path
            self._parse_midi_data()
            # Restore each track's persisted auto state onto the controller so the
            # 'A' buttons + auto visualization reflect the file when tracks rebuild.
            self.seed_controller_auto()
            self._reset_history()
            self.logger.info(f"Loaded MIDI file: {file_path}")
            self.data_changed.emit()
            return True
        except Exception as e:
            self.logger.error(f"Failed to load MIDI file {file_path}: {e}")
            return False

    def create_empty_track(self) -> None:
        """Create an empty MIDI track for new compositions."""
        self.midi_data = mido.MidiFile(type=1, ticks_per_beat=480)
        self.midi_data.tracks.append(mido.MidiTrack())
        self.midi_events.clear()
        self.midi_notes.clear()
        self._doc.sidecar_meta = []
        self.current_filename = None
        # No persisted setup notes -> clear any attached controller's auto state
        # so a MIDI-less song doesn't inherit the previous song's auto flags.
        self.seed_controller_auto()
        self._reset_history()
        self.logger.info("Created empty MIDI track")
        self.data_changed.emit()

    def set_midi_note_map(self, midi_note_map: Dict) -> None:
        """Update the MIDI note mapping and reparse data if available."""
        self.midi_note_map = midi_note_map
        if self.midi_data:
            self._parse_midi_data()
            self.data_changed.emit()

    # --- codec bridge -------------------------------------------------------

    def _parse_midi_data(self) -> None:
        """Parse loaded MIDI into events/notes, then rebuild scene groups + fades.

        The pure mido -> lists conversion lives in MidiCodec; this orchestrates
        the post-parse reconstruction in the order the heuristics require.
        """
        if not self.midi_data:
            return

        # Pure mido -> lists conversion.
        self._codec.parse_into(self._doc)

        # Detect fade-in/out runs in the loaded note-1 events and migrate
        # them out of midi_events into is_fade-tagged midi_notes entries.
        # MUST run before _reattach_scene_groups so the cluster builder
        # doesn't see the fade-in starter (a duplicate brightness event at
        # scene start with velocity 0) and corrupt the scene-name lookup.
        fade_runs = self._fades.detect_on_load()

        # Reattach scene groups: clusters of ≥2 channel-control events at
        # (essentially) the same time get a fresh shared group_id.
        self._scenes.reattach_groups()

        # Attach detected fade runs to the matching scene groups.
        self._fades.attach_runs_to_groups(fade_runs)

        # Re-apply authoring metadata persisted as tdmx1 sidecar metas —
        # corrections over the heuristics above, which stay canonical for
        # legacy files without metas.
        self._apply_sidecar_meta()

        # Ramps are derived from the fade durations that the two passes above
        # settled, so re-emit them all. This is also the migration: a file
        # written before a fade resolved the fixture's own dimmer has its ramp
        # on the wrong note and MIDI channel, and gets it rewritten here.
        self._fades.rebuild_all()

    def _apply_sidecar_meta(self) -> None:
        """Pin stable scene group ids + custom names, and restore fade
        durations (a clip's have no note representation in the file at all; a
        scene's are exact here, where the ramp heuristic only approximates
        them and misses entirely when the ramp was written to the wrong
        fixture)."""
        for meta in self._doc.sidecar_meta:
            at = meta.get("at")
            if not isinstance(at, (int, float)):
                continue
            if meta.get("t") == "scene":
                self._apply_scene_meta(meta, float(at))
            elif meta.get("t") == "clip":
                self._apply_clip_meta(meta, float(at))

    def _apply_scene_meta(self, meta: Dict[str, Any], at: float) -> None:
        ch = meta.get("ch")
        heuristic_gid = next(
            (e["group_id"] for e in self._doc.midi_events
             if e.get("group_id") and e.get("channel") == ch
             and abs(e["time"] - at) < SIDECAR_MATCH_TOLERANCE),
            None)
        if heuristic_gid is None:
            return  # externally edited / moved — heuristics stay in charge
        gid = str(meta.get("gid") or heuristic_gid)
        name = meta.get("name")
        fade_in = max(0.0, float(meta.get("fi", 0.0) or 0.0))
        fade_out = max(0.0, float(meta.get("fo", 0.0) or 0.0))
        for e in self._doc.midi_events:
            if e.get("group_id") == heuristic_gid:
                e["group_id"] = gid
                if isinstance(name, str) and name:
                    e["scene_name"] = name
                e["fade_in"] = fade_in
                e["fade_out"] = fade_out
        for n in self._doc.midi_notes:
            if n.get("parent_group_id") == heuristic_gid:
                n["parent_group_id"] = gid

    def _apply_clip_meta(self, meta: Dict[str, Any], at: float) -> None:
        note = meta.get("note")
        ch = meta.get("ch")
        for e in self._doc.midi_events:
            if (not e.get("group_id") and e.get("note") == note
                    and e.get("channel") == ch
                    and abs(e["time"] - at) < SIDECAR_MATCH_TOLERANCE):
                e["fade_in"] = max(0.0, float(meta.get("fi", 0.0) or 0.0))
                e["fade_out"] = max(0.0, float(meta.get("fo", 0.0) or 0.0))
                return

    def _rebuild_midi_data(self) -> None:
        self._codec.rebuild(self._doc)

    def export_to_file(self, file_path: str, bpm: Optional[int] = None,
                       interop: bool = False) -> bool:
        """Export current MIDI data to file (delegates to MidiCodec).
        interop=True writes a clean standard-MIDI copy without the tdmx1
        authoring metas (the Export MIDI… interchange path)."""
        return self._codec.export(self._doc, file_path, bpm, interop=interop)

    # --- public fade setters (delegated to FadeEngine) ---------------------

    def set_event_fade_in(self, event_id: str, seconds: float) -> bool:
        return self._fades.set_event_fade_in(event_id, seconds)

    def set_event_fade_out(self, event_id: str, seconds: float) -> bool:
        return self._fades.set_event_fade_out(event_id, seconds)

    def set_scene_fade_in(self, group_id: str, seconds: float) -> bool:
        return self._fades.set_scene_fade_in(group_id, seconds)

    def set_scene_fade_out(self, group_id: str, seconds: float) -> bool:
        return self._fades.set_scene_fade_out(group_id, seconds)

    def can_fade_scene(self, group_id: str) -> bool:
        """Whether this scene's fixture has a dimmer for a ramp to ride —
        false for an RGBW wash or a hazer, which the timeline reads to leave
        the fade handles off a clip that could never honour them."""
        return self._fades.can_fade_group(group_id)

    def can_fade_event(self, event_id: str) -> bool:
        """Whether this clip's verb takes a level a ramp could move — true for
        a direct-channel clip, false for a chase, a bank or a song part, whose
        velocity is an index rather than a brightness."""
        return self._fades.can_fade_event(event_id)

    # ------------------------------------------------------------------
    # Editing API — delegated to the clip / scene / selection editors
    # ------------------------------------------------------------------

    def add_event(self, note: int, time: float, velocity: int,
                  channel: Optional[int] = None) -> str:
        return self._clips.add_event(note, time, velocity, channel)

    def update_event_timing(self, event_id: str, new_time: float) -> bool:
        return self._clips.update_event_timing(event_id, new_time)

    def edit_event(self, event_id: str, **kwargs) -> bool:
        return self._clips.edit_event(event_id, **kwargs)

    def delete_event(self, event_id: str) -> bool:
        return self._clips.delete_event(event_id)

    def move_event(self, event_id: str, new_time: float) -> bool:
        return self._clips.move_event(event_id, new_time)

    def set_event_left_edge(self, event_id: str, new_time: float) -> bool:
        return self._clips.set_event_left_edge(event_id, new_time)

    def set_event_right_edge(self, event_id: str, new_right_time: float) -> bool:
        return self._clips.set_event_right_edge(event_id, new_right_time)

    def replace_with_blackout(self, event_id: str) -> bool:
        return self._clips.replace_with_blackout(event_id)

    def add_scene(self, time: float, channels: Dict[str, int],
                  name: str, channel: Optional[int] = None) -> Optional[str]:
        return self._scenes.add_scene(time, channels, name, channel)

    def move_scene(self, group_id: str, new_time: float) -> bool:
        return self._scenes.move_scene(group_id, new_time)

    def update_scene(self, group_id: str, channels: Dict[str, int],
                     name: Optional[str] = None, *,
                     remove: Optional[Iterable[str]] = None) -> bool:
        return self._scenes.update_scene(group_id, channels, name,
                                         remove=remove)

    def delete_scene(self, group_id: str) -> bool:
        return self._scenes.delete_scene(group_id)

    def set_scene_left_edge(self, group_id: str, new_time: float) -> bool:
        return self._scenes.set_scene_left_edge(group_id, new_time)

    def set_scene_right_edge(self, group_id: str, new_right_time: float) -> bool:
        return self._scenes.set_scene_right_edge(group_id, new_right_time)

    def selection_move_bounds(self, event_ids) -> Tuple[float, float]:
        return self._selection.selection_move_bounds(event_ids)

    def move_selection(self, event_ids, delta_sec: float) -> bool:
        return self._selection.move_selection(event_ids, delta_sec)

    def delete_selection(self, event_ids) -> bool:
        return self._selection.delete_selection(event_ids)

    def earliest_event_time(self):
        """Time of the first event, or None when the document is empty."""
        return self._doc.earliest_event_time()

    def shift_all(self, delta_sec: float) -> bool:
        """Slide the whole sequence by `delta_sec` as one undoable edit.

        The audio offset moved and the show is following it (see
        MainWindow.on_waveform_offset_committed): everything authored against
        the music has to travel the same distance, so this is a document-wide
        shift rather than a selection move.
        """
        if not self._doc.shift_all_events(delta_sec):
            return False
        self._doc.reconcile_blackouts()
        self._commit()
        self.logger.info(f"Shifted the whole sequence by {delta_sec:+.3f}s")
        return True

    def get_events_for_ui(self) -> List[Dict[str, Any]]:
        """Get MIDI events formatted for Timeline display."""
        return copy.deepcopy(self.midi_events)

    def get_duration(self) -> float:
        """Get the total duration of the MIDI data."""
        if self.midi_data:
            return self.midi_data.length
        return 0.0

    # ------------------------------------------------------------------
    # Undo / redo — linear snapshot history of the editable doc state
    # ------------------------------------------------------------------
    # Every committed mutation funnels through _commit(), so recording a
    # deep-copied snapshot of (midi_data, midi_events, midi_notes) there gives
    # undo/redo for the whole editing surface without the editors knowing.

    def _snapshot_state(self) -> Dict[str, Any]:
        return {
            "midi_data": copy.deepcopy(self._doc.midi_data),
            "midi_events": copy.deepcopy(self._doc.midi_events),
            "midi_notes": copy.deepcopy(self._doc.midi_notes),
        }

    def _reset_history(self) -> None:
        """Drop history and seed it with the current state as the baseline."""
        self._history = [self._snapshot_state()]
        self._history_index = 0

    def _push_history(self) -> None:
        """Record current state as a new undo step (dropping any redo tail)."""
        if self._history_index < len(self._history) - 1:
            del self._history[self._history_index + 1:]
        self._history.append(self._snapshot_state())
        if len(self._history) > self.MAX_HISTORY:
            del self._history[:len(self._history) - self.MAX_HISTORY]
        self._history_index = len(self._history) - 1

    def _apply_history_state(self, snap: Dict[str, Any]) -> None:
        """Restore doc state from a snapshot, refresh worker + UI (no push)."""
        self._restoring = True
        try:
            self._doc.midi_data = copy.deepcopy(snap["midi_data"])
            self._doc.midi_events = copy.deepcopy(snap["midi_events"])
            self._doc.midi_notes = copy.deepcopy(snap["midi_notes"])
            # The auto flags just came back with the note-71 setup notes; the
            # live controller is their runtime mirror (and what the lane's 'A'
            # button shows), so it has to follow them.
            self.seed_controller_auto()
            self._auto_update_worker()
            self.data_changed.emit()
        finally:
            self._restoring = False

    def can_undo(self) -> bool:
        return self._history_index > 0

    def can_redo(self) -> bool:
        return self._history_index < len(self._history) - 1

    def undo(self) -> bool:
        if not self.can_undo():
            return False
        self._history_index -= 1
        self._apply_history_state(self._history[self._history_index])
        self.logger.debug(f"Undo -> history index {self._history_index}")
        return True

    def redo(self) -> bool:
        if not self.can_redo():
            return False
        self._history_index += 1
        self._apply_history_state(self._history[self._history_index])
        self.logger.debug(f"Redo -> history index {self._history_index}")
        return True

    @contextmanager
    def history_batch(self):
        """Group edits inside this block into a single undo step.

        Pushes one snapshot on exit, but only if at least one commit actually
        happened inside (so a batch that mutates nothing leaves no empty step).
        """
        if self._batching:
            yield
            return
        self._batching = True
        self._batch_dirty = False
        try:
            yield
        finally:
            self._batching = False
            if self._batch_dirty and not self._restoring:
                self._push_history()

    @contextmanager
    def bulk_edit(self):
        """Many edits as one undo step, with the work each edit otherwise
        repeats done once, when the block ends: rebuilding the .mid,
        refreshing the worker, notifying data_changed and re-deriving the
        fade ramps of every lane an edit touched. For authoring at scale —
        the auto program places thousands of clips, where that per-edit work
        costs minutes.

        Inside the block the document's event lists are exact; midi_data, and
        the file length song_end_time() reads from it, stay as the block found
        them. A scene's fade setter still writes its own ramp at once.

        The editors' line per edit is held back to warnings: it narrates a
        hand edit, and a block authors thousands. Their loggers are swapped on
        this manager alone, so another manager's edits still log meanwhile.
        """
        if self._bulk:
            yield
            return
        self._bulk, self._bulk_dirty = True, False
        editors = (self._clips, self._scenes, self._selection)
        loggers = [editor.logger for editor in editors]
        for editor, logger in zip(editors, loggers):
            editor.logger = get_logger(f"{logger.name}.bulk")
            editor.logger.setLevel(logging.WARNING)
        self._fades.defer_lanes()
        with self.history_batch():
            try:
                yield
            finally:
                self._bulk = False
                for editor, logger in zip(editors, loggers):
                    editor.logger = logger
                self._fades.regenerate_deferred()
                if self._bulk_dirty:
                    self._commit()

    # ------------------------------------------------------------------
    # Clip clipboard — copy/paste of clips and scene groups
    # ------------------------------------------------------------------

    def has_clipboard(self) -> bool:
        return bool(self._clipboard)

    def copy_selection(self, event_ids) -> int:
        """Capture selected clips / scene groups into the clipboard.

        Each unit stores its start time RELATIVE to the earliest selected clip
        (so paste can drop the block at any playhead position), its visible
        width (None when it extends to the song end), and its fades. Scene
        groups store per-channel values; single clips store note + velocity.
        BLACKOUT gap markers are skipped. Returns the number of units captured.
        """
        selected_ids = set(event_ids)
        group_ids: List[str] = []
        singles: List[Dict[str, Any]] = []
        seen_groups = set()
        for e in self._doc.midi_events:
            if e['id'] not in selected_ids or self._doc.is_blackout(e):
                continue
            gid = e.get('group_id')
            if gid:
                if gid not in seen_groups:
                    seen_groups.add(gid)
                    group_ids.append(gid)
            else:
                singles.append(e)
        if not group_ids and not singles:
            return 0

        starts: List[float] = []
        for gid in group_ids:
            members = self._doc._group_members(gid)
            if members:
                starts.append(members[0]['time'])
        starts.extend(e['time'] for e in singles)
        sel_start = min(starts)

        units: List[Dict[str, Any]] = []

        for gid in group_ids:
            members = self._doc._group_members(gid)
            if not members:
                continue
            fixture = self._doc._event_fixture(members[0])
            channel = members[0].get('channel')
            note_to_ch = {note: ch
                          for ch, note in self._doc.channel_note_map(channel)}
            start = members[0]['time']
            right = self._doc._group_right_edge(gid, fixture)
            width = None if right == float('inf') else max(0.0, right - start)
            channels: Dict[str, int] = {}
            for m in members:
                ch = note_to_ch.get(m['note'])
                if ch is not None:
                    # *2 round-trips through _channel_to_velocity (value // 2).
                    channels[ch] = int(m['velocity']) * 2
            units.append({
                'kind': 'scene',
                'rel_time': start - sel_start,
                'width': width,
                'fade_in': float(members[0].get('fade_in', 0.0)),
                'fade_out': float(members[0].get('fade_out', 0.0)),
                'channels': channels,
                'name': members[0].get('scene_name') or 'Scene',
                'channel': channel,
            })

        for e in singles:
            fixture = self._doc._event_fixture(e)
            idx = self._doc._index_of(e['id'])
            right = (self._doc._right_edge_of(idx, fixture)
                     if idx >= 0 else float('inf'))
            width = None if right == float('inf') else max(0.0, right - e['time'])
            units.append({
                'kind': 'single',
                'rel_time': e['time'] - sel_start,
                'width': width,
                'fade_in': float(e.get('fade_in', 0.0)),
                'fade_out': float(e.get('fade_out', 0.0)),
                'note': e['note'],
                'velocity': int(e['velocity']),
                'channel': e.get('channel'),
            })

        self._clipboard = units
        self.logger.info(f"Copied {len(units)} clip unit(s) to clipboard")
        return len(units)

    def clipboard_span(self) -> float:
        """Length of the clipboard block: first unit's start to last unit's end.

        This is what a repeat-paste steps by, so a copied section repeats as a
        loop. A unit whose clip runs to the song end has no end to measure
        (`width` is None) and contributes only its start; a block made purely
        of those spans 0.0, and the caller then has nothing to step by.
        """
        span = 0.0
        for unit in self._clipboard:
            width = unit.get('width')
            end = float(unit['rel_time']) + (0.0 if width is None else float(width))
            span = max(span, end)
        return span

    def paste(self, at_time: float) -> bool:
        """Recreate clipboard units starting at at_time, as one undo step.

        Reuses add_event / add_scene (which handle BLACKOUT reconciliation) and
        the existing right-edge / fade setters. Units are pasted left-to-right
        so each clip's trailing cap is inserted with the next clip already in
        place, reconstructing internal gaps.
        """
        if not self._clipboard:
            return False
        at_time = max(0.0, at_time)
        pasted = 0
        with self.history_batch():
            for unit in sorted(self._clipboard, key=lambda u: u['rel_time']):
                target = at_time + unit['rel_time']
                if unit['kind'] == 'scene':
                    gid = self.add_scene(target, unit['channels'], unit['name'],
                                         unit.get('channel'))
                    if not gid:
                        continue
                    if unit['width'] is not None:
                        self.set_scene_right_edge(gid, target + unit['width'])
                    if unit['fade_in'] > 0:
                        self.set_scene_fade_in(gid, unit['fade_in'])
                    if unit['fade_out'] > 0:
                        self.set_scene_fade_out(gid, unit['fade_out'])
                    pasted += 1
                else:
                    eid = self.add_event(unit['note'], target, unit['velocity'],
                                         unit.get('channel'))
                    if not eid:
                        continue
                    if unit['width'] is not None:
                        self.set_event_right_edge(eid, target + unit['width'])
                    if unit['fade_in'] > 0:
                        self.set_event_fade_in(eid, unit['fade_in'])
                    if unit['fade_out'] > 0:
                        self.set_event_fade_out(eid, unit['fade_out'])
                    pasted += 1
        if pasted:
            self.logger.info(f"Pasted {pasted} clip unit(s) at {at_time:.3f}s")
        return pasted > 0

    def _commit(self) -> None:
        """Final step of any mutation: rebuild MIDI, push to worker, notify."""
        if self._bulk:
            self._bulk_dirty = True          # bulk_edit() does all of it once, at its end
            return
        self._rebuild_midi_data()
        self._auto_update_worker()
        self.data_changed.emit()
        if self._restoring:
            return
        if self._batching:
            self._batch_dirty = True
        else:
            self._push_history()

    def _auto_update_worker(self) -> None:
        """Automatically update the worker with current MIDI data if running."""
        if self.current_worker and self.midi_data:
            # Try to update worker in-place to preserve playback position
            if self._update_worker_in_place():
                self.logger.debug("Updated worker MIDI data in-place")
            else:
                # Fallback to restarting if in-place update fails
                self.logger.debug("In-place update failed, restarting worker with modified MIDI data")
                self.start_worker(self.current_filename or "modified", self.midi_data)
            # Re-seed muted channels from the doc on every worker refresh so
            # mute toggles — and undo/redo of them — take effect immediately.
            self._apply_muted_to_worker()

    def _update_worker_in_place(self) -> bool:
        """Update the worker's MIDI data without restarting, preserving playback position."""
        if not self.current_worker or not self.current_worker.isRunning():
            return False

        # Get current playback position
        current_time = self._get_current_playback_time()

        # Safety check: if MIDI data is None or empty, we can't do in-place update
        if not self.midi_data:
            self.logger.warning("Cannot update worker in-place: no MIDI data available")
            return False

        # Update the worker's MIDI data
        success = self.current_worker.update_midi_data(self.midi_data, current_time)

        if success:
            self.logger.debug(f"Successfully updated worker MIDI data in-place at position {current_time:.2f}s")
        else:
            self.logger.warning("In-place MIDI data update failed, will need to restart worker")

        return success

    def _get_current_playback_time(self) -> float:
        """Get the current playback time from the MediaController."""
        # We need a way to get the current playback position
        # This will be set by the MediaController when it calls play_until()
        return getattr(self, '_current_playback_time', 0.0)

    def set_current_playback_time(self, time_seconds: float) -> None:
        """Set the current playback time (called by MediaController)."""
        self._current_playback_time = time_seconds

    # Enhanced playback methods

    def start_playback(self) -> bool:
        """Start MIDI playback with current data and the current routing."""
        if not self.midi_data:
            self.logger.warning("No MIDI data available for playback")
            return False

        return self.start_worker(self.current_filename or "current", self.midi_data)

    def start_worker(self, filename: str, midi_data: Optional[mido.MidiFile] = None) -> bool:
        """
        Start a MIDI worker with the specified file and the current routing.

        Args:
            filename: Path to MIDI file (used for identification)
            midi_data: Optional MidiFile object to use instead of loading from file

        Returns:
            True if worker started successfully
        """
        # Stop any existing worker
        self.stop_worker()

        # Use current MIDI data if available and no specific data provided
        if not midi_data and self.midi_data:
            midi_data = self.midi_data

        # Validate inputs
        if not filename and not midi_data:
            self.error_occurred.emit("Unknown", "No MIDI file or data specified")
            return False

        routing = self.midi_routing
        if not routing.has_output():
            self.logger.warning("No MIDI outputs configured; not starting worker")
            return False

        # Fail fast only when the external port is the sole destination; with
        # the controller also routed the worker keeps that leg alive and the
        # port failure surfaces through the error dialog.
        if routing.port and not routing.to_controller:
            available_ports = MidiWorker.get_available_ports()
            if routing.port not in available_ports:
                self.error_occurred.emit(routing.port, f"MIDI port not available. Available ports: {available_ports}")
                return False

        port_label = routing.port or "Internal controller"

        # Create and start worker
        try:
            worker = MidiWorker(filename, routing.port, midi_data,
                                route_to_controller=routing.to_controller)
            if self.software_controller:
                worker.set_software_controller(self.software_controller)
            elif routing.to_controller:
                self.logger.warning(
                    "Routing to internal controller but none attached yet")
            # Seed persisted per-track mutes so a fresh worker starts silent on
            # muted fixtures (note 71 mute bit -> skipped MIDI channels).
            worker.muted_channels = set(self._doc.mute_channels())

            # Connect signals
            worker.connection_status_changed.connect(
                lambda status: self.connection_status_changed.emit(port_label, status)
            )
            worker.error_occurred.connect(
                lambda error: self.error_occurred.emit(port_label, error)
            )
            worker.playback_position_changed.connect(self.playback_position_changed.emit)
            worker.finished.connect(self.worker_finished.emit)
            worker.finished.connect(worker.deleteLater)

            # Store references
            self.current_worker = worker
            self.current_filename = filename

            # Reset playback time tracking when starting new worker
            self._current_playback_time = 0.0

            # Start the worker thread
            worker.start()

            self.logger.info(f"Started MIDI worker for {filename} ({routing})")
            return True

        except Exception as e:
            self.logger.error(f"Failed to start MIDI worker: {e}")
            self.error_occurred.emit(port_label, f"Failed to start MIDI worker: {e}")
            return False

    def stop_worker(self) -> None:
        """Stop the current MIDI worker."""
        if self.current_worker:
            self.logger.info(f"Stopping MIDI worker for {self.current_filename}")

            # Stop the worker
            self.current_worker.stop()

            # Wait for thread to finish (with timeout)
            if self.current_worker.isRunning():
                self.current_worker.wait(3000)  # 3 second timeout

                if self.current_worker.isRunning():
                    self.logger.warning("MIDI worker thread did not stop gracefully, terminating")
                    self.current_worker.terminate()
                    self.current_worker.wait(1000)

            # Clean up references
            self.current_worker = None
            self.current_filename = None

            self.logger.info("MIDI worker stopped")

    def play_until(self, timeline_time_sec: float) -> None:
        """Request playback up to the specified timeline position."""
        if self.current_worker:
            # Track the current playback time for in-place updates
            self._current_playback_time = timeline_time_sec
            self.current_worker.play_until(timeline_time_sec)

    def seek(self, time_seconds: float) -> None:
        """Seek to a specific time position."""
        if self.current_worker:
            # Track the current playback time for in-place updates
            self._current_playback_time = time_seconds
            self.current_worker.seek(time_seconds)

    def get_current_filename(self) -> Optional[str]:
        """Get the current MIDI filename."""
        return self.current_filename

    def get_current_status(self) -> Optional[MidiConnectionStatus]:
        """Get the current connection status."""
        if self.current_worker:
            return self.current_worker.get_status()
        return None

    def is_running(self) -> bool:
        """Check if a MIDI worker is currently running."""
        return self.current_worker is not None and self.current_worker.isRunning()

    def is_connected(self) -> bool:
        """Check if currently connected to a MIDI port."""
        status = self.get_current_status()
        return status == MidiConnectionStatus.CONNECTED if status else False

    def get_available_ports(self) -> list[str]:
        """Get list of all available MIDI output ports."""
        return MidiWorker.get_available_ports()

    def get_connection_info(self) -> Optional[dict]:
        """Get detailed connection information."""
        if self.current_worker:
            return {
                'type': 'MIDI',
                'filename': self.current_filename,
                'routing': str(self.midi_routing),
                'status': self.current_worker.get_status().value,
                'message_count': self.current_worker.get_message_count(),
                'duration': self.current_worker.get_duration()
            }
        return None

    def reload_file(self, filename: Optional[str] = None) -> bool:
        """
        Reload the current MIDI file or load a new one.

        Args:
            filename: New filename, or None to reload current file

        Returns:
            True if reload was successful
        """
        if not self.midi_routing.has_output():
            return False

        if filename:
            # Loading a new file
            return self.load_from_file(filename) and self.start_playback()
        else:
            # Reloading current data
            return self.start_playback()

    def cleanup(self) -> None:
        """Clean up resources when shutting down."""
        self.stop_worker()