# ABOUTME: One timer for the whole stage — everything that animates ticks on it.
"""The shared animation clock.

Strobes, spinning gobos, slewing moving heads and drifting haze all need to be
re-drawn between DMX frames. Giving each of them its own QTimer meant a rig of
twenty fixtures woke Python up a hundred times a second from sixty different
places; one clock ticks once and everybody reads the same `now()`, which also
makes every animation in the scene agree on the same phase instead of drifting
apart.

Subscribing is what starts it: with nothing to animate the timer is stopped, so
a stage of plain dimmers costs nothing.
"""

import inspect
import time
import weakref
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QTimer

TICK_HZ = 30


class AnimationClock:
    """A single timer that calls back everything currently animating.

    Deliberately not a QObject: there is a module-level instance of this, and
    `import ui.stage` can happen before the QApplication exists — a QObject
    built that early is invalidated the moment one is created. The QTimer it
    owns is made on first subscribe, by which time there certainly is an app.
    """

    def __init__(self):
        self._timer: Optional[QTimer] = None
        self._subscribers: Dict[int, "weakref.ref"] = {}
        self._next_token = 1
        self._now = time.monotonic()

    def _ensure_timer(self) -> QTimer:
        """The timer, made on demand and remade if Qt took it away.

        A QApplication going down deletes every QObject with it, and this
        object outlives one — a test session builds and drops several. Checking
        rather than assuming keeps the stage animating across that.
        """
        if self._timer is not None:
            try:
                self._timer.isActive()
            except RuntimeError:
                self._timer = None
        if self._timer is None:
            self._timer = QTimer()
            self._timer.setInterval(int(1000 / TICK_HZ))
            self._timer.timeout.connect(self._tick)
        return self._timer

    def now(self) -> float:
        """Seconds, monotonic. Every subscriber in one tick sees the same value."""
        return self._now

    def subscribe(self, callback: Callable[[], None]) -> int:
        """Register `callback`, returning the token that unsubscribes it.

        Held weakly. A body that leaves the stage without being cleaned up —
        dropped by a test, or freed while the scene was being torn down — would
        otherwise stay subscribed forever, and tick against a C++ object Qt has
        already deleted.
        """
        token = self._next_token
        self._next_token += 1
        self._subscribers[token] = (weakref.WeakMethod(callback)
                                    if inspect.ismethod(callback)
                                    else weakref.ref(callback))
        timer = self._ensure_timer()
        if not timer.isActive():
            self._now = time.monotonic()
            timer.start()
        return token

    def unsubscribe(self, token: int) -> None:
        self._subscribers.pop(token, None)
        if not self._subscribers and self._timer is not None:
            self._timer.stop()

    def tick_now(self) -> None:
        """Run one tick immediately. For tests, which can't wait on a timer."""
        self._tick()

    def _tick(self) -> None:
        self._now = time.monotonic()
        # Copy: a callback may remove a fixture, and with it its subscription.
        for token, reference in list(self._subscribers.items()):
            callback = reference()
            if callback is None:
                # Its owner is gone and nobody said so. Stop calling it.
                self._subscribers.pop(token, None)
                continue
            try:
                callback()
            except RuntimeError:
                # The Python object survives but Qt deleted the item underneath
                # it. Nothing left to draw, so drop the subscription rather than
                # raise out of a timer with no one to catch it.
                self._subscribers.pop(token, None)
        if not self._subscribers and self._timer is not None:
            self._timer.stop()


# The stage's clock. A module singleton because the scene preview and the stage
# both animate and there is no reason for them to tick separately.
CLOCK = AnimationClock()
