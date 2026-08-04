# ABOUTME: Stage scenario designer UI — a top-left toolbar (add / done) plus a
# ABOUTME: per-selection inspector (name / type / DMX / delete). Placement is by drag.
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QDialog, QFrame, QVBoxLayout, QHBoxLayout,
    QPushButton, QComboBox, QSlider, QSpinBox, QLabel, QLineEdit,
    QMessageBox,
)

from tupperlogger import get_logger
from core.fixture_library import fixture_modes
from ui import icons
from ui.add_fixture_dialog import AddFixtureDialog, used_midi_channels


def _separator() -> QFrame:
    """A thin vertical rule between groups of toolbar controls."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setObjectName("EditToolbarSeparator")
    return line


class StageEditToolbar(QWidget):
    """Top-left overlay toolbar shown while the stage is in edit mode.

    Hosts add-a-fixture (then drag it on the stage to place it) and Done (leave
    edit mode). Add funnels through ``on_patch_changed`` so the caller persists
    the patch; ``on_done`` exits. Deleting a fixture lives in the
    FixtureInspector, not here.
    """

    def __init__(self, stage_controller, on_patch_changed, on_done, parent=None):
        super().__init__(parent)
        self.setObjectName("StageEditToolbar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._stage = stage_controller
        self._notify = on_patch_changed
        self._on_done = on_done
        self.logger = get_logger("ui.fixture_designer")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        title = QLabel('<span style="color:#4aa3df">●</span> EDIT MODE')
        title.setObjectName("EditModeLabel")
        layout.addWidget(title)

        add_button = QPushButton("Add fixture")
        add_button.setObjectName("EditToolbarAdd")
        add_button.clicked.connect(self.open_add_fixture)
        layout.addWidget(add_button)

        layout.addWidget(_separator())
        haze_label = QLabel("HAZE")
        haze_label.setObjectName("EditToolbarHazeLabel")
        layout.addWidget(haze_label)

        # How hazy the room is before any hazer runs. Beams are only visible
        # because there is something in the air for them to scatter off, so a
        # rig with no hazer patched still needs a way to see its own beams.
        self._haze = QSlider(Qt.Orientation.Horizontal)
        self._haze.setObjectName("EditToolbarHaze")
        self._haze.setRange(0, 100)
        self._haze.setFixedWidth(90)
        self._haze.setValue(int(stage_controller.atmosphere.baseline * 100))
        self._haze.setToolTip("How much haze is in the air before any hazer runs")
        self._haze.valueChanged.connect(self._on_haze_changed)
        layout.addWidget(self._haze)

        # Exposure only means anything with the HDR buffer behind it: on the
        # raster path there is no headroom to expose down into, so the control
        # is only offered when the GPU layer is actually running.
        if getattr(stage_controller, "light_layer", None) is not None:
            layout.addWidget(_separator())
            exposure_label = QLabel("EXPOSURE")
            exposure_label.setObjectName("EditToolbarHazeLabel")
            layout.addWidget(exposure_label)

            self._exposure = QSlider(Qt.Orientation.Horizontal)
            self._exposure.setObjectName("EditToolbarHaze")
            self._exposure.setRange(10, 400)          # 0.1x .. 4.0x
            self._exposure.setFixedWidth(90)
            self._exposure.setValue(
                int(stage_controller.config.window.stage_exposure * 100))
            self._exposure.setToolTip(
                "Exposure before the filmic tonemap — how hard the rig is "
                "driven before highlights roll off to white")
            self._exposure.valueChanged.connect(self._on_exposure_changed)
            layout.addWidget(self._exposure)

        done_button = QPushButton("Done")
        done_button.setObjectName("EditToolbarDone")
        done_button.clicked.connect(lambda: self._on_done())
        layout.addWidget(done_button)

        # Drags only fire in design mode, so persist them straight through.
        stage_controller.fixture_moved.connect(lambda _inst: self._notify())

        self.adjustSize()

    def _on_haze_changed(self, value: int) -> None:
        self._stage.set_haze_baseline(value / 100.0)
        self._notify()

    def _on_exposure_changed(self, value: int) -> None:
        self._stage.set_exposure(value / 100.0)
        self._notify()

    def open_add_fixture(self) -> None:
        """Open the fixture browser and patch what it returns. Public: the
        timeline toolbar's "+ Add track" opens the same flow."""
        dialog = AddFixtureDialog(self._stage, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        type_name, dmx_start, channel, mode = dialog.values()
        if channel in used_midi_channels(self._stage.config):
            QMessageBox.warning(
                self, "Channel in use",
                f"MIDI channel {channel} is already used by another fixture. "
                "Pick a free channel.")
            return
        # Drop new fixtures at the stage centre; the user drags from there.
        rect = self._stage.scene.sceneRect()
        self._stage.add_fixture(
            type_name, dmx_start, int(rect.width() / 2), int(rect.height() / 2),
            channel, mode,
        )
        self._notify()


class FixtureInspector(QWidget):
    """Floating inspector card for the selected fixture (edit mode only).

    Shows the fixture's alias, type (read-only) and DMX start address, plus a
    Delete action. Edits write straight onto the FixtureInstance and funnel
    through ``on_patch_changed`` so the host persists the patch. The host
    shows/hides + repositions it in response to ``fixture_selected``.
    """

    def __init__(self, stage_controller, on_patch_changed, parent=None):
        super().__init__(parent)
        self.setObjectName("FixtureInspector")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(280)
        self._stage = stage_controller
        self._notify = on_patch_changed
        self._current = None  # currently selected FixtureInstance

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(6)

        # Header: title + close (clears the selection, which hides the card).
        header = QHBoxLayout()
        title = QLabel("Inspector")
        title.setObjectName("InspectorTitle")
        header.addWidget(title)
        header.addStretch(1)
        close_button = QPushButton()
        close_button.setIcon(icons.icon("close", "#aaaaaa"))
        close_button.setObjectName("InspectorClose")
        close_button.setFixedSize(22, 22)
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(self._on_close)
        header.addWidget(close_button)
        layout.addLayout(header)

        # IDENTITY
        layout.addWidget(self._section_label("IDENTITY"))
        layout.addWidget(self._field_label("Name"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Unnamed fixture")
        self._name_edit.editingFinished.connect(self._on_name_changed)
        layout.addWidget(self._name_edit)

        # PROFILE
        layout.addWidget(self._section_label("PROFILE"))
        layout.addWidget(self._field_label("Fixture type"))
        self._type_value = QLabel("—")
        self._type_value.setObjectName("InspectorReadonly")
        layout.addWidget(self._type_value)

        # Mode changes the channel layout, so it is only offered when the type
        # declares more than one.
        self._mode_label = self._field_label("Mode")
        layout.addWidget(self._mode_label)
        self._mode_combo = QComboBox()
        self._mode_combo.currentTextChanged.connect(self._on_mode_changed)
        layout.addWidget(self._mode_combo)

        # DMX PATCH
        layout.addWidget(self._section_label("DMX PATCH"))
        layout.addWidget(self._field_label("Start address"))
        self._dmx_spin = QSpinBox()
        self._dmx_spin.setRange(1, 512)
        self._dmx_spin.valueChanged.connect(self._on_dmx_changed)
        layout.addWidget(self._dmx_spin)

        # ROUTING
        layout.addWidget(self._section_label("ROUTING"))
        layout.addWidget(self._field_label("MIDI channel"))
        self._channel_spin = QSpinBox()
        self._channel_spin.setRange(0, 15)
        # Commit on editing-finished (not every step) so the duplicate check and
        # revert don't fight the user mid-type.
        self._channel_spin.editingFinished.connect(self._on_channel_changed)
        layout.addWidget(self._channel_spin)

        layout.addSpacing(8)
        self._delete_button = QPushButton("Delete")
        self._delete_button.setObjectName("InspectorDelete")
        self._delete_button.clicked.connect(self._on_delete)
        layout.addWidget(self._delete_button)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------
    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("InspectorSection")
        return lbl

    def _field_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("InspectorFieldLabel")
        return lbl

    # ------------------------------------------------------------------
    # Population / edits
    # ------------------------------------------------------------------
    def set_instance(self, instance) -> None:
        """Populate the fields from a FixtureInstance (no-op for None — the host
        hides the card instead)."""
        self._current = instance
        if instance is None:
            return
        self._name_edit.blockSignals(True)
        self._name_edit.setText(instance.name)
        self._name_edit.blockSignals(False)
        self._type_value.setText(instance.type)

        modes = fixture_modes(instance.type)
        self._mode_combo.blockSignals(True)
        self._mode_combo.clear()
        self._mode_combo.addItems(modes)
        if modes:
            self._mode_combo.setCurrentText(instance.mode or modes[0])
        self._mode_combo.blockSignals(False)
        self._mode_label.setVisible(len(modes) > 1)
        self._mode_combo.setVisible(len(modes) > 1)

        self._dmx_spin.blockSignals(True)
        self._dmx_spin.setValue(instance.dmx_start)
        self._dmx_spin.blockSignals(False)
        self._channel_spin.blockSignals(True)
        self._channel_spin.setValue(max(0, instance.channel))
        self._channel_spin.blockSignals(False)

    def _on_mode_changed(self, mode: str) -> None:
        if self._current is None or not mode:
            return
        # Rebuilding the render group re-emits fixture_selected, so the host
        # keeps the inspector pointed at the same fixture.
        self._stage.change_fixture_mode(self._current, mode)
        self._stage.refresh_labels()
        self._notify()

    def _on_name_changed(self) -> None:
        if self._current is None:
            return
        new_name = self._name_edit.text().strip()
        if new_name != self._current.name:
            self._current.name = new_name
            self._stage.refresh_labels()
            self._notify()

    def _on_dmx_changed(self, value: int) -> None:
        if self._current is None:
            return
        self._current.dmx_start = int(value)
        self._stage.refresh_labels()
        self._notify()

    def _on_channel_changed(self) -> None:
        if self._current is None:
            return
        value = self._channel_spin.value()
        if value == self._current.channel:
            return
        # Channel is the clip-binding identity, so it must stay unique.
        if value in used_midi_channels(self._stage.config, exclude=self._current):
            QMessageBox.warning(
                self, "Channel in use",
                f"MIDI channel {value} is already used by another fixture.")
            self._channel_spin.blockSignals(True)
            self._channel_spin.setValue(max(0, self._current.channel))
            self._channel_spin.blockSignals(False)
            return
        self._current.channel = int(value)
        self._stage.refresh_labels()
        self._notify()

    def _on_delete(self) -> None:
        if self._current is None:
            return
        name = self._current.name or self._current.type
        detail = ""
        bar_count = len(self._current.bars)
        if bar_count > 1:
            detail = f" All {bar_count} bars will be removed."
        reply = QMessageBox.question(
            self, "Delete fixture",
            f"Delete '{name}'?{detail}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        removed = self._stage.remove_selected()
        if removed:
            self._notify()

    def _on_close(self) -> None:
        self._stage.scene.clearSelection()
