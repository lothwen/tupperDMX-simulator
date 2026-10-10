# ABOUTME: SettingsDialog — modal editor for the user-writable preferences in
# ABOUTME: userprefs.json (I/O routing, Art-Net target, log level and folder).
# ABOUTME: Opened from the menu; sidebar-navigated pages (I/O, Web Remote,
# ABOUTME: Audio, Virtual stage, Interface, Logging).

import html
import os
import threading
from dataclasses import asdict
from pathlib import Path

from PyQt6.QtWidgets import (QDialog, QFormLayout, QGroupBox, QVBoxLayout,
                             QHBoxLayout, QLabel, QLineEdit, QSpinBox,
                             QComboBox, QCheckBox, QRadioButton, QButtonGroup,
                             QDialogButtonBox, QMessageBox, QWidget, QFrame,
                             QSizePolicy, QPushButton, QListWidget,
                             QListWidgetItem, QStackedWidget, QSlider,
                             QGridLayout, QToolButton, QFileDialog,
                             QSpacerItem, QApplication)
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtMultimedia import QMediaDevices

from audio import tts
from audio.engine import device_id_str
from core import buildinfo, io_routing
from core.config import Config, IOConfig, UI_SCALE_MIN, UI_SCALE_MAX
from midi.worker import MidiWorker
from dmx.usb_worker import USBDMXWorker
from tupperlogger import DEFAULT_LOG_DIR, current_log_path, resolve_log_dir
from ui.widgets import icons
from ui.io_flow_widget import IOFlowWidget
from ui.widgets.log_tail import LogTailView
from ui.widgets.slider_ticks import SliderTickLabels
from ui.widgets.toggle_switch import ToggleSwitch
from webremote.state import lan_ip


LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

#: How long "Copy path" stays reading "Copied" before it goes back.
COPIED_FLASH_MS = 1200


