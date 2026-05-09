from pathlib import Path
from typing import Optional, List, Dict, Any, Callable, Tuple
from PyQt6.QtCore import QObject, pyqtSignal
import mido
import uuid
import copy

from tupperlogger import get_logger
from software_controller.scene_loader import LedScene, load_led_scenes
from .worker import MidiWorker, MidiConnectionStatus

# Default location of the LED scene definitions used to resolve scene names
# when reconstructing scene groups from a freshly-loaded MIDI file.
_DEFAULT_SCENE_CONFIG_PATH = (Path(__file__).resolve().parent.parent
                              / "software_controller" / "dmx_config.json")


# Identification of BLACKOUT events is by part-name PREFIX (case-insensitive),
# not by note number — keeps it config-driven and lets each fixture have its
# own gap marker (BLACKOUT for ledbar, BLACKOUT_BLINDERS for blinders, ...).
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

# Fade-generation constants (scene groups only). A "fade" is a chain of
# brightness (note 1) note_on events at FADE_STEP_SECONDS apart whose
# velocities ramp linearly between 0 and the group's brightness V.
# Fade notes live ONLY in midi_notes (never in midi_events) so they don't
# pollute scene clustering, timeline rendering, or per-clip mutation logic.
# They round-trip through the saved MIDI file and are detected on load by
# the consecutive-step-with-monotonic-velocity-and-zero-endpoint pattern.
FADE_STEP_SECONDS = 0.10
FADE_NOTE_DURATION = 0.05  # < step so consecutive note_ons don't overlap
FADE_MIN_DURATION = 0.01   # below this, treat as no fade
# Brightness channel-control note number — the fade's target channel.
FADE_BRIGHTNESS_NOTE = 1
# Velocity tolerance when matching a parsed midi_notes entry to a detected
# fade-run member; absorbs the /2 quantization round-trip in MIDI export.
FADE_VELOCITY_MATCH_TOLERANCE = 2

# LED-bar channel-control mapping used by scene clips. Each tuple is
# (json_channel_name, midi_note_number) and matches the cases in
# software_controller/main.py:handle_note_on (notes 1-7).
CHANNEL_NOTE_MAP: Tuple[Tuple[str, int], ...] = (
    ("brightness", 1),
    ("blue", 2),
    ("green", 3),
    ("red", 4),
    ("animation", 5),
    ("speed", 6),
    ("direction", 7),
)
CHANNEL_CONTROL_NOTES = frozenset(n for _, n in CHANNEL_NOTE_MAP)


