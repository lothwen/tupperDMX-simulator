"""
Characterization test: Config load/save and the two-file split.

Covers first-launch creation of userprefs.json, full round-trip fidelity of the
user-editable sections, that save() writes ONLY userprefs (never the shipped
read-only config.json), validation/clamping of out-of-range values, graceful
fallback on a corrupted userprefs file, and auto-registration of the LED-bar
channel-control notes (1-7) in the MIDI note map.

Paths are redirected to a temp dir (Config.load/save call the module-level
userprefs_path()/shipped_config_path() by name, so monkeypatching those on the
config module isolates the test from the real userprefs.json).

Run with:
    uv run python tests/test_config_roundtrip.py
"""

import sys
import os
import json
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import config
from core.config import Config, FixtureInstance


@pytest.fixture(autouse=True)
def _restore_config_paths():
    """Restore the module-level path helpers after each test.

    _patch_paths() rebinds config.shipped_config_path / userprefs_path to point
    at a per-test TemporaryDirectory; without this, the rebinding leaks into
    later test files that read the real shipped config (e.g. via
    config.shipped_fixtures())."""
    orig_shipped, orig_prefs = config.shipped_config_path, config.userprefs_path
    yield
    config.shipped_config_path, config.userprefs_path = orig_shipped, orig_prefs


SHIPPED = {
    "fixtures": [
        {"type": "ledbar", "dmx_start": 1, "bars": [{"x": 10, "y": 20}]},
        {"type": "blinder", "dmx_start": 33, "x": 100, "y": 200},
    ],
}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _patch_paths(tmp):
    """Redirect shipped + userprefs paths into the temp dir."""
    shipped = Path(tmp) / "config.json"
    prefs = Path(tmp) / "userprefs.json"
    config.shipped_config_path = lambda: str(shipped)
    config.userprefs_path = lambda: prefs
    return shipped, prefs


def test_first_launch_creates_userprefs():
    print("test_first_launch_creates_userprefs")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        _assert(not prefs.exists(), "no userprefs before first launch")
        cfg = Config.load()
        _assert(prefs.exists(), "first launch creates userprefs.json")
        _assert(cfg.artnet.ip == "127.0.0.1", "default Art-Net ip")
        _assert(cfg.volume.audio == 0.5, "default audio volume")
        _assert(cfg.fixtures[1].type == "blinder", "shipped fixture patch was applied")


