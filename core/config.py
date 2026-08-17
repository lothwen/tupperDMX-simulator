# ABOUTME: Configuration management for TupperDMX. Splits storage across two files:
# ABOUTME: shipped config.json (read-only fixture patch) and userprefs.json (writable user prefs).
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any
import json
import os
import uuid
from pathlib import Path

from core.fixture_library import (
    fixture_modes, load_fixture_defs, resolve_fixture,
)
from core.paths import bundle_dir, userprefs_dir
from tupperlogger import get_logger

logger = get_logger("config")


def shipped_config_path() -> str:
    """Path to the shipped, read-only config.json.

    Contains the fixture patch (seed). Bundled with the app via
    PyInstaller (see build.spec datas) and never written at runtime.
    """
    return os.path.join(bundle_dir(), "config.json")


def userprefs_path() -> Path:
    """Full path to the user-writable userprefs.json file."""
    return userprefs_dir() / "userprefs.json"


@dataclass
class StageFogConfig:
    """Volumetric fog on the virtual stage. GPU path only, off by default.

    How *much* fog there is is deliberately NOT here: that is
    `WindowConfig.stage_haze`, which already exists and already has a slider.
    One density control, not two. These are the things that decide what that
    density then looks like, and how expensive it is to draw.
    """

    # Needs `stage_gpu` as well — a ray march has nowhere to run on the raster
    # path. Off by default for the same reason `stage_gpu` is.
    enabled: bool = False

    # -- the look ------------------------------------------------------
    # How far up from the floor the fog reaches, in scene units, and how sharply
    # it thins on the way. Haze settles: there is more of it round the ankles
    # than in the truss, and a slab of even density reads as a bug.
    height: float = 520.0
    height_falloff: float = 1.6
    # What colour the air scatters. Not white: real haze is very slightly cool.
    color: str = "#dfe4ee"
    # How much of the light that passes through the fog comes back to the eye.
    scattering: float = 1.0
    # Henyey-Greenstein g. Positive is forward-scattering, which is why a beam
    # aimed at you is blinding and the same beam crossing the stage is a shaft.
    anisotropy: float = 0.55
    # How ragged the air is, 0..3. 0 is a perfectly smooth medium, which
    # integrates to a translucent sheet laid over the picture — the "white
    # filter" look. Real haze has structure at every scale and drifts, and this
    # is that structure: how far the density swings either side of the smooth
    # field. 1 is "a pocket of air can be completely empty"; past that the
    # billows separate into distinct banks with clear air between them, which is
    # a real stage under a smoke machine rather than one under a hazer.
    texture: float = 1.1
    # How big those features are, in scene units. Around a third of the stage's
    # height is what reads as billows rather than as grain.
    texture_scale: float = 260.0
    # A stage-wide key light for the medium itself, so smoke is visible before
    # any fixture is. `light_dir_deg` is measured the way every other axis on
    # this stage is: 90 is straight down the picture.
    light_intensity: float = 0.35
    light_dir_deg: float = 255.0

    # -- what it costs -------------------------------------------------
    # Samples along each view ray. The single biggest cost, and the single
    # biggest difference between smooth shafts and visible banding.
    march_steps: int = 24
    # How deep the marched slab is, in scene units.
    max_distance: float = 1400.0
    # How far each ray's start is dithered, to trade banding for noise the
    # temporal pass then averages away. 0 disables it and the bands come back.
    jitter: float = 0.8
    # Fraction of the viewport the fog is marched at, upsampled on resolve.
    # Half is the shipped default: fog is low-frequency, so the resolution it
    # is computed at is much cheaper to give up than the step count.
    resolution: float = 0.5


