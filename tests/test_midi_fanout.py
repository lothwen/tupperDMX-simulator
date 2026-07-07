"""
Characterization test: MidiWorker output fan-out.

Drives _play_messages_until() directly on an unstarted worker (no thread, no
real MIDI ports) with a fake port and a fake controller injected, covering:
controller-only routing, port-only routing, simultaneous fan-out to both,
muted-channel skipping, and the message-type filter per destination (the
controller consumes note_on verbs only; the port receives the full stream).
Also covers _connect_midi_port()'s no-port routing decisions.

Run with:
    uv run python tests/test_midi_fanout.py
"""

import sys
import os

import mido

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from midi.worker import MidiWorker


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


class FakePort:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


class FakeController:
    def __init__(self):
        self.notes = []

    def handle_note_on(self, channel, note, velocity):
        self.notes.append((channel, note, velocity))


def _make_worker(route_to_controller=False, port=None):
    """Unstarted worker with an empty MidiFile; messages are set directly."""
    worker = MidiWorker("", None, mido.MidiFile(),
                        route_to_controller=route_to_controller)
    worker.port = port
    return worker


_MESSAGES = [
    (0.0, mido.Message('note_on', channel=0, note=60, velocity=100)),
    (0.1, mido.Message('note_off', channel=0, note=60, velocity=0)),
    (0.2, mido.Message('control_change', channel=1, control=7, value=64)),
    (0.3, mido.Message('note_on', channel=1, note=24, velocity=50)),
]


def test_controller_only():
    print("test_controller_only")
    worker = _make_worker(route_to_controller=True)
    controller = FakeController()
    worker.set_software_controller(controller)
    worker.messages = list(_MESSAGES)

    worker._play_messages_until(1.0)
    _assert(controller.notes == [(0, 60, 100), (1, 24, 50)],
            "controller receives only the note_on verbs")


def test_port_only():
    print("test_port_only")
    port = FakePort()
    worker = _make_worker(port=port)
    worker.messages = list(_MESSAGES)

    worker._play_messages_until(1.0)
    _assert(len(port.sent) == 4, "port receives the full stream")
    _assert([m.type for m in port.sent]
            == ['note_on', 'note_off', 'control_change', 'note_on'],
            "port receives all message types in order")


def test_fanout_to_both():
    print("test_fanout_to_both")
    port = FakePort()
    controller = FakeController()
    worker = _make_worker(route_to_controller=True, port=port)
    worker.set_software_controller(controller)
    worker.messages = list(_MESSAGES)

    worker._play_messages_until(1.0)
    _assert(controller.notes == [(0, 60, 100), (1, 24, 50)],
            "fan-out: controller leg receives its note_on verbs")
    _assert(len(port.sent) == 4, "fan-out: port leg receives the full stream")


def test_muted_channel_skipped_everywhere():
    print("test_muted_channel_skipped_everywhere")
    port = FakePort()
    controller = FakeController()
    worker = _make_worker(route_to_controller=True, port=port)
    worker.set_software_controller(controller)
    worker.messages = list(_MESSAGES)
    worker.muted_channels = {1}

    worker._play_messages_until(1.0)
    _assert(controller.notes == [(0, 60, 100)],
            "muted channel suppressed on the controller leg")
    _assert(all(m.channel == 0 for m in port.sent),
            "muted channel suppressed on the port leg")


def test_time_window_respected():
    print("test_time_window_respected")
    controller = FakeController()
    worker = _make_worker(route_to_controller=True)
    worker.set_software_controller(controller)
    worker.messages = list(_MESSAGES)

    worker._play_messages_until(0.05)
    _assert(controller.notes == [(0, 60, 100)],
            "only messages up to the requested time are played")
    worker._play_messages_until(1.0)
    _assert(controller.notes == [(0, 60, 100), (1, 24, 50)],
            "later calls resume where playback left off")


def test_connect_without_port():
    print("test_connect_without_port")
    worker = _make_worker(route_to_controller=True)
    _assert(worker._connect_midi_port() is True,
            "controller-only routing connects without a port")

    worker = _make_worker(route_to_controller=False)
    _assert(worker._connect_midi_port() is False,
            "no routes at all refuses to connect")


def main():
    test_controller_only()
    test_port_only()
    test_fanout_to_both()
    test_muted_channel_skipped_everywhere()
    test_time_window_respected()
    test_connect_without_port()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
