# ABOUTME: The fixture browser's Chases tab — edits a type's sidecar chases and
# ABOUTME: runs them through the real stage render engine.
"""Chase editing for one fixture type.

A chase is a loop over that type's own scenes, stored in the `.tdmx.json`
sidecar's `chases` (see `core/fixture_sidecar.py`). MIDI addresses one by INDEX
— note 70's velocity picks the n-th chase — so their order is semantic, exactly
as the scene banks' is, and this editor keeps it visible and draggable.

    CHASES         [+ New] ┃ CHASE NAME [Slow colour walk]           [Save]
    ◦ Slow colour walk  3× ┃ ┌ SIMULATED OUTPUT ────────────── [▶ Run] ┐
    ◦ Beat stabs        4× ┃ │ 01 · Open white       <the real         │
                           ┃ │ 1s per step            renderer>        │
                           ┃ └────────────────────────────────────────┘
                           ┃ ┌ STEPS ──────────────────────── 3 steps ┐
       [Duplicate][Delete] ┃ │ 01 ▪ [Open white ▾]          ▲ ▼ ✕     │

(with CHASE_TIMING_ENABLED on, the header also carries ◀▶⇄ and Sec|Beats, and
each step row a FADE IN, a HOLD and a LEVEL.)

A step names a scene the way the runtime does — by (bank name, index within
that bank) — which is why the picker offers "BANK · Scene" rather than a flat
list, and why a chase survives a bank RENAME (the Scenes tab rewrites the names
it holds) but not a scene reorder.

**A step can also carry its own fade, hold and level**, and the chase its
direction and timing units — but the software controller cannot play any of
that yet, so `CHASE_TIMING_ENABLED` (below) currently builds none of those
controls. What the engine does is one interval for the whole chase, always
forwards, always at full, and with the flag off this tab shows exactly that:
Run steps at the engine's own CHASE TIME and BLEND TIME.

The model behind them stays live either way — `normalise_chase` fills the keys
in, `denormalise_chase` drops them again — so a chase that already carries
timing keeps every bit of it through a save made here, and a chase that never
had any is written back byte for byte. Turn the flag on when the engine reads
it, and the editor is already built.

The list and the Save button work exactly as the Scenes tab's do, and for the
same reason. **Everything that changes the LIST — new, duplicate, delete,
drag — is written to the sidecar as it happens**, so it can never be waiting
behind a button that speaks for something else. What the button carries is the
chase in the editor: its name, direction, units and steps. That is settled —
saved or put back — before anything can move the editor onto another chase,
another fixture, or out of the dialog. None of it is the surrounding dialog's
OK, because this edits the fixture *library*, not the patch that dialog builds.

**SONG PARTS is the one control here the runtime honours in full.**
`note_layout.song_part_refs` indexes those tags off a chase exactly as it does
off a scene, and a fixture in auto mode runs what they name. An untagged chase
can only ever be fired by hand (note 70) — which, on a rig driven by song
parts, is indistinguishable from a chase that does not work.

Unlike the Scenes tab there is no blank draft: a chase with no steps previews
nothing and runs nothing, so "+ New" puts a real one in the list at the click,
with its first step already pointing at a scene.
"""

import time
from typing import Dict, List, Optional, Sequence, Tuple

from PyQt6.QtCore import QSize, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap, QTransform
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSlider, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.fixture_library import FixtureDef, save_fixture_chases
# The two numbers the engine actually steps a chase on, so that with per-step
# timing off the preview shows what the rig does rather than a number made up
# here. Imported rather than copied: a duplicated default drifts silently.
from software_controller.main import DEFAULT_BLEND_TIME, DEFAULT_CHASE_TIME
from core.note_layout import SONG_PARTS
from ui.fixture.console import (
    PartChip, ScenePreview, clamp, scene_swatch, scene_values,
)
from ui.widgets.chips import flow_holder
from ui.widgets import icons
from ui.widgets.segmented import Segment, segmented
from ui.widgets.panes import RatioSplitter
from tupperlogger import get_logger

logger = get_logger("ui.fixture.chases_tab")

# --- the model ----------------------------------------------------------------
#
# A chase is stored as PARALLEL ARRAYS — banks[i]/scenes[i] locate the i-th
# step — because that is the shape the runtime already reads
# (`software_controller.main.update_chase`). The timing arrays added here follow
# it rather than re-shaping the whole thing into a list of step objects, which
# would strand every chase already written.

# Whether this tab offers the per-step timing it can author but the runtime
# cannot yet play: the chase DIRECTION, the seconds/beats unit, and each step's
# fade, hold and level.
#
# `software_controller.main.update_chase` steps a chase on the per-fixture CHASE
# TIME (MIDI note 54) and crossfades on BLEND TIME — one interval for the whole
# chase, always forwards, always at full. Offering controls the rig then ignores
# is worse than not offering them, so they are built only when this is on.
#
# OFF is not "removed": every path behind it is live code, the model still
# round-trips the keys (`normalise_chase` fills them, `denormalise_chase` drops
# them again, so a sidecar is untouched), and `tests/test_fixture_chases.py`
# turns it on to cover the whole feature. Flip it when the engine reads the
# timing, and the UI is already there.
CHASE_TIMING_ENABLED = False

FORWARD, BACKWARD, BOUNCE = "forward", "backward", "bounce"
DIRECTIONS = (FORWARD, BACKWARD, BOUNCE)

SECONDS, BEATS = "seconds", "beats"
UNITS = (SECONDS, BEATS)

# What a chase written before this editor existed reads back as. A zero fade
# and a one-unit hold is a plain step-through, which is what those chases do.
DEFAULT_FADE = 0.0
DEFAULT_HOLD = 1.0
DEFAULT_LEVEL = 100

# The tempo the Run button assumes for a chase timed in beats. This dialog edits
# the fixture LIBRARY and so has no song under it — a beat has no length here
# until a show gives it one.
PREVIEW_BPM = 120.0

# Nothing useful can be drawn from a step with no duration at all, and a chase
# of them would spin the timer without ever advancing.
MIN_STEP_SECONDS = 0.05

# What a step costs when the chase carries no timing of its own: the engine's
# own CHASE TIME and BLEND TIME, in seconds.
ENGINE_STEP_SECONDS = DEFAULT_CHASE_TIME / 1000.0
ENGINE_FADE_SECONDS = DEFAULT_BLEND_TIME / 1000.0


def _numbers(raw, count: int, default: float, whole: bool = False,
             ceiling: Optional[float] = None) -> List[float]:
    """`count` values off `raw`, filling anything missing or unusable."""
    values = raw if isinstance(raw, list) else []
    out: List[float] = []
    for index in range(count):
        try:
            value = max(0.0, float(values[index]))
        except (IndexError, TypeError, ValueError):
            value = default
        if ceiling is not None:
            value = min(ceiling, value)
        out.append(int(round(value)) if whole else round(value, 2))
    return out


def normalise_chase(chase: dict) -> dict:
    """A chase with every field this editor shows spelled out.

    Done on the way IN, so that the "has this been edited?" comparison is
    against what the editor is actually showing — a chase normalised only on
    the way out would read as edited the moment it was looked at, and leaving
    the tab would ask about work nobody did.

    Anything else the dict carries (`song_parts`, keys from a newer version)
    rides through untouched.
    """
    result = dict(chase)
    banks = [str(name) for name in (chase.get("banks") or [])]
    scenes: List[int] = []
    for value in (chase.get("scenes") or []):
        try:
            scenes.append(max(0, int(value)))
        except (TypeError, ValueError):
            scenes.append(0)
    # A step needs both halves; a ragged pair is truncated to the shorter.
    count = min(len(banks), len(scenes))

    name = str(chase.get("name") or "").strip()
    if name:
        result["name"] = name
    else:
        result.pop("name", None)
    result["banks"] = banks[:count]
    result["scenes"] = scenes[:count]
    result["fades"] = _numbers(chase.get("fades"), count, DEFAULT_FADE)
    result["holds"] = _numbers(chase.get("holds"), count, DEFAULT_HOLD)
    result["levels"] = [int(v) for v in _numbers(
        chase.get("levels"), count, DEFAULT_LEVEL, whole=True, ceiling=100)]
    result["direction"] = (chase.get("direction") if chase.get("direction")
                           in DIRECTIONS else FORWARD)
    result["units"] = chase.get("units") if chase.get("units") in UNITS else SECONDS
    return result


