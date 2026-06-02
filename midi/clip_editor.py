"""ClipEditor — single-clip CRUD and clip-style move/resize/replace.

The timeline UI treats each MIDI event as a "clip" that starts at the event's
`time` and visually extends until the next same-fixture event. These ops move
clips around without overlapping neighbors and maintain BLACKOUT events as gap
markers. The invariants (events sorted; no adjacent BLACKOUTs; no BLACKOUT with
effective duration <= BLACKOUT_EPSILON) are enforced by
MidiDocument.reconcile_blackouts() after every mutation.

Operates on a shared MidiDocument; a single injected commit callback
performs the MIDI rebuild + worker push + data_changed emit."""

from typing import Optional, List, Dict, Any, Tuple
import uuid

from tupperlogger import get_logger
from .constants import (
    MIN_EVENT_DURATION,
    BLACKOUT_EPSILON,
)


class ClipEditor:
    def __init__(self, doc, commit):
        self._doc = doc
        self._commit = commit
        self.logger = get_logger("midi.clip_editor")

    def add_event(self, note: int, time: float, velocity: int) -> str:
        """Add a new MIDI event and return its ID."""
        if str(note) not in self._doc.midi_note_map:
            self.logger.warning(f"Note {note} not in MIDI note map")
            return ""

        event_id = str(uuid.uuid4())
        new_event = {
            "id": event_id,
            "note": note,
            "time": time,
            "velocity": velocity,
            "info": self._doc.midi_note_map[str(note)],
            "group_id": None,
            "scene_name": None,
        }
        
        # Insert in chronological order
        inserted = False
        for i, event in enumerate(self._doc.midi_events):
            if event['time'] > time:
                self._doc.midi_events.insert(i, new_event)
                inserted = True
                break
        if not inserted:
            self._doc.midi_events.append(new_event)
        
        # Mirror in midi_notes so the MIDI file rebuilds with the new note
        note_id = str(uuid.uuid4())
        new_note = {
            "id": note_id,
            "note": note,
            "time": time,
            "velocity": velocity,
            "duration": 0.1  # Default duration
        }
        
        inserted = False
        for i, note_data in enumerate(self._doc.midi_notes):
            if note_data['time'] > time:
                self._doc.midi_notes.insert(i, new_note)
                inserted = True
                break
        if not inserted:
            self._doc.midi_notes.append(new_note)

        # Reconcile so that dropping a clip exactly where a BLACKOUT cap
        # already sits drops the now-zero-duration BLACKOUT instead of
        # leaving it as a width-0 ghost in front of the new clip.
        self._doc.reconcile_blackouts(self._doc._event_fixture(new_event))
        self._commit()
        self.logger.info(f"Added MIDI event: note={note}, time={time:.3f}s")
        return event_id

    def update_event_timing(self, event_id: str, new_time: float) -> bool:
        """Update the timing of a MIDI event."""
        # Find and update event
        event = None
        for e in self._doc.midi_events:
            if e['id'] == event_id:
                old_time = e['time']
                e['time'] = new_time
                event = e
                break
        
        if not event:
            return False
            
        # Find and update corresponding note
        for note in self._doc.midi_notes:
            if (note['note'] == event['note'] and abs(note['time'] - old_time) < 0.01):
                note['time'] = new_time
                break
        
        # Re-sort events by time
        self._doc.midi_events.sort(key=lambda x: x['time'])
        self._doc.midi_notes.sort(key=lambda x: x['time'])
        
        self._commit()
        self.logger.info(f"Updated event timing: {event_id} to {new_time:.3f}s")
        return True

    def edit_event(self, event_id: str, **kwargs) -> bool:
        """Edit a MIDI event's properties."""
        # Find event
        event = None
        for e in self._doc.midi_events:
            if e['id'] == event_id:
                event = e
                break
                
        if not event:
            return False
        
        old_note = event['note']
        old_time = event['time']
        
        # Update event properties
        if 'note' in kwargs and str(kwargs['note']) in self._doc.midi_note_map:
            event['note'] = kwargs['note']
            event['info'] = self._doc.midi_note_map[str(kwargs['note'])]
        if 'time' in kwargs:
            event['time'] = kwargs['time']
        if 'velocity' in kwargs:
            event['velocity'] = kwargs['velocity']
            
        # Find and update corresponding note
        for note in self._doc.midi_notes:
            if (note['note'] == old_note and abs(note['time'] - old_time) < 0.01):
                if 'note' in kwargs:
                    note['note'] = kwargs['note']
                if 'time' in kwargs:
                    note['time'] = kwargs['time']
                if 'velocity' in kwargs:
                    note['velocity'] = kwargs['velocity']
                break
                
        # Re-sort by time
        self._doc.midi_events.sort(key=lambda x: x['time'])
        self._doc.midi_notes.sort(key=lambda x: x['time'])
        
        self._commit()
        self.logger.info(f"Edited event: {event_id}")
        return True

    def delete_event(self, event_id: str) -> bool:
        """Delete a MIDI event."""
        # Find and remove event
        event = None
        for i, e in enumerate(self._doc.midi_events):
            if e['id'] == event_id:
                event = self._doc.midi_events.pop(i)
                break
                
        if not event:
            return False
            
        # Find and remove corresponding note
        note_to_remove = None
        for i, note in enumerate(self._doc.midi_notes):
            if (note['note'] == event['note'] and abs(note['time'] - event['time']) < 0.01):
                note_to_remove = self._doc.midi_notes.pop(i)
                break
                
        self._commit()
        self.logger.info(f"Deleted event: {event_id}")
        return True

    def move_event(self, event_id: str, new_time: float) -> bool:
        """Move a clip horizontally, preserving its width.

        Allows "jumping" over other clips when there is enough free space at
        the destination. Free space = the union of intervals not occupied by
        any non-BLACKOUT clip (other than this one). If no slot can fit the
        clip, the move is rejected.
        """
        idx = self._doc._index_of(event_id)
        if idx < 0:
            return False

        target = self._doc.midi_events[idx]
        fixture = self._doc._event_fixture(target)
        old_time = target['time']
        target_old_right = self._doc._right_edge_of(idx, fixture)
        song_end = self._doc._song_end_time()
        # Last clip on the track has no successor: its visual extent stretches
        # to song_end, but for collision/move purposes we treat it as
        # point-sized so it can be moved freely. Otherwise preserve its
        # visible width so the clip doesn't overlap its successor.
        if target_old_right == float('inf'):
            target_width = MIN_EVENT_DURATION
        else:
            target_width = max(MIN_EVENT_DURATION, target_old_right - old_time)

        if new_time < 0:
            new_time = 0.0

        # Snapshot every same-fixture non-BLACKOUT's intended right edge BEFORE
        # the move. We use this after the move to reinsert BLACKOUTs that
        # preserve each clip's intended visual extent. Other fixtures aren't
        # affected by this move so we ignore them.
        intended_ends: Dict[str, float] = {}
        for i, e in enumerate(self._doc.midi_events):
            if self._doc._event_fixture(e) != fixture or self._doc.is_blackout(e):
                continue
            end = self._doc._right_edge_of(i, fixture)
            if end == float('inf'):
                end = song_end
            intended_ends[e['id']] = end

        # Find the slot whose clamped placement is closest to `new_time`.
        slots = self._doc._free_slots_for_move(event_id, fixture)
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
        # Skip when the target had no successor (was the last clip on its
        # track): its intended end is song_end, and overriding it with the
        # collision-only target_width (MIN_EVENT_DURATION) would cause the
        # post-move loop to spuriously insert a cap-BLACKOUT right after.
        new_target_end = best_new_time + target_width
        if target_old_right != float('inf'):
            intended_ends[event_id] = new_target_end

        # Capture target's "trailing cap" — the immediate same-fixture
        # BLACKOUT after the clip's old position — BEFORE the move. The
        # cap belongs to the clip and should travel with it; otherwise it
        # would be left behind as a stale gap-marker at the old position.
        # The new cap is re-inserted later by the intended-ends restoration
        # loop. We capture by id (stable across the upcoming sort).
        old_trailing_cap_id: Optional[str] = None
        for j in range(idx + 1, len(self._doc.midi_events)):
            e = self._doc.midi_events[j]
            if self._doc._event_fixture(e) != fixture:
                continue
            if self._doc.is_blackout(e):
                old_trailing_cap_id = e['id']
            break

        # Apply the move.
        self._doc._set_event_time_internal(event_id, best_new_time)

        # Drop any same-fixture BLACKOUT that now sits inside target's new
        # visible span (including at the very start). Without this step the
        # target's "next event" can be a BLACKOUT at distance 0 (sort
        # stability puts a same-time BLACKOUT after the moved event),
        # collapsing the clip's visible width to zero — i.e. it disappears.
        # Other fixtures' BLACKOUTs are untouched.
        stale_blackouts = [
            e['id'] for e in self._doc.midi_events
            if e['id'] != event_id
            and self._doc._event_fixture(e) == fixture
            and self._doc.is_blackout(e)
            and best_new_time - BLACKOUT_EPSILON <= e['time'] < new_target_end - BLACKOUT_EPSILON
        ]
        # The old trailing cap may sit outside the new visible span (e.g.
        # for a long-range move) but still needs to go.
        if old_trailing_cap_id is not None and old_trailing_cap_id not in stale_blackouts:
            stale_blackouts.append(old_trailing_cap_id)
        for bo_id in stale_blackouts:
            self._doc._delete_event_internal(bo_id)

        # Restore each same-fixture non-BLACKOUT's intended extent: if its
        # actual next same-fixture event is now past its intended end,
        # insert a BLACKOUT (of the right fixture) at the intended end.
        for eid, intended_end in intended_ends.items():
            e_idx = self._doc._index_of(eid)
            if e_idx < 0:
                continue
            actual_end = self._doc._right_edge_of(e_idx, fixture)
            if actual_end == float('inf'):
                actual_end = song_end
            if actual_end > intended_end + BLACKOUT_EPSILON:
                self._doc._insert_blackout_internal(intended_end, fixture)

        # Reconcile only this fixture (drops zero-duration BLACKOUTs and
        # merges adjacent ones within the same track).
        self._doc.reconcile_blackouts(fixture)

        self._commit()
        self.logger.info(
            f"Moved event {event_id} from {old_time:.3f}s to {best_new_time:.3f}s"
        )
        return True

    def set_event_left_edge(self, event_id: str, new_time: float) -> bool:
        """Resize a clip from the left (changes its start time only)."""
        idx = self._doc._index_of(event_id)
        if idx < 0:
            return False

        target = self._doc.midi_events[idx]
        fixture = self._doc._event_fixture(target)
        old_time = target['time']

        # Clamp range
        # Right limit: keep at least MIN_EVENT_DURATION before next event of
        # the same fixture. If the clip is the last on its track, its visual
        # extent stretches to song_end, so clamp to song_end - MIN_DURATION
        # rather than leaving max_new_time unbounded.
        right_edge = self._doc._right_edge_of(idx, fixture)
        max_new_time = (right_edge - MIN_EVENT_DURATION
                        if right_edge != float('inf')
                        else self._doc._song_end_time() - MIN_EVENT_DURATION)
        # Left limit: cannot cross immediate non-BLACKOUT same-fixture
        # predecessor + MIN_DURATION. If prev is a BLACKOUT of the same
        # fixture, we can extend through it (consuming) up to that BLACKOUT's
        # predecessor. Reconcile will delete the consumed BLACKOUT.
        min_new_time = 0.0
        prev_same: Optional[Dict[str, Any]] = None
        for i in range(idx - 1, -1, -1):
            e = self._doc.midi_events[i]
            if self._doc._event_fixture(e) != fixture:
                continue
            if prev_same is None:
                prev_same = e
            if not self._doc.is_blackout(e):
                min_new_time = e['time'] + MIN_EVENT_DURATION
                break

        new_time = max(min_new_time, min(new_time, max_new_time))
        delta = new_time - old_time
        if abs(delta) < BLACKOUT_EPSILON:
            return False

        # Capture immediate same-fixture prev state before mutation.
        prev_id = prev_same['id'] if prev_same is not None else None
        prev_was_blackout = (prev_same is not None and self._doc.is_blackout(prev_same))

        # Apply target time change.
        self._doc._set_event_time_internal(event_id, new_time)

        # If we shrunk (delta > 0) and the immediate same-fixture prev is
        # non-BLACKOUT (or there is no prev of this fixture), insert a
        # fixture-matching BLACKOUT to fill the gap.
        if delta > 0 and not prev_was_blackout and prev_id is not None:
            self._doc._insert_blackout_internal(old_time, fixture)
        # If we extended (delta < 0) and prev was BLACKOUT, it shrinks naturally
        # (its time stays, target.time decreased → its right edge moved left).
        # reconcile_blackouts() will delete it if duration ≤ epsilon.

        self._doc.reconcile_blackouts(fixture)
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
        idx = self._doc._index_of(event_id)
        if idx < 0:
            return False

        target = self._doc.midi_events[idx]
        fixture = self._doc._event_fixture(target)

        # Clamp range
        # Left limit: at least MIN_EVENT_DURATION after target's start.
        min_right = target['time'] + MIN_EVENT_DURATION
        # Right limit: cannot extend past the next non-BLACKOUT same-fixture
        # event (or infinity). Other fixtures are scoped independently.
        max_right = float('inf')
        for i in range(idx + 1, len(self._doc.midi_events)):
            e = self._doc.midi_events[i]
            if self._doc._event_fixture(e) != fixture:
                continue
            if not self._doc.is_blackout(e):
                max_right = e['time']
                break

        new_right_time = max(min_right, min(new_right_time, max_right))

        # Find the next same-fixture event (BLACKOUT or not) — the gap
        # marker we must move/insert to express the new right edge.
        next_event: Optional[Dict[str, Any]] = None
        for i in range(idx + 1, len(self._doc.midi_events)):
            e = self._doc.midi_events[i]
            if self._doc._event_fixture(e) == fixture:
                next_event = e
                break

        if next_event is not None and self._doc.is_blackout(next_event):
            # Move the existing BLACKOUT to new_right_time. Reconcile handles deletion if zero.
            if abs(next_event['time'] - new_right_time) < BLACKOUT_EPSILON:
                return False
            self._doc._set_event_time_internal(next_event['id'], new_right_time)
        else:
            # No same-fixture BLACKOUT yet. Determine if we need to insert one.
            current_right = self._doc._right_edge_of(idx, fixture)
            if new_right_time < current_right - BLACKOUT_EPSILON:
                # Shrinking: insert fixture-matching BLACKOUT at new_right_time.
                self._doc._insert_blackout_internal(new_right_time, fixture)
            else:
                # No-op: cannot extend past non-BLACKOUT next (already clamped).
                return False

        self._doc.reconcile_blackouts(fixture)
        self._commit()
        self.logger.info(
            f"Resized event {event_id} right edge to {new_right_time:.3f}s"
        )
        return True

    def replace_with_blackout(self, event_id: str) -> bool:
        """Convert a non-BLACKOUT event into the BLACKOUT for its fixture.

        Preserves the event's time slot so neighboring clips don't visually
        expand into the freed space. Adjacent BLACKOUTs are merged afterwards
        via reconcile_blackouts(). No-op (returns False) if the event is
        already a BLACKOUT or its fixture has no BLACKOUT mapping.
        """
        idx = self._doc._index_of(event_id)
        if idx < 0:
            return False
        event = self._doc.midi_events[idx]
        old_note = event['note']
        old_time = event['time']
        fixture = self._doc._event_fixture(event)
        if not self._doc._replace_with_blackout_internal(event):
            return False

        self._doc.reconcile_blackouts(fixture)
        self._commit()
        self.logger.info(
            f"Replaced event {event_id} (note={old_note}, "
            f"time={old_time:.3f}s, fixture={fixture!r}) with BLACKOUT"
        )
        return True