def test_userprefs_roundtrip():
    print("test_userprefs_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))

        cfg = Config.load()
        cfg.artnet.ip = "10.0.0.9"
        cfg.artnet.port = 6460
        cfg.artnet.universe = 5
        cfg.volume.audio = 0.8
        cfg.volume.metronome = 0.2
        cfg.logging.level = "DEBUG"
        cfg.window.width = 1600
        cfg.window.height = 900
        cfg.window.sidebar_width = 320
        cfg.io.midi_to_port = True
        cfg.io.midi_port = "Arduino Leonardo"
        cfg.io.dmx_to_artnet = True
        cfg.io.stage_source = "usb"
        cfg.io.stage_usb_device = "FTDI:FT232R:A1B2C3"
        _assert(cfg.save() is True, "save() reported success")

        cfg2 = Config.load()
        _assert(cfg2.artnet.ip == "10.0.0.9", "artnet ip round-tripped")
        _assert(cfg2.artnet.port == 6460, "artnet port round-tripped")
        _assert(cfg2.artnet.universe == 5, "artnet universe round-tripped")
        _assert(abs(cfg2.volume.audio - 0.8) < 1e-9, "audio volume round-tripped")
        _assert(abs(cfg2.volume.metronome - 0.2) < 1e-9, "metronome volume round-tripped")
        _assert(cfg2.logging.level == "DEBUG", "log level round-tripped")
        _assert(cfg2.window.width == 1600 and cfg2.window.height == 900,
                "window size round-tripped")
        _assert(cfg2.window.sidebar_width == 320, "sidebar width round-tripped")
        _assert(cfg2.io.midi_to_port and cfg2.io.midi_port == "Arduino Leonardo",
                "io MIDI routing round-tripped")
        _assert(cfg2.io.dmx_to_artnet, "io Art-Net output round-tripped")
        _assert(cfg2.io.stage_source == "usb"
                and cfg2.io.stage_usb_device == "FTDI:FT232R:A1B2C3",
                "io stage input round-tripped")

        # The playlist belongs to the show manifest now.
        data = json.loads(prefs.read_text())
        _assert("playlist" not in data, "userprefs no longer carries the playlist")


def test_io_migration_from_legacy_keys():
    """Pre-io userprefs (last_dmx_source/last_midi_port + localhost convention)
    map onto an explicit IOConfig; the next save drops the legacy keys."""
    print("test_io_migration_from_legacy_keys")
    cases = [
        # (legacy prefs, expectation description, checker)
        ({"last_midi_port": "Software emulated", "last_dmx_source": "Art-Net"},
         "sentinel + localhost Art-Net -> all-software defaults",
         lambda io: (io.midi_to_controller and not io.midi_to_port
                     and io.controller_enabled and io.dmx_to_stage
                     and io.stage_source == "internal")),
        ({"last_midi_port": "Arduino Leonardo", "last_dmx_source": "Art-Net"},
         "real port -> external MIDI only",
         lambda io: (not io.midi_to_controller and io.midi_to_port
                     and io.midi_port == "Arduino Leonardo")),
        ({"last_dmx_source": "Art-Net",
          "artnet": {"ip": "192.168.1.50", "port": 6454, "universe": 0}},
         "Art-Net + remote ip -> controller off, stage listens",
         lambda io: (not io.controller_enabled and not io.dmx_to_stage
                     and io.stage_source == "artnet")),
        ({"last_dmx_source": "FTDI:FT232R:A1B2C3"},
         "FTDI source -> controller off, stage reads USB",
         lambda io: (not io.controller_enabled and io.stage_source == "usb"
                     and io.stage_usb_device == "FTDI:FT232R:A1B2C3")),
        ({"last_midi_port": "Arduino Leonardo",
          "io": {"midi_to_controller": True, "midi_to_port": False}},
         "an io section wins over legacy keys",
         lambda io: io.midi_to_controller and not io.midi_to_port),
    ]
    for prefs_data, desc, check in cases:
        with tempfile.TemporaryDirectory() as tmp:
            shipped, prefs = _patch_paths(tmp)
            shipped.write_text(json.dumps(SHIPPED))
            prefs.write_text(json.dumps(prefs_data))
            cfg = Config.load()
            _assert(check(cfg.io), desc)

            cfg.save()
            data = json.loads(prefs.read_text())
            _assert("io" in data, "post-save userprefs carries the io section")
            _assert("last_dmx_source" not in data and "last_midi_port" not in data,
                    "post-save userprefs drops the legacy keys")


def test_io_invalid_stage_source_falls_back():
    print("test_io_invalid_stage_source_falls_back")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        prefs.write_text(json.dumps({"io": {"stage_source": "bogus"}}))
        cfg = Config.load()
        _assert(cfg.io.stage_source == "internal",
                "invalid stage_source falls back to internal")


def test_save_writes_only_userprefs():
    print("test_save_writes_only_userprefs")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        shipped_before = shipped.read_text()

        cfg = Config.load()
        cfg.artnet.port = 7000
        cfg.save()

        _assert(shipped.read_text() == shipped_before,
                "save() leaves the shipped config.json untouched")
        data = json.loads(prefs.read_text())
        _assert("fixtures" not in data and "midi_note_map" not in data
                and "playlist" not in data,
                "userprefs.json carries no shipped/show-owned sections")
        _assert("artnet" in data and "volume" in data and "logging" in data,
                "userprefs.json carries the user-editable sections")


def test_validation_clamps_out_of_range():
    print("test_validation_clamps_out_of_range")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        prefs.write_text(json.dumps({
            "artnet": {"ip": "999.1.1.1", "port": 99999, "universe": -5},
            "volume": {"audio": 5.0, "metronome": -1.0},
            "logging": {"level": "BOGUS"},
            "window": {"width": 10, "height": 999999, "sidebar_width": 9999},
        }))
        cfg = Config.load()
        _assert(cfg.artnet.ip == "127.0.0.1", "invalid IP falls back to default")
        _assert(cfg.artnet.port == 65535, "port clamped to max")
        _assert(cfg.artnet.universe == 0, "universe clamped to min")
        _assert(cfg.volume.audio == 1.0, "audio volume clamped to 1.0")
        _assert(cfg.volume.metronome == 0.0, "metronome volume clamped to 0.0")
        _assert(cfg.logging.level == "INFO", "invalid log level falls back to INFO")
        _assert(cfg.window.width == 400, "window width clamped to min")
        _assert(cfg.window.height == 3000, "window height clamped to max")
        _assert(cfg.window.sidebar_width == 1200, "sidebar width clamped to max")


def test_corrupted_userprefs_falls_back():
    print("test_corrupted_userprefs_falls_back")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        prefs.write_text("{ this is not valid json ")
        cfg = Config.load()  # must not raise
        _assert(cfg.artnet.ip == "127.0.0.1", "corrupted prefs -> default artnet")
        _assert(cfg.volume.audio == 0.5, "corrupted prefs -> default volume")


def test_fixtures_parse_with_type():
    print("test_fixtures_parse_with_type")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        cfg = Config.load()
        _assert(len(cfg.fixtures) == 2, "two fixtures parsed from the flat list")
        led, blinder = cfg.fixtures
        _assert(led.type == "ledbar" and led.dmx_start == 1,
                "ledbar instance carries type + dmx_start")
        _assert(len(led.bars) == 1 and led.bars[0].x == 10,
                "ledbar bar positions parsed")
        _assert(blinder.type == "blinder" and blinder.dmx_start == 33,
                "blinder instance carries type + dmx_start")
        _assert(blinder.x == 100 and blinder.y == 200,
                "blinder position parsed")
        # Channel ranges derive from the type (ledbar=7, blinder=8).
        used = cfg.get_used_dmx_channels()
        _assert(used.get("Ledbar 1") == list(range(1, 8)),
                "ledbar uses 7 channels from dmx_start")
        _assert(used.get("Blinder 1") == list(range(33, 41)),
                "blinder uses 8 channels from dmx_start")


def test_legacy_prefs_sections_parse_but_are_not_saved():
    """Pre-show userprefs still carry playlist/fixtures. They must PARSE (the
    one-time show migration adopts them) but the next save() drops them —
    show.json owns them now."""
    print("test_legacy_prefs_sections_parse_but_are_not_saved")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        audio = Path(tmp) / "cancion.mp3"
        audio.write_bytes(b"x")
        prefs.write_text(json.dumps({
            "playlist": {
                "songs": [{"audio": str(audio), "offset": 1.5,
                           "id": "abc-123", "title": "El Acantilado",
                           "key": "Am", "precount": False,
                           "cache": {"duration": 245.2, "bpm": 142}}],
                "current_song_index": 0,
            },
            "fixtures": [
                {"type": "blinder", "dmx_start": 65, "name": "Front Left",
                 "channel": 5, "x": 5, "y": 6},
            ],
        }))

        cfg = Config.load()
        song = cfg.playlist.songs[0]
        _assert(song.audio == str(audio) and abs(song.offset - 1.5) < 1e-9,
                "legacy playlist song parsed")
        _assert(song.id == "abc-123" and song.title == "El Acantilado"
                and song.key == "Am" and song.precount is False
                and song.cache.get("bpm") == 142,
                "legacy song metadata parsed (migration source)")
        _assert(len(cfg.fixtures) == 1 and cfg.fixtures[0].name == "Front Left"
                and cfg.fixtures[0].channel == 5,
                "legacy prefs fixtures override the shipped seed for parsing")

        cfg.save()
        data = json.loads(prefs.read_text())
        _assert("playlist" not in data and "fixtures" not in data,
                "save() drops the legacy show-owned sections")


def test_unknown_fixture_type_skipped():
    print("test_unknown_fixture_type_skipped")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps({
            "fixtures": [
                {"type": "bogus", "dmx_start": 5},
                {"type": "blinder", "dmx_start": 33, "x": 1, "y": 2},
            ],
            "midi_note_map": {},
        }))
        cfg = Config.load()
        _assert(len(cfg.fixtures) == 1, "unknown fixture type is skipped")
        _assert(cfg.fixtures[0].type == "blinder", "valid fixture still parsed")


