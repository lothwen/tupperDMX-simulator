# ABOUTME: The colour story of a generated show: its palette (three hues and
# ABOUTME: white) from a preset, the fixture palette's hue × mode or three
# ABOUTME: picked colours; which of them a song part wears, hue maths, and the
# ABOUTME: palette a song's mood picks. Qt-free.
"""What colours a generated show wears.

A palette is three hues — what most of the song wears, its partner, and a
contrast kept for choruses, bridges and accents — and white for the hits,
every fixture wearing the same ones. A take names its palette with a SPEC, a
string, so its options stay a plain value:

  * a preset, ``ember`` — one of PALETTES, mixed by hand for LEDs;
  * ``MODE:HUE``, ``triadic:azure`` — the fixture palette's (note 60, the
    Palette tab): one of its MODES walked from one of its twelve HUES, or
    from ``random``, a hue drawn from the take's seed;
  * ``custom:#rrggbb,#rrggbb,#rrggbb`` — primary, secondary and contrast.

The fixture palette lends its maths (generate_song_colors) and nothing else,
so "Triadic" means here what it means on the Palette tab — but the generated
show writes its colours into its own looks and never hands them to the
controller's live colour cycling.
"""
import colorsys
import random
import re
from typing import Optional, Tuple

from autoprogram import analysis
from software_controller.main import COLOR32_WHITE, PALETTE_MODES, generate_song_colors

# Colours as (R, G, B, W).
WHITE = (254, 254, 254, 254)
BLUE = (0, 20, 254, 0)
ICE = (0, 90, 254, 0)
PURPLE = (100, 0, 254, 0)
TEAL = (0, 160, 140, 0)
CYAN = (0, 180, 254, 0)
RED = (254, 0, 0, 0)
CRIMSON = (254, 0, 40, 0)
AMBER = (254, 90, 0, 0)
ORANGE = (254, 50, 0, 0)
MAGENTA = (254, 0, 180, 0)
GOLD = (254, 150, 0, 0)
GREEN = (0, 254, 30, 0)
LIME = (120, 254, 0, 0)
MINT = (0, 254, 130, 0)
PINK = (254, 20, 110, 0)
LAVENDER = (150, 70, 254, 0)

SLOTS = ("primary", "secondary", "contrast")
PALETTES = {
    "acid": dict(primary=LIME, secondary=GREEN, contrast=MAGENTA),
    "candy": dict(primary=PINK, secondary=LAVENDER, contrast=MINT),
    "ember": dict(primary=AMBER, secondary=RED, contrast=ICE),
    "fire": dict(primary=RED, secondary=ORANGE, contrast=GOLD),
    "forest": dict(primary=GREEN, secondary=TEAL, contrast=GOLD),
    "ice": dict(primary=ICE, secondary=CYAN, contrast=WHITE),
    "neon": dict(primary=MAGENTA, secondary=PURPLE, contrast=CYAN),
    "ocean": dict(primary=BLUE, secondary=TEAL, contrast=AMBER),
    "royal": dict(primary=PURPLE, secondary=BLUE, contrast=GOLD),
    "sunset": dict(primary=ORANGE, secondary=CRIMSON, contrast=PURPLE),
}
# The fixture palette's twelve hues (RAINBOW_HUE_12, 30° apart), named, and
# its modes.
HUES = ("red", "orange", "yellow", "chartreuse", "green", "spring",
        "cyan", "azure", "blue", "violet", "magenta", "rose")
RANDOM = "random"
MODES = tuple(mode.lower() for mode in PALETTE_MODES)
CUSTOM = "custom"
_HEX = re.compile(r"#?([0-9a-fA-F]{6})")

# Which of the three a material wears, by the part it first plays: (lead,
# partner). A second material of one part wears them the other way round.
PART_COLOURS = {"INTRO": ("secondary", "primary"), "VERSE": ("primary", "secondary"),
                "CHORUS": ("primary", "contrast"), "BRIDGE": ("contrast", "secondary"),
                "INST": ("secondary", "contrast"), "SOLO": ("contrast", "primary"),
                "BREAK": ("secondary", "secondary"), "OUTRO": ("primary", "secondary")}
# A song is busy when this share of its sounding bars is loud.
BUSY = 0.4


def palette(spec: str, seed: int = 0) -> Tuple[str, dict]:
    """The palette `spec` names, and the spec it resolved to: the same one,
    but for a ``random`` hue, which is drawn from `seed` and named. Raises
    ValueError on a spec that names none."""
    if spec in PALETTES:
        return spec, PALETTES[spec]
    kind, _, rest = spec.partition(":")
    if kind in MODES and (rest in HUES or rest == RANDOM):
        if rest == RANDOM:
            rest = HUES[random.Random(seed).randrange(len(HUES))]
        return f"{kind}:{rest}", harmony(kind, HUES.index(rest))
    picked = [_HEX.fullmatch(c) for c in rest.split(",")]
    if kind == CUSTOM and len(picked) == len(SLOTS) and all(picked):
        rgbs = [bytes.fromhex(m.group(1)) for m in picked]
        return (f"{CUSTOM}:" + ",".join("#" + rgb.hex() for rgb in rgbs),
                dict(zip(SLOTS, (tuple(min(254, x) for x in rgb) + (0,) for rgb in rgbs))))
    raise ValueError(f"no palette '{spec}'")


def harmony(mode: str, hue_index: int) -> dict:
    """The palette the fixture palette's `mode` makes from its
    `hue_index`-th hue. Its first colour leads and its last is the contrast;
    the second partners the first in a mode of three, while a mode of two
    keeps it for the contrast — the verses wear one colour and the choruses
    open out to the other. Color+White's white is the show's, white
    emitters lit too."""
    made = [WHITE if rgb == COLOR32_WHITE else tuple(min(254, x) for x in rgb) + (0,)
            for rgb in generate_song_colors(PALETTE_MODES[MODES.index(mode)], hue_index)]
    return dict(primary=made[0], secondary=made[1] if len(made) > 2 else made[0],
                contrast=made[-1])


def label(spec: str) -> str:
    """How a spec reads: ``Ember``, ``Triadic · Azure``, ``Custom``."""
    kind, _, rest = spec.partition(":")
    return f"{kind.title()} · {rest.title()}" if kind in MODES else kind.title()


def hue(colour) -> Optional[float]:
    """The hue of an (R, G, B, ...) colour in degrees; None when it is white."""
    r, g, b = (max(0.0, x) for x in colour[:3])
    top = max(r, g, b)
    if top <= 0:
        return None
    h, s, _v = colorsys.rgb_to_hsv(r / top, g / top, b / top)
    return h * 360 if s >= 0.3 else None


def hue_gap(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def mood_palette(song: analysis.SongMap) -> str:
    """A palette for the song's mood: minor keys take the warm-and-ice ember
    when the song is busy, the blue ocean when it is not; major keys the
    neon when busy, the sunset when not. Busy is BUSY of its sounding bars
    loud."""
    sound = [b for b in song.bars if b.level != analysis.SILENT]
    busy = sum(b.level == analysis.HIGH for b in sound) >= BUSY * max(1, len(sound))
    if song.minor:
        return "ember" if busy else "ocean"
    return "neon" if busy else "sunset"
