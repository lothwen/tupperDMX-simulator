"""
Characterization test: DMXController frame-sink fan-out.

Exercises the sink seam directly (no background thread, no Art-Net, no
hardware): _send_frame_to_sinks() fans one snapshot to every sink, a raising
sink is isolated (logged once, others still served), and set_sinks() closes
the sinks that drop out of the new list.

Run with:
    uv run python tests/test_controller_sinks.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from software_controller.main import DMXController, UNIVERSE_SIZE


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


class FakeSink:
    def __init__(self):
        self.frames = []
        self.closed = False

    def send_dmx(self, data):
        self.frames.append(list(data))

    def close(self):
        self.closed = True


class RaisingSink(FakeSink):
    def send_dmx(self, data):
        raise RuntimeError("boom")


def test_fanout_to_all_sinks():
    print("test_fanout_to_all_sinks")
    controller = DMXController()
    a, b = FakeSink(), FakeSink()
    frame = [7] * UNIVERSE_SIZE

    controller._send_frame_to_sinks(frame, [a, b])
    _assert(a.frames == [frame] and b.frames == [frame],
            "both sinks receive the same frame")


def test_raising_sink_is_isolated():
    print("test_raising_sink_is_isolated")
    controller = DMXController()
    bad, good = RaisingSink(), FakeSink()
    frame = [1] * UNIVERSE_SIZE

    controller._send_frame_to_sinks(frame, [bad, good])
    controller._send_frame_to_sinks(frame, [bad, good])
    _assert(len(good.frames) == 2,
            "a raising sink never blocks the other sinks")
    _assert(len(controller._sink_errors) == 1,
            "the failing sink is recorded once (de-spammed)")


def test_set_sinks_closes_removed():
    print("test_set_sinks_closes_removed")
    controller = DMXController()
    a, b = FakeSink(), FakeSink()

    controller.set_sinks([a, b])
    _assert(not a.closed and not b.closed, "active sinks stay open")

    controller.set_sinks([b])
    _assert(a.closed, "a sink dropped from the list is closed")
    _assert(not b.closed, "a sink kept in the list stays open")

    controller.set_sinks([])
    _assert(b.closed, "clearing the sinks closes the remainder")


def main():
    test_fanout_to_all_sinks()
    test_raising_sink_is_isolated()
    test_set_sinks_closes_removed()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
