# ABOUTME: Thin entry point: parses --headless BEFORE any Qt import, loads
# ABOUTME: config + logging, and runs either ui.main_window.MainWindow (GUI)
# ABOUTME: or app.headless.run_headless (SBC/rack mode, web remote only).
import argparse
import os
import sys

from core.config import Config, userprefs_path
from core.paths import asset_path
from core import splash
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


def _stage_gpu_requested() -> bool:
    """Whether to ask Qt for an OpenGL surface, decided before Qt starts.

    The surface format has to be set before `QApplication` exists, which is
    before `Config.load()` has run, so this reads the single key it needs
    straight out of userprefs. Anything at all going wrong means "no": the
    raster stage is the answer that works everywhere.
    """
    import json
    # Resolved at call time, not bound at import: the module-level name would
    # be a private copy that a test redirecting the prefs directory cannot
    # reach, which is how the bug this guards against went unnoticed.
    from core.config import userprefs_path
    try:
        with open(userprefs_path(), "r", encoding="utf-8") as handle:
            window = json.load(handle).get("window", {})
        return bool(window.get("stage_gpu", False))
    except (OSError, ValueError, AttributeError):
        # No prefs yet, or unreadable: first launch looks exactly like this.
        return False
    except Exception as exc:
        # Anything else is a bug in here, not a user's missing file. Say so —
        # swallowing it silently turns "the GPU stage does nothing" into an
        # unexplainable report, which is precisely how this went wrong once.
        print(f"warning: could not read the stage renderer preference: {exc!r}",
              file=sys.stderr)
        return False


def _usb_dmx_devices() -> list:
    """The USB DMX dongles this machine can see, in --dmx-usb index order.

    Never cached: the whole point of `--dmx-usb auto` is that the question is
    re-asked at every boot, so a dongle swapped between shows is picked up on
    its own.
    """
    # Imported here rather than at module scope: it pulls in PyQt6.QtCore (and
    # pyserial), and the --headless decision below has to be made before any
    # Qt import happens.
    from dmx.usb_worker import USBDMXWorker
    return USBDMXWorker.list_available_devices()


def _print_usb_dmx_devices() -> int:
    """`--list-dmx-usb`: the dongles, 0-indexed the way --dmx-usb picks them.

    Headless has no Settings dialog, so this listing is the only way to learn
    the "manufacturer:product:serial" string that userprefs.json wants.
    """
    devices = _usb_dmx_devices()
    if not devices:
        print("No USB DMX devices detected (FTDI, USB vendor ID 0x0403).")
        return 0
    for index, device in enumerate(devices):
        print(f"{index}: {device}")
    return 0


def _resolve_dmx_usb(spec, devices):
    """Which device `--dmx-usb <spec>` names, as (device, error).

    (None, None) is the one benign miss: `auto` on a box with nothing plugged
    in is a rack box booting without its dongle, not a mistake. An explicit
    index that is not there IS one — somebody typed a number for hardware this
    machine does not have — and comes back as an error instead.
    """
    if spec == "auto":
        return (devices[0] if devices else None), None
    try:
        index = int(spec)
    except ValueError:
        return None, f"'{spec}' is neither a device index nor 'auto'"
    if not 0 <= index < len(devices):
        return None, (f"no USB DMX device at index {index} "
                      f"({len(devices)} detected — see --list-dmx-usb)")
    return devices[index], None


def _apply_dmx_usb(config, spec, logger) -> bool:
    """Point the software controller's DMX output at the device `--dmx-usb`
    names. False means the spec named nothing and the caller should stop.

    Applied in memory and never written back (Config.override_io): which
    dongle is plugged into this box today is a fact about the run, not a
    preference — persisting it would leave a box started once with the flag
    driving a device nobody ever selected.
    """
    device, error = _resolve_dmx_usb(spec, _usb_dmx_devices())
    if error:
        print(f"--dmx-usb: {error}", file=sys.stderr)
        logger.error(f"--dmx-usb {spec}: {error}")
        return False
    if device is None:
        # `auto` owns the USB output leg for the run: with nothing detected it
        # is OFF, rather than falling back to a device userprefs remembers and
        # this box may no longer have.
        config.override_io(dmx_to_usb=False, usb_out_device=None)
        logger.warning(
            "--dmx-usb auto: no USB DMX device detected — the controller's "
            "USB output is off for this run")
    else:
        config.override_io(dmx_to_usb=True, usb_out_device=device)
        logger.info(f"--dmx-usb {spec}: controller DMX out → {device} "
                    "(this run only — userprefs.json is not modified)")
    return True


