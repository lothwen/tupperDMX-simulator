# ABOUTME: Pure (Qt-free) web-remote state building: song-section spans from
# ABOUTME: song-part markers, setlist metadata probes (mtime-cached) and the
# ABOUTME: full state-message assembly — single source of the WS schema.
import os
import socket
from typing import Any, Dict, List, Optional, Tuple

# (kind, path, mtime) -> value. Files on the setlist rarely change while the
# app runs; mtime keying keeps repeated snapshot builds free.
_probe_cache: Dict[tuple, Any] = {}


def _cached_probe(kind: str, path: Optional[str], compute):
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    key = (kind, path, mtime)
    if key not in _probe_cache:
        try:
            _probe_cache[key] = compute(path)
        except Exception:
            _probe_cache[key] = None
    return _probe_cache[key]


def probe_duration(audio_path: Optional[str]) -> Optional[float]:
    """Audio duration in seconds via a mutagen header read; None on failure."""
    def compute(path):
        import mutagen
        audio = mutagen.File(path)
        if audio is None or audio.info is None:
            return None
        return float(audio.info.length)
    return _cached_probe("duration", audio_path, compute)


def probe_bpm(midi_path: Optional[str]) -> Optional[int]:
    """BPM from the first set_tempo meta in the MIDI file; None if absent."""
    def compute(path):
        import mido
        for msg in mido.MidiFile(path):
            if msg.is_meta and msg.type == "set_tempo":
                return round(mido.tempo2bpm(msg.tempo))
        return None
    return _cached_probe("bpm", midi_path, compute)


def compute_sections(part_markers: List[Tuple[float, str, str]],
                     song_end: float) -> List[dict]:
    """(time, name, color) song-part markers → contiguous section spans
    [{'name','color','start','end'}], sorted by time; each section runs to the
    next marker, the last to song_end. Mirrors the timeline's section strip
    (timeline/tracks/waveform.py draw_song_sections)."""
    markers = sorted(part_markers, key=lambda m: m[0])
    sections = []
    for i, (time, name, color) in enumerate(markers):
        start = max(0.0, float(time))
        end = float(markers[i + 1][0]) if i + 1 < len(markers) else float(song_end)
        end = min(end, float(song_end))
        if end > start:
            sections.append({
                "name": name,
                "color": color,
                "start": start,
                "end": end,
            })
    return sections


def _song_title(song) -> str:
    """Display title for a SongConfig-like object: the show-manifest title
    when set, else the audio basename (sans extension), else the MIDI's."""
    title = getattr(song, "title", "")
    if title:
        return title
    path = getattr(song, "audio", None) or getattr(song, "midi", None)
    if not path:
        return "Untitled"
    return os.path.splitext(os.path.basename(path))[0]


def build_setlist(songs: list, current_index: int) -> dict:
    """config.playlist.songs → the 'setlist' block of the state message."""
    return {
        "current": current_index,
        "songs": [
            {
                "index": i,
                "title": _song_title(song),
                "duration": probe_duration(getattr(song, "audio", None)),
                "bpm": probe_bpm(getattr(song, "midi", None)),
                "key": getattr(song, "key", "") or None,
                "precount": bool(getattr(song, "precount", True)),
            }
            for i, song in enumerate(songs)
        ],
    }


def build_state(*, playing: bool, position: float, duration: Optional[float],
                bpm: int, blackout: bool, setlist: dict,
                sections: List[dict], cues: List[dict],
                show_name: Optional[str] = None,
                countin: Optional[dict] = None) -> dict:
    """Assemble the full 'state' WS message (the schema's single source).
    ``position`` may be negative while the count-in runs (clients render it
    as a countdown); ``countin`` = {'enabled': current song's precount,
    'bars': the global count-in length}."""
    return {
        "type": "state",
        "show_name": show_name,
        "playing": bool(playing),
        "position": float(position),
        "duration": float(duration) if duration else None,
        "bpm": int(bpm),
        "beats_per_bar": 4,
        "blackout": bool(blackout),
        "countin": countin if countin is not None
        else {"enabled": False, "bars": 2},
        "setlist": setlist,
        "sections": sections,
        "cues": cues,
    }


def lan_ip() -> str:
    """Best-effort LAN IP of this machine (for the Settings URL hint). The
    UDP connect never sends a packet — it only makes the OS pick a route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
