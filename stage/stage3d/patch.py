# ABOUTME: What the 3D bar borrows from the 2D stage: the picture coordinates a
# ABOUTME: patch is placed in, and each fixture's effect artwork decoded to arrays.
"""The 3D bar's line into the rest of TupperDMX, for `bar.py`.

  * **picture coordinates** — a patch's x/y are in the 2D stage's scene rect,
    which the bar maps onto its rig plane;
  * **effect artwork** — the GIFs a fixture's sidecar maps its effects to
    (`core.fixture_library.effect_asset`), decoded once into arrays. The LED
    bar's are 8 x 40: one column per bar, one row per LED, which is exactly
    the texture its faces need.

The patch and the DMX come from elsewhere: in the app, the live
`config.fixtures` and `DMXManager`'s frames (`embedded.py`); run standalone, the
launcher's show loader and DMX feed (`scripts/stage3d_bar.py`), which stay out
of this package because `stage/` imports neither `dmx/` nor
`software_controller/`.
"""

import math
from typing import Dict, Optional

import numpy as np
from PyQt6.QtGui import QImage, QImageReader

# The 2D stage's scene rect, which is what a patch's picture coordinates are in
# (`stage/controller.py`).
PICTURE_SIZE = (1536, 1024)


# ==========================================================================
# Effect artwork
# ==========================================================================

class Animation:
    """A GIF as arrays: `frames` is (count, rows, columns, 3) uint8."""

    def __init__(self, frames: np.ndarray, delays: np.ndarray):
        self.frames = frames
        self.ends = np.cumsum(delays)

    @property
    def duration(self) -> float:
        return float(self.ends[-1])

    def at(self, t: float) -> np.ndarray:
        """The frame showing `t` seconds in, looping."""
        return self.frames[int(np.searchsorted(self.ends, t % self.duration, side="right"))
                           % len(self.frames)]


_ANIMATIONS: Dict[str, Optional[Animation]] = {}


def animation(path: Optional[str]) -> Optional[Animation]:
    """The decoded artwork at `path`, decoded once."""
    if not path:
        return None
    if path not in _ANIMATIONS:
        reader = QImageReader(path)
        frames, delays = [], []
        while True:
            image = reader.read()
            if image.isNull():
                break
            # Qt reports the delay after reading the frame it belongs to; a
            # still image or an unset delay reads as a tenth of a second, the
            # rate the app's GIFs were authored at.
            delay = reader.nextImageDelay()
            delays.append((delay if delay > 0 else 100) / 1000.0)
            image = image.convertToFormat(QImage.Format.Format_RGB888)
            bits = image.constBits()
            bits.setsize(image.sizeInBytes())
            rows = np.frombuffer(bits, np.uint8).reshape(image.height(), image.bytesPerLine())
            frames.append(rows[:, :image.width() * 3].reshape(image.height(), image.width(), 3).copy())
            if not reader.supportsAnimation() or reader.imageCount() <= len(frames):
                break
        _ANIMATIONS[path] = Animation(np.stack(frames), np.asarray(delays)) if frames else None
    return _ANIMATIONS[path]


def srgb_to_linear(values: np.ndarray) -> np.ndarray:
    """0-255 sRGB to linear, element-wise — `stage.light.linear_rgb` for arrays."""
    c = np.clip(np.asarray(values, np.float32) / 255.0, 0.0, 1.0)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def speed_factor(speed_hz: Optional[float]) -> float:
    """How fast an effect's artwork plays: 1 Hz is the rate it was authored at,
    held to the range `stage/emission/surface.py` gives its QMovie (10-400%)."""
    if not speed_hz or not math.isfinite(speed_hz):
        return 1.0
    return max(0.1, min(4.0, float(speed_hz)))
