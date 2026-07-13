# ABOUTME: Startup show resolution (last -> recents -> create) plus the
# ABOUTME: one-time migration of the legacy userprefs playlist/patch into a
# ABOUTME: default show project.
import os
import shutil
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Optional

from core.paths import default_shows_dir
from core.show import store
from core.show.model import SHOW_DIR_SUFFIX, Show, ShowError
from tupperlogger import get_logger

logger = get_logger("show.migrate")

DEFAULT_SHOW_NAME = "Untitled"
MAX_RECENT_SHOWS = 8


def remember_show(config, root) -> None:
    """Record root as this machine's current + most recent show (userprefs)."""
    path = str(root)
    config.last_show_path = path
    config.recent_shows = ([path] +
                           [p for p in config.recent_shows if p != path])[:MAX_RECENT_SHOWS]


def _unique_show_name(parent: Path, base: str) -> str:
    name, n = base, 1
    while (parent / (name + SHOW_DIR_SUFFIX)).exists():
        n += 1
        name = f"{base} {n}"
    return name


def _song_title(song) -> str:
    """Display title: audio basename (sans extension), falling back to the
    MIDI basename — same rule as the web remote's setlist."""
    path = song.audio or song.midi
    if not path:
        return ""
    return os.path.splitext(os.path.basename(path))[0]


def migrate_config_to_show(config, shows_dir) -> Show:
    """Turn the current Config contents (the legacy userprefs playlist +
    patch + cue set — or just the shipped defaults on a clean install) into a
    new default show.

    .mid files are COPIED into midis/ (the user's originals stay untouched);
    audio stays referenced in place; the patch and the cues move into the
    manifest. The caller persists the userprefs pointers."""
    shows_dir = Path(shows_dir)
    shows_dir.mkdir(parents=True, exist_ok=True)
    name = _unique_show_name(shows_dir, DEFAULT_SHOW_NAME)
    show = store.create_show(shows_dir, name,
                             fixtures=config.fixtures,
                             cues=deepcopy(config.webremote.cues))
    show.playlist = config.playlist  # adopt the parsed songs as-is
    for i, song in enumerate(show.playlist.songs):
        if not song.id:
            song.id = str(uuid.uuid4())
        if not song.title:
            song.title = _song_title(song) or f"Song {i + 1}"
        if song.midi and os.path.exists(song.midi):
            dest = store.allocate_midi_path(show, song.title)
            shutil.copy2(song.midi, dest)
            song.midi = dest
    store.save_manifest(show)
    return show


def open_or_create_startup_show(config, shows_dir=None) -> Show:
    """Resolve the show to open at startup: last_show_path, then the recent
    list, else migrate/create a default show. Persists the updated pointers
    via config.save(), which is also what makes the migration one-time — the
    second launch takes the last_show_path branch."""
    candidates = []
    if config.last_show_path:
        candidates.append(config.last_show_path)
    candidates += [p for p in config.recent_shows if p not in candidates]

    for path in candidates:
        try:
            show = store.load_show(path)
        except ShowError as e:
            logger.warning(f"Skipping unloadable show '{path}': {e}")
            continue
        remember_show(config, show.root)
        config.save()
        return show

    show = migrate_config_to_show(config, shows_dir or default_shows_dir())
    logger.info(f"Created show '{show.root}' "
                f"({len(show.playlist.songs)} songs adopted from userprefs)")
    remember_show(config, show.root)
    # Also slims userprefs: save() no longer emits playlist/fixtures.
    config.save()
    return show
