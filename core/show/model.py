# ABOUTME: Qt-free show-project model: the Show dataclass (in-memory image of a
# ABOUTME: Name.show/ folder), ShowError, folder-layout constants, song_status().
from dataclasses import dataclass, field
from pathlib import Path
from typing import List
import os

from core.config import FixtureInstance, PlaylistConfig, SongConfig, WebCueConfig

FORMAT_VERSION = 1
MANIFEST_NAME = "show.json"
MIDIS_DIR = "midis"
AUDIO_DIR = "audio"
FIXTURES_DIR = "fixtures"
VERSIONS_DIR = "versions"
SHOW_DIR_SUFFIX = ".show"

# Derived per-song media states (song_status()).
STATUS_LINKED = "linked"                # audio + MIDI both present and resolvable
STATUS_MISSING_AUDIO = "missing_audio"  # audio path set but the file is gone
STATUS_MISSING_MIDI = "missing_midi"    # midi path set but the file is gone
STATUS_AUDIO_ONLY = "audio_only"        # audio present, no MIDI assigned yet
STATUS_NO_AUDIO = "no_audio"            # no audio assigned


class ShowError(Exception):
    """A show folder/manifest could not be created, read or written."""


@dataclass
class Show:
    """One show project: the in-memory image of a `Name.show/` folder.

    Song paths are always ABSOLUTE in memory; the manifest stores them
    relative to `root` when the file lives inside the show folder (owned
    .mid files, bundled audio) and absolute otherwise (referenced-in-place
    audio)."""
    root: Path
    name: str
    playlist: PlaylistConfig = field(default_factory=PlaylistConfig)
    fixtures: List[FixtureInstance] = field(default_factory=list)
    cues: List[WebCueConfig] = field(default_factory=list)
    format_version: int = FORMAT_VERSION

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def midis_dir(self) -> Path:
        return self.root / MIDIS_DIR

    @property
    def audio_dir(self) -> Path:
        return self.root / AUDIO_DIR

    @property
    def fixtures_dir(self) -> Path:
        return self.root / FIXTURES_DIR

    @property
    def versions_dir(self) -> Path:
        return self.root / VERSIONS_DIR


def song_status(song: SongConfig) -> str:
    """Derived media state for one song (drives the toolbar's "linked" chip).

    Missing files win over everything else so the UI can surface relinking;
    "linked" means both audio and MIDI are present and resolvable."""
    if song.audio and not os.path.exists(song.audio):
        return STATUS_MISSING_AUDIO
    if song.midi and not os.path.exists(song.midi):
        return STATUS_MISSING_MIDI
    if not song.audio:
        return STATUS_NO_AUDIO
    if song.midi:
        return STATUS_LINKED
    return STATUS_AUDIO_ONLY
