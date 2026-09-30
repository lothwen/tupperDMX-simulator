# ABOUTME: Import songs from another show folder into the open one: read its
# ABOUTME: manifest without opening it, copy each song's .mid (and, if asked,
# ABOUTME: its audio) in, and name the MIDI channels whose patch disagrees.
import copy
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Iterable, List, Tuple

from core.config import SongConfig
from core.show import store
from core.show.manifest import show_from_manifest
from core.show.migrate import shows_present_in
from core.show.model import MANIFEST_NAME, Show, ShowError


def other_shows(shows_dir, exclude_root=None) -> List[Path]:
    """Show folders in shows_dir, most recently modified first, minus the
    open one."""
    exclude = Path(exclude_root).resolve() if exclude_root else None
    return [p for p in shows_present_in(shows_dir) if p.resolve() != exclude]


def peek_show(root) -> Tuple[Show, List[dict]]:
    """Read another show WITHOUT opening it: (its Show, its raw patch entries).

    Not store.load_show, which switches the global fixture-defs layer to that
    show and creates folders inside it. The patch is kept out of the Show —
    its show-local types would not resolve against the open show's library
    and would only log as dropped — and handed back raw for
    patch_differences()."""
    root = Path(root)
    manifest = root / MANIFEST_NAME
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise ShowError(f"Cannot read '{manifest}': {e}") from e
    if not isinstance(data, dict):
        raise ShowError(f"Manifest in '{root}' is not a JSON object")
    raw = data.get("fixtures")
    raw = [f for f in raw if isinstance(f, dict)] if isinstance(raw, list) else []
    return show_from_manifest(root, {**data, "fixtures": []}), raw


def patch_differences(source_fixtures: Iterable[dict], target_fixtures) -> List[int]:
    """MIDI channels the source patch drives that the target patch does not
    drive with the same fixture (type + mode). An imported song's cues address
    fixtures by channel, so on these channels they would light something else
    here — or nothing."""
    target = {f.channel: (f.type, f.mode) for f in target_fixtures}
    out = set()
    for entry in source_fixtures:
        channel = entry.get("channel")
        if not isinstance(channel, int) or channel < 0:
            continue
        if target.get(channel) != (entry.get("type"), entry.get("mode", "") or ""):
            out.add(channel)
    return sorted(out)


def import_songs(target: Show, songs: Iterable[SongConfig], *,
                 copy_audio: bool) -> Tuple[List[SongConfig], List[str]]:
    """Copies of `songs` (read with peek_show) ready to append to target's
    setlist, plus human-readable warnings.

    Each copy gets a fresh id: voice-TTS renders are matched by song id, and a
    duplicated show shares its source's ids. Its .mid is copied into target's
    midis/; its backing and guide are copied into target's audio/ when
    `copy_audio` (a file several songs share is copied once), else they keep
    pointing at the original files. A missing source file is a warning:
    missing audio keeps its path for relinking, a missing .mid is dropped."""
    new_songs, warnings = [], []
    copied = {}
    for src in songs:
        song = copy.deepcopy(src)
        song.id = str(uuid.uuid4())
        label = song.title or "Untitled"
        if src.midi:
            if os.path.isfile(src.midi):
                song.midi = store.allocate_midi_path(
                    target, song.title or Path(src.midi).stem)
                shutil.copy2(src.midi, song.midi)
            else:
                warnings.append(f"{label}: MIDI file not found, "
                                f"imported without it: {src.midi}")
                song.midi = None
        for attr, what in (("audio", "backing"), ("guide", "guide")):
            path = getattr(src, attr)
            if not path:
                continue
            if not os.path.isfile(path):
                warnings.append(f"{label}: {what} track not found "
                                f"(kept for relinking): {path}")
                continue
            if copy_audio:
                if path not in copied:
                    copied[path] = store.allocate_audio_path(
                        target, os.path.basename(path))
                    shutil.copy2(path, copied[path])
                setattr(song, attr, copied[path])
        new_songs.append(song)
    return new_songs, warnings
