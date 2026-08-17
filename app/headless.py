# ABOUTME: Headless runner — boots AppCore without any GUI (SBC/rack mode):
# ABOUTME: software controller, DMX/MIDI I/O, playback, show loading and the
# ABOUTME: web remote, which is the only interface. main.py sets
# ABOUTME: QT_QPA_PLATFORM=offscreen before any Qt import.
import signal
import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QGuiApplication

from app.core import AppCore
from core.show.migrate import open_or_create_startup_show
from tupperlogger import get_logger
from webremote import state as remote_state


def run_headless(config, argv=None, show_path=None) -> int:
    """Run the application core without a window until SIGINT/SIGTERM.

    QGuiApplication on purpose (not QApplication): QtMultimedia needs a
    gui-level app but no widgets, and any widget accidentally pulled into
    the app/ import graph hard-fails here instead of silently reintroducing
    UI coupling. (If a target's Qt build misbehaves offscreen, QApplication
    is the CI-tested fallback — a one-line change.)
    """
    logger = get_logger("app.headless")

    # Resolve the show project this machine had open (dialog-free by design;
    # a song whose audio went missing keeps its manifest path for a later
    # desktop relink and simply won't play).
    show = open_or_create_startup_show(config, explicit_path=show_path)
    logger.info(f"Show: {show.root}")

    app = QGuiApplication(list(argv) if argv else [sys.argv[0]])

    # The web remote is the ONLY interface headless: force it on when prefs
    # disabled it (in memory only — the prefs file is never modified).
    force_webremote = not config.webremote.enabled
    if force_webremote:
        logger.warning(
            "Web remote disabled in prefs — force-enabling on port "
            f"{config.webremote.port}: it is the only interface in headless mode")

    core = AppCore(config, show, force_webremote=force_webremote)
    core.start()
    logger.info(
        "Headless mode up — web remote at "
        f"http://{remote_state.lan_ip()}:{config.webremote.port}")
    if config.io.stage_source != "internal":
        logger.info(
            f"Stage input '{config.io.stage_source}' feeds no stage view in "
            "headless mode")

    # SIGINT/SIGTERM → the same shutdown path as closing the window. Python
    # signal handlers only run between bytecode instructions, so a keep-alive
    # timer wakes the Qt loop periodically to service them.
    def _shutdown(signum, _frame):
        logger.info(
            f"Signal {signal.Signals(signum).name} received — shutting down")
        core.shutdown()
        app.quit()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    keepalive = QTimer()
    keepalive.setInterval(200)
    keepalive.timeout.connect(lambda: None)
    keepalive.start()

    rc = app.exec()
    core.shutdown()  # idempotent: covers a quit not driven by a signal
    return rc
