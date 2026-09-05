# ABOUTME: The fixture browser's Scenes tab — edits a type's sidecar scene banks
# ABOUTME: with a live preview drawn by the real stage render engine.
"""Scene editing for one fixture type.

A scene is a snapshot of every channel of a fixture, stored in the `.tdmx.json`
sidecar's `banks` (see `core/fixture_sidecar.py`). Banks are what MIDI addresses:
note 36+n selects the n-th bank, note 24+n the n-th scene inside it — so both
orders are semantic, and this editor keeps them visible rather than flattening
them into one list.

    SCENES               ┃  SCENE NAME [Open white] [Save as new] [Create]
    ▾ ANIMATIONS      8  ┃  SONG PARTS  (INTRO)(VERSE)(CHORUS)…
       ▪ Cuadrados       ┃  ┌ SIMULATED OUTPUT ──────────────┐
       ▪ Ping Pong       ┃  │ intensity 100 %      <the real │
    ▾ FILLS           8  ┃  │ colour    white       renderer>│
       ▪ Fuego           ┃  └────────────────────────────────┘
                         ┃  ┌ CHANNEL FADERS ────────────────┐
    [+ New] [Dup] [Del]  ┃  │ ▮ ▮ ▮ ▮ ▮ ▮ ▮ ▮ ▮ ▮            │

Nothing here is simulated twice: the preview builds the same renderer the stage
would (`stage.registry.renderer_for`) and feeds it the scene's DMX values, so
what a scene looks like here is what it looks like on stage — animations, beam
cone, strobe and all.

The tab opens on a blank **draft**: nothing is selected, the faders sit at their
channel defaults, and the button beside the name reads "Create" — dial a look in
and press it, and the draft becomes a scene in the target bank. Clicking a scene
in the list loads it instead, and the button becomes "Save", which overwrites
that one. **+ New ▸ New scene** never drafts: it adds the blank scene to the
target bank there and then, selects it and puts the cursor in its name, because
a command called "New scene" that leaves nothing in the list until some *other*
button is found reads as though the click was lost.

Beside it, **Save as new** writes the edits as the NEXT scene instead of over
the one they came from, which puts the original back — pick a look, change it,
press it, repeat, and a bank of variations builds itself without ever leaving
the editor.

**The Save button carries exactly one scene: the one on the faders.** Everything
that changes the LIST rather than a look — new, duplicate, delete, drag,
rename a bank — is applied to the sidecar as it happens, so the button never
speaks for work the editor isn't showing. What it does speak for is settled
before it can be carried anywhere else: stepping onto another scene, starting
a new one, leaving the fixture or closing the dialog all ask whether to keep
the edits, and "No" puts the scene back the way it was written. None of this is
the surrounding dialog's OK, because this edits the fixture *library*, not the
patch that dialog is building.

One caveat this UI cannot hide: adding, deleting or reordering a scene shifts
the indices after it, and a saved .mid binds a scene by index. Renaming a bank
is safe (the chases naming it are rewritten to follow).
"""

from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMenu, QMessageBox, QPushButton, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.fixture_library import FixtureDef, save_fixture_banks
from core.fixture_state import FixtureState, resolve_state
from core.note_layout import SONG_PARTS
from ui.widgets.chips import flow_holder
# The console — faders, preview and read-out — is shared with the timeline's
# clip editor, so a scene looks the same wherever it is edited. Re-exported
# under their old private names for the callers (and tests) that reach in.
from ui.fixture.console import (  # noqa: F401  (re-exported)
    ChannelFader, FaderSlider as _FaderSlider, FaderStrip, OutputCard,
    ScenePreview, capability_rows as _capability_rows, clamp as _clamp,
    PartChip as _PartChip, colour_text as _colour_text, escape as _escape,
    scene_swatch, scene_values,
    strobe_text as _strobe_text, summary_html as _summary_html,
)
from tupperlogger import get_logger

logger = get_logger("ui.fixture.scenes_tab")

DEFAULT_BANK_NAME = "SCENES"

# Roles carried on the tree items, so a selection resolves back to its data
# without a parallel index.
_ROLE_BANK = Qt.ItemDataRole.UserRole
_ROLE_INDEX = Qt.ItemDataRole.UserRole + 1


# --- the scene list -----------------------------------------------------------

