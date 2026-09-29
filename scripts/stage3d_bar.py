#!/usr/bin/env python3
# ABOUTME: Runs the 3D bar stage (stage/stage3d/) in a window of its own, lit by a
# ABOUTME: show's patch from Art-Net or from the software controller run in-process.
"""The 3D bar, standalone — for working on it without the app.

Run it:

    uv run python scripts/stage3d_bar.py                    # the app's last show, Art-Net
    uv run python scripts/stage3d_bar.py --dmx local        # no app: its controller in here
    uv run python scripts/stage3d_bar.py --show ~/Documents/TupperDMX/demo.show

The renderer lives in `stage/stage3d/` (the app shows it in place of the 2D
stage behind Settings' experimental checkbox). What stays here is what only a
standalone run needs, and what `stage/` may not import:

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
    at a time, the way `DMXManager.dmx_data_received` delivers them.

Controls are listed in `stage/stage3d/bar.py`'s docstring.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtGui import QSurfaceFormat
from PyQt6.QtWidgets import QApplication

from core import fixture_library
from core.config import FixtureInstance, shipped_fixtures, userprefs_path
from core.show.manifest import show_from_manifest
from core.show.model import FIXTURES_DIR, MANIFEST_NAME
from stage.stage3d.bar import BarView, Stage

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


def main() -> int:
    parser = argparse.ArgumentParser(description="The bar, in 3D, lit by a show's patch from DMX.")
    parser.add_argument("--show", help="a .show folder (default: the one the app last opened)")
    parser.add_argument("--dmx", choices=("artnet", "local"), default="artnet",
                        help="artnet: listen for a running TupperDMX; local: run its "
                             "software controller in here (N steps the scenes)")
    parser.add_argument("--port", type=int, help="Art-Net port (default: the app's)")
    parser.add_argument("--universe", type=int, help="Art-Net universe (default: the app's)")
    parser.add_argument("--house-rig", action="store_true",
                        help="start with the photograph's own pars and heads on")
    args = parser.parse_args()

    # As stage/gpu/layer.py's configure_surface_format(): force desktop GL or a
    # Wayland/EGL session hands out GL ES, and do NOT ask for a version.
    fmt = QSurfaceFormat()
    fmt.setAlphaBufferSize(8)
    fmt.setDepthBufferSize(24)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication(sys.argv[:1])
    source, patch = load_patch(args.show)
    stage = Stage(patch)
    stage.rig.enabled = args.house_rig
    print(f"stage3d_bar: patch from {source}: {stage.patch.summary()}; "
          f"{len(stage.statics)} placements, {len(stage.house)} house lights")

    port, universe = artnet_settings()
    feed = DmxFeed(args.dmx, patch, args.port or port,
                   universe if args.universe is None else args.universe)
    app.aboutToQuit.connect(feed.stop)
    if args.dmx == "local":
        feed.next_scene()                 # something to look at straight away

    view = BarView(stage, feed, source)
    view.setWindowTitle("TupperDMX — the bar, in 3D")
    view.resize(1280, 800)
    view.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
