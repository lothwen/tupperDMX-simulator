# ABOUTME: The app's one way into the experimental 3D stage: builds the bar view for
# ABOUTME: the live patch, made safe to sit inside the main window in the 2D stage's place.
"""The 3D bar, embedded.

`BarView` was written to be a window of its own, so four things change when it
sits inside the main window instead:

  * **Esc and F** — the prototype closes / fullscreens `self.window()`, which
    here is the app; the app's own filter already gives both keys to the
    stage (leave fullscreen / stage fullscreen), so the view refuses them;
  * **no DMX feed** — `MainWindow.update_dmx` pushes the app's own frames, so
    nothing binds a second Art-Net socket or runs a second controller;
  * **changing windows** — fullscreen moves the view into the floating stage
    window, and Qt destroys a moved GL widget's context and builds another.
    `BarRenderer` keeps the GL objects of the context it first initialised
    against, so it is replaced — freed while the old context is still
    current — and the next `initializeGL` builds a fresh one;
  * **teardown** — the prototype has none; `dispose()` frees the renderer while
    its context is current.

Failure is soft, as the 2D stage's GPU path is: `create` returns None when the
scene cannot be built (the prototype raises SystemExit for a missing GLB or a
missing house-rig fixture), and `render_failed` fires when the GL context cannot
run it, so the window can put the 2D stage back.
"""

from typing import Optional, Sequence

from PyQt6.QtCore import Qt, pyqtSignal

from core.config import FixtureInstance
from stage.gpu.layer import surface_format
from stage.stage3d.bar import BarRenderer, BarView, Stage
from tupperlogger import get_logger

logger = get_logger("stage.stage3d")


class EmbeddedBarView(BarView):
    """`BarView` minus the parts that assume it is the whole application."""

    # Not `failed`: that name is BarView's own flag.
    render_failed = pyqtSignal()

    def __init__(self, stage: Stage):
        super().__init__(stage)
        # The layer's format (desktop GL; 4.1 core on macOS) plus the depth
        # buffer the prototype's own window asks for.
        fmt = surface_format()
        fmt.setDepthBufferSize(24)
        self.setFormat(fmt)
        self._gl_context = None

    def update_dmx(self, frame: bytearray) -> None:
        self.stage.patch.update_dmx(frame)

    def initializeGL(self) -> None:
        # Qt's pattern for a GL widget that may change windows (see the module
        # docstring): hear the context go before it goes.
        self._gl_context = self.context()
        self._gl_context.aboutToBeDestroyed.connect(self._release_gl)
        super().initializeGL()
        if self.failed:
            logger.warning("The 3D stage cannot render on this OpenGL context")
            self.render_failed.emit()

    def _release_gl(self) -> None:
        """The context is going: free the renderer's GL objects while it is
        still current, and leave a fresh renderer for the next one."""
        self._gl_context = None
        self.makeCurrent()
        self.renderer = BarRenderer(self.stage)
        self.doneCurrent()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_F):
            event.ignore()
            return
        super().keyPressEvent(event)

    def dispose(self) -> None:
        """Stop, free the GL objects while their context is current, and go."""
        self.timer.stop()
        # Not heard during deletion, when the widget is half torn down.
        if self._gl_context is not None:
            self._gl_context.aboutToBeDestroyed.disconnect(self._release_gl)
            self._gl_context = None
        self.makeCurrent()
        self.renderer = None
        self.failed = True          # paintGL returns early from here on
        self.doneCurrent()
        self.deleteLater()


def create(fixtures: Sequence[FixtureInstance]) -> Optional[EmbeddedBarView]:
    """The 3D view of `fixtures`, or None when it cannot be built."""
    try:
        return EmbeddedBarView(Stage(fixtures))
    except (Exception, SystemExit):
        logger.exception("The 3D stage could not be built")
        return None
