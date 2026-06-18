from .artnet_sender import ArtNetSender

from .dmx import led_banks, blinder_banks, led_chases, blinder_chases
import time
import random
import threading
from dataclasses import dataclass
from types import SimpleNamespace

from fixture_library import load_fixture_defs
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
# Scene/chase data still comes from dmx_config.json (via .dmx) keyed by bank
# name; colors stay (r, g, b) tuples. The Art-Net output layout is unchanged:
# LED at ch 0-6, Blinder1 at 32-39, Blinder2 at 64-71.
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


@dataclass
class FixtureType:
    channels: list           # list[ChannelDef]
    banks: dict              # bank-name -> list[scene dataclass] (from .dmx)
    chases: list             # list[{'banks': [...], 'scenes': [...]}]

    @property
    def channel_count(self):
        return len(self.channels)


@dataclass
class FixturePatch:
    type: FixtureType
    dmx_base: int            # 1-indexed DMX start address


# H807SA LED controller (7ch): brightness | blue | green | red | animation
#                              | speed | direction
H807SA_CHANNELS = [
    ChannelDef('brightness', ROLE_BRIGHTNESS, CH_BLEND | CH_KNOB_LEVEL),
    ChannelDef('blue',       ROLE_BLUE,       CH_BLEND),
    ChannelDef('green',      ROLE_GREEN,      CH_BLEND),
    ChannelDef('red',        ROLE_RED,        CH_BLEND),
    ChannelDef('animation',  ROLE_ANIMATION,  0),
    ChannelDef('speed',      ROLE_SPEED,      CH_KNOB_SPEED),
    ChannelDef('direction',  ROLE_DIRECTION,  0),
]

# Warm/cool white blinder (8ch): brightness | cool1 | cool2 | warm1 | warm2
#                                | strobe | mode | speed
BLINDER_CHANNELS = [
    ChannelDef('brightness', ROLE_BRIGHTNESS, CH_BLEND),  # not knob-capped
    ChannelDef('cool1',      ROLE_WHITE_COOL, CH_BLEND),
    ChannelDef('cool2',      ROLE_WHITE_COOL, CH_BLEND),
    ChannelDef('warm1',      ROLE_WHITE_WARM, CH_BLEND),
    ChannelDef('warm2',      ROLE_WHITE_WARM, CH_BLEND),
    ChannelDef('strobe',     ROLE_STROBE,     0),
    ChannelDef('mode',       ROLE_MODE,       0),
    ChannelDef('speed',      ROLE_SPEED,      0),
]

FIXTURE_H807SA = FixtureType(H807SA_CHANNELS, led_banks, led_chases)
FIXTURE_BLINDER = FixtureType(BLINDER_CHANNELS, blinder_banks, blinder_chases)


def _zero_scene(channels):
    """A single all-zero scene whose attributes match the channel fields.

    Library-defined (generic) fixture types ship no scene banks, but the engine
    needs at least one bank so init_fixtures()/load_scene() stay valid. This
    inert default bank fills that role; generic types get no MIDI scene-select
    until the per-type MIDI map (subtask 4) lands."""
    return SimpleNamespace(**{ch.field: 0 for ch in channels})


def build_fixture_types():
    """Fixture-type registry: the two built-in singletons plus every data-driven
    definition from the fixture library. config.json / the patch reference a type
    by name and resolve to a FixtureType here.

    The built-ins are kept as the original singleton objects — note routing
    (_resolve_fixture_handles) and the characterization tests rely on their
    identity — so library defs never override them."""
    types = {"ledbar": FIXTURE_H807SA, "blinder": FIXTURE_BLINDER}
    for name, fdef in load_fixture_defs().items():
        if name in types:
            continue
        channels = [ChannelDef(c.field, c.role, c.flags) for c in fdef.channels]
        types[name] = FixtureType(
            channels=channels,
            banks={"_default": [_zero_scene(channels)]},
            chases=[],
        )
    return types