def test_channel_auto_assigned_in_list_order():
    print("test_channel_auto_assigned_in_list_order")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        # SHIPPED entries omit "channel" (legacy patch).
        shipped.write_text(json.dumps(SHIPPED))
        cfg = Config.load()
        _assert([f.channel for f in cfg.fixtures] == [0, 1],
                "missing channels are assigned 0,1,... in list order (back-compat)")


def test_explicit_and_missing_channels_dont_collide():
    print("test_explicit_and_missing_channels_dont_collide")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        # First fixture pins channel 0 explicitly; the others must skip it.
        shipped.write_text(json.dumps({"fixtures": [
            {"type": "blinder", "dmx_start": 33, "channel": 0},
            {"type": "ledbar", "dmx_start": 1},
            {"type": "blinder", "dmx_start": 65},
        ]}))
        cfg = Config.load()
        chans = [f.channel for f in cfg.fixtures]
        _assert(chans == [0, 1, 2], f"missing channels fill free slots (got {chans})")
        _assert(len(set(chans)) == 3, "no duplicate channels assigned")


def test_add_song_assigns_stable_id():
    print("test_add_song_assigns_stable_id")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        audio = Path(tmp) / "song.mp3"
        audio.write_bytes(b"x")

        cfg = Config.load()
        cfg.add_song(audio=str(audio), offset=0.5)
        song = cfg.playlist.songs[0]
        _assert(bool(song.id), "add_song assigns a stable uuid")
        _assert(song.title == "" and song.key == ""
                and song.precount is True and song.cache == {},
                "other song metadata defaults are innocuous")


