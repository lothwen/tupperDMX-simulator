"""
Timeline manager for TupperDMX Simulator.

Coordinates multiple timeline tracks and provides the main timeline interface.
"""

import os
import math
import copy
from typing import Optional, List, Dict, Any

from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QGraphicsView,
                             QGraphicsScene, QSlider, QPushButton, QLabel,
                             QSizePolicy, QGraphicsLineItem, QGraphicsProxyWidget,
                             QDialog, QMessageBox)
from PyQt6.QtGui import QColor, QPen
from PyQt6.QtCore import Qt, pyqtSignal, QTimer

from .widgets import (ContentView, MidiEventEditDialog, StartEventEditDialog,
                      TrackResizeHandle, CustomTooltip)
from .widgets.scene_edit import SceneEditDialog
from core import note_layout
from core.fixture_library import load_fixture_defs, resolve_fixture
from .tracks import WaveformTrack, MidiEventsTrack, RulerTrack
from .tracks.midi_events import build_render_units, DraggableMidiEvent
from tupperlogger import get_logger

# MIDI note number for the song-part marker that triggers START handling in
# the software controller (see software_controller/main.py:pick_scene_from_song_part).
START_NOTE = note_layout.START


class Timeline(QWidget):
    """Main timeline widget coordinating multiple tracks."""
    
    # Signals
    seek_requested = pyqtSignal(float)
    bpm_detected = pyqtSignal(int)
    waveform_offset_changed = pyqtSignal(float)
    metronome_volume_changed = pyqtSignal(int)
    metronome_mute_toggled = pyqtSignal(bool)
    audio_volume_changed = pyqtSignal(int)
    audio_mute_toggled = pyqtSignal(bool)
    voice_volume_changed = pyqtSignal(int)
    voice_mute_toggled = pyqtSignal(bool)
    midi_data_changed = pyqtSignal()  # Emitted when MIDI data is modified
    track_heights_changed = pyqtSignal(int, int)  # waveform_height, midi_events_height (on commit)
    zoom_changed = pyqtSignal(float)  # current horizontal zoom factor (m11)

    # Constants
    HEADER_WIDTH = 240
    RULER_TRACK_HEIGHT = 25
    DEFAULT_WAVEFORM_TRACK_HEIGHT = 100
    DEFAULT_MIDI_EVENTS_TRACK_HEIGHT = 50
    MIN_TRACK_HEIGHT = 30
    SECONDS_TO_DISPLAY = 240
    PIXELS_PER_SECOND = 20

    def __init__(self, midi_manager=None, audio_file=None, initial_metronome_volume=0.5,
                 initial_audio_volume=0.5, initial_voice_volume=0.8,
                 initial_waveform_track_height=None, initial_midi_events_track_height=None,
                 fixtures=None, parent=None):
        super().__init__(parent)
        self.audio_file = audio_file
        self.midi_manager = midi_manager

        # The patch: one MIDI events track is built per fixture, in patch order
        # (a fixture's index == the MIDI channel that routes to it).
        self.fixtures = list(fixtures or [])

        # Header "TRACKS" counter: fixture count is live; group count is a
        # placeholder until fixture groups are implemented.
        self._group_count = 0

        self.bpm = 120
        self.voice_cues = []  # current song's [(time_sec, text)] for the ruler
        self.ruler_mode = "bars"  # "bars" | "time"
        self.beat_division = 4  # ruler/grid lines per bar in Bars mode (1, 4 or 8)
        self.snap_enabled = True  # snap clip drags to the grid + neighbour edges

        # Resizable track heights (instance attributes so they can be changed at runtime)
        self.waveform_track_height = max(
            self.MIN_TRACK_HEIGHT,
            int(initial_waveform_track_height) if initial_waveform_track_height is not None
            else self.DEFAULT_WAVEFORM_TRACK_HEIGHT
        )
        self.midi_events_track_height = max(
            self.MIN_TRACK_HEIGHT,
            int(initial_midi_events_track_height) if initial_midi_events_track_height is not None
            else self.DEFAULT_MIDI_EVENTS_TRACK_HEIGHT
        )

        # One MidiEventsTrack per patched fixture (built in _init_tracks). Seeded
        # empty so the layout helpers work during pre-track scene setup.
        self.midi_events_tracks = []

        # Resize handle items (created in _init_tracks)
        self.waveform_resize_handle = None
        self.midi_events_resize_handle = None

        # Combined header panel proxy (spans the ruler + waveform header band).
        self._master_header_proxy = None

        # Legacy compatibility - populated by tracks
        self.midi_event_items = []

        # Cache for current UI data (refreshed from MidiManager)
        self._cached_events = []

        # Multi-selection: event ids of selected clips, persisted across the
        # full item rebuild that every redraw performs so a selection survives
        # a move/delete (and can be nudged repeatedly). `_applying_selection`
        # guards the selectionChanged handler from recursing while we restore.
        self._selected_event_ids = set()
        self._applying_selection = False

        # Single shared clip tooltip. Created lazily, reused by every clip.
        # A per-clip tooltip would be orphaned on screen when a redraw
        # destroys the hovered clip before its hoverLeaveEvent fires; one
        # shared widget that redraw_events hides cannot leave a ghost.
        self._clip_tooltip = None

        # Time grid items (managed centrally)
        self.time_grid_items = []

        # Deferred-refresh guard: coalesces multiple refresh requests
        # (e.g. data_changed + explicit call) into a single repaint on
        # the next event-loop tick, preventing scene rebuilds while
        # QGraphicsItem event handlers are still on the call stack.
        self._refresh_pending = False

        # Logger for debug messages
        self.logger = get_logger("timeline.manager")

        # Create control widgets
        self._create_control_widgets(initial_metronome_volume, initial_audio_volume,
                                     initial_voice_volume)
        
        # Initialize UI
        self.init_ui()
        # Build the fixture tracks before the scenes so setup_scenes() can derive
        # the scene height from them (each track's height is per-instance now, so
        # the total is the sum of the real track objects, not fixture count).
        self._build_midi_event_tracks()
        self.setup_scenes()

        # Initialize tracks
        self._init_tracks()
        
    def _create_control_widgets(self, initial_metronome_volume, initial_audio_volume,
                                initial_voice_volume=0.8):
        """Build the combined header panel: a "TRACKS" title + fixture/group
        counter over MASTER (audio), CLICK (metronome) and VOICE (TTS guide)
        volume rows."""
        # MASTER = audio output volume, drawn with a green→orange level gradient.
        self.audio_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.audio_volume_slider.setObjectName("MasterVolumeSlider")
        self.audio_volume_slider.setRange(0, 100)
        self.audio_volume_slider.setValue(int(initial_audio_volume * 100))
        self.audio_volume_slider.valueChanged.connect(self.audio_volume_changed.emit)
        self.audio_volume_slider.valueChanged.connect(self._update_audio_db)

        self.audio_mute_button = QPushButton("M")
        self.audio_mute_button.setObjectName("AudioMuteButton")
        self.audio_mute_button.setCheckable(True)
        self.audio_mute_button.setFixedSize(18, 18)
        self.audio_mute_button.toggled.connect(self.on_audio_mute_toggled)

        self._audio_db_label = QLabel()
        self._audio_db_label.setObjectName("VolumeDbLabel")
        self._audio_db_label.setFixedWidth(36)
        self._audio_db_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)

        # METRO = metronome volume (kept here so the control isn't lost; the
        # metronome mute also lives on the toolbar).
        self.metronome_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.metronome_volume_slider.setObjectName("MetroVolumeSlider")
        self.metronome_volume_slider.setRange(0, 100)
        self.metronome_volume_slider.setValue(int(initial_metronome_volume * 100))
        self.metronome_volume_slider.valueChanged.connect(self.metronome_volume_changed.emit)
        self.metronome_volume_slider.valueChanged.connect(self._update_metro_db)

        self.metronome_mute_button = QPushButton("M")
        self.metronome_mute_button.setObjectName("MetronomeMuteButton")
        self.metronome_mute_button.setCheckable(True)
        self.metronome_mute_button.setFixedSize(18, 18)
        self.metronome_mute_button.toggled.connect(self.on_mute_toggled)

        self._metro_db_label = QLabel()
        self._metro_db_label.setObjectName("VolumeDbLabel")
        self._metro_db_label.setFixedWidth(36)
        self._metro_db_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)

        # VOICE = TTS guide bus (voice cues + spoken count-in).
        self.voice_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.voice_volume_slider.setObjectName("MetroVolumeSlider")
        self.voice_volume_slider.setRange(0, 100)
        self.voice_volume_slider.setValue(int(initial_voice_volume * 100))
        self.voice_volume_slider.valueChanged.connect(self.voice_volume_changed.emit)
        self.voice_volume_slider.valueChanged.connect(self._update_voice_db)

        self.voice_mute_button = QPushButton("M")
        self.voice_mute_button.setObjectName("MetronomeMuteButton")
        self.voice_mute_button.setCheckable(True)
        self.voice_mute_button.setFixedSize(18, 18)
        self.voice_mute_button.toggled.connect(self.on_voice_mute_toggled)

        self._voice_db_label = QLabel()
        self._voice_db_label.setObjectName("VolumeDbLabel")
        self._voice_db_label.setFixedWidth(36)
        self._voice_db_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)

        # "TRACKS" title row with the live fixture / placeholder group counter.
        tracks_title = QLabel("TRACKS")
        tracks_title.setObjectName("TracksTitle")
        self.track_count_label = QLabel()
        self.track_count_label.setObjectName("TracksCount")
        self.track_count_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                            | Qt.AlignmentFlag.AlignVCenter)
        tracks_row = QHBoxLayout()
        tracks_row.setContentsMargins(0, 0, 0, 0)
        tracks_row.addWidget(tracks_title)
        tracks_row.addStretch(1)
        tracks_row.addWidget(self.track_count_label)

        self.master_header_container = QWidget()
        self.master_header_container.setObjectName("TrackHeaderPanel")
        self.master_header_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.master_header_container.setFixedWidth(self.HEADER_WIDTH)
        panel = QVBoxLayout(self.master_header_container)
        panel.setContentsMargins(8, 6, 8, 8)
        panel.setSpacing(4)
        panel.addLayout(tracks_row)
        panel.addStretch(1)
        panel.addLayout(self._volume_row("MASTER", self.audio_mute_button,
                                         self.audio_volume_slider, self._audio_db_label))
        panel.addLayout(self._volume_row("CLICK", self.metronome_mute_button,
                                         self.metronome_volume_slider, self._metro_db_label))
        panel.addLayout(self._volume_row("VOICE", self.voice_mute_button,
                                         self.voice_volume_slider, self._voice_db_label))
        panel.addStretch(1)

        self._update_audio_db(self.audio_volume_slider.value())
        self._update_metro_db(self.metronome_volume_slider.value())
        self._update_voice_db(self.voice_volume_slider.value())
        self._update_track_counts()

    def _volume_row(self, label_text, mute_button, slider, db_label) -> QHBoxLayout:
        """One labeled volume row: title, mute toggle, slider, dB readout."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        label = QLabel(label_text)
        label.setObjectName("VolumeRowLabel")
        label.setFixedWidth(46)
        row.addWidget(label)
        row.addWidget(mute_button)
        row.addWidget(slider, 1)
        row.addWidget(db_label)
        return row

    @staticmethod
    def _format_db(percent: int) -> str:
        """Render a 0-100 volume percentage as an approximate dB readout
        (100% = 0 dB, 0% = -∞)."""
        if percent <= 0:
            return "-∞"
        db = 20.0 * math.log10(percent / 100.0)
        return "0dB" if db >= -0.5 else f"{round(db)}dB"

    def _update_audio_db(self, value: int) -> None:
        self._audio_db_label.setText(self._format_db(value))

    def _update_metro_db(self, value: int) -> None:
        self._metro_db_label.setText(self._format_db(value))

    def _update_voice_db(self, value: int) -> None:
        self._voice_db_label.setText(self._format_db(value))

    def _update_track_counts(self) -> None:
        """Refresh the TRACKS counter (live fixtures · placeholder groups)."""
        self.track_count_label.setText(f"{len(self.fixtures)} · {self._group_count} grp")

    def set_group_count(self, count: int) -> None:
        """Set the group count shown in the TRACKS header (groups TBD)."""
        self._group_count = max(0, int(count))
        self._update_track_counts()

    def _layout_master_header(self) -> None:
        """Create/position the combined header panel so it spans the ruler and
        waveform header band as one block."""
        if self._master_header_proxy is None:
            self._master_header_proxy = QGraphicsProxyWidget()
            self._master_header_proxy.setWidget(self.master_header_container)
            # Above the per-track background rects, below the resize handle (z=6).
            self._master_header_proxy.setZValue(1)
        if self._master_header_proxy.scene() is None:
            self.header_scene.addItem(self._master_header_proxy)
        bottom = self._waveform_y() + self.waveform_track_height
        self.master_header_container.setFixedWidth(self.HEADER_WIDTH)
        self.master_header_container.setFixedHeight(int(bottom))
        self._master_header_proxy.setPos(0, 0)

    def init_ui(self):
        """Initialize the UI layout."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header_view = QGraphicsView(self)
        self.header_scene = QGraphicsScene(self)
        self.header_view.setScene(self.header_scene)
        self.header_view.setFixedWidth(self.HEADER_WIDTH)
        self.header_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.header_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.header_view.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding) 
        
        self.content_view = ContentView(self, self)
        self.content_scene = QGraphicsScene(self)
        # Disable BSP spatial indexing.  The default BspTreeIndex defers
        # internal tree rebuilds, and stale pointers in the index can
        # survive across event-loop ticks when items are rapidly removed
        # and re-added (e.g. during timeline redraws).  With NoIndex,
        # Qt does a linear scan on each paint — acceptable for our item
        # count (<200) — and eliminates the use-after-free SIGSEGV.
        self.content_scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)
        self.content_view.setScene(self.content_scene)
        # Track selection centrally so multi-select affordances and the
        # persisted id set stay in sync as clips are (de)selected.
        self.content_scene.selectionChanged.connect(self._on_selection_changed)

        layout.addWidget(self.header_view)
        layout.addWidget(self.content_view)

        # Sync scrollbars
        self.content_view.verticalScrollBar().valueChanged.connect(self.header_view.verticalScrollBar().setValue)
        self.header_view.verticalScrollBar().valueChanged.connect(self.content_view.verticalScrollBar().setValue)

    def setup_scenes(self):
        """Setup the graphics scenes."""
        self.content_width = self.PIXELS_PER_SECOND * self.SECONDS_TO_DISPLAY

        self.header_scene.setBackgroundBrush(QColor(30, 30, 30))
        self.content_scene.setBackgroundBrush(QColor(30, 30, 30))

        self._recompute_scene_height()
        self.header_scene.setSceneRect(0, 0, self.HEADER_WIDTH, self.scene_height)
        self.content_scene.setSceneRect(0, 0, self.content_width, self.scene_height)

        self.draw_playhead()
        self.draw_time_grid()

    def _waveform_y(self) -> int:
        """Y offset of the waveform track (flush under the ruler)."""
        return self.RULER_TRACK_HEIGHT

    def _midi_tracks_top(self) -> int:
        """Y offset of the first (top) MIDI events track.

        Only a 1px hairline separates the waveform band from the first fixture
        header (each header carries its own bottom border); a wider gap would
        show the dark scene background as a black band."""
        return self._waveform_y() + self.waveform_track_height

    def _in_waveform_band(self, scene_y: float) -> bool:
        """True if `scene_y` falls within the waveform track's vertical band
        (where song-part sections are dropped/overlaid)."""
        top = self._waveform_y()
        return top <= scene_y < top + self.waveform_track_height

    def _midi_track_y(self, index: int) -> int:
        """Y offset of the MIDI events track at `index`.

        Heights are per-track (a collapsed lane is shorter), so the offset is the
        running sum of the preceding tracks' heights rather than `index * h`.

        Fixture tracks stack flush (no inter-track gap): each header draws its
        own 1px separator line, so any gap here would just show the dark scene
        background as an extra black band between rows."""
        y = self._midi_tracks_top()
        for track in self.midi_events_tracks[:index]:
            y += track.get_track_height()
        return y

    def _midi_tracks_bottom(self) -> int:
        """Y just past the last MIDI events track (== top when the patch is empty)."""
        if not self.midi_events_tracks:
            return self._midi_tracks_top()
        last = len(self.midi_events_tracks) - 1
        return self._midi_track_y(last) + self.midi_events_tracks[last].get_track_height()

    def _recompute_scene_height(self):
        """Compute total scene height from the current track layout."""
        self.scene_height = self._midi_tracks_bottom()

    def _fixture_labels(self, ordered) -> List[str]:
        """Header label per patched fixture (in display order): its name, or the
        capitalized type (plus a 1-based ordinal when that type appears more than
        once in the patch) -> e.g. ['Ledbar', 'Blinder 1', 'Blinder 2']."""
        type_counts: Dict[str, int] = {}
        for inst in ordered:
            type_counts[inst.type] = type_counts.get(inst.type, 0) + 1
        seen: Dict[str, int] = {}
        labels: List[str] = []
        for inst in ordered:
            name = (getattr(inst, "name", "") or "").strip()
            if name:
                labels.append(name)
                continue
            cap = inst.type.capitalize()
            if type_counts.get(inst.type, 0) > 1:
                seen[inst.type] = seen.get(inst.type, 0) + 1
                labels.append(f"{cap} {seen[inst.type]}")
            else:
                labels.append(cap)
        return labels

    def _build_midi_event_tracks(self):
        """(Re)build the MidiEventsTrack list from the current patch — objects
        only, no drawing. Tracks are laid out top-to-bottom by DMX address
        (presentation), but each binds to its fixture's stable MIDI channel
        (identity), so reordering/insert/remove never re-binds existing clips."""
        display = sorted(self.fixtures, key=lambda f: getattr(f, "dmx_start", 0))
        labels = self._fixture_labels(display)

        def _channel(inst, row):
            # Stable channel for real fixtures; fall back to the display row for
            # synthetic/test fixtures built without one.
            ch = getattr(inst, "channel", -1)
            return ch if ch >= 0 else row

        self.midi_events_tracks = [
            MidiEventsTrack(self, self.content_scene, self.header_scene,
                            fixture_index=_channel(inst, i), label=labels[i])
            for i, inst in enumerate(display)
        ]

    def _init_tracks(self):
        """Initialize all timeline tracks (one MIDI track per patched fixture).

        Tracks are laid out top-to-bottom by DMX address (presentation), but
        each track binds to its fixture's stable MIDI channel (identity), so
        reordering/insert/remove never re-binds existing clips."""
        self.ruler_track = RulerTrack(self, self.content_scene, self.header_scene)
        self.waveform_track = WaveformTrack(self, self.content_scene, self.header_scene)

        # MIDI tracks are already built (before setup_scenes, so the scene height
        # could account for them); here we just lay out their headers.

        # Draw initial track layout
        self.ruler_track.draw_header()
        self.ruler_track.draw_ruler()

        self.waveform_track.draw_header(self._waveform_y())
        for i, track in enumerate(self.midi_events_tracks):
            track.draw_header(self._midi_track_y(i))

        # Resize handles for the resizable tracks
        self._create_resize_handles()

        # Combined header panel overlaying the ruler + waveform header band.
        self._layout_master_header()

        # Draw initial waveform if audio file provided
        if self.audio_file:
            self.waveform_track.draw_waveform(self.audio_file, self._waveform_y())

    def _create_resize_handles(self):
        """Create draggable resize handles at the bottom of the resizable tracks."""
        # Waveform handle
        self.waveform_resize_handle = TrackResizeHandle(
            self.HEADER_WIDTH, self.MIN_TRACK_HEIGHT,
            lambda: self.waveform_track_height
        )
        self.waveform_resize_handle.height_preview.connect(self._on_waveform_height_preview)
        self.waveform_resize_handle.height_committed.connect(self._on_waveform_height_committed)
        self.header_scene.addItem(self.waveform_resize_handle)

        self._create_midi_resize_handle()
        self._reposition_resize_handles()

    def _create_midi_resize_handle(self):
        """Create the single shared MIDI-events resize handle (all fixture tracks
        share one height). No-op when the patch is empty. Sits at the bottom of
        the last track; dragging resizes every MIDI track."""
        if self.midi_events_tracks:
            self.midi_events_resize_handle = TrackResizeHandle(
                self.HEADER_WIDTH, self.MIN_TRACK_HEIGHT,
                lambda: self.midi_events_track_height
            )
            self.midi_events_resize_handle.height_preview.connect(self._on_midi_events_height_preview)
            self.midi_events_resize_handle.height_committed.connect(self._on_midi_events_height_committed)
            self.header_scene.addItem(self.midi_events_resize_handle)

    def rebuild_for_patch(self, fixtures):
        """Rebuild the MIDI tracks in place for a changed fixture patch (designer
        add/remove/dmx/channel/name edit), preserving the ruler/waveform tracks
        and their controls. Safe to call synchronously — the trigger is a
        designer button, not a timeline-scene event."""
        for track in self.midi_events_tracks:
            track.clear_items()
        self.midi_events_tracks = []
        # The shared MIDI resize handle is rebuilt to match the new track count.
        if self.midi_events_resize_handle is not None:
            if self.midi_events_resize_handle.scene():
                self.header_scene.removeItem(self.midi_events_resize_handle)
            self.midi_events_resize_handle = None

        self.fixtures = list(fixtures or [])
        self._update_track_counts()
        self._build_midi_event_tracks()
        self._create_midi_resize_handle()
        # Reposition headers/content, redraw clips onto the new lanes (by
        # channel), refresh grid/playhead, and place the handles.
        self._reflow_layout(redraw_waveform=False)

    def _reposition_resize_handles(self):
        """Place each resize handle at the bottom border of its track header."""
        thickness = TrackResizeHandle.HANDLE_THICKNESS
        if self.waveform_resize_handle is not None:
            self.waveform_resize_handle.setPos(
                0, self._waveform_y() + self.waveform_track_height - thickness
            )
        if self.midi_events_resize_handle is not None:
            self.midi_events_resize_handle.setPos(
                0, self._midi_tracks_bottom() - thickness
            )

    def _on_waveform_height_preview(self, new_height: int):
        """Live update during waveform resize drag (no expensive waveform redraw)."""
        if new_height == self.waveform_track_height:
            return
        self.waveform_track_height = max(self.MIN_TRACK_HEIGHT, int(new_height))
        self._reflow_layout(redraw_waveform=False)

    def _on_waveform_height_committed(self, new_height: int):
        """Drag finished — apply final height and trigger waveform redraw."""
        self.waveform_track_height = max(self.MIN_TRACK_HEIGHT, int(new_height))
        self._reflow_layout(redraw_waveform=True)
        self.logger.info(f"Waveform track height set to {self.waveform_track_height}px")
        self.track_heights_changed.emit(self.waveform_track_height, self.midi_events_track_height)

    def _on_midi_events_height_preview(self, new_height: int):
        """Live update during MIDI events resize drag."""
        if new_height == self.midi_events_track_height:
            return
        self.midi_events_track_height = max(self.MIN_TRACK_HEIGHT, int(new_height))
        self._reflow_layout(redraw_waveform=False)

    def _on_midi_events_height_committed(self, new_height: int):
        """Drag finished for MIDI events — persist and reflow."""
        self.set_midi_events_height(new_height)

    def set_midi_events_height(self, height: int):
        """Set the shared fixture-track height, reflow, and persist. Used by
        both the resize-handle commit and the toolbar's S/M/L presets."""
        self.midi_events_track_height = max(self.MIN_TRACK_HEIGHT, int(height))
        self._reflow_layout(redraw_waveform=False)
        self.logger.info(f"MIDI events track height set to {self.midi_events_track_height}px")
        self.track_heights_changed.emit(self.waveform_track_height, self.midi_events_track_height)

    def viewport_height_hint(self) -> int:
        """Height the timeline widget needs so the content view shows the whole
        scene without a vertical scrollbar (content is always wider than the
        view, so the horizontal scrollbar is accounted for)."""
        return (self.scene_height
                + self.content_view.horizontalScrollBar().sizeHint().height()
                + 2 * self.content_view.frameWidth())

    def reflow_tracks(self):
        """Restack every track after a per-track height change (e.g. a fixture
        lane collapsing/expanding). Same layout pass as a resize, minus the
        waveform pixmap rebuild."""
        self._reflow_layout(redraw_waveform=False)

    def set_all_tracks_collapsed(self, collapsed: bool):
        """Collapse or expand every fixture lane at once, then restack.

        Drives the toolbar's Rows presets: 'S' collapses all lanes to a single
        header row; 'M'/'L' expand them (the height itself is set separately).
        View-only — collapse state isn't persisted."""
        for track in self.midi_events_tracks:
            track.set_collapsed(collapsed)
        self.reflow_tracks()

    def _reflow_layout(self, redraw_waveform: bool = False):
        """Reposition headers, content, and overlays after a height change."""
        # Recompute scene height and update both scene rects
        self._recompute_scene_height()
        self.header_scene.setSceneRect(0, 0, self.HEADER_WIDTH, self.scene_height)
        self.content_scene.setSceneRect(0, 0, self.content_width, self.scene_height)

        # Headers
        self.waveform_track.draw_header(self._waveform_y())
        for i, track in enumerate(self.midi_events_tracks):
            track.draw_header(self._midi_track_y(i))

        # Combined header panel follows the (possibly resized) waveform band.
        self._layout_master_header()

        # MIDI events content (cheap to redraw — keeps box heights in sync)
        if self._cached_events:
            self._applying_selection = True
            try:
                for i, track in enumerate(self.midi_events_tracks):
                    track.redraw_events(self._cached_events, self._midi_track_y(i))
            finally:
                self._applying_selection = False
            self._reapply_selection()

        # Waveform — only do the expensive pixmap rebuild on commit
        if redraw_waveform and self.audio_file:
            self.waveform_track.draw_waveform(self.audio_file, self._waveform_y())
        elif self.waveform_track.waveform_item is not None:
            # Keep existing pixmap visible at the new track top while dragging
            self.waveform_track.waveform_item.setYPos(self._waveform_y())

        # Song-structure overlay follows the (possibly resized) waveform band.
        self._draw_song_sections()

        # Playhead height + grid lines span the full scene
        if hasattr(self, 'playhead') and self.playhead is not None:
            self.playhead.setLine(0, 0, 0, self.scene_height)
        self.draw_time_grid()

        # Move resize handles to their new positions
        self._reposition_resize_handles()

    def draw_playhead(self):
        """Draw the playhead line."""
        pen = QPen(QColor(255, 0, 0, 200), 2)
        pen.setCosmetic(True)
        self.playhead = QGraphicsLineItem(0, 0, 0, self.scene_height)
        self.playhead.setPen(pen)
        self.playhead.setZValue(10)
        self.content_scene.addItem(self.playhead)

    def update_playhead(self, time_seconds):
        """Update playhead position."""
        x_pos = time_seconds * self.PIXELS_PER_SECOND
        self.playhead.setX(x_pos)

    def draw_time_grid(self):
        """Draw the background grid lines, following the active ruler mode.

        Bars mode: `beat_division` lines per bar, brighter on bar boundaries.
        Time mode: a line per second, brighter on the ruler's major interval
        (every 5 s) so the grid aligns with the m:ss ticks above it.
        """
        for item in self.time_grid_items:
            self.content_scene.removeItem(item)
        self.time_grid_items.clear()

        if self.ruler_mode == "time":
            step_px = self.PIXELS_PER_SECOND
            major_every = self.ruler_track.TIME_MAJOR_INTERVAL
        else:
            seconds_per_bar = (60.0 / self.bpm) * 4
            step_px = (seconds_per_bar / self.beat_division) * self.PIXELS_PER_SECOND
            major_every = self.beat_division

        if step_px == 0:
            return
        count = int(self.content_width / step_px)

        for i in range(count):
            x_pos = i * step_px
            line = QGraphicsLineItem(x_pos, self.RULER_TRACK_HEIGHT, x_pos, self.scene_height)
            if i % major_every == 0:
                grid_pen = QPen(QColor(80, 80, 80), 1.5)
            else:
                grid_pen = QPen(QColor(60, 60, 60), 0.8)
            grid_pen.setCosmetic(True)
            line.setPen(grid_pen)
            line.setZValue(-1)  # Keep grid lines in background
            self.content_scene.addItem(line)
            self.time_grid_items.append(line)

    def clip_tooltip(self) -> CustomTooltip:
        """Return the single shared clip tooltip, creating it on first use.

        Reused by every clip so its lifetime is decoupled from the transient
        clip items (which are destroyed and rebuilt on every redraw).
        """
        if self._clip_tooltip is None:
            self._clip_tooltip = CustomTooltip(self.window())
        return self._clip_tooltip

    def hide_clip_tooltip(self) -> None:
        """Hide the shared clip tooltip if it exists (no-op otherwise)."""
        if self._clip_tooltip is not None:
            self._clip_tooltip.hide_tooltip()

    def refresh_from_manager(self, detect_bpm: bool = False) -> None:
        """Refresh timeline display from MidiManager data.

        BPM is auto-detected from the MIDI tempo only when ``detect_bpm`` is
        True (i.e. a file was just loaded). Edit-driven refreshes leave it
        False so a manually-set BPM survives moves/resizes/deletes — the
        loaded tempo meta is stale once the user overrides BPM in the toolbar.
        """
        if not self.midi_manager:
            return

        # Get current data from MidiManager
        self._cached_events = self.midi_manager.get_events_for_ui()

        # Redraw tracks with cached data. Guard selectionChanged while the old
        # (possibly selected) items are torn down — removing a selected item
        # fires selectionChanged, which would otherwise wipe the persisted id
        # set before we can restore it below.
        self._applying_selection = True
        try:
            for i, track in enumerate(self.midi_events_tracks):
                track.redraw_events(self._cached_events, self._midi_track_y(i))
        finally:
            self._applying_selection = False

        # Restore the selection (the redraw rebuilt every clip from scratch).
        self._reapply_selection()

        # Sync each fixture's 'A' (auto) button to the controller, which
        # load_from_file / create_empty_track re-seed from the song's persisted
        # setup notes — so switching songs follows the saved auto state.
        self.refresh_auto_buttons()

        # Song-structure arrangement overlay on the waveform.
        self._draw_song_sections()

        # Detect BPM from first event if available (load-time only).
        if detect_bpm and self._cached_events:
            # Try to get BPM from MIDI data
            duration = self.midi_manager.get_duration()
            if duration > 0:
                self._detect_bpm_from_data()

    def _schedule_refresh(self) -> None:
        """Request a deferred refresh on the next event-loop tick.

        Multiple calls before the tick coalesce into a single refresh.
        Use this instead of refresh_from_manager() whenever the call
        originates from a QGraphicsItem event handler (mouse release,
        double-click, context-menu action, drop, etc.) to avoid
        destroying scene items while Qt is still processing events on
        them — which causes use-after-free SIGSEGV crashes.
        """
        if not self._refresh_pending:
            self._refresh_pending = True
            QTimer.singleShot(0, self._do_deferred_refresh)

    def _do_deferred_refresh(self) -> None:
        self._refresh_pending = False
        self.refresh_from_manager()

    # --- Multi-selection --------------------------------------------------

    def selected_clip_items(self):
        """All currently-selected non-BLACKOUT clip items in the scene."""
        if self.content_scene is None:
            return []
        return [it for it in self.content_scene.selectedItems()
                if isinstance(it, DraggableMidiEvent) and not it.is_blackout_clip]

    def _expanded_selected_event_ids(self):
        """Selected clips expanded to underlying event ids (scene members too)."""
        ids = set()
        for clip in self.selected_clip_items():
            if clip.group_event_ids:
                ids.update(clip.group_event_ids)
            else:
                ids.add(clip.event_id)
        return list(ids)

    def _on_selection_changed(self):
        """Keep the persisted id set + multi-select affordances in sync."""
        if self._applying_selection or not hasattr(self, 'midi_events_tracks'):
            return
        clips = self.selected_clip_items()
        self._selected_event_ids = {c.event_id for c in clips}
        multi = len(clips) >= 2
        for track in self.midi_events_tracks:
            for item in track.items:
                if isinstance(item, DraggableMidiEvent) and not item.is_blackout_clip:
                    item.set_multi_selected(multi and item.isSelected())

    def _reapply_selection(self):
        """Re-select clips by id after a redraw rebuilt every clip item."""
        if not self._selected_event_ids:
            return
        self._applying_selection = True
        try:
            restored = []
            for track in self.midi_events_tracks:
                for item in track.items:
                    if (isinstance(item, DraggableMidiEvent)
                            and not item.is_blackout_clip
                            and item.event_id in self._selected_event_ids):
                        item.setSelected(True)
                        restored.append(item)
            # Drop ids that no longer exist (merged / deleted clips).
            self._selected_event_ids = {it.event_id for it in restored}
            multi = len(restored) >= 2
            for it in restored:
                it.set_multi_selected(multi)
        finally:
            self._applying_selection = False

    def selection_move_bounds_px(self):
        """(min_dx, max_dx) in scene pixels for moving the current selection."""
        ids = self._expanded_selected_event_ids()
        if not self.midi_manager or not ids:
            return (0.0, 0.0)
        lo, hi = self.midi_manager.selection_move_bounds(ids)
        return (lo * self.PIXELS_PER_SECOND, hi * self.PIXELS_PER_SECOND)

    def on_selection_moved(self, delta_sec: float):
        """Group-move drag committed — shift the whole selection by delta."""
        if not self.midi_manager:
            return
        ids = self._expanded_selected_event_ids()
        if not ids:
            return
        if self.midi_manager.move_selection(ids, delta_sec):
            self._schedule_refresh()

    def delete_selected(self):
        """Delete every selected clip behind a single confirmation dialog."""
        if not self.midi_manager:
            return
        clips = self.selected_clip_items()
        if not clips:
            return
        ids = [c.event_id for c in clips]
        count = len(clips)
        noun = "event" if count == 1 else "events"
        reply = QMessageBox.question(
            None, "Delete Events",
            f"Replace {count} selected {noun} with blackouts?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._selected_event_ids = set()
        if self.midi_manager.delete_selection(ids):
            self._schedule_refresh()

    def copy_selected(self):
        """Copy the current clip selection into the clip clipboard."""
        if not self.midi_manager:
            return
        ids = self._expanded_selected_event_ids()
        if not ids:
            return
        self.midi_manager.copy_selection(ids)

    def paste_at_playhead(self):
        """Paste clipboard clips starting at the current playhead time."""
        if not self.midi_manager or not self.midi_manager.has_clipboard():
            return
        if not hasattr(self, 'playhead') or self.playhead is None:
            return
        time_seconds = max(0.0, self.playhead.x() / self.PIXELS_PER_SECOND)
        if self.midi_manager.paste(time_seconds):
            self._schedule_refresh()

    def _seek_to_scene_x(self, scene_x: float):
        """Move the playhead + request a seek for a plain empty-lane click."""
        time_seconds = max(0.0, scene_x / self.PIXELS_PER_SECOND)
        self.update_playhead(time_seconds)
        self.seek_requested.emit(time_seconds)

    def _detect_bpm_from_data(self) -> None:
        """Detect BPM from MIDI data structure."""
        if not self.midi_manager:
            return
        bpm = self.midi_manager.detect_bpm()
        if bpm is not None:
            self.bpm_detected.emit(bpm)

    def set_bpm(self, bpm):
        """Update BPM and redraw ruler and time grid to match new tempo."""
        if self.bpm != bpm:
            self.bpm = bpm
            self.ruler_track.redraw()
            self.draw_time_grid()
            self.logger.info(f"Timeline BPM updated to {bpm}")

    def set_voice_cues(self, cues):
        """Install the current song's voice cues [(time_sec, text)] and redraw
        their markers on the ruler."""
        self.voice_cues = list(cues or [])
        self.ruler_track.redraw()

    def set_ruler_mode(self, mode):
        """Switch the ruler between musical bars and mm:ss clock time."""
        if mode not in ("bars", "time") or mode == self.ruler_mode:
            return
        self.ruler_mode = mode
        self.ruler_track.redraw()
        self.draw_time_grid()
        self.logger.info(f"Timeline ruler mode set to {mode}")

    def set_beat_division(self, divisions):
        """Set how many ruler/grid lines subdivide each bar in Bars mode."""
        if divisions <= 0 or divisions == self.beat_division:
            return
        self.beat_division = divisions
        self.ruler_track.redraw()
        self.draw_time_grid()
        self.logger.info(f"Timeline beat division set to {divisions} per bar")

    def set_snap_enabled(self, enabled):
        """Enable/disable snapping of clip drags to the grid and clip edges."""
        self.snap_enabled = bool(enabled)
        self.logger.info(f"Timeline snap {'enabled' if self.snap_enabled else 'disabled'}")

    def snap_interval_px(self):
        """Scene-px spacing of the finest visible grid line, for clip snapping.

        Mirrors what the ruler/grid draw so snapping lands on visible lines:
        one-second lines in Time mode, or bar/`beat_division` lines in Bars
        mode. Returns None when snapping is off or unavailable (callers then
        skip grid snapping).
        """
        if not self.snap_enabled:
            return None
        pps = self.PIXELS_PER_SECOND
        if pps <= 0:
            return None
        if self.ruler_mode == "time":
            spacing_sec = self.ruler_track.TIME_MINOR_INTERVAL
        else:
            if self.bpm <= 0:
                return None
            seconds_per_bar = (60.0 / self.bpm) * 4
            spacing_sec = seconds_per_bar / self.beat_division
        px = spacing_sec * pps
        return px if px > 0 else None

    def update_ruler_labels(self):
        """Update ruler labels based on zoom level."""
        self.ruler_track.update_ruler_labels()

    def update_midi_text_truncation(self):
        """Update text truncation for all MIDI event labels based on current zoom level."""
        if not self._cached_events:
            return

        zoom_factor = self.content_view.transform().m11()
        font_metrics = self.content_view.fontMetrics()
        sorted_events = sorted(self._cached_events, key=lambda x: x['time'])
        song_length = self.midi_manager.song_end_time() if self.midi_manager else 240
        extended_end = song_length * 1.05

        from PyQt6.QtWidgets import QGraphicsTextItem
        # Each track has its own filtered slice — so end-times must be
        # recomputed per track or the blinder text would inherit gaps from
        # ledbar neighbors (and vice-versa). Iterate over render units (which
        # collapse 7-event scene groups into a single visual clip) so a
        # group's text gets the full clip width, not the ~0 gap between two
        # group members at the same time.
        for track in self.midi_events_tracks:
            if not track.items:
                continue
            track_events = [e for e in sorted_events if track.matches_filter(e)]
            units = build_render_units(track_events)
            rep_id_to_end = {}
            for i, unit in enumerate(units):
                end = units[i + 1]["time"] if i + 1 < len(units) else extended_end
                rep_id_to_end[unit["representative"]["id"]] = (unit["time"], end)

            for item in track.items:
                if not isinstance(item, QGraphicsTextItem):
                    continue
                full_text = item.data(0)
                if not full_text:
                    continue
                event_id = item.data(1)
                if event_id not in rep_id_to_end:
                    continue
                start, end = rep_id_to_end[event_id]
                duration = max(0.01, end - start)
                width = max(duration * self.PIXELS_PER_SECOND, 20)
                available_width = (width - 10) * zoom_factor
                item.setPlainText(self.get_truncated_text(full_text, available_width, font_metrics))

    def get_truncated_text(self, text, available_width, font_metrics):
        """Return text truncated to fit within available width, with ellipsis if needed."""
        if available_width <= 0:
            return ""
            
        # If the text fits entirely, return it as-is
        text_width = font_metrics.horizontalAdvance(text)
        if text_width <= available_width:
            return text
            
        # Calculate available width for ellipsis (3 dots)
        ellipsis = "..."
        ellipsis_width = font_metrics.horizontalAdvance(ellipsis)
        available_for_text = available_width - ellipsis_width
        
        if available_for_text <= 0:
            return ellipsis
            
        # Binary search to find the maximum characters that fit
        left, right = 0, len(text)
        best_length = 0
        
        while left <= right:
            mid = (left + right) // 2
            substring = text[:mid]
            substring_width = font_metrics.horizontalAdvance(substring)
            
            if substring_width <= available_for_text:
                best_length = mid
                left = mid + 1
            else:
                right = mid - 1
                
        return text[:best_length] + ellipsis if best_length > 0 else ellipsis

    # Event handlers
    def on_mute_toggled(self, checked):
        self.metronome_mute_toggled.emit(checked)

    def on_audio_mute_toggled(self, checked):
        self.audio_mute_toggled.emit(checked)

    def on_voice_mute_toggled(self, checked):
        self.voice_mute_toggled.emit(checked)

    # MIDI event management methods (event-id based)
    def _find_cached_event(self, event_id: str):
        """Look up a cached event by id."""
        for e in self._cached_events:
            if e.get('id') == event_id:
                return e
        return None

    def _group_id_for_event(self, event_id: str):
        """Return the scene group_id for an event_id, or None for singles."""
        evt = self._find_cached_event(event_id)
        return evt.get('group_id') if evt else None

    def refresh_auto_buttons(self) -> None:
        """Re-sync every fixture track's header toggles (auto/mute/lock) from the
        controller + document."""
        for track in getattr(self, 'midi_events_tracks', []):
            track.sync_header_state()

    def _draw_song_sections(self) -> None:
        """Redraw the song-part arrangement overlay on the waveform band."""
        if not self.midi_manager:
            return
        song_parts = [e for e in self._cached_events
                      if self.midi_manager.is_song_part(e)]
        self.waveform_track.draw_song_sections(song_parts, self._waveform_y())

    def on_song_section_moved(self, event_id: str, new_time: float):
        """A song-section box MOVE committed. A song part is a point-in-time
        global marker (no BLACKOUT extent machinery), so set its time directly
        and let the overlay recompute contiguous spans by sorted time."""
        if not self.midi_manager or not event_id:
            return
        self.midi_manager.update_event_timing(event_id, new_time)
        self._schedule_refresh()

    def on_song_section_delete(self, event_id: str):
        """Delete a song-section marker outright (no blackout conversion — that
        is fixture-lane behaviour; song parts are a global arrangement)."""
        if not self.midi_manager or not event_id:
            return
        event_data = self._find_cached_event(event_id)
        part = (getattr(event_data.get('info'), 'part', None)
                if event_data else None) or "section"
        reply = QMessageBox.question(
            None, "Delete Song Section",
            f"Delete the '{part}' section marker?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if self.midi_manager.delete_event(event_id):
            self._schedule_refresh()

    def on_midi_event_moved(self, event_id: str, new_time: float):
        """Whole-clip MOVE drag committed.

        Routes to scene API when the event is part of a scene group so that
        all 7 channel-control events move atomically. Always refreshes — even
        on rejected commits — so the clip snaps back to its data-derived
        position instead of leaving a ghost preview at the dragged x.
        """
        if not self.midi_manager or not event_id:
            return
        group_id = self._group_id_for_event(event_id)
        if group_id:
            self.midi_manager.move_scene(group_id, new_time)
        else:
            self.midi_manager.move_event(event_id, new_time)
        self._schedule_refresh()

    def on_midi_event_left_edge(self, event_id: str, new_time: float):
        """Left-edge resize drag committed.

        Always refreshes (see on_midi_event_moved for rationale).
        """
        if not self.midi_manager or not event_id:
            return
        group_id = self._group_id_for_event(event_id)
        if group_id:
            self.midi_manager.set_scene_left_edge(group_id, new_time)
        else:
            self.midi_manager.set_event_left_edge(event_id, new_time)
        self._schedule_refresh()

    def on_midi_event_right_edge(self, event_id: str, new_right_time: float):
        """Right-edge resize drag committed.

        Always refreshes (see on_midi_event_moved for rationale).
        """
        if not self.midi_manager or not event_id:
            return
        group_id = self._group_id_for_event(event_id)
        if group_id:
            self.midi_manager.set_scene_right_edge(group_id, new_right_time)
        else:
            self.midi_manager.set_event_right_edge(event_id, new_right_time)
        self._schedule_refresh()

    def on_midi_event_fade_in(self, event_id: str, seconds: float):
        """Fade-IN handle drag committed.

        Routes to scene API for grouped clips so all members carry a uniform
        fade. The manager's setters emit data_changed which already drives
        the redraw — no explicit refresh_from_manager() needed here.
        """
        if not self.midi_manager or not event_id:
            return
        group_id = self._group_id_for_event(event_id)
        if group_id:
            self.midi_manager.set_scene_fade_in(group_id, seconds)
        else:
            self.midi_manager.set_event_fade_in(event_id, seconds)

    def on_midi_event_fade_out(self, event_id: str, seconds: float):
        """Fade-OUT handle drag committed. See on_midi_event_fade_in."""
        if not self.midi_manager or not event_id:
            return
        group_id = self._group_id_for_event(event_id)
        if group_id:
            self.midi_manager.set_scene_fade_out(group_id, seconds)
        else:
            self.midi_manager.set_event_fade_out(event_id, seconds)

    def on_midi_event_edit_by_id(self, event_id: str):
        """Edit dialog request for the event with `event_id`.

        Routes to a scene-specific dialog when the event belongs to a
        scene group, otherwise falls back to the per-event dialog.
        """
        if not self.midi_manager or not event_id:
            return
        event_data = self._find_cached_event(event_id)
        if not event_data:
            return

        group_id = event_data.get('group_id')
        if group_id:
            self._edit_scene(group_id)
            return

        if int(event_data.get('note', -1)) == START_NOTE:
            dialog = StartEventEditDialog(event_data)
        else:
            dialog = MidiEventEditDialog(event_data, self.midi_manager.midi_note_map)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_values = dialog.get_values()
            if self.midi_manager.edit_event(event_id, **new_values):
                self._schedule_refresh()
                self.logger.info(
                    f"MIDI event edited: note={new_values['note']}, "
                    f"time={new_values['time']:.3f}s, velocity={new_values['velocity']}"
                )

    def _edit_scene(self, group_id: str):
        """Open the scene editing dialog for the group with `group_id`."""
        members = [e for e in self._cached_events
                   if e.get('group_id') == group_id]
        if not members:
            return
        # Reconstruct channels dict from per-note velocities (controller's
        # convention: channel value = velocity * 2). Default to 0 for any
        # missing channels (partial groups).
        velocity_by_note = {m['note']: m['velocity'] for m in members}
        field_note = self.midi_manager.channel_note_map(members[0].get('channel'))
        channels = {ch_name: min(255, velocity_by_note.get(note, 0) * 2)
                    for ch_name, note in field_note}
        current_name = (members[0].get('scene_name')
                        or f"Scene @ {members[0]['time']:.2f}s")

        dialog = SceneEditDialog(current_name, channels)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.get_values()
            if self.midi_manager.update_scene(group_id,
                                              channels=values['channels'],
                                              name=values['name']):
                self._schedule_refresh()
                self.logger.info(
                    f"Scene '{values['name']}' updated (group_id={group_id})"
                )

    def on_midi_event_delete_by_id(self, event_id: str):
        """Delete request for the event with `event_id`.

        BLACKOUT clips are removed outright (the user is clearing a gap).
        Regular clips are converted into a fixture-matching BLACKOUT instead
        of being deleted, so the time slot remains accounted for and adjacent
        clips don't visually expand into the freed space.
        """
        if not self.midi_manager or not event_id:
            return
        event_data = self._find_cached_event(event_id)
        if not event_data:
            return

        group_id = event_data.get('group_id')
        if group_id:
            scene_label = (event_data.get('scene_name')
                           or f"scene at {event_data['time']:.2f}s")
            title = "Delete Scene"
            prompt = (f"Replace the scene '{scene_label}' "
                      f"with a blackout? (All 7 channel events removed.)")
        else:
            info = event_data.get('info', None)
            if info and hasattr(info, 'part'):
                event_name = info.part
            else:
                event_name = f"Note {event_data['note']}"
            is_blackout = self.midi_manager.is_blackout(event_data)
            if is_blackout:
                title = "Remove Blackout"
                prompt = (f"Remove the blackout '{event_name}' "
                          f"at {event_data['time']:.2f}s?")
            else:
                title = "Delete MIDI Event"
                prompt = (f"Replace the MIDI event '{event_name}' at "
                          f"{event_data['time']:.2f}s with a blackout?")

        reply = QMessageBox.question(
            None, title, prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if group_id:
            ok = self.midi_manager.delete_scene(group_id)
        elif self.midi_manager.is_blackout(event_data):
            ok = self.midi_manager.delete_event(event_id)
        else:
            ok = self.midi_manager.replace_with_blackout(event_id)
        if ok:
            self._schedule_refresh()

    def fixture_at_y(self, scene_y: float):
        """Return the MIDI channel of the fixture whose lane owns `scene_y`, or
        None.

        Used by clip-library drops to validate that a dropped note targets a
        matching fixture's track and to stamp the new event's MIDI channel.
        Lanes are laid out by display row (DMX order); the returned value is the
        fixture's stable channel, not its row.
        """
        for row, track in enumerate(self.midi_events_tracks):
            top = self._midi_track_y(row)
            if top <= scene_y < top + track.get_track_height():
                return track.fixture_index
        return None

    def _fixture_for_channel(self, channel):
        """The patched fixture instance with stable `channel`, or None.

        Falls back to positional lookup for synthetic fixtures built without a
        stable channel (the display row doubles as the channel there)."""
        if channel is None:
            return None
        for inst in self.fixtures:
            if getattr(inst, "channel", -1) == channel:
                return inst
        if 0 <= channel < len(self.fixtures):
            inst = self.fixtures[channel]
            if getattr(inst, "channel", -1) < 0:
                return inst
        return None

    def _fixture_type(self, channel):
        """Type name of the patched fixture with stable `channel`, or None."""
        inst = self._fixture_for_channel(channel)
        return inst.type if inst else None

    def _fdef(self, channel):
        """FixtureDef of the patched fixture with stable `channel`, or None."""
        inst = self._fixture_for_channel(channel)
        if inst is None:
            return None
        return resolve_fixture(inst.type, getattr(inst, "mode", ""))

    def _note_is_song_part(self, note: int) -> bool:
        return bool(self.midi_manager
                    and self.midi_manager.is_song_part({"note": int(note)}))

    def can_accept_clip_drop(self, note: int, scene_y: float) -> bool:
        """Return True if a clip carrying `note` may land at `scene_y`.

        A note is droppable on a fixture lane when it is a meaningful verb for
        that fixture (channel offsets within range, scene/bank/chase verbs when
        the fixture defines them, global/utility verbs anywhere). Song parts
        drop onto the waveform band, where they form the arrangement overlay."""
        if not self.midi_manager:
            return False
        if self._note_is_song_part(note):
            return self._in_waveform_band(scene_y)
        target_idx = self.fixture_at_y(scene_y)
        if target_idx is None:
            return False
        # A locked track rejects new clips.
        if self.midi_manager.is_fixture_locked(target_idx):
            return False
        return note_layout.note_applies_to(note, self._fdef(target_idx))

    def handle_clip_drop(self, note: int, velocity: int, scene_x: float, scene_y: float) -> bool:
        """Validate and insert a dropped clip. Returns True on success."""
        if not self.can_accept_clip_drop(note, scene_y):
            return False
        time_seconds = max(0.0, scene_x / self.PIXELS_PER_SECOND)
        clamped_velocity = max(1, min(127, int(velocity)))
        # Song parts stay global (channel=None) and land on the waveform
        # arrangement overlay; fixture notes land on the exact fixture instance
        # they were dropped on.
        is_song_part = self._note_is_song_part(note)
        target_idx = None if is_song_part else self.fixture_at_y(scene_y)
        channel = None if is_song_part else target_idx
        event_id = self.midi_manager.add_event(note, time_seconds,
                                               clamped_velocity, channel)
        if not event_id:
            return False
        self._schedule_refresh()
        where = "arrangement" if is_song_part else f"fixture #{target_idx}"
        self.logger.info(
            f"Clip dropped: note={note} at {time_seconds:.3f}s on {where}"
        )
        return True

    def can_accept_scene_drop(self, scene_y: float,
                              fixture_type: Optional[str] = None) -> bool:
        """A scene targets a track whose fixture defines scene banks, and whose
        type matches the scene's owning fixture (when the payload names one)."""
        if not self.midi_manager:
            return False
        target_idx = self.fixture_at_y(scene_y)
        # A locked track rejects new clips.
        if target_idx is not None and self.midi_manager.is_fixture_locked(target_idx):
            return False
        target_type = self._fixture_type(target_idx)
        fdef = self._fdef(target_idx)
        if not (fdef and fdef.banks):
            return False
        return fixture_type is None or fixture_type == target_type

    def handle_scene_drop(self, name: str, channels: dict,
                          scene_x: float, scene_y: float,
                          fixture_type: Optional[str] = None) -> bool:
        """Validate and insert a dropped scene. Returns True on success."""
        if not self.can_accept_scene_drop(scene_y, fixture_type):
            return False
        time_seconds = max(0.0, scene_x / self.PIXELS_PER_SECOND)
        target_idx = self.fixture_at_y(scene_y)
        group_id = self.midi_manager.add_scene(time_seconds, channels, name,
                                               target_idx)
        if not group_id:
            return False
        self._schedule_refresh()
        self.logger.info(
            f"Scene dropped: '{name}' at {time_seconds:.3f}s on fixture "
            f"#{target_idx} (group_id={group_id})"
        )
        return True

    def on_add_event_requested(self, time_seconds, fixture_index=None):
        """Handle request to add a new MIDI event at the specified time.

        `fixture_index` (the lane the request came from) targets that exact
        fixture; song parts ignore it and stay global.
        """
        if not self.midi_manager:
            return

        # Create event data with default values
        new_event_data = {
            'time': time_seconds,
            'velocity': 127,
            'note': None  # Will be set by dialog
        }

        dialog = MidiEventEditDialog(new_event_data, self.midi_manager.midi_note_map, is_new_event=True)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Get the new values from dialog
            new_values = dialog.get_values()

            # Route to the lane's fixture unless it's a global song part.
            channel = (None if self._note_is_song_part(new_values['note'])
                       else fixture_index)
            event_id = self.midi_manager.add_event(
                new_values['note'],
                new_values['time'],
                new_values['velocity'],
                channel,
            )

            if event_id:
                # Refresh the timeline display
                self._schedule_refresh()
                
                # Get part name for logging
                note_info = self.midi_manager.midi_note_map.get(str(new_values['note']))
                part_name = getattr(note_info, 'part', f'Note {new_values["note"]}') if note_info else f'Note {new_values["note"]}'
                self.logger.info(f"MIDI event added: {part_name} at {new_values['time']:.3f}s, velocity={new_values['velocity']}")
            else:
                self.logger.error(f"Failed to add MIDI event: note {new_values['note']} not in note map")

    # Legacy compatibility methods
    def set_files(self, audio_file, midi_file=None):
        """Set audio file. MIDI files should be loaded via MidiManager."""
        self.audio_file = audio_file

        # Clear existing items
        self.midi_event_items.clear()

        # Redraw waveform if audio file provided
        if audio_file:
            self.waveform_track.draw_waveform(audio_file, self._waveform_y())

        QTimer.singleShot(0, self.fit_to_width)
    
    def set_waveform_offset(self, offset_sec: float) -> None:
        """Set the waveform offset position."""
        if self.waveform_track.waveform_item:
            x_pos = offset_sec * self.PIXELS_PER_SECOND
            self.waveform_track.waveform_item.setX(x_pos)
            self.logger.debug(f"Waveform offset set to {offset_sec:.3f}s")

    def fit_to_width(self):
        """Fit timeline to view width."""
        view_width = self.content_view.viewport().width()
        scene_width = self.content_scene.width()

        if scene_width <= 0 or view_width <= 0:
            return

        min_scale = view_width / scene_width
        self.content_view.resetTransform()
        self.content_view.scale(min_scale, 1)
        self.update_ruler_labels()
        self.update_midi_text_truncation()
        self.emit_zoom_changed()

    def set_zoom(self, factor: float):
        """Set an absolute horizontal zoom factor on the timeline view."""
        if factor <= 0:
            return
        self.content_view.resetTransform()
        self.content_view.scale(factor, 1)
        self.update_ruler_labels()
        self.update_midi_text_truncation()
        self.emit_zoom_changed()

    def emit_zoom_changed(self):
        """Report the current horizontal zoom factor (m11) to listeners.

        Called after any zoom change — Fit, the toolbar slider, or Ctrl+wheel
        (from ContentView) — so the toolbar slider can track the live zoom.
        """
        self.zoom_changed.emit(self.content_view.transform().m11())

    def get_modified_midi_data(self):
        """Get the modified MIDI data from MidiManager."""
        if self.midi_manager and self.midi_manager.midi_data:
            return self.midi_manager.midi_data
        return None

    def get_time_from_pos(self, x_pos):
        """Convert X position to time in seconds."""
        scene_x = self.content_view.mapToScene(x_pos, 0).x()
        return scene_x / self.PIXELS_PER_SECOND if self.PIXELS_PER_SECOND > 0 else 0

    def mousePressEvent(self, event):
        """Handle mouse press for seeking."""
        if event.button() == Qt.MouseButton.LeftButton:
            view_pos = self.content_view.mapFrom(self, event.pos())
            if self.content_view.rect().contains(view_pos):
                time_seconds = self.get_time_from_pos(view_pos.x())
                self.update_playhead(time_seconds)
                self.seek_requested.emit(time_seconds)
        else:
            super().mousePressEvent(event)

    def cleanup(self):
        """Cleanup all tracks when timeline is destroyed."""
        if (self._master_header_proxy is not None
                and self._master_header_proxy.scene()):
            self.header_scene.removeItem(self._master_header_proxy)
        if hasattr(self, 'ruler_track'):
            self.ruler_track.cleanup()
        if hasattr(self, 'waveform_track'):
            self.waveform_track.clear_items()
        for track in getattr(self, 'midi_events_tracks', []):
            track.clear_items()