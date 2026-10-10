# ABOUTME: Startup show resolution (last -> recents -> create): a first launch
# ABOUTME: copies the shipped demo show, while legacy userprefs that still
# ABOUTME: carry a playlist get the one-time migration into a default show.
import os
import shutil
import uuid
from pathlib import Path
from typing import Optional

from core.paths import asset_path, default_shows_dir
from core.show import store
from core.show.model import SHOW_DIR_SUFFIX, Show, ShowError
from tupperlogger import get_logger

logger = get_logger("show.migrate")

DEFAULT_SHOW_NAME = "Untitled"
DEMO_SHOW_NAME = "demo"
MAX_RECENT_SHOWS = 8


def remember_show(config, root) -> None:
    """Record root as this machine's current + most recent show (userprefs).

    Stored absolute (but NOT symlink-resolved): the pointer outlives the
    process that wrote it, and the next run's working directory is whatever
    started it — a relative path here, which `--show Documents/x.show` makes
    easy to produce, would resolve somewhere else or nowhere at all.
    """
    path = os.path.abspath(str(root))
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
    guide's, then the MIDI's — same rule as the web remote's setlist."""
    path = song.audio or song.guide or song.midi
    if not path:
        return ""
    return os.path.splitext(os.path.basename(path))[0]


def migrate_config_to_show(config, shows_dir) -> Show:
    """Turn the current Config contents (the legacy userprefs playlist + patch
    — or just the shipped defaults on a clean install) into a new default show.

    .mid files are COPIED into midis/ (the user's originals stay untouched);
    audio stays referenced in place; the patch moves into the manifest. The
    caller persists the userprefs pointers.

    The legacy `webremote.cues` set is deliberately NOT carried over: the
    remote's buttons are now the show's starred library clips, and a cue that
    fired several fixtures at once has no single-clip equivalent. A new show
    starts with no favourites."""
    shows_dir = Path(shows_dir)
    shows_dir.mkdir(parents=True, exist_ok=True)
    name = _unique_show_name(shows_dir, DEFAULT_SHOW_NAME)
    show = store.create_show(shows_dir, name, fixtures=config.fixtures)
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


def demo_template_dir() -> Path:
    """The show a first launch copies, shipped read-only under assets/."""
    return Path(asset_path("demo.show"))


def create_demo_show(shows_dir) -> Show:
    """Copy the shipped demo show into shows_dir and load the copy.

    The template is never loaded in place: load_show creates missing
    subfolders in whatever it opens, and the bundle dir may be read-only
    (PyInstaller, Program Files, /opt/tupperdmx). Files are copied by content
    only, so a read-only install cannot hand the copy files the autosave is
    then unable to write. A copy that fails halfway is removed, so a show
    folder without its .mid is never left behind to be adopted later."""
    template = demo_template_dir()
    if not store.is_show_dir(template):
        raise ShowError(f"No demo show template at '{template}'")
    shows_dir = Path(shows_dir)
    name = _unique_show_name(shows_dir, DEMO_SHOW_NAME)
    root = shows_dir / (name + SHOW_DIR_SUFFIX)
    root.mkdir(parents=True)
    try:
        for src in template.rglob("*"):
            dest = root / src.relative_to(template)
            if src.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
        show = store.load_show(root)
        show.name = name
        store.save_manifest(show)
    except (OSError, ShowError):
        shutil.rmtree(root, ignore_errors=True)
        raise
    return show


def shows_present_in(shows_dir) -> list:
    """Show folders already sitting in shows_dir, most recently modified first."""
    try:
        found = [p for p in Path(shows_dir).iterdir() if store.is_show_dir(p)]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def open_or_create_startup_show(config, shows_dir=None, explicit_path=None) -> Show:
    """Resolve the show to open at startup, in order: an explicitly requested
    path (--show), last_show_path, the recent list, a show folder already
    present in the shows dir, and only then a new show: the legacy userprefs
    playlist migrated into one when there is a playlist, the demo otherwise
    (an empty show if the demo cannot be copied). Persists the updated
    pointers via config.save(), which is also what makes the creation
    one-time — the second launch takes the last_show_path branch."""
    candidates = []
    if explicit_path:
        candidates.append(str(explicit_path))
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

    shows_dir = Path(shows_dir or default_shows_dir())
    # Nothing the prefs pointed at was usable. Before creating a show, adopt
    # one that is already on disk: headless has no show picker, so an operator
    # who copies a .show folder onto the box over the share has no way to
    # select it, and creating the demo instead buries it for good.
    for root in shows_present_in(shows_dir):
        try:
            show = store.load_show(root)
        except ShowError as e:
            logger.warning(f"Skipping unloadable show '{root}': {e}")
            continue
        logger.info(f"Adopted the show already present at '{root}'")
        remember_show(config, show.root)
        config.save()
        return show

    if config.playlist.songs:
        show = migrate_config_to_show(config, shows_dir)
        logger.info(f"Created show '{show.root}' "
                    f"({len(show.playlist.songs)} songs adopted from userprefs)")
    else:
        try:
            show = create_demo_show(shows_dir)
            logger.info(f"Created show '{show.root}' from the demo")
        except (OSError, ShowError) as e:
            logger.warning(f"Cannot copy the demo show ({e}); "
                           f"creating an empty one")
            show = migrate_config_to_show(config, shows_dir)
            logger.info(f"Created show '{show.root}'")
    remember_show(config, show.root)
    # Also slims userprefs: save() no longer emits playlist/fixtures.
    config.save()
    return show
