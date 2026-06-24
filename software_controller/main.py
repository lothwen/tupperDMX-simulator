from .artnet_sender import ArtNetSender

import time
import random
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace

from fixture_library import load_fixture_defs
from config import shipped_fixtures
from tupperlogger import get_logger


# Constants from the Arduino code
DEFAULT_BLEND_TIME = 300
DEFAULT_COLOR_CHANGE_TIME = 5000
DEFAULT_CHASE_TIME = 1000

# App constants
DMX_UPDATE_HZ = 20
LOOP_FREQUENCY_HZ = 40
UNIVERSE_SIZE = 96

# Color definitions
COLOR32_WHITE = (255, 255, 230)
COLOR32_ORANGE = (255, 165, 0)
COLOR32_CYAN = (0, 255, 255)
COLOR32_PINK = (255, 192, 203)

# HSL Constants
RAINBOW_HUE_12 = [0.0, 0.083, 0.166, 0.25, 0.333, 0.416, 0.5, 0.583, 0.666, 0.75, 0.833, 0.916]
DEFAULT_HSL_S = 1.0
DEFAULT_HSL_L = 0.5

PALETTE_MODES = ['COMPLEMENTARY', 'ANALOGOUS', 'TRIADIC', 'COLOR+WHITE', 'MONOCHROMATIC', 'SINGLE']

# The song-part names a fixture def's scenes/chases can be tagged with via
# "song_parts" (listed in MIDI-note order, notes 61-69). handle_note_on passes
# one of these names to pick_scene_from_song_part; anything else is ignored.
SONG_PARTS = ['INTRO', 'VERSE', 'CHORUS', 'BREAK', 'BRIDGE', 'INST', 'SOLO', 'OUTRO', 'END']


def hsl_to_rgb(h, s, l):
    def hue_to_rgb(p, q, t):
        if t < 0: t += 1
        if t > 1: t -= 1
        if t < 1/6: return p + (q - p) * 6 * t
        if t < 1/2: return q
        if t < 2/3: return p + (q - p) * (2/3 - t) * 6
        return p

    if s == 0:
        r, g, b = l, l, l
    else:
        q = l * (1 + s) if l < 0.5 else l + s - l * s
        p = 2 * l - q
        r = hue_to_rgb(p, q, h + 1/3)
        g = hue_to_rgb(p, q, h)
        b = hue_to_rgb(p, q, h - 1/3)

    return round(r * 255), round(g * 255), round(b * 255)


