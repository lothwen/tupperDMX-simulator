import asyncio
import threading
import queue
from typing import Optional
from pyartnet import ArtNetNode

from config import Config
from tupperlogger import get_logger
from .base import DMXWorkerBase, DMXConnectionStatus


class ArtNetSim(ArtNetNode):
    """Custom Art-Net Node for simulation that captures DMX data."""
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._sim_queue = queue.Queue()

    def _send_universe(self, universe: int, data_size: int, data: bytearray, universe_obj) -> None:
        """Override to capture DMX data instead of sending."""
        self._sim_queue.put(data)

    def get_dmx_data(self) -> Optional[bytearray]:
        """Get the next DMX data packet if available."""
        try:
            return self._sim_queue.get_nowait()
        except queue.Empty:
            return None

    async def start(self):
        """Start the Art-Net node."""
        self.start_refresh()

    async def stop(self):
        """Stop the Art-Net node."""
        self.stop_refresh()


class ArtNetWorker(DMXWorkerBase):
    """Worker for receiving DMX data via Art-Net protocol."""
    
    def __init__(self, config: Config):
        super().__init__(f"Art-Net ({config.artnet.ip}:{config.artnet.port})")
        self.config = config
        self.node: Optional[ArtNetSim] = None
        self.stop_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.logger = get_logger("dmx.artnet")

    def run(self) -> None:
        """Start the Art-Net worker in async event loop."""
        self._is_running = True
        self._set_status(DMXConnectionStatus.CONNECTING)
        
        # Create new event loop for this thread
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        try:
            self._loop.run_until_complete(self._async_worker())
        except Exception as e:
            self.logger.error(f"Art-Net worker error: {e}")
            self._emit_error(f"Art-Net worker error: {e}")
        finally:
            if self._loop:
                self._loop.close()
            self._is_running = False
            self._set_status(DMXConnectionStatus.DISCONNECTED)
            self.logger.info("Art-Net worker stopped")

    async def _async_worker(self) -> None:
        """Async worker function."""
        try:
            # Initialize Art-Net node
            self.logger.info(f"Connecting to Art-Net at {self.config.artnet.ip}:{self.config.artnet.port}")
            self.node = ArtNetSim(
                ip=self.config.artnet.ip, 
                port=self.config.artnet.port
            )
            self.node.add_universe(self.config.artnet.universe)
            
            # Start the node
            await self.node.start()
            self._set_status(DMXConnectionStatus.CONNECTED)
            self.logger.info(f"Art-Net connected, listening on universe {self.config.artnet.universe}")
            
            # Main data processing loop
            while not self.stop_event.is_set():
                dmx_data = self.node.get_dmx_data()
                if dmx_data:
                    self.dmx_data_received.emit(dmx_data)
                    self.logger.debug(f"Received DMX data: {len(dmx_data)} bytes")
                
                # Small delay to prevent excessive CPU usage
                await asyncio.sleep(0.01)
                
        except Exception as e:
            self.logger.error(f"Art-Net connection error: {e}")
            self._emit_error(f"Art-Net connection error: {e}")
        finally:
            # Clean shutdown
            if self.node:
                try:
                    await self.node.stop()
                    self.logger.debug("Art-Net node stopped")
                except Exception as e:
                    self.logger.warning(f"Error stopping Art-Net node: {e}")
                self.node = None

    def stop(self) -> None:
        """Stop the Art-Net worker."""
        self.stop_event.set()
        
        # If we have a running loop, schedule the shutdown
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._schedule_shutdown)
    
    def _schedule_shutdown(self) -> None:
        """Schedule shutdown in the event loop."""
        if self.node:
            asyncio.create_task(self._shutdown())
    
    async def _shutdown(self) -> None:
        """Async shutdown helper."""
        if self.node:
            await self.node.stop()
            
    def get_connection_info(self) -> dict:
        """Get detailed connection information."""
        return {
            'type': 'Art-Net',
            'ip': self.config.artnet.ip,
            'port': self.config.artnet.port,
            'universe': self.config.artnet.universe,
            'status': self.status.value
        }