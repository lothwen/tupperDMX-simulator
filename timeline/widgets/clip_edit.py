# ABOUTME: One clip-edit dialog for any fixture — its channels, banks and
# ABOUTME: chases come from the fixture's own definition, never from a table.
"""Editing one timeline clip.

A clip is a MIDI note on a fixture's lane. The note is a *verb*, the MIDI
channel selects the fixture, and the velocity is the verb's parameter — so what
a clip means, and therefore what editing it should look like, is a question only
the fixture at that channel can answer. This dialog asks it:

    ┌ Wild Wash Pro · ch 2 · DMX 33-40 ─────────── at 12.340 s ┐
    │ EVENT TYPE  [ Channels ▸ Red            ▾]               │
    │                                                          │
    │  ▮  200 of 255 — ColorIntensity                          │
    │ ┌ SIMULATED OUTPUT ────────────────────────────────────┐ │
    │ │ intensity 100 %                     <the renderer>   │ │
    │ └──────────────────────────────────────────────────────┘ │
    │ TIME [ 12.340 ] s                          [Cancel] [OK] │
    └──────────────────────────────────────────────────────────┘

Two shapes, one shell. A **scene** clip — several channel verbs at one instant,
sharing a group — gets the full console: a fader per channel over the live
preview. Any other clip gets its verb's own small editor (`verb_editors.py`),
because a chase index and a blend time are not the same control.

Everything offered comes from `note_layout.verb_choices(fdef)`, which is gated
by the same `note_applies_to` drag-and-drop validates with — so the picker can
never author a clip the timeline would have refused as a drop.
"""

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from core import note_layout
from core.fixture_library import FixtureDef, resolve_fixture, save_fixture_banks
from midi.constants import _channel_to_velocity
from midi.scene_editor import TAKE_MANUAL_VELOCITY
from software_controller.scene_loader import load_scenes
from ui.channel_affordances import (
    CapabilityGrid, ColorWheel, affordance_for, apply_color, current_color,
    wheel_applies,
)
from ui.fixture_chips import chip
from ui.fixture_console import (
    FaderStrip, OutputCard, default_values, fields_to_values, values_to_fields,
)
from tupperlogger import get_logger
from .verb_editors import ChannelValueEditor, default_velocity_for, editor_class_for

logger = get_logger("timeline.clip_edit")

DEFAULT_BANK_NAME = "SCENES"

# Fader drags fire per pixel; coalesce before hitting a serial DMX port.
LIVE_THROTTLE_MS = 30

# The channel verbs are MIDI notes 1-23, so a fixture's 24th slot and beyond
# cannot be driven from the timeline at all.
ADDRESSABLE_CHANNELS = note_layout.CHANNEL_OFFSET_RANGE[1]

# Sentinel put on the picker while a brand-new clip has no verb yet.
_UNSET = -1
# Sentinel for "make this a scene instead", offered on the add path.
_NEW_SCENE = -2