@dataclass
class WindowConfig:
    width: int = 1280
    height: int = 720
    dmx_panel_collapsed: bool = False
    waveform_track_height: int = 100
    midi_events_track_height: int = 50
    timeline_section_height: int = 230
    # Rows "S" mode: every fixture lane collapsed to one row. Persisted so the
    # section height (fitted to collapsed lanes) stays consistent on reload.
    tracks_collapsed: bool = False
    # How hazy the virtual stage's air is before any hazer runs, 0..1. Beams are
    # only visible because there is something for them to scatter off, so this
    # is what a rig with no hazer patched still gets to see.
    stage_haze: float = 0.15
    # Draw the stage's light on the GPU: one HDR buffer everything adds into,
    # tonemapped once with ACES, instead of 8-bit brushes that alpha-stack.
    # Off by default — it needs a working OpenGL context, and the raster path
    # is the one that survives a venue machine with no usable driver.
    stage_gpu: bool = False
    # Exposure applied before the tonemap, 0.1..4.0. Only the GPU path reads it.
    stage_exposure: float = 1.0
    # Volumetric fog: what `stage_haze` looks like once it is marched through
    # rather than multiplied into a beam's alpha. Needs `stage_gpu`.
    fog: StageFogConfig = field(default_factory=StageFogConfig)


@dataclass
class ArtNetConfig:
    ip: str = "127.0.0.1"
    port: int = 6454
    universe: int = 0


@dataclass
class IOConfig:
    """Explicit I/O routing between the three blocks (sequencer → internal
    controller → virtual stage). Sequencer MIDI out and controller DMX out
    fan out (any combination); the stage displays exactly one DMX feed."""
    # Sequencer MIDI outputs
    midi_to_controller: bool = True
    midi_to_port: bool = False
    midi_port: Optional[str] = None
    # Internal software DMX controller
    controller_enabled: bool = True
    dmx_to_stage: bool = True
    dmx_to_artnet: bool = False        # ArtNetSender → config.artnet target
    dmx_to_usb: bool = False
    usb_out_device: Optional[str] = None      # "FTDI:<desc>:<serial>"
    # Virtual stage input: "internal" | "artnet" | "usb"
    stage_source: str = "internal"
    stage_usb_device: Optional[str] = None    # "FTDI:<desc>:<serial>"


@dataclass
class VoiceCueConfig:
    """One spoken guide cue (Fase 4): TTS text fired at a timeline position,
    routed to the voice bus (the musician's click side). Rides the show
    manifest — guide audio is not sequence data, so it stays out of the .mid."""
    time: float = 0.0
    text: str = ""


@dataclass
class SongConfig:
    """Configuration for a single song in the playlist."""
    midi: Optional[str] = None
    audio: Optional[str] = None  # Primary key - audio file path uniquely identifies a song
    offset: float = 0.0
    # Guide track: optional full-mix reference for designing lights (the
    # backing in `audio` stays the live master). Editor-only monitoring;
    # live surfaces never play it.
    guide: Optional[str] = None
    guide_offset: float = 0.0
    # Show-model metadata (Fase 3). id is a stable uuid assigned by the show
    # layer (so identity survives file renames); cache holds derived
    # {duration, bpm, audio_mtime} values, never hand-edited.
    id: str = ""
    title: str = ""
    key: str = ""                                       # musical key, e.g. "Am"
    precount: bool = True                               # count-in click; off = segue from previous song
    voice_cues: List[VoiceCueConfig] = field(default_factory=list)
    cache: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlaylistConfig:
    """Configuration for the playlist."""
    songs: List["SongConfig"] = field(default_factory=list)
    current_song_index: int = 0


@dataclass
class FixturePosition:
    x: int
    y: int


def fixture_channel_count(type_name: str, mode: str = "") -> int:
    """DMX channel count for a fixture type (0 if the type is unknown).

    Every type — including the built-in ledbar/blinder — is data-driven from the
    fixture library, so the count comes from its definition. A fixture's mode
    decides how many channels it occupies, so pass it whenever one is known; an
    empty mode means the type's default."""
    fdef = resolve_fixture(type_name, mode)
    return fdef.channel_count if fdef else 0


