# ABOUTME: show.json (de)serialization: Show <-> manifest dict. Song paths are
# ABOUTME: relativized against the show root on save and resolved — but never
# ABOUTME: dropped when the file is missing — on load.
import os
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import Config, PlaylistConfig, SongConfig
from core.show.model import FORMAT_VERSION, SHOW_DIR_SUFFIX, Show, ShowError


def _portable(path: Optional[str], root: Path) -> Optional[str]:
    """Relative POSIX path when the file lives inside the show folder
    (owned .mid files, bundled audio), absolute otherwise (referenced-in-place
    audio)."""
    if not path:
        return None
    p = Path(path)
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(p)


def _resolved(value: Any, root: Path) -> Optional[str]:
    """Absolute path for in-memory use. Missing files KEEP their path — the
    "missing media" state needs it for relinking — so this is deliberately
    not Config._validate_file_path (which nulls nonexistent paths)."""
    if not isinstance(value, str) or not value:
        return None
    p = Path(value)
    if not p.is_absolute():
        p = root / p
    return os.path.normpath(str(p))


def show_to_manifest(show: Show) -> Dict[str, Any]:
    """Serialize a Show to the show.json dict."""
    songs = []
    for song in show.playlist.songs:
        songs.append({
            "id": song.id,
            "title": song.title,
            "audio": _portable(song.audio, show.root),
            "midi": _portable(song.midi, show.root),
            "offset": song.offset,
            "key": song.key,
            "precount": song.precount,
            "voice_cues": [asdict(c) for c in song.voice_cues],
            "cache": dict(song.cache),
        })
    return {
        "format_version": show.format_version,
        "name": show.name,
        "songs": songs,
        "current_song_index": show.playlist.current_song_index,
        "fixtures": [asdict(f) for f in show.fixtures],
        "webremote_cues": [asdict(c) for c in show.cues],
    }


def show_from_manifest(root: Path, data: Dict[str, Any]) -> Show:
    """Build a Show from a parsed show.json dict.

    Raises ShowError on a non-dict payload, a missing/invalid format_version,
    or a format_version newer than this app understands."""
    if not isinstance(data, dict):
        raise ShowError(f"Manifest in '{root}' is not a JSON object")
    version = data.get("format_version")
    if not isinstance(version, int) or version < 1:
        raise ShowError(f"Manifest in '{root}' has no valid format_version")
    if version > FORMAT_VERSION:
        raise ShowError(
            f"Show '{root}' uses format_version {version}; "
            f"this app supports up to {FORMAT_VERSION}")

    songs = []
    for entry in data.get("songs", []):
        if not isinstance(entry, dict):
            continue
        songs.append(SongConfig(
            midi=_resolved(entry.get("midi"), root),
            audio=_resolved(entry.get("audio"), root),
            offset=Config._validate_float(entry.get("offset", 0.0), -3600.0, 3600.0),
            id=str(entry.get("id") or uuid.uuid4()),
            title=str(entry.get("title", "")),
            key=str(entry.get("key", "")),
            precount=bool(entry.get("precount", True)),
            voice_cues=Config._parse_voice_cues(entry.get("voice_cues")),
            cache=entry["cache"] if isinstance(entry.get("cache"), dict) else {},
        ))

    name = data.get("name")
    if not isinstance(name, str) or not name:
        name = root.name
        if name.endswith(SHOW_DIR_SUFFIX):
            name = name[:-len(SHOW_DIR_SUFFIX)]

    return Show(
        root=root,
        name=name,
        playlist=PlaylistConfig(
            songs=songs,
            current_song_index=Config._validate_int(
                data.get("current_song_index", 0), 0, max(0, len(songs) - 1)),
        ),
        fixtures=Config._parse_fixtures(data.get("fixtures", [])),
        cues=Config._parse_web_cues(data.get("webremote_cues", [])),
        format_version=version,
    )
