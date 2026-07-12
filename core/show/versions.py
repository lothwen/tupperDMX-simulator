# ABOUTME: Show version snapshots (Qt-free): copies of show.json + midis/ in
# ABOUTME: versions/ — timestamped autosaves with retention, pinned by name.
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from core.show.model import MANIFEST_NAME, MIDIS_DIR, Show, ShowError
from core.show.store import atomic_write_json
from tupperlogger import get_logger

logger = get_logger("show.versions")

SNAPSHOT_MIN_INTERVAL_S = 300   # at most one automatic snapshot per 5 min
RETAIN_UNPINNED = 25            # unpinned snapshots kept by prune()
_META_NAME = "meta.json"


@dataclass(frozen=True)
class VersionInfo:
    version_id: str      # directory name inside versions/
    timestamp: str       # ISO-ish creation time
    pinned: bool
    name: str            # user label ("" for automatic snapshots)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", (name or "").lower()).strip("-")


def take_snapshot(show: Show, *, pinned: bool = False,
                  name: Optional[str] = None) -> VersionInfo:
    """Copy the show's current show.json + midis/ (audio is referenced, never
    copied — a snapshot is KBs) into versions/<timestamp>[-slug]/."""
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    base = stamp + (f"-{_slug(name)}" if name and _slug(name) else "")
    show.versions_dir.mkdir(parents=True, exist_ok=True)
    version_id, n = base, 1
    while (show.versions_dir / version_id).exists():
        n += 1
        version_id = f"{base}-{n}"
    dest = show.versions_dir / version_id
    dest.mkdir()

    if show.manifest_path.is_file():
        shutil.copy2(show.manifest_path, dest / MANIFEST_NAME)
    if show.midis_dir.is_dir():
        shutil.copytree(show.midis_dir, dest / MIDIS_DIR)
    info = VersionInfo(version_id=version_id, timestamp=stamp,
                       pinned=bool(pinned), name=name or "")
    atomic_write_json(dest / _META_NAME, {
        "timestamp": info.timestamp, "pinned": info.pinned, "name": info.name})
    logger.info(f"Snapshot taken: {dest}")
    return info


def list_versions(show: Show) -> List[VersionInfo]:
    """All snapshots, newest first. Tolerates missing/corrupt meta.json
    (falls back to the directory name, unpinned)."""
    if not show.versions_dir.is_dir():
        return []
    versions: List[VersionInfo] = []
    for entry in sorted(show.versions_dir.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        meta = {}
        try:
            meta = json.loads((entry / _META_NAME).read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        versions.append(VersionInfo(
            version_id=entry.name,
            timestamp=str(meta.get("timestamp") or entry.name),
            pinned=bool(meta.get("pinned", False)),
            name=str(meta.get("name", "")),
        ))
    return versions


def prune(show: Show, retain: int = RETAIN_UNPINNED) -> int:
    """Delete the oldest UNPINNED snapshots beyond `retain`. Pinned versions
    are never touched. Returns the number removed."""
    unpinned = [v for v in list_versions(show) if not v.pinned]
    doomed = unpinned[retain:]
    for version in doomed:
        shutil.rmtree(show.versions_dir / version.version_id, ignore_errors=True)
        logger.info(f"Pruned snapshot: {version.version_id}")
    return len(doomed)


def restore(show: Show, version_id: str) -> None:
    """Bring show.json + midis/ back to `version_id`'s state. The current
    state is snapshotted first, so a restore is itself undoable. The caller
    must reload the show from disk afterwards."""
    src = show.versions_dir / version_id
    if not src.is_dir() or not (src / MANIFEST_NAME).is_file():
        raise ShowError(f"No restorable version '{version_id}' in {show.versions_dir}")

    take_snapshot(show, name="pre-restore")

    # Manifest: atomic replace via a sibling temp copy.
    tmp = show.manifest_path.with_suffix(".json.tmp")
    shutil.copy2(src / MANIFEST_NAME, tmp)
    tmp.replace(show.manifest_path)

    # Owned midis: the snapshot's set replaces the current one wholesale.
    if show.midis_dir.is_dir():
        shutil.rmtree(show.midis_dir)
    if (src / MIDIS_DIR).is_dir():
        shutil.copytree(src / MIDIS_DIR, show.midis_dir)
    else:
        show.midis_dir.mkdir(exist_ok=True)
    logger.info(f"Restored show '{show.root}' to version {version_id}")
