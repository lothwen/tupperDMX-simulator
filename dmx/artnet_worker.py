import asyncio
import threading
import socket
import struct
from typing import Optional
from PyQt6.QtCore import pyqtSignal

from config import Config
from tupperlogger import get_logger
from .base import DMXWorkerBase, DMXConnectionStatus
from software_controller.main import DMXController


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
    """Worker for receiving DMX data via Art-Net protocol."""
    
    # Signal to notify when software controller instance changes
    software_controller_changed = pyqtSignal(object)
    
    def __init__(self, config: Config):
        super().__init__(f"Art-Net ({config.artnet.ip}:{config.artnet.port})")
        self.config = config
        self.transport = None
        self.protocol = None
        self.stop_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.logger = get_logger("dmx.artnet")
        self.software_controller: Optional[DMXController] = None

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
            # Check if we should start the software controller (local simulation)
            if self.config.artnet.ip in ["127.0.0.1", "localhost"]:
                self.logger.info("Starting local software controller for simulation")
                self.software_controller = DMXController()
                self.software_controller.start_background()
                self.software_controller_changed.emit(self.software_controller)
            
            # Start UDP listener
            self.logger.info(f"Listening for Art-Net at {self.config.artnet.ip}:{self.config.artnet.port}")
            
            # Bind to all interfaces if 0.0.0.0, otherwise specific IP
            # For localhost, we bind to 127.0.0.1
            bind_ip = self.config.artnet.ip
            if bind_ip == "localhost":
                bind_ip = "127.0.0.1"
                
            self.transport, self.protocol = await self._loop.create_datagram_endpoint(
                lambda: ArtNetProtocol(self._on_dmx_data, self.config.artnet.universe, self.logger),
                local_addr=(bind_ip, self.config.artnet.port)
            )
            
            self._set_status(DMXConnectionStatus.CONNECTED)
            self.logger.info(f"Art-Net listener started on universe {self.config.artnet.universe}")
            
            # Main loop to keep the thread alive
            while not self.stop_event.is_set():
                await asyncio.sleep(0.1)
                
        except OSError as e:
            self.logger.error(f"Failed to bind Art-Net port: {e}")
            self._emit_error(f"Failed to bind port {self.config.artnet.port}: {e}")
        except Exception as e:
            self.logger.error(f"Art-Net error: {e}")
            self._emit_error(f"Art-Net error: {e}")
        finally:
            # Clean shutdown
            if self.transport:
                self.transport.close()
                self.transport = None
                
            if self.software_controller:
                self.logger.info("Stopping local software controller")
                self.software_controller.stop()
                self.software_controller = None
                self.software_controller_changed.emit(None)

    def _on_dmx_data(self, data):
        """Callback when DMX data is received."""
        if data:
            # Convert to bytearray if needed
            byte_data = bytearray(data)
            self.dmx_data_received.emit(byte_data)

    def stop(self) -> None:
        """Stop the Art-Net worker."""
        self.stop_event.set()
        
        # If we have a running loop, schedule the shutdown
        # The loop is running _async_worker which checks stop_event
        pass
            
    def get_connection_info(self) -> dict:
        """Get detailed connection information."""
        return {
            'type': 'Art-Net',
            'ip': self.config.artnet.ip,
            'port': self.config.artnet.port,
            'universe': self.config.artnet.universe,
            'status': self.status.value
        }