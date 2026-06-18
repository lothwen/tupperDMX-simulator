# ABOUTME: Configuration management for TupperDMX. Splits storage across two files:
# ABOUTME: shipped config.json (read-only fixtures + midi_note_map) and userprefs.json (writable user prefs).
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any
import json
import os
import sys
from pathlib import Path

import platformdirs


_APP_NAME = "tupperDMX"


def _bundle_dir() -> str:
    """Directory containing read-only resources shipped with the app.

    Mirrors main._base_dir(): when frozen via PyInstaller, resources are
    extracted to sys._MEIPASS; in development, they sit beside this file.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def shipped_config_path() -> str:
    """Path to the shipped, read-only config.json.

    Contains fixtures and midi_note_map. Bundled with the app via
    PyInstaller (see build.spec datas) and never written at runtime.
    """
    return os.path.join(_bundle_dir(), "config.json")


def userprefs_dir() -> Path:
    """Platform-conventional directory for the writable userprefs.json.

    Windows: %APPDATA%\\tupperDMX  (Roaming)
    macOS:   ~/Library/Application Support/tupperDMX
    Linux:   ~/.config/tupperDMX
    """
    if sys.platform == "win32":
        return Path(platformdirs.user_data_dir(_APP_NAME, appauthor=False, roaming=True))
    if sys.platform == "darwin":
        return Path(platformdirs.user_data_dir(_APP_NAME, appauthor=False))
    return Path(platformdirs.user_config_dir(_APP_NAME, appauthor=False))


def userprefs_path() -> Path:
    """Full path to the user-writable userprefs.json file."""
    return userprefs_dir() / "userprefs.json"


@dataclass
class WindowConfig:
    width: int = 1280
    height: int = 720
    dmx_panel_collapsed: bool = False
    waveform_track_height: int = 100
    midi_events_track_height: int = 50


@dataclass
class ArtNetConfig:
    ip: str = "127.0.0.1"
    port: int = 6454
    universe: int = 0


@dataclass
class SongConfig:
    """Configuration for a single song in the playlist."""
    midi: Optional[str] = None
    audio: Optional[str] = None  # Primary key - audio file path uniquely identifies a song
    offset: float = 0.0


@dataclass
class PlaylistConfig:
    """Configuration for the playlist."""
    songs: List["SongConfig"] = field(default_factory=list)
    current_song_index: int = 0


@dataclass
class FixturePosition:
    x: int
    y: int


# DMX channel count per fixture type. Derived from the type rather than stored
# per-instance; mirrors the render classes' dmx_channel_count (fixtures.py).
FIXTURE_CHANNEL_COUNTS: Dict[str, int] = {
    "ledbar": 7,
    "blinder": 8,
}


@dataclass
class FixtureInstance:
    """One patched fixture: an explicit type + DMX address, plus per-instance
    render params. Replaces the old type-by-array split (led_bar_controllers /
    blinders) with a single data-driven list."""
    type: str
    dmx_start: int
    x: int = 0
    y: int = 0
    bars: List[FixturePosition] = field(default_factory=list)  # ledbar: visual bars
    multiply_factor: float = 2.0                               # blinder render


@dataclass
class MidiNoteConfig:
    part: str
    color: str
    fixture: str = "ledbar"


@dataclass
class VolumeConfig:
    metronome: float = 0.5
    audio: float = 0.5


@dataclass
class LoggingConfig:
    level: str = "INFO"
    console_output: bool = True
    file_output: bool = True
    log_dir: str = "logs"
    max_file_size: int = 10 * 1024 * 1024  # 10MB
    backup_count: int = 5


# LED-bar channel-control MIDI notes. These mirror cases 1-7 in
# software_controller/main.py:handle_note_on — each note carries one DMX
# channel value (velocity * 2) for the LED bar. They're auto-registered in
# midi_note_map so the clip library can drop scenes (which expand into 7
# simultaneous note-on messages) through the existing add_event pipeline.
LEDBAR_CHANNEL_NOTES: Dict[str, MidiNoteConfig] = {
    "1": MidiNoteConfig(part="CH:BRIGHTNESS", color="#555555", fixture="ledbar"),
    "2": MidiNoteConfig(part="CH:BLUE",       color="#3333aa", fixture="ledbar"),
    "3": MidiNoteConfig(part="CH:GREEN",      color="#33aa33", fixture="ledbar"),
    "4": MidiNoteConfig(part="CH:RED",        color="#aa3333", fixture="ledbar"),
    "5": MidiNoteConfig(part="CH:ANIM",       color="#555555", fixture="ledbar"),
    "6": MidiNoteConfig(part="CH:SPEED",      color="#555555", fixture="ledbar"),
    "7": MidiNoteConfig(part="CH:DIRECTION",  color="#555555", fixture="ledbar"),
}


@dataclass
class Config:
    window: WindowConfig = field(default_factory=WindowConfig)
    artnet: ArtNetConfig = field(default_factory=ArtNetConfig)
    playlist: PlaylistConfig = field(default_factory=PlaylistConfig)
    fixtures: List[FixtureInstance] = field(default_factory=list)
    midi_note_map: Dict[str, MidiNoteConfig] = field(default_factory=dict)
    volume: VolumeConfig = field(default_factory=VolumeConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    last_dmx_source: Optional[str] = None
    last_midi_port: Optional[str] = None

    def __post_init__(self):
        # Ensure channel-control notes are registered even when constructed
        # via Config() directly (defaults path), not just via load().
        for note_str, note_cfg in LEDBAR_CHANNEL_NOTES.items():
            self.midi_note_map.setdefault(note_str, note_cfg)

    @classmethod
    def load(cls) -> "Config":
        """Load full configuration from shipped config.json + userprefs.json.

        On first launch (or whenever userprefs.json is absent), the file is
        created eagerly in the platform-conventional location with defaults.
        """
        config = cls()

        # Shipped, read-only data — fixtures + midi_note_map.
        shipped = shipped_config_path()
        if os.path.exists(shipped):
            try:
                with open(shipped, 'r') as f:
                    cls._apply_shipped(config, json.load(f))
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                print(f"Warning: error parsing shipped config '{shipped}': {e}")
                print("Using default fixtures and MIDI note map.")
        else:
            print(f"Warning: shipped config '{shipped}' is missing.")
            print("Using default fixtures and MIDI note map.")

        # Writable user preferences.
        prefs = userprefs_path()
        if prefs.exists():
            try:
                with open(prefs, 'r') as f:
                    cls._apply_userprefs(config, json.load(f))
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                print(f"Warning: error parsing userprefs '{prefs}': {e}")
                print("Using default user preferences.")
        else:
            # First launch: create userprefs.json eagerly with defaults.
            config.save()

        return config

    @classmethod
    def _apply_shipped(cls, config: "Config", data: Dict[str, Any]) -> None:
        """Populate fixtures and midi_note_map from shipped config data."""
        if "fixtures" in data:
            config.fixtures = cls._parse_fixtures(data["fixtures"])

        if "midi_note_map" in data:
            note_map = {}
            for note, note_data in data["midi_note_map"].items():
                if isinstance(note_data, dict) and "part" in note_data and "color" in note_data:
                    note_map[note] = MidiNoteConfig(
                        part=str(note_data["part"]),
                        color=cls._validate_color(note_data["color"]),
                        fixture=str(note_data.get("fixture", "ledbar")),
                    )
            config.midi_note_map = note_map

        # Auto-register LED-bar channel-control notes (1-7). The shipped
        # file should already include these; this is a safety net so that
        # scene drops always have a target in the note map.
        for note_str, note_cfg in LEDBAR_CHANNEL_NOTES.items():
            config.midi_note_map.setdefault(note_str, note_cfg)

    @classmethod
    def _apply_userprefs(cls, config: "Config", data: Dict[str, Any]) -> None:
        """Populate user-editable sections from userprefs.json data."""
        if "window" in data:
            window_data = data["window"]
            config.window = WindowConfig(
                width=cls._validate_int(window_data.get("width", 1280), 400, 4000),
                height=cls._validate_int(window_data.get("height", 720), 300, 3000),
                dmx_panel_collapsed=bool(window_data.get("dmx_panel_collapsed", False)),
                waveform_track_height=cls._validate_int(window_data.get("waveform_track_height", 100), 30, 1000),
                midi_events_track_height=cls._validate_int(window_data.get("midi_events_track_height", 50), 30, 1000)
            )

        if "artnet" in data:
            artnet_data = data["artnet"]
            config.artnet = ArtNetConfig(
                ip=cls._validate_ip(artnet_data.get("ip", "127.0.0.1")),
                port=cls._validate_int(artnet_data.get("port", 6454), 1, 65535),
                universe=cls._validate_int(artnet_data.get("universe", 0), 0, 32767)
            )

        if "playlist" in data:
            playlist_data = data["playlist"]
            songs = []
            for song_data in playlist_data.get("songs", []):
                songs.append(SongConfig(
                    midi=cls._validate_file_path(song_data.get("midi")),
                    audio=cls._validate_file_path(song_data.get("audio")),
                    offset=cls._validate_float(song_data.get("offset", 0.0), -3600.0, 3600.0)
                ))
            config.playlist = PlaylistConfig(
                songs=songs,
                current_song_index=cls._validate_int(playlist_data.get("current_song_index", 0), 0, max(0, len(songs) - 1))
            )

        if "volume" in data:
            volume_data = data["volume"]
            config.volume = VolumeConfig(
                metronome=cls._validate_float(volume_data.get("metronome", 0.5), 0.0, 1.0),
                audio=cls._validate_float(volume_data.get("audio", 0.5), 0.0, 1.0)
            )

        if "logging" in data:
            logging_data = data["logging"]
            config.logging = LoggingConfig(
                level=cls._validate_log_level(logging_data.get("level", "INFO")),
                console_output=bool(logging_data.get("console_output", True)),
                file_output=bool(logging_data.get("file_output", True)),
                log_dir=str(logging_data.get("log_dir", "logs")),
                max_file_size=cls._validate_int(logging_data.get("max_file_size", 10 * 1024 * 1024), 1024, 100 * 1024 * 1024),
                backup_count=cls._validate_int(logging_data.get("backup_count", 5), 1, 20)
            )

        config.last_dmx_source = data.get("last_dmx_source")
        config.last_midi_port = data.get("last_midi_port")

    @staticmethod
    def _parse_fixtures(fixtures_data: Any) -> List[FixtureInstance]:
        """Parse the flat fixtures list. Each entry carries an explicit type +
        DMX address; unknown types and malformed entries are skipped."""
        fixtures: List[FixtureInstance] = []

        if not isinstance(fixtures_data, list):
            return fixtures

        for entry in fixtures_data:
            if not isinstance(entry, dict):
                continue
            fixture_type = str(entry.get("type", ""))
            if fixture_type not in FIXTURE_CHANNEL_COUNTS or "dmx_start" not in entry:
                continue

            bars = []
            for bar_data in entry.get("bars", []):
                if isinstance(bar_data, dict) and "x" in bar_data and "y" in bar_data:
                    bars.append(FixturePosition(
                        x=Config._validate_int(bar_data["x"], 0, 10000),
                        y=Config._validate_int(bar_data["y"], 0, 10000)
                    ))

            fixtures.append(FixtureInstance(
                type=fixture_type,
                dmx_start=Config._validate_int(entry["dmx_start"], 1, 512),
                x=Config._validate_int(entry.get("x", 0), 0, 10000),
                y=Config._validate_int(entry.get("y", 0), 0, 10000),
                bars=bars,
                multiply_factor=Config._validate_float(entry.get("multiply_factor", 2.0), 0.1, 10.0),
            ))

        return fixtures

    @staticmethod
    def _validate_int(value: Any, min_val: int, max_val: int) -> int:
        """Validate and clamp integer values."""
        try:
            val = int(value)
            return max(min_val, min(max_val, val))
        except (ValueError, TypeError):
            return min_val

    @staticmethod
    def _validate_float(value: Any, min_val: float, max_val: float) -> float:
        """Validate and clamp float values."""
        try:
            val = float(value)
            return max(min_val, min(max_val, val))
        except (ValueError, TypeError):
            return min_val

    @staticmethod
    def _validate_ip(value: Any) -> str:
        """Basic IP address validation."""
        if not isinstance(value, str):
            return "127.0.0.1"

        parts = value.split('.')
        if len(parts) != 4:
            return "127.0.0.1"

        try:
            for part in parts:
                num = int(part)
                if not 0 <= num <= 255:
                    return "127.0.0.1"
            return value
        except ValueError:
            return "127.0.0.1"

    @staticmethod
    def _validate_color(value: Any) -> str:
        """Validate hex color format."""
        if not isinstance(value, str):
            return "#ffffff"

        if value.startswith("#") and len(value) == 7:
            try:
                int(value[1:], 16)
                return value
            except ValueError:
                pass

        return "#ffffff"

    @staticmethod
    def _validate_file_path(value: Any) -> Optional[str]:
        """Validate file path exists."""
        if not isinstance(value, str):
            return None

        if os.path.exists(value):
            return value

        return None

    @staticmethod
    def _validate_log_level(value: Any) -> str:
        """Validate logging level."""
        if not isinstance(value, str):
            return "INFO"

        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        level = value.upper()

        if level in valid_levels:
            return level

        return "INFO"

    def save(self) -> bool:
        """Save user-editable preferences to userprefs.json.

        The shipped config.json is never modified — fixtures and
        midi_note_map are read-only at runtime.
        """
        try:
            data = self._userprefs_to_dict()
            path = userprefs_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving userprefs: {e}")
            return False

    def _userprefs_to_dict(self) -> Dict[str, Any]:
        """Convert user-editable sections to a dictionary for JSON serialization."""
        data: Dict[str, Any] = {}

        data["window"] = asdict(self.window)
        data["artnet"] = asdict(self.artnet)
        data["playlist"] = {
            "songs": [asdict(song) for song in self.playlist.songs],
            "current_song_index": self.playlist.current_song_index
        }
        data["volume"] = asdict(self.volume)
        data["logging"] = asdict(self.logging)

        if self.last_dmx_source:
            data["last_dmx_source"] = self.last_dmx_source
        if self.last_midi_port:
            data["last_midi_port"] = self.last_midi_port

        return data

    # Playlist management methods
    def get_current_song(self) -> Optional[SongConfig]:
        """Get the current song configuration."""
        if not self.playlist.songs:
            return None
        index = self.playlist.current_song_index
        if 0 <= index < len(self.playlist.songs):
            return self.playlist.songs[index]
        return None

    def set_current_song_index(self, index: int) -> bool:
        """Set the current song index. Returns True if successful."""
        if 0 <= index < len(self.playlist.songs):
            self.playlist.current_song_index = index
            return True
        return False

    def find_song_by_audio(self, audio_path: str) -> Optional[int]:
        """Find a song index by audio file path. Returns None if not found."""
        for i, song in enumerate(self.playlist.songs):
            if song.audio == audio_path:
                return i
        return None

    def add_song(self, audio: str, midi: Optional[str] = None, offset: float = 0.0) -> int:
        """Add a new song to the playlist. Returns the new song's index."""
        song = SongConfig(
            audio=self._validate_file_path(audio),
            midi=self._validate_file_path(midi) if midi else None,
            offset=self._validate_float(offset, -3600.0, 3600.0)
        )
        self.playlist.songs.append(song)
        return len(self.playlist.songs) - 1

    def remove_song(self, index: int) -> bool:
        """Remove a song from the playlist. Returns True if successful."""
        if 0 <= index < len(self.playlist.songs):
            self.playlist.songs.pop(index)
            # Adjust current index if needed
            if self.playlist.current_song_index >= len(self.playlist.songs):
                self.playlist.current_song_index = max(0, len(self.playlist.songs) - 1)
            elif self.playlist.current_song_index > index:
                self.playlist.current_song_index -= 1
            return True
        return False

    def update_current_song(self, midi: Optional[str] = None, offset: Optional[float] = None) -> None:
        """Update the current song's MIDI or offset."""
        song = self.get_current_song()
        if song:
            if midi is not None:
                song.midi = self._validate_file_path(midi) if midi else None
            if offset is not None:
                song.offset = self._validate_float(offset, -3600.0, 3600.0)

    def update_volume(self, metronome: Optional[float] = None, audio: Optional[float] = None) -> None:
        """Update volume settings with validation."""
        if metronome is not None:
            self.volume.metronome = self._validate_float(metronome, 0.0, 1.0)
        if audio is not None:
            self.volume.audio = self._validate_float(audio, 0.0, 1.0)

    def get_used_dmx_channels(self) -> Dict[str, List[int]]:
        """Get mapping of fixture names to their DMX channels."""
        used_channels = {}

        type_counts: Dict[str, int] = {}
        for fixture in self.fixtures:
            type_counts[fixture.type] = type_counts.get(fixture.type, 0) + 1
            fixture_name = f"{fixture.type.capitalize()} {type_counts[fixture.type]}"
            channel_count = FIXTURE_CHANNEL_COUNTS.get(fixture.type, 0)
            channels = list(range(fixture.dmx_start, fixture.dmx_start + channel_count))
            if channels:
                used_channels[fixture_name] = channels

        return used_channels