# (The UI stage renderer keeps its own name->render-class dispatch in
# ui/stage_controller.py; generic types render via fixtures.GenericFixture.)
FIXTURE_TYPES = build_fixture_types()

# Default patch used when no config fixtures are injected (standalone / tests):
# the LED bar at DMX 1..7, Blinder1 at 33..40, Blinder2 at 65..72. The live app
# builds the patch from config.fixtures instead (see DMXController._build_patch).
DEFAULT_PATCH = [
    FixturePatch(FIXTURE_H807SA,  1),
    FixturePatch(FIXTURE_BLINDER, 33),
    FixturePatch(FIXTURE_BLINDER, 65),
]


@dataclass
class FixtureState:
    """Runtime state for one active fixture (replaces the old per-type status
    dicts). Palette fields are only meaningful for fixtures with R/G/B channels
    but are kept on every fixture for uniformity."""
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
    palette_size: int = 0
    is_palette_random: bool = False
    color_change_time: int = DEFAULT_COLOR_CHANGE_TIME
    blend_time: int = DEFAULT_BLEND_TIME
    blend_time_elapsed: float = 0
    last_chase_change: float = 0
    song_part_triggered: bool = False
    # Derived once in init_fixtures():
    base: int = 0        # offset into present[]/target[] (= dmx_base - 1)
    blend_base: int = 0  # offset into blend_start[] for this fixture