def known_fixture_types() -> set:
    """All resolvable fixture-type names, from the fixture library."""
    return set(load_fixture_defs())


@dataclass
class FixtureInstance:
    """One patched fixture: an explicit type + DMX address, plus per-instance
    render params. Replaces the old type-by-array split (led_bar_controllers /
    blinders) with a single data-driven list."""
    type: str
    dmx_start: int
    name: str = ""                                             # user alias (optional)
    channel: int = -1                                          # stable MIDI channel (0-15); -1 = unassigned
    mode: str = ""                                             # OFL mode; "" = the type's first
    x: int = 0
    y: int = 0
    # How it is rigged: True = flown, hanging under a truss (housing upside
    # down, beam thrown at the floor); False = standing on the deck (housing
    # the right way up, beam thrown at the ceiling). True is the default
    # because a beam that points down is what every stage drew before a fixture
    # could say which way up it was.
    hanging: bool = True
    bars: List[FixturePosition] = field(default_factory=list)  # ledbar: visual bars


@dataclass
class VolumeConfig:
    metronome: float = 0.5
    audio: float = 0.5
    voice: float = 0.8                                  # TTS guide bus (cues + spoken count-in)


# Valid AudioConfig.routing values (mirrors audio/mixer.py ROUTING_MODES;
# kept literal here so the Qt-free core has no dependency on the audio package).
AUDIO_ROUTING_MODES = ("stereo", "split_backing_left", "split_backing_right")


@dataclass
class AudioConfig:
    """Machine-level audio output setup (Fase 4 live player). Device and
    channel routing are per-machine concerns, so they live in userprefs —
    per-song count-in on/off stays in the show (SongConfig.precount)."""
    output_device: str = ""              # QAudioDevice id; "" = system default
    routing: str = "stereo"              # see AUDIO_ROUTING_MODES
    count_in_bars: int = 2               # length of the precount (bars of 4)
    count_in_voice: bool = True          # speak beat numbers on the last bar
    tts_voice: str = "es_ES-davefx-medium"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    console_output: bool = True
    file_output: bool = True
    log_dir: str = "logs"
    max_file_size: int = 10 * 1024 * 1024  # 10MB
    backup_count: int = 5


@dataclass
class WebCueConfig:
    """One web-remote lighting cue: a labelled button that fires MIDI messages
    through the configured MIDI outputs (same path as clip-library preview)."""
    label: str
    color: str = "#888888"                # hex swatch shown on the web button
    messages: List[List[int]] = field(default_factory=list)  # [channel, note, velocity]


def default_webremote_cues() -> List["WebCueConfig"]:
    """Shipped cue set, built from existing controller verbs (color macros
    72-75, ledbar bank-select 36+n / scene-load 24+n, blackout-all 51) against
    the default patch channels (0 = ledbar, 1/2 = blinders)."""
    all_fx = [0, 1, 2]
    return [
        WebCueConfig("Warm", "#ffb86b", [[c, 73, 127] for c in all_fx]),       # ORANGE macro
        WebCueConfig("Cool Blue", "#7cc4ff", [[c, 74, 127] for c in all_fx]),  # CYAN macro
        WebCueConfig("Strobe", "#e6e6e6", [[0, 40, 0], [0, 24, 100]]),         # STROBES bank + scene 0
        WebCueConfig("Full flash", "#ffffff", [[c, 72, 127] for c in all_fx]), # WHITE macro
        WebCueConfig("Magenta", "#ff5bd1", [[c, 75, 127] for c in all_fx]),    # PINK macro
        WebCueConfig("Blackout", "#666666", [[0, 51, 0]]),                     # global toggle
    ]


@dataclass
class WebRemoteConfig:
    """Embedded web-remote server (tablet UI). Off by default; the page is
    served on the LAN without auth, so enabling is an explicit choice."""
    enabled: bool = False
    port: int = 8765
    cues: List[WebCueConfig] = field(default_factory=default_webremote_cues)