def test_update_current_song_guide():
    print("test_update_current_song_guide")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        audio = Path(tmp) / "backing.mp3"
        audio.write_bytes(b"x")
        guide = Path(tmp) / "mezcla.mp3"
        guide.write_bytes(b"x")

        cfg = Config.load()
        cfg.add_song(audio=str(audio))
        song = cfg.playlist.songs[0]
        _assert(song.guide is None and song.guide_offset == 0.0,
                "new songs default to no guide")

        cfg.update_current_song(guide=str(guide), guide_offset=0.75)
        _assert(song.guide == str(guide), "update_current_song sets the guide")
        _assert(abs(song.guide_offset - 0.75) < 1e-9,
                "update_current_song sets the guide offset")

        cfg.update_current_song(guide_offset=99999.0)
        _assert(song.guide_offset == 3600.0, "guide offset is clamped")
        _assert(song.guide == str(guide), "omitted guide kwarg leaves it alone")

        cfg.update_current_song(guide="")
        _assert(song.guide is None, "empty-string guide clears it")


def test_show_pointers_roundtrip():
    print("test_show_pointers_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))

        cfg = Config.load()
        _assert(cfg.last_show_path is None and cfg.recent_shows == [],
                "show pointers default to empty")
        cfg.last_show_path = "/shows/Gira.show"
        cfg.recent_shows = ["/shows/Gira.show", "/shows/Vieja.show"]
        cfg.save()

        cfg2 = Config.load()
        _assert(cfg2.last_show_path == "/shows/Gira.show", "last_show_path round-tripped")
        _assert(cfg2.recent_shows == ["/shows/Gira.show", "/shows/Vieja.show"],
                "recent_shows round-tripped")

        # Malformed entries are dropped, not crashed on.
        prefs.write_text(json.dumps({"last_show_path": 42, "recent_shows": ["/a", 7, ""]}))
        cfg3 = Config.load()
        _assert(cfg3.last_show_path is None, "non-string last_show_path becomes None")
        _assert(cfg3.recent_shows == ["/a"], "non-string recent entries dropped")


def test_save_is_atomic():
    """save() writes via temp file + os.replace: no .tmp leftovers and the
    previous userprefs.json survives a failed serialization."""
    print("test_save_is_atomic")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))

        cfg = Config.load()
        cfg.save()
        leftovers = [p for p in prefs.parent.iterdir() if p.suffix == ".tmp"]
        _assert(leftovers == [], "no temp files left next to userprefs.json")

        before = prefs.read_text()
        cfg.recent_shows = [object()]  # unserializable -> json.dump raises
        _assert(cfg.save() is False, "failed save reports False")
        _assert(prefs.read_text() == before, "failed save leaves the old file intact")
        leftovers = [p for p in prefs.parent.iterdir() if p.suffix == ".tmp"]
        _assert(leftovers == [], "failed save cleans up its temp file")


