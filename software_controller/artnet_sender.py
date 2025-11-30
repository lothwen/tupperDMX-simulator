import socket
import struct
from tupperlogger import get_logger

class ArtNetSender:
    """
    Handles sending DMX data via Art-Net protocol (UDP).
    """
    def __init__(self, ip="127.0.0.1", port=6454, universe=0):
        self.logger = get_logger("software_controller.artnet_sender")
        self.ip = ip
        self.port = port
        self.universe = universe
        self.sequence = 0
        
        # Create UDP socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        
        self.logger.info(f"ArtNet Sender initialized for {self.ip}:{self.port} Universe {self.universe}")

    def send_dmx(self, data):
        """
        Send DMX data as an Art-Net packet.
        
        Args:
            data (list or bytearray): DMX channel values (0-255)
        """
        try:
            # Ensure data is bytearray
            if isinstance(data, list):
                data = bytearray(data)
            
            # Cap data at 512 bytes
            if len(data) > 512:
                data = data[:512]
            
            # Art-Net header
            # ID: "Art-Net\0"
            # OpCode: OpOutput (0x5000) -> Little Endian: 0x00, 0x50
            # ProtVer: 14 -> Big Endian: 0x00, 0x0e
            header = b'Art-Net\x00' + b'\x00\x50' + b'\x00\x0e'
            
            # Sequence (1 byte)
            self.sequence = (self.sequence + 1) % 256
            sequence_byte = bytes([self.sequence])
            
            # Physical (1 byte)
            physical = b'\x00'
            
            # Universe (2 bytes, Little Endian)
            # Bit 15: 0
            # Bit 14-8: Net
            # Bit 7-4: Sub-Net
            # Bit 3-0: Universe
            # For simplicity, we just pack the universe number as little endian short
            universe_bytes = struct.pack('<H', self.universe)
            
            # Length (2 bytes, Big Endian)
            length_bytes = struct.pack('>H', len(data))
            
            # Construct packet
            packet = header + sequence_byte + physical + universe_bytes + length_bytes + data
            
            # Send
            self.sock.sendto(packet, (self.ip, self.port))
            
        except Exception as e:
            self.logger.error(f"Error sending ArtNet packet: {e}")

    def close(self):
        """Close the socket."""
        if self.sock:
            self.sock.close()
            self.logger.info("ArtNet Sender socket closed")
