"""MidiCodec — serialization between mido.MidiFile and the MidiDocument lists.

Pure transforms over a MidiDocument: read a file, parse a MidiFile into the
``midi_events`` / ``midi_notes`` lists, rebuild a MidiFile from those lists,
apply a tempo, and export to disk. No scene-group / fade reconstruction lives
here — that orchestration stays on MidiManager, which calls parse_into() first.
"""

from typing import List, Optional
import json
import os
import tempfile
import shutil
import uuid

import mido

from tupperlogger import get_logger
from .constants import VALUE_BEARING_NOTES

# Authoring metadata that plain MIDI can't carry (stable scene group ids,
# custom scene names, per-clip fades) rides inside the same .mid as text meta
# events with this prefix + a compact JSON payload. Standard MIDI consumers
# ignore text metas, so the file stays a valid interchange .mid; the version
# is implicit in the prefix. Payload times ("at") are absolute SECONDS —
# tempo-independent, so apply_tempo's tick rescaling never desyncs them.
SIDECAR_PREFIX = "tdmx1:"
# Parsed event times are tick-quantized (and re-quantized on tempo changes),
# so payload-to-event matching tolerates a few milliseconds of drift.
SIDECAR_MATCH_TOLERANCE = 0.02


class MidiCodec:
    """Convert between mido.MidiFile and the document's event/note lists."""

    def __init__(self):
        self.logger = get_logger("midi.codec")

    def read(self, file_path: str) -> mido.MidiFile:
        """Load a MIDI file from disk (raises on failure — caller handles it)."""
        return mido.MidiFile(file_path)

    def parse_into(self, doc) -> None:
        """Parse doc.midi_data into doc.midi_events and doc.midi_notes.

        Pure mido -> lists conversion. Scene-group clustering and fade-run
        detection happen afterwards in MidiManager, not here.
        """
        if not doc.midi_data:
            return

        doc.midi_events.clear()
        doc.midi_notes.clear()
        doc.sidecar_meta = []

        # Parse for MIDI events (using note map)
        current_time = 0
        for msg in doc.midi_data:
            current_time += msg.time
            if msg.is_meta and msg.type == 'text' \
                    and msg.text.startswith(SIDECAR_PREFIX):
                try:
                    payload = json.loads(msg.text[len(SIDECAR_PREFIX):])
                except ValueError as e:
                    self.logger.warning(f"Skipping malformed sidecar meta: {e}")
                    continue
                if isinstance(payload, dict):
                    doc.sidecar_meta.append(payload)
                continue
            if msg.type != 'note_on':
                continue
            # Value-bearing notes (channel sets / scene loaders) carry their
            # value in velocity, so velocity 0 is real and admitted. Other
            # notes follow the standard "note_on velocity 0 == note_off"
            # convention and are skipped here.
            if msg.velocity == 0 and msg.note not in VALUE_BEARING_NOTES:
                continue
            # Resolve the label/colour against the note's fixture (its MIDI
            # channel selects the fixture); per-fixture verbs like a fixture's
            # 8th channel aren't in the fixture-agnostic map but describe_note
            # resolves them. Notes with no defined meaning are skipped.
            note_info = doc._note_info(msg.note, msg.channel, msg.velocity)
            if note_info:
                doc.midi_events.append({
                    "id": str(uuid.uuid4()),
                    "note": msg.note,
                    "time": current_time,
                    "velocity": msg.velocity,
                    # MIDI channel = fixture instance index in the patch (the
                    # generalized note map routes by channel). Carried so the
                    # document scopes per fixture and rebuild round-trips it.
                    "channel": msg.channel,
                    "info": note_info,
                    "group_id": None,
                    "scene_name": None,
                    # Fade durations in seconds. UI-only state for now —
                    # not persisted to the MIDI file. Reset on every load.
                    "fade_in": 0.0,
                    "fade_out": 0.0,
                })

        # Parse note_on/note_off pairs into duration-bearing entries so the
        # MIDI file can be reconstructed on save (see rebuild).
        current_time = 0
        active_notes = {}
        for msg in doc.midi_data:
            current_time += msg.time
            if msg.type == 'note_on' and msg.velocity > 0:
                active_notes[(msg.channel, msg.note)] = {
                    "time": current_time, "velocity": msg.velocity}
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                key = (msg.channel, msg.note)
                if key in active_notes:
                    note_id = str(uuid.uuid4())
                    start_time = active_notes[key]["time"]
                    doc.midi_notes.append({
                        "id": note_id,
                        "note": msg.note,
                        "time": start_time,
                        "duration": current_time - start_time,
                        "velocity": active_notes[key]["velocity"],
                        "channel": msg.channel,
                    })
                    del active_notes[key]

    def rebuild(self, doc) -> None:
        """Rebuild doc.midi_data from doc.midi_events and doc.midi_notes."""
        if not doc.midi_data:
            return

        try:
            # Create new MIDI file with current structure
            new_mid = mido.MidiFile(type=doc.midi_data.type, ticks_per_beat=doc.midi_data.ticks_per_beat)
            track = mido.MidiTrack()
            new_mid.tracks.append(track)

            # Add tempo and meta messages from original. Stale sidecar metas
            # are dropped here and regenerated below from the CURRENT edit
            # state — regeneration, never accumulation.
            tempo_microseconds = 500000  # Default tempo (120 BPM)
            for msg in doc.midi_data.merged_track:
                if msg.is_meta:
                    if msg.type == 'text' and msg.text.startswith(SIDECAR_PREFIX):
                        continue
                    if msg.type == 'set_tempo':
                        tempo_microseconds = msg.tempo
                    track.append(msg.copy())
            for meta in self._generate_sidecar(doc):
                track.append(meta)

            # Build all MIDI events from notes data
            all_events = []
            for note_data in doc.midi_notes:
                # Carry the is_fade flag through so the sort can place fade
                # notes AFTER originals at the same timestamp — last-write-
                # wins is the mechanism that makes brightness=0 win at scene
                # start during a fade-in (the original group's brightness
                # note also lives at that exact time).
                is_fade = bool(note_data.get('is_fade', False))
                # MIDI channel = fixture instance index (the note map routes by
                # channel). Default 0 for notes without one (synthetic data /
                # fade ramps, which target the lead ledbar at channel 0).
                channel = note_data.get('channel') or 0
                # Note on
                all_events.append({
                    'time': note_data['time'],
                    'type': 'note_on',
                    'note': note_data['note'],
                    'velocity': note_data['velocity'],
                    'channel': channel,
                    'is_fade': is_fade,
                })
                # Note off
                all_events.append({
                    'time': note_data['time'] + note_data['duration'],
                    'type': 'note_off',
                    'note': note_data['note'],
                    'velocity': 0,
                    'channel': channel,
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

            doc.midi_data = new_mid

        except Exception as e:
            self.logger.error(f"Error rebuilding MIDI data: {e}")

    def _generate_sidecar(self, doc) -> List[mido.MetaMessage]:
        """Fresh tdmx1 text metas from the current edit state, all at delta 0
        (payload times are absolute seconds, so placement doesn't matter).

        One meta per scene group (stable gid + custom name + fade values) and
        one per ungrouped clip that carries a fade (clips have no ramp-note
        representation — without this the fade dies on reload)."""
        metas: List[mido.MetaMessage] = []

        def _text(payload: dict) -> mido.MetaMessage:
            return mido.MetaMessage(
                'text',
                text=SIDECAR_PREFIX + json.dumps(
                    payload, separators=(",", ":"), sort_keys=True),
                time=0)

        seen_groups = {}
        for e in doc.midi_events:
            gid = e.get('group_id')
            if gid and (gid not in seen_groups
                        or e['time'] < seen_groups[gid]['time']):
                seen_groups[gid] = e
        for gid, rep in sorted(seen_groups.items(), key=lambda kv: kv[1]['time']):
            metas.append(_text({
                "t": "scene",
                "at": round(rep['time'], 6),
                "ch": rep.get('channel') or 0,
                "gid": gid,
                "name": rep.get('scene_name') or "",
                "fi": round(rep.get('fade_in', 0.0), 6),
                "fo": round(rep.get('fade_out', 0.0), 6),
            }))

        for e in doc.midi_events:
            if e.get('group_id'):
                continue
            fi = e.get('fade_in', 0.0)
            fo = e.get('fade_out', 0.0)
            if fi <= 0.0 and fo <= 0.0:
                continue
            metas.append(_text({
                "t": "clip",
                "at": round(e['time'], 6),
                "ch": e.get('channel') or 0,
                "note": int(e['note']),
                "fi": round(fi, 6),
                "fo": round(fo, 6),
            }))
        return metas

    def _strip_sidecar_copy(self, midi_data: mido.MidiFile) -> mido.MidiFile:
        """A copy of midi_data without tdmx1 metas (the clean interop export).
        Dropped metas donate their delta time to the next message so note
        timing is untouched."""
        out = mido.MidiFile(type=midi_data.type,
                            ticks_per_beat=midi_data.ticks_per_beat)
        for track in midi_data.tracks:
            clean = mido.MidiTrack()
            carry = 0
            for msg in track:
                if (msg.is_meta and msg.type == 'text'
                        and msg.text.startswith(SIDECAR_PREFIX)):
                    carry += msg.time
                    continue
                clean.append(msg.copy(time=msg.time + carry))
                carry = 0
            out.tracks.append(clean)
        return out

    def apply_tempo(self, doc, bpm: int) -> None:
        """Write bpm as a set_tempo meta message in doc.midi_data.

        Preserves the wall-clock time of every event: scales all delta ticks
        by old_tempo / new_tempo (since absolute_seconds = absolute_ticks *
        tempo / (ticks_per_beat * 1_000_000), keeping that product constant
        requires the inverse scaling on the ticks). Then replaces every
        existing set_tempo with the new value, or inserts one at time=0 in
        track 0 if none was present.

        Does NOT call rebuild(): that function accumulates meta message deltas
        from merged_track before emitting notes, which would offset note
        timings. In-place rescaling avoids that pitfall and is also safe when
        doc.midi_data is already in sync with doc.midi_notes.
        """
        if not doc.midi_data or not doc.midi_data.tracks:
            return

        new_tempo = mido.bpm2tempo(bpm)
        old_tempo = 500000
        for track in doc.midi_data.tracks:
            for msg in track:
                if msg.is_meta and msg.type == 'set_tempo':
                    old_tempo = msg.tempo

        if old_tempo != new_tempo:
            scale = old_tempo / new_tempo
            for track in doc.midi_data.tracks:
                for i, msg in enumerate(track):
                    if msg.time:
                        track[i] = msg.copy(time=int(round(msg.time * scale)))

        found = False
        for track in doc.midi_data.tracks:
            for i, msg in enumerate(track):
                if msg.is_meta and msg.type == 'set_tempo':
                    track[i] = mido.MetaMessage('set_tempo', tempo=new_tempo, time=msg.time)
                    found = True
        if not found:
            doc.midi_data.tracks[0].insert(
                0, mido.MetaMessage('set_tempo', tempo=new_tempo, time=0)
            )

    def export(self, doc, file_path: str, bpm: Optional[int] = None,
               interop: bool = False) -> bool:
        """Export doc.midi_data to file using a safe temporary-file approach.

        If bpm is provided, the session tempo is written into the file as a
        set_tempo meta message before saving. With interop=True a clean copy
        without tdmx1 sidecar metas is written (standard-MIDI interchange);
        doc.midi_data itself is not modified by the stripping.
        """
        if not doc.midi_data:
            self.logger.error("No MIDI data to export")
            return False

        if bpm is not None:
            self.apply_tempo(doc, bpm)

        out_data = self._strip_sidecar_copy(doc.midi_data) if interop \
            else doc.midi_data

        # Create temporary file in the same directory as target
        target_dir = os.path.dirname(os.path.abspath(file_path))
        temp_path = None

        try:
            # Create temporary file with .tmp extension in target directory
            temp_fd, temp_path = tempfile.mkstemp(suffix='.tmp', dir=target_dir, prefix='midi_export_')
            os.close(temp_fd)  # Close file descriptor, we'll use the path

            # Export MIDI data to temporary file first
            out_data.save(temp_path)
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