def test_audio_config_roundtrip():
    print("test_audio_config_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))

        cfg = Config.load()
        _assert(cfg.audio.routing == "stereo", "default routing is stereo")
        _assert(cfg.audio.count_in_bars == 2, "default count-in is 2 bars")
        _assert(cfg.audio.count_in_voice is True, "spoken count-in defaults on")
        _assert(cfg.audio.click_accent is True, "the click accents beat 1 by default")
        _assert(cfg.audio.output_device == "", "default output device is system default")
        _assert(abs(cfg.volume.voice - 0.8) < 1e-9, "default voice volume")

        cfg.audio.output_device = "alsa_output.pci-0000"
        cfg.audio.routing = "split_backing_left"
        cfg.audio.count_in_bars = 4
        cfg.audio.count_in_voice = False
        cfg.audio.click_accent = False
        cfg.audio.tts_voice = "en_US-lessac-medium"
        cfg.volume.voice = 0.4
        cfg.save()

        cfg2 = Config.load()
        _assert(cfg2.audio.output_device == "alsa_output.pci-0000",
                "output device round-tripped")
        _assert(cfg2.audio.routing == "split_backing_left", "routing round-tripped")
        _assert(cfg2.audio.count_in_bars == 4, "count-in bars round-tripped")
        _assert(cfg2.audio.count_in_voice is False, "count-in voice round-tripped")
        _assert(cfg2.audio.click_accent is False, "click accent round-tripped")
        _assert(cfg2.audio.tts_voice == "en_US-lessac-medium", "tts voice round-tripped")
        _assert(abs(cfg2.volume.voice - 0.4) < 1e-9, "voice volume round-tripped")


def test_audio_config_validation():
    print("test_audio_config_validation")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))
        prefs.write_text(json.dumps({
            "audio": {"routing": "bogus", "count_in_bars": 99, "tts_voice": ""},
            "volume": {"voice": 7.0},
        }))
        cfg = Config.load()
        _assert(cfg.audio.routing == "stereo", "invalid routing falls back to stereo")
        _assert(cfg.audio.count_in_bars == 8, "count-in bars clamped to max")
        _assert(cfg.audio.tts_voice == "es_ES-davefx-medium",
                "empty tts voice falls back to the default")
        _assert(cfg.volume.voice == 1.0, "voice volume clamped to 1.0")


def test_next_free_channel():
    print("test_next_free_channel")
    cfg = Config()
    cfg.fixtures = [
        FixtureInstance(type="ledbar", dmx_start=1, channel=0),
        FixtureInstance(type="blinder", dmx_start=33, channel=2),
    ]
    _assert(cfg.next_free_channel() == 1, "returns the lowest free channel (the gap)")
    cfg.fixtures.append(FixtureInstance(type="blinder", dmx_start=65, channel=1))
    _assert(cfg.next_free_channel() == 3, "skips all used channels")


