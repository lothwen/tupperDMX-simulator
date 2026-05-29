from .artnet_sender import ArtNetSender

from .dmx import led_banks, blinder_banks, led_chases, blinder_chases, LedScene, BlinderScene
import time
import copy
import random
import threading
import threading
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

# DMX Channel definitions
LED_START_CHANNEL = 0
LED_NUM_BLEND_CHANNELS = 4
LED_NUM_TOTAL_CHANNELS = 7

BLINDER1_START_CHANNEL = 32
BLINDER1_NUM_BLEND_CHANNELS = 5
BLINDER1_NUM_TOTAL_CHANNELS = 8

BLINDER2_START_CHANNEL = 64
BLINDER2_NUM_BLEND_CHANNELS = 5
BLINDER2_NUM_TOTAL_CHANNELS = 8

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

    When ``dialog`` is None the function returns None to 
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
            hsl_to_rgb(((hue - 30) % 360) / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            hsl_to_rgb(((hue + 30) % 360) / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
        ]
    if mode == 'TRIADIC':
        return [
            hsl_to_rgb(((hue - 120) % 360) / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
            hsl_to_rgb(((hue + 120) % 360) / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
        ]
    if mode == 'COLOR+WHITE':
        return [
            COLOR32_WHITE,
            hsl_to_rgb(hue / 360.0, DEFAULT_HSL_S, DEFAULT_HSL_L),
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


class DMXController:
    def __init__(self):
        self.logger = get_logger("software_controller.main")
        self.lock = threading.Lock()

        self.led_status = {
            'bank': 'ANIMATIONS',
            'scene': 0,
            'color': COLOR32_WHITE,
            'blend_time': DEFAULT_BLEND_TIME,
            'blend_time_elapsed': 0,
            'color_change_time': DEFAULT_COLOR_CHANGE_TIME,
            'chase_change_time': DEFAULT_CHASE_TIME,
            'is_blackout': True,
            'is_chasing': False,
            'is_palette_random': False,
            'palette_index': 0,
            'palette_size': 0,
            'palette_ptr': None,
            'chase': 0,
            'chase_index': 0
        }
        self.song_tempo = 145 # TODO: hardcoded value, find a way to pass this with a midi note
        self.color_palette = []
        self.blinder1_status = {
            'bank': 0,
            'scene': 0,
            'is_blackout': True,
            'is_chasing': False,
            'chase': 0,
            'chase_index': 0,
            'chase_change_time': DEFAULT_CHASE_TIME,
            'blend_time': DEFAULT_BLEND_TIME,
            'blend_time_elapsed': 0
        }
        self.blinder2_status = copy.deepcopy(self.blinder1_status)

        self.universe_present = [0] * UNIVERSE_SIZE
        self.universe_future = [0] * UNIVERSE_SIZE
        self.universe_past = [0] * UNIVERSE_SIZE

        self.led = LedScene()
        self.blinder1 = BlinderScene()
        self.blinder2 = BlinderScene()

        self.blackout_all_state = False
        self.current_velocity = 127
        self.brightness_cap = 255
        self.speed_cap = 255
        
        self.last_palette_change = 0
        self.last_led_chase_change = 0
        self.last_blinder1_chase_change = 0
        self.last_blinder1_chase_change = 0
        self.last_blinder2_chase_change = 0

        self._running = False
        self._thread = None


    def update_led_scene(self, bank, scene_index):
        if not self.led_status['is_blackout'] and self.led_status['bank'] == bank and self.led_status['scene'] == scene_index and not self.led_status['is_chasing']:
            self.blackout_led()
            return
            
        scene = led_banks[bank][scene_index]
        self.led.brightness = min(scene.brightness, self.brightness_cap)
        
        if scene.blue == 255 and scene.green == 255 and scene.red == 255:
            self.led.blue = self.led_status['color'][2]
            self.led.green = self.led_status['color'][1]
            self.led.red = self.led_status['color'][0]
        else:
            self.led.blue = scene.blue
            self.led.green = scene.green
            self.led.red = scene.red
            self.led_status['palette_size'] = 0
            self.led_status['palette_ptr'] = None

        self.led.animation = scene.animation
        if self.speed_cap == 0:
            self.led.speed = self.current_velocity * 2
        elif self.speed_cap == 255:
            self.led.speed = scene.speed
        else:
            self.led.speed = self.speed_cap
        self.led.direction = scene.direction

        self.led_status['bank'] = bank
        self.led_status['scene'] = scene_index
        self.led_status['is_blackout'] = False
        self.led_status['blend_time_elapsed'] = 0
        self.universe_past = copy.deepcopy(self.universe_present)
        
        # Immediately apply non-blended channels (animation, speed, direction) to universe_present
        self.universe_present[LED_START_CHANNEL + LED_NUM_BLEND_CHANNELS:LED_START_CHANNEL + LED_NUM_TOTAL_CHANNELS] = [self.led.animation, self.led.speed, self.led.direction]

    def update_blinder_scene(self, blinder, bank_name, scene_index):
        status = self.blinder1_status if blinder == 1 else self.blinder2_status
        if not status['is_blackout'] and status['bank'] == bank_name and status['scene'] == scene_index and not status['is_chasing']:
            self.blackout_blinder(blinder)
            return

        scene = blinder_banks[bank_name][scene_index]
        blinder_obj = self.blinder1 if blinder == 1 else self.blinder2
        
        blinder_obj.brightness = scene.brightness
        blinder_obj.cool1 = scene.cool1
        blinder_obj.cool2 = scene.cool2
        blinder_obj.warm1 = scene.warm1
        blinder_obj.warm2 = scene.warm2
        blinder_obj.strobe = scene.strobe
        blinder_obj.mode = scene.mode
        blinder_obj.speed = scene.speed

        status['bank'] = bank_name
        status['scene'] = scene_index
        status['is_blackout'] = False
        status['blend_time_elapsed'] = 0
        self.universe_past = copy.deepcopy(self.universe_present)
        
        # Immediately apply non-blended channels (strobe, mode, speed) to universe_present
        start_channel = BLINDER1_START_CHANNEL if blinder == 1 else BLINDER2_START_CHANNEL
        num_blend_channels = BLINDER1_NUM_BLEND_CHANNELS
        self.universe_present[start_channel + num_blend_channels:start_channel + BLINDER1_NUM_TOTAL_CHANNELS] = [blinder_obj.strobe, blinder_obj.mode, blinder_obj.speed]

    def blackout_led(self):
        self.logger.debug("Blackout LED")

        self.led = LedScene(brightness=0, blue=0, green=0, red=0, animation=0, speed=0, direction=0)
        self.led_status['is_blackout'] = True
        self.led_status['is_chasing'] = False
        self.led_status['palette_size'] = 0
        self.led_status['palette_ptr'] = None
        self.update_universe()

    def blackout_blinder(self, blinder):
        self.logger.debug(f"Blackout Blinder{blinder}")

        if blinder == 1:
            self.blinder1 = BlinderScene(brightness=0, cool1=0, cool2=0, warm1=0, warm2=0, strobe=0, mode=0, speed=0)
            self.blinder1_status['is_blackout'] = True
            self.blinder1_status['is_chasing'] = False
        else:
            self.blinder2 = BlinderScene(brightness=0, cool1=0, cool2=0, warm1=0, warm2=0, strobe=0, mode=0, speed=0)
            self.blinder2_status['is_blackout'] = True
            self.blinder2_status['is_chasing'] = False
        self.update_universe()

    def blackout_all(self):
        self.blackout_all_state = not self.blackout_all_state
        self.logger.info(f"Blackout All state: {'enabled' if self.blackout_all_state else 'disabled'}")

    def set_channel(self, channel_name, value):
        setattr(self.led, channel_name, value)

    def set_led_color(self, color):
        self.led_status['color'] = color
        self.led.blue = color[2]
        self.led.green = color[1]
        self.led.red = color[0]
        self.led_status['blend_time_elapsed'] = 0
        self.universe_past = copy.deepcopy(self.universe_present)

    def change_led_color_palette(self, palette, index, size, blend_time, color_change_time):
        self.led_status['palette_ptr'] = palette
        self.led_status['palette_index'] = index
        self.led_status['palette_size'] = size
        self.led_status['blend_time'] = blend_time
        self.led_status['color_change_time'] = color_change_time
        self.set_led_color(palette[index])
        self.last_palette_change = time.time()

    def load_led_chase(self, chase_index):
        self.led_status['is_chasing'] = True
        self.led_status['chase'] = chase_index
        chase = led_chases[chase_index]
        self.update_led_scene(chase['banks'][0], chase['scenes'][0])
        self.last_led_chase_change = time.time()

    def load_blinder_chase(self, blinder, chase_index):
        status = self.blinder1_status if blinder == 1 else self.blinder2_status
        status['is_chasing'] = True
        status['chase'] = chase_index
        chase = blinder_chases[chase_index]
        self.update_blinder_scene(blinder, chase['banks'][0], chase['scenes'][0])
        if blinder == 1:
            self.last_blinder1_chase_change = time.time()
        else:
            self.last_blinder2_chase_change = time.time()
        
    def handle_note_on(self, channel, pitch, velocity):
        with self.lock:
            self.logger.debug(f"Note On: channel={channel}, pitch={pitch}, velocity={velocity}")
            self.current_velocity = velocity

            match pitch:
                case 0:  # C-1
                    pass # NoteOff, do nothing
                case 1:   # C#-1
                    self.set_channel('brightness', velocity * 2)
                case 2:   # D-1
                    self.set_channel('blue', velocity * 2) 
                case 3:   # D#-1
                    self.set_channel('green', velocity * 2) 
                case 4:   # E-1
                    self.set_channel('red', velocity * 2)
                case 5:   # F-1
                    self.set_channel('animation', velocity * 2)
                case 6:   # F#-1
                    self.set_channel('speed', velocity * 2)
                case 7:   # G-1
                    self.set_channel('direction', velocity * 2)
                case 8:   # G#-1
                    self.led_status['is_palette_random'] = True
                case 9:   # A-1
                    self.led_status['is_palette_random'] = False
                case 10:  # A#-1
                    self.led_status['blend_time'] = velocity * 100
                case 11:  # B-1
                    self.led_status['color_change_time'] = velocity * 1000
                case 12:  # C0
                    self.update_led_scene(self.led_status['bank'], 0)
                case 13:  # C#0
                    self.update_led_scene(self.led_status['bank'], 1)
                case 14:  # D0
                    self.update_led_scene(self.led_status['bank'], 2)
                case 15:  # D#0
                    self.update_led_scene(self.led_status['bank'], 3)
                case 16:  # E0
                    self.update_led_scene(self.led_status['bank'], 4)
                case 17:  # F0
                    self.update_led_scene(self.led_status['bank'], 5)
                case 18:  # F#0
                    self.update_led_scene(self.led_status['bank'], 6)
                case 19:  # G0
                    self.update_led_scene(self.led_status['bank'], 7)
                case 20:  # G#0
                    self.set_led_color(COLOR32_WHITE)
                case 21:  # A0
                    self.set_led_color(COLOR32_ORANGE)
                case 22:  # A#0
                    self.set_led_color(COLOR32_CYAN)
                case 23:  # B0
                    self.set_led_color(COLOR32_PINK)
                case 24:  # C1
                    self.led_status['bank'] = 'FILLS'
                case 25:  # C#1
                    self.led_status['bank'] = 'TUBES'
                case 26:  # D1
                    self.update_blinder_scene(1, 'BLINDER_SCENES', 0)
                    self.update_blinder_scene(2, 'BLINDER_SCENES', 0)
                case 27:  # D#1
                    self.update_blinder_scene(1, 'BLINDER_SCENES', 1)
                    self.update_blinder_scene(2, 'BLINDER_SCENES', 1)
                case 28:  # E1
                    self.update_blinder_scene(1, 'BLINDER_SCENES', 2)
                    self.update_blinder_scene(2, 'BLINDER_SCENES', 2)
                case 29:  # F1
                    self.update_blinder_scene(1, 'BLINDER_SCENES', 3)
                    self.update_blinder_scene(2, 'BLINDER_SCENES', 3)
                case 30:  # F#1
                    self.update_blinder_scene(1, 'BLINDER_SCENES', 4)
                    self.update_blinder_scene(2, 'BLINDER_SCENES', 4)
                case 31:  # G1
                    self.update_blinder_scene(1, 'BLINDER_SCENES', 1)
                case 32:  # G#1
                    self.update_blinder_scene(2, 'BLINDER_SCENES', 1)
                case 38:  # D2 - Precount beats
                    self.update_blinder_scene(1, 'BLINDER_SCENES', 0)
                    self.update_blinder_scene(2, 'BLINDER_SCENES', 0)
                case 39:  # D#2 - Precount downbeats
                    self.update_blinder_scene(1, 'BLINDER_SCENES', 1)
                    self.update_blinder_scene(2, 'BLINDER_SCENES', 1)
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
                    self.blackout_led()
                case 51:  # D#3
                    self.blackout_blinder(1)
                    self.blackout_blinder(2)
                case 52:  # E3
                    self.blackout_all()

            self.update_universe()

    def pick_scene_from_song_part(self, song_part, velocity):
        self.blackout_led()
        self.blackout_blinder(1)
        self.blackout_blinder(2)

        if self.color_palette:
                self.change_led_color_palette(self.color_palette, 0,
                  len(self.color_palette), self.beat_duration(self.song_tempo) * 1,
                  self.beat_duration(self.song_tempo) * 4)

        if song_part == 1: # INTRO
            if self.color_palette:
                self.set_led_color(self.color_palette[0])
            self.update_led_scene('FILLS2', 2)

        elif song_part == 2: # VERSE
            self.update_led_scene('FILLS', random.randint(0, 7))

        elif song_part == 3: # CHORUS
            self.load_led_chase(random.randint(9, 14))

        elif song_part == 4: # BREAK
            self.update_blinder_scene(1, 'BLINDER_SCENES', 0)
            self.update_blinder_scene(2, 'BLINDER_SCENES', 0)

        elif song_part == 5: # BRIDGE
            self.update_led_scene('ANIMATIONS', 5)
            self.update_blinder_scene(1, 'BLINDER_SCENES', 3)
            self.update_blinder_scene(2, 'BLINDER_SCENES', 3)

        elif song_part == 6: # INST
            self.update_led_scene('ANIMATIONS', random.randint(5, 7))
            self.load_blinder_chase(1, 1)
            self.load_blinder_chase(2, 1)

        elif song_part == 7: # SOLO
            self.update_led_scene('ANIMATIONS', random.randint(5, 7))
            self.load_blinder_chase(1, 1)
            self.load_blinder_chase(2, 1)

        elif song_part == 8: # OUTRO
            self.load_led_chase(random.randint(0, 2))
            self.update_blinder_scene(1, 'BLINDER_SCENES', 3)
            self.update_blinder_scene(2, 'BLINDER_SCENES', 3)

        elif song_part == 9: # END
            self.load_blinder_chase(1, 0)
            self.load_blinder_chase(2, 0)

    def beat_duration(self, bpms):
        if bpms == 0:
            return 0
        return (1000 / bpms * 60)

    def update_universe(self):
        # Update LED channels
        self.universe_future[LED_START_CHANNEL:LED_START_CHANNEL + LED_NUM_TOTAL_CHANNELS] = [self.led.brightness, self.led.blue, self.led.green, self.led.red, self.led.animation, self.led.speed, self.led.direction]
        
        # Update Blinder 1 channels
        self.universe_future[BLINDER1_START_CHANNEL:BLINDER1_START_CHANNEL + BLINDER1_NUM_TOTAL_CHANNELS] = [self.blinder1.brightness, self.blinder1.cool1, self.blinder1.cool2, self.blinder1.warm1, self.blinder1.warm2, self.blinder1.strobe, self.blinder1.mode, self.blinder1.speed]

        # Update Blinder 2 channels
        self.universe_future[BLINDER2_START_CHANNEL:BLINDER2_START_CHANNEL + BLINDER2_NUM_TOTAL_CHANNELS] = [self.blinder2.brightness, self.blinder2.cool1, self.blinder2.cool2, self.blinder2.warm1, self.blinder2.warm2, self.blinder2.strobe, self.blinder2.mode, self.blinder2.speed]

    def lerp(self, start, end, t):
        return start + (end - start) * t
        
    def start_background(self):
        """Start the controller in a background thread."""
        if self._running:
            return
            
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
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
                # Color palette timer
                if self.led_status['palette_size'] > 1:
                    if current_time - self.last_palette_change >= self.led_status['color_change_time'] / 1000.0:
                        i = 1
                        if self.led_status['is_palette_random']:
                            i = random.randint(16, 48)
                        
                        self.led_status['palette_index'] = (self.led_status['palette_index'] + i) % self.led_status['palette_size']
                        self.set_led_color(self.led_status['palette_ptr'][self.led_status['palette_index']])
                        self.last_palette_change = current_time

                # Chase timers
                self.last_led_chase_change = self._handle_chase(self.led_status, self.last_led_chase_change, led_chases, self.update_led_scene, current_time)
                self.last_blinder1_chase_change = self._handle_chase(self.blinder1_status, self.last_blinder1_chase_change, blinder_chases, self.update_blinder_scene, current_time, 1)
                self.last_blinder2_chase_change = self._handle_chase(self.blinder2_status, self.last_blinder2_chase_change, blinder_chases, self.update_blinder_scene, current_time, 2)

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
                        self._apply_blend(self.led_status, dt, LED_START_CHANNEL, LED_NUM_BLEND_CHANNELS, LED_NUM_TOTAL_CHANNELS)
                        self._apply_blend(self.blinder1_status, dt, BLINDER1_START_CHANNEL, BLINDER1_NUM_BLEND_CHANNELS, BLINDER1_NUM_TOTAL_CHANNELS)
                        self._apply_blend(self.blinder2_status, dt, BLINDER2_START_CHANNEL, BLINDER2_NUM_BLEND_CHANNELS, BLINDER2_NUM_TOTAL_CHANNELS)
                        data_to_send = self.universe_present
                
                if data_to_send:
                    dmx_sender.send_dmx(data_to_send)
            
            time.sleep(1.0 / LOOP_FREQUENCY_HZ)
            
        dmx_sender.close()

    def _handle_chase(self, status, last_change_time, chases, update_func, current_time, *args):
        if status['is_chasing'] and current_time - last_change_time >= status['chase_change_time'] / 1000.0:
            chase = chases[status['chase']]
            status['chase_index'] = (status['chase_index'] + 1) % len(chase['scenes'])
            update_func(*args, chase['banks'][status['chase_index']], chase['scenes'][status['chase_index']])
            self.update_universe()
            return current_time
        return last_change_time

    def _apply_blend(self, status, dt, start_channel, num_blend_channels, num_total_channels):
        if status['blend_time_elapsed'] < status['blend_time']:
            status['blend_time_elapsed'] += dt
            t = min(status['blend_time_elapsed'] / status['blend_time'], 1.0)
            
            # Blend the color/brightness channels
            for i in range(num_blend_channels):
                channel = start_channel + i
                past_val = self.universe_past[channel]
                future_val = self.universe_future[channel]
                self.universe_present[channel] = int(self.lerp(past_val, future_val, t))
        else:
            # Blend complete - copy all channels from future to present
            end_channel = start_channel + num_total_channels
            self.universe_present[start_channel:end_channel] = self.universe_future[start_channel:end_channel]


