# ABOUTME: SettingsDialog — modal editor for the user-writable preferences in
# ABOUTME: userprefs.json (I/O routing, Art-Net target, log level). Opened from
# ABOUTME: the menu; two-column Input/Output page with a live signal-flow graph.

from PyQt6.QtWidgets import (QDialog, QFormLayout, QGroupBox, QVBoxLayout,
                             QHBoxLayout, QLabel, QLineEdit, QSpinBox,
                             QComboBox, QCheckBox, QRadioButton, QButtonGroup,
                             QDialogButtonBox, QMessageBox, QWidget, QFrame,
                             QSizePolicy)
from PyQt6.QtCore import Qt, QVariantAnimation, QEasingCurve, QRectF, QPointF
from PyQt6.QtGui import QPainter, QColor, QBrush

import io_routing
from config import Config, IOConfig
from midi.worker import MidiWorker
from dmx.usb_worker import USBDMXWorker
from ui.io_flow_widget import IOFlowWidget


LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class _ToggleSwitch(QCheckBox):
    """An iOS-style sliding toggle. Stays a QCheckBox so isChecked()/setChecked()
    and the ``toggled`` signal work exactly like a checkbox; only the painting
    and the click target change. The knob animation is driven from
    checkStateSet() (not the ``toggled`` signal) so programmatic setChecked under
    blockSignals() still animates correctly."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(44, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pos = 1.0 if self.isChecked() else 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(120)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._anim.valueChanged.connect(self._on_anim)

    def _on_anim(self, value):
        self._pos = float(value)
        self.update()

    def checkStateSet(self):
        super().checkStateSet()
        target = 1.0 if self.isChecked() else 0.0
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(target)
        self._anim.start()

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()

        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        if self.isChecked():
            track = QColor("#4CAF50")
        else:
            track = QColor("#555555")
        if not enabled:
            track = track.darker(160)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)

        d = r.height() - 4
        x = r.left() + 2 + self._pos * (r.width() - d - 4)
        knob = QColor("#f0f0f0") if enabled else QColor("#999999")
        p.setBrush(QBrush(knob))
        p.drawEllipse(QPointF(x + d / 2, r.center().y()), d / 2, d / 2)
        p.end()


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
        self.setMinimumSize(880, 640)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("Input / Output")
        title.setObjectName("SettingsSectionTitle")
        layout.addWidget(title)

        layout.addLayout(self._build_preset_row())

        columns = QHBoxLayout()
        columns.setSpacing(14)

        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(self._build_sequencer_card())
        left.addWidget(self._build_controller_card())
        left.addWidget(self._build_stage_card())
        left.addWidget(self._build_logging_card())
        left.addStretch(1)
        columns.addLayout(left, 11)

        columns.addWidget(self._build_flow_panel(), 9)
        layout.addLayout(columns, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._widgets_from_io(config.io)
        self._on_io_changed()

    # --- card factory ---------------------------------------------------------

    def _make_card(self, number, title, subtitle, header_extra=None):
        """Rounded settings card with an optional circled-number badge, an
        uppercase title and a dim subtitle. Returns (frame, body_layout) for the
        caller to fill. ``header_extra`` is right-aligned in the header row.

        The badge stays flush-left as a header marker; the returned body layout
        is indented to line up under the title (skipped when there's no badge)."""
        frame = QFrame()
        frame.setObjectName("SettingsCard")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        if number is not None:
            badge = QLabel(number)
            badge.setObjectName("SettingsCardBadge")
            badge.setFixedSize(22, 22)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.addWidget(badge)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        title_label = QLabel(title)
        title_label.setObjectName("SettingsCardTitle")
        title_col.addWidget(title_label)
        if subtitle:
            sub_label = QLabel(subtitle)
            sub_label.setObjectName("SettingsCardSubtitle")
            title_col.addWidget(sub_label)
        header.addLayout(title_col)

        header.addStretch(1)
        if header_extra is not None:
            header.addWidget(header_extra)
        outer.addLayout(header)

        # Indent the body to align under the title (badge width 22 + spacing 8).
        body = QVBoxLayout()
        body.setContentsMargins(30 if number is not None else 0, 0, 0, 0)
        body.setSpacing(8)
        outer.addLayout(body)

        return frame, body

    def _build_preset_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Quick mode:"))
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(io_routing.PRESET_NAMES)
        self.preset_combo.activated.connect(self._on_preset_selected)
        row.addWidget(self.preset_combo)
        hint = QLabel("Presets set the complete routing; «Custom» unlocks the controls.")
        hint.setObjectName("SettingsHintLabel")
        row.addWidget(hint, 1)
        return row

    # --- cards ----------------------------------------------------------------

    def _build_sequencer_card(self) -> QFrame:
        card, body = self._make_card("1", "SEQUENCER", "timeline")

        hint = QLabel("MIDI outputs · several at once")
        hint.setObjectName("SettingsDimLabel")
        body.addWidget(hint)

        self.midi_ctrl_check = QCheckBox("Internal controller")
        self.midi_ctrl_check.toggled.connect(self._on_io_changed)
        body.addWidget(self.midi_ctrl_check)

        port_row = QHBoxLayout()
        self.midi_port_check = QCheckBox("MIDI port:")
        self.midi_port_check.toggled.connect(self._on_io_changed)
        port_row.addWidget(self.midi_port_check)
        self.midi_port_combo = QComboBox()
        self.midi_port_combo.currentIndexChanged.connect(self._on_io_changed)
        port_row.addWidget(self.midi_port_combo, 1)
        body.addLayout(port_row)

        return card

    def _build_controller_card(self) -> QFrame:
        header_extra = QWidget()
        hx = QHBoxLayout(header_extra)
        hx.setContentsMargins(0, 0, 0, 0)
        hx.setSpacing(8)
        self.controller_state_label = QLabel("Enabled")
        self.controller_state_label.setObjectName("SettingsToggleLabel")
        hx.addWidget(self.controller_state_label)
        self.controller_toggle = _ToggleSwitch()
        self.controller_toggle.toggled.connect(self._on_io_changed)
        hx.addWidget(self.controller_toggle)

        card, body = self._make_card("2", "INTERNAL CONTROLLER",
                                     None, header_extra=header_extra)

        # All greyable content lives in an inner container.
        self.controller_body = QWidget()
        inner = QVBoxLayout(self.controller_body)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(6)

        midi_in = QLabel("MIDI input: Sequencer (fixed)")
        midi_in.setObjectName("SettingsDimLabel")
        inner.addWidget(midi_in)

        dmx_hint = QLabel("DMX outputs · several at once")
        dmx_hint.setObjectName("SettingsDimLabel")
        inner.addWidget(dmx_hint)

        self.stage_out_check = QCheckBox("Virtual stage")
        self.stage_out_check.toggled.connect(self._on_io_changed)
        inner.addWidget(self.stage_out_check)

        artnet_row = QHBoxLayout()
        self.artnet_out_check = QCheckBox("Art-Net")
        self.artnet_out_check.toggled.connect(self._on_io_changed)
        artnet_row.addWidget(self.artnet_out_check)
        self.artnet_fields = QWidget()
        af = QHBoxLayout(self.artnet_fields)
        af.setContentsMargins(0, 0, 0, 0)
        af.setSpacing(6)
        self.ip_edit = QLineEdit(self.config.artnet.ip)
        self.ip_edit.setPlaceholderText("127.0.0.1")
        self.ip_edit.textChanged.connect(self._on_io_changed)
        af.addWidget(self.ip_edit, 1)
        port_lbl = QLabel(":")
        port_lbl.setObjectName("SettingsDimLabel")
        af.addWidget(port_lbl)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(self.config.artnet.port)
        self.port_spin.setFixedWidth(72)
        self.port_spin.valueChanged.connect(self._on_io_changed)
        af.addWidget(self.port_spin)
        uni_lbl = QLabel("universe")
        uni_lbl.setObjectName("SettingsDimLabel")
        af.addWidget(uni_lbl)
        self.universe_spin = QSpinBox()
        self.universe_spin.setRange(0, 32767)
        self.universe_spin.setValue(self.config.artnet.universe)
        self.universe_spin.setFixedWidth(60)
        self.universe_spin.valueChanged.connect(self._sync_universe)
        af.addWidget(self.universe_spin)
        artnet_row.addWidget(self.artnet_fields, 1)
        inner.addLayout(artnet_row)

        usb_row = QHBoxLayout()
        self.usb_out_check = QCheckBox("USB DMX")
        self.usb_out_check.toggled.connect(self._on_io_changed)
        usb_row.addWidget(self.usb_out_check)
        self.usb_out_combo = QComboBox()
        self.usb_out_combo.currentIndexChanged.connect(self._on_io_changed)
        usb_row.addWidget(self.usb_out_combo, 1)
        inner.addLayout(usb_row)

        body.addWidget(self.controller_body)
        return card

    def _build_stage_card(self) -> QFrame:
        card, body = self._make_card("3", "VIRTUAL STAGE", "DMX input · only one")

        self.stage_internal_radio = QRadioButton("Internal controller")

        artnet_row = QHBoxLayout()
        self.stage_artnet_radio = QRadioButton("Art-Net · listen")
        artnet_row.addWidget(self.stage_artnet_radio)
        uni_lbl = QLabel("universe")
        uni_lbl.setObjectName("SettingsDimLabel")
        artnet_row.addWidget(uni_lbl)
        self.stage_universe_spin = QSpinBox()
        self.stage_universe_spin.setRange(0, 32767)
        self.stage_universe_spin.setValue(self.config.artnet.universe)
        self.stage_universe_spin.setFixedWidth(60)
        self.stage_universe_spin.valueChanged.connect(self._sync_universe)
        artnet_row.addWidget(self.stage_universe_spin)
        artnet_row.addStretch(1)

        usb_row = QHBoxLayout()
        self.stage_usb_radio = QRadioButton("USB DMX · read from bus")
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

        body.addWidget(self.stage_internal_radio)
        body.addLayout(artnet_row)
        body.addLayout(usb_row)
        return card

    def _build_logging_card(self) -> QFrame:
        card, body = self._make_card(None, "LOGGING", None)

        row = QHBoxLayout()
        row.addWidget(QLabel("Level:"))
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(LOG_LEVELS)
        current = (self.config.logging.level
                   if self.config.logging.level in LOG_LEVELS else "INFO")
        self.log_level_combo.setCurrentText(current)
        row.addWidget(self.log_level_combo)
        hint = QLabel("Applies on next launch.")
        hint.setObjectName("SettingsDimLabel")
        row.addWidget(hint, 1)
        body.addLayout(row)
        return card

    def _build_flow_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("SignalFlowPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        title = QLabel("SIGNAL FLOW")
        title.setObjectName("SettingsCardTitle")
        layout.addWidget(title)

        self.flow_widget = IOFlowWidget()
        self.flow_widget.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Expanding)
        layout.addWidget(self.flow_widget, 1)

        self.validation_label = QLabel()
        self.validation_label.setObjectName("IOValidationLabel")
        self.validation_label.setWordWrap(True)
        layout.addWidget(self.validation_label)
        return panel

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
                  self.controller_toggle, self.stage_out_check,
                  self.artnet_out_check, self.usb_out_check,
                  self.stage_internal_radio, self.stage_artnet_radio,
                  self.stage_usb_radio):
            w.blockSignals(True)

        self.midi_ctrl_check.setChecked(io.midi_to_controller)
        self.midi_port_check.setChecked(io.midi_to_port)
        self.controller_toggle.setChecked(io.controller_enabled)
        self.stage_out_check.setChecked(io.dmx_to_stage)
        self.artnet_out_check.setChecked(io.dmx_to_artnet)
        self.usb_out_check.setChecked(io.dmx_to_usb)
        self.stage_internal_radio.setChecked(io.stage_source == "internal")
        self.stage_artnet_radio.setChecked(io.stage_source == "artnet")
        self.stage_usb_radio.setChecked(io.stage_source == "usb")

        for w in (self.midi_ctrl_check, self.midi_port_check,
                  self.controller_toggle, self.stage_out_check,
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
            controller_enabled=self.controller_toggle.isChecked(),
            dmx_to_stage=self.stage_out_check.isChecked(),
            dmx_to_artnet=self.artnet_out_check.isChecked(),
            dmx_to_usb=self.usb_out_check.isChecked(),
            usb_out_device=self.usb_out_combo.currentData(),
            stage_source=stage_source,
            stage_usb_device=self.stage_usb_combo.currentData(),
        )

    # --- live feedback --------------------------------------------------------

    def _sync_universe(self, value: int) -> None:
        """Keep the two universe spinboxes in lockstep — they edit the one
        shared ArtNetConfig.universe (sender target + listen filter)."""
        for spin in (self.universe_spin, self.stage_universe_spin):
            if spin.value() != value:
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
        self._on_io_changed()

    def _on_preset_selected(self, index: int) -> None:
        name = self.preset_combo.itemText(index)
        self._widgets_from_io(io_routing.apply_preset(name, self._io_from_widgets()))
        self._on_io_changed()

    def _on_io_changed(self, *_args) -> None:
        io = self._io_from_widgets()

        # Dependent-control enablement
        self.midi_port_combo.setEnabled(self.midi_port_check.isChecked())
        self.controller_body.setEnabled(io.controller_enabled)
        self.controller_state_label.setText(
            "Enabled" if io.controller_enabled else "Disabled")
        self.artnet_fields.setEnabled(self.artnet_out_check.isChecked())
        self.usb_out_combo.setEnabled(self.usb_out_check.isChecked())
        self.stage_universe_spin.setEnabled(self.stage_artnet_radio.isChecked())
        self.stage_usb_combo.setEnabled(self.stage_usb_radio.isChecked())

        # Preset feedback (without re-triggering apply)
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentText(io_routing.match_preset(io))
        self.preset_combo.blockSignals(False)

        # Live signal-flow graph
        self.flow_widget.set_state(io_routing.flow_state(
            io, self.ip_edit.text().strip(), self.universe_spin.value()))

        # Validation strip
        errors, warnings = io_routing.validate(io)
        if errors:
            lines = [f"⛔ {e}" for e in errors] + [f"⚠ {w}" for w in warnings]
            text, state = "\n".join(lines), "error"
        elif warnings:
            text, state = "\n".join(f"⚠ {w}" for w in warnings), "warn"
        else:
            text, state = "✓ Routing valid — no conflicts.", "ok"
        self.validation_label.setText(text)
        self.validation_label.setProperty("state", state)
        self.validation_label.style().unpolish(self.validation_label)
        self.validation_label.style().polish(self.validation_label)

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