class _SceneTree(QTreeWidget):
    """Banks and their scenes, reorderable by drag.

    The drop is resolved to a (source, destination) pair and reported; the items
    themselves are never moved by Qt. The tab owns the ordering, and rebuilding
    the tree from the data afterwards is the only way the two can't drift —
    letting Qt reshuffle the items would leave their bank/index roles pointing
    at where the scene used to be.
    """

    # bank name, scene index (-1 for the bank itself) -> bank name, position
    move_requested = pyqtSignal(str, int, str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("AddFxScenes")
        self.setColumnCount(2)
        self.setHeaderHidden(True)
        self.setIndentation(12)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(1, 46)

    def dropEvent(self, event):
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        dragged = self.currentItem()
        target = self.itemAt(event.position().toPoint())
        if dragged is None:
            return
        destination = self._destination(dragged, target,
                                        self.dropIndicatorPosition())
        if destination is None:
            return
        source_index = dragged.data(0, _ROLE_INDEX)
        self.move_requested.emit(dragged.data(0, _ROLE_BANK),
                                 -1 if source_index is None else source_index,
                                 destination[0], destination[1])

    def _destination(self, dragged, target,
                     position) -> Optional[Tuple[str, int]]:
        """Where the drop lands, or None when it isn't a legal shape.

        A bank stays top-level; a scene always ends up inside some bank.
        """
        above = position == QAbstractItemView.DropIndicatorPosition.AboveItem
        onto = position == QAbstractItemView.DropIndicatorPosition.OnItem
        is_bank = dragged.parent() is None

        if is_bank:
            # Banks only reorder among banks. Dropping past the last one (empty
            # space, or below a bank) appends.
            if target is None:
                return ("", self.topLevelItemCount())
            bank_item = target if target.parent() is None else target.parent()
            index = self.indexOfTopLevelItem(bank_item)
            return ("", index if above else index + 1)

        if target is None:
            return None
        if target.parent() is None:
            # Onto a bank header: the front of that bank.
            return (target.data(0, _ROLE_BANK), 0) if onto else None
        index = target.parent().indexOfChild(target)
        return (target.data(0, _ROLE_BANK), index if above else index + 1)


# --- the tab ------------------------------------------------------------------

class FixtureScenesTab(QWidget):
    """List, edit and preview the scene banks of one fixture type."""

    # A write landed on the type's sidecar: whoever reads the library (the clip
    # library panel) is now stale. Emitted from `_save`, the one thing here that
    # touches disk, so it covers Create, Save and the on-the-way-out prompt.
    library_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._type_name = ""
        self._fdef: Optional[FixtureDef] = None
        self._dmx_start = 1
        # bank name -> list of scene dicts. Owned outright: mutated in place,
        # and nothing reaches the sidecar until Save is pressed.
        self._banks: Dict[str, List[dict]] = {}
        self._chases: List[dict] = []
        self._selected: Optional[Tuple[str, int]] = None   # (bank, index)
        # The scene being composed while nothing is selected: it belongs to no
        # bank yet, which is exactly what Create is for.
        self._draft: Optional[dict] = None
        # The selected scene as the sidecar last got it. Comparing against it is
        # what "this scene has unsaved changes" means — a flag would have to be
        # cleared by every path that puts a scene back, and one that forgot
        # would leave the button lying.
        self._baseline: Optional[dict] = None
        self._part_chips: Dict[str, _PartChip] = {}
        # A bank rename has to rewrite the chases naming it, so that one edit
        # widens what the next save writes.
        self._save_chases = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(14)
        layout.addWidget(self._build_list_panel())
        layout.addWidget(self._build_editor_panel(), 1)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_list_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("AddFxScenesPanel")
        panel.setFixedWidth(238)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(6)
        title = QLabel("SCENES")
        title.setObjectName("AddFxFieldLabel")
        header.addWidget(title)
        header.addStretch(1)
        self.new_button = QToolButton()
        self.new_button.setObjectName("AddFxNewScene")
        self.new_button.setText("+ New")
        self.new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.new_button)
        menu.addAction("New scene", self._new_scene)
        menu.addAction("New bank…", self._add_bank)
        self.new_button.setMenu(menu)
        header.addWidget(self.new_button)
        layout.addLayout(header)

        self.tree = _SceneTree()
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

        name_row = QHBoxLayout()
        name_row.setSpacing(12)
        caption = QLabel("SCENE NAME")
        caption.setObjectName("AddFxFieldLabel")
        caption.setFixedWidth(96)
        name_row.addWidget(caption)
        self.name_input = QLineEdit()
        self.name_input.setObjectName("AddFxSceneName")
        self.name_input.setPlaceholderText("Unnamed scene")
        # Per keystroke, not on `editingFinished`: the button reports what the
        # editor holds, and waiting for the field to lose focus would leave it
        # dark over a name that has already been changed. `textEdited` is the
        # user's typing only — `setText` from `_refresh_editor` is not an edit.
        self.name_input.textEdited.connect(lambda _text: self._on_name_edited())
        name_row.addWidget(self.name_input, 1)
        self.save_as_button = QPushButton("Save as new")
        self.save_as_button.setObjectName("AddFxSceneSaveAs")
        self.save_as_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_as_button.setToolTip(
            "Write these changes as a new scene, leaving this one as it was")
        self.save_as_button.setEnabled(False)
        self.save_as_button.clicked.connect(self._save_as_new)
        name_row.addWidget(self.save_as_button)
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("AddFxSceneSave")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.setToolTip(
            "Write this scene to the fixture's .tdmx.json sidecar")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._on_save_clicked)
        name_row.addWidget(self.save_button)
        layout.addLayout(name_row)

        parts_row = QHBoxLayout()
        parts_row.setSpacing(12)      # same gutter as the name row above
        parts_caption = QLabel("SONG PARTS")
        parts_caption.setObjectName("AddFxFieldLabel")
        parts_caption.setFixedWidth(96)
        parts_row.addWidget(parts_caption, 0, Qt.AlignmentFlag.AlignTop)
        # The nine tags fit one row at the dialog's own width; the flow layout
        # is there for when it doesn't — squeezed into a QHBoxLayout they would
        # overlap instead. Each is sized by the stylesheet's font, which is the
        # only thing that knows how wide the text really is.
        holder = flow_holder(spacing=4)
        for part in SONG_PARTS:
            chip = _PartChip(part)
            chip.setToolTip(f"Offer this scene automatically during {part}")
            chip.clicked.connect(self._on_song_parts_changed)
            self._part_chips[part] = chip
            holder.layout().addWidget(chip)
        parts_row.addWidget(holder, 1)
        layout.addLayout(parts_row)

        self._output = OutputCard()
        self._faders = FaderStrip()
        self._faders.value_changed.connect(self._on_fader_changed)
        layout.addWidget(self._output, 1)
        layout.addWidget(self._faders)
        return panel

    @property
    def preview(self):
        """The stage preview inside the output card. Kept as a direct handle
        because `AddFixtureDialog` calls `scenes_tab.preview.clear()` on close —
        the bodies own animation timers on the shared 30 Hz clock."""
        return self._output.preview

    @property
    def summary_label(self):
        """The SIMULATED OUTPUT read-out inside the output card."""
        return self._output.summary_label

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------
    def set_fixture(self, type_name: str, fdef: Optional[FixtureDef],
                    dmx_start: int) -> None:
        """Show `fdef`'s banks. Called whenever the browser's type or mode
        changes — the channel set, and so every scene, depends on the mode."""
        # Unsaved edits belong to the OUTGOING fixture; settle them before
        # `self._banks` becomes someone else's.
        self.save_pending_changes()
        self._type_name = type_name
        self._fdef = fdef
        self._dmx_start = dmx_start
        self._selected = None
        # Deep-ish copy: the scene dicts are edited in place, and fdef.banks is
        # handed out of the memoized registry.
        self._banks = {name: [dict(scene) for scene in scenes]
                       for name, scenes in (fdef.banks if fdef else {}).items()}
        self._chases = [dict(chase) for chase in (fdef.chases if fdef else [])]
        self._baseline = None
        self._save_chases = False
        # Open on a blank scene rather than on somebody else's: the faders are
        # live from the first click, and Create is what puts them in a bank.
        self._draft = self._default_scene() if fdef else None

        self._faders.set_fixture(fdef, dmx_start)
        self._output.set_fixture(fdef)
        self._rebuild_tree()
        self._refresh_editor()

    def refresh_chases(self, fdef: Optional[FixtureDef]) -> None:
        """Re-read the chases from `fdef` — the Chases tab has just rewritten
        them on disk.

        Only the chases: the banks here may be carrying an unsaved edit, and
        the chase editor never touches them. This copy exists solely so a bank
        rename can follow through into the chases naming it, so a stale one
        would put the old names back the next time one is renamed.
        """
        self._chases = [dict(chase) for chase in (fdef.chases if fdef else [])]
        self._save_chases = False

    def set_dmx_start(self, dmx_start: int) -> None:
        """Re-stamp the fader addresses against a new DMX start."""
        self._dmx_start = dmx_start
        self._faders.set_dmx_start(dmx_start)

    def _rebuild_tree(self) -> None:
        """Relay the whole tree from `self._banks`, keeping the selection."""
        wanted = self._selected
        self.tree.blockSignals(True)
        self.tree.clear()
        restored = None
        for bank_name, scenes in self._banks.items():
            parent = QTreeWidgetItem(self.tree, [bank_name, str(len(scenes))])
            parent.setData(0, _ROLE_BANK, bank_name)
            parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsEditable)
            parent.setForeground(1, QColor("#7a7a7a"))
            parent.setTextAlignment(1, int(Qt.AlignmentFlag.AlignRight
                                           | Qt.AlignmentFlag.AlignVCenter))
            for index, scene in enumerate(scenes):
                child = QTreeWidgetItem(parent, [self._scene_label(scene, index),
                                                 self._parts_label(scene)])
                child.setData(0, _ROLE_BANK, bank_name)
                child.setData(0, _ROLE_INDEX, index)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsEditable)
                child.setIcon(0, self._swatch(scene))
                child.setForeground(1, QColor("#7a7a7a"))
                child.setTextAlignment(1, int(Qt.AlignmentFlag.AlignRight
                                              | Qt.AlignmentFlag.AlignVCenter))
                parts = self._parts_of(scene)
                if parts:
                    child.setToolTip(1, ", ".join(parts))
                if wanted == (bank_name, index):
                    restored = child
            parent.setExpanded(True)
        if restored is not None:
            # Still inside the block: putting the cursor back is not the user
            # moving it, and `_on_tree_selection` asks about unsaved work.
            self.tree.setCurrentItem(restored)
        self.tree.blockSignals(False)

    def _scene_label(self, scene: dict, index: int) -> str:
        return str(scene.get("name") or f"Scene {index + 1}")

    @staticmethod
    def _parts_of(scene: dict) -> List[str]:
        parts = scene.get("song_parts")
        return [str(p) for p in parts] if isinstance(parts, list) else []

    @classmethod
    def _parts_label(cls, scene: dict) -> str:
        """The song-part tags, abbreviated to fit the narrow second column —
        the full list rides along as the row's tooltip."""
        parts = cls._parts_of(scene)
        if not parts:
            return ""
        extra = f"+{len(parts) - 1}" if len(parts) > 1 else ""
        return f"{parts[0][:3]}{extra}"

    def _swatch(self, scene: dict):
        """A chip of the colour this scene resolves to, painted by the engine."""
        return QIcon(scene_swatch(self._fdef, scene))

    # ------------------------------------------------------------------
    # Selection + the editor's state
    # ------------------------------------------------------------------
    def _on_tree_selection(self, current, _previous) -> None:
        left_a_scene = self._selected is not None
        # Whatever is pending belongs to the scene being LEFT, which is still
        # `self._selected` here — settle it before that name changes hands.
        self._settle_scene_edits()
        if current is None:
            self._selected = None
        else:
            bank = current.data(0, _ROLE_BANK)
            index = current.data(0, _ROLE_INDEX)
            self._selected = (bank, index) if index is not None else None
        if self._selected is None and left_a_scene:
            # Stepping off a scene starts a new one. Moving between bank headers
            # doesn't: it only re-targets the draft already being composed.
            self._draft = self._default_scene()
        scene = self._current_scene()
        self._baseline = (dict(scene) if self._selected is not None
                          and scene is not None else None)
        self._refresh_editor()

    def _scene_dirty(self) -> bool:
        """Whether the selected scene has edits the sidecar hasn't got. A draft
        is never "dirty" — it is not in the banks at all; `_draft_touched`
        answers for that one."""
        if self._selected is None or self._baseline is None:
            return False
        return self._current_scene() != self._baseline

    def _settle_scene_edits(self) -> None:
        """Ask what to do with the edits on the selected scene, then do it.

        Called before anything that moves the faders onto something else, and
        before anything that writes for its own reasons — a scene's edits are
        never carried silently onto another scene, nor swept into a save the
        user asked for about something else.
        """
        if not self._scene_dirty():
            return
        bank, index = self._selected
        scene = self._current_scene()
        keep = QMessageBox.question(
            self, "Unsaved scene",
            f"Save the changes to '{self._scene_label(scene, index)}'?")
        if keep == QMessageBox.StandardButton.Yes:
            self._save()
            return
        # Put it back: the dict is the one the banks hold, so it is restored in
        # place, and the row that has been following the edits is re-stamped.
        scene.clear()
        scene.update(self._baseline)
        self._relabel(bank, index)

    def _relabel(self, bank: str, index: int) -> None:
        """Re-stamp one row from its scene — name, tags and swatch.

        A targeted alternative to `_rebuild_tree` for the paths that keep one
        row in step with the editor. The discard in `_settle_scene_edits` needs
        it rather than wants it: that one runs inside `currentItemChanged`, and
        relaying the tree there would delete the item under the signal.
        """
        scenes = self._banks.get(bank, [])
        if not 0 <= index < len(scenes):
            return
        for row in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(row)
            if parent.data(0, _ROLE_BANK) != bank:
                continue
            item = parent.child(index)
            if item is None:
                return
            scene = scenes[index]
            parts = self._parts_of(scene)
            self.tree.blockSignals(True)     # setText is not a rename
            item.setText(0, self._scene_label(scene, index))
            item.setText(1, self._parts_label(scene))
            item.setIcon(0, self._swatch(scene))
            item.setToolTip(1, ", ".join(parts))
            self.tree.blockSignals(False)
            return

    def _drafting(self) -> bool:
        """Whether the faders hold a scene that is in no bank yet."""
        return self._selected is None and self._draft is not None

    def _default_scene(self) -> dict:
        """A blank scene: every channel at the default its definition declares."""
        if self._fdef is None:
            return {}
        return {spec.field: _clamp(spec.default_value)
                for spec in self._fdef.channels}

    def _current_scene(self) -> Optional[dict]:
        """What the faders are editing: the selected scene, or the draft."""
        if self._selected is None:
            return self._draft
        bank, index = self._selected
        scenes = self._banks.get(bank, [])
        return scenes[index] if 0 <= index < len(scenes) else None

    def _scene_values(self, scene: Optional[dict]) -> List[int]:
        """A scene as a DMX slice, filling anything it omits with the channel's
        declared default — the same fallback `resolve_state` applies."""
        return scene_values(self._fdef, scene)

    def _resolve(self, values: List[int]) -> Optional[FixtureState]:
        if self._fdef is None or not values:
            return None
        return resolve_state(self._fdef, values)

    def _refresh_editor(self) -> None:
        """Push the scene on the faders — selected or draft — into the name,
        tags, faders and preview."""
        if self._selected is None and self._draft is None and self._fdef:
            # The invariant every path relies on: nothing selected always means
            # composing a new scene, so the faders are never dead.
            self._draft = self._default_scene()
        scene = self._current_scene()
        editable = scene is not None
        self.name_input.setEnabled(editable)
        for chip in self._part_chips.values():
            chip.setEnabled(editable)
        self._faders.set_editable(editable)
        self.duplicate_button.setEnabled(self.tree.currentItem() is not None)
        self.delete_button.setEnabled(self.tree.currentItem() is not None)
        self._refresh_save_button()

        # A draft shows an empty field with its placeholder — the "Scene N"
        # fallback is a label for something already in a bank.
        if scene is None:
            text = ""
        elif self._selected is None:
            text = str(scene.get("name", ""))
        else:
            text = self._scene_label(scene, self._selected[1])
        self.name_input.blockSignals(True)
        self.name_input.setText(text)
        self.name_input.blockSignals(False)

        parts = scene.get("song_parts") if scene else None
        parts = set(parts) if isinstance(parts, list) else set()
        for name, chip in self._part_chips.items():
            chip.setChecked(name in parts)

        values = self._scene_values(scene)
        self._faders.set_values(values)
        self._refresh_output(values)
        self._refresh_status()

    def _refresh_output(self, values: List[int]) -> None:
        self._output.set_values(values)

    def _refresh_save_button(self) -> None:
        """Create a draft, Save the scene on the faders — and nothing else.

        Changes to the list itself are already on disk by the time they show,
        so the button is only ever offering what the editor is showing.
        """
        offer_create = self._drafting()
        self.save_button.setText("Create" if offer_create else "Save")
        self.save_button.setEnabled(offer_create or self._scene_dirty())
        # Branching needs something to branch FROM, and a draft is already on
        # its way to being a new scene — Create is that.
        self.save_as_button.setEnabled(self._selected is not None
                                       and self._current_scene() is not None)

    def _refresh_status(self) -> None:
        """The line under the list: what is pending, where a new scene would
        land, and why an edit might be ignored."""
        if self._fdef is None:
            self.status_label.setText("")
            return
        scene = self._current_scene()
        if (not self._drafting() and scene is not None
                and not any(self._scene_values(scene))):
            self.status_label.setText(
                "Every channel is 0 — the engine skips all-zero scenes.")
            return
        if self._scene_dirty():
            self.status_label.setText(
                f"Unsaved changes to '{self._scene_label(scene, self._selected[1])}'"
                " — press Save to write them to the fixture's .tdmx.json.")
            return
        if self._drafting():
            self.status_label.setText(
                f"New scene — Create adds it to {self._target_bank()}.")
            return
        total = sum(len(scenes) for scenes in self._banks.values())
        if not total:
            # A fixture taken straight from the OFL project has no banks at all.
            self.status_label.setText(
                "No scenes yet — dial one in and press Create, saved to the "
                "fixture's .tdmx.json sidecar.")
            return
        self.status_label.setText(
            f"{total} scene(s) in {len(self._banks)} bank(s) · "
            f"saved to the fixture's .tdmx.json")

    # ------------------------------------------------------------------
    # Edits
    # ------------------------------------------------------------------
    def _on_fader_changed(self, index: int, value: int) -> None:
        """Live while dragging: the scene and the preview follow the fader, and
        the sidecar catches up when Create/Save is pressed."""
        scene = self._current_scene()
        if scene is None or self._fdef is None or index >= self._fdef.channel_count:
            return
        scene[self._fdef.channels[index].field] = value
        self._refresh_output(self._scene_values(scene))
        if not self._drafting():
            self._refresh_save_button()
            self._refresh_status()

    def _on_name_edited(self) -> None:
        """The typed name, into the scene, the row and the button — live."""
        scene = self._current_scene()
        if scene is None:
            return
        name = self.name_input.text().strip()
        if name == str(scene.get("name", "")):
            return
        if name:
            scene["name"] = name
        else:
            scene.pop("name", None)
        if self._drafting():
            return          # nothing in the tree to relabel yet
        self._relabel(*self._selected)
        self._refresh_save_button()
        self._refresh_status()

    def _on_song_parts_changed(self) -> None:
        scene = self._current_scene()
        if scene is None:
            return
        parts = [name for name in SONG_PARTS if self._part_chips[name].isChecked()]
        if parts:
            scene["song_parts"] = parts
        else:
            scene.pop("song_parts", None)
        if self._drafting():
            return
        self._relabel(*self._selected)
        self._refresh_save_button()
        self._refresh_status()

    def _on_item_renamed(self, item: QTreeWidgetItem, column: int) -> None:
        """An inline edit in the tree renames a bank or a scene."""
        if column != 0 or self._fdef is None:
            return
        text = item.text(0).strip()
        index = item.data(0, _ROLE_INDEX)
        if index is None:
            # A bank rename relays the whole tree, which would delete `item`
            # while this signal is still on the stack; run it after we return.
            old = item.data(0, _ROLE_BANK)
            QTimer.singleShot(0, lambda: self._rename_bank(old, text))
        else:
            bank = self._banks.get(item.data(0, _ROLE_BANK), [])
            if 0 <= index < len(bank):
                # Editing the row IS editing the scene the faders are on — Qt
                # made it current to start the edit — so this is a scene edit
                # like any other, and the button picks it up.
                bank[index]["name"] = text or f"Scene {index + 1}"
                self._refresh_editor()

    # ------------------------------------------------------------------
    # The list itself
    #
    # Adding, copying, deleting, reordering and renaming all change what MIDI
    # addresses, show in the list the moment they are done, and are not what
    # the Save button is about — so each one writes as it happens. Those that
    # can run with an edited scene on the faders settle it first, so their
    # write never sweeps up a pending edit (and `_settle_scene_edits` asks
    # nothing unless there is one). The bank-level ones cannot: a bank header
    # is the current item there, so no scene is selected.
    # ------------------------------------------------------------------
    def _rename_bank(self, old: str, new: str) -> None:
        """Rename a bank, following through into the chases that name it.

        Chases reference a bank by NAME, so this is the one rename that can be
        made safe — and has to be, or a rename silently breaks every chase.
        """
        if not new or new == old or old not in self._banks:
            self._rebuild_tree()
            return
        if new in self._banks:
            QMessageBox.warning(self, "Rename bank",
                                f"A bank called '{new}' already exists.")
            self._rebuild_tree()
            return

        self._banks = {(new if name == old else name): scenes
                       for name, scenes in self._banks.items()}
        for chase in self._chases:
            names = chase.get("banks")
            if isinstance(names, list):
                chase["banks"] = [new if n == old else n for n in names]
        if self._selected and self._selected[0] == old:
            self._selected = (new, self._selected[1])
        self._save_chases = True
        self._save()
        self._rebuild_tree()
        self._refresh_editor()

    def _add_bank(self) -> None:
        if self._fdef is None:
            return
        name, ok = QInputDialog.getText(self, "New bank", "Bank name:")
        name = name.strip()
        if not ok or not name:
            return
        if name in self._banks:
            QMessageBox.warning(self, "New bank",
                                f"A bank called '{name}' already exists.")
            return
        self._settle_scene_edits()
        self._banks[name] = []
        self._selected = None
        self._save()
        self._rebuild_tree()
        self._refresh_editor()

    def _target_bank(self) -> str:
        """Which bank a created scene joins: the selected one, else the first,
        else a default one (a fixture straight from OFL has no banks at all)."""
        item = self.tree.currentItem()
        if item is not None:
            return item.data(0, _ROLE_BANK)
        return next(iter(self._banks), DEFAULT_BANK_NAME)

    def _new_scene(self) -> None:
        """Add a blank scene to the target bank and select it for editing.

        Unlike the draft the tab opens on, this one exists from the click: it
        is in the list, in a bank, in the sidecar, and its name is focused so
        it can be typed over. What the Save button then carries is the look
        dialled into it, which is all it ever carries.
        """
        if self._fdef is None:
            return
        self._settle_scene_edits()
        bank = self._target_bank()
        scenes = self._banks.setdefault(bank, [])
        scene = self._default_scene()
        scene["name"] = f"Scene {len(scenes) + 1}"
        scenes.append(scene)
        self._selected = (bank, len(scenes) - 1)
        self._draft = None
        self._save()
        self._rebuild_tree()
        self._refresh_editor()
        self.name_input.setFocus(Qt.FocusReason.OtherFocusReason)
        self.name_input.selectAll()

    def _create(self) -> None:
        """Turn the draft on the faders into a real scene in its target bank."""
        if self._fdef is None or self._draft is None:
            return
        bank = self._target_bank()
        scenes = self._banks.setdefault(bank, [])
        scene = dict(self._draft)
        scene.setdefault("name", f"Scene {len(scenes) + 1}")
        scenes.append(scene)
        self._selected = (bank, len(scenes) - 1)
        self._draft = None
        self._save()
        self._rebuild_tree()
        self._refresh_editor()

    def _save_as_new(self) -> None:
        """Write what is on the faders as a NEW scene, next to the one it came
        from, and put that one back the way it was written.

        The template loop: the edits become the new scene rather than an
        overwrite, so building a bank of variations never needs a Duplicate, a
        re-select and a second round of dialling.
        """
        if self._fdef is None or self._selected is None or self._baseline is None:
            return
        bank, index = self._selected
        scenes = self._banks.get(bank, [])
        if not 0 <= index < len(scenes):
            return
        scene = dict(scenes[index])              # the edits, name and all
        if scene.get("name", "") == self._baseline.get("name", ""):
            # Not renamed on the way through, so name it for its source the
            # way Duplicate does rather than leaving the bank with two of them.
            scene["name"] = f"{self._scene_label(self._baseline, index)} copy"
        scenes[index] = dict(self._baseline)     # the original, untouched
        scenes.insert(index + 1, scene)
        # Selecting the new one is what makes the loop work: the next branch
        # comes off this scene, so repeats come out in the order they were made.
        self._selected = (bank, index + 1)
        self._save()
        self._rebuild_tree()
        self._refresh_editor()
        self.name_input.setFocus(Qt.FocusReason.OtherFocusReason)
        self.name_input.selectAll()

    def _duplicate(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        self._settle_scene_edits()
        bank = item.data(0, _ROLE_BANK)
        index = item.data(0, _ROLE_INDEX)
        if index is None:
            self._duplicate_bank(bank)
            return
        scenes = self._banks.get(bank, [])
        if not 0 <= index < len(scenes):
            return
        copy = dict(scenes[index])
        copy["name"] = f"{self._scene_label(copy, index)} copy"
        scenes.insert(index + 1, copy)
        self._selected = (bank, index + 1)
        self._save()
        self._rebuild_tree()
        self._refresh_editor()

    def _duplicate_bank(self, bank: str) -> None:
        scenes = self._banks.get(bank)
        if scenes is None:
            return
        name = f"{bank} copy"
        suffix = 2
        while name in self._banks:
            name, suffix = f"{bank} copy {suffix}", suffix + 1
        self._banks[name] = [dict(scene) for scene in scenes]
        self._selected = None
        self._save()
        self._rebuild_tree()
        self._refresh_editor()

    def _delete(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        bank = item.data(0, _ROLE_BANK)
        index = item.data(0, _ROLE_INDEX)
        if index is None:
            self._delete_bank(bank)
            return
        scenes = self._banks.get(bank, [])
        if not 0 <= index < len(scenes):
            return
        if QMessageBox.question(
                self, "Delete scene",
                f"Delete '{self._scene_label(scenes[index], index)}'?") != \
                QMessageBox.StandardButton.Yes:
            return
        # No settling: whatever was pending on it goes with it.
        del scenes[index]
        self._selected = (bank, min(index, len(scenes) - 1)) if scenes else None
        self._save()
        self._rebuild_tree()
        self._refresh_editor()

    def _delete_bank(self, bank: str) -> None:
        scenes = self._banks.get(bank)
        if scenes is None:
            return
        # Chases name a bank, so deleting one leaves them pointing at nothing.
        affected = sum(1 for chase in self._chases
                       if bank in (chase.get("banks") or []))
        message = f"Delete the bank '{bank}' and its {len(scenes)} scene(s)?"
        if affected:
            message += f"\n\n{affected} chase(s) reference this bank."
        if QMessageBox.question(self, "Delete bank", message) != \
                QMessageBox.StandardButton.Yes:
            return
        del self._banks[bank]
        self._selected = None
        self._save()
        self._rebuild_tree()
        self._refresh_editor()

    def _on_move(self, source_bank: str, source_index: int,
                 target_bank: str, position: int) -> None:
        """Apply a drag: move one scene, or one whole bank, to `position`.

        Both orders are what MIDI addresses (note 24+n picks the n-th scene,
        36+n the n-th bank), so a drag here re-binds notes — the same as adding
        or deleting one does.
        """
        self._settle_scene_edits()
        if source_index < 0:
            self._move_bank(source_bank, position)
        else:
            self._move_scene(source_bank, source_index, target_bank, position)
        self._save()
        self._rebuild_tree()
        self._refresh_editor()

    def _move_bank(self, name: str, position: int) -> None:
        order = [bank for bank in self._banks if bank != name]
        if name not in self._banks:
            return
        # `position` counts the dragged bank itself, so drop it from the index
        # once the list no longer contains it.
        current = list(self._banks).index(name)
        insert_at = max(0, min(len(order), position - (1 if position > current else 0)))
        order.insert(insert_at, name)
        self._banks = {bank: self._banks[bank] for bank in order}
        self._selected = None

    def _move_scene(self, source_bank: str, source_index: int,
                    target_bank: str, position: int) -> None:
        source = self._banks.get(source_bank)
        target = self._banks.get(target_bank)
        if source is None or target is None or not 0 <= source_index < len(source):
            return
        scene = source.pop(source_index)
        if source_bank == target_bank and source_index < position:
            position -= 1
        position = max(0, min(len(target), position))
        target.insert(position, scene)
        self._selected = (target_bank, position)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _on_save_clicked(self) -> None:
        """The button: Create the draft, or Save the scene it is showing."""
        if self._drafting():
            self._create()
        else:
            self._save()

    def _draft_touched(self) -> bool:
        """A draft counts as work only once it has moved off the defaults —
        otherwise merely visiting the tab would ask on the way out."""
        return self._drafting() and self._draft != self._default_scene()

    def save_pending_changes(self) -> None:
        """Settle unsaved work on the way out — to another fixture, or out of
        the dialog. Without an autosave, leaving quietly would drop it."""
        if self._fdef is None:
            return
        if self._draft_touched():
            # A draft is in no bank yet, so there is nothing to put back and
            # nothing in the list to keep: it is created, or it is dropped.
            answer = QMessageBox.question(
                self, "Unsaved scene",
                f"Add the new scene to '{self._type_name}'?")
            if answer == QMessageBox.StandardButton.Yes:
                self._create()
            else:
                self._draft = self._default_scene()
            return
        self._settle_scene_edits()

    def _save(self) -> None:
        """Write the banks through to the type's sidecar.

        The one thing here that touches disk. It edits the fixture library, not
        the patch the surrounding dialog builds, so the dialog's OK/Cancel has
        nothing to do with it.
        """
        if self._fdef is None or not self._type_name:
            return
        try:
            path = save_fixture_banks(
                self._type_name, self._fdef.mode, self._banks,
                self._chases if self._save_chases else None)
        except OSError as error:
            logger.error(f"Could not save scenes for '{self._type_name}': {error}")
            self.status_label.setText(f"Could not save: {error}")
            return
        if path is None:
            self.status_label.setText("Could not save — see the log.")
            return
        # The sidecar now holds what the selected scene holds: that is the new
        # baseline, and so the button goes quiet.
        scene = self._current_scene()
        self._baseline = (dict(scene) if self._selected is not None
                          and scene is not None else None)
        self._save_chases = False
        self._refresh_save_button()
        self._refresh_status()
        self.library_changed.emit()


