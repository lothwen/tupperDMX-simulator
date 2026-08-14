# ABOUTME: One small editor per class of MIDI verb — what its velocity means,
# ABOUTME: as a control. Picked from a table, so a new verb is a table row.
"""Velocity editors, one per verb class.

A clip's note is a *verb* and its velocity is that verb's parameter — but what
the parameter MEANS changes completely with the verb: a channel verb's velocity
is `value // 2`, a chase verb's is a chase index, a blend-time verb's is
tenths of a second, START's packs a hue and a palette mode into one byte. One
spin box labelled "Velocity" is the wrong control for all of them.

So each class gets a control that speaks its own units, behind one protocol:

    editor.set_context(fdef, note)   # which fixture, which verb
    editor.set_velocity(v)           # load
    editor.velocity() -> int         # store
    editor.hint() -> str             # one line: what this does at runtime
    editor.changed                   # emitted on any user edit

`editor_for(note, fdef)` picks one from a table whose predicates mirror the
match arms of `software_controller.main.handle_note_on` — keep the two in step.

Deliberately velocity-only: time, fixture and scene values belong to the dialog
around these, not to any one verb.
"""

from typing import Callable, List, Optional, Tuple

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from core import note_layout, ofl
from core.fixture_library import FixtureDef
from core.fixture_state import capability_at
from midi.constants import _channel_to_velocity
from ui.fixture.console import ChannelFader, default_values

# Velocity for a verb that takes no parameter. Must be non-zero: a note_on at
# velocity 0 reads as a note_off. Matches the take-manual convention.
INERT_VELOCITY = 64

# velocity * 100 ms is the controller's timing unit, so 127 is the ceiling.
MAX_TIME_SECONDS = 12.7


class VerbEditor(QWidget):
    """Base: a verb whose velocity carries nothing to edit."""

    changed = pyqtSignal()

    #: Velocity to fall back to when a clip is retyped INTO this verb class.
    #: Never carry the old one over — the same number means something else.
    default_velocity = INERT_VELOCITY

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fdef: Optional[FixtureDef] = None
        self._note = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._layout = layout

    def set_context(self, fdef: Optional[FixtureDef], note: int) -> None:
        self._fdef = fdef
        self._note = int(note)
        self._rebuild()

    def _rebuild(self) -> None:
        """Re-read anything that depends on the fixture or the note."""

    def set_velocity(self, velocity: int) -> None:
        pass

    def velocity(self) -> int:
        return self.default_velocity

    def hint(self) -> str:
        return ""