class _CapabilityPopover(QWidget):
    """The discrete-channel picker, floating over the fader that opened it."""

    picked = pyqtSignal(int)

    def __init__(self, spec, value: int, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("ClipEditPopover")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        title = QLabel(spec.label or spec.field)
        title.setObjectName("AddFxCardTitle")
        layout.addWidget(title)
        self.grid = CapabilityGrid()
        self.grid.set_channel(spec)
        self.grid.set_value(value)
        self.grid.value_changed.connect(self._on_picked)
        layout.addWidget(self.grid)

    def _on_picked(self, value: int) -> None:
        self.picked.emit(value)
        self.close()

    def popup_near(self, anchor) -> None:
        self.adjustSize()
        point = anchor.mapToGlobal(anchor.rect().topLeft())
        self.move(point.x() - self.width() // 2, point.y() - self.height())
        self.show()


@dataclass(frozen=True)
class ClipEditResult:
    """What the dialog decided. `kind` says which write the caller should make.

    "scene"     -> update_scene(group_id, channels, name, remove=removed)
    "new_scene" -> add_scene(time, channels, name, channel)
    "clip"      -> edit_event(event_id, note=note, velocity=velocity)
    """
    kind: str
    time: float
    name: str = ""
    channels: Dict[str, int] = field(default_factory=dict)
    removed: FrozenSet[str] = frozenset()
    note: int = 0
    velocity: int = 0


class ClipEditDialog(QDialog):
    """Edit one clip against the fixture it lives on."""

    #: [(channel, note, velocity), ...] to audition live. Phase 5 wires this.
    live_messages = pyqtSignal(list)
    #: A fixture-library write happened; whoever shows the library should reload.
    library_changed = pyqtSignal()

    # -- construction ------------------------------------------------------
    def __init__(self, *, fdef: Optional[FixtureDef], type_name: str,
                 channel, dmx_start: int, time: float,
                 song_duration: float = 3600.0, locked: bool = False,
                 parent=None):
        super().__init__(parent)
        self._fdef = fdef
        self._type_name = type_name
        self._channel = channel
        self._dmx_start = dmx_start
        self._locked = locked
        self._kind = "clip"
        self._is_new = False
        # Which channel fields the scene actually carries, and which the user
        # moved. Only their union is submitted — see _result_channels.
        self._present: Set[int] = set()
        self._touched: Set[int] = set()
        self._removed: Set[int] = set()
        self._editors: Dict[int, object] = {}
        # Live output: what the rig looked like when this opened, whether it was
        # ever armed (so Cancel knows there is something to undo), and whether
        # the sequencer is running (which would fight us for the same slots).
        self._original_messages: List[tuple] = []
        self._live_ever_armed = False
        self._reverted = False
        self._playing = False

        self.setObjectName("ClipEdit")
        self.setModal(True)
        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(LIVE_THROTTLE_MS)
        self._live_timer.timeout.connect(self._emit_live)
        self._build_ui(time, song_duration)

    @classmethod
    def for_scene(cls, *, fdef, type_name, channel, dmx_start, name,
                  channels: Dict[str, int], present: Set[str], time,
                  song_duration=3600.0, locked=False, parent=None):
        """A scene group: the full console, one fader per channel."""
        dialog = cls(fdef=fdef, type_name=type_name, channel=channel,
                     dmx_start=dmx_start, time=time,
                     song_duration=song_duration, locked=locked, parent=parent)
        dialog._enter_scene(name, channels, present)
        return dialog

    @classmethod
    def for_clip(cls, *, fdef, type_name, channel, dmx_start, note, velocity,
                 time, song_duration=3600.0, is_new=False, locked=False,
                 parent=None):
        """A single-verb clip, or a brand-new one when `is_new`."""
        dialog = cls(fdef=fdef, type_name=type_name, channel=channel,
                     dmx_start=dmx_start, time=time,
                     song_duration=song_duration, locked=locked, parent=parent)
        dialog._enter_clip(note, velocity, is_new)
        return dialog

    # -- layout ------------------------------------------------------------
    def _build_ui(self, time: float, song_duration: float) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)
        layout.addLayout(self._build_header())

        self.body = QStackedWidget()
        self.body.setObjectName("ClipEditBody")
        self.body.addWidget(self._build_scene_page())
        self.body.addWidget(self._build_clip_page())
        layout.addWidget(self.body, 1)

        time_row = QHBoxLayout()
        time_caption = QLabel("TIME")
        time_caption.setObjectName("AddFxFieldLabel")
        time_row.addWidget(time_caption)
        self.time_spin = QDoubleSpinBox()
        self.time_spin.setDecimals(3)
        self.time_spin.setRange(0.0, max(song_duration, time + 1.0))
        self.time_spin.setSingleStep(0.1)
        self.time_spin.setSuffix(" s")
        self.time_spin.setValue(max(0.0, float(time)))
        time_row.addWidget(self.time_spin)

        # Never on by default and never remembered: this dialog can be opened
        # in front of an audience, and a fader drag would be visible.
        self.live_check = QCheckBox("Live output")
        self.live_check.setToolTip(
            "Send these values to the rig while you edit.\n"
            "Cancel re-sends what the clip held when this opened — that is a "
            "re-send, not a restore: it cannot put back what the automatic "
            "program was doing.")
        self.live_check.toggled.connect(self._on_live_toggled)
        time_row.addWidget(self.live_check)
        time_row.addStretch(1)
        self.status_label = QLabel()
        self.status_label.setObjectName("AddFxStatus")
        self.status_label.setWordWrap(True)
        time_row.addWidget(self.status_label, 2)
        layout.addLayout(time_row)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _apply_lock(self) -> None:
        """A locked lane is read-only everywhere else — drops already check it;
        the edit dialog was the one door that did not. Applied AFTER a mode is
        entered, so its own status line does not overwrite this one."""
        if not self._locked:
            return
        for widget in (self.time_spin, self.scene_name, self.verb_combo,
                       self.param_stack, self.load_button, self.save_button,
                       self.live_check):
            widget.setEnabled(False)
        self.scene_faders.set_editable(False)
        self.scene_faders.setEnabled(False)
        self.buttons.button(
            QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._say("This fixture's lane is locked — unlock it to edit.",
                  error=True)

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        title = QLabel(self._fdef.name if self._fdef else (self._type_name or "Clip"))
        title.setObjectName("AddFxCardTitle")
        row.addWidget(title)
        if self._fdef is not None and self._fdef.mode:
            row.addWidget(chip(self._fdef.mode, "#4fb0c0"))
        if self._channel is not None:
            span = self._fdef.channel_count if self._fdef else 0
            where = f"ch {self._channel}"
            if span:
                where += f" · DMX {self._dmx_start}-{self._dmx_start + span - 1}"
            row.addWidget(chip(where, "#8f8f8f"))
        row.addStretch(1)
        return row

    def _build_scene_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        name_row = QHBoxLayout()
        caption = QLabel("SCENE NAME")
        caption.setObjectName("AddFxFieldLabel")
        name_row.addWidget(caption)
        self.scene_name = QLineEdit()
        self.scene_name.setObjectName("AddFxSceneName")
        self.scene_name.setPlaceholderText("Scene")
        name_row.addWidget(self.scene_name, 1)

        self.load_button = QToolButton()
        self.load_button.setText("Load from library ▾")
        self.load_button.setObjectName("AddFxSceneAction")
        self.load_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self.load_menu = QMenu(self.load_button)
        self.load_button.setMenu(self.load_menu)
        name_row.addWidget(self.load_button)

        self.save_button = QPushButton("Save as scene…")
        self.save_button.setObjectName("AddFxSceneSave")
        self.save_button.clicked.connect(self._save_to_library)
        name_row.addWidget(self.save_button)
        layout.addLayout(name_row)

        self.library_hint = QLabel(
            "Loading copies the values into this clip. The clip stays a manual "
            "scene and will not follow later library edits — for one that "
            "does, use a Scene verb instead.")
        self.library_hint.setObjectName("AddFxHint")
        self.library_hint.setWordWrap(True)
        layout.addWidget(self.library_hint)

        self.scene_output = OutputCard()
        layout.addWidget(self.scene_output, 1)

        # Dialling a colour on three or four separate faders is guesswork, so
        # a fixture that mixes one gets a wheel. Shown only when solve_color
        # can actually place a hue on it.
        self.color_row = QWidget()
        color_layout = QHBoxLayout(self.color_row)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.setSpacing(10)
        color_caption = QLabel("COLOUR")
        color_caption.setObjectName("AddFxFieldLabel")
        color_layout.addWidget(color_caption)
        self.color_wheel = ColorWheel()
        self.color_wheel.setFixedSize(140, 140)
        self.color_wheel.color_changed.connect(self._on_color_picked)
        color_layout.addWidget(self.color_wheel)
        color_hint = QLabel(
            "Fitted to whatever this fixture actually mixes — its other "
            "emitters are zeroed so a stale colour cannot tint the result.")
        color_hint.setObjectName("AddFxHint")
        color_hint.setWordWrap(True)
        color_layout.addWidget(color_hint, 1)
        layout.addWidget(self.color_row)

        self.scene_faders = FaderStrip()
        self.scene_faders.value_changed.connect(self._on_fader_changed)
        self.scene_faders.name_clicked.connect(self._on_channel_name_clicked)
        layout.addWidget(self.scene_faders)
        return page

    def _build_clip_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        verb_row = QHBoxLayout()
        caption = QLabel("EVENT TYPE")
        caption.setObjectName("AddFxFieldLabel")
        verb_row.addWidget(caption)
        self.verb_combo = QComboBox()
        self.verb_combo.setObjectName("ClipEditVerb")
        self.verb_combo.currentIndexChanged.connect(self._on_verb_changed)
        verb_row.addWidget(self.verb_combo, 1)
        layout.addLayout(verb_row)

        self.param_stack = QStackedWidget()
        self.param_stack.setObjectName("ClipEditParam")
        layout.addWidget(self.param_stack)

        self.clip_output = OutputCard()
        layout.addWidget(self.clip_output, 1)
        return page

    # -- entering a mode ---------------------------------------------------
    def _enter_scene(self, name: str, channels: Dict[str, int],
                     present: Set[str]) -> None:
        self._kind = "scene"
        self.setWindowTitle(f"Edit scene — {name}" if name else "Edit scene")
        self.body.setCurrentIndex(0)
        self.scene_name.setText(name)

        fields = self._fields()
        self._present = {i for i, f in enumerate(fields) if f in present}
        self.scene_faders.set_fixture(self._fdef, self._dmx_start)
        self.scene_faders.set_addressable_limit(ADDRESSABLE_CHANNELS)
        self.scene_faders.set_present(self._present)
        self.scene_faders.set_values(fields_to_values(self._fdef, channels))
        self._mark_pickable_channels()
        self.scene_output.set_fixture(self._fdef)
        self.scene_output.set_values(self.scene_faders.values())
        self.color_row.setVisible(wheel_applies(self._fdef))
        self._sync_color_wheel()
        self._rebuild_library_menu()
        self.resize(880, 760)
        self._refresh_scene_status()
        self._apply_lock()

    def _enter_clip(self, note: Optional[int], velocity: int,
                    is_new: bool) -> None:
        self._kind = "clip"
        self._is_new = is_new
        self.setWindowTitle("Add clip" if is_new else "Edit clip")
        self.body.setCurrentIndex(1)
        self.clip_output.set_fixture(self._fdef)
        self._populate_verbs(is_new)
        self._select_verb(_UNSET if is_new else int(note or 0), velocity)
        self.resize(720, 620)
        self._apply_lock()

    def _populate_verbs(self, is_new: bool) -> None:
        self.verb_combo.blockSignals(True)
        self.verb_combo.clear()
        if is_new:
            self.verb_combo.addItem("Select event type…", _UNSET)
            if self._fdef is not None:
                self.verb_combo.addItem("New scene (all channels)…", _NEW_SCENE)
        for group, entries in note_layout.verb_choices(self._fdef):
            self._add_header(group)
            for note, label, _color in entries:
                self.verb_combo.addItem(f"    {label}", note)
        self.verb_combo.blockSignals(False)

    def _add_header(self, text: str) -> None:
        """A non-selectable row that titles the group under it."""
        self.verb_combo.addItem(text, None)
        index = self.verb_combo.count() - 1
        item = self.verb_combo.model().item(index)
        if item is not None:
            item.setFlags(Qt.ItemFlag.NoItemFlags)

    def _select_verb(self, note: int, velocity: int) -> None:
        for index in range(self.verb_combo.count()):
            if self.verb_combo.itemData(index) == note:
                self.verb_combo.setCurrentIndex(index)
                break
        else:
            # A clip whose verb no longer applies to its fixture — a mode
            # change can do this. Say so rather than silently retyping it.
            self.verb_combo.insertItem(
                0, f"    Note {note} (not valid on this fixture)", note)
            self.verb_combo.setCurrentIndex(0)
            self._say(f"Note {note} has no meaning on {self._type_name}. "
                      "Pick a verb it does support.", error=True)
        self._build_param_editor(velocity)
        self._refresh_ok()

    # -- verb editors ------------------------------------------------------
    def _build_param_editor(self, velocity: Optional[int] = None) -> None:
        note = self.current_note()
        if note is None or note < 0:
            self.param_stack.setVisible(False)
            self.clip_output.setVisible(False)
            return
        self.param_stack.setVisible(True)

        editor = self._editors.get(note)
        if editor is None:
            editor = editor_class_for(note)()
            editor.set_context(self._fdef, note)
            editor.changed.connect(self._on_param_changed)
            self._editors[note] = editor
            self.param_stack.addWidget(editor)
        editor.set_velocity(default_velocity_for(note)
                            if velocity is None else velocity)
        self.param_stack.setCurrentWidget(editor)

        # Only a channel verb has anything for the stage to show.
        is_channel = isinstance(editor, ChannelValueEditor)
        self.clip_output.setVisible(is_channel)
        if is_channel:
            self.clip_output.set_values(editor.slice_values())

    def _on_verb_changed(self, _index: int) -> None:
        # Retyping crosses verb classes, where the same velocity means something
        # else entirely — so the new verb's own default is what loads.
        self._build_param_editor(None)
        self._refresh_ok()

    def _on_param_changed(self) -> None:
        editor = self.param_stack.currentWidget()
        if isinstance(editor, ChannelValueEditor):
            self.clip_output.set_values(editor.slice_values())
        self._push_live()

    def _on_fader_changed(self, index: int, _value: int) -> None:
        self._touched.add(index)
        self._removed.discard(index)
        self.scene_faders.set_present(self._present | self._touched)
        self.scene_output.set_values(self.scene_faders.values())
        self._sync_color_wheel()
        self._refresh_scene_status()
        self._push_live()

    # -- colour wheel ------------------------------------------------------
    def _on_color_picked(self, color) -> None:
        """A hue off the wheel, fitted onto whatever this fixture mixes."""
        before = self.scene_faders.values()
        after = apply_color(self._fdef, before, color)
        for index, (old, new) in enumerate(zip(before, after)):
            if old == new:
                continue
            fader = self.scene_faders.fader(index)
            if fader is not None:
                fader.set_value(new)          # silent — we report once below
            self._touched.add(index)
            self._removed.discard(index)
        self.scene_faders.set_present(self._present | self._touched)
        self.scene_output.set_values(self.scene_faders.values())
        self._refresh_scene_status()
        self._push_live()

    def _sync_color_wheel(self) -> None:
        """Follow the faders. `set_rgb` does not emit, so no feedback loop."""
        if not self.color_row.isVisible():
            return
        red, green, blue = current_color(self._fdef, self.scene_faders.values())
        self.color_wheel.set_rgb(red, green, blue)

    # -- discrete channels -------------------------------------------------
    def _mark_pickable_channels(self) -> None:
        """Show, on the faders themselves, which channels open a picker.

        Without this the picker is a hidden gesture on an unmarked 9 px label —
        present in the code and undiscoverable in the app."""
        if self._fdef is None:
            self.scene_faders.set_pickable(set())
            return
        pickable = {index for index in range(self._fdef.channel_count)
                    if affordance_for(self._fdef, index) == "grid"}
        self.scene_faders.set_pickable(
            pickable,
            "Click to choose from this channel's own options — they preview "
            "side by side, all playing at once.")

    def _on_channel_name_clicked(self, index: int) -> None:
        """A channel whose capabilities name discrete choices opens a picker
        instead of asking the user to hunt for the value on a fader."""
        if self._locked or affordance_for(self._fdef, index) != "grid":
            return
        fader = self.scene_faders.fader(index)
        if fader is None:
            return
        popover = _CapabilityPopover(fader.spec, fader.value(), self)
        popover.picked.connect(
            lambda value, i=index: self._apply_capability(i, value))
        popover.popup_near(fader.name_label)

    def _apply_capability(self, index: int, value: int) -> None:
        fader = self.scene_faders.fader(index)
        if fader is None:
            return
        fader.set_value(value)
        self._on_fader_changed(index, value)

    # -- state -------------------------------------------------------------
    def current_note(self) -> Optional[int]:
        data = self.verb_combo.currentData()
        return None if data is None else int(data)

    def _fields(self) -> List[str]:
        return [spec.field for spec in self._fdef.channels] if self._fdef else []

    def _refresh_ok(self) -> None:
        note = self.current_note()
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if self._kind == "scene":
            ok.setEnabled(not self._locked)
            return
        ok.setEnabled(not self._locked and note is not None and note != _UNSET)
        if note == _NEW_SCENE:
            self._switch_to_new_scene()

    def _switch_to_new_scene(self) -> None:
        """The add path's "make this a scene" entry: same dialog, other page."""
        self._kind = "new_scene"
        self.setWindowTitle("Add scene")
        self.body.setCurrentIndex(0)
        self.scene_faders.set_fixture(self._fdef, self._dmx_start)
        self.scene_faders.set_addressable_limit(ADDRESSABLE_CHANNELS)
        self._present = set()
        self.scene_faders.set_present(set())
        self.scene_faders.set_values(default_values(self._fdef))
        self._mark_pickable_channels()
        self.scene_output.set_fixture(self._fdef)
        self.scene_output.set_values(self.scene_faders.values())
        self.color_row.setVisible(wheel_applies(self._fdef))
        self._sync_color_wheel()
        self._rebuild_library_menu()
        self.resize(880, 760)
        self._refresh_scene_status()
        self.buttons.button(
            QDialogButtonBox.StandardButton.Ok).setEnabled(not self._locked)

    # -- live output -------------------------------------------------------
    def set_transport_playing(self, playing: bool) -> None:
        """Disarm and lock out live output while the sequencer runs — its own
        note_ons and ours would fight over the same slots every tick."""
        self._playing = bool(playing)
        if playing and self.live_check.isChecked():
            self.live_check.setChecked(False)      # reverts via _on_live_toggled
        self.live_check.setEnabled(not playing and not self._locked)
        self.live_check.setToolTip(
            "Disabled while the sequencer is playing."
            if playing else self.live_check.toolTip())

    def _capture_original(self) -> None:
        """What the clip was sending when this opened — Cancel's undo."""
        self._original_messages = list(self._live_messages())

    def _live_messages(self) -> List[tuple]:
        """`[(channel, note, velocity), ...]` for the look currently dialled in.

        A scene and a channel verb both LEAD WITH note 48 (take manual): a bare
        channel set is overwritten by the next auto frame, which would make the
        preview useless. The other verbs are their own commands and get none.
        """
        if self._channel is None:
            return []
        channel = int(self._channel)
        if self._kind in ("scene", "new_scene"):
            fields = self._fields()
            values = self.scene_faders.values()
            wanted = sorted((self._present | self._touched) - self._removed)
            messages = [(channel, note_layout.TAKE_MANUAL, TAKE_MANUAL_VELOCITY)]
            for index, (_field, note) in enumerate(
                    note_layout.field_note_map(self._fdef) if self._fdef else []):
                if index in wanted and index < len(values):
                    messages.append((channel, note,
                                     _channel_to_velocity(values[index])))
            return messages

        note = self.current_note()
        if note is None or note < 0:
            return []
        editor = self.param_stack.currentWidget()
        velocity = (editor.velocity() if editor is not None
                    else default_velocity_for(note))
        if isinstance(editor, ChannelValueEditor):
            return [(channel, note_layout.TAKE_MANUAL, TAKE_MANUAL_VELOCITY),
                    (channel, note, int(velocity))]
        return [(channel, note, int(velocity))]

    def _on_live_toggled(self, on: bool) -> None:
        if on:
            if not self._live_ever_armed:
                self._capture_original()
            self._live_ever_armed = True
            self._reverted = False
            self._push_live()
            return
        self._revert_live()

    def _push_live(self) -> None:
        """Queue a live send. Coalesced — a fader drag fires per pixel."""
        if not self.live_check.isChecked() or self._playing or self._locked:
            return
        self._live_timer.start()

    def _emit_live(self) -> None:
        messages = self._live_messages()
        if messages:
            self.live_messages.emit(messages)

    def _revert_live(self) -> None:
        """Re-send what the clip held on open. Guarded so reject() and
        closeEvent cannot both fire it."""
        if not self._live_ever_armed or self._reverted:
            return
        self._reverted = True
        self._live_timer.stop()
        if self._original_messages:
            self.live_messages.emit(list(self._original_messages))

    def reject(self):
        self._revert_live()
        super().reject()

    def accept(self):
        # Leave the rig showing what was just committed.
        if self._live_ever_armed and self.live_check.isChecked():
            self._live_timer.stop()
            self._emit_live()
        self._reverted = True
        super().accept()

    # -- fixture library ---------------------------------------------------
    def _rebuild_library_menu(self) -> None:
        """Bank submenus over the fixture's own stored scenes.

        Read through `load_scenes` directly rather than the SceneEditor's cache,
        which is keyed per process and would keep answering with the pre-save
        banks after this dialog writes one.
        """
        self.load_menu.clear()
        scenes = self._library_scenes()
        if not scenes:
            action = self.load_menu.addAction("No stored scenes")
            action.setEnabled(False)
            self.load_button.setEnabled(False)
            return
        self.load_button.setEnabled(True)
        by_bank: Dict[str, list] = {}
        for scene in scenes:
            by_bank.setdefault(scene.bank, []).append(scene)
        for bank, bank_scenes in by_bank.items():
            submenu = self.load_menu.addMenu(bank)
            for scene in sorted(bank_scenes, key=lambda s: s.index):
                action = submenu.addAction(scene.name)
                action.triggered.connect(
                    lambda _checked=False, s=scene: self._load_from_library(s))

    def _library_scenes(self) -> list:
        if self._fdef is None or not self._type_name:
            return []
        try:
            return load_scenes(self._type_name, self._fdef.mode)
        except Exception as exc:                       # a broken sidecar
            logger.warning(f"Could not read scenes for {self._type_name}: {exc}")
            return []

    def _load_from_library(self, scene) -> None:
        """Copy a stored scene onto the faders. Every channel it names joins
        the clip — that is what the user asked for by picking it."""
        fields = self._fields()
        self.scene_faders.set_values(fields_to_values(self._fdef, scene.values))
        self._touched |= {i for i, field in enumerate(fields)
                          if field in scene.values}
        self._removed -= self._touched
        self.scene_faders.set_present(self._present | self._touched)
        self.scene_output.set_values(self.scene_faders.values())
        if not self.scene_name.text().strip():
            self.scene_name.setText(scene.name)
        self._say(f"Loaded “{scene.name}” from {scene.bank}.")

    def _save_to_library(self) -> None:
        """Write the faders into the fixture's own scene banks.

        Deliberately NOT part of OK: this edits the fixture *library*, which
        every show using the type shares, while OK edits one clip in this show.
        Folding them together would make Cancel a lie about what is on disk.
        """
        if self._fdef is None or not self._type_name:
            return
        banks = {name: [dict(scene) for scene in scenes]
                 for name, scenes in self._fdef.banks.items()}
        bank = self._ask_bank(list(banks))
        if bank is None:
            return
        default_name = self.scene_name.text().strip() or "Scene"
        name, ok = QInputDialog.getText(self, "Save as scene", "Scene name:",
                                        text=default_name)
        name = name.strip()
        if not ok or not name:
            return

        scene = values_to_fields(self._fdef, self.scene_faders.values())
        scene["name"] = name
        target = banks.setdefault(bank, [])
        existing = next((i for i, s in enumerate(target)
                         if str(s.get("name", "")) == name), None)
        if existing is not None:
            answer = QMessageBox.question(
                self, "Scene exists",
                f"“{name}” already exists in {bank}.\n\n"
                f"Overwrite it? Choosing No adds a second scene with the same "
                f"name — a saved .mid binds a scene by INDEX, so overwriting "
                f"in place is the only option that leaves existing shows alone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes)
            if answer == QMessageBox.StandardButton.Cancel:
                return
            if answer == QMessageBox.StandardButton.Yes:
                # Keep the keys the sidecar already had for this scene, so
                # song_parts tags and anything unmodelled survive the write.
                merged = dict(target[existing])
                merged.update(scene)
                target[existing] = merged
            else:
                target.append(scene)
        else:
            target.append(scene)

        path = save_fixture_banks(self._type_name, self._fdef.mode, banks)
        if path is None:
            self._say("Could not save — see the log.", error=True)
            return
        # save_fixture_banks clears the library cache, so the def in hand is
        # now stale: re-resolve before anything else reads its banks.
        refreshed = resolve_fixture(self._type_name, self._fdef.mode)
        if refreshed is not None:
            self._fdef = refreshed
            self.scene_output.set_fixture(self._fdef)
            self.scene_output.set_values(self.scene_faders.values())
        self._rebuild_library_menu()
        self.library_changed.emit()
        self._say(f"Saved “{name}” to {bank} in the fixture library.")

    def _ask_bank(self, banks: List[str]) -> Optional[str]:
        choices = banks + ["New bank…"]
        current = self.scene_name.text().strip()
        pick, ok = QInputDialog.getItem(self, "Save as scene", "Bank:",
                                        choices, 0, False)
        if not ok:
            return None
        if pick != "New bank…":
            return pick
        name, ok = QInputDialog.getText(self, "New bank", "Bank name:",
                                        text=DEFAULT_BANK_NAME)
        name = name.strip().upper()
        return name if ok and name else None

    def _refresh_scene_status(self) -> None:
        unreachable = self.scene_faders.unaddressable()
        if unreachable:
            self._say(
                f"{len(unreachable)} channel(s) past DMX slot "
                f"{ADDRESSABLE_CHANNELS} cannot be driven from the timeline — "
                f"the channel verbs are MIDI notes 1-{ADDRESSABLE_CHANNELS}.")
            return
        untouched = sorted(set(range(len(self.scene_faders)))
                           - self._present - self._touched)
        if untouched:
            self._say(f"{len(untouched)} channel(s) are not part of this scene "
                      f"(shown dimmed). Move one to add it.")
            return
        self._say("")

    def _say(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("state", "error" if error else "info")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    # -- result ------------------------------------------------------------
    def get_values(self) -> Optional[ClipEditResult]:
        time = self.time_spin.value()
        if self._kind in ("scene", "new_scene"):
            return ClipEditResult(
                kind=self._kind,
                time=time,
                name=self.scene_name.text().strip() or "Scene",
                channels=self._result_channels(),
                removed=frozenset(self._fields()[i] for i in sorted(self._removed)
                                  if i < len(self._fields())),
            )
        note = self.current_note()
        if note is None or note < 0:
            return None
        editor = self.param_stack.currentWidget()
        velocity = (editor.velocity() if editor is not None
                    else default_velocity_for(note))
        return ClipEditResult(kind="clip", time=time, note=note,
                              velocity=int(velocity))

    def _result_channels(self) -> Dict[str, int]:
        """Only the channels the scene already carried plus the ones the user
        moved.

        Submitting every fader instead would silently widen every scene it
        touched: at runtime an absent channel keeps whatever the previous frame
        left in it, while a present one at its default FORCES that value. Those
        are different looks, so an untouched channel stays out.
        """
        fields = self._fields()
        values = self.scene_faders.values()
        wanted = (self._present | self._touched) - self._removed
        # Whatever the fixture is, only the addressable head can be authored.
        return {fields[i]: values[i] for i in sorted(wanted)
                if i < len(fields) and i < ADDRESSABLE_CHANNELS}

    def closeEvent(self, event):
        # Closing the window is a cancel; _revert_live is guarded so this and
        # reject() cannot both send.
        self._revert_live()
        self._live_timer.stop()
        # The preview bodies own animation timers on the shared 30 Hz clock and
        # keep ticking (and holding this dialog alive) unless they are dropped.
        self.scene_output.clear()
        self.clip_output.clear()
        super().closeEvent(event)
