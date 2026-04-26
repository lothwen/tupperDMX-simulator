from typing import Optional, List, Dict, Any, Callable, Tuple
from PyQt6.QtCore import QObject, pyqtSignal
import mido
import uuid
import copy

from tupperlogger import get_logger
from .worker import MidiWorker, MidiConnectionStatus


# Identification of BLACKOUT events is by part name (case-insensitive),
# not by note number — keeps it config-driven.
BLACKOUT_PART_NAME = "BLACKOUT"

# Minimum non-BLACKOUT clip duration in seconds. Below this, an event is too
# short to remain a valid clip and gets clamped during edits.
MIN_EVENT_DURATION = 0.01

# A BLACKOUT whose effective duration (distance to its next event) is below
# this threshold gets removed by reconcile_blackouts().
BLACKOUT_EPSILON = 1e-4

# Tolerance used when matching a midi_notes entry to its midi_events parent
# by (note_number, time). Must be larger than BLACKOUT_EPSILON to survive
# tiny drift from float arithmetic during a series of edits.
NOTE_MATCH_TOLERANCE = 0.005


class MidiManager(QObject):
    """
    Manages MIDI worker lifecycle and provides a high-level interface.
    """
    
    # Signals
    connection_status_changed = pyqtSignal(str, str)  # port_name, status
    error_occurred = pyqtSignal(str, str)  # port_name, error message
    playback_position_changed = pyqtSignal(float)  # current time
    worker_finished = pyqtSignal()
    data_changed = pyqtSignal()  # Emitted when MIDI data is modified
    
    def __init__(self, midi_note_map: Optional[Dict] = None):
        super().__init__()
        self.current_worker: Optional[MidiWorker] = None
        self.current_filename: Optional[str] = None
        self.current_port_name: Optional[str] = None
        
        # MIDI Data Storage - Single Source of Truth
        self.midi_data: Optional[mido.MidiFile] = None
        self.midi_events: List[Dict[str, Any]] = []  # Events for UI display with unique IDs
        self.midi_notes: List[Dict[str, Any]] = []   # Notes for piano roll with unique IDs
        self.midi_note_map = midi_note_map or {}
        
        # Track current playback position for in-place updates
        self._current_playback_time: float = 0.0
        
        self.logger = get_logger("midi.manager")
        
        # Software controller reference
        self.software_controller = None

    def set_software_controller(self, controller):
        """Set the software controller instance."""
        self.software_controller = controller
        if self.current_worker:
            self.current_worker.set_software_controller(controller)

    # MIDI Data Management Methods  
    def load_from_file(self, file_path: str) -> bool:
        """Load MIDI data from file and parse it for UI display."""
        try:
            self.midi_data = mido.MidiFile(file_path)
            self.current_filename = file_path
            self._parse_midi_data()
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
        self.current_filename = None
        self.logger.info("Created empty MIDI track")
        self.data_changed.emit()
        
    def set_midi_note_map(self, midi_note_map: Dict) -> None:
        """Update the MIDI note mapping and reparse data if available."""
        self.midi_note_map = midi_note_map
        if self.midi_data:
            self._parse_midi_data()
            self.data_changed.emit()
    
    def _parse_midi_data(self) -> None:
        """Parse the loaded MIDI data into events and notes for UI display."""
        if not self.midi_data:
            return
            
        self.midi_events.clear()
        self.midi_notes.clear()
        
        # Parse for MIDI events (using note map)
        current_time = 0
        for msg in self.midi_data:
            current_time += msg.time
            if msg.type == 'note_on' and msg.velocity > 0:
                # Check if this note is in our note map (for events track)
                note_info = self.midi_note_map.get(str(msg.note))
                if note_info:
                    event_id = str(uuid.uuid4())
                    self.midi_events.append({
                        "id": event_id,
                        "note": msg.note,
                        "time": current_time,
                        "velocity": msg.velocity,
                        "info": note_info
                    })
        
        # Parse for piano roll notes
        current_time = 0
        active_notes = {}
        for msg in self.midi_data:
            current_time += msg.time
            if msg.type == 'note_on' and msg.velocity > 0:
                active_notes[msg.note] = {"time": current_time, "velocity": msg.velocity}
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                if msg.note in active_notes:
                    note_id = str(uuid.uuid4())
                    start_time = active_notes[msg.note]["time"]
                    self.midi_notes.append({
                        "id": note_id,
                        "note": msg.note,
                        "time": start_time,
                        "duration": current_time - start_time,
                        "velocity": active_notes[msg.note]["velocity"]
                    })
                    del active_notes[msg.note]
                    
    def get_events_for_ui(self) -> List[Dict[str, Any]]:
        """Get MIDI events formatted for Timeline display."""
        return copy.deepcopy(self.midi_events)
    
    def get_notes_for_ui(self) -> List[Dict[str, Any]]:
        """Get MIDI notes formatted for piano roll display.""" 
        return copy.deepcopy(self.midi_notes)
    
    def get_duration(self) -> float:
        """Get the total duration of the MIDI data."""
        if self.midi_data:
            return self.midi_data.length
        return 0.0

    # MIDI Modification Methods
    
    def add_event(self, note: int, time: float, velocity: int) -> str:
        """Add a new MIDI event and return its ID."""
        if str(note) not in self.midi_note_map:
            self.logger.warning(f"Note {note} not in MIDI note map")
            return ""
            
        event_id = str(uuid.uuid4())
        new_event = {
            "id": event_id,
            "note": note,
            "time": time,
            "velocity": velocity,
            "info": self.midi_note_map[str(note)]
        }
        
        # Insert in chronological order
        inserted = False
        for i, event in enumerate(self.midi_events):
            if event['time'] > time:
                self.midi_events.insert(i, new_event)
                inserted = True
                break
        if not inserted:
            self.midi_events.append(new_event)
        
        # Also add to piano roll
        note_id = str(uuid.uuid4())
        new_note = {
            "id": note_id,
            "note": note,
            "time": time,
            "velocity": velocity,
            "duration": 0.1  # Default duration
        }
        
        inserted = False
        for i, note_data in enumerate(self.midi_notes):
            if note_data['time'] > time:
                self.midi_notes.insert(i, new_note)
                inserted = True
                break
        if not inserted:
            self.midi_notes.append(new_note)
            
        self._rebuild_midi_data()
        self._auto_update_worker()
        self.data_changed.emit()
        self.logger.info(f"Added MIDI event: note={note}, time={time:.3f}s")
        return event_id
    
    def update_event_timing(self, event_id: str, new_time: float) -> bool:
        """Update the timing of a MIDI event."""
        # Find and update event
        event = None
        for e in self.midi_events:
            if e['id'] == event_id:
                old_time = e['time']
                e['time'] = new_time
                event = e
                break
        
        if not event:
            return False
            
        # Find and update corresponding note
        for note in self.midi_notes:
            if (note['note'] == event['note'] and abs(note['time'] - old_time) < 0.01):
                note['time'] = new_time
                break
        
        # Re-sort events by time
        self.midi_events.sort(key=lambda x: x['time'])
        self.midi_notes.sort(key=lambda x: x['time'])
        
        self._rebuild_midi_data()
        self._auto_update_worker()
        self.data_changed.emit()
        self.logger.info(f"Updated event timing: {event_id} to {new_time:.3f}s")
        return True
    
    def edit_event(self, event_id: str, **kwargs) -> bool:
        """Edit a MIDI event's properties."""
        # Find event
        event = None
        for e in self.midi_events:
            if e['id'] == event_id:
                event = e
                break
                
        if not event:
            return False
        
        old_note = event['note']
        old_time = event['time']
        
        # Update event properties
        if 'note' in kwargs and str(kwargs['note']) in self.midi_note_map:
            event['note'] = kwargs['note']
            event['info'] = self.midi_note_map[str(kwargs['note'])]
        if 'time' in kwargs:
            event['time'] = kwargs['time']
        if 'velocity' in kwargs:
            event['velocity'] = kwargs['velocity']
            
        # Find and update corresponding note
        for note in self.midi_notes:
            if (note['note'] == old_note and abs(note['time'] - old_time) < 0.01):
                if 'note' in kwargs:
                    note['note'] = kwargs['note']
                if 'time' in kwargs:
                    note['time'] = kwargs['time']
                if 'velocity' in kwargs:
                    note['velocity'] = kwargs['velocity']
                break
                
        # Re-sort by time
        self.midi_events.sort(key=lambda x: x['time'])
        self.midi_notes.sort(key=lambda x: x['time'])
        
        self._rebuild_midi_data()
        self._auto_update_worker()
        self.data_changed.emit()
        self.logger.info(f"Edited event: {event_id}")
        return True
    
    def delete_event(self, event_id: str) -> bool:
        """Delete a MIDI event."""
        # Find and remove event
        event = None
        for i, e in enumerate(self.midi_events):
            if e['id'] == event_id:
                event = self.midi_events.pop(i)
                break
                
        if not event:
            return False
            
        # Find and remove corresponding note
        note_to_remove = None
        for i, note in enumerate(self.midi_notes):
            if (note['note'] == event['note'] and abs(note['time'] - event['time']) < 0.01):
                note_to_remove = self.midi_notes.pop(i)
                break
                
        self._rebuild_midi_data()
        self._auto_update_worker()
        self.data_changed.emit()
        self.logger.info(f"Deleted event: {event_id}")
        return True

    # ------------------------------------------------------------------
    # Clip-style edit ops (move / set_left_edge / set_right_edge)
    # ------------------------------------------------------------------
    #
    # The timeline UI treats each MIDI event as a "clip" that starts at the
    # event's `time` and visually extends until the next event. These ops
    # let the UI move clips around without overlapping neighbors and
    # automatically maintain BLACKOUT events as gap markers.
    #
    # Invariants enforced by reconcile_blackouts() after every mutation:
    #   - Events sorted by time.
    #   - No two adjacent BLACKOUTs (the later one is merged away).
    #   - No BLACKOUT with effective duration <= BLACKOUT_EPSILON.

    def is_blackout(self, event: Dict[str, Any]) -> bool:
        """Return True if `event` is a BLACKOUT (matched by part name)."""
        info = event.get('info') if event else None
        if info is None:
            return False
        part = getattr(info, 'part', '') or ''
        return part.upper() == BLACKOUT_PART_NAME

    def _blackout_note_number(self) -> Optional[int]:
        """Return the MIDI note number mapped to BLACKOUT, or None."""
        for note_str, info in self.midi_note_map.items():
            if getattr(info, 'part', '').upper() == BLACKOUT_PART_NAME:
                try:
                    return int(note_str)
                except (TypeError, ValueError):
                    continue
        return None

    def _index_of(self, event_id: str) -> int:
        for i, e in enumerate(self.midi_events):
            if e['id'] == event_id:
                return i
        return -1

    def _right_edge_of(self, idx: int) -> float:
        """Visual right edge of event at index idx (= time of next event, or +inf)."""
        if 0 <= idx < len(self.midi_events) - 1:
            return self.midi_events[idx + 1]['time']
        return float('inf')

    def _song_end_time(self) -> float:
        """Effective end-of-timeline used as the song-end clamp for moves."""
        duration = self.get_duration()
        if duration and duration > 0:
            return duration
        # Fallback for empty/missing MIDI data — match the timeline's UI default.
        return 240.0

    def _non_blackout_intervals(self, exclude_event_id: Optional[str] = None
                                ) -> List[Tuple[float, float, str]]:
        """Visible intervals of non-BLACKOUT clips, sorted by start.

        Each clip's interval = [clip.time, next_event.time). The last event's
        interval extends to song_end. Excludes `exclude_event_id` if given.
        """
        song_end = self._song_end_time()
        intervals: List[Tuple[float, float, str]] = []
        for i, e in enumerate(self.midi_events):
            if self.is_blackout(e) or e['id'] == exclude_event_id:
                continue
            end = (self.midi_events[i + 1]['time']
                   if i + 1 < len(self.midi_events) else song_end)
            intervals.append((e['time'], end, e['id']))
        intervals.sort(key=lambda x: x[0])
        return intervals

    def _free_slots_for_move(self, exclude_event_id: str
                             ) -> List[Tuple[float, float]]:
        """Compute (start, end) intervals where a clip can be placed without
        overlapping any non-BLACKOUT clip (excluding `exclude_event_id`)."""
        obstacles = self._non_blackout_intervals(exclude_event_id)
        slots: List[Tuple[float, float]] = []
        cursor = 0.0
        for start, end, _ in obstacles:
            if start > cursor:
                slots.append((cursor, start))
            cursor = max(cursor, end)
        # Tail slot extends past the last obstacle to the song end.
        song_end = self._song_end_time()
        if cursor < song_end:
            slots.append((cursor, song_end))
        return slots

    def _insert_blackout_internal(self, time: float) -> Optional[str]:
        """Insert a BLACKOUT event+note at `time`. No rebuild/emit. Returns new id, or None."""
        bo_note = self._blackout_note_number()
        if bo_note is None:
            self.logger.warning(
                "Cannot auto-insert BLACKOUT: no entry with part='BLACKOUT' in midi_note_map"
            )
            return None

        event_id = str(uuid.uuid4())
        info = self.midi_note_map[str(bo_note)]
        self.midi_events.append({
            "id": event_id,
            "note": bo_note,
            "time": time,
            "velocity": 100,
            "info": info,
        })
        self.midi_events.sort(key=lambda x: x['time'])

        note_id = str(uuid.uuid4())
        self.midi_notes.append({
            "id": note_id,
            "note": bo_note,
            "time": time,
            "velocity": 100,
            "duration": 0.1,
        })
        self.midi_notes.sort(key=lambda x: x['time'])
        return event_id

    def _delete_event_internal(self, event_id: str) -> bool:
        """Remove event AND its corresponding note. No rebuild/emit."""
        event = None
        for i, e in enumerate(self.midi_events):
            if e['id'] == event_id:
                event = self.midi_events.pop(i)
                break
        if event is None:
            return False
        for i, note in enumerate(self.midi_notes):
            if note['note'] == event['note'] and abs(note['time'] - event['time']) < NOTE_MATCH_TOLERANCE:
                self.midi_notes.pop(i)
                break
        return True

    def _set_event_time_internal(self, event_id: str, new_time: float) -> bool:
        """Set an event's time and the matching note's time. No rebuild/emit."""
        for e in self.midi_events:
            if e['id'] == event_id:
                old_time = e['time']
                e['time'] = new_time
                # Mirror in midi_notes (matched by note + old time)
                for note in self.midi_notes:
                    if (note['note'] == e['note']
                            and abs(note['time'] - old_time) < NOTE_MATCH_TOLERANCE):
                        note['time'] = new_time
                        break
                self.midi_events.sort(key=lambda x: x['time'])
                self.midi_notes.sort(key=lambda x: x['time'])
                return True
        return False

    def reconcile_blackouts(self) -> None:
        """Enforce BLACKOUT invariants. Idempotent."""
        # Sort first
        self.midi_events.sort(key=lambda x: x['time'])
        self.midi_notes.sort(key=lambda x: x['time'])

        # Pass 1: drop zero/negative-duration BLACKOUTs.
        # A BLACKOUT's effective duration = (next event's time) - (its time).
        # If <= BLACKOUT_EPSILON, drop it.
        dropped = True
        while dropped:
            dropped = False
            for i, e in enumerate(self.midi_events):
                if not self.is_blackout(e):
                    continue
                next_time = (self.midi_events[i + 1]['time']
                             if i + 1 < len(self.midi_events) else float('inf'))
                if next_time - e['time'] <= BLACKOUT_EPSILON:
                    self._delete_event_internal(e['id'])
                    dropped = True
                    break  # restart loop after mutation

        # Pass 2: merge adjacent BLACKOUTs (delete the later one of any pair).
        merged = True
        while merged:
            merged = False
            for i in range(len(self.midi_events) - 1):
                if self.is_blackout(self.midi_events[i]) and self.is_blackout(self.midi_events[i + 1]):
                    self._delete_event_internal(self.midi_events[i + 1]['id'])
                    merged = True
                    break

    def _commit(self) -> None:
        """Final step of any mutation: rebuild MIDI, push to worker, notify."""
        self._rebuild_midi_data()
        self._auto_update_worker()
        self.data_changed.emit()

    def move_event(self, event_id: str, new_time: float) -> bool:
        """Move a clip horizontally, preserving its width.

        Allows "jumping" over other clips when there is enough free space at
        the destination. Free space = the union of intervals not occupied by
        any non-BLACKOUT clip (other than this one). If no slot can fit the
        clip, the move is rejected.
        """
        idx = self._index_of(event_id)
        if idx < 0:
            return False

        target = self.midi_events[idx]
        old_time = target['time']
        target_old_right = self._right_edge_of(idx)
        # Clamp width — may be infinite if the target was the last event.
        song_end = self._song_end_time()
        target_width = (target_old_right - old_time
                        if target_old_right != float('inf')
                        else max(MIN_EVENT_DURATION, song_end - old_time))
        target_width = max(MIN_EVENT_DURATION, target_width)

        if new_time < 0:
            new_time = 0.0

        # Snapshot every non-BLACKOUT's intended right edge BEFORE the move.
        # We use this after the move to reinsert BLACKOUTs that preserve each
        # clip's intended visual extent.
        intended_ends: Dict[str, float] = {}
        for i, e in enumerate(self.midi_events):
            if not self.is_blackout(e):
                end = self._right_edge_of(i)
                if end == float('inf'):
                    end = song_end
                intended_ends[e['id']] = end

        # Find the slot whose clamped placement is closest to `new_time`.
        slots = self._free_slots_for_move(event_id)
        best_new_time: Optional[float] = None
        best_distance = float('inf')
        for slot_start, slot_end in slots:
            if slot_end - slot_start + BLACKOUT_EPSILON < target_width:
                continue  # too small
            valid_min = slot_start
            valid_max = slot_end - target_width
            clamped = max(valid_min, min(new_time, valid_max))
            dist = abs(clamped - new_time)
            if dist < best_distance:
                best_distance = dist
                best_new_time = clamped

        if best_new_time is None:
            self.logger.debug(
                f"move_event {event_id}: no slot fits target width {target_width:.3f}s"
            )
            return False
        if abs(best_new_time - old_time) < BLACKOUT_EPSILON:
            return False

        # Update target's intended end now that we know its new position.
        new_target_end = best_new_time + target_width
        intended_ends[event_id] = new_target_end

        # Apply the move.
        self._set_event_time_internal(event_id, best_new_time)

        # Drop any BLACKOUT that now sits strictly inside target's new visible
        # span. Without this step the target's "next event" can land in the
        # middle of its own extent, visually shrinking the clip.
        stale_blackouts = [
            e['id'] for e in self.midi_events
            if e['id'] != event_id
            and self.is_blackout(e)
            and best_new_time + BLACKOUT_EPSILON < e['time'] < new_target_end - BLACKOUT_EPSILON
        ]
        for bo_id in stale_blackouts:
            self._delete_event_internal(bo_id)

        # Restore each non-BLACKOUT's intended extent: if its actual next event
        # is now past its intended end, insert a BLACKOUT at the intended end.
        for eid, intended_end in intended_ends.items():
            e_idx = self._index_of(eid)
            if e_idx < 0:
                continue
            actual_end = self._right_edge_of(e_idx)
            if actual_end == float('inf'):
                actual_end = song_end
            if actual_end > intended_end + BLACKOUT_EPSILON:
                self._insert_blackout_internal(intended_end)

        # Reconcile (drops zero-duration BLACKOUTs, merges adjacent ones).
        self.reconcile_blackouts()

        self._commit()
        self.logger.info(
            f"Moved event {event_id} from {old_time:.3f}s to {best_new_time:.3f}s"
        )
        return True

    def set_event_left_edge(self, event_id: str, new_time: float) -> bool:
        """Resize a clip from the left (changes its start time only)."""
        idx = self._index_of(event_id)
        if idx < 0:
            return False

        target = self.midi_events[idx]
        old_time = target['time']

        # Clamp range
        # Right limit: keep at least MIN_EVENT_DURATION before next event (or 0
        # if no next — unconstrained on the right of the timeline).
        right_edge = self._right_edge_of(idx)
        max_new_time = (right_edge - MIN_EVENT_DURATION
                        if right_edge != float('inf')
                        else float('inf'))
        # Left limit: cannot cross immediate non-BLACKOUT predecessor + MIN_DURATION.
        # If prev is BLACKOUT, we can extend through it (consuming) up to that
        # BLACKOUT's predecessor. Reconcile will delete the consumed BLACKOUT.
        min_new_time = 0.0
        for i in range(idx - 1, -1, -1):
            e = self.midi_events[i]
            if not self.is_blackout(e):
                min_new_time = e['time'] + MIN_EVENT_DURATION
                break

        new_time = max(min_new_time, min(new_time, max_new_time))
        delta = new_time - old_time
        if abs(delta) < BLACKOUT_EPSILON:
            return False

        # Capture immediate prev state before mutation.
        prev_id = self.midi_events[idx - 1]['id'] if idx > 0 else None
        prev_was_blackout = (prev_id is not None
                             and self.is_blackout(self.midi_events[idx - 1]))

        # Apply target time change.
        self._set_event_time_internal(event_id, new_time)

        # If we shrunk (delta > 0) and the immediate prev is non-BLACKOUT
        # (or there is no prev), insert a BLACKOUT to fill the gap.
        if delta > 0 and not prev_was_blackout and prev_id is not None:
            self._insert_blackout_internal(old_time)
        # If we extended (delta < 0) and prev was BLACKOUT, it shrinks naturally
        # (its time stays, target.time decreased → its right edge moved left).
        # reconcile_blackouts() will delete it if duration ≤ epsilon.

        self.reconcile_blackouts()
        self._commit()
        self.logger.info(
            f"Resized event {event_id} left edge {old_time:.3f}s → {new_time:.3f}s"
        )
        return True

    def set_event_right_edge(self, event_id: str, new_right_time: float) -> bool:
        """Resize a clip from the right.

        The right edge of clip[i] is the time of clip[i+1]; this method is the
        moral equivalent of "where should this clip end?". Inserts/grows/
        shrinks/deletes the BLACKOUT immediately following `event_id`.
        """
        idx = self._index_of(event_id)
        if idx < 0:
            return False

        target = self.midi_events[idx]

        # Clamp range
        # Left limit: at least MIN_EVENT_DURATION after target's start.
        min_right = target['time'] + MIN_EVENT_DURATION
        # Right limit: cannot extend past the next non-BLACKOUT (or infinity).
        max_right = float('inf')
        for i in range(idx + 1, len(self.midi_events)):
            if not self.is_blackout(self.midi_events[i]):
                max_right = self.midi_events[i]['time']
                break

        new_right_time = max(min_right, min(new_right_time, max_right))

        # Determine current behavior: is there a BLACKOUT immediately after target?
        next_idx = idx + 1
        next_event = self.midi_events[next_idx] if next_idx < len(self.midi_events) else None

        if next_event is not None and self.is_blackout(next_event):
            # Move the existing BLACKOUT to new_right_time. Reconcile handles deletion if zero.
            if abs(next_event['time'] - new_right_time) < BLACKOUT_EPSILON:
                return False
            self._set_event_time_internal(next_event['id'], new_right_time)
        else:
            # No BLACKOUT yet. Determine if we need to insert one.
            current_right = self._right_edge_of(idx)
            if new_right_time < current_right - BLACKOUT_EPSILON:
                # Shrinking: insert BLACKOUT at new_right_time.
                self._insert_blackout_internal(new_right_time)
            else:
                # No-op: cannot extend past non-BLACKOUT next (already clamped).
                return False

        self.reconcile_blackouts()
        self._commit()
        self.logger.info(
            f"Resized event {event_id} right edge to {new_right_time:.3f}s"
        )
        return True

    def _rebuild_midi_data(self) -> None:
        """Rebuild the MIDI file from current event and note data."""
        if not self.midi_data:
            return
            
        try:
            # Create new MIDI file with current structure
            new_mid = mido.MidiFile(type=self.midi_data.type, ticks_per_beat=self.midi_data.ticks_per_beat)
            track = mido.MidiTrack()
            new_mid.tracks.append(track)
            
            # Add tempo and meta messages from original
            tempo_microseconds = 500000  # Default tempo (120 BPM)
            for msg in self.midi_data.merged_track:
                if msg.is_meta:
                    if msg.type == 'set_tempo':
                        tempo_microseconds = msg.tempo
                    track.append(msg.copy())
            
            # Build all MIDI events from notes data
            all_events = []
            for note_data in self.midi_notes:
                # Note on
                all_events.append({
                    'time': note_data['time'],
                    'type': 'note_on',
                    'note': note_data['note'],
                    'velocity': note_data['velocity'],
                    'channel': 0
                })
                # Note off
                all_events.append({
                    'time': note_data['time'] + note_data['duration'],
                    'type': 'note_off',
                    'note': note_data['note'],
                    'velocity': 0,
                    'channel': 0
                })
            
            # Sort by time and convert to MIDI messages
            all_events.sort(key=lambda x: x['time'])
            current_time_seconds = 0
            
            for event in all_events:
                delta_time_seconds = event['time'] - current_time_seconds
                delta_ticks = int(mido.second2tick(delta_time_seconds,
                                                 ticks_per_beat=new_mid.ticks_per_beat,
                                                 tempo=tempo_microseconds))
                
                msg = mido.Message(
                    event['type'],
                    channel=event['channel'],
                    note=event['note'],
                    velocity=event['velocity'],
                    time=delta_ticks
                )
                track.append(msg)
                current_time_seconds = event['time']
            
            self.midi_data = new_mid
            
        except Exception as e:
            self.logger.error(f"Error rebuilding MIDI data: {e}")
    
    def _auto_update_worker(self) -> None:
        """Automatically update the worker with current MIDI data if running."""
        if self.current_worker and self.current_port_name and self.midi_data:
            # Try to update worker in-place to preserve playback position
            if self._update_worker_in_place():
                self.logger.debug("Updated worker MIDI data in-place")
            else:
                # Fallback to restarting if in-place update fails
                self.logger.debug("In-place update failed, restarting worker with modified MIDI data")
                self.start_worker(self.current_filename or "modified", self.current_port_name, self.midi_data)

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
    
    def export_to_file(self, file_path: str) -> bool:
        """Export current MIDI data to file using safe temporary file approach."""
        if not self.midi_data:
            self.logger.error("No MIDI data to export")
            return False
        
        import os
        import tempfile
        import shutil
        
        # Create temporary file in the same directory as target
        target_dir = os.path.dirname(os.path.abspath(file_path))
        temp_path = None
        
        try:
            # Create temporary file with .tmp extension in target directory
            temp_fd, temp_path = tempfile.mkstemp(suffix='.tmp', dir=target_dir, prefix='midi_export_')
            os.close(temp_fd)  # Close file descriptor, we'll use the path
            
            # Export MIDI data to temporary file first
            self.midi_data.save(temp_path)
            self.logger.debug(f"MIDI data written to temporary file: {temp_path}")
            
            # Verify the temporary file was created and has content
            if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
                raise Exception("Temporary MIDI file is empty or was not created")
            
            # Atomic move from temporary file to final destination
            shutil.move(temp_path, file_path)
            self.logger.info(f"Exported MIDI data to: {file_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to export MIDI file {file_path}: {e}")
            
            # Clean up temporary file if it still exists
            try:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
                    self.logger.debug(f"Cleaned up temporary file: {temp_path}")
            except Exception as cleanup_error:
                self.logger.warning(f"Failed to clean up temporary file {temp_path}: {cleanup_error}")
            
            return False

    # Enhanced playback methods
    
    def start_playback(self, port_name: str) -> bool:
        """Start MIDI playback with current data."""
        if not self.midi_data:
            self.logger.warning("No MIDI data available for playback")
            return False
            
        return self.start_worker(self.current_filename or "current", port_name, self.midi_data)
            
    def start_worker(self, filename: str, port_name: str, midi_data: Optional[mido.MidiFile] = None) -> bool:
        """
        Start a MIDI worker with the specified file and port.
        
        Args:
            filename: Path to MIDI file (used for identification)
            port_name: MIDI output port name
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
            self.error_occurred.emit(port_name or "Unknown", "No MIDI file or data specified")
            return False
        
        if not port_name:
            self.error_occurred.emit("Unknown", "No MIDI port specified")
            return False
        
        # Validate port availability
        available_ports = MidiWorker.get_available_ports()
        if port_name not in available_ports:
            self.error_occurred.emit(port_name, f"MIDI port not available. Available ports: {available_ports}")
            return False
        
        # Create and start worker
        try:
            worker = MidiWorker(filename, port_name, midi_data)
            if self.software_controller:
                worker.set_software_controller(self.software_controller)
            
            # Connect signals
            worker.connection_status_changed.connect(
                lambda status: self.connection_status_changed.emit(port_name, status)
            )
            worker.error_occurred.connect(
                lambda error: self.error_occurred.emit(port_name, error)
            )
            worker.playback_position_changed.connect(self.playback_position_changed.emit)
            worker.finished.connect(self.worker_finished.emit)
            worker.finished.connect(worker.deleteLater)
            
            # Store references
            self.current_worker = worker
            self.current_filename = filename
            self.current_port_name = port_name
            
            # Reset playback time tracking when starting new worker
            self._current_playback_time = 0.0
            
            # Start the worker thread
            worker.start()
            
            self.logger.info(f"Started MIDI worker for {filename} on port {port_name}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to start MIDI worker: {e}")
            self.error_occurred.emit(port_name, f"Failed to start MIDI worker: {e}")
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
            self.current_port_name = None
            
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
    
    def get_current_port_name(self) -> Optional[str]:
        """Get the current MIDI port name."""
        return self.current_port_name
    
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
                'port_name': self.current_port_name,
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
        if not self.current_port_name:
            return False
        
        if filename:
            # Loading a new file
            return self.load_from_file(filename) and self.start_playback(self.current_port_name)
        else:
            # Reloading current data
            return self.start_playback(self.current_port_name)
    
    def switch_port(self, new_port_name: str) -> bool:
        """
        Switch to a different MIDI port while keeping current data.
        
        Args:
            new_port_name: New MIDI port name
            
        Returns:
            True if switch was successful
        """
        if not self.midi_data:
            return False
        
        return self.start_playback(new_port_name)
    
    def cleanup(self) -> None:
        """Clean up resources when shutting down."""
        self.stop_worker()