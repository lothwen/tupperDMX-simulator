# ABOUTME: Clip library panel — lists draggable MIDI-note clips grouped by fixture.
# ABOUTME: Drops onto the timeline insert events on the matching fixture track.

import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QToolBox, QListWidget,
                             QListWidgetItem, QLabel, QAbstractItemView)
from PyQt6.QtGui import QColor, QPixmap, QIcon, QPainter
from PyQt6.QtCore import Qt, QMimeData, QSize, pyqtSignal

from config import Config, MidiNoteConfig
from midi.manager import CHANNEL_NOTE_MAP
from software_controller.scene_loader import LedScene, load_led_scenes
from tupperlogger import get_logger


# Custom MIME type carries a JSON payload {"note": int, "velocity": int}.
# Plain-text fallback is also set so the payload is visible in tools that
# only inspect text/plain (handy for debugging the drop chain).
CLIP_MIME_TYPE = "application/x-tupperdmx-midi-clip"
DEFAULT_DROP_VELOCITY = 127

# MIME type for LED-scene drops. The payload is a JSON object with the
# scene's name and full 7-channel dict; on drop the timeline expands it
# into 7 simultaneous note-on events sharing a group_id.
SCENE_MIME_TYPE = "application/x-tupperdmx-led-scene"

# Fixtures listed first in the toolbox; anything else falls in alphabetical
# order after these.
FIXTURE_PAGE_PRIORITY = {"ledbar": 0, "blinder": 1}

# Default location of the LED scene definitions, relative to the project root.
DEFAULT_SCENE_CONFIG_PATH = (Path(__file__).resolve().parent.parent
                             / "software_controller" / "dmx_config.json")


class ClipListWidget(QListWidget):
    """A list of clips for a single fixture; emits the custom MIME on drag."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ClipList")
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setIconSize(QSize(16, 16))
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setUniformItemSizes(True)

    def mimeData(self, items):
        if not items:
            return None
        item = items[0]
        note = item.data(Qt.ItemDataRole.UserRole)
        if note is None:
            return None
        payload = {"note": int(note), "velocity": DEFAULT_DROP_VELOCITY}
        encoded = json.dumps(payload).encode("utf-8")
        mime = QMimeData()
        mime.setData(CLIP_MIME_TYPE, encoded)
        mime.setText(json.dumps(payload))
        return mime


class SceneListWidget(QListWidget):
    """A list of LED scenes for a single bank; emits SCENE_MIME_TYPE on drag.

    Each item carries the full scene payload (name + 7-channel dict) so the
    drop handler doesn't need to look anything up. On drop, the timeline
    expands the payload into a 7-event scene group.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SceneList")
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setIconSize(QSize(16, 16))
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setUniformItemSizes(True)

    def mimeData(self, items):
        if not items:
            return None
        item = items[0]
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            return None
        encoded = json.dumps(payload).encode("utf-8")
        mime = QMimeData()
        mime.setData(SCENE_MIME_TYPE, encoded)
        mime.setText(json.dumps(payload))
        return mime


