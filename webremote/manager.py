# ABOUTME: Qt-side web-remote bridge: owns the server worker's QThread, builds
# ABOUTME: state snapshots from the live managers and executes remote commands.
from typing import Optional

from PyQt6.QtCore import QObject, QThread, QTimer

from tupperlogger import get_logger
from webremote import state as remote_state
from webremote.server import WebRemoteWorker


class WebRemoteManager(QObject):
    """Bridges the web-remote server and the Qt app. Every remote command is
    executed on the Qt thread (the worker's command_received signal arrives
    queued), so transport and cue routing reuse the exact same code paths as
    the desktop UI (MainWindow methods / _preview_clip)."""

    TICK_MS = 100  # pos updates at 10 Hz; also polls the blackout toggle

    def __init__(self, config, window):
        super().__init__()
        self.config = config
        self.window = window
        self.logger = get_logger("webremote.manager")
        self.worker: Optional[WebRemoteWorker] = None
        self.thread: Optional[QThread] = None
        self._last_pos = -1.0
        self._last_blackout = False

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(self.TICK_MS)
        self._tick_timer.timeout.connect(self._on_tick)

        # Structural-change sources → fresh full snapshot. All are no-ops
        # while no worker is running.
        w = window
        w.media_controller.playback_state_changed.connect(self._publish_full)
        w.media_controller.media_loaded.connect(self._publish_full)
        w.midi_manager.data_changed.connect(self._publish_full)
        w.timeline_toolbar.bpm_changed.connect(self._publish_full)
        w.audio_controller.beat_occurred.connect(self._on_beat)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def apply_config(self) -> None:
        """Reconcile the running server with config.webremote (idempotent)."""
        web = self.config.webremote
        if self.worker is not None and (not web.enabled or self.worker.port != web.port):
            self._stop_worker()
        if web.enabled and self.worker is None:
            self._start_worker(web.port)

    def cleanup(self) -> None:
        self._stop_worker()

    def _start_worker(self, port: int) -> None:
        worker = WebRemoteWorker(port)
        thread = QThread()
        worker.moveToThread(thread)

        worker.command_received.connect(self._on_command)
        worker.client_count_changed.connect(self._publish_full)
        worker.error_occurred.connect(self.logger.error)

        thread.started.connect(worker.run)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self.worker = worker
        self.thread = thread
        thread.start()
        self._tick_timer.start()
        # Seed the snapshot; the worker caches it for clients that connect
        # before any structural change fires.
        self._publish_full()
        self.logger.info(f"Web remote started on port {port}")

    def _stop_worker(self) -> None:
        if self.worker is None or self.thread is None:
            return
        self._tick_timer.stop()
        self.worker.stop()
        if self.thread.isRunning():
            self.thread.quit()
            self.thread.wait(5000)
            if self.thread.isRunning():
                self.logger.warning("Web remote thread did not stop gracefully, terminating")
                self.thread.terminate()
                self.thread.wait(1000)
        self.worker = None
        self.thread = None
        self.logger.info("Web remote stopped")

    # ------------------------------------------------------------------
    # Commands (Qt thread)
    # ------------------------------------------------------------------
    def _on_command(self, cmd: dict) -> None:
        try:
            self._execute(cmd)
        except Exception as e:
            self.logger.warning(f"Web remote command {cmd} failed: {e}")

    def _execute(self, cmd: dict) -> None:
        action = cmd.get("action")
        w = self.window
        if action == "play_pause":
            w.play_pause_playback()
        elif action == "stop":
            w.stop_playback()
        elif action == "next":
            w.playlist_controller.next_song()
        elif action == "prev":
            w.playlist_controller.prev_song()
        elif action == "select_song":
            w.playlist_controller.go_to_song(cmd.get("index", -1))
        elif action == "seek":
            duration = self._current_duration()
            target = max(0.0, cmd.get("time", 0.0))
            if duration:
                target = min(target, duration)
            w.seek_playback(target)
        elif action == "cue":
            cues = self.config.webremote.cues
            index = cmd.get("index", -1)
            if 0 <= index < len(cues):
                w._preview_clip([tuple(m) for m in cues[index].messages])
        elif action == "blackout":
            w._preview_clip([(0, 51, 0)])  # blackout-all verb (a toggle)
        elif action == "panic":
            # Stop the show and force blackout ON. Note 51 toggles, so only
            # send it when blackout isn't already active.
            w.stop_playback()
            if not self._blackout_state():
                w._preview_clip([(0, 51, 0)])
        # Immediate feedback: every command ends with a fresh snapshot.
        self._publish_full()

    # ------------------------------------------------------------------
    # State snapshots (Qt thread)
    # ------------------------------------------------------------------
    def _blackout_state(self) -> bool:
        controller = getattr(self.window, "software_controller", None)
        return bool(getattr(controller, "blackout_all_state", False))

    def _current_duration(self) -> Optional[float]:
        song = self.config.get_current_song()
        audio = song.audio if song else None
        return (remote_state.probe_duration(audio)
                or self.window.midi_manager.song_end_time())

    def _build_state(self) -> dict:
        w = self.window
        duration = self._current_duration()
        markers = [(e["time"], e["info"].part, e["info"].color)
                   for e in w.midi_manager.midi_events
                   if w.midi_manager.is_song_part(e) and e.get("info")]
        blackout = self._blackout_state()
        self._last_blackout = blackout
        return remote_state.build_state(
            playing=w.media_controller.get_playback_state(),
            position=w.media_controller.get_current_position(),
            duration=duration,
            bpm=w.timeline_toolbar.get_bpm(),
            blackout=blackout,
            setlist=remote_state.build_setlist(
                self.config.playlist.songs,
                self.config.playlist.current_song_index),
            sections=remote_state.compute_sections(markers, duration or 0.0),
            cues=[{"index": i, "label": c.label, "color": c.color}
                  for i, c in enumerate(self.config.webremote.cues)],
        )

    def _publish_full(self, *_) -> None:
        if self.worker is None:
            return
        self.worker.set_song_paths(
            [s.audio for s in self.config.playlist.songs])
        self.worker.publish_state(self._build_state())

    def _on_tick(self) -> None:
        if self.worker is None:
            return
        playing = self.window.media_controller.get_playback_state()
        pos = self.window.media_controller.get_current_position()
        if playing or pos != self._last_pos:
            self._last_pos = pos
            self.worker.publish_event({"type": "pos", "t": pos, "playing": playing})
        # The blackout toggle can flip outside our commands (timeline note-51
        # events, desktop clip previews) — poll it so the web button tracks.
        if self._blackout_state() != self._last_blackout:
            self._publish_full()

    def _on_beat(self, beat: int) -> None:
        if self.worker is not None:
            self.worker.publish_event({"type": "beat", "beat": beat})
