# ABOUTME: SettingsDialog — modal editor for the user-writable preferences in
# ABOUTME: userprefs.json (I/O routing, Art-Net target, log level). Opened from
# ABOUTME: the menu; sidebar-navigated pages (I/O, Web Remote, Audio, Logging).

import html
import threading

from PyQt6.QtWidgets import (QDialog, QFormLayout, QGroupBox, QVBoxLayout,
                             QHBoxLayout, QLabel, QLineEdit, QSpinBox,
                             QComboBox, QCheckBox, QRadioButton, QButtonGroup,
                             QDialogButtonBox, QMessageBox, QWidget, QFrame,
                             QSizePolicy, QPushButton, QListWidget,
                             QListWidgetItem, QStackedWidget)
from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtMultimedia import QMediaDevices

from audio import tts
from audio.engine import device_id_str
from core import buildinfo, io_routing
from core.config import Config, IOConfig
from midi.worker import MidiWorker
from dmx.usb_worker import USBDMXWorker
from ui import icons
from ui.io_flow_widget import IOFlowWidget
from ui.toggle_switch import ToggleSwitch
from webremote.state import lan_ip


LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class SettingsDialog(QDialog):
    """Modal editor for the user-writable preferences in userprefs.json.

    Pure config editor: on accept it validates the inputs and writes them back
    into the provided Config object. Applying the changes live and persisting
    them (Config.save) is the caller's responsibility. The optional
    ``audio_engine`` is only used by the Audio page's "Test voice" button.
    """

    _voice_download_done = pyqtSignal(str, str)  # voice, error ("" = ok)

    _PAGES = (("swap", "Input / Output"), ("monitor", "Web Remote"),
              ("volume", "Audio"), ("list-view", "Logging"))

    def __init__(self, config: Config, parent=None, audio_engine=None):
        super().__init__(parent)
        self.config = config
        self.audio_engine = audio_engine
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumSize(1000, 640)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())

        right = QVBoxLayout()
        right.setContentsMargins(16, 14, 16, 14)
        right.setSpacing(12)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_io_page())
        self.pages.addWidget(self._build_webremote_page())
        self.pages.addWidget(self._build_audio_page())
        self.pages.addWidget(self._build_logging_page())
        right.addWidget(self.pages, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        right.addWidget(buttons)
        root.addLayout(right, 1)

        self.sidebar_list.setCurrentRow(0)  # also sets the window title
        self._widgets_from_io(config.io)
        self._on_io_changed()

    # --- sidebar & pages --------------------------------------------------------

    def _build_sidebar(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("SettingsSidebar")
        frame.setFixedWidth(185)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 10, 8, 10)
        layout.setSpacing(8)

        self.sidebar_list = QListWidget()
        self.sidebar_list.setObjectName("SettingsSidebarList")
        self.sidebar_list.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar_list.setIconSize(QSize(15, 15))
        for icon_name, name in self._PAGES:
            self.sidebar_list.addItem(QListWidgetItem(
                icons.icon(icon_name, "#cfcfcf", selected_color="#082230"),
                name))
        self.sidebar_list.currentRowChanged.connect(self._on_page_selected)
        layout.addWidget(self.sidebar_list, 1)

        # Dev `git describe` tags are long (v1.0-56-gabc-dirty); show only the
        # release component and keep the full tag on hover.
        version = QLabel(f"TupperDMX · {buildinfo.GIT_TAG.split('-')[0]}")
        version.setObjectName("SettingsVersionLabel")
        version.setToolTip(buildinfo.GIT_TAG)
        layout.addWidget(version)
        return frame

    def _on_page_selected(self, row: int) -> None:
        self.pages.setCurrentIndex(row)
        self.setWindowTitle(f"Settings — {self._PAGES[row][1]}")

    def _make_page(self, title, subtitle=None):
        """Settings page with a heading row + hairline separator. Returns
        (page, layout) for the caller to fill."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(8)
        title_label = QLabel(title)
        title_label.setObjectName("SettingsSectionTitle")
        header.addWidget(title_label)
        if subtitle:
            sub_label = QLabel(subtitle)
            sub_label.setObjectName("SettingsDimLabel")
            header.addWidget(sub_label)
        header.addStretch(1)
        layout.addLayout(header)

        line = QFrame()
        line.setObjectName("SettingsHairline")
        line.setFixedHeight(1)
        layout.addWidget(line)
        return page, layout

    def _build_io_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        layout.addLayout(self._build_preset_row())

        columns = QHBoxLayout()
        columns.setSpacing(14)

        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(self._build_sequencer_card())
        left.addWidget(self._build_controller_card())
        left.addWidget(self._build_stage_card())
        left.addStretch(1)
        columns.addLayout(left, 11)

        columns.addWidget(self._build_flow_panel(), 9)
        layout.addLayout(columns, 1)
        return page

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
        self.controller_toggle = ToggleSwitch()
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

    def _build_webremote_page(self) -> QWidget:
        page, layout = self._make_page("Web Remote")

        self.webremote_toggle = ToggleSwitch()
        self.webremote_toggle.setChecked(self.config.webremote.enabled)
        card, _body = self._make_card(None, "TABLET CONTROL",
                                      "Serves a web panel on the local network.",
                                      header_extra=self.webremote_toggle)
        layout.addWidget(card)

        row = QHBoxLayout()
        row.addWidget(QLabel("Port:"))
        self.webremote_port_spin = QSpinBox()
        self.webremote_port_spin.setRange(1024, 65535)
        self.webremote_port_spin.setValue(self.config.webremote.port)
        row.addWidget(self.webremote_port_spin)
        self.webremote_url_label = QLabel()
        self.webremote_url_label.setObjectName("SettingsLinkLabel")
        self.webremote_url_label.setOpenExternalLinks(True)
        self.webremote_url_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.webremote_url_label.linkHovered.connect(self._on_webremote_link_hover)
        row.addWidget(self.webremote_url_label, 1)
        layout.addLayout(row)

        hint = QLabel("Open this address on a tablet connected to the "
                      "same Wi-Fi network.")
        hint.setObjectName("SettingsDimLabel")
        layout.addWidget(hint)
        layout.addStretch(1)

        self.webremote_toggle.toggled.connect(self._update_webremote_url)
        self.webremote_port_spin.valueChanged.connect(self._update_webremote_url)
        self._update_webremote_url()
        return page

    def _update_webremote_url(self, *_) -> None:
        if self.webremote_toggle.isChecked():
            self._webremote_url = f"http://{lan_ip()}:{self.webremote_port_spin.value()}"
            self._render_webremote_link(False)
        else:
            self._webremote_url = ""
            self.webremote_url_label.setText("Disabled — no server runs.")

    def _render_webremote_link(self, hover: bool) -> None:
        if not self._webremote_url:
            return
        deco = "underline" if hover else "none"
        url = self._webremote_url
        self.webremote_url_label.setText(
            f'<a href="{url}" style="color:#79cdf0; '
            f'text-decoration:{deco};">{url}</a>')

    def _on_webremote_link_hover(self, link: str) -> None:
        # setText resets the label's auto link cursor, so re-apply it here.
        self._render_webremote_link(bool(link))
        if link:
            self.webremote_url_label.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.webremote_url_label.unsetCursor()

    def _build_audio_page(self) -> QWidget:
        """Audio page (Fase 4 live player): output device, click/voice channel
        routing, count-in length and the TTS voice."""
        page, layout = self._make_page("Audio", "output · click & voice")

        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(10)

        self.audio_device_combo = QComboBox()
        self.audio_device_combo.addItem("System default", "")
        for device in QMediaDevices.audioOutputs():
            self.audio_device_combo.addItem(device.description(),
                                            device_id_str(device))
        current = self.config.audio.output_device
        idx = self.audio_device_combo.findData(current)
        if idx < 0:  # persisted device currently absent — keep, don't lose it
            self.audio_device_combo.addItem(f"{current} (not connected)", current)
            idx = self.audio_device_combo.count() - 1
        self.audio_device_combo.setCurrentIndex(idx)
        form.addRow("Output:", self.audio_device_combo)

        self.audio_routing_combo = QComboBox()
        for label, value in (
            ("Stereo — everything on both channels", "stereo"),
            ("Split — backing LEFT · click+voice RIGHT", "split_backing_left"),
            ("Split — backing RIGHT · click+voice LEFT", "split_backing_right"),
        ):
            self.audio_routing_combo.addItem(label, value)
        idx = self.audio_routing_combo.findData(self.config.audio.routing)
        self.audio_routing_combo.setCurrentIndex(max(0, idx))
        form.addRow("Routing:", self.audio_routing_combo)

        countin_row = QHBoxLayout()
        self.count_in_bars_spin = QSpinBox()
        self.count_in_bars_spin.setRange(1, 8)
        self.count_in_bars_spin.setValue(self.config.audio.count_in_bars)
        self.count_in_bars_spin.setSuffix(" bars")
        countin_row.addWidget(self.count_in_bars_spin)
        self.count_in_voice_check = QCheckBox("Speak the last bar")
        self.count_in_voice_check.setChecked(self.config.audio.count_in_voice)
        countin_row.addWidget(self.count_in_voice_check)
        hint = QLabel("Per-song on/off: SETLIST toolbar")
        hint.setObjectName("SettingsDimLabel")
        countin_row.addWidget(hint, 1)
        form.addRow("Count-in:", countin_row)

        voice_row = QHBoxLayout()
        self.tts_voice_combo = QComboBox()
        for voice in tts.VOICES:
            self.tts_voice_combo.addItem(tts.voice_label(voice), voice)
        idx = self.tts_voice_combo.findData(self.config.audio.tts_voice)
        if idx < 0:
            self.tts_voice_combo.addItem(self.config.audio.tts_voice,
                                         self.config.audio.tts_voice)
            idx = self.tts_voice_combo.count() - 1
        self.tts_voice_combo.setCurrentIndex(idx)
        self.tts_voice_combo.currentIndexChanged.connect(self._update_voice_status)
        voice_row.addWidget(self.tts_voice_combo, 1)
        self.voice_download_button = QPushButton("Download")
        self.voice_download_button.clicked.connect(self._on_download_voice)
        voice_row.addWidget(self.voice_download_button)
        self.voice_test_button = QPushButton("Test")
        self.voice_test_button.clicked.connect(self._on_test_voice)
        voice_row.addWidget(self.voice_test_button)
        form.addRow("Voice:", voice_row)

        layout.addLayout(form)

        self.voice_status_label = QLabel()
        self.voice_status_label.setObjectName("SettingsDimLabel")
        layout.addWidget(self.voice_status_label)
        layout.addStretch(1)

        self._voice_download_done.connect(self._on_voice_download_done)
        self._update_voice_status()
        return page

    def _selected_voice(self) -> str:
        return self.tts_voice_combo.currentData() or tts.DEFAULT_VOICE

    def _update_voice_status(self, *_) -> None:
        voice = self._selected_voice()
        if not tts.is_available():
            self.voice_status_label.setText(
                "TTS engine (piper) not installed — voice features disabled.")
            self.voice_download_button.setEnabled(False)
            self.voice_test_button.setEnabled(False)
            return
        installed = tts.is_voice_installed(voice)
        self.voice_download_button.setEnabled(not installed)
        self.voice_test_button.setEnabled(installed and self.audio_engine is not None)
        self.voice_status_label.setText(
            "Voice installed." if installed
            else "Voice not downloaded yet (~60 MB, one time).")

    def _on_download_voice(self) -> None:
        voice = self._selected_voice()
        self.voice_download_button.setEnabled(False)
        self.voice_status_label.setText(f"Downloading {voice}…")

        def work():
            try:
                tts.download_voice(voice)
                self._voice_download_done.emit(voice, "")
            except Exception as e:
                self._voice_download_done.emit(voice, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_voice_download_done(self, voice: str, error: str) -> None:
        if error:
            self.voice_status_label.setText(f"Download failed: {error}")
            self.voice_download_button.setEnabled(True)
            return
        self._update_voice_status()

    def _on_test_voice(self) -> None:
        """Render a short phrase (cache-first) and play it on the voice bus."""
        if self.audio_engine is None:
            return
        voice = self._selected_voice()
        words = tts.count_words_for_voice(voice)
        samples = tts.render(", ".join(words), voice)
        if samples is None:
            self.voice_status_label.setText("Could not render the test phrase.")
            return
        self.audio_engine.play_voice(samples)

    def _build_logging_page(self) -> QWidget:
        page, layout = self._make_page("Logging")

        row = QHBoxLayout()
        row.addWidget(QLabel("Level:"))
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(LOG_LEVELS)
        current = (self.config.logging.level
                   if self.config.logging.level in LOG_LEVELS else "INFO")
        self.log_level_combo.setCurrentText(current)
        row.addWidget(self.log_level_combo, 1)
        layout.addLayout(row)

        hint = QLabel("Level changes apply on the next launch.")
        hint.setObjectName("SettingsDimLabel")
        layout.addWidget(hint)

        layout.addSpacing(18)
        stage_title = QLabel("VIRTUAL STAGE")
        stage_title.setObjectName("SettingsDimLabel")
        layout.addWidget(stage_title)

        self.stage_gpu_check = QCheckBox("Render the stage light on the GPU")
        self.stage_gpu_check.setChecked(self.config.window.stage_gpu)
        self.stage_gpu_check.setToolTip(
            "Accumulate every fixture's light in one high-dynamic-range buffer "
            "and tonemap it with the ACES filmic curve.\n"
            "Beams add where they cross and overdriven fixtures blow out "
            "instead of flattening to white.\n"
            "Needs a working OpenGL driver; falls back to the normal renderer "
            "if there isn't one.")
        layout.addWidget(self.stage_gpu_check)

        gpu_hint = QLabel("Applies on the next launch.")
        gpu_hint.setObjectName("SettingsDimLabel")
        layout.addWidget(gpu_hint)

        layout.addStretch(1)
        return page

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

        # A route can't be enabled without detected hardware behind it.
        self.midi_port_check.setEnabled(bool(midi_ports))
        self.midi_port_check.setToolTip(
            "" if midi_ports else "No MIDI output ports detected")
        for w in (self.usb_out_check, self.stage_usb_radio):
            w.setEnabled(bool(usb_devices))
            w.setToolTip("" if usb_devices else "No USB DMX device detected")

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
        error_tag = icons.img_tag("warning", "#e24a4a", 11)
        warn_tag = icons.img_tag("warning", "#e2a04a", 11)
        if errors:
            lines = ([f"{error_tag} {html.escape(e)}" for e in errors]
                     + [f"{warn_tag} {html.escape(w)}" for w in warnings])
            text, state = "<br>".join(lines), "error"
        elif warnings:
            text, state = "<br>".join(
                f"{warn_tag} {html.escape(w)}" for w in warnings), "warn"
        else:
            text = (f'{icons.img_tag("check", "#4caf50", 11)} '
                    "Routing valid — no conflicts.")
            state = "ok"
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
        self.config.window.stage_gpu = self.stage_gpu_check.isChecked()
        self.config.webremote.enabled = self.webremote_toggle.isChecked()
        self.config.webremote.port = self.webremote_port_spin.value()
        self.config.audio.output_device = self.audio_device_combo.currentData() or ""
        self.config.audio.routing = self.audio_routing_combo.currentData()
        self.config.audio.count_in_bars = self.count_in_bars_spin.value()
        self.config.audio.count_in_voice = self.count_in_voice_check.isChecked()
        self.config.audio.tts_voice = self._selected_voice()
        self.accept()
