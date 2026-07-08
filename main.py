# ABOUTME: Thin entry point: builds the QApplication, loads config + logging,
# ABOUTME: and shows ui.main_window.MainWindow.
import sys
import os

from PyQt6.QtWidgets import QApplication

from core.config import Config, userprefs_path
from core.paths import asset_path
from tupperlogger import setup_logging, get_logger
from ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)

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

    # Initialize logging system
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
        logger = logging.getLogger("system.startup")
        logger.error(f"Failed to setup logging system: {e}")

    logger = get_logger("system.startup")
    logger.info("Starting TupperDMX Simulator")
    logger.info(f"Configuration loaded (data: config.json, prefs: {userprefs_path()})")
    logger.debug(f"Logging level: {config.logging.level}")

    try:
        main_window = MainWindow(config)
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


if __name__ == "__main__":
    main()
