# ABOUTME: Stage scenario designer UI — a top-left toolbar (add / done) plus a
# ABOUTME: per-selection inspector (name / type / DMX / delete). Placement is by drag.
from contextlib import contextmanager

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QCheckBox, QDialog, QFrame, QVBoxLayout, QHBoxLayout,
    QPushButton, QComboBox, QSlider, QSpinBox, QLabel, QLineEdit,
    QMessageBox,
)

from tupperlogger import get_logger
from core.fixture_library import fixture_modes
from ui.widgets import icons
from ui.widgets.toggle_switch import ToggleSwitch
from ui.fixture.browser import AddFixtureDialog, used_midi_channels


def _separator() -> QFrame:
    """A thin vertical rule between groups of toolbar controls."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setObjectName("EditToolbarSeparator")
    return line


@contextmanager
def _blocked(widget):
    """Set a widget's value without it reporting the change back as an edit."""
    previous = widget.blockSignals(True)
    try:
        yield widget
    finally:
        widget.blockSignals(previous)


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
        self.logger = get_logger("ui.fixture.designer")

        # Two rows: the patch and the air on top, the fog's look underneath.
        # The fog controls only exist on the GPU path, so on a machine without
        # one the toolbar is exactly the single row it always was.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 6)
        outer.setSpacing(6)
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        outer.addLayout(layout)

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

        # A read-out, because a slider whose effect is subtle needs to say it
        # moved. Half the "it does nothing" reports about air are about not
        # being sure anything was sent.
        self._haze_value = QLabel(f"{self._haze.value()}%")
        self._haze_value.setObjectName("EditToolbarFogCaption")
        self._haze_value.setFixedWidth(34)
        layout.addWidget(self._haze_value)

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

        self._build_fog_row(outer)

        # Drags only fire in design mode, so persist them straight through.
        stage_controller.fixture_moved.connect(lambda _inst: self._notify())

        self.adjustSize()

    # -- the air -------------------------------------------------------
    def _build_fog_row(self, outer: QVBoxLayout) -> None:
        """The volumetric fog's look, live while the stage is in front of you.

        The switch is here rather than only in Settings, and that is the whole
        point of the row. With it hidden two rooms away, every slider on this
        line silently did nothing: they moved, they were saved, and the stage
        never changed — which is indistinguishable from a broken control, and
        was duly reported as one.

        Only what changes the *look* lives here — how much fog there is is the
        HAZE slider on the row above, and what the march costs is in Settings,
        because neither is something anyone tunes by eye against a rig.
        """
        self._fog_widgets = []
        self._fog_switch = None
        if getattr(self._stage, "light_layer", None) is None:
            return

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        outer.addLayout(row)

        fog = self._stage.config.window.fog

        self._fog_switch = QCheckBox("FOG")
        self._fog_switch.setObjectName("EditToolbarFogSwitch")
        self._fog_switch.setChecked(fog.enabled)
        self._fog_switch.setToolTip(
            "March every view ray through the air instead of scaling each "
            "beam by one stage-wide haze number.\n"
            "Shafts are integrated through the fog that is actually there, so "
            "a beam brightens crossing a bank of smoke.")
        self._fog_switch.toggled.connect(self._on_fog_toggled)
        row.addWidget(self._fog_switch)

        # (key, caption, slider range, scale, width, tooltip)
        #
        # TEXTURE leads, is captioned like a heading and is given nearly twice
        # the travel of the rest, because it is the one control on this row that
        # decides whether the stage reads as air or as a filter laid over the
        # picture — the others tune air you have already got. A slider that
        # matters more is worth more of the toolbar.
        for key, caption, low, high, scale, width, tip in (
            ("texture", "TEXTURE", 0, 300, 100.0, 130,
             "How ragged the air is, and the difference between fog and a "
             "white filter.\n"
             "At 0 the march integrates a perfectly smooth medium — the "
             "translucent-sheet look. Around 1 a pocket of air can be "
             "completely empty, which is what gives a shaft its billows. Past "
             "that the billows separate into banks with clear air between "
             "them: a stage under a smoke machine rather than under a hazer.\n"
             "The air drifts across the stage as it churns, and the bigger "
             "this is the more plainly you can see it move."),
            ("height", "height", 50, 2000, 1.0, 70,
             "How far up from the floor the fog reaches"),
            ("height_falloff", "falloff", 0, 120, 10.0, 70,
             "How sharply it thins with height — haze settles, so a slab of "
             "even density reads as a bug"),
            ("scattering", "scatter", 0, 800, 100.0, 70,
             "How much of the light passing through the fog comes back to you"),
            ("anisotropy", "g", -95, 95, 100.0, 70,
             "Henyey-Greenstein g: forward-scattering, which is why a beam "
             "aimed at you is blinding and the same beam across the stage is "
             "a shaft"),
            ("light_intensity", "ambient", 0, 400, 100.0, 70,
             "A stage-wide key on the medium itself, so smoke is visible "
             "before any fixture is"),
        ):
            lead = key == "texture"
            caption_label = QLabel(caption)
            caption_label.setObjectName(
                "EditToolbarTextureCaption" if lead
                else "EditToolbarFogCaption")
            caption_label.setToolTip(tip)
            row.addWidget(caption_label)

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setObjectName(
                "EditToolbarFogTexture" if lead else "EditToolbarHaze")
            slider.setRange(low, high)
            slider.setFixedWidth(width)
            slider.setValue(int(round(getattr(fog, key) * scale)))
            slider.setToolTip(tip)
            slider.valueChanged.connect(
                lambda value, k=key, s=scale: self._on_fog_changed(k, value / s))
            row.addWidget(slider)
            self._fog_widgets += [caption_label, slider]

        self._fog_color = QPushButton()
        self._fog_color.setObjectName("EditToolbarFogColor")
        self._fog_color.setFixedWidth(28)
        self._fog_color.setToolTip("What colour the air scatters")
        self._fog_color.clicked.connect(self._pick_fog_color)
        self._paint_fog_swatch(fog.color)
        row.addWidget(self._fog_color)
        self._fog_widgets.append(self._fog_color)

        row.addStretch(1)
        self._sync_fog_enabled()

    def _sync_fog_enabled(self) -> None:
        """Grey the shaping controls out while the march is off.

        A control that does nothing has to *say* it does nothing. The switch
        itself stays live, because it is the way back.
        """
        if self._fog_switch is None:
            return
        enabled = self._stage.config.window.fog.enabled
        for widget in self._fog_widgets:
            widget.setEnabled(enabled)
        self._fog_switch.setToolTip(
            self._fog_switch.toolTip().split("\n\n")[0]
            + ("" if enabled else "\n\nOff: the sliders beside this do nothing "
                                  "until it is on."))

    def showEvent(self, event):
        """Re-read the settings the toolbar mirrors.

        Fog can also be switched from Settings, and the toolbar outlives that
        dialog — so a row that only ever greyed itself once, at construction,
        would go on showing the wrong state for the rest of the session.
        """
        super().showEvent(event)
        if self._fog_switch is not None:
            fog = self._stage.config.window.fog
            with _blocked(self._fog_switch):
                self._fog_switch.setChecked(fog.enabled)
            self._sync_fog_enabled()

    def _paint_fog_swatch(self, color: str) -> None:
        self._fog_color.setStyleSheet(
            f"background-color: {color}; border: 1px solid #555; min-height: 16px;")

    def _pick_fog_color(self) -> None:
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QColorDialog

        current = QColor(self._stage.config.window.fog.color)
        chosen = QColorDialog.getColor(current, self, "Scattering colour")
        if not chosen.isValid():
            return
        self._on_fog_changed("color", chosen.name())
        self._paint_fog_swatch(self._stage.config.window.fog.color)

    def _on_fog_toggled(self, on: bool) -> None:
        self._stage.set_fog(enabled=on)
        self._sync_fog_enabled()
        self._notify()

    def _on_fog_changed(self, key: str, value) -> None:
        self._stage.set_fog(**{key: value})
        self._notify()

    def _on_haze_changed(self, value: int) -> None:
        self._stage.set_haze_baseline(value / 100.0)
        self._haze_value.setText(f"{value}%")
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

        # MOUNTING — which way up the fixture is rigged. The switch carries a
        # label on each side and a line saying what the current one does: one
        # word on a toggle only ever names half of it, and the half it names is
        # the state you are not in.
        self._mounting = QWidget()
        mounting = QVBoxLayout(self._mounting)
        mounting.setContentsMargins(0, 0, 0, 0)
        mounting.setSpacing(5)
        mounting.addWidget(self._section_label("MOUNTING"))

        sides = QHBoxLayout()
        sides.setSpacing(8)
        self._floor_label = self._side_label("On the floor")
        self._ceiling_label = self._side_label("Hanging")
        self._hanging_switch = ToggleSwitch()
        self._hanging_switch.toggled.connect(self._on_hanging_toggled)
        sides.addWidget(self._floor_label)
        sides.addWidget(self._hanging_switch)
        sides.addWidget(self._ceiling_label)
        sides.addStretch(1)
        mounting.addLayout(sides)

        self._mounting_hint = QLabel()
        self._mounting_hint.setObjectName("InspectorHint")
        self._mounting_hint.setWordWrap(True)
        # A wrapped QLabel reports the height of ONE line unless its policy
        # says otherwise, and a layout believes it — which cuts the second line
        # off into whatever is underneath.
        policy = self._mounting_hint.sizePolicy()
        policy.setHeightForWidth(True)
        self._mounting_hint.setSizePolicy(policy)
        mounting.addWidget(self._mounting_hint)
        layout.addWidget(self._mounting)

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

    def _side_label(self, text: str) -> QLabel:
        """One end of a two-ended switch. The side in force lights up."""
        lbl = QLabel(text)
        lbl.setObjectName("InspectorSideLabel")
        lbl.setProperty("active", "false")
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

        hanging = bool(getattr(instance, "hanging", True))
        self._mounting.setVisible(self._stage.can_rig_inverted(instance))
        self._hanging_switch.blockSignals(True)
        self._hanging_switch.setChecked(hanging)
        self._hanging_switch.blockSignals(False)
        self._show_mounting(hanging)

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

    def _show_mounting(self, hanging: bool) -> None:
        """Light the side the switch is on, and say what that side does."""
        for label, active in ((self._ceiling_label, hanging),
                              (self._floor_label, not hanging)):
            label.setProperty("active", "true" if active else "false")
            label.style().unpolish(label)
            label.style().polish(label)
        self._mounting_hint.setText(
            "Upside down, beam aimed at the stage."
            if hanging else
            "Upright, beam aimed at the ceiling.")

    def _on_hanging_toggled(self, hanging: bool) -> None:
        self._show_mounting(hanging)
        if self._current is None:
            return
        # Rebuilds the render group, which re-emits fixture_selected — same
        # path a mode change takes, so the inspector stays on this fixture.
        self._stage.set_fixture_hanging(self._current, hanging)
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
