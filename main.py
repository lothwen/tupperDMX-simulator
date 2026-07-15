# ABOUTME: Thin entry point: parses --headless BEFORE any Qt import, loads
# ABOUTME: config + logging, and runs either ui.main_window.MainWindow (GUI)
# ABOUTME: or app.headless.run_headless (SBC/rack mode, web remote only).
import argparse
import os
import sys

from core.config import Config, userprefs_path
from core.paths import asset_path
from core.show.migrate import open_or_create_startup_show
from tupperlogger import setup_logging, get_logger


def _setup_logging(config):
    """Initialize the logging system, falling back to basicConfig."""
    try:
        setup_logging(
            level=config.logging.level,
            console_output=config.logging.console_output,
            file_output=config.logging.file_output,
            log_dir=config.logging.log_dir,
            max_file_size=config.logging.max_file_size,
            backup_count=config.logging.backup_count
        )
    except Exception as e:
        # Fallback to basic logging if setup fails
        import logging
        logging.basicConfig(level=logging.INFO)
        logging.getLogger("system.startup").error(
            f"Failed to setup logging system: {e}")


def run_gui(argv):
    from PyQt6.QtWidgets import QApplication

    app = QApplication(argv)

    # Load and apply dark theme
    qss_path = asset_path("styles", "dark-theme.qss")
    if os.path.exists(qss_path):
        with open(qss_path, 'r', encoding='utf-8') as f:
            app.setStyleSheet(f.read())

    try:
        config = Config.load()
    except Exception as e:
        # Show configuration error before main window is created
        from ui.error_dialogs import ErrorDialogManager
        if ErrorDialogManager.show_configuration_error(None, str(e), can_reset=True):
            # User chose to reset: remove userprefs.json (shipped config.json is read-only)
            try:
                prefs = userprefs_path()
                if prefs.exists():
                    os.remove(prefs)
                config = Config.load()  # Will recreate userprefs.json with defaults
            except Exception as reset_error:
                # If we can't even reset, use default config in memory
                config = Config()
        else:
            # Use default configuration
            config = Config()

    _setup_logging(config)
    logger = get_logger("system.startup")
    logger.info("Starting TupperDMX Simulator")
    logger.info(f"Configuration loaded (data: config.json, prefs: {userprefs_path()})")
    logger.debug(f"Logging level: {config.logging.level}")

    try:
        # Resolve the show project this machine had open, migrating the legacy
        # userprefs playlist/patch into a default show on first launch.
        show = open_or_create_startup_show(config)
        logger.info(f"Show: {show.root}")

        from ui.main_window import MainWindow
        main_window = MainWindow(config, show)
        main_window.show()

        logger.info("Application started successfully")

        sys.exit(app.exec())
    except KeyboardInterrupt:
        logger.info("Application interrupted by user")
    except Exception as e:
        logger.critical(f"Application crashed: {e}")

        # Show crash dialog if possible
        try:
            from ui.error_dialogs import ErrorDialog
            crash_dialog = ErrorDialog(
                None,
                "Application Crash",
                "The application encountered an unexpected error and needs to close.",
                str(e),
                "error"
            )
            crash_dialog.exec()
        except Exception:
            pass  # If we can't show dialog, just exit

        raise


def run_headless_mode(argv):
    try:
        config = Config.load()
    except Exception as e:
        # No dialogs headless: log, run on in-memory defaults, and NEVER
        # delete the prefs file (that reset flow is a desktop decision).
        print(f"Failed to load configuration: {e} — using defaults",
              file=sys.stderr)
        config = Config()

    _setup_logging(config)
    logger = get_logger("system.startup")
    logger.info("Starting TupperDMX Simulator (headless)")
    logger.info(f"Configuration loaded (data: config.json, prefs: {userprefs_path()})")

    from app.headless import run_headless
    return run_headless(config, argv)


def main():
    parser = argparse.ArgumentParser(description="TupperDMX Simulator")
    parser.add_argument(
        "--headless", action="store_true",
        help="run without a GUI (SBC/rack mode): software controller, DMX/MIDI "
             "I/O, playback and the web remote — the remote is the only interface")
    # Qt consumes its own arguments (-platform, -style, ...): pass them on.
    args, qt_args = parser.parse_known_args(sys.argv[1:])
    qt_argv = [sys.argv[0]] + qt_args

    if args.headless:
        # Headless boxes have no display server. Must be set before ANY Qt
        # import (same recipe as tests/conftest.py); setdefault so an operator
        # can still pick another platform plugin explicitly.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        sys.exit(run_headless_mode(qt_argv))

    run_gui(qt_argv)


if __name__ == "__main__":
    main()