def copy_chase(chase: dict) -> dict:
    """A chase copied deeply enough to be compared against and restored from.

    Its steps are parallel LISTS, so `dict(chase)` would hand back a copy still
    sharing them — and a baseline that follows the edits it is there to detect
    is a Save button that never lights up.
    """
    return {key: (list(value) if isinstance(value, list) else value)
            for key, value in chase.items()}


def denormalise_chase(chase: dict) -> dict:
    """A chase with everything still at its default dropped, for writing.

    `normalise_chase` fills these back in on the way in, so the round trip is
    exact — and a sidecar full of chases nobody has timed is left byte for byte
    as it was when one of its neighbours is edited.
    """
    result = dict(chase)
    for key, default in (("fades", DEFAULT_FADE), ("holds", DEFAULT_HOLD),
                         ("levels", DEFAULT_LEVEL)):
        if all(value == default for value in result.get(key) or []):
            result.pop(key, None)
    if result.get("direction") == FORWARD:
        result.pop("direction", None)
    if result.get("units") == SECONDS:
        result.pop("units", None)
    return result


def step_count(chase: dict) -> int:
    return len(chase.get("scenes") or [])


def cycle_length(chase: dict) -> float:
    """One pass through the steps, in the chase's own units."""
    return sum((chase.get("fades") or [])) + sum((chase.get("holds") or []))


def run_order(chase: dict) -> List[int]:
    """The step indices in play order, for one loop of the chase.

    Bounce turns at the ends without repeating them, so a 3-step bounce is
    0 1 2 1 — playing the endpoints twice reads as a stall, not a turn.
    """
    total = step_count(chase)
    if total <= 1 or not CHASE_TIMING_ENABLED:
        # The engine only ever runs a chase forwards.
        return list(range(total))
    direction = chase.get("direction")
    if direction == BACKWARD:
        return list(range(total - 1, -1, -1))
    if direction == BOUNCE:
        return list(range(total)) + list(range(total - 2, 0, -1))
    return list(range(total))


def level_channels(fdef: Optional[FixtureDef]) -> List[int]:
    """The slots a step's LEVEL scales: the fixture's dimmers.

    A fixture with no dimmer channel is dimmed on its colour emitters instead,
    which is the same answer the engine gives when it resolves intensity. Fine
    slots are left alone — scaling a coarse channel is within a 1/255 of right,
    and this is a preview.
    """
    if fdef is None:
        return []
    dimmers = [index for index, spec in enumerate(fdef.channels)
               if spec.fine_of < 0
               and any(cap.type == "Intensity" for cap in spec.capabilities)]
    if dimmers:
        return dimmers
    return [index for index, spec in enumerate(fdef.channels)
            if spec.fine_of < 0
            and any(cap.type == "ColorIntensity" for cap in spec.capabilities)]


def _apply_level(values: List[int], indices: Sequence[int],
                 level: int) -> List[int]:
    out = list(values)
    scale = max(0, min(100, level)) / 100.0
    for index in indices:
        if index < len(out):
            out[index] = clamp(round(out[index] * scale))
    return out


# --- small widgets ------------------------------------------------------------

# Column widths shared by the step header and the step rows, which are separate
# widgets and would otherwise drift apart.
# The list opens at this share of the tab's width, and never narrower than the
# floor — wide enough for a bank name and its song-part tags. Both tabs use the
# same pair: they are the same editor over different data, and a list that was
# one width in Scenes and another in Chases would read as an accident.
_LIST_RATIO = 0.3
_LIST_MIN_WIDTH = 230

_COL_NUM = 24
_COL_SWATCH = 14
_COL_TIME = 74
_COL_SLIDER = 60
_COL_VALUE = 30
_COL_BUTTON = 20
_COL_SPACING = 6
# The picker names a whole "BANK · Scene" and would otherwise ask for the width
# of its longest one, pushing the row's buttons off the card.
_COL_SCENE_MIN = 72


def _glyph_icon(name: str) -> QIcon:
    """One of the icon set's SVGs, or a mirrored one for "<name>-mirrored".

    The set has a play triangle and no left-pointing twin, and the direction
    control wants the pair — a `prev` carries the skip bar of a transport
    button, which is a different verb.
    """
    if name.endswith("-mirrored"):
        pixmap = icons.pixmap(name[:-len("-mirrored")], 11)
        return QIcon(pixmap.transformed(QTransform().scale(-1, 1)))
    return icons.icon(name)


class _TrimmedSpin(QDoubleSpinBox):
    """A duration field that shows 2 and 1.5 rather than 2.00 and 1.50.

    The unit rides beside the box as its own label, so it stays put at the
    right-hand edge instead of trailing the digits around.
    """

    def textFromValue(self, value: float) -> str:
        return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


class _DurationField(QFrame):
    """A step time: the number, and the unit it is counted in."""

    value_changed = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("AddFxStepTime")
        self.setFixedWidth(_COL_TIME)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(2)
        self._spin = _TrimmedSpin()
        self._spin.setObjectName("AddFxStepTimeSpin")
        self._spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self._spin.setDecimals(2)
        self._spin.setRange(0.0, 600.0)
        self._spin.setSingleStep(0.5)
        self._spin.valueChanged.connect(self.value_changed)
        layout.addWidget(self._spin, 1)
        self._unit = QLabel()
        self._unit.setObjectName("AddFxStepUnit")
        layout.addWidget(self._unit)

    def value(self) -> float:
        return round(self._spin.value(), 2)

    def setValue(self, value: float) -> None:
        self._spin.blockSignals(True)
        self._spin.setValue(float(value))
        self._spin.blockSignals(False)

    def set_unit(self, text: str) -> None:
        self._unit.setText(text)


