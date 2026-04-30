# ABOUTME: Clip library panel — lists draggable MIDI-note clips grouped by fixture.
# ABOUTME: Drops onto the timeline insert events on the matching fixture track.

import json
from collections import defaultdict
from typing import Dict, List, Tuple

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QToolBox, QListWidget,
                             QListWidgetItem, QLabel, QAbstractItemView)
from PyQt6.QtGui import QColor, QPixmap, QIcon, QPainter
from PyQt6.QtCore import Qt, QMimeData, QSize

from config import Config, MidiNoteConfig
from tupperlogger import get_logger


# Custom MIME type carries a JSON payload {"note": int, "velocity": int}.
# Plain-text fallback is also set so the payload is visible in tools that
# only inspect text/plain (handy for debugging the drop chain).
CLIP_MIME_TYPE = "application/x-tupperdmx-midi-clip"
DEFAULT_DROP_VELOCITY = 127

# Fixtures listed first in the toolbox; anything else falls in alphabetical
# order after these.
FIXTURE_PAGE_PRIORITY = {"ledbar": 0, "blinder": 1}


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


class ClipLibraryWidget(QWidget):
    """Panel listing draggable MIDI clips grouped by fixture."""

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
        """Rebuild the toolbox from the current midi_note_map."""
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
            groups[info.fixture].append((note_num, info))

        if not groups:
            placeholder = QLabel(
                "No MIDI notes configured.\n"
                "Edit config.json to add clips to the library."
            )
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setWordWrap(True)
            self.toolbox.addItem(placeholder, "Library")
            return

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
            self.toolbox.addItem(page, fixture.capitalize())

        self.logger.info(
            f"Clip library populated: {len(groups)} fixture group(s), "
            f"{sum(len(v) for v in groups.values())} clip(s)"
        )

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
