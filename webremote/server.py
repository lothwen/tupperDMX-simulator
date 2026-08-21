# ABOUTME: Web-remote server worker: aiohttp HTTP + WebSocket app running its
# ABOUTME: own asyncio loop on a QThread (same pattern as dmx/artnet_worker.py).
import asyncio
import errno
import json
import os
import threading
import time
from typing import Callable, List, Optional, Tuple

from aiohttp import web, WSMsgType, WSCloseCode
from PyQt6.QtCore import QObject, pyqtSignal

from core.paths import bundle_dir
from tupperlogger import get_logger
from webremote.commands import parse_command
from webremote.peaks import compute_peaks
from webremote.state import probe_duration


def default_static_dir() -> str:
    """Shipped web page location (bundle_dir handles dev vs. PyInstaller)."""
    return os.path.join(bundle_dir(), "webremote", "static")


# How long /api/waveform waits for the engine to finish decoding the song it
# was asked about. The remote fires the moment the state message says the song
# changed, which is BEFORE the decode that same change started has finished, so
# arriving early is the normal path rather than an error. Generous because the
# SBC decodes a five-minute track far slower than a laptop does.
WAVEFORM_WAIT_S = 20.0


class WebRemoteWorker(QObject):
    """Serves the tablet page and relays WS traffic. Runs on its own QThread;
    `publish_state`/`publish_event`/`set_song_paths` are called from the Qt
    thread and hop into the server loop via call_soon_threadsafe, while
    validated commands travel back through the queued `command_received`
    signal (same cross-thread idiom as StageFrameBridge)."""

    command_received = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)
    client_count_changed = pyqtSignal(int)

    def __init__(self, port: int, static_dir: Optional[str] = None):
        super().__init__()
        self.port = port
        self.static_dir = static_dir or default_static_dir()
        self.stop_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._clients: set = set()          # touched only from the server loop
        self._latest_state: Optional[dict] = None
        self._song_paths: List[Optional[str]] = []
        self._decoded_audio: Optional[Callable[[], Optional[Tuple]]] = None
        self.logger = get_logger("webremote.server")

    def run(self) -> None:
        """Thread body: create this thread's event loop and serve until stop()."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            self.logger.error(f"Web remote worker error: {e}")
            self.error_occurred.emit(f"Web remote worker error: {e}")
        finally:
            self._loop.close()
            self._loop = None
            self.logger.info("Web remote server stopped")

    async def _serve(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/api/state", self._handle_state)
        app.router.add_get("/api/waveform", self._handle_waveform)
        app.router.add_static("/static", self.static_dir)

        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "0.0.0.0", self.port)
            try:
                await site.start()
            except OSError as e:
                if e.errno == errno.EADDRINUSE:
                    msg = (f"Web remote port {self.port} is already in use — "
                           f"another TupperDMX instance or application may be "
                           f"running.")
                else:
                    msg = f"Failed to bind web remote port {self.port}: {e}"
                self.logger.error(msg)
                self.error_occurred.emit(msg)
                return

            self.logger.info(f"Web remote serving on 0.0.0.0:{self.port}")
            while not self.stop_event.is_set():
                await asyncio.sleep(0.1)
        finally:
            for ws in list(self._clients):
                await ws.close(code=WSCloseCode.GOING_AWAY, message=b"shutdown")
            self._clients.clear()
            await runner.cleanup()

    def stop(self) -> None:
        self.stop_event.set()

    # ------------------------------------------------------------------
    # Called from the Qt thread
    # ------------------------------------------------------------------
    def publish_state(self, state: dict) -> None:
        """Cache + broadcast a full state message. The cache means clients
        connecting before/after the loop is up still get a snapshot."""
        self._latest_state = state
        self._post(json.dumps(state))

    def publish_event(self, msg: dict) -> None:
        """Broadcast a light event (pos/beat); dropped if the loop isn't up."""
        self._post(json.dumps(msg))

    def set_song_paths(self, paths: List[Optional[str]]) -> None:
        """Audio paths by setlist index for /api/waveform. Kept server-side;
        paths are never sent to clients."""
        self._song_paths = list(paths)

    def set_decoded_audio_source(
            self, source: Optional[Callable[[], Optional[Tuple]]]) -> None:
        """Install what /api/waveform reads samples through: a callable
        returning (setlist index, decoded path, PCM) for the track the engine
        currently holds, or None while nothing is loaded. Installed from the Qt
        thread, but CALLED from the server loop — see WebRemoteManager for why
        that is safe."""
        self._decoded_audio = source

    def _post(self, payload: str) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._broadcast, payload)
            except RuntimeError:
                pass  # loop shut down between the check and the call

    # ------------------------------------------------------------------
    # Server-loop side
    # ------------------------------------------------------------------
    def _broadcast(self, payload: str) -> None:
        for ws in list(self._clients):
            asyncio.ensure_future(self._safe_send(ws, payload))

    async def _safe_send(self, ws, payload: str) -> None:
        # A failing socket is dropped, never allowed to break the others
        # (same isolation philosophy as the controller's DMX sinks).
        try:
            await ws.send_str(payload)
        except Exception:
            self._clients.discard(ws)

    def _handle_text(self, text: str) -> Optional[dict]:
        """Validate one inbound WS text frame into a command dict (or None)."""
        return parse_command(text)

    async def _handle_index(self, request):
        return web.FileResponse(os.path.join(self.static_dir, "index.html"))

    async def _handle_state(self, request):
        return web.json_response(self._latest_state or {})

    async def _handle_waveform(self, request):
        try:
            index = int(request.query.get("song", ""))
        except ValueError:
            return web.json_response({"error": "bad song index"}, status=400)

        paths = self._song_paths
        path = paths[index] if 0 <= index < len(paths) else None
        if not path or not os.path.exists(path):
            return web.json_response({"error": "unknown song"}, status=404)

        decoded = await self._await_decoded(index)
        if decoded is None:
            return web.json_response({"error": "song not decoded"}, status=409)
        decoded_path, pcm = decoded

        loop = asyncio.get_running_loop()
        # Off the server loop: binning a five-minute track is tens of ms of
        # numpy, which releases the GIL but would still stall the WS traffic.
        bins = await loop.run_in_executor(
            None, compute_peaks, decoded_path, pcm)
        # The duration of what the BINS cover — the file the engine decoded,
        # which is the monitored track and may be shorter than the song. The
        # client scales the peaks by it so they stay under the playhead.
        duration = await loop.run_in_executor(None, probe_duration, decoded_path)
        return web.json_response({"song": index, "duration": duration, "bins": bins})

    async def _await_decoded(self, index: int) -> Optional[Tuple[str, object]]:
        """Wait for the engine to be holding setlist `index`, then hand back
        (decoded path, PCM). None if it never turns up.

        Polled rather than pushed: the engine clears the mixer for the whole
        length of a decode, so 'not there yet' and 'this is the previous song'
        are the same state and there is nothing to race against — whereas an
        event fired on load would have to be matched up with whichever request
        was waiting for it.

        Watches stop_event as well as the clock: aiohttp's cleanup waits for
        in-flight handlers, so a request parked here would hold the whole
        thread past the manager's 5 s grace and get it terminated mid-flight."""
        deadline = time.monotonic() + WAVEFORM_WAIT_S
        while not self.stop_event.is_set():
            source = self._decoded_audio
            current = source() if source is not None else None
            if current is not None and current[0] == index:
                return current[1], current[2]
            if time.monotonic() >= deadline:
                self.logger.warning(
                    f"Waveform for song {index}: nothing decoded after "
                    f"{WAVEFORM_WAIT_S:.0f}s")
                return None
            await asyncio.sleep(0.05)
        return None

    async def _handle_ws(self, request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        self.client_count_changed.emit(len(self._clients))
        try:
            if self._latest_state is not None:
                await ws.send_str(json.dumps(self._latest_state))
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    cmd = self._handle_text(msg.data)
                    if cmd is not None:
                        self.command_received.emit(cmd)
        finally:
            self._clients.discard(ws)
            self.client_count_changed.emit(len(self._clients))
        return ws
