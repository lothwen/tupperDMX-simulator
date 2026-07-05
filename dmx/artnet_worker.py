import asyncio
import threading
import socket
import struct
import errno
from typing import Optional

from config import Config
from tupperlogger import get_logger
from .base import DMXWorkerBase, DMXConnectionStatus


class ArtNetProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_data_received, universe, logger):
        self.on_data_received = on_data_received
        self.universe = universe
        self.logger = logger

    def connection_made(self, transport):
        self.transport = transport
        self.logger.info("ArtNet UDP transport established")

    def datagram_received(self, data, addr):
        try:
            # Basic ArtNet validation
            if len(data) < 18:
                return
            
            # Check ID "Art-Net\0"
            if data[0:8] != b'Art-Net\x00':
                return
                
            # Check OpCode (OpOutput = 0x5000 -> 0x00, 0x50 little endian)
            opcode = struct.unpack('<H', data[8:10])[0]
            if opcode != 0x5000:
                return
                
            # Check Protocol Version (14 -> 0x00, 0x0e big endian)
            prot_ver = struct.unpack('>H', data[10:12])[0]
            if prot_ver < 14:
                return
                
            # Sequence (data[12]) - ignore
            # Physical (data[13]) - ignore
            
            # Universe (data[14:16] little endian)
            universe = struct.unpack('<H', data[14:16])[0]
            if universe != self.universe:
                return
                
            # Length (data[16:18] big endian)
            length = struct.unpack('>H', data[16:18])[0]
            
            # Data
            dmx_data = data[18:18+length]
            
            self.on_data_received(dmx_data)
            
        except Exception as e:
            self.logger.warning(f"Error parsing ArtNet packet: {e}")


class ArtNetWorker(DMXWorkerBase):
    """Worker for receiving DMX data via Art-Net protocol.

    Pure listener: binds all interfaces on the configured port and feeds the
    frames of the configured universe to dmx_data_received."""

    def __init__(self, config: Config):
        super().__init__(f"Art-Net (listen :{config.artnet.port})")
        self.config = config
        self.transport = None
        self.protocol = None
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
            # Start UDP listener on all interfaces (config.artnet.ip is the
            # controller's *output* target, not the listen address).
            self.logger.info(f"Listening for Art-Net on 0.0.0.0:{self.config.artnet.port}")

            self.transport, self.protocol = await self._loop.create_datagram_endpoint(
                lambda: ArtNetProtocol(self._on_dmx_data, self.config.artnet.universe, self.logger),
                local_addr=("0.0.0.0", self.config.artnet.port)
            )
            
            self._set_status(DMXConnectionStatus.CONNECTED)
            self.logger.info(f"Art-Net listener started on universe {self.config.artnet.universe}")
            
            # Main loop to keep the thread alive
            while not self.stop_event.is_set():
                await asyncio.sleep(0.1)
                
        except OSError as e:
            port = self.config.artnet.port
            if e.errno == errno.EADDRINUSE:
                msg = (f"Art-Net port {port} is already in use — another "
                       f"TupperDMX instance or Art-Net application may be running.")
            else:
                msg = f"Failed to bind Art-Net port {port}: {e}"
            self.logger.error(msg)
            self._emit_error(msg)
        except Exception as e:
            self.logger.error(f"Art-Net error: {e}")
            self._emit_error(f"Art-Net error: {e}")
        finally:
            # Clean shutdown
            if self.transport:
                self.transport.close()
                self.transport = None

    def _on_dmx_data(self, data):
        """Callback when DMX data is received."""
        if data:
            # Convert to bytearray if needed
            byte_data = bytearray(data)
            self.dmx_data_received.emit(byte_data)

    def stop(self) -> None:
        """Stop the Art-Net worker."""
        self.stop_event.set()

    def get_connection_info(self) -> dict:
        """Get detailed connection information."""
        return {
            'type': 'Art-Net',
            'bind': '0.0.0.0',
            'port': self.config.artnet.port,
            'universe': self.config.artnet.universe,
            'status': self.status.value
        }