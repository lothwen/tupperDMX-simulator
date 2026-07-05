# ABOUTME: SettingsDialog — modal editor for the user-writable preferences in
# ABOUTME: userprefs.json (I/O routing, Art-Net target, log level). Opened from the menu.

from PyQt6.QtWidgets import (QDialog, QFormLayout, QGroupBox, QVBoxLayout,
                             QHBoxLayout, QLabel, QLineEdit, QSpinBox,
                             QComboBox, QCheckBox, QRadioButton, QButtonGroup,
                             QDialogButtonBox, QMessageBox, QWidget)

import io_routing
from config import Config, IOConfig
from midi.worker import MidiWorker
from dmx.usb_worker import USBDMXWorker


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
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addWidget(self._build_io_group())
        layout.addWidget(self._build_logging_group())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._widgets_from_io(config.io)
        self._on_io_changed()

    # --- Input / Output section ----------------------------------------------

    def _build_io_group(self) -> QGroupBox:
        group = QGroupBox("Input / Output")
        layout = QVBoxLayout(group)
        layout.setSpacing(10)

        # Quick-mode preset
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset:"))
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(io_routing.PRESET_NAMES)
        self.preset_combo.activated.connect(self._on_preset_selected)
        preset_row.addWidget(self.preset_combo, 1)
        layout.addLayout(preset_row)

        layout.addWidget(self._build_sequencer_group())
        layout.addWidget(self._build_controller_group())
        layout.addWidget(self._build_stage_group())

        # Resulting-flow strip + soft warnings, refreshed live.
        self.flow_label = QLabel()
        self.flow_label.setWordWrap(True)
        self.flow_label.setObjectName("IOFlowLabel")
        layout.addWidget(self.flow_label)

        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #d9a13b;")
        self.warning_label.hide()
        layout.addWidget(self.warning_label)

        return group

    def _build_sequencer_group(self) -> QGroupBox:
        group = QGroupBox("1 · Sequencer (timeline)")
        layout = QVBoxLayout(group)
        layout.addWidget(QLabel("MIDI outputs (any combination):"))

        self.midi_ctrl_check = QCheckBox("Internal controller")
        self.midi_ctrl_check.toggled.connect(self._on_io_changed)
        layout.addWidget(self.midi_ctrl_check)

        port_row = QHBoxLayout()
        self.midi_port_check = QCheckBox("External MIDI port")
        self.midi_port_check.toggled.connect(self._on_io_changed)
        port_row.addWidget(self.midi_port_check)
        self.midi_port_combo = QComboBox()
        self.midi_port_combo.currentIndexChanged.connect(self._on_io_changed)
        port_row.addWidget(self.midi_port_combo, 1)
        layout.addLayout(port_row)

        return group

    def _build_controller_group(self) -> QGroupBox:
        group = QGroupBox("2 · Internal DMX controller")
        group.setCheckable(True)
        group.toggled.connect(self._on_io_changed)
        self.controller_group = group
        layout = QVBoxLayout(group)

        layout.addWidget(QLabel("MIDI input: sequencer (fixed)"))
        layout.addWidget(QLabel("DMX outputs (any combination):"))

        self.stage_out_check = QCheckBox("Virtual stage")
        self.stage_out_check.toggled.connect(self._on_io_changed)
        layout.addWidget(self.stage_out_check)

        self.artnet_out_check = QCheckBox("Art-Net")
        self.artnet_out_check.toggled.connect(self._on_io_changed)
        layout.addWidget(self.artnet_out_check)

        # Art-Net target (also carries the listen port/universe for input 3).
        self.artnet_form = QWidget()
        form = QFormLayout(self.artnet_form)
        form.setContentsMargins(24, 0, 0, 0)
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
        layout.addWidget(self.artnet_form)

        usb_row = QHBoxLayout()
        self.usb_out_check = QCheckBox("USB DMX output")
        self.usb_out_check.toggled.connect(self._on_io_changed)
        usb_row.addWidget(self.usb_out_check)
        self.usb_out_combo = QComboBox()
        self.usb_out_combo.currentIndexChanged.connect(self._on_io_changed)
        usb_row.addWidget(self.usb_out_combo, 1)
        layout.addLayout(usb_row)

        return group

    def _build_stage_group(self) -> QGroupBox:
        group = QGroupBox("3 · Virtual stage")
        layout = QVBoxLayout(group)
        layout.addWidget(QLabel("DMX input:"))

        self.stage_internal_radio = QRadioButton("Internal controller")
        self.stage_artnet_radio = QRadioButton("Art-Net (listen)")
        usb_row = QHBoxLayout()
        self.stage_usb_radio = QRadioButton("USB DMX read")
        usb_row.addWidget(self.stage_usb_radio)
        self.stage_usb_combo = QComboBox()
        self.stage_usb_combo.currentIndexChanged.connect(self._on_io_changed)
        usb_row.addWidget(self.stage_usb_combo, 1)

        # Explicit button group: the USB radio sits inside a row container, so
        # parent-based auto-exclusivity would not cover all three.
        self.stage_source_group = QButtonGroup(self)
        for radio in (self.stage_internal_radio, self.stage_artnet_radio,
                      self.stage_usb_radio):
            self.stage_source_group.addButton(radio)
            radio.toggled.connect(self._on_io_changed)

        layout.addWidget(self.stage_internal_radio)
        layout.addWidget(self.stage_artnet_radio)
        layout.addLayout(usb_row)
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

    # --- widgets <-> IOConfig -------------------------------------------------

    @staticmethod
    def _populate_combo(combo: QComboBox, items, selected) -> None:
        """Fill a combo with name/data pairs; a persisted-but-absent selection
        is appended with a '(not connected)' suffix so it isn't silently lost
        (currentData() always carries the raw name)."""
        combo.blockSignals(True)
        combo.clear()
        for name in items:
            combo.addItem(name, name)
        if selected:
            idx = combo.findData(selected)
            if idx < 0:
                combo.addItem(f"{selected} (not connected)", selected)
                idx = combo.count() - 1
            combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _widgets_from_io(self, io: IOConfig) -> None:
        """Load an IOConfig into the controls (without preset feedback loops)."""
        for w in (self.midi_ctrl_check, self.midi_port_check,
                  self.controller_group, self.stage_out_check,
                  self.artnet_out_check, self.usb_out_check,
                  self.stage_internal_radio, self.stage_artnet_radio,
                  self.stage_usb_radio):
            w.blockSignals(True)

        self.midi_ctrl_check.setChecked(io.midi_to_controller)
        self.midi_port_check.setChecked(io.midi_to_port)
        self.controller_group.setChecked(io.controller_enabled)
        self.stage_out_check.setChecked(io.dmx_to_stage)
        self.artnet_out_check.setChecked(io.dmx_to_artnet)
        self.usb_out_check.setChecked(io.dmx_to_usb)
        self.stage_internal_radio.setChecked(io.stage_source == "internal")
        self.stage_artnet_radio.setChecked(io.stage_source == "artnet")
        self.stage_usb_radio.setChecked(io.stage_source == "usb")

        for w in (self.midi_ctrl_check, self.midi_port_check,
                  self.controller_group, self.stage_out_check,
                  self.artnet_out_check, self.usb_out_check,
                  self.stage_internal_radio, self.stage_artnet_radio,
                  self.stage_usb_radio):
            w.blockSignals(False)

        midi_ports = MidiWorker.get_available_ports()
        usb_devices = USBDMXWorker.list_available_devices()
        self._populate_combo(self.midi_port_combo, midi_ports, io.midi_port)
        self._populate_combo(self.usb_out_combo, usb_devices, io.usb_out_device)
        self._populate_combo(self.stage_usb_combo, usb_devices, io.stage_usb_device)

    def _io_from_widgets(self) -> IOConfig:
        if self.stage_artnet_radio.isChecked():
            stage_source = "artnet"
        elif self.stage_usb_radio.isChecked():
            stage_source = "usb"
        else:
            stage_source = "internal"
        return IOConfig(
            midi_to_controller=self.midi_ctrl_check.isChecked(),
            midi_to_port=self.midi_port_check.isChecked(),
            midi_port=self.midi_port_combo.currentData(),
            controller_enabled=self.controller_group.isChecked(),
            dmx_to_stage=self.stage_out_check.isChecked(),
            dmx_to_artnet=self.artnet_out_check.isChecked(),
            dmx_to_usb=self.usb_out_check.isChecked(),
            usb_out_device=self.usb_out_combo.currentData(),
            stage_source=stage_source,
            stage_usb_device=self.stage_usb_combo.currentData(),
        )

    # --- live feedback --------------------------------------------------------

    def _on_preset_selected(self, index: int) -> None:
        name = self.preset_combo.itemText(index)
        self._widgets_from_io(io_routing.apply_preset(name, self._io_from_widgets()))
        self._on_io_changed()

    def _on_io_changed(self, *_args) -> None:
        io = self._io_from_widgets()

        # Dependent-control enablement
        self.midi_port_combo.setEnabled(self.midi_port_check.isChecked())
        self.artnet_form.setEnabled(self.artnet_out_check.isChecked())
        self.usb_out_combo.setEnabled(self.usb_out_check.isChecked())
        self.stage_usb_combo.setEnabled(self.stage_usb_radio.isChecked())

        # Preset feedback (without re-triggering apply)
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentText(io_routing.match_preset(io))
        self.preset_combo.blockSignals(False)

        # Flow strip + soft warnings
        self.flow_label.setText(io_routing.flow_text(io))
        _, warnings = io_routing.validate(io)
        self.warning_label.setText("\n".join(f"⚠ {w}" for w in warnings))
        self.warning_label.setVisible(bool(warnings))

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
        io = self._io_from_widgets()

        ip = self.ip_edit.text().strip()
        if io.dmx_to_artnet and not self._is_valid_ipv4(ip):
            QMessageBox.warning(
                self, "Invalid IP address",
                f"'{ip}' is not a valid IPv4 address (e.g. 192.168.1.50)."
            )
            return

        errors, _ = io_routing.validate(io)
        if errors:
            QMessageBox.warning(self, "Invalid I/O routing", "\n".join(errors))
            return

        if self._is_valid_ipv4(ip):
            self.config.artnet.ip = ip
        self.config.artnet.port = self.port_spin.value()
        self.config.artnet.universe = self.universe_spin.value()
        self.config.io = io
        self.config.logging.level = self.log_level_combo.currentText()
        self.accept()
