"""
Characterization test: DMXController frame-sink fan-out.

Exercises the sink seam directly (no background thread, no Art-Net, no
hardware): _send_frame_to_sinks() fans one snapshot to every sink, a raising
sink is isolated (logged once, others still served), and set_sinks() closes
the sinks that drop out of the new list. The output loop's two steps —
tick_timers() and tick_frame() — run on the controller's own clock, which is
what lets an offline replay drive one without touching a live one.

Run with:
    uv run python tests/test_controller_sinks.py
"""

import os
import random
import sys
from types import SimpleNamespace

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


def test_timers_run_on_the_controllers_own_clock():
    print("test_timers_run_on_the_controllers_own_clock")
    clock = SimpleNamespace(now=100.0)
    controller = DMXController(clock=lambda: clock.now, rng=random.Random(0))
    fx = next(i for i, s in enumerate(controller.fixtures) if s.type.chases)
    controller.load_chase(fx, 0)
    step_time = controller.fixtures[fx].chase_change_time / 1000.0

    controller.tick_timers(100.0 + step_time / 2)
    _assert(controller.fixtures[fx].chase_index == 0,
            "half a step in, the chase holds its first scene")
    controller.tick_timers(100.0 + step_time)
    _assert(controller.fixtures[fx].chase_index == 1,
            "a whole step on the injected clock moves it on — the chase was "
            "armed on that clock, not on the wall's")

    controller.blackout_all_state = True
    _assert(controller.tick_frame(50) == [0] * UNIVERSE_SIZE,
            "a blacked-out frame is all zero")


def main():
    test_fanout_to_all_sinks()
    test_raising_sink_is_isolated()
    test_set_sinks_closes_removed()
    test_timers_run_on_the_controllers_own_clock()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
