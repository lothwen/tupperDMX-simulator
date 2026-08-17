# ABOUTME: Disk store for show projects: atomic JSON writes, create/load/save/
# ABOUTME: duplicate of Name.show/ folders, and unique midis/ path allocation.
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Iterable

from core import fixture_library
from core.show.manifest import show_from_manifest, show_to_manifest
from core.show.model import (
    AUDIO_DIR,
    FIXTURES_DIR,
    MANIFEST_NAME,
    MIDIS_DIR,
    SHOW_DIR_SUFFIX,
    VERSIONS_DIR,
    Show,
    ShowError,
)

_SUBDIRS = (MIDIS_DIR, AUDIO_DIR, FIXTURES_DIR, VERSIONS_DIR)


def atomic_write_json(path, data) -> None:
    """Write JSON via a temp file + os.replace so readers never see partials
    and a crash mid-write cannot corrupt the previous version."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _split_name(name: str) -> tuple:
    """(folder name with .show suffix, display name without it)."""
    name = (name or "").strip()
    if not name or name == SHOW_DIR_SUFFIX:
        raise ShowError("Show name cannot be empty")
    dirname = name if name.endswith(SHOW_DIR_SUFFIX) else name + SHOW_DIR_SUFFIX
    return dirname, dirname[:-len(SHOW_DIR_SUFFIX)]


def is_show_dir(path) -> bool:
    """True when path is a folder containing a show.json manifest."""
    p = Path(path)
    return p.is_dir() and (p / MANIFEST_NAME).is_file()


def create_show(parent, name: str, *, fixtures: Iterable = (), cues: Iterable = ()) -> Show:
    """Create Name.show/ (with its subfolders) under parent and write its
    manifest. Refuses to overwrite an existing folder. Activates the new
    show's (empty) show-local fixture-defs layer."""
    dirname, display = _split_name(name)
    root = Path(parent) / dirname
    if root.exists():
        raise ShowError(f"'{root}' already exists")
    for sub in _SUBDIRS:
        (root / sub).mkdir(parents=True)
    fixture_library.set_show_fixtures_dir(root / FIXTURES_DIR)
    show = Show(root=root, name=display, fixtures=list(fixtures), cues=list(cues))
    save_manifest(show)
    return show


def load_show(root) -> Show:
    """Load a show folder. Raises ShowError when the manifest is missing,
    unreadable, or rejected by show_from_manifest.

    Side effect: activates the show-local fixture-defs layer BEFORE parsing
    the manifest, so a patch referencing a show-local type resolves. Only one
    show is open at a time, so the layer simply tracks the last load."""
    root = Path(root)
    manifest = root / MANIFEST_NAME
    if not manifest.is_file():
        raise ShowError(f"'{root}' is not a show folder (no {MANIFEST_NAME})")
    try:
        # utf-8-sig for the same reason userprefs.json uses it: a manifest
        # hand-edited over the rack box's SMB share comes back with a BOM,
        # and a show that will not parse is a show that will not open.
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise ShowError(f"Cannot read '{manifest}': {e}") from e
    fixture_library.set_show_fixtures_dir(root / FIXTURES_DIR)
    show = show_from_manifest(root, data)
    # Self-heal subfolders pruned by sync tools or manual cleanup.
    for sub in _SUBDIRS:
        (root / sub).mkdir(exist_ok=True)
    return show


def save_manifest(show: Show) -> None:
    """Atomically (re)write the show's show.json."""
    atomic_write_json(show.manifest_path, show_to_manifest(show))


def duplicate_show(show: Show, dest_parent, new_name: str) -> Path:
    """Copy the show folder — minus its versions/ history — under a new name
    and retitle the copy's manifest. Returns the new root."""
    dirname, display = _split_name(new_name)
    dest = Path(dest_parent) / dirname
    if dest.exists():
        raise ShowError(f"'{dest}' already exists")
    shutil.copytree(show.root, dest, ignore=shutil.ignore_patterns(VERSIONS_DIR))
    (dest / VERSIONS_DIR).mkdir(exist_ok=True)
    copy = load_show(dest)
    copy.name = display
    save_manifest(copy)
    return dest


def allocate_midi_path(show: Show, title: str) -> str:
    """Absolute path for a new song's .mid inside midis/, made unique with a
    numeric suffix against both existing files and paths already claimed by
    the playlist (allocated but not yet written)."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", (title or "").lower()).strip("-") or "song"
    show.midis_dir.mkdir(parents=True, exist_ok=True)
    taken = {os.path.normpath(s.midi) for s in show.playlist.songs if s.midi}
    n = 1
    while True:
        base = slug if n == 1 else f"{slug}-{n}"
        candidate = show.midis_dir / f"{base}.mid"
        if not candidate.exists() and os.path.normpath(str(candidate)) not in taken:
            return str(candidate)
        n += 1