def generate_song_colors(mode, hue_index, FromDialog=False):
    """Build a palette for the given mode and hue index.

    When ``FromDialog`` is True the function returns None
    """
    if hue_index >= 12:
        if FromDialog:
            return None
        hue = random.randint(0, 360)
    else:
        hue = RAINBOW_HUE_12[hue_index % len(RAINBOW_HUE_12)] * 360

    if mode == 'COMPLEMENTARY':
        return [
            hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            hsl_to_rgb(((hue + 180) % 360) / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
        ]
    if mode == 'ANALOGOUS':
        return [
            hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            hsl_to_rgb(((hue - 30) % 360) / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            hsl_to_rgb(((hue + 30) % 360) / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
        ]
    if mode == 'TRIADIC':
        return [
            hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            hsl_to_rgb(((hue - 120) % 360) / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            hsl_to_rgb(((hue + 120) % 360) / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
        ]
    if mode == 'COLOR+WHITE':
        return [
            hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            COLOR32_WHITE,
        ]
    if mode == 'MONOCHROMATIC':
        return [
            hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L + 0.15),
            hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L + 0.30),
        ]
    if mode == 'SINGLE':
        return [
            hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
        ]
    return []


# ============================================================================
# DATA-DRIVEN FIXTURE MODEL (ported from the Arduino data-driven-fixtures branch)
#
# A FixtureType describes ONE kind of light: its channels (each with a semantic
# role + behaviour flags), plus its scene banks and chases. One generic engine
# drives every fixture in the patch from this data, so the per-type
# update_led_scene / update_blinder_scene / blackout_* duplication is gone.
#
# Channels, scene banks and chases all come from the fixture-library JSON
# (fixtures/*.json via fixture_library.load_fixture_defs); colors stay
# (r, g, b) tuples. There are no hardcoded fixture types here anymore — the
# patch is whatever config.fixtures (type name + DMX address) describes.
# ============================================================================

# Channel semantic roles. The engine uses these to address channels by meaning
# (which one carries brightness, the R/G/B color, etc.) instead of by position.
ROLE_GENERIC = 0
ROLE_BRIGHTNESS = 1
ROLE_RED = 2
ROLE_GREEN = 3
ROLE_BLUE = 4
ROLE_WHITE_WARM = 5
ROLE_WHITE_COOL = 6
ROLE_ANIMATION = 7
ROLE_SPEED = 8
ROLE_DIRECTION = 9
ROLE_STROBE = 10
ROLE_MODE = 11

# Per-channel behaviour flags (bitmask).
CH_BLEND = 0x01       # interpolate over blend_time (else snap instantly)
CH_KNOB_LEVEL = 0x02  # capped by the brightness_cap knob at scene load
CH_KNOB_SPEED = 0x04  # value from MIDI velocity / speed_cap knob / scene


@dataclass(frozen=True)
class ChannelDef:
    field: str   # attribute on the scene dataclass (also the universe order)
    role: int
    flags: int


@dataclass(frozen=True)
class SongPartEntry:
    """One scene or chase a fixture type runs for a given song part. Built from
    the "song_parts" tags on scenes/chases in the fixture def (see
    build_fixture_types). For chases bank is "" and index is the chase index;
    for scenes bank is the bank name and index is the scene index."""
    is_chase: bool
    bank: str
    index: int


@dataclass
class FixtureType:
    channels: list           # list[ChannelDef]
    banks: dict              # bank-name -> list[scene namespace] (channel fields)
    chases: list             # list[{'banks': [...], 'scenes': [...]}]
    # song-part name -> list[SongPartEntry]; what this type runs per song part.
    song_part_program: dict = field(default_factory=dict)

    @property
    def channel_count(self):
        return len(self.channels)


def _scene_from_dict(channels, scene):
    """Build a scene namespace from a raw scene dict, keyed by channel field.

    Non-channel metadata (e.g. "name") is dropped; missing channels default to
    0. Matches the attribute access (getattr(scene_obj, ch.field)) the engine
    uses in load_scene()."""
    return SimpleNamespace(**{ch.field: scene.get(ch.field, 0) for ch in channels})


def _zero_scene(channels):
    """A single all-zero scene whose attributes match the channel fields.

    Fixture types that ship no scene banks still need at least one bank so
    init_fixtures()/load_scene() stay valid. This inert default bank fills that
    role; such types get no MIDI scene-select until the per-type MIDI map
    (subtask 4) lands."""
    return SimpleNamespace(**{ch.field: 0 for ch in channels})


def build_fixture_types():
    """Fixture-type registry built entirely from the fixture library. Every
    fixtures/*.json definition becomes a FixtureType (channels + scene banks +
    chases); config.json / the patch reference a type by name and resolve here.

    A definition with no banks gets an inert all-zero default bank so the engine
    stays valid for it."""
    types = {}
    for name, fdef in load_fixture_defs().items():
        channels = [ChannelDef(c.field, c.role, c.flags) for c in fdef.channels]
        banks = {bank_name: [_scene_from_dict(channels, s) for s in scenes]
                 for bank_name, scenes in fdef.banks.items()}
        if not banks:
            banks = {"_default": [_zero_scene(channels)]}
        types[name] = FixtureType(channels=channels, banks=banks,
                                  chases=list(fdef.chases),
                                  song_part_program=_build_song_part_program(fdef))
    return types


def _build_song_part_program(fdef):
    """Index the "song_parts" tags on a fixture def's scenes and chases into a
    {part-name: [SongPartEntry]} map. Read from the raw fdef dicts because
    _scene_from_dict drops the tag from the runtime scene namespaces."""
    program = {}
    for bank_name, scenes in fdef.banks.items():
        for idx, scene in enumerate(scenes):
            for part in scene.get("song_parts", []):
                program.setdefault(part, []).append(SongPartEntry(False, bank_name, idx))
    for idx, chase in enumerate(fdef.chases):
        for part in chase.get("song_parts", []):
            program.setdefault(part, []).append(SongPartEntry(True, "", idx))
    return program


# (The UI stage renderer keeps its own name->render-class dispatch in
# ui/stage_controller.py; generic types render via fixtures.GenericFixture.)
FIXTURE_TYPES = build_fixture_types()


@dataclass
class Fixture:
    """One fixture in the patch: its type definition (channels / scene banks /
    chases) and DMX address, together with all its live runtime state. Merges
    the old static FixturePatch with the mutable per-fixture FixtureState.
    Palette fields are only meaningful for fixtures with R/G/B channels but are
    kept on every fixture for uniformity."""
    type: object = None      # FixtureType: channels, scene banks, chases
    dmx_base: int = 0        # 1-indexed DMX start address
    bank: object = 0
    scene: int = 0
    is_blackout: bool = True
    is_chasing: bool = False
    chase: int = 0
    chase_index: int = 0
    chase_change_time: int = DEFAULT_CHASE_TIME
    color: tuple = COLOR32_WHITE
    palette_ptr: object = None
    palette_index: int = 0
    is_color_cycling: bool = False
    is_palette_random: bool = False
    color_change_time: int = DEFAULT_COLOR_CHANGE_TIME
    blend_time: int = DEFAULT_BLEND_TIME
    blend_time_elapsed: float = 0
    last_chase_change: float = 0
    last_palette_change: float = 0
    song_part_triggered: bool = False
    # Derived once in init_fixtures():
    base: int = 0        # offset into present[]/target[] (= dmx_base - 1)
    blend_base: int = 0  # offset into blend_start[] for this fixture


class DMXController:
    def __init__(self, fixtures=None):
        self.logger = get_logger("software_controller.main")
        self.lock = threading.Lock()

        # DMX buffers (lean strategy from the branch):
        #   present     - full DMX output buffer (what we send over Art-Net)
        #   target      - per-channel blend target (snap channels write present)
        #   blend_start - compact start value, only for CH_BLEND channels
        self.present = [0] * UNIVERSE_SIZE
        self.target = [0] * UNIVERSE_SIZE
        self.blend_start = []  # sized in init_fixtures()

        # Build the fixture list from injected config fixtures (type + dmx_start),
        # or fall back to the default 3-fixture patch for standalone use / tests.
        self.init_fixtures(fixtures)
        self.num_fixtures = len(self.fixtures)
        # Conventional "lead" fixture (index 0): the automatic song-part program
        # leaves it un-flagged so a manual scene can grab it (see
        # pick_scene_from_song_part). Note ROUTING no longer uses fixed handles —
        # the MIDI channel selects the fixture in handle_note_on.
        self.led = 0

        self.song_tempo = 145 # TODO: hardcoded value, find a way to pass this with a midi note

        self.blackout_all_state = False
        self.brightness_cap = 255
        self.speed_cap = 255

        self._running = False
        self._thread = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def _resolve_patch(self, fixtures):
        """Map config fixture instances (type name + dmx_start) onto
        (FixtureType, dmx_base) pairs. Unknown types are skipped. When no
        fixtures are injected (standalone / tests), the shipped config.json patch
        is used, so the default matches the live app instead of duplicating it."""
        if not fixtures:
            fixtures = shipped_fixtures()

        patch = []
        for inst in fixtures:
            ftype = FIXTURE_TYPES.get(getattr(inst, "type", None))
            if ftype is None:
                self.logger.warning(f"Unknown fixture type '{getattr(inst, 'type', None)}', skipping")
                continue
            patch.append((ftype, inst.dmx_start, getattr(inst, "channel", -1)))
        return patch

    def init_fixtures(self, fixtures):
        """Build the fixture list from the resolved patch (injected config
        fixtures or the default fallback), deriving each fixture's universe
        offset and its slice of the compact blend buffer. ``_fx_by_channel``
        maps each fixture's stable MIDI channel to its list index, so note
        routing is independent of list/patch order."""
        self.fixtures = []
        self._fx_by_channel = {}
        blend_base = 0
        for ft, dmx_base, channel in self._resolve_patch(fixtures):
            s = Fixture(type=ft, dmx_base=dmx_base)
            s.bank = next(iter(ft.banks))  # first bank ('ANIMATIONS' / 'BLINDER_SCENES')
            s.base = dmx_base - 1
            s.blend_base = blend_base
            blend_base += sum(1 for ch in ft.channels if ch.flags & CH_BLEND)
            # Fall back to the list index for patches built without explicit
            # channels (e.g. hand-constructed test fixtures) — matches the old
            # channel-equals-position behaviour.
            ch = channel if channel >= 0 else len(self.fixtures)
            self._fx_by_channel[ch] = len(self.fixtures)
            self.fixtures.append(s)
        self.blend_start = [0] * blend_base

    # ------------------------------------------------------------------
    # Channel accessors (address channels by role/meaning)
    # ------------------------------------------------------------------
    def channel_index_by_role(self, fx, role):
        for i, ch in enumerate(self.fixtures[fx].type.channels):
            if ch.role == role:
                return i
        return -1

    def set_channel_target(self, fx, role, value):
        i = self.channel_index_by_role(fx, role)
        if i < 0:
            return
        ch = self.fixtures[fx].type.channels[i]
        if ch.flags & CH_BLEND:
            self.target[self.fixtures[fx].base + i] = value
        else:
            self.present[self.fixtures[fx].base + i] = value

    def reseed_all_blend_starts(self):
        """Snapshot the current present[] frame as the blend start for every
        blend channel. Mirrors the old global universe_past = present copy done
        on any scene/color load."""
        for fx in range(self.num_fixtures):
            s = self.fixtures[fx]
            base = s.base
            k = s.blend_base
            for i, ch in enumerate(s.type.channels):
                if ch.flags & CH_BLEND:
                    self.blend_start[k] = self.present[base + i]
                    k += 1

    # ------------------------------------------------------------------
    # Scene loading
    # ------------------------------------------------------------------
    def load_scene(self, fx, bank, scene, blend_time=None):
        s = self.fixtures[fx]
        if blend_time is not None:
            s.blend_time = blend_time

        # Turn the fixture off if this exact scene is already running (toggle).
        if bank == s.bank and scene == s.scene and not s.is_blackout and not s.is_chasing:
            self.blackout_fixture(fx)
            return

        ft = s.type
        scene_obj = ft.banks[bank][scene]

        # Palette substitution: if the fixture has R/G/B and the scene sets all
        # three to the sentinel 255, the live palette color is used instead.
        ri = self.channel_index_by_role(fx, ROLE_RED)
        gi = self.channel_index_by_role(fx, ROLE_GREEN)
        bi = self.channel_index_by_role(fx, ROLE_BLUE)
        has_rgb = ri >= 0 and gi >= 0 and bi >= 0
        palette_mode = False
        if has_rgb:
            palette_mode = (getattr(scene_obj, ft.channels[ri].field) == 255 and
                            getattr(scene_obj, ft.channels[gi].field) == 255 and
                            getattr(scene_obj, ft.channels[bi].field) == 255)

        for i, ch in enumerate(ft.channels):
            raw = getattr(scene_obj, ch.field)
            value = raw

            if ch.role in (ROLE_RED, ROLE_GREEN, ROLE_BLUE):
                if palette_mode:
                    value = (s.color[0] if ch.role == ROLE_RED
                             else s.color[1] if ch.role == ROLE_GREEN
                             else s.color[2])
            elif ch.flags & CH_KNOB_LEVEL:
                # Brightness capped by the brightness knob.
                if raw > self.brightness_cap:
                    value = self.brightness_cap
            elif ch.flags & CH_KNOB_SPEED:
                # Speed capped by the speed knob.
                if raw > self.speed_cap:
                    value = self.speed_cap              

            if ch.flags & CH_BLEND:
                self.target[s.base + i] = value
            else:
                self.present[s.base + i] = value

        # Literal RGB scene -> stop cycling so the timer stops overriding color
        # (the palette is kept so a later song-part can re-arm it).
        if has_rgb and not palette_mode:
            s.is_color_cycling = False

        s.bank = bank
        s.scene = scene
        s.is_blackout = False

        # Start blend transition.
        s.blend_time_elapsed = 0
        self.reseed_all_blend_starts()

        self.logger.debug(f"Scene loaded -> fx:{fx}, bank:{bank}, scene:{scene}")

    # ------------------------------------------------------------------
    # Chases
    # ------------------------------------------------------------------
    def load_chase(self, fx, chase):
        s = self.fixtures[fx]
        ft = s.type
        s.is_chasing = True
        s.chase = chase
        step = ft.chases[chase]
        self.load_scene(fx, step['banks'][0], step['scenes'][0])
        s.last_chase_change = time.time()  # arm the step timer

    def update_chase(self, fx, now):
        s = self.fixtures[fx]
        if not s.is_chasing:
            return
        if now - s.last_chase_change < s.chase_change_time / 1000.0:
            return

        chase = s.type.chases[s.chase]
        s.chase_index = (s.chase_index + 1) % len(chase['scenes'])
        self.load_scene(fx, chase['banks'][s.chase_index], chase['scenes'][s.chase_index])
        s.last_chase_change = now

    # ------------------------------------------------------------------
    # Blends
    # ------------------------------------------------------------------
    def update_blend(self, fx, dt):
        s = self.fixtures[fx]
        if s.blend_time_elapsed >= s.blend_time:
            return

        s.blend_time_elapsed += dt
        t = s.blend_time_elapsed / s.blend_time

        ft = s.type
        k = s.blend_base
        for i, ch in enumerate(ft.channels):
            if ch.flags & CH_BLEND:
                self.present[s.base + i] = self.lerp(self.blend_start[k], self.target[s.base + i], t)
                k += 1

    def lerp(self, start, end, t):
        if t < 0:
            t = 0
        if t > 1:
            t = 1
        return int(start + (end - start) * t)

    # ------------------------------------------------------------------
    # Color (LED palette system)
    # ------------------------------------------------------------------
    def set_fixture_color(self, fx, color):
        # Set the RGB channels of the fixture and start a blend transition.
        self.fixtures[fx].color = color
        self.set_channel_target(fx, ROLE_RED, color[0])
        self.set_channel_target(fx, ROLE_GREEN, color[1])
        self.set_channel_target(fx, ROLE_BLUE, color[2])
        self.fixtures[fx].blend_time_elapsed = 0
        self.reseed_all_blend_starts()

    def change_fixture_color(self, fx, color):
        # Solid manual color: stop cycling but keep the palette so a later
        # song-part can re-arm it.
        self.fixtures[fx].is_color_cycling = False
        self.set_fixture_color(fx, color)

    def change_fixture_color_palette(self, fx, palette, blend_time, 
            color_change_time, start_index=0):
        s = self.fixtures[fx]
        s.palette_ptr = palette
        s.palette_index = start_index
        s.blend_time = blend_time
        s.color_change_time = color_change_time
        s.is_color_cycling = True
        self.set_fixture_color(fx, palette[start_index])
        s.last_palette_change = time.time()

    # ------------------------------------------------------------------
    # Blackouts
    # ------------------------------------------------------------------
    def blackout_fixture(self, fx):
        self.logger.debug(f"Blackout fx:{fx}")
        s = self.fixtures[fx]
        ft = s.type
        for i, ch in enumerate(ft.channels):
            self.present[s.base + i] = 0
            if ch.flags & CH_BLEND:
                self.target[s.base + i] = 0
        s.is_blackout = True
        s.is_chasing = False
        s.song_part_triggered = False
        # Stop cycling but keep the palette so a later song-part can re-arm it.
        s.is_color_cycling = False

    def blackout_all(self):
        self.blackout_all_state = not self.blackout_all_state
        self.logger.info(f"Blackout All state: {'enabled' if self.blackout_all_state else 'disabled'}")

    def set_fixture_manual(self, fx):
        # Drop one fixture out of the automatic program (chase + palette cycling)
        # so the background loop stops stomping manually-set values. The palette is
        # kept (is_color_cycling just goes off) so a later song-part can re-arm it.
        s = self.fixtures[fx]
        s.is_chasing = False
        s.is_color_cycling = False
        s.song_part_triggered = False

    def take_manual_control(self, fx):
        # A fixture takes manual control (note 48 on its channel): it drops out of
        # the automatic program and holds its frame for the incoming manual scene,
        # while every other fixture the song-part program left on (e.g. blinders
        # from a BREAK) blacks out so the manual scene starts clean. Fixtures the
        # user triggered by hand are left running (they don't carry
        # song_part_triggered).
        self.set_fixture_manual(fx)
        self.fixtures[fx].is_blackout = False
        for other in range(self.num_fixtures):
            if other != fx and self.fixtures[other].song_part_triggered:
                self.blackout_fixture(other)

    # ------------------------------------------------------------------
    # Note-map helpers (a note is a universal verb; the channel picks the
    # fixture; each verb is resolved against that fixture's own def)
    # ------------------------------------------------------------------
    def set_channel_offset(self, fx, offset, value):
        """Set the offset-th DMX channel (1-based) of a fixture directly.
        Offsets past the fixture's channel count are ignored so a note can't
        bleed into the next fixture's address space."""
        s = self.fixtures[fx]
        if 1 <= offset <= s.type.channel_count:
            self.present[s.base + offset - 1] = value

    def bank_names(self, fx):
        """Ordered bank names for a fixture (its def's JSON / insertion order)."""
        return list(self.fixtures[fx].type.banks.keys())

    def select_bank(self, fx, bank_idx):
        """Make bank_idx the fixture's current bank (ignored if out of range)."""
        names = self.bank_names(fx)
        if 0 <= bank_idx < len(names):
            self.fixtures[fx].bank = names[bank_idx]

    def _note_load_scene(self, fx, bank, scene, velocity):
        """Load a scene in response to a note: blend time comes from velocity
        (x100 ms, min 1 so blend channels still reach their target), and the
        fixture drops out of the automatic program so the scene sticks over any
        running chase/cycle."""
        self.load_scene(fx, bank, scene, max(1, velocity * 100))
        self.set_fixture_manual(fx)

    def load_scene_in_current_bank(self, fx, scene_idx, velocity):
        s = self.fixtures[fx]
        scenes = s.type.banks.get(s.bank)
        if scenes and 0 <= scene_idx < len(scenes):
            self._note_load_scene(fx, s.bank, scene_idx, velocity)

    def load_scene_relative(self, fx, delta, velocity):
        """Step to the previous/next scene of the current bank (clamped)."""
        s = self.fixtures[fx]
        scenes = s.type.banks.get(s.bank)
        if not scenes:
            return
        scene_idx = s.scene + delta
        if 0 <= scene_idx < len(scenes):
            self._note_load_scene(fx, s.bank, scene_idx, velocity)

    def load_scene_packed(self, fx, velocity):
        """Load a bank+scene packed into one velocity (bank=vel//8, scene=vel%8).
        Velocity is consumed by the encoding, so the fixture's current blend_time
        is used (not a velocity-derived one)."""
        names = self.bank_names(fx)
        bank_idx, scene_idx = velocity // 8, velocity % 8
        if 0 <= bank_idx < len(names):
            scenes = self.fixtures[fx].type.banks[names[bank_idx]]
            if 0 <= scene_idx < len(scenes):
                self.load_scene(fx, names[bank_idx], scene_idx)
                self.set_fixture_manual(fx)

    # ------------------------------------------------------------------
    # MIDI input
    # ------------------------------------------------------------------
    def handle_note_on(self, channel, pitch, velocity):
        """Route one MIDI note to the patch. The note number is a universal
        *verb*, the MIDI channel selects *which* fixture (its stable channel id,
        looked up in the patch), and velocity carries the verb's parameter.
        Per-fixture verbs are
        resolved against that fixture's own data-driven def (channels / banks /
        chases), so the one layout works for any fixture type. See the layout
        table in the plan / ROADMAP subtask 4."""
        with self.lock:
            self.logger.debug(f"Note On: channel={channel}, pitch={pitch}, velocity={velocity}")

            # ---- Global verbs (channel-agnostic) ----
            if pitch == 51:                      # blackout all (toggle)
                self.blackout_all()
                return
            if 61 <= pitch <= 69:                # song-part markers INTRO..END
                self.pick_scene_from_song_part(SONG_PARTS[pitch - 61], velocity)
                return

            # ---- Per-fixture verbs: the channel selects the fixture ----
            # The MIDI channel is the fixture's stable id, not its list index,
            # so resolve it through the patch map.
            fx = self._fx_by_channel.get(channel)
            if fx is None:
                return
            s = self.fixtures[fx]

            match pitch:
                case n if 1 <= n <= 23:          # set channel offset n = velocity*2
                    self.set_channel_offset(fx, n, velocity * 2)
                case n if 24 <= n <= 35:         # load scene (n-24) in current bank
                    self.load_scene_in_current_bank(fx, n - 24, velocity)
                case n if 36 <= n <= 47:         # select bank (n-36)
                    self.select_bank(fx, n - 36)
                case 48:                         # take manual control of the fixture
                    self.take_manual_control(fx)
                case 49:                         # drop to manual (keep current frame)
                    self.set_fixture_manual(fx)
                case 50:                         # blackout this fixture
                    self.blackout_fixture(fx)
                case 52:                         # blend time (ms)
                    s.blend_time = velocity * 100
                case 53:                         # color-change time (ms)
                    s.color_change_time = velocity * 100
                case 54:                         # chase-change time (ms)
                    s.chase_change_time = velocity * 100
                case 55:                         # palette random on
                    s.is_palette_random = True
                case 56:                         # palette random off
                    s.is_palette_random = False
                case 57:                         # previous scene in current bank
                    self.load_scene_relative(fx, -1, velocity)
                case 58:                         # next scene in current bank
                    self.load_scene_relative(fx, 1, velocity)
                case 59:                         # bank+scene packed in velocity
                    self.load_scene_packed(fx, velocity)
                case 60:                         # START: velocity encodes the palette
                    # hue_index (0-11: hue, 12+: random) and palette mode. Store
                    # the palette but leave cycling off until a song-part arms it.
                    hue_index = velocity // 6
                    palette_mode = velocity % 6
                    s.palette_ptr = generate_song_colors(
                        PALETTE_MODES[palette_mode], hue_index)
                    s.palette_index = 0
                case 70:                         # load chase (velocity = index)
                    # Runs via update_chase until a later verb/song-part
                    # reprograms the fixture. No set_fixture_manual here: it
                    # clears is_chasing and would stop the chase immediately.
                    if 0 <= velocity < len(s.type.chases):
                        self.load_chase(fx, velocity)
                case 72:                         # color macros
                    self.change_fixture_color(fx, COLOR32_WHITE)
                case 73:
                    self.change_fixture_color(fx, COLOR32_ORANGE)
                case 74:
                    self.change_fixture_color(fx, COLOR32_CYAN)
                case 75:
                    self.change_fixture_color(fx, COLOR32_PINK)
                case _:
                    pass  # 0, 70-71, 76-127: NOP / reserved

    def pick_scene_from_song_part(self, part, velocity):
        """Run the song-part program: for the requested part (a SONG_PARTS name),
        load whatever scene or chase each fixture type tagged for it (data-driven
        from the fixture defs' "song_parts" tags). Fixtures with nothing tagged
        for the part stay blacked out."""
        if part not in SONG_PARTS:
            return

        # Fixtures whose type carries a song-part program are owned by the
        # automatic show; leave anything else (e.g. a generic wash) untouched.
        managed = [fx for fx in range(self.num_fixtures)
                   if self.fixtures[fx].type.song_part_program]

        for fx in managed:
            self.blackout_fixture(fx)

        for fx in managed:
            entries = self.fixtures[fx].type.song_part_program.get(part)
            if not entries:
                continue
            entry = random.choice(entries)
            if entry.is_chase:
                self.load_chase(fx, entry.index)
            else:
                self.load_scene(fx, entry.bank, entry.index)

            if self.fixtures[fx].palette_ptr:
                self.change_fixture_color_palette(fx, self.fixtures[fx].palette_ptr,
                  self.beat_duration(self.song_tempo) * 1,
                  self.beat_duration(self.song_tempo) * 4)

        # Mark non-LED fixtures as owned by the automatic program so a later
        # manual LED scene clears them (see take_manual_control). Fixtures
        # left blacked out for this part get the flag too, but blacking out an
        # off fixture is a harmless no-op.
        for fx in managed:
            if fx != self.led:
                self.fixtures[fx].song_part_triggered = True

    def beat_duration(self, bpms):
        if bpms == 0:
            return 0
        return (1000 / bpms * 60)

    # ------------------------------------------------------------------
    # Lifecycle / output loop
    # ------------------------------------------------------------------
    def start_background(self):
        """Start the controller in a background thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.logger.info("Software Controller started in background")

    def stop(self):
        """Stop the background thread."""
        self._running = False
        if self._thread:
            self._thread.join()
            self._thread = None
        self.logger.info("Software Controller stopped")

    def _run_loop(self):
        # Initialize ArtNet Sender
        # We use default localhost:6454 for simulation
        dmx_sender = ArtNetSender(ip="127.0.0.1", port=6454, universe=0)

        last_dmx_update = time.time()

        while self._running:

            current_time = time.time()
            with self.lock:
                # Color palette timers (per fixture: each can run its own palette
                # at its own color_change_time).
                for fx in range(self.num_fixtures):
                    s = self.fixtures[fx]
                    if s.is_color_cycling and len(s.palette_ptr) > 1 and \
                       current_time - s.last_palette_change >= s.color_change_time / 1000.0:
                        i = random.randint(16, 48) if s.is_palette_random else 1
                        s.palette_index = (s.palette_index + i) % len(s.palette_ptr)
                        self.set_fixture_color(fx, s.palette_ptr[s.palette_index])
                        s.last_palette_change = current_time

                # Chase timers
                for fx in range(self.num_fixtures):
                    self.update_chase(fx, current_time)

            # DMX Loop
            if current_time - last_dmx_update >= 1.0 / DMX_UPDATE_HZ:
                data_to_send = None
                with self.lock:
                    dt = (current_time - last_dmx_update) * 1000
                    last_dmx_update = current_time

                    if self.blackout_all_state:
                        data_to_send = [0] * UNIVERSE_SIZE
                    else:
                        # Blend logic
                        for fx in range(self.num_fixtures):
                            self.update_blend(fx, dt)
                        data_to_send = self.present

                if data_to_send:
                    dmx_sender.send_dmx(data_to_send)

            time.sleep(1.0 / LOOP_FREQUENCY_HZ)

        dmx_sender.close()