class ClipLibraryWidget(QWidget):
    """Panel listing draggable MIDI clips grouped by fixture."""

    # Emitted when an item is clicked. Payload is a list of (note, velocity)
    # tuples — length 1 for a single clip, length 7 for an LED scene
    # expanded into channel-control notes (matches CHANNEL_NOTE_MAP).
    preview_requested = pyqtSignal(list)

    # Notes 1-7 are channel-control entries used internally by scene drops;
    # the dedicated scene pages already cover them, so we hide them from the
    # per-fixture clip pages to avoid clutter and accidental single drops.
    HIDDEN_CHANNEL_NOTES = frozenset(range(1, 8))

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.setObjectName("ClipLibrary")
        self.config = config
        self.logger = get_logger("ui.clip_library")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.toolbox = QToolBox(self)
        self.toolbox.setObjectName("ClipLibraryToolBox")
        layout.addWidget(self.toolbox)

        # Match the DMX display panel's width policy so the splitter behaves
        # the same regardless of which tab is active.
        self.setMinimumWidth(0)
        self.setMaximumWidth(260)

        self._populate()

    def _populate(self) -> None:
        """Rebuild the toolbox from the current midi_note_map and scene library."""
        while self.toolbox.count() > 0:
            widget = self.toolbox.widget(0)
            self.toolbox.removeItem(0)
            if widget is not None:
                widget.deleteLater()

        groups: Dict[str, List[Tuple[int, MidiNoteConfig]]] = defaultdict(list)
        for note_str, info in self.config.midi_note_map.items():
            try:
                note_num = int(note_str)
            except (ValueError, TypeError):
                continue
            if note_num in self.HIDDEN_CHANNEL_NOTES:
                continue
            groups[info.fixture].append((note_num, info))

        for fixture in sorted(
            groups.keys(),
            key=lambda f: (FIXTURE_PAGE_PRIORITY.get(f, 99), f),
        ):
            page = ClipListWidget()
            for note_num, info in sorted(groups[fixture], key=lambda x: x[0]):
                item = QListWidgetItem(f"{info.part}")
                item.setIcon(self._color_swatch(info.color))
                item.setData(Qt.ItemDataRole.UserRole, note_num)
                page.addItem(item)
            page.itemClicked.connect(self._on_clip_item_clicked)
            self.toolbox.addItem(page, fixture.capitalize())

        self._populate_scene_pages()

        if self.toolbox.count() == 0:
            placeholder = QLabel(
                "No clips configured.\n"
                "Edit config.json or dmx_config.json to add clips."
            )
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setWordWrap(True)
            self.toolbox.addItem(placeholder, "Library")
            return

        self.logger.info(
            f"Clip library populated: {len(groups)} fixture group(s), "
            f"{self.toolbox.count()} toolbox section(s) total"
        )

    def _populate_scene_pages(self) -> None:
        """Add one toolbox section per LED scene bank from dmx_config.json."""
        try:
            scenes = load_led_scenes(DEFAULT_SCENE_CONFIG_PATH)
        except FileNotFoundError:
            self.logger.warning(
                f"Scene config not found at {DEFAULT_SCENE_CONFIG_PATH}; "
                "scene pages skipped"
            )
            return

        by_bank: Dict[str, List[LedScene]] = defaultdict(list)
        for s in scenes:
            by_bank[s.bank].append(s)

        for bank in sorted(by_bank.keys()):
            page = SceneListWidget()
            for scene in sorted(by_bank[bank], key=lambda s: s.index):
                payload = self._scene_to_payload(scene)
                item = QListWidgetItem(scene.name)
                item.setIcon(self._scene_swatch(scene))
                item.setData(Qt.ItemDataRole.UserRole, payload)
                page.addItem(item)
            page.itemClicked.connect(self._on_scene_item_clicked)
            self.toolbox.addItem(page, bank)

    @staticmethod
    def _scene_to_payload(scene: LedScene) -> Dict:
        return {
            "name": scene.name,
            "channels": {
                "brightness": scene.brightness,
                "blue": scene.blue,
                "green": scene.green,
                "red": scene.red,
                "animation": scene.animation,
                "speed": scene.speed,
                "direction": scene.direction,
            },
        }

    def _scene_swatch(self, scene: LedScene) -> QIcon:
        """Color swatch from RGB; near-white scenes get a small star marker
        because they trigger the controller's color-passthrough behavior."""
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        scale = max(scene.brightness / 255.0, 0.18)
        color = QColor(int(scene.red * scale),
                       int(scene.green * scale),
                       int(scene.blue * scale))
        painter.setBrush(color)
        painter.setPen(QColor(0, 0, 0, 80))
        painter.drawRect(0, 0, 15, 15)
        # Mark white-passthrough scenes (R==G==B and ≥254) with a tiny star.
        if (scene.red == scene.green == scene.blue
                and scene.red >= 254):
            painter.setPen(QColor(0, 0, 0))
            font = painter.font()
            font.setPointSize(8)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(2, 13, "*")
        painter.end()
        return QIcon(pixmap)

    def _color_swatch(self, hex_color: str) -> QIcon:
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setBrush(QColor(hex_color))
        painter.setPen(QColor(0, 0, 0, 80))
        painter.drawRect(0, 0, 15, 15)
        painter.end()
        return QIcon(pixmap)

    def refresh_configuration(self, config: Config) -> None:
        """Refresh the library when configuration changes."""
        self.config = config
        self._populate()
        self.logger.info("Clip library refreshed with new configuration")

    def _on_clip_item_clicked(self, item: QListWidgetItem) -> None:
        """Auditioning a single clip emits one (note, velocity=127) tuple."""
        note = item.data(Qt.ItemDataRole.UserRole)
        if note is None:
            return
        self.preview_requested.emit([(int(note), DEFAULT_DROP_VELOCITY)])

    def _on_scene_item_clicked(self, item: QListWidgetItem) -> None:
        """Auditioning a scene emits 7 (note, velocity) tuples — one per
        channel-control note — mirroring how a scene drop is encoded in the
        timeline (see midi.manager.add_scene)."""
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            return
        channels = payload.get("channels") or {}
        if not isinstance(channels, dict):
            return
        messages: List[Tuple[int, int]] = []
        for ch_name, note in CHANNEL_NOTE_MAP:
            if ch_name not in channels:
                continue
            # Same lossy 0..255 → 0..127 mapping the controller doubles back.
            velocity = max(0, min(127, int(channels[ch_name]) // 2))
            messages.append((note, velocity))
        if messages:
            self.preview_requested.emit(messages)
