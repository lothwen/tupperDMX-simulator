import socket
import threading
import json
import json
from tupperlogger import get_logger



class DMXSocketServer:
    def __init__(self, host='127.0.0.1', port=5001):
        self.logger = get_logger("software_controller.dmx_socket")
        self.host = host

        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.clients = []

    def listen(self):
        self.sock.listen(5)
        self.logger.info(f"DMX server listening on {self.host}:{self.port}")
        threading.Thread(target=self.accept_clients).start()


    def accept_clients(self):
        while True:
            client, address = self.sock.accept()

            self.logger.info(f"DMX client connected from {address}")
            self.clients.append(client)

    def send_data(self, data):
        json_data = json.dumps(data) + '\n'
        for client in self.clients[:]:
            try:
                client.sendall(json_data.encode('utf-8'))
            except (socket.error, BrokenPipeError):
                self.clients.remove(client)