def _channel_to_velocity(value: int) -> int:
    """Map a 0..255 DMX channel value to a 0..127 MIDI velocity (floor).

    The software controller doubles velocity to recover the channel value, so
    this conversion is intentionally lossy at odd inputs (255 → 127 → 254).
    """
    try:
        v = int(value) // 2
    except (TypeError, ValueError):
        v = 0
    return max(1, min(127, v))


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
        # note_on/note_off pairs (with duration) used to rebuild the MIDI file on save
        self.midi_notes: List[Dict[str, Any]] = []
        self.midi_note_map = midi_note_map or {}
        
        # Track current playback position for in-place updates
        self._current_playback_time: float = 0.0

        # Externally-provided song duration (e.g. the audio file's length).
        # Used as a floor when computing the song-end clamp so clip-edit
        # operations have a sensible range even when the loaded MIDI is
        # sparse — see song_end_time().
        self._song_duration_override: float = 0.0

        self.logger = get_logger("midi.manager")

        # Software controller reference
        self.software_controller = None

        # Lazy-loaded LED scene library used to resolve human-readable names
        # for scene clusters reconstructed from MIDI files.
        self._led_scenes: Optional[List[LedScene]] = None

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
            if msg.type != 'note_on':
                continue
            # Channel-control notes (1-7) are scene channels and a velocity of
            # 0 carries a real "channel = 0" value, so they're admitted. Other
            # notes follow the standard "note_on velocity 0 == note_off"
            # convention and are skipped here.
            if msg.velocity == 0 and msg.note not in CHANNEL_CONTROL_NOTES:
                continue
            note_info = self.midi_note_map.get(str(msg.note))
            if note_info:
                self.midi_events.append({
                    "id": str(uuid.uuid4()),
                    "note": msg.note,
                    "time": current_time,
                    "velocity": msg.velocity,
                    "info": note_info,
                    "group_id": None,
                    "scene_name": None,
                    # Fade durations in seconds. UI-only state for now —
                    # not persisted to the MIDI file. Reset on every load.
                    "fade_in": 0.0,
                    "fade_out": 0.0,
                })

        # Parse note_on/note_off pairs into duration-bearing entries so the
        # MIDI file can be reconstructed on save (see _rebuild_midi_data).
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

        # Detect fade-in/out runs in the loaded note-1 events and migrate
        # them out of midi_events into is_fade-tagged midi_notes entries.
        # MUST run before _reattach_scene_groups so the cluster builder
        # doesn't see the fade-in starter (a duplicate brightness event at
        # scene start with velocity 0) and corrupt the scene-name lookup.
        fade_runs = self._detect_fade_notes_on_load()

        # Reattach scene groups: clusters of ≥2 channel-control events at
        # (essentially) the same time get a fresh shared group_id. Groups
        # are ephemeral — they live only in memory; persistence relies on
        # the same-time clustering rule on every reload.
        self._reattach_scene_groups()

        # Attach detected fade runs to the matching scene groups: sets
        # fade_in/fade_out values on the group's members and stamps the
        # parent_group_id on the corresponding midi_notes entries.
        self._attach_fade_runs_to_groups(fade_runs)

    def _reattach_scene_groups(self) -> None:
        """Cluster channel-control events at the same time into scene groups.

        Heuristic: any 2+ events whose times match within BLACKOUT_EPSILON and
        whose note number is in CHANNEL_CONTROL_NOTES form one scene clip.
        Existing group_id assignments are preserved (idempotent re-entry).
        """
        self.midi_events.sort(key=lambda x: x["time"])
        i = 0
        while i < len(self.midi_events):
            e = self.midi_events[i]
            if (e["note"] not in CHANNEL_CONTROL_NOTES
                    or e.get("group_id")):
                i += 1
                continue
            cluster = [e]
            j = i + 1
            while j < len(self.midi_events):
                f = self.midi_events[j]
                if abs(f["time"] - e["time"]) > BLACKOUT_EPSILON:
                    break
                if (f["note"] in CHANNEL_CONTROL_NOTES
                        and not f.get("group_id")):
                    cluster.append(f)
                j += 1
            if len(cluster) >= 2:
                gid = str(uuid.uuid4())
                # Prefer the real name from the LED scene library; fall back
                # to a time-based label for partial or hand-edited clusters
                # that don't match any library entry.
                scene_name = (self._lookup_scene_name(cluster)
                              or f"Scene @ {e['time']:.2f}s")
                for c in cluster:
                    c["group_id"] = gid
                    c["scene_name"] = scene_name
            i += 1

    def _detect_fade_notes_on_load(self) -> List[Dict[str, Any]]:
        """Spot fade ramps in freshly-parsed midi_events; migrate to midi_notes.

        Heuristic: any contiguous run of note-1 events at FADE_STEP_SECONDS
        apart (within NOTE_MATCH_TOLERANCE) with monotonic velocities and a
        zero endpoint is a fade. Detected events are removed from
        midi_events and the matching midi_notes entries are tagged
        is_fade=True. The parent_group_id is filled in later by
        _attach_fade_runs_to_groups once scene clustering has run.

        Returns the list of detected runs so the attach pass can find them.
        """
        self.midi_events.sort(key=lambda x: x['time'])

        # Filter to note-1 (brightness) events; secondary sort by velocity
        # so at same-time conflicts the smaller velocity comes first. This
        # is what lets fade endpoints (vel=0) win when they coincide with
        # an original brightness note (vel=V): for fade-in, the original
        # vel=V at scene_start sits next to the fade starter vel=0; for
        # fade-out, the fade ender vel=0 at clip_end sits next to the next
        # group's vel=V. Putting vel=0 first means the run-extender picks
        # the fade member, keeping the run monotonic.
        candidates = sorted(
            (e for e in self.midi_events if e['note'] == FADE_BRIGHTNESS_NOTE),
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
                expected = run_members[-1]['time'] + FADE_STEP_SECONDS
                if cand['time'] > expected + NOTE_MATCH_TOLERANCE:
                    break  # gap too large — run ends here
                if cand['time'] < expected - NOTE_MATCH_TOLERANCE:
                    # Same-time / sub-step duplicate (e.g. the original
                    # group's brightness note coincident with the fade
                    # starter). Skip it without breaking the run.
                    continue
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
        # `_fade_run_idx` field — _attach_fade_runs_to_groups consumes and
        # clears it.
        for run_idx, run in enumerate(runs):
            for m in run['members']:
                # Find midi_notes entry — first untagged matching one wins.
                for n in self.midi_notes:
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
        self.midi_events = [e for e in self.midi_events
                            if e['id'] not in detected_ids]
        return runs

    def _attach_fade_runs_to_groups(self,
                                    fade_runs: List[Dict[str, Any]]) -> None:
        """Match each detected fade run to a scene group and fill in metadata.

        Fade-in matches a group whose start time aligns with the run's
        first note. Fade-out matches a group whose visible-clip-end aligns
        with the run's last note. Unmatched runs are dropped (their tagged
        midi_notes get reverted to regular notes).
        """
        if not fade_runs:
            return

        all_groups: Dict[str, Dict[str, Any]] = {}
        for e in self.midi_events:
            gid = e.get('group_id')
            if gid and gid not in all_groups:
                all_groups[gid] = {
                    'fixture': self._event_fixture(e),
                    'time': e['time'],
                }
        # Pre-compute clip ends per group for fade-out matching.
        for gid, info in all_groups.items():
            re = self._group_right_edge(gid, info['fixture'])
            if re == float('inf'):
                re = self._song_end_time()
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
            for m in self._group_members(target_gid):
                if run['kind'] == 'in':
                    m['fade_in'] = duration
                else:
                    m['fade_out'] = duration
            for n in self.midi_notes:
                if n.get('_fade_run_idx') == run_idx:
                    n['parent_group_id'] = target_gid
            matched_run_idxs.add(run_idx)

        # Clean up: clear the temporary _fade_run_idx on all is_fade notes,
        # and revert is_fade flag for any orphan run that didn't match a
        # group (so they play as regular brightness events instead of being
        # silently dropped from regeneration).
        for n in self.midi_notes:
            if '_fade_run_idx' not in n:
                continue
            if n['_fade_run_idx'] not in matched_run_idxs:
                n['is_fade'] = False
                n.pop('parent_group_id', None)
            n.pop('_fade_run_idx', None)

    def _get_led_scenes(self) -> List[LedScene]:
        """Return the cached LED scene library, loading it on first access."""
        if self._led_scenes is None:
            try:
                self._led_scenes = load_led_scenes(_DEFAULT_SCENE_CONFIG_PATH)
            except Exception as exc:
                self.logger.warning(
                    f"Could not load LED scene library from "
                    f"{_DEFAULT_SCENE_CONFIG_PATH}: {exc}"
                )
                self._led_scenes = []
        return self._led_scenes

    def _lookup_scene_name(self, cluster: List[Dict[str, Any]]) -> Optional[str]:
        """Match a freshly-clustered scene against the LED scene library.

        Comparison happens in MIDI-velocity space (the same lossy domain the
        scene values live in once embedded in a MIDI file), so 254/255 raw
        channel values both collapse to velocity 127 and still match.
        Returns the matching scene's name, or None if no exact match exists.
        """
        note_to_channel = {note: ch for ch, note in CHANNEL_NOTE_MAP}
        cluster_vels: Dict[str, int] = {}
        for ev in cluster:
            ch = note_to_channel.get(ev["note"])
            if ch is None:
                continue
            cluster_vels[ch] = int(ev["velocity"])
        # Need all 7 channels present for an unambiguous match.
        if len(cluster_vels) != len(CHANNEL_NOTE_MAP):
            return None
        for scene in self._get_led_scenes():
            scene_vels = {
                "brightness": _channel_to_velocity(scene.brightness),
                "blue": _channel_to_velocity(scene.blue),
                "green": _channel_to_velocity(scene.green),
                "red": _channel_to_velocity(scene.red),
                "animation": _channel_to_velocity(scene.animation),
                "speed": _channel_to_velocity(scene.speed),
                "direction": _channel_to_velocity(scene.direction),
            }
            if scene_vels == cluster_vels:
                return scene.name
        return None
                    
    def get_events_for_ui(self) -> List[Dict[str, Any]]:
        """Get MIDI events formatted for Timeline display."""
        return copy.deepcopy(self.midi_events)

    def get_duration(self) -> float:
        """Get the total duration of the MIDI data."""
        if self.midi_data:
            return self.midi_data.length
        return 0.0

    def set_song_duration(self, duration: float) -> None:
        """Hint at the song's actual end time (e.g. the audio file's length).

        Used as a floor for song_end_time() so clip-edit bounds aren't
        pinned to the (potentially tiny) MIDI duration after the first
        clip is dropped on an otherwise-empty track.
        """
        self._song_duration_override = max(0.0, float(duration))

    def song_end_time(self) -> float:
        """Effective end-of-timeline for clip placement and visual extents.

        Returns the maximum of:
          * the loaded MIDI's actual length;
          * any externally-provided audio duration (set_song_duration);
          * a 240s default fallback.
        """
        midi_length = self.midi_data.length if self.midi_data else 0.0
        return max(midi_length, self._song_duration_override, 240.0)

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
            "info": self.midi_note_map[str(note)],
            "group_id": None,
            "scene_name": None,
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
        for i, note_data in enumerate(self.midi_notes):
            if note_data['time'] > time:
                self.midi_notes.insert(i, new_note)
                inserted = True
                break
        if not inserted:
            self.midi_notes.append(new_note)

        # Reconcile so that dropping a clip exactly where a BLACKOUT cap
        # already sits drops the now-zero-duration BLACKOUT instead of
        # leaving it as a width-0 ghost in front of the new clip.
        self.reconcile_blackouts(self._event_fixture(new_event))
        self._commit()
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
        """Return True if `event` is any BLACKOUT* gap marker (prefix match)."""
        info = event.get('info') if event else None
        if info is None:
            return False
        part = getattr(info, 'part', '') or ''
        return part.upper().startswith(BLACKOUT_PART_NAME)

    def _event_fixture(self, event: Dict[str, Any]) -> Optional[str]:
        """Return the fixture this event belongs to ('ledbar', 'blinder', ...)."""
        info = event.get('info') if event else None
        return getattr(info, 'fixture', None) if info else None

    def _blackout_note_number(self, fixture: Optional[str] = None) -> Optional[int]:
        """Return the MIDI note number mapped to BLACKOUT* for `fixture`.

        If `fixture` is None, returns the first match found (legacy behavior).
        """
        for note_str, info in self.midi_note_map.items():
            if not getattr(info, 'part', '').upper().startswith(BLACKOUT_PART_NAME):
                continue
            if fixture is not None and getattr(info, 'fixture', None) != fixture:
                continue
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

    def _right_edge_of(self, idx: int, fixture: Optional[str] = None) -> float:
        """Visual right edge of event at index idx.

        If `fixture` is given, returns the time of the next event of the
        same fixture (so each track is scoped independently); otherwise
        falls back to the next event globally.
        """
        if fixture is None:
            if 0 <= idx < len(self.midi_events) - 1:
                return self.midi_events[idx + 1]['time']
            return float('inf')
        for j in range(idx + 1, len(self.midi_events)):
            if self._event_fixture(self.midi_events[j]) == fixture:
                return self.midi_events[j]['time']
        return float('inf')

    def _song_end_time(self) -> float:
        """Backwards-compatible alias for song_end_time()."""
        return self.song_end_time()

    def _non_blackout_intervals(self, exclude_event_id: Optional[str] = None,
                                fixture: Optional[str] = None
                                ) -> List[Tuple[float, float, str]]:
        """Visible intervals of non-BLACKOUT clips, sorted by start.

        Each clip's interval = [clip.time, next_event.time). The last event's
        interval extends to song_end. Excludes `exclude_event_id` if given.
        If `fixture` is given, only events of that fixture are considered and
        each clip's right edge becomes the next *same-fixture* event.
        """
        song_end = self._song_end_time()
        candidates: List[Dict[str, Any]] = (
            [e for e in self.midi_events if self._event_fixture(e) == fixture]
            if fixture is not None else self.midi_events
        )
        intervals: List[Tuple[float, float, str]] = []
        for i, e in enumerate(candidates):
            if self.is_blackout(e) or e['id'] == exclude_event_id:
                continue
            end = (candidates[i + 1]['time']
                   if i + 1 < len(candidates) else song_end)
            intervals.append((e['time'], end, e['id']))
        intervals.sort(key=lambda x: x[0])
        return intervals

    def _free_slots_for_move(self, exclude_event_id: str,
                             fixture: Optional[str] = None
                             ) -> List[Tuple[float, float]]:
        """Compute (start, end) intervals where a clip can be placed without
        overlapping any non-BLACKOUT clip of the same fixture (excluding
        `exclude_event_id`)."""
        obstacles = self._non_blackout_intervals(exclude_event_id, fixture)
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

    def _insert_blackout_internal(self, time: float,
                                  fixture: Optional[str] = None) -> Optional[str]:
        """Insert a BLACKOUT event+note at `time`. No rebuild/emit. Returns new id, or None."""
        bo_note = self._blackout_note_number(fixture)
        if bo_note is None:
            self.logger.warning(
                f"Cannot auto-insert BLACKOUT for fixture={fixture!r}: "
                "no matching entry in midi_note_map"
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
            "group_id": None,
            "scene_name": None,
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
        # Skip is_fade entries — those are owned by their parent scene group,
        # not by this event, even when their (note, time) coincide with it.
        for i, note in enumerate(self.midi_notes):
            if note.get('is_fade'):
                continue
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
                # Mirror in midi_notes (matched by note + old time). Skip
                # fade notes — they are regenerated separately by the caller.
                for note in self.midi_notes:
                    if note.get('is_fade'):
                        continue
                    if (note['note'] == e['note']
                            and abs(note['time'] - old_time) < NOTE_MATCH_TOLERANCE):
                        note['time'] = new_time
                        break
                self.midi_events.sort(key=lambda x: x['time'])
                self.midi_notes.sort(key=lambda x: x['time'])
                return True
        return False

    def reconcile_blackouts(self, fixture: Optional[str] = None) -> None:
        """Enforce BLACKOUT invariants per fixture. Idempotent.

        BLACKOUTs are scoped to their own fixture: a blinder BLACKOUT
        sandwiched between two ledbar events should NOT be merged or
        dropped based on those neighbors. When `fixture` is None, every
        fixture present in the data is reconciled in turn.
        """
        if fixture is None:
            fixtures = {self._event_fixture(e) for e in self.midi_events}
            fixtures.discard(None)
            for f in fixtures:
                self.reconcile_blackouts(f)
            return

        # Sort first
        self.midi_events.sort(key=lambda x: x['time'])
        self.midi_notes.sort(key=lambda x: x['time'])

        # Pass 1: drop zero/negative-duration same-fixture BLACKOUTs.
        # A BLACKOUT's effective duration = (next same-fixture event's time) - (its time).
        # If <= BLACKOUT_EPSILON, drop it.
        dropped = True
        while dropped:
            dropped = False
            filtered = [e for e in self.midi_events
                        if self._event_fixture(e) == fixture]
            for i, e in enumerate(filtered):
                if not self.is_blackout(e):
                    continue
                next_time = (filtered[i + 1]['time']
                             if i + 1 < len(filtered) else float('inf'))
                if next_time - e['time'] <= BLACKOUT_EPSILON:
                    self._delete_event_internal(e['id'])
                    dropped = True
                    break  # restart loop after mutation

        # Pass 2: merge adjacent same-fixture BLACKOUTs (delete the later one).
        merged = True
        while merged:
            merged = False
            filtered = [e for e in self.midi_events
                        if self._event_fixture(e) == fixture]
            for i in range(len(filtered) - 1):
                if self.is_blackout(filtered[i]) and self.is_blackout(filtered[i + 1]):
                    self._delete_event_internal(filtered[i + 1]['id'])
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
        fixture = self._event_fixture(target)
        old_time = target['time']
        target_old_right = self._right_edge_of(idx, fixture)
        song_end = self._song_end_time()
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
        for i, e in enumerate(self.midi_events):
            if self._event_fixture(e) != fixture or self.is_blackout(e):
                continue
            end = self._right_edge_of(i, fixture)
            if end == float('inf'):
                end = song_end
            intended_ends[e['id']] = end

        # Find the slot whose clamped placement is closest to `new_time`.
        slots = self._free_slots_for_move(event_id, fixture)
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
        for j in range(idx + 1, len(self.midi_events)):
            e = self.midi_events[j]
            if self._event_fixture(e) != fixture:
                continue
            if self.is_blackout(e):
                old_trailing_cap_id = e['id']
            break

        # Apply the move.
        self._set_event_time_internal(event_id, best_new_time)

        # Drop any same-fixture BLACKOUT that now sits inside target's new
        # visible span (including at the very start). Without this step the
        # target's "next event" can be a BLACKOUT at distance 0 (sort
        # stability puts a same-time BLACKOUT after the moved event),
        # collapsing the clip's visible width to zero — i.e. it disappears.
        # Other fixtures' BLACKOUTs are untouched.
        stale_blackouts = [
            e['id'] for e in self.midi_events
            if e['id'] != event_id
            and self._event_fixture(e) == fixture
            and self.is_blackout(e)
            and best_new_time - BLACKOUT_EPSILON <= e['time'] < new_target_end - BLACKOUT_EPSILON
        ]
        # The old trailing cap may sit outside the new visible span (e.g.
        # for a long-range move) but still needs to go.
        if old_trailing_cap_id is not None and old_trailing_cap_id not in stale_blackouts:
            stale_blackouts.append(old_trailing_cap_id)
        for bo_id in stale_blackouts:
            self._delete_event_internal(bo_id)

        # Restore each same-fixture non-BLACKOUT's intended extent: if its
        # actual next same-fixture event is now past its intended end,
        # insert a BLACKOUT (of the right fixture) at the intended end.
        for eid, intended_end in intended_ends.items():
            e_idx = self._index_of(eid)
            if e_idx < 0:
                continue
            actual_end = self._right_edge_of(e_idx, fixture)
            if actual_end == float('inf'):
                actual_end = song_end
            if actual_end > intended_end + BLACKOUT_EPSILON:
                self._insert_blackout_internal(intended_end, fixture)

        # Reconcile only this fixture (drops zero-duration BLACKOUTs and
        # merges adjacent ones within the same track).
        self.reconcile_blackouts(fixture)

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
        fixture = self._event_fixture(target)
        old_time = target['time']

        # Clamp range
        # Right limit: keep at least MIN_EVENT_DURATION before next event of
        # the same fixture. If the clip is the last on its track, its visual
        # extent stretches to song_end, so clamp to song_end - MIN_DURATION
        # rather than leaving max_new_time unbounded.
        right_edge = self._right_edge_of(idx, fixture)
        max_new_time = (right_edge - MIN_EVENT_DURATION
                        if right_edge != float('inf')
                        else self._song_end_time() - MIN_EVENT_DURATION)
        # Left limit: cannot cross immediate non-BLACKOUT same-fixture
        # predecessor + MIN_DURATION. If prev is a BLACKOUT of the same
        # fixture, we can extend through it (consuming) up to that BLACKOUT's
        # predecessor. Reconcile will delete the consumed BLACKOUT.
        min_new_time = 0.0
        prev_same: Optional[Dict[str, Any]] = None
        for i in range(idx - 1, -1, -1):
            e = self.midi_events[i]
            if self._event_fixture(e) != fixture:
                continue
            if prev_same is None:
                prev_same = e
            if not self.is_blackout(e):
                min_new_time = e['time'] + MIN_EVENT_DURATION
                break

        new_time = max(min_new_time, min(new_time, max_new_time))
        delta = new_time - old_time
        if abs(delta) < BLACKOUT_EPSILON:
            return False

        # Capture immediate same-fixture prev state before mutation.
        prev_id = prev_same['id'] if prev_same is not None else None
        prev_was_blackout = (prev_same is not None and self.is_blackout(prev_same))

        # Apply target time change.
        self._set_event_time_internal(event_id, new_time)

        # If we shrunk (delta > 0) and the immediate same-fixture prev is
        # non-BLACKOUT (or there is no prev of this fixture), insert a
        # fixture-matching BLACKOUT to fill the gap.
        if delta > 0 and not prev_was_blackout and prev_id is not None:
            self._insert_blackout_internal(old_time, fixture)
        # If we extended (delta < 0) and prev was BLACKOUT, it shrinks naturally
        # (its time stays, target.time decreased → its right edge moved left).
        # reconcile_blackouts() will delete it if duration ≤ epsilon.

        self.reconcile_blackouts(fixture)
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
        fixture = self._event_fixture(target)

        # Clamp range
        # Left limit: at least MIN_EVENT_DURATION after target's start.
        min_right = target['time'] + MIN_EVENT_DURATION
        # Right limit: cannot extend past the next non-BLACKOUT same-fixture
        # event (or infinity). Other fixtures are scoped independently.
        max_right = float('inf')
        for i in range(idx + 1, len(self.midi_events)):
            e = self.midi_events[i]
            if self._event_fixture(e) != fixture:
                continue
            if not self.is_blackout(e):
                max_right = e['time']
                break

        new_right_time = max(min_right, min(new_right_time, max_right))

        # Find the next same-fixture event (BLACKOUT or not) — the gap
        # marker we must move/insert to express the new right edge.
        next_event: Optional[Dict[str, Any]] = None
        for i in range(idx + 1, len(self.midi_events)):
            e = self.midi_events[i]
            if self._event_fixture(e) == fixture:
                next_event = e
                break

        if next_event is not None and self.is_blackout(next_event):
            # Move the existing BLACKOUT to new_right_time. Reconcile handles deletion if zero.
            if abs(next_event['time'] - new_right_time) < BLACKOUT_EPSILON:
                return False
            self._set_event_time_internal(next_event['id'], new_right_time)
        else:
            # No same-fixture BLACKOUT yet. Determine if we need to insert one.
            current_right = self._right_edge_of(idx, fixture)
            if new_right_time < current_right - BLACKOUT_EPSILON:
                # Shrinking: insert fixture-matching BLACKOUT at new_right_time.
                self._insert_blackout_internal(new_right_time, fixture)
            else:
                # No-op: cannot extend past non-BLACKOUT next (already clamped).
                return False

        self.reconcile_blackouts(fixture)
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
        idx = self._index_of(event_id)
        if idx < 0:
            return False
        event = self.midi_events[idx]
        if self.is_blackout(event):
            return False

        fixture = self._event_fixture(event)
        bo_note = self._blackout_note_number(fixture)
        if bo_note is None:
            self.logger.warning(
                f"Cannot replace event {event_id} with BLACKOUT: "
                f"no BLACKOUT note mapped for fixture={fixture!r}"
            )
            return False

        old_note = event['note']
        old_time = event['time']
        event['note'] = bo_note
        event['info'] = self.midi_note_map[str(bo_note)]
        for note in self.midi_notes:
            if (note['note'] == old_note
                    and abs(note['time'] - old_time) < NOTE_MATCH_TOLERANCE):
                note['note'] = bo_note
                break

        self.reconcile_blackouts(fixture)
        self._commit()
        self.logger.info(
            f"Replaced event {event_id} (note={old_note}, "
            f"time={old_time:.3f}s, fixture={fixture!r}) with BLACKOUT"
        )
        return True

    # ------------------------------------------------------------------
    # Scene-clip ops (atomic operations on a group of 7 channel events)
    # ------------------------------------------------------------------
    #
    # A "scene" is 7 simultaneous note_on events on the LED-bar fixture, one
    # per DMX channel (notes 1..7). They share a `group_id` while in memory;
    # on file save the group_id is dropped and only the 7 same-time note_ons
    # remain — _reattach_scene_groups() rebuilds the group on next load.

    def _add_event_internal(self, note: int, time: float, velocity: int,
                            *, group_id: Optional[str] = None,
                            scene_name: Optional[str] = None) -> Optional[str]:
        """Insert one event+note without rebuild/commit. Returns event_id."""
        if str(note) not in self.midi_note_map:
            return None
        event_id = str(uuid.uuid4())
        self.midi_events.append({
            "id": event_id,
            "note": note,
            "time": time,
            "velocity": velocity,
            "info": self.midi_note_map[str(note)],
            "group_id": group_id,
            "scene_name": scene_name,
            "fade_in": 0.0,
            "fade_out": 0.0,
        })
        self.midi_events.sort(key=lambda x: x["time"])

        self.midi_notes.append({
            "id": str(uuid.uuid4()),
            "note": note,
            "time": time,
            "velocity": velocity,
            "duration": 0.1,
        })
        self.midi_notes.sort(key=lambda x: x["time"])
        return event_id

    def _group_members(self, group_id: str) -> List[Dict[str, Any]]:
        return [e for e in self.midi_events if e.get("group_id") == group_id]

    def _group_right_edge(self, group_id: str,
                          fixture: Optional[str]) -> float:
        """Time of the first non-member same-fixture event after the group."""
        members = self._group_members(group_id)
        if not members:
            return float('inf')
        group_time = members[0]["time"]
        member_ids = {e["id"] for e in members}
        nearest = float('inf')
        for e in self.midi_events:
            if (self._event_fixture(e) == fixture
                    and e["id"] not in member_ids
                    and e["time"] > group_time + BLACKOUT_EPSILON
                    and e["time"] < nearest):
                nearest = e["time"]
        return nearest

    def _free_slots_for_group_move(self, group_id: str,
                                   fixture: Optional[str]
                                   ) -> List[Tuple[float, float]]:
        """Slots where the group can land. All group members excluded as obstacles."""
        member_ids = {e["id"] for e in self._group_members(group_id)}
        candidates = [e for e in self.midi_events
                      if self._event_fixture(e) == fixture]
        obstacles: List[Tuple[float, float]] = []
        for i, e in enumerate(candidates):
            if self.is_blackout(e) or e["id"] in member_ids:
                continue
            end = float('inf')
            for j in range(i + 1, len(candidates)):
                if candidates[j]["id"] in member_ids:
                    continue
                end = candidates[j]["time"]
                break
            if end == float('inf'):
                end = self._song_end_time()
            obstacles.append((e["time"], end))
        obstacles.sort(key=lambda x: x[0])

        slots: List[Tuple[float, float]] = []
        cursor = 0.0
        for start, end in obstacles:
            if start > cursor:
                slots.append((cursor, start))
            cursor = max(cursor, end)
        song_end = self._song_end_time()
        if cursor < song_end:
            slots.append((cursor, song_end))
        return slots

    def add_scene(self, time: float, channels: Dict[str, int],
                  name: str) -> Optional[str]:
        """Create a 7-event LED scene group at `time`. Returns group_id or None."""
        group_id = str(uuid.uuid4())
        added = 0
        for ch_name, note in CHANNEL_NOTE_MAP:
            if ch_name not in channels:
                continue
            velocity = _channel_to_velocity(channels[ch_name])
            if self._add_event_internal(note, time, velocity,
                                        group_id=group_id,
                                        scene_name=name) is not None:
                added += 1
        if added == 0:
            self.logger.warning(
                f"add_scene at {time:.3f}s produced 0 events "
                "(channel-control notes missing from midi_note_map?)"
            )
            return None
        # Reconcile in case the scene landed on top of a cap-BLACKOUT;
        # otherwise reconcile would have to wait for a later mutation to
        # clear the now-zero-duration BLACKOUT.
        members = self._group_members(group_id)
        if members:
            self.reconcile_blackouts(self._event_fixture(members[0]))
        self._commit()
        self.logger.info(
            f"Added scene '{name}' at {time:.3f}s ({added} channels, "
            f"group_id={group_id})"
        )
        return group_id

    def move_scene(self, group_id: str, new_time: float) -> bool:
        """Move all events sharing group_id to new_time atomically."""
        members = self._group_members(group_id)
        if not members:
            return False
        fixture = self._event_fixture(members[0])
        old_time = members[0]["time"]
        member_ids = {e["id"] for e in members}

        target_old_right = self._group_right_edge(group_id, fixture)
        song_end = self._song_end_time()
        # When nothing follows the group, target width is just the minimum —
        # the group can move freely. Otherwise preserve the clip's visible
        # width so it doesn't overlap its successor after the move.
        if target_old_right == float('inf'):
            target_width = MIN_EVENT_DURATION
        else:
            target_width = max(MIN_EVENT_DURATION, target_old_right - old_time)
        if new_time < 0:
            new_time = 0.0

        # Snapshot intended ends for OTHER same-fixture non-BLACKOUT clips.
        intended_ends: Dict[str, float] = {}
        for i, e in enumerate(self.midi_events):
            if (self._event_fixture(e) != fixture
                    or self.is_blackout(e)
                    or e.get("group_id") == group_id):
                continue
            end = self._right_edge_of(i, fixture)
            if end == float('inf'):
                end = song_end
            intended_ends[e["id"]] = end

        slots = self._free_slots_for_group_move(group_id, fixture)
        best_new_time: Optional[float] = None
        best_distance = float('inf')
        for slot_start, slot_end in slots:
            if slot_end - slot_start + BLACKOUT_EPSILON < target_width:
                continue
            clamped = max(slot_start, min(new_time, slot_end - target_width))
            d = abs(clamped - new_time)
            if d < best_distance:
                best_distance = d
                best_new_time = clamped
        if best_new_time is None:
            self.logger.debug(
                f"move_scene {group_id}: no slot fits target width "
                f"{target_width:.3f}s"
            )
            return False
        if abs(best_new_time - old_time) < BLACKOUT_EPSILON:
            return False

        new_target_end = best_new_time + target_width

        # Capture the group's old trailing cap (if any) BEFORE the move so
        # it travels with the clip — same reason as move_event. We look for
        # the first non-member same-fixture event after the group's start.
        old_trailing_cap_id: Optional[str] = None
        sorted_after: List[Dict[str, Any]] = sorted(
            (e for e in self.midi_events
             if self._event_fixture(e) == fixture
             and e["id"] not in member_ids
             and e["time"] > old_time + BLACKOUT_EPSILON),
            key=lambda e: e["time"],
        )
        if sorted_after and self.is_blackout(sorted_after[0]):
            old_trailing_cap_id = sorted_after[0]["id"]

        for eid in member_ids:
            self._set_event_time_internal(eid, best_new_time)

        # Drop stale BLACKOUTs inside the group's new visible span (start
        # inclusive — same reason as move_event: a same-time BLACKOUT at
        # the new position would otherwise collapse the visible width).
        stale = [e["id"] for e in self.midi_events
                 if e["id"] not in member_ids
                 and self._event_fixture(e) == fixture
                 and self.is_blackout(e)
                 and best_new_time - BLACKOUT_EPSILON <= e["time"]
                 < new_target_end - BLACKOUT_EPSILON]
        if old_trailing_cap_id is not None and old_trailing_cap_id not in stale:
            stale.append(old_trailing_cap_id)
        for bid in stale:
            self._delete_event_internal(bid)

        # Restore intended ends for other same-fixture clips.
        for eid, intended_end in intended_ends.items():
            idx = self._index_of(eid)
            if idx < 0:
                continue
            actual_end = self._right_edge_of(idx, fixture)
            if actual_end == float('inf'):
                actual_end = song_end
            if actual_end > intended_end + BLACKOUT_EPSILON:
                self._insert_blackout_internal(intended_end, fixture)

        # Re-cap the group itself: when the group had a trailing cap before
        # the move, that cap was deleted (it travels with the clip). The
        # equivalent of move_event's `intended_ends[event_id] override`
        # lives here — group members are excluded from `intended_ends`,
        # so we have to insert the new cap explicitly. We use
        # _group_right_edge to look at non-member same-fixture neighbors.
        if target_old_right != float('inf'):
            group_actual_end = self._group_right_edge(group_id, fixture)
            if group_actual_end == float('inf'):
                group_actual_end = song_end
            if group_actual_end > new_target_end + BLACKOUT_EPSILON:
                self._insert_blackout_internal(new_target_end, fixture)

        self._regenerate_group_fades(group_id)
        self.reconcile_blackouts(fixture)
        self._commit()
        self.logger.info(
            f"Moved scene {group_id} from {old_time:.3f}s to "
            f"{best_new_time:.3f}s"
        )
        return True

    def update_scene(self, group_id: str, channels: Dict[str, int],
                     name: Optional[str] = None) -> bool:
        """Update channel velocities (and optionally name) of a scene group.

        Time and group membership stay unchanged. For each known channel in
        `channels`, the matching event's velocity is set to channel_value//2.
        Channels missing from the dict are left alone.
        """
        members = self._group_members(group_id)
        if not members:
            return False
        members_by_note = {m["note"]: m for m in members}

        for ch_name, note in CHANNEL_NOTE_MAP:
            if ch_name not in channels:
                continue
            evt = members_by_note.get(note)
            if evt is None:
                continue
            new_vel = _channel_to_velocity(channels[ch_name])
            evt["velocity"] = new_vel
            # Mirror in midi_notes (matched by note + time). Skip is_fade
            # entries — they belong to the group's fade ramp, not the
            # original channel-control event.
            for n in self.midi_notes:
                if n.get("is_fade"):
                    continue
                if (n["note"] == note
                        and abs(n["time"] - evt["time"]) < NOTE_MATCH_TOLERANCE):
                    n["velocity"] = new_vel
                    break

        if name is not None:
            for m in members:
                m["scene_name"] = name

        # Brightness V may have changed → fade ramp endpoint changes.
        self._regenerate_group_fades(group_id)
        self._commit()
        self.logger.info(
            f"Updated scene {group_id}"
            + (f" (renamed to '{name}')" if name is not None else "")
        )
        return True

    def delete_scene(self, group_id: str) -> bool:
        """Remove all members of the group; insert a single BLACKOUT at group_time."""
        members = self._group_members(group_id)
        if not members:
            return False
        fixture = self._event_fixture(members[0])
        group_time = members[0]["time"]
        # Wipe fade notes BEFORE deleting the members; once members are gone
        # _regenerate_group_fades wouldn't know what to wipe (no group anchor).
        self._wipe_group_fades(group_id)
        for e in list(members):
            self._delete_event_internal(e["id"])
        self._insert_blackout_internal(group_time, fixture)
        self.reconcile_blackouts(fixture)
        self._commit()
        self.logger.info(f"Deleted scene {group_id} (replaced with BLACKOUT)")
        return True

    def set_scene_left_edge(self, group_id: str, new_time: float) -> bool:
        """Resize a scene from the left — shifts all members' time together."""
        members = self._group_members(group_id)
        if not members:
            return False
        fixture = self._event_fixture(members[0])
        old_time = members[0]["time"]
        member_ids = {e["id"] for e in members}

        right_edge = self._group_right_edge(group_id, fixture)
        max_new_time = (right_edge - MIN_EVENT_DURATION
                        if right_edge != float('inf') else float('inf'))

        # Walk back through same-fixture non-member events sorted by time.
        prev_candidates = sorted(
            (e for e in self.midi_events
             if self._event_fixture(e) == fixture
             and e["id"] not in member_ids
             and e["time"] < old_time - BLACKOUT_EPSILON),
            key=lambda e: e["time"],
        )
        prev_same: Optional[Dict[str, Any]] = (
            prev_candidates[-1] if prev_candidates else None
        )
        min_new_time = 0.0
        for e in reversed(prev_candidates):
            if not self.is_blackout(e):
                min_new_time = e["time"] + MIN_EVENT_DURATION
                break

        new_time = max(min_new_time, min(new_time, max_new_time))
        delta = new_time - old_time
        if abs(delta) < BLACKOUT_EPSILON:
            return False

        prev_was_blackout = (prev_same is not None
                             and self.is_blackout(prev_same))

        for eid in member_ids:
            self._set_event_time_internal(eid, new_time)

        # If we shrunk and the immediate predecessor is non-BLACKOUT, fill the
        # exposed gap with a fixture-matching BLACKOUT.
        if delta > 0 and not prev_was_blackout and prev_same is not None:
            self._insert_blackout_internal(old_time, fixture)

        self._clamp_group_fades_to_visible_duration(group_id)
        self._regenerate_group_fades(group_id)
        self.reconcile_blackouts(fixture)
        self._commit()
        self.logger.info(
            f"Resized scene {group_id} left edge {old_time:.3f}s → "
            f"{new_time:.3f}s"
        )
        return True

    def set_scene_right_edge(self, group_id: str,
                             new_right_time: float) -> bool:
        """Resize a scene from the right — moves/inserts the trailing BLACKOUT."""
        members = self._group_members(group_id)
        if not members:
            return False
        fixture = self._event_fixture(members[0])
        group_time = members[0]["time"]
        member_ids = {e["id"] for e in members}

        min_right = group_time + MIN_EVENT_DURATION

        # Find the next non-member same-fixture non-BLACKOUT (caps the right edge)
        # and the next non-member same-fixture event of any kind (the gap marker
        # we may need to move).
        max_right = float('inf')
        next_event: Optional[Dict[str, Any]] = None
        for e in self.midi_events:
            if (self._event_fixture(e) != fixture
                    or e["id"] in member_ids
                    or e["time"] <= group_time + BLACKOUT_EPSILON):
                continue
            if next_event is None:
                next_event = e
            if not self.is_blackout(e):
                max_right = e["time"]
                break

        new_right_time = max(min_right, min(new_right_time, max_right))

        if next_event is not None and self.is_blackout(next_event):
            if abs(next_event["time"] - new_right_time) < BLACKOUT_EPSILON:
                return False
            self._set_event_time_internal(next_event["id"], new_right_time)
        else:
            current_right = (max_right if max_right != float('inf')
                             else self._song_end_time())
            if new_right_time < current_right - BLACKOUT_EPSILON:
                self._insert_blackout_internal(new_right_time, fixture)
            else:
                return False

        self._clamp_group_fades_to_visible_duration(group_id)
        self._regenerate_group_fades(group_id)
        self.reconcile_blackouts(fixture)
        self._commit()
        self.logger.info(
            f"Resized scene {group_id} right edge to {new_right_time:.3f}s"
        )
        return True

    # --- Fades ----------------------------------------------------------------
    # UI-only state for now: fade_in / fade_out (seconds) live on the event
    # dict and are not persisted to the MIDI file. data_changed is emitted on
    # change so the timeline redraws; _commit is intentionally NOT called
    # because no MIDI message rewrite is needed.

    def _visible_duration_for_event(self, event_id: str) -> float:
        """Visible width of a clip in seconds (next same-fixture event - this)."""
        idx = self._index_of(event_id)
        if idx < 0:
            return 0.0
        fixture = self._event_fixture(self.midi_events[idx])
        right = self._right_edge_of(idx, fixture)
        if right == float('inf'):
            right = self._song_end_time()
        return max(0.0, right - self.midi_events[idx]['time'])

    def _visible_duration_for_group(self, group_id: str) -> float:
        members = self._group_members(group_id)
        if not members:
            return 0.0
        fixture = self._event_fixture(members[0])
        right = self._group_right_edge(group_id, fixture)
        if right == float('inf'):
            right = self._song_end_time()
        return max(0.0, right - members[0]['time'])

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

    def _generate_fade_notes(self, group_id: str
                             ) -> List[Tuple[float, int]]:
        """Compute (time, velocity) tuples for a group's fade-in + fade-out.

        Pure function over current state — caller is responsible for
        wiping prior is_fade entries and inserting these into midi_notes.
        Returns empty when the group has no brightness note (note 1) or
        the brightness velocity is 0.
        """
        members = self._group_members(group_id)
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
        fixture = self._event_fixture(brightness)
        right_edge = self._group_right_edge(group_id, fixture)
        if right_edge == float('inf'):
            right_edge = self._song_end_time()

        notes: List[Tuple[float, int]] = []
        if fade_in >= FADE_MIN_DURATION:
            notes.extend(self._linear_fade_ramp(
                start_time, fade_in, 0, target_vel, FADE_STEP_SECONDS))
        if fade_out >= FADE_MIN_DURATION:
            notes.extend(self._linear_fade_ramp(
                right_edge - fade_out, fade_out, target_vel, 0,
                FADE_STEP_SECONDS))
        return notes

    def _regenerate_group_fades(self, group_id: str) -> None:
        """Wipe and rewrite this group's fade notes in midi_notes.

        Idempotent — call after any state change that affects the group's
        position, extent, or brightness. Fade notes are appended after a
        wipe so stable-sort by time keeps them after originals at the same
        timestamp (the rebuild step relies on this for last-write-wins
        ordering of the same-time brightness conflict at scene start).
        """
        self.midi_notes = [
            n for n in self.midi_notes
            if not (n.get("is_fade") and n.get("parent_group_id") == group_id)
        ]
        for t, vel in self._generate_fade_notes(group_id):
            self.midi_notes.append({
                "id": str(uuid.uuid4()),
                "note": FADE_BRIGHTNESS_NOTE,
                "time": t,
                "duration": FADE_NOTE_DURATION,
                "velocity": vel,
                "is_fade": True,
                "parent_group_id": group_id,
            })
        self.midi_notes.sort(key=lambda x: x["time"])

    def _wipe_group_fades(self, group_id: str) -> None:
        """Remove all fade notes for a group (used on delete)."""
        self.midi_notes = [
            n for n in self.midi_notes
            if not (n.get("is_fade") and n.get("parent_group_id") == group_id)
        ]

    def _clamp_group_fades_to_visible_duration(self, group_id: str) -> None:
        """Shrink fade_in/fade_out values that no longer fit a resized clip.

        Called after a left/right resize. Preserves the relative balance
        between in and out when both have to shrink. Idempotent.
        """
        members = self._group_members(group_id)
        if not members:
            return
        visible = self._visible_duration_for_group(group_id)
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

    def set_event_fade_in(self, event_id: str, seconds: float) -> bool:
        idx = self._index_of(event_id)
        if idx < 0:
            return False
        evt = self.midi_events[idx]
        clamped = self._clamp_fade(seconds, evt.get('fade_out', 0.0),
                                   self._visible_duration_for_event(event_id))
        if abs(clamped - evt.get('fade_in', 0.0)) < 1e-6:
            return False
        evt['fade_in'] = clamped
        self.data_changed.emit()
        return True

    def set_event_fade_out(self, event_id: str, seconds: float) -> bool:
        idx = self._index_of(event_id)
        if idx < 0:
            return False
        evt = self.midi_events[idx]
        clamped = self._clamp_fade(seconds, evt.get('fade_in', 0.0),
                                   self._visible_duration_for_event(event_id))
        if abs(clamped - evt.get('fade_out', 0.0)) < 1e-6:
            return False
        evt['fade_out'] = clamped
        self.data_changed.emit()
        return True

    def set_scene_fade_in(self, group_id: str, seconds: float) -> bool:
        members = self._group_members(group_id)
        if not members:
            return False
        other = members[0].get('fade_out', 0.0)
        clamped = self._clamp_fade(seconds, other,
                                   self._visible_duration_for_group(group_id))
        if abs(clamped - members[0].get('fade_in', 0.0)) < 1e-6:
            return False
        for m in members:
            m['fade_in'] = clamped
        self._regenerate_group_fades(group_id)
        self._commit()
        return True

    def set_scene_fade_out(self, group_id: str, seconds: float) -> bool:
        members = self._group_members(group_id)
        if not members:
            return False
        other = members[0].get('fade_in', 0.0)
        clamped = self._clamp_fade(seconds, other,
                                   self._visible_duration_for_group(group_id))
        if abs(clamped - members[0].get('fade_out', 0.0)) < 1e-6:
            return False
        for m in members:
            m['fade_out'] = clamped
        self._regenerate_group_fades(group_id)
        self._commit()
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
                # Carry the is_fade flag through so the sort can place fade
                # notes AFTER originals at the same timestamp — last-write-
                # wins is the mechanism that makes brightness=0 win at scene
                # start during a fade-in (the original group's brightness
                # note also lives at that exact time).
                is_fade = bool(note_data.get('is_fade', False))
                # Note on
                all_events.append({
                    'time': note_data['time'],
                    'type': 'note_on',
                    'note': note_data['note'],
                    'velocity': note_data['velocity'],
                    'channel': 0,
                    'is_fade': is_fade,
                })
                # Note off
                all_events.append({
                    'time': note_data['time'] + note_data['duration'],
                    'type': 'note_off',
                    'note': note_data['note'],
                    'velocity': 0,
                    'channel': 0,
                    'is_fade': is_fade,
                })

            # Sort by time, then by is_fade so fade events follow originals
            # at the same timestamp.
            all_events.sort(key=lambda x: (x['time'], x['is_fade']))
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