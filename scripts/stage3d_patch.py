#!/usr/bin/env python3
# ABOUTME: What connects the 3D bar to the app: the show's patch, a DMX feed (the
# ABOUTME: app's Art-Net or its software controller in-process) and effect artwork.
"""The 3D bar's line into the rest of TupperDMX, for `scripts/stage3d_bar.py`.

Three things, all borrowed from the app rather than re-implemented:

  * **the patch** — read from a show folder's `show.json` exactly as the app
    parses it (`core.show.manifest.show_from_manifest`), with the show-local
    fixture layer activated first, so a show-local type resolves. Read only:
    unlike `core.show.store.load_show`, nothing here creates or writes a file;
  * **the DMX** — either the app's own Art-Net listener
    (`dmx.artnet_worker.ArtNetWorker`, what Settings ▸ Input / Output uses for
    an Art-Net stage feed), receiving what a running TupperDMX sends when its
    controller's DMX output goes to Art-Net; or the app's software controller
    (`software_controller.main.DMXController`) run in-process, stepped through
    each fixture's own scenes from the keyboard, for when the app is not
    running. Either way the frames arrive on the Qt thread, a whole universe
    at a time, the way `DMXManager.dmx_data_received` delivers them;
  * **effect artwork** — the GIFs a fixture's sidecar maps its effects to
    (`core.fixture_library.effect_asset`), decoded once into arrays. The LED
    bar's are 8 x 40: one column per bar, one row per LED, which is exactly
    the texture its faces need.
"""

import json
import math
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import numpy as np
from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader

from core import fixture_library
from core.config import FixtureInstance, shipped_fixtures, userprefs_path
from core.show.manifest import show_from_manifest
from core.show.model import FIXTURES_DIR, MANIFEST_NAME

# The 2D stage's scene rect, which is what a patch's picture coordinates are in
# (`stage/controller.py`).
PICTURE_SIZE = (1536, 1024)

ARTNET_PORT = 6454


# ==========================================================================
# The patch
# ==========================================================================

def _prefs() -> dict:
    try:
        return json.loads(userprefs_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def load_patch(show: Optional[str]) -> Tuple[str, List[FixtureInstance]]:
    """(where it came from, the patch): the show named, else the one the app
    last opened, else the seed patch every new show starts from."""
    path = show or _prefs().get("last_show_path")
    if path:
        root = Path(path)
        manifest = root / MANIFEST_NAME
        if manifest.is_file():
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            fixture_library.set_show_fixtures_dir(root / FIXTURES_DIR)
            return root.name, list(show_from_manifest(root, data).fixtures)
        if show:
            raise SystemExit(f"'{show}' is not a show folder (no {MANIFEST_NAME})")
    return "config.json seed patch", shipped_fixtures()


def artnet_settings() -> Tuple[int, int]:
    """(port, universe) as the app's preferences have them."""
    artnet = _prefs().get("artnet", {})
    return int(artnet.get("port", ARTNET_PORT)), int(artnet.get("universe", 0))


# ==========================================================================
# The DMX feed
# ==========================================================================

class DmxFeed(QObject):
    """Whole universes, on the Qt thread, from one source."""

    frame = pyqtSignal(bytearray)

    def __init__(self, source: str, patch: List[FixtureInstance],
                 port: int = ARTNET_PORT, universe: int = 0):
        super().__init__()
        self.source = source
        self.port, self.universe = port, universe
        self.frames = 0
        self.error = ""
        self._stamps: List[float] = []
        self._thread = self._worker = self.controller = self._bridge = None
        self._step = 0
        if source == "artnet":
            self._start_artnet()
        elif source == "local":
            self._start_local(patch)
        else:
            raise ValueError(f"unknown DMX source '{source}'")

    # -- sources ---------------------------------------------------------
    def _start_artnet(self) -> None:
        from dmx.artnet_worker import ArtNetWorker
        settings = SimpleNamespace(artnet=SimpleNamespace(port=self.port, universe=self.universe))
        self._worker = ArtNetWorker(settings)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.dmx_data_received.connect(self._received)
        self._worker.error_occurred.connect(self._failed)
        self._thread.start()

    def _start_local(self, patch: List[FixtureInstance]) -> None:
        from dmx.manager import StageFrameBridge
        from software_controller.main import DMXController
        self.controller = DMXController(patch)
        self._bridge = StageFrameBridge()
        self._bridge.frame_received.connect(self._received)
        self.controller.set_sinks([self._bridge])
        self.controller.start_background()

    def _received(self, data: bytearray) -> None:
        self.frames += 1
        now = time.monotonic()
        self._stamps = [s for s in self._stamps if now - s < 1.0] + [now]
        self.frame.emit(data)

    def _failed(self, message: str) -> None:
        self.error = message

    # -- what the HUD says -----------------------------------------------
    def rate(self) -> float:
        now = time.monotonic()
        return float(sum(1 for s in self._stamps if now - s < 1.0))

    def describe(self) -> str:
        if self.source == "artnet":
            where = f"Art-Net :{self.port} universe {self.universe}"
            if self.error:
                return f"{where} - {self.error}"
            if not self.frames:
                return (f"{where} - waiting: set the app's controller DMX out to "
                        f"Art-Net 127.0.0.1 (Settings > Input / Output)")
            return f"{where} - {self.rate():.0f} frames/s"
        state = "blackout" if self.controller.blackout_all_state else f"scene step {self._step}"
        return f"local software controller - {state} - {self.rate():.0f} frames/s"

    # -- the local controller's keys -------------------------------------
    def next_scene(self) -> bool:
        """Every fixture to its next scene, across all its banks in order."""
        controller = self.controller
        if controller is None:
            return False
        self._step += 1
        with controller.lock:
            for fx, fixture in enumerate(controller.fixtures):
                scenes = [(bank, index) for bank, entries in fixture.type.banks.items()
                          for index in range(len(entries))]
                if scenes:
                    bank, index = scenes[(self._step - 1) % len(scenes)]
                    controller.load_scene(fx, bank, index)
                    # Out of the automatic program, or its chase stomps the scene.
                    controller.set_fixture_manual(fx)
        return True

    def blackout(self) -> bool:
        if self.controller is None:
            return False
        with self.controller.lock:
            self.controller.blackout_all()
        return True

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._thread.quit()
            self._thread.wait(2000)
        if self.controller is not None:
            self.controller.set_sinks([])
            self.controller.stop()


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
