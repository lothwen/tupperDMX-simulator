from typing import Optional
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from config import Config, IOConfig
from tupperlogger import get_logger
from .base import DMXWorkerBase, DMXConnectionStatus
from .artnet_worker import ArtNetWorker
from .usb_worker import USBDMXWorker
from software_controller.main import DMXController, UNIVERSE_SIZE
from software_controller.artnet_sender import ArtNetSender
from software_controller.usb_dmx_sender import USBDMXSender


class StageFrameBridge(QObject):
    """In-process frame sink: carries controller frames from its daemon thread
    into Qt. The cross-thread signal emission is delivered queued on the main
    thread, so downstream consumers (stage, DMX monitor) stay thread-safe."""

    frame_received = pyqtSignal(bytearray)

    def send_dmx(self, data):
        self.frame_received.emit(bytearray(data))

    def close(self):
        pass


class DMXManager(QObject):
    """I/O coordinator: owns the internal software controller's lifecycle and
    DMX output sinks, plus the stage-input worker (Art-Net listener / USB
    reader), all driven by config.io via apply_io()."""

    # Signals
    dmx_data_received = pyqtSignal(bytearray)
    connection_status_changed = pyqtSignal(str, str)  # source, status
    error_occurred = pyqtSignal(str, str)  # source, error message
    source_changed = pyqtSignal(str)  # new source
    software_controller_changed = pyqtSignal(object) # software controller instance

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.current_worker: Optional[DMXWorkerBase] = None
        self.current_thread: Optional[QThread] = None
        self.current_source: Optional[str] = None
        self.software_controller: Optional[DMXController] = None
        self._bridge = StageFrameBridge()
        self._bridge_connected = False
        self.logger = get_logger("dmx.manager")

    # ------------------------------------------------------------------
    # Routing application
    # ------------------------------------------------------------------
    def apply_io(self, io: IOConfig) -> None:
        """Idempotent apply of the full I/O routing: controller lifecycle,
        controller output sinks, then the stage-input feed."""
        self._apply_controller(io)
        self._apply_sinks(io)
        self._apply_stage_input(io)

    def restart_stage_input(self) -> None:
        """Re-apply the stage-input leg (error-dialog retry hook)."""
        self._apply_stage_input(self.config.io)

    def get_software_controller(self) -> Optional[DMXController]:
        return self.software_controller

    def _apply_controller(self, io: IOConfig) -> None:
        if io.controller_enabled and self.software_controller is None:
            self.logger.info("Starting internal software controller")
            self.software_controller = DMXController(self.config.fixtures)
            self.software_controller.start_background()
            self.software_controller_changed.emit(self.software_controller)
        elif not io.controller_enabled and self.software_controller is not None:
            self.logger.info("Stopping internal software controller")
            controller = self.software_controller
            self.software_controller = None
            controller.set_sinks([])  # detach sinks before stopping the loop
            controller.stop()
            self.software_controller_changed.emit(None)

    def _apply_sinks(self, io: IOConfig) -> None:
        if self.software_controller is None:
            return
        sinks = []
        if io.dmx_to_stage:
            sinks.append(self._bridge)
        if io.dmx_to_artnet:
            sinks.append(ArtNetSender(
                ip=self.config.artnet.ip,
                port=self.config.artnet.port,
                universe=self.config.artnet.universe))
        if io.dmx_to_usb and io.usb_out_device:
            sinks.append(USBDMXSender(io.usb_out_device))
        self.software_controller.set_sinks(sinks)

    def _apply_stage_input(self, io: IOConfig) -> None:
        self.stop_worker()
        # Blank the stage so the previous feed's last frame doesn't linger
        # while the new one spins up.
        self.dmx_data_received.emit(bytearray(UNIVERSE_SIZE))

        self._set_bridge_connected(io.stage_source == "internal")

        if io.stage_source == "artnet":
            self._start_worker(ArtNetWorker(self.config))
        elif io.stage_source == "usb":
            if io.stage_usb_device:
                parts = io.stage_usb_device.split(":")
                device_id = parts[2] if len(parts) >= 3 else io.stage_usb_device
                self._start_worker(USBDMXWorker(device_id))
            else:
                self.logger.warning("Stage input set to USB but no device selected")

    def _set_bridge_connected(self, want: bool) -> None:
        if want and not self._bridge_connected:
            self._bridge.frame_received.connect(self.dmx_data_received.emit)
            self._bridge_connected = True
        elif not want and self._bridge_connected:
            self._bridge.frame_received.disconnect()
            self._bridge_connected = False

    # ------------------------------------------------------------------
    # Stage-input worker lifecycle
    # ------------------------------------------------------------------
    def _start_worker(self, worker: DMXWorkerBase) -> None:
        """Run a stage-input worker on its own QThread."""
        source = worker.get_source_name()

        thread = QThread()
        worker.moveToThread(thread)

        worker.dmx_data_received.connect(self.dmx_data_received.emit)
        worker.connection_status_changed.connect(
            lambda status: self.connection_status_changed.emit(source, status)
        )
        worker.error_occurred.connect(
            lambda error: self.error_occurred.emit(source, error)
        )

        thread.started.connect(worker.run)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self.current_worker = worker
        self.current_thread = thread
        self.current_source = source

        thread.start()
        self.source_changed.emit(source)
        self.logger.info(f"Started DMX worker for {source}")

    def stop_worker(self) -> None:
        """Stop the current stage-input worker."""
        if self.current_worker and self.current_thread:
            self.logger.info(f"Stopping DMX worker for {self.current_source}")

            # Stop the worker
            self.current_worker.stop()

            # Wait for thread to finish (with timeout)
            if self.current_thread.isRunning():
                self.current_thread.quit()
                self.current_thread.wait(5000)  # 5 second timeout

                if self.current_thread.isRunning():
                    self.logger.warning("DMX worker thread did not stop gracefully, terminating")
                    self.current_thread.terminate()
                    self.current_thread.wait(1000)

            # Clean up references
            self.current_worker = None
            self.current_thread = None
            self.current_source = None

            self.logger.info("DMX worker stopped")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def get_current_source(self) -> Optional[str]:
        """Get the current DMX source identifier."""
        return self.current_source

    def get_current_status(self) -> Optional[DMXConnectionStatus]:
        """Get the current connection status."""
        if self.current_worker:
            return self.current_worker.get_status()
        return None

    def is_connected(self) -> bool:
        """Check if currently connected to a DMX source."""
        status = self.get_current_status()
        return status == DMXConnectionStatus.CONNECTED if status else False

    def get_connection_info(self) -> Optional[dict]:
        """Get detailed connection information for current worker."""
        if self.current_worker and hasattr(self.current_worker, 'get_connection_info'):
            return self.current_worker.get_connection_info()
        return None

    def cleanup(self) -> None:
        """Clean up resources when shutting down."""
        self.stop_worker()
        if self.software_controller is not None:
            controller = self.software_controller
            self.software_controller = None
            controller.set_sinks([])
            controller.stop()
            self.software_controller_changed.emit(None)