class _StepStrip(QWidget):
    """The band under the preview: one segment per step, sized by its duration.

    It is the only thing that says where in the loop a running chase IS — the
    read-out beside it names the step, but a step that fades for four seconds
    and holds for one looks identical at both ends of the fade.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(26)
        self._weights: List[float] = []
        self._active = -1
        self._progress = 0.0

    def set_steps(self, weights: Sequence[float]) -> None:
        self._weights = [max(MIN_STEP_SECONDS, float(w)) for w in weights]
        self._active = min(self._active, len(self._weights) - 1)
        self.update()

    def set_position(self, active: int, progress: float) -> None:
        self._active = active
        self._progress = max(0.0, min(1.0, progress))
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#151515"))
        if not self._weights:
            return
        total = sum(self._weights)
        x = 0.0
        width = self.width()
        for index, weight in enumerate(self._weights):
            span = width * weight / total
            box = self.rect().adjusted(int(x) + 1, 5, 0, -5)
            box.setWidth(max(1, int(span) - 2))
            active = index == self._active
            painter.setPen(QColor("#4a90e2" if active else "#2f2f2f"))
            painter.setBrush(QColor("#24405a" if active else "#1d1d1d"))
            painter.drawRoundedRect(box, 2, 2)
            if active:
                head = box.left() + box.width() * self._progress
                painter.setPen(QColor("#bfe0fb"))
                painter.drawLine(int(head), box.top(), int(head), box.bottom())
            x += span
        painter.end()


class _StepRow(QWidget):
    """One step: which scene, how it gets there, how long it stays, how bright."""

    changed = pyqtSignal()
    moved = pyqtSignal(int)        # -1 up, +1 down
    removed = pyqtSignal()

    def __init__(self, number: int, parent=None):
        super().__init__(parent)
        self.setObjectName("AddFxStepRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(_COL_SPACING)

        label = QLabel(f"{number:02d}")
        label.setObjectName("AddFxStepNumber")
        label.setFixedWidth(_COL_NUM)
        layout.addWidget(label)

        self._swatch = QLabel()
        self._swatch.setFixedWidth(_COL_SWATCH)
        layout.addWidget(self._swatch)

        self.scene_combo = QComboBox()
        self.scene_combo.setObjectName("AddFxStepScene")
        self.scene_combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Fixed)
        self.scene_combo.setMinimumWidth(_COL_SCENE_MIN)
        self.scene_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.scene_combo.currentIndexChanged.connect(
            lambda _index: self.changed.emit())
        layout.addWidget(self.scene_combo, 1)

        # The per-step timing, when the runtime can play it (CHASE_TIMING_ENABLED).
        self.fade_field: Optional[_DurationField] = None
        self.hold_field: Optional[_DurationField] = None
        self.level_slider: Optional[QSlider] = None
        self.level_label: Optional[QLabel] = None
        if CHASE_TIMING_ENABLED:
            self.fade_field = _DurationField()
            self.fade_field.setToolTip("Time spent crossing into this step")
            self.fade_field.value_changed.connect(lambda _v: self.changed.emit())
            layout.addWidget(self.fade_field)

            self.hold_field = _DurationField()
            self.hold_field.setToolTip(
                "Time held on this step once it has arrived")
            self.hold_field.value_changed.connect(lambda _v: self.changed.emit())
            layout.addWidget(self.hold_field)

            self.level_slider = QSlider(Qt.Orientation.Horizontal)
            self.level_slider.setObjectName("AddFxStepLevel")
            self.level_slider.setRange(0, 100)
            self.level_slider.setFixedWidth(_COL_SLIDER)
            self.level_slider.valueChanged.connect(self._on_level)
            layout.addWidget(self.level_slider)
            self.level_label = QLabel()
            self.level_label.setObjectName("AddFxStepLevelValue")
            self.level_label.setFixedWidth(_COL_VALUE)
            self.level_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
            layout.addWidget(self.level_label)

        for glyph, hint, slot in (
                ("▲", "Move this step up", lambda: self.moved.emit(-1)),
                ("▼", "Move this step down", lambda: self.moved.emit(1)),
                ("✕", "Remove this step", self.removed.emit)):
            button = QToolButton()
            button.setObjectName("AddFxStepButton")
            button.setText(glyph)
            button.setToolTip(hint)
            button.setFixedWidth(_COL_BUTTON)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(slot)
            layout.addWidget(button)

    def _on_level(self, value: int) -> None:
        self.level_label.setText(str(value))
        self.changed.emit()

    def set_scenes(self, choices: Sequence[Tuple[str, str, int]],
                   selected: Tuple[str, int]) -> None:
        """Fill the picker with (label, bank, index) and point it at `selected`.

        A step naming a scene that is no longer there keeps an entry of its own
        rather than snapping onto somebody else's look — a chase pointing at a
        deleted scene is something to see, not something to silently repair.
        """
        self.scene_combo.blockSignals(True)
        self.scene_combo.clear()
        position = -1
        for label, bank, index in choices:
            if (bank, index) == selected:
                # Scanned rather than `findData`d: the data is a Python tuple,
                # and Qt compares it as an opaque QVariant that never matches.
                position = self.scene_combo.count()
            self.scene_combo.addItem(label, (bank, index))
        if position < 0:
            bank, index = selected
            self.scene_combo.addItem(f"{bank} · #{index + 1} (missing)", selected)
            position = self.scene_combo.count() - 1
        self.scene_combo.setCurrentIndex(position)
        self.scene_combo.setToolTip(self.scene_combo.currentText())
        self.scene_combo.blockSignals(False)

    def set_values(self, fade: float, hold: float, level: int, unit: str,
                   swatch: Optional[QPixmap]) -> None:
        if swatch is not None:
            self._swatch.setPixmap(swatch)
        if not CHASE_TIMING_ENABLED:
            return
        self.fade_field.setValue(fade)
        self.hold_field.setValue(hold)
        self.fade_field.set_unit(unit)
        self.hold_field.set_unit(unit)
        self.level_slider.blockSignals(True)
        self.level_slider.setValue(int(level))
        self.level_slider.blockSignals(False)
        self.level_label.setText(str(int(level)))

    def scene_ref(self) -> Tuple[str, int]:
        return self.scene_combo.currentData() or ("", 0)


# --- the chase list -----------------------------------------------------------

_ROLE_INDEX = Qt.ItemDataRole.UserRole


class _ChaseTree(QTreeWidget):
    """The chases, reorderable by drag.

    Flat, unlike the Scenes tab's tree: a chase belongs to no bank. The drop is
    reported as a (from, to) pair and the items are never moved by Qt — the tab
    owns the order, and relaying the list from the data afterwards is the only
    way the two can't drift.
    """

    move_requested = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("AddFxScenes")     # the Scenes tab's list styling
        self.setColumnCount(2)
        self.setHeaderHidden(True)
        self.setIndentation(0)
        self.setRootIsDecorated(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(1, 34)

    def dropEvent(self, event):
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        dragged = self.currentItem()
        if dragged is None:
            return
        source = self.indexOfTopLevelItem(dragged)
        target = self.itemAt(event.position().toPoint())
        if target is None:
            self.move_requested.emit(source, self.topLevelItemCount())
            return
        position = self.indexOfTopLevelItem(target)
        above = (self.dropIndicatorPosition()
                 == QAbstractItemView.DropIndicatorPosition.AboveItem)
        self.move_requested.emit(source, position if above else position + 1)


def _ring(filled: bool) -> QIcon:
    """The dot beside a chase name — filled while that chase is running."""
    pixmap = QPixmap(10, 10)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor("#4a90e2" if filled else "#5a5a5a"))
    painter.setBrush(QColor("#4a90e2") if filled else Qt.GlobalColor.transparent)
    painter.drawEllipse(1, 1, 7, 7)
    painter.end()
    return QIcon(pixmap)


# --- the tab ------------------------------------------------------------------

class FixtureChasesTab(QWidget):
    """List, edit and run the chases of one fixture type."""

    # A write landed on the type's sidecar — whoever reads the library (the
    # clip library panel, the Scenes tab's copy of the chases) is now stale.
    library_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._type_name = ""
        self._fdef: Optional[FixtureDef] = None
        # Owned outright: normalised on the way in, mutated in place, and
        # nothing reaches the sidecar until it is written for.
        self._chases: List[dict] = []
        self._banks: Dict[str, List[dict]] = {}
        self._selected: Optional[int] = None
        # The selected chase as the sidecar last got it. Comparing against it is
        # what "this chase has unsaved changes" means — a flag would have to be
        # cleared by every path that puts one back, and one that forgot would
        # leave the button lying.
        self._baseline: Optional[dict] = None
        self._rows: List[_StepRow] = []
        self._part_chips: Dict[str, PartChip] = {}
        # Which step the preview shows while nothing is running: whichever one
        # was last touched, so editing step 3 shows step 3.
        self._focus = 0

        self._run_order: List[int] = []
        self._run_position = 0
        self._run_elapsed = 0.0
        self._run_clock = 0.0
        self._run_from: List[int] = []
        self._run_timer = QTimer(self)
        self._run_timer.setInterval(33)
        self._run_timer.timeout.connect(self._on_run_tick)

        # A splitter rather than a fixed rail: how much room the list wants
        # depends on how long the bank and scene names are, which is the
        # fixture author's business rather than ours. It opens at a share of
        # the width — so a wide dialog gives a wide list — and the divider is
        # there for when that share is wrong.
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(0)
        self.splitter = RatioSplitter(self._build_list_panel(),
                                      self._build_editor_panel(), _LIST_RATIO)
        layout.addWidget(self.splitter)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_list_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("AddFxScenesPanel")
        # A floor, not a width: the splitter decides the rest.
        panel.setMinimumWidth(_LIST_MIN_WIDTH)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(6)
        title = QLabel("CHASES")
        title.setObjectName("AddFxFieldLabel")
        header.addWidget(title)
        header.addStretch(1)
        self.new_button = QToolButton()
        self.new_button.setObjectName("AddFxNewScene")
        self.new_button.setText("+ New")
        self.new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_button.clicked.connect(self._new_chase)
        header.addWidget(self.new_button)
        layout.addLayout(header)

        self.tree = _ChaseTree()
        self.tree.currentItemChanged.connect(self._on_tree_selection)
        self.tree.itemChanged.connect(self._on_item_renamed)
        self.tree.move_requested.connect(self._on_move)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.setObjectName("AddFxSceneAction")
        self.duplicate_button.clicked.connect(self._duplicate)
        buttons.addWidget(self.duplicate_button)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("AddFxSceneAction")
        self.delete_button.clicked.connect(self._delete)
        buttons.addWidget(self.delete_button)
        layout.addLayout(buttons)

        self.status_label = QLabel()
        self.status_label.setObjectName("AddFxHint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        return panel

    def _build_editor_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self._build_header_card())
        layout.addWidget(self._build_preview_card())
        layout.addWidget(self._build_steps_card(), 1)
        return panel

    def _build_header_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("AddFxChaseHeader")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(12, 8, 12, 10)
        outer.setSpacing(8)
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        outer.addLayout(layout)
        outer.addLayout(self._build_song_parts_row())

        # Caption beside the field, not above it, and the same 96px column the
        # SONG PARTS caption below uses — so the two captions line up and the
        # card reads as one two-column form, exactly as the Scenes tab's does.
        caption = QLabel("CHASE NAME")
        caption.setObjectName("AddFxFieldLabel")
        caption.setFixedWidth(96)
        layout.addWidget(caption)
        self.name_input = QLineEdit()
        self.name_input.setObjectName("AddFxSceneName")
        self.name_input.setPlaceholderText("Unnamed chase")
        self.name_input.setMinimumWidth(100)
        # Per keystroke, not on `editingFinished`: the Save button reports what
        # the editor holds, and waiting for the field to lose focus would leave
        # it dark over a name that has already been changed. `textEdited` is the
        # user's typing only — `setText` from `_refresh_editor` is not an edit.
        self.name_input.textEdited.connect(lambda _text: self._on_name_edited())
        layout.addWidget(self.name_input, 1)

        self._direction_buttons: Dict[str, QPushButton] = {}
        self._units_buttons: Dict[str, QPushButton] = {}
        if not CHASE_TIMING_ENABLED:
            # No direction, no units: the engine runs every chase forwards on
            # one interval, so there is nothing here to choose. Nothing tall is
            # left in the row either, so Save goes straight into it.
            layout.addWidget(self._build_save_button())
            return card

        direction_column = QVBoxLayout()
        direction_column.setSpacing(4)
        direction_caption = QLabel("DIRECTION")
        direction_caption.setObjectName("AddFxFieldLabel")
        direction_column.addWidget(direction_caption)
        # Caption at the top, control at the bottom — level with the name
        # field and the Save button, which are taller than a switch.
        direction_column.addStretch(1)
        frame, self._direction_buttons = segmented(
            [Segment(FORWARD, tooltip="Play the steps in order, then start "
                                      "again", icon=_glyph_icon("play")),
             Segment(BACKWARD, tooltip="Play the steps last to first",
                     icon=_glyph_icon("play-mirrored")),
             Segment(BOUNCE, tooltip="Run to the end and back, without "
                                     "repeating the ends",
                     icon=_glyph_icon("swap"))],
            on_pick=self._on_direction_picked, checked=FORWARD, parent=self)
        direction_column.addWidget(frame)
        layout.addLayout(direction_column)

        units_column = QVBoxLayout()
        units_column.setSpacing(4)
        units_caption = QLabel("TIMING UNITS")
        units_caption.setObjectName("AddFxFieldLabel")
        units_column.addWidget(units_caption)
        # Caption at the top, control at the bottom — level with the name
        # field and the Save button, which are taller than a switch.
        units_column.addStretch(1)
        frame, self._units_buttons = segmented(
            [Segment(SECONDS, "Seconds"),
             Segment(BEATS, "Beats",
                     f"Step times counted in beats — Run assumes "
                     f"{PREVIEW_BPM:.0f} BPM, since this dialog has no song")],
            on_pick=self._on_units_picked, checked=SECONDS, parent=self)
        units_column.addWidget(frame)
        layout.addLayout(units_column)

        layout.addLayout(self._build_save_column())
        return card

    def _build_song_parts_row(self) -> QHBoxLayout:
        """The tags auto mode picks a chase by.

        The one thing on this tab the runtime already honours in full:
        `note_layout.song_part_refs` indexes these off a chase exactly as it
        does off a scene, and a fixture in auto mode runs what they name. A
        chase with none can only ever be fired by hand (note 70), which is
        indistinguishable from a chase that does not work.
        """
        row = QHBoxLayout()
        row.setSpacing(12)
        caption = QLabel("SONG PARTS")
        caption.setObjectName("AddFxFieldLabel")
        caption.setFixedWidth(96)
        row.addWidget(caption, 0, Qt.AlignmentFlag.AlignTop)
        # The nine tags fit one row at the dialog's own width; the flow layout
        # is there for when they don't — squeezed into a QHBoxLayout they would
        # overlap instead.
        holder = flow_holder(spacing=4)
        for part in SONG_PARTS:
            chip = PartChip(part)
            chip.setToolTip(f"Run this chase automatically during {part}")
            chip.clicked.connect(self._on_song_parts_changed)
            self._part_chips[part] = chip
            holder.layout().addWidget(chip)
        row.addWidget(holder, 1)
        return row

    def _build_save_button(self) -> QPushButton:
        self.save_button = QPushButton("✓ Save")
        self.save_button.setObjectName("AddFxSceneSave")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.setToolTip(
            "Write this chase to the fixture's .tdmx.json sidecar")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save)
        return self.save_button

    def _build_save_column(self) -> QVBoxLayout:
        """Save hung from the bottom of a column, for the timing layout: the
        direction and units controls carry a caption above them, so a button
        dropped straight into that row would float halfway up it."""
        save_column = QVBoxLayout()
        save_column.setSpacing(4)
        save_column.addSpacing(1)
        save_column.addStretch(1)
        save_column.addWidget(self._build_save_button())
        return save_column

    def _build_preview_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("AddFxOutputCard")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(14, 10, 8, 0)
        caption = QLabel("SIMULATED OUTPUT")
        caption.setObjectName("AddFxHint")
        header.addWidget(caption)
        header.addStretch(1)
        self.run_button = QPushButton("Run")
        self.run_button.setIcon(icons.icon("play", "#ffffff"))
        self.run_button.setIconSize(QSize(11, 11))
        self.run_button.setObjectName("AddFxChaseRun")
        self.run_button.setCheckable(True)
        self.run_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_button.setToolTip("Play this chase through the preview")
        self.run_button.clicked.connect(self._on_run_clicked)
        header.addWidget(self.run_button)
        outer.addLayout(header)

        body = QHBoxLayout()
        body.setContentsMargins(14, 2, 8, 4)
        body.setSpacing(12)
        readout = QVBoxLayout()
        readout.setSpacing(2)
        self.step_label = QLabel()
        self.step_label.setObjectName("AddFxChaseStep")
        readout.addWidget(self.step_label)
        self.step_detail = QLabel()
        self.step_detail.setObjectName("AddFxSceneSummary")
        self.step_detail.setWordWrap(True)
        readout.addWidget(self.step_detail)
        readout.addStretch(1)
        holder = QWidget()
        holder.setLayout(readout)
        holder.setFixedWidth(200)
        body.addWidget(holder)
        self._preview = ScenePreview()
        self._preview.setMinimumHeight(104)
        self._preview.setMaximumHeight(150)
        self._preview.setSizePolicy(QSizePolicy.Policy.Expanding,
                                    QSizePolicy.Policy.Expanding)
        body.addWidget(self._preview, 1)
        outer.addLayout(body, 1)

        self.strip = _StepStrip()
        outer.addWidget(self.strip)
        return card

    def _build_steps_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("AddFxStepsCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        caption = QLabel("STEPS")
        caption.setObjectName("AddFxFieldLabel")
        header.addWidget(caption)
        header.addStretch(1)
        self.steps_summary = QLabel()
        self.steps_summary.setObjectName("AddFxHint")
        header.addWidget(self.steps_summary)
        layout.addLayout(header)
        layout.addWidget(self._build_steps_header())

        scroll = QScrollArea()
        scroll.setObjectName("AddFxStepScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self._steps_layout = QVBoxLayout(body)
        self._steps_layout.setContentsMargins(0, 0, 0, 0)
        self._steps_layout.setSpacing(2)
        self._steps_layout.addStretch(1)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        self.add_step_button = QToolButton()
        self.add_step_button.setObjectName("AddFxAddStep")
        self.add_step_button.setText("New step")
        self.add_step_button.setIcon(icons.icon("plus"))
        self.add_step_button.setIconSize(QSize(10, 10))
        self.add_step_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.add_step_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_step_button.clicked.connect(self._add_step)
        layout.addWidget(self.add_step_button, 0, Qt.AlignmentFlag.AlignLeft)
        return card

    def _build_steps_header(self) -> QWidget:
        row = QWidget()
        row.setObjectName("AddFxStepHeader")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(_COL_SPACING)
        columns = [("#", _COL_NUM, 0), ("SCENE", 0, 1)]
        if CHASE_TIMING_ENABLED:
            columns += [("FADE IN", _COL_TIME, 0), ("HOLD", _COL_TIME, 0),
                        ("LEVEL", _COL_SLIDER + _COL_VALUE + _COL_SPACING, 0)]
        for text, width, stretch in columns:
            label = QLabel(text)
            label.setObjectName("AddFxFieldLabel")
            if width:
                label.setFixedWidth(width)
            if text == "SCENE":
                # The rows carry a colour chip ahead of the picker; the caption
                # sits over the picker, not over the chip.
                layout.addSpacing(_COL_SWATCH)
            layout.addWidget(label, stretch)
        # The three per-row buttons, which have no caption of their own.
        layout.addSpacing(3 * _COL_BUTTON + 3 * _COL_SPACING)
        return row

    @property
    def preview(self) -> ScenePreview:
        """The stage preview. Kept as a direct handle because
        `AddFixtureDialog` calls `chases_tab.preview.clear()` on close — the
        bodies own animation timers on the shared 30 Hz clock."""
        return self._preview

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------
    def set_fixture(self, type_name: str, fdef: Optional[FixtureDef]) -> None:
        """Show `fdef`'s chases. Called whenever the browser's type or mode
        changes — the banks a step names, and so every step, depend on the
        mode."""
        # Unsaved edits belong to the OUTGOING fixture; settle them before
        # `self._chases` becomes someone else's.
        self.save_pending_changes()
        self._stop_run()
        self._type_name = type_name
        self._fdef = fdef
        self._selected = None
        self._baseline = None
        self._focus = 0
        self._load(fdef)
        self._preview.set_fixture(fdef)
        self._rebuild_tree()
        self._refresh_editor()

    def select_chase(self, index: int) -> bool:
        """Put the cursor on one chase by its index — which is what a clip
        carries, because note 70 loads a chase by index and nothing else.

        Not signal-blocked: this IS the cursor moving, so the editor follows
        it, and (as with any selection here) the chase starts running.
        """
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            if item.data(0, _ROLE_INDEX) == index:
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                return True
        return False

    def reload(self, fdef: Optional[FixtureDef]) -> None:
        """Re-read everything from `fdef` — the Scenes tab has just written the
        same sidecar.

        A bank rename rewrites the chases naming it, so this copy can be stale
        in the one way that matters. Pending edits are settled first, exactly
        as they are when the fixture changes: they belong to the chase as it
        was, and this is the last moment that chase still exists.
        """
        if fdef is None or self._fdef is None:
            return
        self.save_pending_changes()
        selected = self._selected
        self._load(fdef)
        self._fdef = fdef
        self._selected = selected if selected is not None \
            and selected < len(self._chases) else None
        self._baseline = (copy_chase(self._chases[self._selected])
                          if self._selected is not None else None)
        self._rebuild_tree()
        self._refresh_editor()

    def _load(self, fdef: Optional[FixtureDef]) -> None:
        self._chases = [normalise_chase(chase)
                        for chase in (fdef.chases if fdef else [])]
        self._banks = {name: list(scenes)
                       for name, scenes in (fdef.banks if fdef else {}).items()}

    def _rebuild_tree(self) -> None:
        """Relay the list from `self._chases`, keeping the selection."""
        wanted = self._selected
        self.tree.blockSignals(True)
        self.tree.clear()
        restored = None
        for index, chase in enumerate(self._chases):
            item = QTreeWidgetItem(self.tree, [self._chase_label(chase, index),
                                               f"{step_count(chase)}×"])
            item.setData(0, _ROLE_INDEX, index)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            item.setIcon(0, _ring(False))
            item.setToolTip(0, self._chase_tooltip(chase))
            item.setForeground(1, QColor("#7a7a7a"))
            item.setTextAlignment(1, int(Qt.AlignmentFlag.AlignRight
                                         | Qt.AlignmentFlag.AlignVCenter))
            if wanted == index:
                restored = item
        if restored is not None:
            # Still inside the block: putting the cursor back is not the user
            # moving it, and `_on_tree_selection` asks about unsaved work.
            self.tree.setCurrentItem(restored)
        self.tree.blockSignals(False)

    @staticmethod
    def _chase_label(chase: dict, index: int) -> str:
        return str(chase.get("name") or f"Chase {index + 1}")

    @staticmethod
    def _chase_tooltip(chase: dict) -> str:
        """Which song parts run this chase — the only thing that decides
        whether auto mode can reach it at all."""
        parts = chase.get("song_parts")
        if isinstance(parts, list) and parts:
            return "Runs automatically during " + ", ".join(str(p) for p in parts)
        return "No song parts — only note 70 can fire this chase"

    def _relabel(self, index: int) -> None:
        """Re-stamp one row from its chase — a targeted alternative to
        `_rebuild_tree` for the paths that keep the list in step with the
        editor. The discard in `_settle_chase_edits` needs it rather than wants
        it: that one runs inside `currentItemChanged`, and relaying the list
        there would delete the item under the signal."""
        item = self.tree.topLevelItem(index)
        if item is None or not 0 <= index < len(self._chases):
            return
        chase = self._chases[index]
        self.tree.blockSignals(True)          # setText is not a rename
        item.setText(0, self._chase_label(chase, index))
        item.setText(1, f"{step_count(chase)}×")
        item.setToolTip(0, self._chase_tooltip(chase))
        self.tree.blockSignals(False)

    # ------------------------------------------------------------------
    # Selection and the editor's state
    # ------------------------------------------------------------------
    def _on_tree_selection(self, current, _previous) -> None:
        # Whatever is pending belongs to the chase being LEFT, which is still
        # `self._selected` here — settle it before that changes hands.
        self._settle_chase_edits()
        self._stop_run()
        self._selected = current.data(0, _ROLE_INDEX) if current else None
        chase = self._current_chase()
        self._baseline = copy_chase(chase) if chase is not None else None
        self._focus = 0
        self._refresh_editor()
        # Picking a chase runs it. A chase IS its motion — a still frame of one
        # step says almost nothing about it — so the question "what does this
        # one look like?" is answered by playing it, and Run becomes Stop for
        # the times you would rather it held still.
        # `run_button.isEnabled()` rather than a second copy of its rule:
        # _refresh_editor has just decided whether this chase can run at all.
        if self.run_button.isEnabled():
            self._start_run()

    def _current_chase(self) -> Optional[dict]:
        if self._selected is None:
            return None
        if not 0 <= self._selected < len(self._chases):
            return None
        return self._chases[self._selected]

    def _dirty(self) -> bool:
        """Whether the selected chase has edits the sidecar hasn't got."""
        if self._selected is None or self._baseline is None:
            return False
        return self._current_chase() != self._baseline

    def _settle_chase_edits(self) -> None:
        """Ask what to do with the edits on the selected chase, then do it.

        Called before anything that moves the editor onto something else, and
        before anything that writes for its own reasons — a chase's edits are
        never carried silently onto another chase, nor swept into a save the
        user asked for about something else.
        """
        if not self._dirty():
            return
        index = self._selected
        chase = self._current_chase()
        keep = QMessageBox.question(
            self, "Unsaved chase",
            f"Save the changes to '{self._chase_label(chase, index)}'?")
        if keep == QMessageBox.StandardButton.Yes:
            self._write()
            return
        # Put it back: the dict is the one the list holds, so it is restored in
        # place, and the row that has been following the edits is re-stamped.
        chase.clear()
        chase.update(copy_chase(self._baseline))
        self._relabel(index)

    def _refresh_editor(self) -> None:
        """Push the selected chase into the name, the segments, the steps and
        the preview."""
        chase = self._current_chase()
        editable = chase is not None
        self.name_input.setEnabled(editable)
        self.add_step_button.setEnabled(editable)
        for chip in self._part_chips.values():
            chip.setEnabled(editable)
        self.run_button.setEnabled(editable and step_count(chase) > 0)
        for button in (*self._direction_buttons.values(),
                       *self._units_buttons.values()):
            button.setEnabled(editable)
        self.duplicate_button.setEnabled(editable)
        self.delete_button.setEnabled(editable)

        self.name_input.blockSignals(True)
        self.name_input.setText(
            self._chase_label(chase, self._selected) if chase else "")
        self.name_input.setCursorPosition(0)
        self.name_input.blockSignals(False)

        parts = chase.get("song_parts") if chase else None
        parts = set(parts) if isinstance(parts, list) else set()
        for name, chip in self._part_chips.items():
            chip.setChecked(name in parts)

        direction = chase.get("direction") if chase else FORWARD
        for value, button in self._direction_buttons.items():
            button.setChecked(value == direction)
        units = chase.get("units") if chase else SECONDS
        for value, button in self._units_buttons.items():
            button.setChecked(value == units)

        self._rebuild_steps()
        self._refresh_save_button()
        self._refresh_status()
        self._refresh_preview()

    def _scene_choices(self) -> List[Tuple[str, str, int]]:
        """Every scene this type has, as (label, bank, index).

        Labelled by BANK first because that is how the runtime names one — a
        step stores (bank, index within that bank), not a flat position.
        """
        choices: List[Tuple[str, str, int]] = []
        for bank, scenes in self._banks.items():
            for index, scene in enumerate(scenes):
                name = str(scene.get("name") or f"#{index + 1}")
                choices.append((f"{bank} · {name}", bank, index))
        return choices

    def _scene_at(self, chase: dict, step: int) -> Optional[dict]:
        """The scene dict a step names, or None when it names nothing."""
        banks = chase.get("banks") or []
        scenes = chase.get("scenes") or []
        if not 0 <= step < min(len(banks), len(scenes)):
            return None
        bank = self._banks.get(banks[step]) or []
        index = scenes[step]
        return bank[index] if 0 <= index < len(bank) else None

    def _step_name(self, chase: dict, step: int) -> str:
        scene = self._scene_at(chase, step)
        if scene is None:
            return "— missing —"
        return str(scene.get("name") or f"#{(chase['scenes'][step]) + 1}")

    def _unit_glyph(self, chase: Optional[dict]) -> str:
        if CHASE_TIMING_ENABLED and chase and chase.get("units") == BEATS:
            return "b"
        return "s"

    def _rebuild_steps(self) -> None:
        """Relay the step rows from the selected chase."""
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows = []
        chase = self._current_chase()
        if chase is None:
            return
        choices = self._scene_choices()
        unit = self._unit_glyph(chase)
        for step in range(step_count(chase)):
            row = _StepRow(step + 1)
            row.set_scenes(choices,
                           (chase["banks"][step], chase["scenes"][step]))
            row.set_values(chase["fades"][step], chase["holds"][step],
                           chase["levels"][step], unit,
                           scene_swatch(self._fdef, self._scene_at(chase, step)))
            row.changed.connect(lambda s=step: self._on_step_changed(s))
            row.moved.connect(lambda delta, s=step: self._move_step(s, delta))
            row.removed.connect(lambda s=step: self._remove_step(s))
            # Before the trailing stretch, so the rows stay at the top.
            self._steps_layout.insertWidget(len(self._rows), row)
            self._rows.append(row)

    def _refresh_save_button(self) -> None:
        self.save_button.setEnabled(self._dirty())

    def _refresh_status(self) -> None:
        """The line under the list, and the summary over the steps."""
        chase = self._current_chase()
        if chase is not None:
            total = step_count(chase)
            plural = "" if total == 1 else "s"
            if not total:
                self.steps_summary.setText("no steps yet")
            elif CHASE_TIMING_ENABLED:
                self.steps_summary.setText(
                    f"{total} step{plural} · {cycle_length(chase):g}"
                    f"{self._unit_glyph(chase)} cycle")
            else:
                self.steps_summary.setText(f"{total} step{plural}")
        else:
            self.steps_summary.setText("")

        if self._fdef is None:
            self.status_label.setText("")
            return
        if not self._banks:
            self.status_label.setText(
                "This fixture has no scenes yet — a chase steps through them, "
                "so build some on the Scenes tab first.")
            return
        if self._dirty():
            self.status_label.setText(
                f"Unsaved changes to "
                f"'{self._chase_label(chase, self._selected)}' — press Save to "
                f"write them to the fixture's .tdmx.json.")
            return
        if not self._chases:
            self.status_label.setText(
                "No chases yet — press + New, saved to the fixture's "
                ".tdmx.json sidecar.")
            return
        self.status_label.setText(
            f"{len(self._chases)} chase(s) · MIDI note 70 loads one by index · "
            f"saved to the fixture's .tdmx.json")

    # ------------------------------------------------------------------
    # Edits to the chase in the editor — all of these ride the Save button
    # ------------------------------------------------------------------
    def _on_name_edited(self) -> None:
        chase = self._current_chase()
        if chase is None:
            return
        name = self.name_input.text().strip()
        if name:
            chase["name"] = name
        else:
            chase.pop("name", None)
        self._relabel(self._selected)
        self._refresh_save_button()
        self._refresh_status()

    def _on_song_parts_changed(self) -> None:
        """Tags are chase content, so they ride the Save button like the rest."""
        chase = self._current_chase()
        if chase is None:
            return
        parts = [name for name in SONG_PARTS if self._part_chips[name].isChecked()]
        if parts:
            chase["song_parts"] = parts
        else:
            chase.pop("song_parts", None)
        self._relabel(self._selected)
        self._refresh_save_button()
        self._refresh_status()

    def _on_direction_picked(self, direction: str) -> None:
        chase = self._current_chase()
        if chase is None or chase.get("direction") == direction:
            return
        chase["direction"] = direction
        self._stop_run()
        self._refresh_save_button()
        self._refresh_status()

    def _on_units_picked(self, units: str) -> None:
        """Switch the unit the step times are counted in.

        The NUMBERS are left exactly as they are: 2 seconds becoming 2 beats is
        a re-reading of the same authored value, not a conversion — converting
        would silently rewrite every step against a tempo this dialog doesn't
        have.
        """
        chase = self._current_chase()
        if chase is None or chase.get("units") == units:
            return
        chase["units"] = units
        self._stop_run()
        glyph = self._unit_glyph(chase)
        for row in self._rows:
            if row.fade_field is not None:
                row.fade_field.set_unit(glyph)
                row.hold_field.set_unit(glyph)
        self._refresh_save_button()
        self._refresh_status()

    def _on_step_changed(self, step: int) -> None:
        chase = self._current_chase()
        if chase is None or not 0 <= step < len(self._rows):
            return
        row = self._rows[step]
        bank, index = row.scene_ref()
        chase["banks"][step] = bank
        chase["scenes"][step] = index
        if CHASE_TIMING_ENABLED:
            chase["fades"][step] = row.fade_field.value()
            chase["holds"][step] = row.hold_field.value()
            chase["levels"][step] = row.level_slider.value()
        row.set_values(chase["fades"][step], chase["holds"][step],
                       chase["levels"][step], self._unit_glyph(chase),
                       scene_swatch(self._fdef, self._scene_at(chase, step)))
        self._focus = step
        self._refresh_save_button()
        self._refresh_status()
        if not self._run_timer.isActive():
            self._refresh_preview()
        else:
            self.strip.set_steps(self._step_seconds(chase))

    def _add_step(self) -> None:
        """Append a step, copying the last one so a chase builds by tweaking.

        The first step of an empty chase takes the fixture's first scene, since
        a step that names nothing can neither be previewed nor run.
        """
        chase = self._current_chase()
        if chase is None:
            return
        total = step_count(chase)
        if total:
            source = total - 1
            chase["banks"].append(chase["banks"][source])
            chase["scenes"].append(chase["scenes"][source])
            chase["fades"].append(chase["fades"][source])
            chase["holds"].append(chase["holds"][source])
            chase["levels"].append(chase["levels"][source])
        else:
            choices = self._scene_choices()
            if not choices:
                QMessageBox.information(
                    self, "New step",
                    "This fixture has no scenes to step through yet — build "
                    "some on the Scenes tab first.")
                return
            _label, bank, index = choices[0]
            chase["banks"].append(bank)
            chase["scenes"].append(index)
            chase["fades"].append(DEFAULT_FADE)
            chase["holds"].append(DEFAULT_HOLD)
            chase["levels"].append(DEFAULT_LEVEL)
        self._focus = step_count(chase) - 1
        self._stop_run()
        self._relabel(self._selected)
        self._rebuild_steps()
        self._refresh_save_button()
        self._refresh_status()
        self._refresh_preview()

    def _move_step(self, step: int, delta: int) -> None:
        chase = self._current_chase()
        if chase is None:
            return
        target = step + delta
        if not (0 <= step < step_count(chase) and 0 <= target < step_count(chase)):
            return
        for key in ("banks", "scenes", "fades", "holds", "levels"):
            chase[key][step], chase[key][target] = \
                chase[key][target], chase[key][step]
        self._focus = target
        self._stop_run()
        self._rebuild_steps()
        self._refresh_save_button()
        self._refresh_status()
        self._refresh_preview()

    def _remove_step(self, step: int) -> None:
        chase = self._current_chase()
        if chase is None or not 0 <= step < step_count(chase):
            return
        for key in ("banks", "scenes", "fades", "holds", "levels"):
            del chase[key][step]
        self._focus = max(0, min(self._focus, step_count(chase) - 1))
        self._stop_run()
        self._relabel(self._selected)
        self._rebuild_steps()
        self._refresh_editor()

    # ------------------------------------------------------------------
    # The list itself
    #
    # Adding, copying, deleting, reordering and renaming all change what MIDI
    # addresses (note 70's velocity is the chase index), show in the list the
    # moment they are done, and are not what the Save button is about — so each
    # one writes as it happens, after settling whatever the editor is holding.
    # ------------------------------------------------------------------
    def _new_chase(self) -> None:
        if self._fdef is None:
            return
        choices = self._scene_choices()
        if not choices:
            QMessageBox.information(
                self, "New chase",
                "This fixture has no scenes to step through yet — build some "
                "on the Scenes tab first.")
            return
        self._settle_chase_edits()
        self._stop_run()
        _label, bank, index = choices[0]
        self._chases.append(normalise_chase({
            "name": f"Chase {len(self._chases) + 1}",
            "banks": [bank], "scenes": [index],
        }))
        self._selected = len(self._chases) - 1
        self._write()
        self._rebuild_tree()
        self._refresh_editor()
        self.name_input.setFocus(Qt.FocusReason.OtherFocusReason)
        self.name_input.selectAll()

    def _duplicate(self) -> None:
        if self._selected is None:
            return
        self._settle_chase_edits()
        self._stop_run()
        index = self._selected
        copy = copy_chase(self._chases[index])
        copy["name"] = f"{self._chase_label(copy, index)} copy"
        self._chases.insert(index + 1, copy)
        self._selected = index + 1
        self._write()
        self._rebuild_tree()
        self._refresh_editor()

    def _delete(self) -> None:
        if self._selected is None:
            return
        index = self._selected
        chase = self._chases[index]
        if QMessageBox.question(
                self, "Delete chase",
                f"Delete '{self._chase_label(chase, index)}'?\n\nChases are "
                f"addressed by index, so this shifts every one after it.") != \
                QMessageBox.StandardButton.Yes:
            return
        self._stop_run()
        # No settling: whatever was pending on it goes with it.
        del self._chases[index]
        self._selected = min(index, len(self._chases) - 1) if self._chases else None
        self._baseline = (copy_chase(self._chases[self._selected])
                          if self._selected is not None else None)
        self._write()
        self._rebuild_tree()
        self._refresh_editor()

    def _on_move(self, source: int, position: int) -> None:
        """Apply a drag. Note 70's velocity IS the chase index, so this re-binds
        notes — the same as adding or deleting one does."""
        if not 0 <= source < len(self._chases):
            return
        self._settle_chase_edits()
        self._stop_run()
        chase = self._chases.pop(source)
        # `position` counted the dragged chase itself, which the list no longer
        # holds.
        if source < position:
            position -= 1
        position = max(0, min(len(self._chases), position))
        self._chases.insert(position, chase)
        self._selected = position
        self._write()
        self._rebuild_tree()
        self._refresh_editor()

    def _on_item_renamed(self, item: QTreeWidgetItem, column: int) -> None:
        """An inline edit in the list renames a chase."""
        if column != 0 or self._fdef is None:
            return
        index = item.data(0, _ROLE_INDEX)
        if index is None or not 0 <= index < len(self._chases):
            return
        # Editing the row IS editing the chase in the editor — Qt made it
        # current to start the edit — so this is a chase edit like any other,
        # and the Save button picks it up.
        self._chases[index]["name"] = item.text(0).strip() or f"Chase {index + 1}"
        self._refresh_editor()

    # ------------------------------------------------------------------
    # Running the chase through the preview
    # ------------------------------------------------------------------
    def _to_seconds(self, chase: dict, value: float) -> float:
        if CHASE_TIMING_ENABLED and chase.get("units") == BEATS:
            return value * 60.0 / PREVIEW_BPM
        return value

    def _step_timing(self, chase: dict, step: int) -> Tuple[float, float]:
        """One step's (fade, hold) in seconds.

        With per-step timing off, every step costs what the engine charges it —
        one CHASE TIME, crossfading over one BLEND TIME — so Run shows the rig
        rather than a number invented here.
        """
        if not CHASE_TIMING_ENABLED:
            return (ENGINE_FADE_SECONDS,
                    max(0.0, ENGINE_STEP_SECONDS - ENGINE_FADE_SECONDS))
        return (self._to_seconds(chase, chase["fades"][step]),
                self._to_seconds(chase, chase["holds"][step]))

    def _step_seconds(self, chase: dict) -> List[float]:
        """Each step's wall-clock length, in play order."""
        return [max(MIN_STEP_SECONDS, sum(self._step_timing(chase, step)))
                for step in run_order(chase)]

    def _step_values(self, chase: dict, step: int) -> List[int]:
        """A step as a DMX slice: its scene, scaled by its level."""
        values = scene_values(self._fdef, self._scene_at(chase, step))
        if not CHASE_TIMING_ENABLED:
            return values           # the engine plays every step at full
        return _apply_level(values, level_channels(self._fdef),
                            chase["levels"][step])

    def _on_run_clicked(self) -> None:
        if self.run_button.isChecked():
            self._start_run()
        else:
            self._stop_run()

    def _start_run(self) -> None:
        chase = self._current_chase()
        if chase is None or not step_count(chase):
            self._stop_run()
            return
        self._run_order = run_order(chase)
        self._run_position = 0
        self._run_elapsed = 0.0
        self._run_clock = time.monotonic()
        # The first step fades in from wherever the preview already is, which
        # is the step the editor was showing — so pressing Run doesn't snap.
        self._run_from = self._step_values(chase, self._focus)
        self.run_button.setChecked(True)
        self.run_button.setText("Stop")
        self.run_button.setIcon(icons.icon("close", "#ffffff"))
        self.strip.set_steps(self._step_seconds(chase))
        self._mark_running(True)
        self._run_timer.start()

    def _stop_run(self) -> None:
        if self._run_timer.isActive():
            self._run_timer.stop()
        self.run_button.setChecked(False)
        self.run_button.setText("Run")
        self.run_button.setIcon(icons.icon("play", "#ffffff"))
        self._mark_running(False)
        self._refresh_preview()

    def hideEvent(self, event):
        """Stop on the way out — to the Scenes tab, or out of the dialog.

        Now that selecting a chase starts one, a run is the normal state of
        this tab rather than something the operator switched on, so leaving it
        ticking at 30 Hz against a preview nobody is looking at would be the
        common case rather than the rare one.
        """
        super().hideEvent(event)
        self._stop_run()

    def _mark_running(self, running: bool) -> None:
        if self._selected is None:
            return
        item = self.tree.topLevelItem(self._selected)
        if item is not None:
            self.tree.blockSignals(True)
            item.setIcon(0, _ring(running))
            self.tree.blockSignals(False)

    def _on_run_tick(self) -> None:
        chase = self._current_chase()
        if chase is None or not self._run_order:
            self._stop_run()
            return
        now = time.monotonic()
        self._run_elapsed += now - self._run_clock
        self._run_clock = now

        # Advance past however many steps have elapsed — a 33 ms tick can cross
        # more than one when the steps are short.
        for _ in range(len(self._run_order) + 1):
            step = self._run_order[self._run_position]
            span = max(MIN_STEP_SECONDS, sum(self._step_timing(chase, step)))
            if self._run_elapsed < span:
                break
            self._run_elapsed -= span
            self._run_from = self._step_values(chase, step)
            self._run_position = (self._run_position + 1) % len(self._run_order)
        else:
            # Every step is shorter than one tick: hold at the first rather
            # than spin the loop forever.
            self._run_elapsed = 0.0

        step = self._run_order[self._run_position]
        fade, hold = self._step_timing(chase, step)
        target = self._step_values(chase, step)
        if fade > 0 and self._run_elapsed < fade:
            ratio = self._run_elapsed / fade
            values = [clamp(round(a + (b - a) * ratio))
                      for a, b in zip(self._run_from, target)]
        else:
            values = target

        span = max(MIN_STEP_SECONDS, fade + hold)
        self._preview.set_values(values)
        self.strip.set_position(self._run_position, self._run_elapsed / span)
        self._show_step(chase, step)

    def _refresh_preview(self) -> None:
        """The still picture: the step the editor is looking at."""
        chase = self._current_chase()
        if chase is None or not step_count(chase):
            self._preview.set_values([])
            self.strip.set_steps([])
            self.step_label.setText("")
            self.step_detail.setText("")
            return
        step = max(0, min(self._focus, step_count(chase) - 1))
        self._preview.set_values(self._step_values(chase, step))
        self.strip.set_steps(self._step_seconds(chase))
        position = run_order(chase).index(step) if step in run_order(chase) else 0
        self.strip.set_position(position, 0.0)
        self._show_step(chase, step)

    def _show_step(self, chase: dict, step: int) -> None:
        self.step_label.setText(
            f"{step + 1:02d} · {self._step_name(chase, step)}")
        if CHASE_TIMING_ENABLED:
            unit = self._unit_glyph(chase)
            detail = (f"{chase['fades'][step]:g}{unit} fade / "
                      f"{chase['holds'][step]:g}{unit} hold · "
                      f"{chase['levels'][step]} %")
        else:
            # What the engine charges every step, named as such — this is not
            # a property of the chase, so it must not read like one.
            fade, hold = self._step_timing(chase, step)
            detail = (f"{fade + hold:g}s per step · {fade:g}s blend"
                      f"<br>(the fixture's CHASE TIME)")
        self.step_detail.setText(detail)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save_pending_changes(self) -> None:
        """Settle unsaved work on the way out — to another fixture, or out of
        the dialog. Without an autosave, leaving quietly would drop it."""
        if self._fdef is None:
            return
        self._settle_chase_edits()

    def _save(self) -> None:
        """The Save button: write the chase the editor is showing."""
        self._write()

    def _write(self) -> None:
        """Write the chases through to the type's sidecar.

        The one thing here that touches disk. It edits the fixture library, not
        the patch the surrounding dialog builds, so the dialog's OK/Cancel has
        nothing to do with it — and it writes only `chases`, because the Scenes
        tab is holding its own copy of the banks.
        """
        if self._fdef is None or not self._type_name:
            return
        try:
            path = save_fixture_chases(
                self._type_name, self._fdef.mode,
                [denormalise_chase(chase) for chase in self._chases])
        except OSError as error:
            logger.error(f"Could not save chases for '{self._type_name}': {error}")
            self.status_label.setText(f"Could not save: {error}")
            return
        if path is None:
            self.status_label.setText("Could not save — see the log.")
            return
        # The sidecar now holds what the selected chase holds: that is the new
        # baseline, and so the button goes quiet.
        chase = self._current_chase()
        self._baseline = copy_chase(chase) if chase is not None else None
        self._refresh_save_button()
        self._refresh_status()
        self.library_changed.emit()
