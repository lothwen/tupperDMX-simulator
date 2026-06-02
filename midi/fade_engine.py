"""FadeEngine — scene-group fade detection, generation, and UI setters.

A "fade" is a chain of brightness (note 1) note_on events ramping linearly
between (near) zero and the group's brightness V. Fade notes live ONLY in
midi_notes (never midi_events) so they don't pollute scene clustering, timeline
rendering, or per-clip mutation logic. They round-trip through the saved MIDI
file and are detected on load by the consecutive-step-with-monotonic-velocity-
and-zero-endpoint pattern.

Operates on a shared MidiDocument; the scene setters use injected commit /
notify-changed callbacks so the engine never touches Qt directly.
"""

from typing import Optional, List, Dict, Any, Tuple, Callable
import uuid

from tupperlogger import get_logger
from .constants import (
    NOTE_MATCH_TOLERANCE,
    FADE_STEP_SECONDS,
    FADE_NOTE_DURATION,
    FADE_MIN_DURATION,
    FADE_BRIGHTNESS_NOTE,
    FADE_VELOCITY_MATCH_TOLERANCE,
)


class FadeEngine:
    def __init__(self, doc, commit: Callable[[], None],
                 notify_changed: Callable[[], None]):
        self._doc = doc
        self._commit = commit
        self._notify_changed = notify_changed
        self.logger = get_logger("midi.fade_engine")

    # --- load-time detection -------------------------------------------------

    def detect_on_load(self) -> List[Dict[str, Any]]:
        """Spot fade ramps in freshly-parsed midi_events; migrate to midi_notes.

        Heuristic: any contiguous run of note-1 events at FADE_STEP_SECONDS
        apart (within NOTE_MATCH_TOLERANCE) with monotonic velocities and a
        zero endpoint is a fade. Detected events are removed from
        midi_events and the matching midi_notes entries are tagged
        is_fade=True. The parent_group_id is filled in later by
        attach_runs_to_groups once scene clustering has run.

        Returns the list of detected runs so the attach pass can find them.
        """
        doc = self._doc
        doc.midi_events.sort(key=lambda x: x['time'])

        # Filter to note-1 (brightness) events; secondary sort by velocity
        # so at same-time conflicts the smaller velocity comes first. This
        # is what lets fade endpoints (vel=0) win when they coincide with
        # an original brightness note (vel=V): for fade-in, the original
        # vel=V at scene_start sits next to the fade starter vel=0; for
        # fade-out, the fade ender vel=0 at clip_end sits next to the next
        # group's vel=V. Putting vel=0 first means the run-extender picks
        # the fade member, keeping the run monotonic.
        candidates = sorted(
            (e for e in doc.midi_events if e['note'] == FADE_BRIGHTNESS_NOTE),
            key=lambda e: (e['time'], e['velocity']),
        )

        runs: List[Dict[str, Any]] = []
        used_ids: set = set()

        for i, anchor in enumerate(candidates):
            if anchor['id'] in used_ids:
                continue
            run_members = [anchor]
            for j in range(i + 1, len(candidates)):
                cand = candidates[j]
                if cand['id'] in used_ids:
                    break
                last_time = run_members[-1]['time']
                expected = last_time + FADE_STEP_SECONDS
                if cand['time'] > expected + NOTE_MATCH_TOLERANCE:
                    break  # gap too large — run ends here
                if cand['time'] < last_time + NOTE_MATCH_TOLERANCE:
                    # Same-time / sub-tolerance duplicate (e.g. original
                    # group's brightness note coincident with the fade
                    # starter). Skip without breaking the run.
                    continue
                if cand['time'] < expected - NOTE_MATCH_TOLERANCE:
                    # Off-step endpoint: strictly between the last step
                    # and the next expected step. _linear_fade_ramp
                    # emits one of these whenever the user's chosen
                    # fade duration isn't a clean multiple of
                    # FADE_STEP_SECONDS (which is the common case —
                    # arbitrary drag positions rarely land on the grid).
                    # It is always the FINAL note in the ramp; absorb
                    # it and stop. Without this branch, fade-out's
                    # vels[-1]=1 endpoint is missed and the whole run
                    # fails the `vels[-1] <= 1` check.
                    run_members.append(cand)
                    break
                run_members.append(cand)
            if len(run_members) < 2:
                continue
            vels = [m['velocity'] for m in run_members]
            mono_up = all(vels[k] <= vels[k + 1] for k in range(len(vels) - 1))
            mono_dn = all(vels[k] >= vels[k + 1] for k in range(len(vels) - 1))
            # Endpoint floor is 1 (not 0) — see _linear_fade_ramp on why
            # velocity 0 can't be emitted into a MIDI file.
            is_fade_in = mono_up and vels[0] <= 1 and vels[-1] > vels[0]
            is_fade_out = mono_dn and vels[-1] <= 1 and vels[0] > vels[-1]
            if not (is_fade_in or is_fade_out):
                continue
            kind = 'in' if is_fade_in else 'out'
            runs.append({
                'kind': kind,
                'members': run_members,
                'start_time': run_members[0]['time'],
                'end_time': run_members[-1]['time'],
                'peak_velocity': max(vels),
            })
            for m in run_members:
                used_ids.add(m['id'])

        if not runs:
            return []

        # Migrate: remove fade events from midi_events, tag corresponding
        # midi_notes entries. We match midi_notes by (note, time, velocity)
        # since a fade-in starter at scene start collides with the original
        # brightness note in (note, time) but differs in velocity.
        # Track which run each tagged midi_note belongs to via a temporary
        # `_fade_run_idx` field — attach_runs_to_groups consumes and clears it.
        for run_idx, run in enumerate(runs):
            for m in run['members']:
                # Find midi_notes entry — first untagged matching one wins.
                for n in doc.midi_notes:
                    if n.get('is_fade'):
                        continue
                    if n['note'] != m['note']:
                        continue
                    if abs(n['time'] - m['time']) > NOTE_MATCH_TOLERANCE:
                        continue
                    if abs(n['velocity'] - m['velocity']) > FADE_VELOCITY_MATCH_TOLERANCE:
                        continue
                    n['is_fade'] = True
                    n['parent_group_id'] = None  # filled in by attach pass
                    n['_fade_run_idx'] = run_idx
                    break

        detected_ids = used_ids
        doc.midi_events = [e for e in doc.midi_events
                           if e['id'] not in detected_ids]
        return runs

    def attach_runs_to_groups(self, fade_runs: List[Dict[str, Any]]) -> None:
        """Match each detected fade run to a scene group and fill in metadata.

        Fade-in matches a group whose start time aligns with the run's
        first note. Fade-out matches a group whose visible-clip-end aligns
        with the run's last note. Unmatched runs are dropped (their tagged
        midi_notes get reverted to regular notes).
        """
        if not fade_runs:
            return
        doc = self._doc

        all_groups: Dict[str, Dict[str, Any]] = {}
        for e in doc.midi_events:
            gid = e.get('group_id')
            if gid and gid not in all_groups:
                all_groups[gid] = {
                    'fixture': doc._event_fixture(e),
                    'time': e['time'],
                }
        # Pre-compute clip ends per group for fade-out matching.
        for gid, info in all_groups.items():
            re = doc._group_right_edge(gid, info['fixture'])
            if re == float('inf'):
                re = doc._song_end_time()
            info['clip_end'] = re

        matched_run_idxs: set = set()
        for run_idx, run in enumerate(fade_runs):
            if run['kind'] == 'in':
                anchor = run['start_time']
                target_gid = next(
                    (gid for gid, info in all_groups.items()
                     if abs(info['time'] - anchor) < NOTE_MATCH_TOLERANCE),
                    None)
            else:
                anchor = run['end_time']
                target_gid = next(
                    (gid for gid, info in all_groups.items()
                     if abs(info['clip_end'] - anchor) < NOTE_MATCH_TOLERANCE),
                    None)
            if target_gid is None:
                continue  # orphan run — leave its midi_notes tags to be cleaned
            duration = run['end_time'] - run['start_time']
            for m in doc._group_members(target_gid):
                if run['kind'] == 'in':
                    m['fade_in'] = duration
                else:
                    m['fade_out'] = duration
            for n in doc.midi_notes:
                if n.get('_fade_run_idx') == run_idx:
                    n['parent_group_id'] = target_gid
            matched_run_idxs.add(run_idx)

        # Clean up: clear the temporary _fade_run_idx on all is_fade notes,
        # and revert is_fade flag for any orphan run that didn't match a
        # group (so they play as regular brightness events instead of being
        # silently dropped from regeneration).
        for n in doc.midi_notes:
            if '_fade_run_idx' not in n:
                continue
            if n['_fade_run_idx'] not in matched_run_idxs:
                n['is_fade'] = False
                n.pop('parent_group_id', None)
            n.pop('_fade_run_idx', None)

    # --- ramp math -----------------------------------------------------------

    def _clamp_fade(self, requested: float, other_fade: float,
                    visible_duration: float) -> float:
        """Clamp a fade duration to [0, visible_duration - other_fade]."""
        room = max(0.0, visible_duration - max(0.0, other_fade))
        return max(0.0, min(requested, room))

    def _linear_fade_ramp(self, start_time: float, duration: float,
                          vel_start: int, vel_end: int,
                          step: float) -> List[Tuple[float, int]]:
        """Generate (time, velocity) tuples for a fade ramp.

        Notes are emitted at ``start_time, start_time+step, ...`` up to and
        INCLUDING ``start_time+duration``. The last point always lands at
        ``start_time+duration`` with velocity ``vel_end`` so endpoint values
        are exact even when ``duration`` isn't a clean multiple of ``step``.

        Velocities are clamped to a MINIMUM of 1: a MIDI ``note_on`` with
        velocity 0 is conventionally interpreted as ``note_off`` on parse,
        which would silently drop the fade endpoint on the next file load.
        Velocity 1 yields a DMX channel value of 2 — visually indis-
        tinguishable from 0 (~0.8% of full brightness) but round-trips
        through the MIDI file cleanly.
        """
        if duration < FADE_MIN_DURATION:
            return []
        notes: List[Tuple[float, int]] = []
        n_full = int(duration / step + 1e-9)
        for i in range(n_full + 1):
            t = start_time + i * step
            if t > start_time + duration + 1e-9:
                break
            frac = (t - start_time) / duration
            vel = round(vel_start + (vel_end - vel_start) * frac)
            notes.append((t, max(1, min(127, int(vel)))))
        end_time = start_time + duration
        if not notes or abs(notes[-1][0] - end_time) > 1e-6:
            notes.append((end_time, max(1, min(127, int(vel_end)))))
        else:
            notes[-1] = (end_time, max(1, min(127, int(vel_end))))
        return notes

    def _generate_fade_notes(self, group_id: str) -> List[Tuple[float, int]]:
        """Compute (time, velocity) tuples for a group's fade-in + fade-out.

        Pure function over current state — caller is responsible for
        wiping prior is_fade entries and inserting these into midi_notes.
        Returns empty when the group has no brightness note (note 1) or
        the brightness velocity is 0.
        """
        doc = self._doc
        members = doc._group_members(group_id)
        if not members:
            return []
        brightness = next((m for m in members
                           if m["note"] == FADE_BRIGHTNESS_NOTE), None)
        if brightness is None or brightness["velocity"] <= 0:
            return []
        target_vel = int(brightness["velocity"])
        fade_in = max(0.0, brightness.get("fade_in", 0.0))
        fade_out = max(0.0, brightness.get("fade_out", 0.0))

        start_time = brightness["time"]
        fixture = doc._event_fixture(brightness)
        right_edge = doc._group_right_edge(group_id, fixture)
        if right_edge == float('inf'):
            right_edge = doc._song_end_time()

        notes: List[Tuple[float, int]] = []
        if fade_in >= FADE_MIN_DURATION:
            notes.extend(self._linear_fade_ramp(
                start_time, fade_in, 0, target_vel, FADE_STEP_SECONDS))
        if fade_out >= FADE_MIN_DURATION:
            notes.extend(self._linear_fade_ramp(
                right_edge - fade_out, fade_out, target_vel, 0,
                FADE_STEP_SECONDS))
        return notes

    # --- generation / mutation ----------------------------------------------

    def regenerate_group(self, group_id: str) -> None:
        """Wipe and rewrite this group's fade notes in midi_notes.

        Idempotent — call after any state change that affects the group's
        position, extent, or brightness. Fade notes are appended after a
        wipe so stable-sort by time keeps them after originals at the same
        timestamp (the rebuild step relies on this for last-write-wins
        ordering of the same-time brightness conflict at scene start).
        """
        doc = self._doc
        doc.midi_notes = [
            n for n in doc.midi_notes
            if not (n.get("is_fade") and n.get("parent_group_id") == group_id)
        ]
        for t, vel in self._generate_fade_notes(group_id):
            doc.midi_notes.append({
                "id": str(uuid.uuid4()),
                "note": FADE_BRIGHTNESS_NOTE,
                "time": t,
                "duration": FADE_NOTE_DURATION,
                "velocity": vel,
                "is_fade": True,
                "parent_group_id": group_id,
            })
        doc.midi_notes.sort(key=lambda x: x["time"])

    def wipe_group(self, group_id: str) -> None:
        """Remove all fade notes for a group (used on delete)."""
        doc = self._doc
        doc.midi_notes = [
            n for n in doc.midi_notes
            if not (n.get("is_fade") and n.get("parent_group_id") == group_id)
        ]

    def clamp_group_to_visible(self, group_id: str) -> None:
        """Shrink fade_in/fade_out values that no longer fit a resized clip.

        Called after a left/right resize. Preserves the relative balance
        between in and out when both have to shrink. Idempotent.
        """
        doc = self._doc
        members = doc._group_members(group_id)
        if not members:
            return
        visible = doc._visible_duration_for_group(group_id)
        fade_in = max(0.0, members[0].get('fade_in', 0.0))
        fade_out = max(0.0, members[0].get('fade_out', 0.0))
        if fade_in + fade_out <= visible + 1e-9:
            return
        if fade_in + fade_out <= 0:
            return
        scale = visible / (fade_in + fade_out)
        new_in = max(0.0, fade_in * scale)
        new_out = max(0.0, fade_out * scale)
        for m in members:
            m['fade_in'] = new_in
            m['fade_out'] = new_out

    # --- UI setters ----------------------------------------------------------
    # set_event_* are UI-only (no MIDI rewrite — single events don't generate
    # fade ramps), so they only notify. set_scene_* regenerate the ramp and
    # commit a full rebuild.

    def set_event_fade_in(self, event_id: str, seconds: float) -> bool:
        doc = self._doc
        idx = doc._index_of(event_id)
        if idx < 0:
            return False
        evt = doc.midi_events[idx]
        clamped = self._clamp_fade(seconds, evt.get('fade_out', 0.0),
                                   doc._visible_duration_for_event(event_id))
        if abs(clamped - evt.get('fade_in', 0.0)) < 1e-6:
            return False
        evt['fade_in'] = clamped
        self._notify_changed()
        return True

    def set_event_fade_out(self, event_id: str, seconds: float) -> bool:
        doc = self._doc
        idx = doc._index_of(event_id)
        if idx < 0:
            return False
        evt = doc.midi_events[idx]
        clamped = self._clamp_fade(seconds, evt.get('fade_in', 0.0),
                                   doc._visible_duration_for_event(event_id))
        if abs(clamped - evt.get('fade_out', 0.0)) < 1e-6:
            return False
        evt['fade_out'] = clamped
        self._notify_changed()
        return True

    def set_scene_fade_in(self, group_id: str, seconds: float) -> bool:
        doc = self._doc
        members = doc._group_members(group_id)
        if not members:
            return False
        other = members[0].get('fade_out', 0.0)
        clamped = self._clamp_fade(seconds, other,
                                   doc._visible_duration_for_group(group_id))
        if abs(clamped - members[0].get('fade_in', 0.0)) < 1e-6:
            return False
        for m in members:
            m['fade_in'] = clamped
        self.regenerate_group(group_id)
        self._commit()
        return True

    def set_scene_fade_out(self, group_id: str, seconds: float) -> bool:
        doc = self._doc
        members = doc._group_members(group_id)
        if not members:
            return False
        other = members[0].get('fade_in', 0.0)
        clamped = self._clamp_fade(seconds, other,
                                   doc._visible_duration_for_group(group_id))
        if abs(clamped - members[0].get('fade_out', 0.0)) < 1e-6:
            return False
        for m in members:
            m['fade_out'] = clamped
        self.regenerate_group(group_id)
        self._commit()
        return True