def test_stage_render_prefs_roundtrip():
    print("test_stage_render_prefs_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))

        cfg = Config.load()
        _assert(cfg.window.stage_gpu is False,
                "the GPU stage is off until asked for")
        _assert(abs(cfg.window.stage_exposure - 1.0) < 1e-9,
                "default exposure is 1.0")

        cfg.window.stage_gpu = True
        cfg.window.stage_exposure = 2.5
        cfg.save()

        cfg2 = Config.load()
        _assert(cfg2.window.stage_gpu is True, "stage_gpu survives a save/load")
        _assert(abs(cfg2.window.stage_exposure - 2.5) < 1e-9,
                "and so does the exposure")

        # Out of range on either side is clamped, not obeyed.
        prefs.write_text(json.dumps({"window": {"stage_exposure": 99.0}}))
        _assert(Config.load().window.stage_exposure <= 4.0,
                "an absurd exposure clamps to the top of the range")


def test_volumetric_fog_prefs_roundtrip():
    print("test_volumetric_fog_prefs_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))

        cfg = Config.load()
        _assert(cfg.window.fog.enabled is False,
                "volumetric fog is off until asked for, like the GPU stage")
        _assert(cfg.window.fog.march_steps == 24,
                "and the shipped step count targets integrated graphics")

        _assert(cfg.window.fog.texture > 0.0,
                "and the air is ragged out of the box — a perfectly smooth "
                "medium integrates to a translucent sheet over the picture, "
                "which is the one look this feature exists to avoid")

        cfg.window.fog.enabled = True
        cfg.window.fog.anisotropy = -0.4
        cfg.window.fog.color = "#112233"
        cfg.window.fog.march_steps = 48
        cfg.window.fog.resolution = 0.25
        cfg.window.fog.texture = 0.4
        cfg.window.fog.texture_scale = 500.0
        cfg.save()

        again = Config.load().window.fog
        _assert(again.enabled is True, "the whole block survives a save/load")
        _assert(abs(again.anisotropy + 0.4) < 1e-9, "including a backward g")
        _assert(again.color == "#112233", "and the scattering colour")
        _assert(again.march_steps == 48 and abs(again.resolution - 0.25) < 1e-9,
                "and the quality settings")
        _assert(abs(again.texture - 0.4) < 1e-9
                and abs(again.texture_scale - 500.0) < 1e-9,
                "and how ragged the air is, and at what size")

        # Nothing reaching a shader may be out of range: a march of 0 steps
        # draws nothing and a g of exactly 1 is a division by zero.
        prefs.write_text(json.dumps({"window": {"fog": {
            "march_steps": 0, "anisotropy": 1.0, "resolution": 8.0,
            "color": "rgb(1,2,3)", "max_distance": -50, "texture": 4.0,
        }}}))
        fog = Config.load().window.fog
        _assert(fog.march_steps >= 4, f"steps clamp up ({fog.march_steps})")
        _assert(fog.texture <= 3.0,
                f"raggedness has a ceiling — past it the noise has stopped "
                f"getting coarser and gone two-tone ({fog.texture})")
        _assert(fog.texture > 1.0,
                f"but the ceiling is well past 1: clamping the thin side of "
                f"the noise against empty air is what separates billows into "
                f"banks, so it is the exaggerated end of the control rather "
                f"than the broken end ({fog.texture})")
        _assert(abs(fog.anisotropy) < 1.0,
                f"g stops short of the singularity ({fog.anisotropy})")
        _assert(fog.resolution <= 1.0,
                f"fog is never marched above screen resolution ({fog.resolution})")
        _assert(fog.color == "#dfe4ee",
                f"an unparseable colour falls back rather than reaching GLSL "
                f"({fog.color})")
        _assert(fog.max_distance > 0.0, "and the slab has depth")

        # A prefs file written before this feature existed must still load.
        prefs.write_text(json.dumps({"window": {"stage_gpu": True}}))
        older = Config.load().window
        _assert(older.stage_gpu is True and older.fog.enabled is False,
                "prefs from before volumetric fog load with it simply off")


def test_the_launcher_reads_the_stage_gpu_pref():
    """The launcher decides whether to ask Qt for a desktop-GL surface, and it
    has to do that BEFORE QApplication exists — so it reads userprefs directly
    rather than through Config.

    This shipped broken once: it imported `userprefs_path` from the wrong
    module, the ImportError was swallowed by a bare `except`, and the answer
    was always False. The stage then came up on an OpenGL ES context and the
    light layer refused to run, with nothing to connect the two.
    """
    print("test_the_launcher_reads_the_stage_gpu_pref")
    import main

    with tempfile.TemporaryDirectory() as tmp:
        shipped, prefs = _patch_paths(tmp)
        shipped.write_text(json.dumps(SHIPPED))

        prefs.write_text(json.dumps({"window": {"stage_gpu": True}}))
        _assert(main._stage_gpu_requested() is True,
                "the launcher sees the preference when it is on")

        prefs.write_text(json.dumps({"window": {"stage_gpu": False}}))
        _assert(main._stage_gpu_requested() is False, "and when it is off")

        prefs.write_text("{ not json at all")
        _assert(main._stage_gpu_requested() is False,
                "unreadable prefs mean the raster path, not a crash")

        prefs.unlink()
        _assert(main._stage_gpu_requested() is False,
                "and so does a first launch with no prefs at all")


def main():
    test_first_launch_creates_userprefs()
    test_userprefs_roundtrip()
    test_io_migration_from_legacy_keys()
    test_io_invalid_stage_source_falls_back()
    test_save_writes_only_userprefs()
    test_validation_clamps_out_of_range()
    test_corrupted_userprefs_falls_back()
    test_fixtures_parse_with_type()
    test_legacy_prefs_sections_parse_but_are_not_saved()
    test_unknown_fixture_type_skipped()
    test_channel_auto_assigned_in_list_order()
    test_explicit_and_missing_channels_dont_collide()
    test_add_song_assigns_stable_id()
    test_update_current_song_guide()
    test_show_pointers_roundtrip()
    test_save_is_atomic()
    test_audio_config_roundtrip()
    test_audio_config_validation()
    test_next_free_channel()
    test_stage_render_prefs_roundtrip()
    test_volumetric_fog_prefs_roundtrip()
    test_the_launcher_reads_the_stage_gpu_pref()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