class NoParamEditor(VerbEditor):
    """A verb that is entirely itself: the note IS the instruction."""

    _HINTS = {
        note_layout.TAKE_MANUAL:
            "Takes manual control of the fixture, so the song-part program "
            "stops driving it.",
        note_layout.MANUAL:
            "Drops the fixture to manual, holding whatever frame it is on.",
        note_layout.BLACKOUT_FIXTURE:
            "Blacks out this fixture only.",
        note_layout.BLACKOUT_ALL:
            "Blacks out every fixture. A toggle — a second one restores.",
        note_layout.PALETTE_RANDOM_ON:
            "The fixture picks its palette colour at random from here on.",
        note_layout.PALETTE_RANDOM_OFF:
            "The fixture walks its palette in order from here on.",
        note_layout.SCENE_PREV:
            "Steps back one scene in the fixture's current bank.",
        note_layout.SCENE_NEXT:
            "Steps forward one scene in the fixture's current bank.",
        note_layout.START:
            "Initialises the palette. Edit the colours with the palette "
            "editor.",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._label = QLabel()
        self._label.setObjectName("AddFxHint")
        self._label.setWordWrap(True)
        self._layout.addWidget(self._label)

    def _rebuild(self) -> None:
        self._label.setText(self.hint())

    def hint(self) -> str:
        if note_layout.is_song_part(self._note):
            name = note_layout.SONG_PARTS[self._note - note_layout.SONG_PART_BASE]
            return (f"Marks the {name} section. Every fixture in auto mode "
                    f"loads whatever it has tagged for it.")
        if note_layout.COLOR_MACRO_BASE <= self._note < (
                note_layout.COLOR_MACRO_BASE + len(note_layout.COLOR_MACROS)):
            macro = note_layout.COLOR_MACROS[
                self._note - note_layout.COLOR_MACRO_BASE]
            return (f"Sets the fixture to {macro.lower()}, fitted to whatever "
                    f"emitters it actually has.")
        if note_layout.BANK_SELECT_RANGE[0] <= self._note \
                <= note_layout.BANK_SELECT_RANGE[1]:
            return ("Selects the bank the scene verbs then index into. "
                    "The note is the choice.")
        return self._HINTS.get(self._note, "")


class TimeEditor(VerbEditor):
    """Blend / colour-change / chase-change time: velocity is tenths of a
    second (the controller multiplies by 100 ms)."""

    default_velocity = 0

    _HINTS = {
        note_layout.BLEND_TIME: "How long the fixture takes to cross-fade "
                                "between looks.",
        note_layout.COLOR_CHANGE_TIME: "How long a palette colour change takes.",
        note_layout.CHASE_CHANGE_TIME: "How long a chase step takes.",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self._spin = QDoubleSpinBox()
        self._spin.setDecimals(1)
        self._spin.setRange(0.0, MAX_TIME_SECONDS)
        self._spin.setSingleStep(0.1)
        self._spin.setSuffix(" s")
        self._spin.valueChanged.connect(lambda _v: self.changed.emit())
        row.addWidget(self._spin)
        row.addStretch(1)
        self._layout.addLayout(row)
        self._label = QLabel()
        self._label.setObjectName("AddFxHint")
        self._label.setWordWrap(True)
        self._layout.addWidget(self._label)

    def _rebuild(self) -> None:
        self._label.setText(self.hint())

    def set_velocity(self, velocity: int) -> None:
        self._spin.blockSignals(True)
        self._spin.setValue(max(0, min(127, int(velocity))) / 10.0)
        self._spin.blockSignals(False)

    def velocity(self) -> int:
        return max(0, min(127, round(self._spin.value() * 10)))

    def hint(self) -> str:
        return self._HINTS.get(self._note, "")


class SceneLoadEditor(VerbEditor):
    """Scene load (24-35) and the prev/next steps: the note picks the scene,
    the velocity is the blend time to get there."""

    default_velocity = 0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._target = QLabel()
        self._target.setObjectName("AddFxRowValue")
        self._target.setWordWrap(True)
        self._layout.addWidget(self._target)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        caption = QLabel("blend")
        caption.setObjectName("AddFxFieldLabel")
        row.addWidget(caption)
        self._spin = QDoubleSpinBox()
        self._spin.setDecimals(1)
        self._spin.setRange(0.0, MAX_TIME_SECONDS)
        self._spin.setSingleStep(0.1)
        self._spin.setSuffix(" s")
        self._spin.valueChanged.connect(lambda _v: self.changed.emit())
        row.addWidget(self._spin)
        row.addStretch(1)
        self._layout.addLayout(row)

        self._label = QLabel()
        self._label.setObjectName("AddFxHint")
        self._label.setWordWrap(True)
        self._layout.addWidget(self._label)

    def _rebuild(self) -> None:
        info = note_layout.describe_note(self._note, self._fdef)
        self._target.setText(info.part if info else "")
        self._label.setText(self.hint())

    def set_velocity(self, velocity: int) -> None:
        self._spin.blockSignals(True)
        self._spin.setValue(max(0, min(127, int(velocity))) / 10.0)
        self._spin.blockSignals(False)

    def velocity(self) -> int:
        return max(0, min(127, round(self._spin.value() * 10)))

    def hint(self) -> str:
        if self._note in (note_layout.SCENE_PREV, note_layout.SCENE_NEXT):
            way = "back" if self._note == note_layout.SCENE_PREV else "forward"
            return (f"Steps {way} one scene in whichever bank the fixture is "
                    f"on, blending over the time above.")
        index = self._note - note_layout.SCENE_LOAD_RANGE[0]
        return (f"Loads scene {index + 1} of the fixture's CURRENT bank — a "
                f"bank-select verb earlier on the lane decides which. "
                f"Re-loading the scene it is already on blacks it out.")


class PackedSceneEditor(VerbEditor):
    """Note 59: one velocity carries both the bank and the scene."""

    default_velocity = 0
    SCENES_PER_BANK = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        bank_caption = QLabel("bank")
        bank_caption.setObjectName("AddFxFieldLabel")
        row.addWidget(bank_caption)
        self._bank = QComboBox()
        self._bank.currentIndexChanged.connect(lambda _i: self.changed.emit())
        row.addWidget(self._bank, 1)
        scene_caption = QLabel("scene")
        scene_caption.setObjectName("AddFxFieldLabel")
        row.addWidget(scene_caption)
        self._scene = QComboBox()
        self._scene.currentIndexChanged.connect(lambda _i: self.changed.emit())
        row.addWidget(self._scene, 1)
        self._layout.addLayout(row)

        self._label = QLabel()
        self._label.setObjectName("AddFxHint")
        self._label.setWordWrap(True)
        self._layout.addWidget(self._label)

    def _rebuild(self) -> None:
        banks = list(self._fdef.banks) if self._fdef else []
        self._bank.blockSignals(True)
        self._bank.clear()
        for index, name in enumerate(banks[:16]):
            self._bank.addItem(name, index)
        if not banks:
            self._bank.addItem("bank 0", 0)
        self._bank.blockSignals(False)

        self._scene.blockSignals(True)
        self._scene.clear()
        for index in range(self.SCENES_PER_BANK):
            self._scene.addItem(f"scene {index + 1}", index)
        self._scene.blockSignals(False)
        self._label.setText(self.hint())

    def set_velocity(self, velocity: int) -> None:
        value = max(0, min(127, int(velocity)))
        bank, scene = divmod(value, self.SCENES_PER_BANK)
        self._bank.blockSignals(True)
        self._bank.setCurrentIndex(min(bank, self._bank.count() - 1))
        self._bank.blockSignals(False)
        self._scene.blockSignals(True)
        self._scene.setCurrentIndex(min(scene, self._scene.count() - 1))
        self._scene.blockSignals(False)

    def velocity(self) -> int:
        bank = max(0, self._bank.currentIndex())
        scene = max(0, self._scene.currentIndex())
        return min(127, bank * self.SCENES_PER_BANK + scene)

    def hint(self) -> str:
        return ("Loads a bank and a scene in one go — the velocity packs them "
                "as bank×8 + scene, so only the first 8 scenes of a bank are "
                "reachable this way.")


class ChaseEditor(VerbEditor):
    """Note 70: the velocity IS the chase index."""

    default_velocity = 0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._combo = QComboBox()
        self._combo.currentIndexChanged.connect(lambda _i: self.changed.emit())
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._combo, 1)
        self._layout.addLayout(row)
        self._label = QLabel()
        self._label.setObjectName("AddFxHint")
        self._label.setWordWrap(True)
        self._layout.addWidget(self._label)

    def _rebuild(self) -> None:
        chases = list(self._fdef.chases) if self._fdef else []
        self._combo.blockSignals(True)
        self._combo.clear()
        for index, chase in enumerate(chases):
            self._combo.addItem(str(chase.get("name") or f"Chase {index + 1}"),
                                index)
        if not chases:
            self._combo.addItem("no chases on this fixture", 0)
            self._combo.setEnabled(False)
        else:
            self._combo.setEnabled(True)
        self._combo.blockSignals(False)
        self._label.setText(self.hint())

    def set_velocity(self, velocity: int) -> None:
        index = max(0, min(int(velocity), self._combo.count() - 1))
        self._combo.blockSignals(True)
        self._combo.setCurrentIndex(index)
        self._combo.blockSignals(False)

    def velocity(self) -> int:
        return max(0, self._combo.currentIndex())

    def hint(self) -> str:
        return ("Runs the chase until a later verb or song part reprograms "
                "the fixture.")


class ChannelValueEditor(VerbEditor):
    """Notes 1-23: one of the fixture's DMX channels, set to velocity * 2.

    A real console fader rather than a number, coloured by the channel's family
    and captioned with what the capability under the current value actually
    says — so "88" reads as "Gobo 3" or "Strobe 4.2 Hz" while you drag it.
    """

    default_velocity = 0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fader: Optional[ChannelFader] = None
        self._pending = 0
        self._row = QHBoxLayout()
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(14)
        self._row.addStretch(1)
        self._layout.addLayout(self._row)
        self._label = QLabel()
        self._label.setObjectName("AddFxHint")
        self._label.setWordWrap(True)
        self._layout.addWidget(self._label)

    @property
    def channel_index(self) -> int:
        """Which DMX slot this verb drives (0-based)."""
        return self._note - 1

    def _rebuild(self) -> None:
        if self._fader is not None:
            self._fader.setParent(None)
            self._fader = None
        spec = self._spec()
        if spec is None:
            self._label.setText(
                f"Channel {self._note} is past this fixture's channel count.")
            return
        self._fader = ChannelFader(self.channel_index, spec, self._note)
        self._fader.address_label.setText(f"ch {self._note}")
        self._fader.valueChanged.connect(self._on_value)
        self._row.insertWidget(0, self._fader)
        self._fader.set_value(self._pending)
        self._refresh_readout()

    def _spec(self):
        if self._fdef is None or not (
                0 <= self.channel_index < self._fdef.channel_count):
            return None
        return self._fdef.channels[self.channel_index]

    def _on_value(self, _index: int, value: int) -> None:
        self._pending = value
        self._refresh_readout()
        self.changed.emit()

    def value(self) -> int:
        """The channel's DMX value, 0-255."""
        return self._fader.value() if self._fader is not None else self._pending

    def slice_values(self) -> List[int]:
        """The whole fixture at its defaults, with this channel overridden —
        what the preview needs, since a capability can depend on another slot."""
        values = default_values(self._fdef)
        if 0 <= self.channel_index < len(values):
            values[self.channel_index] = self.value()
        return values

    def set_velocity(self, velocity: int) -> None:
        self._pending = min(255, max(0, int(velocity)) * 2)
        if self._fader is not None:
            self._fader.set_value(self._pending)
            self._refresh_readout()

    def velocity(self) -> int:
        return _channel_to_velocity(self.value())

    def _refresh_readout(self) -> None:
        self._label.setText(self.hint())

    def hint(self) -> str:
        spec = self._spec()
        if spec is None or self._fdef is None:
            return ""
        value = self.value()
        # A switching channel's meaning lives on another slot, so this has to be
        # asked with the whole frame or it describes the wrong channel.
        cap = capability_at(self._fdef, self.channel_index, self.slice_values())
        what = ""
        if cap is not None:
            comment = str(cap.get("comment") or "").strip()
            what = comment or cap.type
            for key, kind, unit in (("angle", "angle", "°"),
                                    ("speed", "speed", " Hz"),
                                    ("percent", "percent", " %")):
                amount = ofl.capability_value(cap, value, key, kind)
                if amount is not None:
                    what = f"{what} · {amount:.0f}{unit}"
                    break
        readout = f"{value} of 255" + (f" — {what}" if what else "")
        # The floor exists because a note_on at velocity 0 reads as a note_off.
        if _channel_to_velocity(value) * 2 != value:
            readout += (f" · replays as {_channel_to_velocity(value) * 2} "
                        f"(the velocity is half the value, and 0 is a note-off)")
        return readout


# The picker. Predicates mirror handle_note_on's match arms, first hit wins.
_TABLE: Tuple[Tuple[Callable[[int], bool], type], ...] = (
    (lambda n: note_layout.CHANNEL_OFFSET_RANGE[0] <= n
     <= note_layout.CHANNEL_OFFSET_RANGE[1], ChannelValueEditor),
    (lambda n: note_layout.SCENE_LOAD_RANGE[0] <= n
     <= note_layout.SCENE_LOAD_RANGE[1], SceneLoadEditor),
    (lambda n: n in (note_layout.SCENE_PREV, note_layout.SCENE_NEXT),
     SceneLoadEditor),
    (lambda n: n == note_layout.SCENE_PACKED, PackedSceneEditor),
    (lambda n: n in (note_layout.BLEND_TIME, note_layout.COLOR_CHANGE_TIME,
                     note_layout.CHASE_CHANGE_TIME), TimeEditor),
    (lambda n: n == note_layout.CHASE_LOAD, ChaseEditor),
)


def editor_class_for(note: int) -> type:
    """The VerbEditor subclass that speaks `note`'s units."""
    n = int(note)
    for matches, klass in _TABLE:
        if matches(n):
            return klass
    return NoParamEditor


def default_velocity_for(note: int) -> int:
    """What a clip's velocity should become when it is retyped INTO `note`.

    Never carry the old velocity across verb classes: channel-at-200 is
    velocity 100, and as a chase verb that would silently select chase #100.
    """
    return editor_class_for(note).default_velocity