def platform_neutral(form: QFormLayout) -> QFormLayout:
    """`form` laid out the same on every OS.

    QFormLayout takes its alignment and growth from three style hints, and no
    two styles answer them alike: QMacStyle centres the whole form and freezes
    the fields at their size hint, Fusion left-aligns and stretches every field
    to the full row. Setting the properties is what stops the style being asked
    at all.

    `ExpandingFieldsGrow` is the growth policy, not `AllNonFixedFieldsGrow`: a
    spin box holding "24" has no business being 700px wide, and only a field
    that asks for the room — a size policy of Expanding — gets it.
    """
    form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
    form.setFieldGrowthPolicy(
        QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    return form


def human_bytes(count: float) -> str:
    """A byte count as "38.4 MB" — one decimal, because the log folder is the
    kind of number you glance at rather than audit."""
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024


def folder_summary(path) -> str:
    """"38.4 MB · 12 files" for `path`, or why there is no such number.

    Files only and one level deep: this describes a log folder, and a rotating
    handler never makes subdirectories.
    """
    try:
        with os.scandir(path) as entries:
            files = [entry for entry in entries if entry.is_file()]
    except OSError:
        return "not created yet"
    if not files:
        return "empty"
    total = sum(entry.stat().st_size for entry in files)
    return f"{human_bytes(total)} · {len(files)} file{'s' if len(files) != 1 else ''}"


#: UI-scale slider granularity, in percent, and how long a drag has to settle
#: before the preview restyles the app.
SCALE_STEP = 5
SCALE_PREVIEW_DELAY_MS = 150


#: Virtual-stage graphics presets, in slider order: one handle over the whole
#: renderer, the way a game's quality preset works.
#:
#: Each names only the values it governs, and what a preset governs is what it
#: COSTS. The fog's ambient direction, its extent, the stage haze and the
#: exposure are all left alone: they decide what the stage looks like, and a
#: quality tier that quietly moved them would be changing the look rather than
#: the fidelity of it. (`max_distance` in particular reads like a cost and is
#: not one — the march is normalised to a fixed optical depth over the slab, so
#: a deeper slab is fog that reaches further back at the same price.)
#:
#: The two that do cost are step count and resolution, and they are not
#: symmetric: the fog buffer scales with resolution SQUARED, while one march
#: step is two octaves of value noise — sixteen hash lookups — plus a medium
#: fetch and a phase function. So the cheap tiers give up resolution first,
#: which is also the one fog can most afford to lose: it is soft and
#: low-frequency, and the temporal pass averages the dither away regardless.
#: Relative march cost is `resolution² × steps`, which puts the ladder at
#: roughly 1 : 5 : 30 — each stop a real step rather than a nudge.
QUALITY_PRESETS = (
    ("Off", dict(gpu=False, fog=False)),
    ("Low", dict(gpu=True, fog=False)),
    # Quarter resolution is a 16x saving and buys the step count that keeps
    # shafts smooth: this is the tier meant to run on integrated graphics.
    ("Mid", dict(gpu=True, fog=True, march_steps=24, resolution=0.25,
                 jitter=0.8)),
    ("High", dict(gpu=True, fog=True, march_steps=32, resolution=0.5,
                  jitter=0.8)),
    ("Ultra", dict(gpu=True, fog=True, march_steps=48, resolution=1.0,
                   jitter=0.8)),
)
#: The two presets that are defined by a switch rather than by numbers, and the
#: slider's last stop, which is a readout: Custom is where the handle sits when
#: the controls below spell out no preset at all.
QUALITY_OFF = 0
QUALITY_LOW = 1
QUALITY_CUSTOM = len(QUALITY_PRESETS)


class SettingsDialog(QDialog):
    """Modal editor for the user-writable preferences in userprefs.json.

    Pure config editor: on accept it validates the inputs and writes them back
    into the provided Config object. Applying the changes live and persisting
    them (Config.save) is the caller's responsibility. The optional
    ``audio_engine`` is only used by the Audio page's "Test voice" button.
    """

    _voice_download_done = pyqtSignal(str, str)  # voice, error ("" = ok)
    #: The UI scale to show right now — emitted while the slider is dragged,
    #: and once more on reject to put back whatever was showing on open. The
    #: window applies it; this dialog never touches the stylesheet itself.
    ui_scale_previewed = pyqtSignal(float)

    _PAGES = (("swap", "Input / Output"), ("monitor", "Web Remote"),
              ("volume", "Audio"), ("bolt", "Virtual stage"),
              ("grid-view", "Interface"), ("list-view", "Logging"))

    def __init__(self, config: Config, parent=None, audio_engine=None):
        super().__init__(parent)
        self.config = config
        self.audio_engine = audio_engine
        self.setWindowTitle("Settings")
        self.setModal(True)

        # UI-scale preview: what was showing when the dialog opened, so reject
        # can put it back, and the timer that keeps a drag from restyling the
        # whole app on every pixel.
        self._scale_at_open = config.window.ui_scale
        self._scale_preview_timer = QTimer(self)
        self._scale_preview_timer.setSingleShot(True)
        self._scale_preview_timer.timeout.connect(self._emit_scale_preview)
        # A floor, not the real minimum — the I/O page's flow diagram needs
        # more than this and Qt takes the larger. Kept low so the dialog is
        # sized by what it actually holds rather than by a number chosen for a
        # bigger screen than the 1280x800 this has to open on.
        self.setMinimumSize(900, 560)

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
        self.pages.addWidget(self._build_stage_page())
        self.pages.addWidget(self._build_interface_page())
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

        # Dev `git describe` tags are long (v1.1-rc1-56-gabc-dirty); show only
        # the release they follow and keep the full tag on hover.
        version = QLabel(f"TupperDMX · {buildinfo.release_of(buildinfo.GIT_TAG)}")
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

        form = platform_neutral(QFormLayout())
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

    def _build_interface_page(self) -> QWidget:
        page, layout = self._make_page("Interface", "scale")

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("UI scale:"))

        self.scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.scale_slider.setRange(round(UI_SCALE_MIN * 100),
                                   round(UI_SCALE_MAX * 100))
        self.scale_slider.setSingleStep(SCALE_STEP)
        self.scale_slider.setPageStep(SCALE_STEP * 2)
        self.scale_slider.setTickInterval(SCALE_STEP * 2)
        self.scale_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.scale_slider.setValue(round(self.config.window.ui_scale * 100))
        self.scale_slider.setToolTip(
            "How big the interface's type and spacing are.\n"
            "Below 100% the whole app fits a 1280-wide display more "
            "comfortably; above it suits a large or high-DPI screen.\n"
            "Past about 125% the window needs more than 1280px of width.")
        self.scale_slider.valueChanged.connect(self._on_scale_slider)
        row.addWidget(self.scale_slider, 1)

        self.scale_value_label = QLabel()
        self.scale_value_label.setObjectName("SettingsValueLabel")
        self.scale_value_label.setMinimumWidth(48)
        self.scale_value_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                            | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.scale_value_label)

        reset = QPushButton("Reset")
        reset.setToolTip("Back to 100%, the size the interface was designed at")
        reset.clicked.connect(lambda: self.scale_slider.setValue(100))
        row.addWidget(reset)
        layout.addLayout(row)

        hint = QLabel("Previewed as you drag, and kept only if you press OK.")
        hint.setObjectName("SettingsDimLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._update_scale_label()
        layout.addStretch(1)
        return page

    def _on_scale_slider(self, value: int) -> None:
        """Preview the scale the slider is on.

        Debounced because re-applying the stylesheet re-polishes every widget
        in the app, and a drag emits a value per pixel — without this, dragging
        the handle across the track would restyle the whole window ~80 times.
        """
        self._update_scale_label()
        self._scale_preview_timer.start(SCALE_PREVIEW_DELAY_MS)

    def _update_scale_label(self) -> None:
        self.scale_value_label.setText(f"{self.scale_slider.value()}%")

    def _emit_scale_preview(self) -> None:
        self.ui_scale_previewed.emit(self.scale_slider.value() / 100.0)

    def _build_logging_page(self) -> QWidget:
        """Level, where the files go, and what is being written right now.

        Both settings take effect as soon as the dialog is accepted, which is
        what makes the pane below them worth watching: turn DEBUG on and the
        detail starts arriving in it.

        The pane tails the file the app is *actually* writing, which is not
        necessarily the folder in the field above it: a folder typed here is
        where logging moves to when the dialog is accepted.
        """
        page, layout = self._make_page("Logging")

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        grid.addWidget(QLabel("Level:"), 0, 0)
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(LOG_LEVELS)
        current = (self.config.logging.level
                   if self.config.logging.level in LOG_LEVELS else "INFO")
        self.log_level_combo.setCurrentText(current)
        self.log_level_combo.setToolTip(
            "How much detail is logged. Applies as soon as you press OK.\n"
            "DEBUG is worth turning on to reproduce a problem, and worth "
            "turning off again afterwards.")
        grid.addWidget(self.log_level_combo, 0, 1)

        grid.addItem(QSpacerItem(0, 10), 1, 0)

        grid.addWidget(QLabel("Folder:"), 2, 0)
        grid.addLayout(self._build_log_folder_row(), 2, 1)

        self.log_file_hint = QLabel()
        self.log_file_hint.setObjectName("SettingsDimLabel")
        grid.addWidget(self.log_file_hint, 3, 1)

        grid.addItem(QSpacerItem(0, 10), 4, 0)

        live_label = QLabel("Live log")
        grid.addWidget(live_label, 5, 0, Qt.AlignmentFlag.AlignTop)
        self.log_tail = LogTailView()
        self.log_tail.set_path(current_log_path())
        grid.addWidget(self.log_tail, 5, 1)
        grid.setRowStretch(5, 1)

        layout.addLayout(grid, 1)
        self._update_log_folder_info()
        return page

    def _build_log_folder_row(self) -> QHBoxLayout:
        """The path field (folder icon · path · size) and its two buttons."""
        row = QHBoxLayout()
        row.setSpacing(8)

        field = QFrame()
        field.setObjectName("SettingsPathField")
        inner = QHBoxLayout(field)
        inner.setContentsMargins(8, 3, 8, 3)
        inner.setSpacing(6)

        browse = QToolButton()
        browse.setObjectName("SettingsPathIcon")
        browse.setAutoRaise(True)
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.setIcon(icons.icon("folder-open", "#8a8a8a"))
        browse.setIconSize(QSize(14, 14))
        browse.setToolTip("Choose a folder…")
        browse.clicked.connect(self._on_browse_log_dir)
        inner.addWidget(browse)

        self.log_dir_edit = QLineEdit(self.config.logging.log_dir)
        self.log_dir_edit.setObjectName("SettingsPathEdit")
        self.log_dir_edit.setFrame(False)
        # Empty is a real setting, not a missing one: it means "wherever the
        # app puts them by default", so the placeholder spells that folder out
        # instead of leaving the field looking unconfigured.
        self.log_dir_edit.setPlaceholderText(str(DEFAULT_LOG_DIR.resolve()))
        self.log_dir_edit.setToolTip(
            "Where the rotating log files are written.\n"
            "Leave it empty to keep them in the app's own logs/ folder.\n"
            "Applies as soon as you press OK; files already written stay "
            "where they are.")
        self.log_dir_edit.textChanged.connect(self._update_log_folder_info)
        inner.addWidget(self.log_dir_edit, 1)

        self.log_dir_stats = QLabel()
        self.log_dir_stats.setObjectName("SettingsDimLabel")
        inner.addWidget(self.log_dir_stats)
        row.addWidget(field, 1)

        open_button = QPushButton(icons.icon("external-link", "#cfcfcf"),
                                  " Open folder")
        open_button.setToolTip("Show the folder in the system file manager")
        open_button.clicked.connect(self._on_open_log_dir)
        row.addWidget(open_button)

        self.log_copy_button = QPushButton("Copy path")
        self.log_copy_button.setToolTip("Copy the folder's full path to the clipboard")
        self.log_copy_button.clicked.connect(self._on_copy_log_dir)
        row.addWidget(self.log_copy_button)
        return row

    def _chosen_log_dir(self) -> Path:
        """The folder the field currently names, blank resolved to the
        default, as an absolute path."""
        return resolve_log_dir(self.log_dir_edit.text()).expanduser().absolute()

    def _update_log_folder_info(self, *_args) -> None:
        """Re-read what the named folder holds, and which file it will grow."""
        self.log_dir_stats.setText(folder_summary(self._chosen_log_dir()))

        if not self.config.logging.file_output:
            self.log_file_hint.setText("File logging is off.")
            return
        active = current_log_path()
        name = active.name if active else "one file per day"
        rotation = human_bytes(self.config.logging.max_file_size)
        self.log_file_hint.setText(
            f"{name} · rotates at {rotation}, keeps "
            f"{self.config.logging.backup_count}")

    def _on_browse_log_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Log folder", str(self._chosen_log_dir()))
        if chosen:
            self.log_dir_edit.setText(chosen)

    def _on_open_log_dir(self) -> None:
        folder = self._chosen_log_dir()
        if not folder.is_dir():
            QMessageBox.information(
                self, "Log folder",
                f"{folder} doesn't exist yet.\n\n"
                "It is created the first time the app logs to it.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _on_copy_log_dir(self) -> None:
        QApplication.clipboard().setText(str(self._chosen_log_dir()))
        self.log_copy_button.setText("Copied")
        QTimer.singleShot(COPIED_FLASH_MS,
                          lambda: self.log_copy_button.setText("Copy path"))

    def _build_stage_page(self) -> QWidget:
        page, layout = self._make_page("Virtual stage",
                                       "quality · renderer · fog")

        # Set before anything the presets govern is built: applying a preset
        # writes those controls, and their change signals come straight back
        # here asking which preset is showing.
        self._applying_preset = False
        self._build_quality_slider(layout)

        self.stage_gpu_check = QCheckBox("Render the stage light on the GPU")
        self.stage_gpu_check.setChecked(self.config.window.stage_gpu)
        self.stage_gpu_check.setToolTip(
            "Accumulate every fixture's light in one high-dynamic-range buffer "
            "and tonemap it with the ACES filmic curve.\n"
            "Beams add where they cross and blow out instead of flattening "
            "to white.\n"
            "Needs a working OpenGL driver; falls back to the normal renderer "
            "if there isn't one.")
        layout.addWidget(self.stage_gpu_check)

        self._build_fog_controls(layout)

        # Wired last: the preset both reads and writes every control above, so
        # all of them have to exist before the first sync.
        self.quality_slider.valueChanged.connect(self._on_quality_slider)
        for signal in (self.stage_gpu_check.toggled, self.fog_check.toggled,
                       self.fog_steps_spin.valueChanged,
                       self.fog_resolution_combo.currentIndexChanged,
                       self.fog_jitter_spin.valueChanged):
            signal.connect(self._sync_quality_slider)
        self._sync_quality_slider()

        # Outside the quality presets on purpose: it is a different renderer,
        # not a setting of this one.
        self.stage_3d_check = QCheckBox("Experimental: 3D stage (looks cool)")
        self.stage_3d_check.setChecked(self.config.window.stage_3d_experimental)
        self.stage_3d_check.setToolTip(
            "Show the 3D bar prototype, lit by the show's patch, in place of "
            "the 2D stage.\n"
            "Needs OpenGL 4.1. The stage designer switches back to the 2D "
            "stage.\n"
            "Drag to look around, hold WASD to fly where you look, wheel to "
            "step, F for fullscreen, -/+ for exposure.")
        layout.addWidget(self.stage_3d_check)

        layout.addStretch(1)
        return page

    def _build_quality_slider(self, layout: QVBoxLayout) -> None:
        """One handle over everything below it.

        As much a readout as a control: the handle is *derived* from the values
        it governs, so reaching past it and changing one of them by hand lands
        on Custom rather than leaving the slider lying about the rig.
        """
        row = QGridLayout()
        row.setHorizontalSpacing(10)
        row.setVerticalSpacing(2)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Quality:"), 0, 0)

        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(0, QUALITY_CUSTOM)
        self.quality_slider.setSingleStep(1)
        self.quality_slider.setPageStep(1)
        self.quality_slider.setTickInterval(1)
        self.quality_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.quality_slider.setToolTip(
            "How much the virtual stage's renderer is asked to do.\n"
            "Off is the raster path, the one that draws on any machine; every "
            "other stop turns the GPU renderer on and spends the rest on "
            "fog.\n"
            "Custom is what the slider reads when the controls below match no "
            "preset — it is not something you pick.")
        row.addWidget(self.quality_slider, 0, 1)

        # The names live under the ticks, so the strip has to share the
        # slider's column to share its width.
        self.quality_ticks = SliderTickLabels(
            self.quality_slider,
            [name for name, _ in QUALITY_PRESETS] + ["Custom"])
        row.addWidget(self.quality_ticks, 1, 1)
        row.setColumnStretch(1, 1)
        layout.addLayout(row)

    def _on_quality_slider(self, index: int) -> None:
        """Write the preset the handle landed on into the controls below.

        Custom has nothing to apply, so landing on it by hand puts the handle
        straight back where the current values say it belongs.
        """
        if index == QUALITY_CUSTOM:
            self._sync_quality_slider()
            return

        values = QUALITY_PRESETS[index][1]
        self._applying_preset = True
        try:
            self.stage_gpu_check.setChecked(values["gpu"])
            self.fog_check.setChecked(values["fog"])
            if values["fog"]:
                self.fog_steps_spin.setValue(values["march_steps"])
                self._select_fog_resolution(values["resolution"])
                self.fog_jitter_spin.setValue(
                    int(round(values["jitter"] * 100)))
        finally:
            self._applying_preset = False
        self._show_quality(index)

    def _sync_quality_slider(self, *_) -> None:
        """Put the handle where the controls below say it belongs."""
        if self._applying_preset:
            return
        index = self._current_quality_index()
        self.quality_slider.blockSignals(True)
        self.quality_slider.setValue(index)
        self.quality_slider.blockSignals(False)
        self._show_quality(index)

    def _current_quality_index(self) -> int:
        """Which preset the controls currently spell out, or Custom.

        Only what is actually in effect counts. With the GPU renderer off
        nothing below it draws at all, and with fog off its quality numbers are
        greyed out — matching on values nobody can see would read Custom for a
        stage that looks exactly like the preset.
        """
        if not self.stage_gpu_check.isChecked():
            return QUALITY_OFF
        if not self.fog_check.isChecked():
            return QUALITY_LOW
        for index in range(QUALITY_LOW + 1, QUALITY_CUSTOM):
            values = QUALITY_PRESETS[index][1]
            if (self.fog_steps_spin.value() == values["march_steps"]
                    and abs(self.fog_resolution_combo.currentData()
                            - values["resolution"]) < 1e-6
                    and self.fog_jitter_spin.value()
                        == int(round(values["jitter"] * 100))):
                return index
        return QUALITY_CUSTOM

    def _show_quality(self, index: int) -> None:
        # Driven from here rather than from the slider's valueChanged: a sync
        # sets the handle with the signal blocked, and the strip still has to
        # follow.
        self.quality_ticks.set_active(index)
        # Off IS the GPU renderer being off, so the checkbox has nothing left
        # to say; the slider is the way back on.
        self.stage_gpu_check.setEnabled(index != QUALITY_OFF)

    def _build_fog_controls(self, layout: QVBoxLayout) -> None:
        """Volumetric fog: the switch, and what the march costs.

        What the fog *looks* like is not here — that is the FOG row on the
        stage's edit toolbar, because it is tuned by eye against a rig. These
        are the settings you pick once for the machine you are running on.
        """
        fog = self.config.window.fog

        layout.addSpacing(10)
        self.fog_check = QCheckBox("Volumetric fog")
        self.fog_check.setChecked(fog.enabled)
        self.fog_check.setToolTip(
            "March every view ray through the air instead of scaling each "
            "beam's brightness by one stage-wide haze number.\n"
            "Shafts are integrated through the fog that is actually there, so "
            "a beam brightens crossing a puff of smoke and god rays fall out "
            "on their own.\n"
            "Needs the GPU renderer above.")
        self.fog_check.setEnabled(self.stage_gpu_check.isChecked())
        layout.addWidget(self.fog_check)

        # A ray march has nowhere to run on the raster path, so the whole block
        # follows the checkbox above it rather than being separately wrong.
        self.stage_gpu_check.toggled.connect(self.fog_check.setEnabled)

        quality = QFrame()
        quality.setObjectName("SettingsSubCard")
        form = platform_neutral(QFormLayout(quality))
        form.setContentsMargins(18, 6, 0, 0)
        form.setSpacing(6)

        self.fog_steps_spin = QSpinBox()
        self.fog_steps_spin.setRange(4, 128)
        self.fog_steps_spin.setValue(fog.march_steps)
        self.fog_steps_spin.setToolTip(
            "Samples along each view ray. The biggest cost here, and the "
            "difference between smooth shafts and visible banding.\n"
            "24 is comfortable on integrated graphics.")
        form.addRow("March steps:", self.fog_steps_spin)

        self.fog_resolution_combo = QComboBox()
        for caption, value in (("Quarter (fastest)", 0.25), ("Half", 0.5),
                               ("Full (slowest)", 1.0)):
            self.fog_resolution_combo.addItem(caption, value)
        self._select_fog_resolution(fog.resolution)
        self.fog_resolution_combo.setToolTip(
            "What fraction of the window the fog is marched at before being "
            "scaled back up.\n"
            "Fog is soft and low-frequency, so resolution is much cheaper to "
            "give up than step count.")
        form.addRow("Resolution:", self.fog_resolution_combo)

        self.fog_distance_spin = QSpinBox()
        self.fog_distance_spin.setRange(100, 8000)
        self.fog_distance_spin.setSingleStep(100)
        self.fog_distance_spin.setSuffix(" px")
        self.fog_distance_spin.setValue(int(fog.max_distance))
        self.fog_distance_spin.setToolTip(
            "How deep the marched slab of air is, in stage units.")
        form.addRow("Max distance:", self.fog_distance_spin)

        self.fog_jitter_spin = QSpinBox()
        self.fog_jitter_spin.setRange(0, 200)
        self.fog_jitter_spin.setSuffix(" %")
        self.fog_jitter_spin.setValue(int(round(fog.jitter * 100)))
        self.fog_jitter_spin.setToolTip(
            "How far each ray's start is dithered, trading banding for noise "
            "that is then averaged away over a few frames.\n"
            "At 0 the bands come back.")
        form.addRow("Jitter:", self.fog_jitter_spin)

        self.fog_light_dir_spin = QSpinBox()
        self.fog_light_dir_spin.setRange(0, 360)
        self.fog_light_dir_spin.setSuffix("°")
        self.fog_light_dir_spin.setValue(int(round(fog.light_dir_deg)))
        self.fog_light_dir_spin.setToolTip(
            "Which way the stage-wide key on the medium comes from. "
            "90° is straight down the picture, the way every other angle on "
            "this stage is measured.")
        form.addRow("Ambient from:", self.fog_light_dir_spin)

        layout.addWidget(quality)
        quality.setEnabled(fog.enabled and self.stage_gpu_check.isChecked())
        self.fog_check.toggled.connect(
            lambda on: quality.setEnabled(on and self.stage_gpu_check.isChecked()))
        self.stage_gpu_check.toggled.connect(
            lambda on: quality.setEnabled(on and self.fog_check.isChecked()))

        fog_hint = QLabel("Fog appearance lives on the stage's edit toolbar.")
        fog_hint.setObjectName("SettingsDimLabel")
        layout.addWidget(fog_hint)

    def _select_fog_resolution(self, value: float) -> None:
        """Pick the nearest offered resolution — prefs may hold anything in
        range, and the combo offers three sensible ones."""
        best = min(range(self.fog_resolution_combo.count()),
                   key=lambda i: abs(self.fog_resolution_combo.itemData(i) - value))
        self.fog_resolution_combo.setCurrentIndex(best)

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

    def reject(self) -> None:
        """Cancel, undoing the UI scale the slider previewed.

        Overriding reject() rather than wiring the Cancel button covers Esc and
        the window's close button too — QDialog routes all three through here.
        """
        self._scale_preview_timer.stop()
        self.ui_scale_previewed.emit(self._scale_at_open)
        super().reject()

    def _log_dir_is_usable(self, log_dir: str) -> bool:
        """Can we actually log into `log_dir`? Creates it to find out.

        Blank needs no check — it is the default folder, which the logging
        system creates for itself. Anything else is somewhere the operator
        pointed us, and a folder that turns out to be unwritable at the next
        launch loses the log silently, which is exactly when it is wanted.
        """
        if not log_dir:
            return True
        folder = Path(log_dir).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            usable = os.access(folder, os.W_OK)
            reason = "" if usable else "it is not writable"
        except OSError as error:
            usable, reason = False, str(error)
        if not usable:
            QMessageBox.warning(
                self, "Invalid log folder",
                f"Logs can't be written to '{log_dir}' — {reason}.\n\n"
                "Pick another folder, or clear the field to use the default.")
        return usable

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

        log_dir = self.log_dir_edit.text().strip()
        if not self._log_dir_is_usable(log_dir):
            return

        if self._is_valid_ipv4(ip):
            self.config.artnet.ip = ip
        self.config.artnet.port = self.port_spin.value()
        self.config.artnet.universe = self.universe_spin.value()
        self.config.io = io
        self.config.logging.level = self.log_level_combo.currentText()
        self.config.logging.log_dir = log_dir
        self.config.window.stage_gpu = self.stage_gpu_check.isChecked()
        self.config.window.stage_3d_experimental = self.stage_3d_check.isChecked()
        self.config.window.ui_scale = self.scale_slider.value() / 100.0
        # Through the config's own parser, so the dialog and a hand-edited
        # prefs file agree about what is in range.
        self.config.window.fog = Config._parse_fog({
            **asdict(self.config.window.fog),
            "enabled": self.fog_check.isChecked(),
            "march_steps": self.fog_steps_spin.value(),
            "resolution": self.fog_resolution_combo.currentData(),
            "max_distance": float(self.fog_distance_spin.value()),
            "jitter": self.fog_jitter_spin.value() / 100.0,
            "light_dir_deg": float(self.fog_light_dir_spin.value()),
        })
        self.config.webremote.enabled = self.webremote_toggle.isChecked()
        self.config.webremote.port = self.webremote_port_spin.value()
        self.config.audio.output_device = self.audio_device_combo.currentData() or ""
        self.config.audio.routing = self.audio_routing_combo.currentData()
        self.config.audio.count_in_bars = self.count_in_bars_spin.value()
        self.config.audio.count_in_voice = self.count_in_voice_check.isChecked()
        self.config.audio.tts_voice = self._selected_voice()
        self.accept()