@dataclass
class Config:
    window: WindowConfig = field(default_factory=WindowConfig)
    artnet: ArtNetConfig = field(default_factory=ArtNetConfig)
    playlist: PlaylistConfig = field(default_factory=PlaylistConfig)
    fixtures: List[FixtureInstance] = field(default_factory=list)
    volume: VolumeConfig = field(default_factory=VolumeConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    io: IOConfig = field(default_factory=IOConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    webremote: WebRemoteConfig = field(default_factory=WebRemoteConfig)
    # Show-project pointers (machine-level: which show this machine had open).
    last_show_path: Optional[str] = None
    recent_shows: List[str] = field(default_factory=list)

    def next_free_channel(self) -> int:
        """Lowest non-negative MIDI channel not used by any patched fixture.
        The designer's auto-default when adding a fixture."""
        used = {f.channel for f in self.fixtures if f.channel >= 0}
        ch = 0
        while ch in used:
            ch += 1
        return ch

    @classmethod
    def load(cls) -> "Config":
        """Load full configuration from shipped config.json + userprefs.json.

        On first launch (or whenever userprefs.json is absent), the file is
        created eagerly in the platform-conventional location with defaults.
        """
        config = cls()

        # Shipped, read-only data — the fixture patch seed.
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
                # utf-8-sig, not the locale encoding: this file is meant to be
                # hand-edited (the rack box exports it over SMB), and an editor
                # on a Windows client leaves a UTF-8 BOM that json.load rejects
                # outright — which used to cost the operator the whole file.
                with open(prefs, 'r', encoding='utf-8-sig') as f:
                    cls._apply_userprefs(config, json.load(f))
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                kept = cls._preserve_bad_prefs(prefs)
                print(f"Warning: error parsing userprefs '{prefs}': {e}")
                print("Using default user preferences" + (
                    f"; the unparseable file was kept as '{kept}'."
                    if kept else "."))
        else:
            # First launch: create userprefs.json eagerly with defaults.
            config.save()

        return config

    @staticmethod
    def _preserve_bad_prefs(prefs: Path) -> Optional[Path]:
        """Move an unparseable userprefs.json aside, returning where it went.

        Loading defaults over it is not neutral: the very next save() rewrites
        the file, and `last_show_path`/`recent_shows` are the ONLY record of
        which show to open — losing them silently boots into a brand-new empty
        show, which on a headless box (no dialog, no show picker) looks exactly
        like the app ignoring the preferences.
        """
        kept = prefs.with_name(prefs.name + ".bad")
        try:
            os.replace(prefs, kept)
            return kept
        except OSError:
            return None

    @classmethod
    def _apply_shipped(cls, config: "Config", data: Dict[str, Any]) -> None:
        """Populate the fixture patch from shipped config data. The note layout
        is no longer config-driven — it's derived from the fixture defs via
        note_layout (see build_note_map / describe_note)."""
        if "fixtures" in data:
            config.fixtures = cls._parse_fixtures(data["fixtures"])

    @classmethod
    def _apply_userprefs(cls, config: "Config", data: Dict[str, Any]) -> None:
        """Populate user-editable sections from userprefs.json data.

        The playlist and fixtures sections are legacy (pre-show): they are
        still PARSED here — they are the source the one-time show migration
        adopts — but save() no longer writes them; the open show's manifest
        owns them now."""
        if "fixtures" in data:
            config.fixtures = cls._parse_fixtures(data["fixtures"])

        if "window" in data:
            window_data = data["window"]
            config.window = WindowConfig(
                width=cls._validate_int(window_data.get("width", 1280), 400, 4000),
                height=cls._validate_int(window_data.get("height", 720), 300, 3000),
                dmx_panel_collapsed=bool(window_data.get("dmx_panel_collapsed", False)),
                waveform_track_height=cls._validate_int(window_data.get("waveform_track_height", 100), 30, 1000),
                midi_events_track_height=cls._validate_int(window_data.get("midi_events_track_height", 50), 30, 1000),
                timeline_section_height=cls._validate_int(window_data.get("timeline_section_height", 230), 120, 2000),
                tracks_collapsed=bool(window_data.get("tracks_collapsed", False)),
                stage_haze=cls._validate_float(
                    window_data.get("stage_haze", 0.15), 0.0, 1.0),
                stage_gpu=bool(window_data.get("stage_gpu", False)),
                stage_exposure=cls._validate_float(
                    window_data.get("stage_exposure", 1.0), 0.1, 4.0),
                fog=cls._parse_fog(window_data.get("fog")),
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
                    offset=cls._validate_float(song_data.get("offset", 0.0), -3600.0, 3600.0),
                    id=str(song_data.get("id") or ""),
                    title=str(song_data.get("title", "")),
                    key=str(song_data.get("key", "")),
                    precount=bool(song_data.get("precount", True)),
                    voice_cues=cls._parse_voice_cues(song_data.get("voice_cues")),
                    cache=(song_data["cache"]
                           if isinstance(song_data.get("cache"), dict) else {}),
                ))
            config.playlist = PlaylistConfig(
                songs=songs,
                current_song_index=cls._validate_int(playlist_data.get("current_song_index", 0), 0, max(0, len(songs) - 1))
            )

        if "volume" in data:
            volume_data = data["volume"]
            config.volume = VolumeConfig(
                metronome=cls._validate_float(volume_data.get("metronome", 0.5), 0.0, 1.0),
                audio=cls._validate_float(volume_data.get("audio", 0.5), 0.0, 1.0),
                voice=cls._validate_float(volume_data.get("voice", 0.8), 0.0, 1.0)
            )

        if "audio" in data:
            audio_data = data["audio"]
            defaults = AudioConfig()
            routing = audio_data.get("routing", defaults.routing)
            if routing not in AUDIO_ROUTING_MODES:
                routing = defaults.routing
            config.audio = AudioConfig(
                output_device=str(audio_data.get("output_device", "") or ""),
                routing=routing,
                count_in_bars=cls._validate_int(
                    audio_data.get("count_in_bars", defaults.count_in_bars), 1, 8),
                count_in_voice=bool(
                    audio_data.get("count_in_voice", defaults.count_in_voice)),
                tts_voice=str(audio_data.get("tts_voice", defaults.tts_voice)
                              or defaults.tts_voice),
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

        if "io" in data:
            io_data = data["io"]
            defaults = IOConfig()
            stage_source = io_data.get("stage_source", "internal")
            if stage_source not in ("internal", "artnet", "usb"):
                stage_source = "internal"
            config.io = IOConfig(
                midi_to_controller=bool(io_data.get("midi_to_controller", defaults.midi_to_controller)),
                midi_to_port=bool(io_data.get("midi_to_port", defaults.midi_to_port)),
                midi_port=cls._validate_optional_str(io_data.get("midi_port")),
                controller_enabled=bool(io_data.get("controller_enabled", defaults.controller_enabled)),
                dmx_to_stage=bool(io_data.get("dmx_to_stage", defaults.dmx_to_stage)),
                dmx_to_artnet=bool(io_data.get("dmx_to_artnet", defaults.dmx_to_artnet)),
                dmx_to_usb=bool(io_data.get("dmx_to_usb", defaults.dmx_to_usb)),
                usb_out_device=cls._validate_optional_str(io_data.get("usb_out_device")),
                stage_source=stage_source,
                stage_usb_device=cls._validate_optional_str(io_data.get("stage_usb_device")),
            )
        else:
            # Legacy prefs (pre-io): derive the routing from the old implicit
            # selectors. One-time: the next save() writes the io section and
            # drops the legacy keys.
            config.io = cls._migrate_legacy_io(data, config.artnet.ip)

        config.last_show_path = cls._validate_optional_str(data.get("last_show_path"))
        recents = data.get("recent_shows")
        if isinstance(recents, list):
            config.recent_shows = [s for s in recents if isinstance(s, str) and s]

        if "webremote" in data:
            web_data = data["webremote"]
            config.webremote = WebRemoteConfig(
                enabled=bool(web_data.get("enabled", False)),
                port=cls._validate_int(web_data.get("port", 8765), 1024, 65535),
                cues=(cls._parse_web_cues(web_data["cues"])
                      if "cues" in web_data else default_webremote_cues()),
            )

    @staticmethod
    def _migrate_legacy_io(data: Dict[str, Any], artnet_ip: str) -> "IOConfig":
        """Map the pre-io implicit selectors (last_midi_port / last_dmx_source +
        the localhost-spawns-controller convention) onto an explicit IOConfig."""
        io = IOConfig()

        last_midi_port = data.get("last_midi_port")
        if isinstance(last_midi_port, str) and last_midi_port and last_midi_port != "Software emulated":
            io.midi_to_controller = False
            io.midi_to_port = True
            io.midi_port = last_midi_port

        last_dmx_source = data.get("last_dmx_source")
        if isinstance(last_dmx_source, str):
            if last_dmx_source.startswith("FTDI"):
                io.controller_enabled = False
                io.dmx_to_stage = False
                io.stage_source = "usb"
                io.stage_usb_device = last_dmx_source
            elif last_dmx_source == "Art-Net" and artnet_ip not in ("127.0.0.1", "localhost"):
                io.controller_enabled = False
                io.dmx_to_stage = False
                io.stage_source = "artnet"
            # Art-Net + localhost == the old loopback simulation == defaults.

        return io

    @staticmethod
    def _parse_web_cues(cues_data: Any) -> List[WebCueConfig]:
        """Parse the webremote cue list. Entries without a label and malformed
        messages are dropped; channel/note/velocity are clamped to MIDI ranges."""
        cues: List[WebCueConfig] = []

        if not isinstance(cues_data, list):
            return cues

        for entry in cues_data:
            if not isinstance(entry, dict):
                continue
            label = entry.get("label")
            if not isinstance(label, str) or not label:
                continue

            messages: List[List[int]] = []
            raw_messages = entry.get("messages", [])
            if isinstance(raw_messages, list):
                for msg in raw_messages:
                    if not isinstance(msg, (list, tuple)) or len(msg) != 3:
                        continue
                    messages.append([
                        Config._validate_int(msg[0], 0, 15),
                        Config._validate_int(msg[1], 0, 127),
                        Config._validate_int(msg[2], 0, 127),
                    ])

            color = entry.get("color")
            cues.append(WebCueConfig(
                label=label,
                color=color if isinstance(color, str) and color else "#888888",
                messages=messages,
            ))

        return cues

    @staticmethod
    def _parse_voice_cues(cues_data: Any) -> List[VoiceCueConfig]:
        """Parse a song's voice-cue list. Entries without text are dropped;
        times clamp to the valid timeline range."""
        cues: List[VoiceCueConfig] = []
        if not isinstance(cues_data, list):
            return cues
        for entry in cues_data:
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            cues.append(VoiceCueConfig(
                time=Config._validate_float(entry.get("time", 0.0), 0.0, 36000.0),
                text=text.strip(),
            ))
        cues.sort(key=lambda c: c.time)
        return cues

    @staticmethod
    def _validate_optional_str(value: Any) -> Optional[str]:
        """Pass through non-empty strings, anything else becomes None."""
        if isinstance(value, str) and value:
            return value
        return None

    @classmethod
    def _parse_fog(cls, fog_data: Any) -> StageFogConfig:
        """The stage's volumetric-fog settings, clamped to what can be drawn.

        Every bound here is a real limit rather than taste: a march of 0 steps
        draws nothing, one of 512 stalls the frame, and a resolution above 1
        would render more fog pixels than there are screen pixels to show them.
        """
        if not isinstance(fog_data, dict):
            return StageFogConfig()
        defaults = StageFogConfig()
        return StageFogConfig(
            enabled=bool(fog_data.get("enabled", defaults.enabled)),
            height=cls._validate_float(
                fog_data.get("height", defaults.height), 1.0, 4000.0),
            height_falloff=cls._validate_float(
                fog_data.get("height_falloff", defaults.height_falloff), 0.0, 12.0),
            color=cls._validate_hex_color(
                fog_data.get("color", defaults.color), defaults.color),
            scattering=cls._validate_float(
                fog_data.get("scattering", defaults.scattering), 0.0, 8.0),
            # Fully forward or fully backward scattering is a singularity in the
            # Henyey-Greenstein phase function, so it stops just short of both.
            anisotropy=cls._validate_float(
                fog_data.get("anisotropy", defaults.anisotropy), -0.95, 0.95),
            # Past 1 the thin side of the noise clamps against empty air, which
            # is the point rather than a limit: that is what separates billows
            # into banks with clear air between them. The ceiling is where the
            # structure stops getting coarser and starts being two-tone.
            texture=cls._validate_float(
                fog_data.get("texture", defaults.texture), 0.0, 3.0),
            texture_scale=cls._validate_float(
                fog_data.get("texture_scale", defaults.texture_scale),
                20.0, 2000.0),
            light_intensity=cls._validate_float(
                fog_data.get("light_intensity", defaults.light_intensity), 0.0, 4.0),
            light_dir_deg=cls._validate_float(
                fog_data.get("light_dir_deg", defaults.light_dir_deg), 0.0, 360.0),
            march_steps=cls._validate_int(
                fog_data.get("march_steps", defaults.march_steps), 4, 128),
            max_distance=cls._validate_float(
                fog_data.get("max_distance", defaults.max_distance), 100.0, 8000.0),
            jitter=cls._validate_float(
                fog_data.get("jitter", defaults.jitter), 0.0, 2.0),
            resolution=cls._validate_float(
                fog_data.get("resolution", defaults.resolution), 0.125, 1.0),
        )

    @staticmethod
    def _validate_hex_color(value: Any, fallback: str) -> str:
        """A `#rrggbb` string, or the default. Nothing else reaches a shader."""
        if isinstance(value, str) and len(value) == 7 and value.startswith("#"):
            try:
                int(value[1:], 16)
            except ValueError:
                return fallback
            return value.lower()
        return fallback

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
            if fixture_type not in known_fixture_types() or "dmx_start" not in entry:
                # A patch entry whose type no longer resolves is dropped, and
                # the fixture simply disappears from the show — so say why.
                logger.warning(
                    f"Dropping patched fixture '{fixture_type or '?'}': "
                    f"{'no dmx_start' if fixture_type in known_fixture_types() else 'unknown type'}")
                continue

            # An unknown mode falls back to the type's default rather than
            # dropping the fixture. Storing "" rather than the resolved name
            # means renaming a mode in a def never strands a saved patch.
            mode = str(entry.get("mode", "") or "")
            if mode and mode not in fixture_modes(fixture_type):
                logger.warning(f"Fixture '{fixture_type}' has no mode '{mode}'; "
                               f"using its default mode")
                mode = ""

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
                name=str(entry.get("name", "")),
                channel=Config._validate_int(entry.get("channel", -1), -1, 15),
                mode=mode,
                x=Config._validate_int(entry.get("x", 0), 0, 10000),
                y=Config._validate_int(entry.get("y", 0), 0, 10000),
                # A patch saved before fixtures could be rigged either way is
                # read as flown, which is where its beams already pointed.
                hanging=bool(entry.get("hanging", True)),
                bars=bars,
            ))

        # Assign a stable MIDI channel to any fixture that lacks one. Scanning in
        # list order means a legacy patch (no channel key) reproduces today's
        # 0,1,2,... by position, so existing .mid clips stay correctly bound.
        used = {f.channel for f in fixtures if f.channel >= 0}
        nxt = 0
        for f in fixtures:
            if f.channel < 0:
                while nxt in used:
                    nxt += 1
                f.channel = nxt
                used.add(nxt)

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

        The shipped config.json is never modified — its fixture patch seed is
        read-only at runtime.
        """
        try:
            # Local import: core.show modules import from core.config.
            from core.show.store import atomic_write_json
            atomic_write_json(userprefs_path(), self._userprefs_to_dict())
            return True
        except Exception as e:
            print(f"Error saving userprefs: {e}")
            return False

    def _userprefs_to_dict(self) -> Dict[str, Any]:
        """Convert user-editable sections to a dictionary for JSON serialization."""
        data: Dict[str, Any] = {}

        # Machine-level prefs only: the playlist and the fixture patch belong
        # to the open show's manifest (show.json) and are deliberately not
        # written here — legacy playlist/fixtures keys are read once by the
        # show migration and dropped on the next save.
        data["window"] = asdict(self.window)
        data["artnet"] = asdict(self.artnet)
        data["volume"] = asdict(self.volume)
        data["logging"] = asdict(self.logging)
        data["io"] = asdict(self.io)
        data["audio"] = asdict(self.audio)
        data["webremote"] = asdict(self.webremote)
        data["last_show_path"] = self.last_show_path
        data["recent_shows"] = list(self.recent_shows)

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
            offset=self._validate_float(offset, -3600.0, 3600.0),
            id=str(uuid.uuid4()),
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

    def update_current_song(self, midi: Optional[str] = None, offset: Optional[float] = None,
                            guide: Optional[str] = None,
                            guide_offset: Optional[float] = None) -> None:
        """Update the current song's MIDI, offsets or guide track ("" clears)."""
        song = self.get_current_song()
        if song:
            if midi is not None:
                song.midi = self._validate_file_path(midi) if midi else None
            if offset is not None:
                song.offset = self._validate_float(offset, -3600.0, 3600.0)
            if guide is not None:
                song.guide = self._validate_file_path(guide) if guide else None
            if guide_offset is not None:
                song.guide_offset = self._validate_float(guide_offset, -3600.0, 3600.0)

    def update_volume(self, metronome: Optional[float] = None, audio: Optional[float] = None,
                      voice: Optional[float] = None) -> None:
        """Update volume settings with validation."""
        if metronome is not None:
            self.volume.metronome = self._validate_float(metronome, 0.0, 1.0)
        if audio is not None:
            self.volume.audio = self._validate_float(audio, 0.0, 1.0)
        if voice is not None:
            self.volume.voice = self._validate_float(voice, 0.0, 1.0)

    def get_used_dmx_channels(self) -> Dict[str, List[int]]:
        """Get mapping of fixture names to their DMX channels."""
        used_channels = {}

        type_counts: Dict[str, int] = {}
        for fixture in self.fixtures:
            type_counts[fixture.type] = type_counts.get(fixture.type, 0) + 1
            fixture_name = f"{fixture.type.capitalize()} {type_counts[fixture.type]}"
            channel_count = fixture_channel_count(fixture.type, fixture.mode)
            channels = list(range(fixture.dmx_start, fixture.dmx_start + channel_count))
            if channels:
                used_channels[fixture_name] = channels

        return used_channels


def shipped_fixtures() -> List[FixtureInstance]:
    """The default fixture patch, parsed from the shipped, read-only config.json
    (the seed, before any userprefs override). Single source of truth for the
    default patch: the software controller uses it when built without an explicit
    patch (standalone / tests), so its default matches the live app instead of
    duplicating it. Returns [] if the shipped config is missing or unparseable."""
    try:
        with open(shipped_config_path(), "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    return Config._parse_fixtures(data.get("fixtures", []))
