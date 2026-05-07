# ABOUTME: Runtime accessor for build metadata (commit, tag, build date).
# ABOUTME: Reads _buildinfo.py when frozen, falls back to live `git` for dev runs.

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

_DEV_PLACEHOLDER = "dev"


def _try_generated() -> Optional[Tuple[str, str, str]]:
    """Return values baked in at PyInstaller build time, if available."""
    try:
        from _buildinfo import BUILD_DATE, GIT_COMMIT, GIT_TAG  # type: ignore
    except Exception:
        return None
    return BUILD_DATE, GIT_COMMIT, GIT_TAG


def _git(*args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _from_live_git() -> Tuple[str, str, str]:
    """Best-effort metadata for development runs from a working tree."""
    commit = _git("rev-parse", "--short", "HEAD") or _DEV_PLACEHOLDER
    tag = _git("describe", "--tags", "--always", "--dirty") or _DEV_PLACEHOLDER
    iso_date = _git("show", "-s", "--format=%cI", "HEAD")
    build_date = iso_date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return build_date, commit, tag


_resolved = _try_generated() or _from_live_git()

BUILD_DATE: str = _resolved[0]
GIT_COMMIT: str = _resolved[1]
GIT_TAG: str = _resolved[2]