class DMXController:
    def __init__(self, fixtures=None):
        self.logger = get_logger("software_controller.main")
        self.lock = threading.Lock()

        # Build the patch from injected config fixtures (type + dmx_start), or
        # fall back to the default 3-fixture patch for standalone use / tests.
        self.patch = self._build_patch(fixtures)
        self.num_fixtures = len(self.patch)
        self._resolve_fixture_handles()

        self.song_tempo = 145 # TODO: hardcoded value, find a way to pass this with a midi note
        self.color_palette = []

        # DMX buffers (lean strategy from the branch):
        #   present     - full DMX output buffer (what we send over Art-Net)
        #   target      - per-channel blend target (snap channels write present)
        #   blend_start - compact start value, only for CH_BLEND channels
        self.present = [0] * UNIVERSE_SIZE
        self.target = [0] * UNIVERSE_SIZE
        self.blend_start = []  # sized in init_fixtures()

        self.blackout_all_state = False
        self.current_velocity = 127
        self.brightness_cap = 255
        self.speed_cap = 255

        self.last_palette_change = 0

        self.fixtures = []
        self.init_fixtures()

        self._running = False
        self._thread = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def _build_patch(self, fixtures):
        """Map config fixture instances (type name + dmx_start) onto the
        FixtureType registry. Unknown types are skipped; an empty/absent list
        falls back to the default patch."""
        if not fixtures:
            return list(DEFAULT_PATCH)

        patch = []
        for inst in fixtures:
            ftype = FIXTURE_TYPES.get(getattr(inst, "type", None))
            if ftype is None:
                self.logger.warning(f"Unknown fixture type '{getattr(inst, 'type', None)}', skipping")
                continue
            patch.append(FixturePatch(ftype, inst.dmx_start))
        return patch or list(DEFAULT_PATCH)

    def _resolve_fixture_handles(self):
        """Resolve the LED / blinder index handles the note router uses.

        For now the emulator keeps the single-LED + up-to-two-blinder semantics
        (handles derived from the patch instead of hardcoded 0/1/2); generalizing
        note->fixture routing is subtask 4."""
        self.led = next((i for i, p in enumerate(self.patch)
                         if p.type is FIXTURE_H807SA), 0)
        blinders = [i for i, p in enumerate(self.patch) if p.type is FIXTURE_BLINDER]
        self.blinder1 = blinders[0] if len(blinders) > 0 else self.led
        self.blinder2 = blinders[1] if len(blinders) > 1 else self.blinder1

    def init_fixtures(self):
        """Initialise per-fixture runtime state from the patch, deriving each
        fixture's universe offset and its slice of the compact blend buffer."""
        self.fixtures = []
        blend_base = 0
        for fx in range(self.num_fixtures):
            ft = self.patch[fx].type
            s = FixtureState()
            s.bank = next(iter(ft.banks))  # first bank ('ANIMATIONS' / 'BLINDER_SCENES')
            s.base = self.patch[fx].dmx_base - 1
            s.blend_base = blend_base
            blend_base += sum(1 for ch in ft.channels if ch.flags & CH_BLEND)
            self.fixtures.append(s)
        self.blend_start = [0] * blend_base

    # ------------------------------------------------------------------
    # Channel accessors (address channels by role/meaning)
    # ------------------------------------------------------------------
    def channel_index_by_role(self, fx, role):
        for i, ch in enumerate(self.patch[fx].type.channels):
            if ch.role == role:
                return i
        return -1

    def set_channel_instant(self, fx, role, value):
        i = self.channel_index_by_role(fx, role)
        if i >= 0:
            self.present[self.fixtures[fx].base + i] = value

    def set_channel_target(self, fx, role, value):
        i = self.channel_index_by_role(fx, role)
        if i < 0:
            return
        ch = self.patch[fx].type.channels[i]
        if ch.flags & CH_BLEND:
            self.target[self.fixtures[fx].base + i] = value
        else:
            self.present[self.fixtures[fx].base + i] = value

    def reseed_all_blend_starts(self):
        """Snapshot the current present[] frame as the blend start for every
        blend channel. Mirrors the old global universe_past = present copy done
        on any scene/color load."""
        for fx in range(self.num_fixtures):
            ft = self.patch[fx].type
            base = self.fixtures[fx].base
            k = self.fixtures[fx].blend_base
            for i, ch in enumerate(ft.channels):
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

        ft = self.patch[fx].type
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
                # 0 -> MIDI velocity / 1-254 -> knob speed / 255 -> scene value
                if self.speed_cap == 0:
                    value = self.current_velocity * 2
                elif self.speed_cap == 255:
                    value = raw
                else:
                    value = self.speed_cap

            if ch.flags & CH_BLEND:
                self.target[s.base + i] = value
            else:
                self.present[s.base + i] = value

        # Literal RGB scene -> drop the palette so the timer stops overriding color.
        if has_rgb and not palette_mode:
            s.palette_size = 0
            s.palette_ptr = None

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
        ft = self.patch[fx].type
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

        chase = self.patch[fx].type.chases[s.chase]
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

        ft = self.patch[fx].type
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

    def change_led_color(self, color):
        # Solid manual color: drop the palette so the timer stops cycling.
        self.fixtures[self.led].palette_size = 0
        self.fixtures[self.led].palette_ptr = None
        self.set_fixture_color(self.led, color)

    def change_led_color_palette(self, palette, index, size, blend_time, color_change_time):
        s = self.fixtures[self.led]
        s.palette_ptr = palette
        s.palette_index = index
        s.palette_size = size
        s.blend_time = blend_time
        s.color_change_time = color_change_time
        self.set_fixture_color(self.led, palette[index])
        self.last_palette_change = time.time()

    # ------------------------------------------------------------------
    # Blackouts
    # ------------------------------------------------------------------
    def blackout_fixture(self, fx):
        self.logger.debug(f"Blackout fx:{fx}")
        s = self.fixtures[fx]
        ft = self.patch[fx].type
        for i, ch in enumerate(ft.channels):
            self.present[s.base + i] = 0
            if ch.flags & CH_BLEND:
                self.target[s.base + i] = 0
        s.is_blackout = True
        s.is_chasing = False
        s.song_part_triggered = False
        # Drop the palette (no-op for fixtures without one).
        s.palette_size = 0
        s.palette_ptr = None

    def blackout_all(self):
        self.blackout_all_state = not self.blackout_all_state
        self.logger.info(f"Blackout All state: {'enabled' if self.blackout_all_state else 'disabled'}")

    def reset_led_blinder_status(self):
        # Drop out of chase/palette so the background loop stops stomping the
        # manual values, and clear blinders that the automatic song-part program
        # left on (e.g. from a BREAK) so a manual scene starts clean. Blinders
        # the user triggered by hand (notes 26-32) are left running.
        self.fixtures[self.led].is_chasing = False
        self.fixtures[self.led].is_blackout = False
        self.fixtures[self.led].palette_size = 0
        self.fixtures[self.led].palette_ptr = None
        if self.fixtures[self.blinder1].song_part_triggered:
            self.blackout_fixture(self.blinder1)
        if self.fixtures[self.blinder2].song_part_triggered:
            self.blackout_fixture(self.blinder2)

    # ------------------------------------------------------------------
    # MIDI input
    # ------------------------------------------------------------------
    def handle_note_on(self, channel, pitch, velocity):
        with self.lock:
            self.logger.debug(f"Note On: channel={channel}, pitch={pitch}, velocity={velocity}")
            self.current_velocity = velocity

            match pitch:
                case 0:  # C-1
                    pass # NoteOff, do nothing
                case 1:   # C#-1
                    self.set_channel_instant(self.led, ROLE_BRIGHTNESS, velocity * 2)
                case 2:   # D-1
                    self.set_channel_instant(self.led, ROLE_BLUE, velocity * 2)
                case 3:   # D#-1
                    self.set_channel_instant(self.led, ROLE_GREEN, velocity * 2)
                case 4:   # E-1
                    self.set_channel_instant(self.led, ROLE_RED, velocity * 2)
                case 5:   # F-1
                    # The animation note is sent once per manual scene (unlike
                    # brightness, which streams during fades), so we use it as
                    # the single trigger to take manual LED control. Assumes a
                    # manual scene always includes all 7 channel notes (1-7).
                    self.reset_led_blinder_status()
                    self.set_channel_instant(self.led, ROLE_ANIMATION, velocity * 2)
                case 6:   # F#-1
                    self.set_channel_instant(self.led, ROLE_SPEED, velocity * 2)
                case 7:   # G-1
                    self.set_channel_instant(self.led, ROLE_DIRECTION, velocity * 2)
                case 8:   # G#-1
                    self.fixtures[self.led].is_palette_random = True
                case 9:   # A-1
                    self.fixtures[self.led].is_palette_random = False
                case 10:  # A#-1
                    self.fixtures[self.led].blend_time = velocity * 100
                case 11:  # B-1
                    self.fixtures[self.led].color_change_time = velocity * 1000
                case 12:  # C0
                    self.load_scene(self.led, self.fixtures[self.led].bank, 0)
                case 13:  # C#0
                    self.load_scene(self.led, self.fixtures[self.led].bank, 1)
                case 14:  # D0
                    self.load_scene(self.led, self.fixtures[self.led].bank, 2)
                case 15:  # D#0
                    self.load_scene(self.led, self.fixtures[self.led].bank, 3)
                case 16:  # E0
                    self.load_scene(self.led, self.fixtures[self.led].bank, 4)
                case 17:  # F0
                    self.load_scene(self.led, self.fixtures[self.led].bank, 5)
                case 18:  # F#0
                    self.load_scene(self.led, self.fixtures[self.led].bank, 6)
                case 19:  # G0
                    self.load_scene(self.led, self.fixtures[self.led].bank, 7)
                case 20:  # G#0
                    self.change_led_color(COLOR32_WHITE)
                case 21:  # A0
                    self.change_led_color(COLOR32_ORANGE)
                case 22:  # A#0
                    self.change_led_color(COLOR32_CYAN)
                case 23:  # B0
                    self.change_led_color(COLOR32_PINK)
                case 24:  # C1
                    self.fixtures[self.led].bank = 'FILLS'
                case 25:  # C#1
                    self.fixtures[self.led].bank = 'TUBES'
                case 26:  # D1
                    self.load_scene(self.blinder1, 'BLINDER_SCENES', 0, velocity * 100)
                    self.load_scene(self.blinder2, 'BLINDER_SCENES', 0, velocity * 100)
                    self.fixtures[self.blinder1].song_part_triggered = self.fixtures[self.blinder2].song_part_triggered = False
                    self.fixtures[self.blinder1].is_chasing = self.fixtures[self.blinder2].is_chasing = False
                case 27:  # D#1
                    self.load_scene(self.blinder1, 'BLINDER_SCENES', 1, velocity * 100)
                    self.load_scene(self.blinder2, 'BLINDER_SCENES', 1, velocity * 100)
                    self.fixtures[self.blinder1].song_part_triggered = self.fixtures[self.blinder2].song_part_triggered = False
                    self.fixtures[self.blinder1].is_chasing = self.fixtures[self.blinder2].is_chasing = False
                case 28:  # E1
                    self.load_scene(self.blinder1, 'BLINDER_SCENES', 2, velocity * 100)
                    self.load_scene(self.blinder2, 'BLINDER_SCENES', 2, velocity * 100)
                    self.fixtures[self.blinder1].song_part_triggered = self.fixtures[self.blinder2].song_part_triggered = False
                    self.fixtures[self.blinder1].is_chasing = self.fixtures[self.blinder2].is_chasing = False
                case 29:  # F1
                    self.load_scene(self.blinder1, 'BLINDER_SCENES', 3, velocity * 100)
                    self.load_scene(self.blinder2, 'BLINDER_SCENES', 3, velocity * 100)
                    self.fixtures[self.blinder1].song_part_triggered = self.fixtures[self.blinder2].song_part_triggered = False
                    self.fixtures[self.blinder1].is_chasing = self.fixtures[self.blinder2].is_chasing = False
                case 30:  # F#1
                    self.load_scene(self.blinder1, 'BLINDER_SCENES', 4, velocity * 100)
                    self.load_scene(self.blinder2, 'BLINDER_SCENES', 4, velocity * 100)
                    self.fixtures[self.blinder1].song_part_triggered = self.fixtures[self.blinder2].song_part_triggered = False
                    self.fixtures[self.blinder1].is_chasing = self.fixtures[self.blinder2].is_chasing = False
                case 31:  # G1
                    self.load_scene(self.blinder1, 'BLINDER_SCENES', 1, velocity * 100)
                    self.fixtures[self.blinder1].song_part_triggered = False
                    self.fixtures[self.blinder1].is_chasing = False
                case 32:  # G#1
                    self.load_scene(self.blinder2, 'BLINDER_SCENES', 1, velocity * 100)
                    self.fixtures[self.blinder2].song_part_triggered = False
                    self.fixtures[self.blinder2].is_chasing = False
                case 38:  # D2 - Precount beats
                    self.load_scene(self.blinder1, 'BLINDER_SCENES', 0, 1)
                    self.load_scene(self.blinder2, 'BLINDER_SCENES', 0, 1)
                case 39:  # D#2 - Precount downbeats
                    self.load_scene(self.blinder1, 'BLINDER_SCENES', 1, 1)
                    self.load_scene(self.blinder2, 'BLINDER_SCENES', 1, 1)
                case 40:  # E2 - START
                    # velocity encodes 2 values: hue_index (0-11: hue index, 12: random hue) and palette mode
                    hue_index = velocity // 6
                    palette_mode = velocity % 6
                    self.color_palette = generate_song_colors(
                    PALETTE_MODES[palette_mode], hue_index)
                case 41:  # F2
                    self.pick_scene_from_song_part(1, velocity) # INTRO
                case 42:  # F#2
                    self.pick_scene_from_song_part(2, velocity) # VERSE
                case 43:  # G2
                    self.pick_scene_from_song_part(3, velocity) # CHORUS
                case 44:  # G#2
                    self.pick_scene_from_song_part(4, velocity) # BREAK
                case 45:  # A2
                    self.pick_scene_from_song_part(5, velocity) # BRIDGE
                case 46:  # A#2
                    self.pick_scene_from_song_part(6, velocity) # INST
                case 47:  # B2
                    self.pick_scene_from_song_part(7, velocity) # SOLO
                case 48:  # C3
                    self.pick_scene_from_song_part(8, velocity) # OUTRO
                case 49:  # C#3
                    self.pick_scene_from_song_part(9, velocity) # END
                case 50:  # D3
                    self.blackout_fixture(self.led)
                case 51:  # D#3
                    self.blackout_fixture(self.blinder1)
                    self.blackout_fixture(self.blinder2)
                case 52:  # E3
                    self.blackout_all()

    def pick_scene_from_song_part(self, song_part, velocity):
        self.blackout_fixture(self.led)
        self.blackout_fixture(self.blinder1)
        self.blackout_fixture(self.blinder2)

        if self.color_palette:
                self.change_led_color_palette(self.color_palette, 0,
                  len(self.color_palette), self.beat_duration(self.song_tempo) * 1,
                  self.beat_duration(self.song_tempo) * 4)

        if song_part == 1: # INTRO
            if self.color_palette:
                self.change_led_color(self.color_palette[0])
            self.load_scene(self.led, 'FILLS2', 2)

        elif song_part == 2: # VERSE
            self.load_scene(self.led, 'FILLS2', 1) # Hardcoded animation for all VERSEs

        elif song_part == 3: # CHORUS
            self.load_chase(self.led, random.randint(9, 14))

        elif song_part == 4: # BREAK
            self.load_scene(self.blinder1, 'BLINDER_SCENES', 1)
            self.load_scene(self.blinder2, 'BLINDER_SCENES', 1)

        elif song_part == 5: # BRIDGE
            self.load_scene(self.led, 'ANIMATIONS', 6)
            self.load_chase(self.blinder1, 1)
            self.load_chase(self.blinder2, 1)

        elif song_part == 6: # INST
            self.load_scene(self.led, 'ANIMATIONS', random.randint(5, 7))
            self.load_chase(self.blinder1, 1)
            self.load_chase(self.blinder2, 1)

        elif song_part == 7: # SOLO
            self.load_scene(self.led, 'ANIMATIONS', random.randint(5, 7))
            self.load_chase(self.blinder1, 1)
            self.load_chase(self.blinder2, 1)

        elif song_part == 8: # OUTRO
            self.load_chase(self.led, random.randint(0, 2))
            self.load_scene(self.blinder1, 'BLINDER_SCENES', 3)
            self.load_scene(self.blinder2, 'BLINDER_SCENES', 3)

        elif song_part == 9: # END
            self.load_chase(self.blinder1, 0)
            self.load_chase(self.blinder2, 0)

        # Mark blinders as owned by the automatic program so a later manual LED
        # scene clears them (see reset_led_blinder_status). Parts that leave the
        # blinders blacked out get the flag too, but blacking out an off blinder
        # is a harmless no-op.
        self.fixtures[self.blinder1].song_part_triggered = True
        self.fixtures[self.blinder2].song_part_triggered = True

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
                # Color palette timer (LED only)
                led = self.fixtures[self.led]
                if led.palette_size > 1:
                    if current_time - self.last_palette_change >= led.color_change_time / 1000.0:
                        i = 1
                        if led.is_palette_random:
                            i = random.randint(16, 48)

                        led.palette_index = (led.palette_index + i) % led.palette_size
                        self.set_fixture_color(self.led, led.palette_ptr[led.palette_index])
                        self.last_palette_change = current_time

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
