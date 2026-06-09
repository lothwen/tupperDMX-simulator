# ABOUTME: SettingsDialog — modal editor for the user-writable preferences in
# ABOUTME: userprefs.json (Art-Net target, volumes, log level). Opened from the menu.

from PyQt6.QtWidgets import (QDialog, QFormLayout, QGroupBox, QVBoxLayout,
                             QLabel, QLineEdit, QSpinBox, QComboBox,
                             QDialogButtonBox, QMessageBox)

from config import Config


LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class SettingsDialog(QDialog):
    """Modal editor for the user-writable preferences in userprefs.json.

    Pure config editor: on accept it validates the inputs and writes them back
    into the provided Config object. Applying the changes live and persisting
    them (Config.save) is the caller's responsibility.
    """

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addWidget(self._build_artnet_group())
        layout.addWidget(self._build_logging_group())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # --- section builders ---------------------------------------------------

    def _build_artnet_group(self) -> QGroupBox:
        group = QGroupBox("Art-Net Output")
        form = QFormLayout(group)

        self.ip_edit = QLineEdit(self.config.artnet.ip)
        self.ip_edit.setPlaceholderText("127.0.0.1")

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(self.config.artnet.port)

        self.universe_spin = QSpinBox()
        self.universe_spin.setRange(0, 32767)
        self.universe_spin.setValue(self.config.artnet.universe)

        form.addRow("IP address:", self.ip_edit)
        form.addRow("Port:", self.port_spin)
        form.addRow("Universe:", self.universe_spin)
        hint = QLabel("Use 127.0.0.1 to drive the built-in software controller.")
        hint.setWordWrap(True)
        form.addRow(hint)
        return group

    def _build_logging_group(self) -> QGroupBox:
        group = QGroupBox("Logging")
        form = QFormLayout(group)

        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(LOG_LEVELS)
        current = (self.config.logging.level
                   if self.config.logging.level in LOG_LEVELS else "INFO")
        self.log_level_combo.setCurrentText(current)

        form.addRow("Level:", self.log_level_combo)
        hint = QLabel("Log level changes take effect on the next launch.")
        hint.setWordWrap(True)
        form.addRow(hint)
        return group

    # --- accept -------------------------------------------------------------

    @staticmethod
    def _is_valid_ipv4(text: str) -> bool:
        parts = text.split(".")
        if len(parts) != 4:
            return False
        try:
            return all(0 <= int(p) <= 255 for p in parts)
        except ValueError:
            return False

    def _on_accept(self) -> None:
        ip = self.ip_edit.text().strip()
        if not self._is_valid_ipv4(ip):
            QMessageBox.warning(
                self, "Invalid IP address",
                f"'{ip}' is not a valid IPv4 address (e.g. 127.0.0.1)."
            )
            return

        self.config.artnet.ip = ip
        self.config.artnet.port = self.port_spin.value()
        self.config.artnet.universe = self.universe_spin.value()
        self.config.logging.level = self.log_level_combo.currentText()
        self.accept()
