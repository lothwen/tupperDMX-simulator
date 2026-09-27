# ABOUTME: Pure (Qt-free) web-remote state building: song-section spans from
# ABOUTME: song-part markers, setlist metadata probes (mtime-cached), the show
# ABOUTME: folder listing and the full state-message assembly — single source
# ABOUTME: of the WS schema.
import os
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.paths import default_shows_dir
from core.show.migrate import shows_present_in
from core.show.model import SHOW_DIR_SUFFIX

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


def song_duration(song) -> Optional[float]:
    """How long a song is: its LONGEST audio track, backing or guide.

    Which of the two the editor happens to be monitoring is not the song's
    length — a 20-second backing under a full-length guide would otherwise
    trim the remote's scrubber to 20 seconds. The desktop timeline spans the
    same maximum (see Timeline.apply_song_extent)."""
    lengths = [d for d in (probe_duration(getattr(song, "audio", None)),
                           probe_duration(getattr(song, "guide", None))) if d]
    return max(lengths) if lengths else None


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
    when set, else the basename (sans extension) of whichever file the song
    has — backing, guide or MIDI."""
    title = getattr(song, "title", "")
    if title:
        return title
    path = (getattr(song, "audio", None) or getattr(song, "guide", None)
            or getattr(song, "midi", None))
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
                "duration": song_duration(song),
                "bpm": probe_bpm(getattr(song, "midi", None)),
                "key": getattr(song, "key", "") or None,
                "precount": bool(getattr(song, "precount", True)),
            }
            for i, song in enumerate(songs)
        ],
    }


def _display_name(root: Path) -> str:
    """Folder name without the .show suffix (the desktop's own naming rule)."""
    name = root.name
    return name[:-len(SHOW_DIR_SUFFIX)] if name.endswith(SHOW_DIR_SUFFIX) else name


def shows_dir(current_root=None) -> Path:
    """Folder the remote's show menu lists: the one holding the open show, so
    the menu always contains (and can mark) the show on screen; the default
    shows dir only serves a box with no show open."""
    return Path(current_root).parent if current_root else default_shows_dir()


def _listing(current_root=None) -> List[Tuple[str, Path]]:
    """(display name, root) for every show folder in that directory, by name —
    sorted so a name shared by two folders always resolves to the same one."""
    entries = [(_display_name(root), root)
               for root in shows_present_in(shows_dir(current_root))]
    entries.sort(key=lambda e: (e[0].lower(), str(e[1])))
    return entries


def build_shows(current_root=None) -> List[dict]:
    """The 'shows' block: [{'name','current'}] for the folders the remote may
    switch to. Only names travel to clients — never paths, same rule as the
    setlist's audio — so a client names an entry and resolve_show() maps it
    back; nothing a remote sends is ever joined onto a path."""
    current = os.path.normpath(str(current_root)) if current_root else None
    return [{"name": name,
             "current": current is not None
             and os.path.normpath(str(root)) == current}
            for name, root in _listing(current_root)]


def resolve_show(name: str, current_root=None) -> Optional[Path]:
    """Inverse of build_shows: the root a client-supplied display name refers
    to, or None when it names nothing in the menu's folder."""
    for entry_name, root in _listing(current_root):
        if entry_name == name:
            return root
    return None


def build_state(*, playing: bool, position: float, duration: Optional[float],
                bpm: int, blackout: bool, setlist: dict,
                sections: List[dict], cues: List[dict],
                show_name: Optional[str] = None,
                shows: Optional[List[dict]] = None,
                countin: Optional[dict] = None,
                beats_per_bar: int = 4, beat_note: int = 4) -> dict:
    """Assemble the full 'state' WS message (the schema's single source).
    ``position`` may be negative while the count-in runs (clients render it
    as a countdown); ``countin`` = {'enabled': current song's precount,
    'bars': the global count-in length}; ``shows`` = build_shows().
    ``beats_per_bar`` and ``beat_note`` are the song's meter (6 and 8 for 6/8):
    a beat is one ``beat_note`` note, and ``bpm`` always counts QUARTERS, so a
    client times one beat as 60 / bpm * 4 / beat_note seconds."""
    return {
        "type": "state",
        "show_name": show_name,
        "shows": shows if shows is not None else [],
        "playing": bool(playing),
        "position": float(position),
        "duration": float(duration) if duration else None,
        "bpm": int(bpm),
        "beats_per_bar": int(beats_per_bar),
        "beat_note": int(beat_note),
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