def run_gui(argv, show_path=None, dmx_usb=None):
    # The boot splash is already up (the bootloader drew it while the onefile
    # archive unpacked); from here on it reports what we are actually doing.
    splash.update("Starting Qt...", 0.10)

    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication

    if _stage_gpu_requested():
        # Before QApplication, or it has no effect at all.
        from stage.gpu.layer import configure_surface_format
        configure_surface_format()

    app = QApplication(argv)
    # Wayland matches a window to its taskbar icon via the app_id (derived from
    # the desktop-file name), not the Qt window icon; this must match the
    # installed tupperdmx.desktop basename.
    app.setDesktopFileName("tupperdmx")
    app.setWindowIcon(QIcon(asset_path("icons", "TupperDMX-icon.png")))

    # Load and apply dark theme. At its designed size for now — the config
    # error dialog below is shown before there IS a config to read a UI scale
    # from, and it still has to be styled.
    from ui import theme
    app.setStyleSheet(theme.load_qss())

    splash.update("Loading configuration...", 0.30)
    try:
        config = Config.load()
    except Exception as e:
        # Show configuration error before main window is created. The splash is
        # always-on-top, so it has to go first or it sits over the dialog.
        splash.close()
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

    # Now the preference is readable, re-apply the theme at the user's scale.
    theme.apply_scale(app, config.window.ui_scale)

    _setup_logging(config)
    logger = get_logger("system.startup")
    logger.info("Starting TupperDMX Simulator")
    logger.info(f"Configuration loaded (data: config.json, prefs: {userprefs_path()})")
    logger.debug(f"Logging level: {config.logging.level}")

    if dmx_usb is not None and not _apply_dmx_usb(config, dmx_usb, logger):
        splash.close()   # always-on-top: it would outlive the process otherwise
        sys.exit(2)

    try:
        # Resolve the show project this machine had open, migrating the legacy
        # userprefs playlist/patch into a default show on first launch.
        splash.update("Opening the show...", 0.50)
        show = open_or_create_startup_show(config, explicit_path=show_path)
        logger.info(f"Show: {show.root}")

        splash.update("Building the interface...", 0.75)
        from ui.main_window import MainWindow
        main_window = MainWindow(config, show)

        splash.update("Ready", 1.0)
        # Down before the window comes up, never after: on Windows the splash
        # is a topmost, undecorated Tk window and would cover it.
        splash.close()
        main_window.show()

        logger.info("Application started successfully")

        sys.exit(app.exec())
    except KeyboardInterrupt:
        logger.info("Application interrupted by user")
    except Exception as e:
        logger.critical(f"Application crashed: {e}")

        # Show crash dialog if possible
        splash.close()
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


def run_headless_mode(argv, show_path=None, dmx_usb=None):
    # A rack box has no one to watch a splash, and may have no display at all.
    splash.close()

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

    if dmx_usb is not None and not _apply_dmx_usb(config, dmx_usb, logger):
        return 2

    try:
        # Imported here, not at module scope, so the failure lands after the
        # logging system is up: the whole Qt/audio/DMX graph is pulled in on
        # this line, and on a rack box a missing shared library shows up
        # nowhere else. run_gui has always had a crash dialog; headless had
        # neither that nor a log line, so a startup crash was silent.
        from app.headless import run_headless
        return run_headless(config, argv, show_path=show_path)
    except Exception:
        logger.critical("Headless startup failed", exc_info=True)
        raise


def main():
    parser = argparse.ArgumentParser(description="TupperDMX Simulator")
    parser.add_argument(
        "--headless", action="store_true",
        help="run without a GUI (SBC/rack mode): software controller, DMX/MIDI "
             "I/O, playback and the web remote — the remote is the only interface")
    parser.add_argument(
        "--show", metavar="PATH", default=None,
        help="open this Name.show/ folder instead of the one userprefs.json "
             "remembers, and remember it from then on. Headless has no show "
             "picker, so this is how a rack box is pointed at a show copied "
             "onto it; an unloadable path falls back to the usual resolution")
    parser.add_argument(
        "--list-dmx-usb", action="store_true",
        help="list the USB DMX (FTDI) devices this machine can see, 0-indexed "
             "the way --dmx-usb selects them, and exit")
    parser.add_argument(
        "--dmx-usb", metavar="INDEX|auto", default=None,
        help="send the software controller's DMX out of this USB device: an "
             "index from --list-dmx-usb, or 'auto' for the first one detected. "
             "Applies to this run only — userprefs.json is not rewritten. "
             "Headless has no Settings dialog, so this is how a rack box picks "
             "a dongle without hand-editing the prefs file")
    # Qt consumes its own arguments (-platform, -style, ...): pass them on.
    args, qt_args = parser.parse_known_args(sys.argv[1:])
    qt_argv = [sys.argv[0]] + qt_args

    # A listing is not a launch: nothing else is loaded, and it answers the
    # same on a box with no display and no show.
    if args.list_dmx_usb:
        sys.exit(_print_usb_dmx_devices())

    if args.headless:
        # Headless boxes have no display server. Must be set before ANY Qt
        # import (same recipe as tests/conftest.py); setdefault so an operator
        # can still pick another platform plugin explicitly.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        sys.exit(run_headless_mode(qt_argv, show_path=args.show,
                                   dmx_usb=args.dmx_usb))

    run_gui(qt_argv, show_path=args.show, dmx_usb=args.dmx_usb)


if __name__ == "__main__":
    main()
