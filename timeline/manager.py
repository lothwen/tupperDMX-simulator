"""
Timeline manager for TupperDMX Simulator.

Coordinates multiple timeline tracks and provides the main timeline interface.

`Timeline` keeps the scenes, the lane layout, redraws, zoom and the song's
extent. The rest of it lives beside it:

- `clip_commands` — what editing a clip does (drags, the editor, deletes, drops)
- `selection` — the multi-selection and the clip clipboard
- `lane_reorder` — carrying a lane to a new row by its header grip
- `master_header` — the TRACKS counter and the three bus volume rows
- `lanes` — Qt-free patch lookups (labels, row order, channel -> fixture)
"""

from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QGraphicsView,
                             QGraphicsScene, QSizePolicy, QGraphicsLineItem,
                             QGraphicsRectItem, QGraphicsProxyWidget)
from PyQt6.QtGui import QBrush, QColor, QPen
from PyQt6.QtCore import Qt, pyqtSignal, QTimer

from .widgets import (ContentView, TrackResizeHandle,
                      HeaderResizeHandle, CustomTooltip)
from core.tempo_map import TempoMap
from .tracks.clip_geometry import uniform_grid
from . import lanes
from .clip_commands import ClipCommandsMixin
from .lane_reorder import LaneReorder
from .master_header import MasterHeaderPanel
from .selection import SelectionMixin
from .tracks import WaveformTrack, MidiEventsTrack, RulerTrack
from .tracks.base import elided_label
from .tracks.midi_events import AutoSceneClip, DraggableMidiEvent
from tupperlogger import get_logger


class Timeline(ClipCommandsMixin, SelectionMixin, QWidget):
    """Main timeline widget coordinating multiple tracks."""
    
    # Signals
    seek_requested = pyqtSignal(float)
    # A song section's "Play from" menu: seek to this time, then play.
    play_from_requested = pyqtSignal(float)
    bpm_detected = pyqtSignal(int)
    # Live preview during a waveform drag (and on a programmatic set).
    waveform_offset_changed = pyqtSignal(float)
    # Drag finished: (offset the drag started at, offset it ended at).
    waveform_offset_committed = pyqtSignal(float, float)
    metronome_volume_changed = pyqtSignal(int)
    metronome_mute_toggled = pyqtSignal(bool)
    audio_volume_changed = pyqtSignal(int)
    audio_mute_toggled = pyqtSignal(bool)
    voice_volume_changed = pyqtSignal(int)
    voice_mute_toggled = pyqtSignal(bool)
    midi_data_changed = pyqtSignal()  # Emitted when MIDI data is modified
    track_heights_changed = pyqtSignal(int, int)  # waveform_height, midi_events_height (on commit)
    zoom_changed = pyqtSignal(float)  # current horizontal zoom factor (m11)
    # The clip editor wrote a fixture's scene banks, so anything showing the
    # library (the clip-library panel) is now stale.
    library_changed = pyqtSignal()
    # Audition a clip on the real rig: [(channel, note, velocity), ...], the
    # same payload the clip library previews with.
    preview_requested = pyqtSignal(list)
    # A ruler marker was dragged to a new time: (kind, old_time, new_time).
    # The ruler draws tuples, not the song's marker objects, so the old time is
    # how the window finds the one to retime (core.markers.marker_at).
    marker_move_committed = pyqtSignal(str, float, float)
    # A ruler marker was double-clicked: (kind, time) — open its editor on it.
    marker_edit_requested = pyqtSignal(str, float)
    # The header/content divider was released at a new header width, in pixels.
    header_width_changed = pyqtSignal(int)
    # A lane was dragged to a new row by its header grip: every fixture's MIDI
    # channel, top to bottom. Emitted on release, only when the order changed.
    track_order_changed = pyqtSignal(list)
    # A lane header was double-clicked (or its menu used): open that fixture's
    # editor. Carries the fixture's stable MIDI channel. The dialog lives in
    # `ui/`, which this package must not import — hence the signal.
    fixture_edit_requested = pyqtSignal(int)

    # Constants
    # HEADER_WIDTH is the designed width; __init__ shadows it with the
    # UI-scaled one, because a lane's name has to fit the strip it is drawn in
    # and this strip is painted, not styled — the stylesheet's scale rewrite
    # cannot reach it. Every reader goes through the instance, so shadowing is
    # all it takes.
    HEADER_WIDTH = 240
    # Bounds the divider drag clamps to. The floor is scaled like the designed
    # width above, because what it protects — the header's TRACKS/volume rows —
    # is styled and so grows with the interface (at 1.5 those rows already need
    # ~184px). The ceiling is a plain cap: past it the strip eats the clips it
    # exists to label, whatever the scale.
    MIN_HEADER_WIDTH = 170
    MAX_HEADER_WIDTH = 600
    # Two lanes, not one: a marker's flag and name ride the top strip
    # (RulerTrack.MARKER_LANE_H), the ticks and bar/time numbers the rest. A
    # bar number's ink is 16px tall and a marker's name chip 14 — they cannot
    # share a 25px band whatever order they are drawn in, and the ruler's
    # numbering is not the half that gives way.
    RULER_TRACK_HEIGHT = 40
    DEFAULT_WAVEFORM_TRACK_HEIGHT = 100
    DEFAULT_MIDI_EVENTS_TRACK_HEIGHT = 50
    MIN_TRACK_HEIGHT = 30
    # How much of a fixture's type colour reaches its lane background. Low on
    # purpose: the tint names the row, it does not compete with the clips on it.
    LANE_TINT_STRENGTH = 0.16
    SECONDS_TO_DISPLAY = 240
    PIXELS_PER_SECOND = 20

    def __init__(self, midi_manager=None, audio_file=None, initial_metronome_volume=0.5,
                 initial_audio_volume=0.5, initial_voice_volume=0.8,
                 initial_waveform_track_height=None, initial_midi_events_track_height=None,
                 initial_header_width=None,
                 fixtures=None, ui_scale=1.0, parent=None):
        super().__init__(parent)
        # The header width is baked into scene rects and painted items, so it
        # is measured here rather than styled; `set_ui_scale` re-measures it
        # when the scale changes under a running window.
        # (`timeline/` never imports `ui/`, hence the injected scale.)
        # A width dragged by hand is passed in instead and outranks the scale
        # from then on — see `set_header_width`.
        self._ui_scale = ui_scale
        self._header_width_user_set = bool(initial_header_width)
        self.HEADER_WIDTH = (self._clamp_header_width(initial_header_width)
                             if self._header_width_user_set
                             else round(self.HEADER_WIDTH * ui_scale))
        self.audio_file = audio_file
        self.midi_manager = midi_manager

        # How the waveform band gets its samples: set_audio_source() installs
        # path -> (duration, PCM). Kept as an injected callable because the
        # samples live in the audio engine, which this layer does not import.
        self.audio_source = None

        # Every audio track the current song carries (backing AND guide),
        # declared by set_song_tracks(). The timeline spans the longest of
        # them, not the one being monitored — see apply_song_extent().
        self.song_audio_files = []

        # Whether the sequencer is running, told by MainWindow. Gates a clip
        # dialog's live output — see set_transport_playing.
        self._transport_playing = False

        # The patch: one MIDI events track is built per fixture, in patch order
        # (a fixture's index == the MIDI channel that routes to it).
        self.fixtures = list(fixtures or [])

        self.bpm = 120  # the song's BASE tempo (the .mid's single set_tempo)
        # Where the bar lines actually fall: the base tempo plus the song's
        # tempo markers. The scene's x axis stays SECONDS, so a tempo change
        # moves the grid under the clips and never the clips themselves.
        self.tempo_map = TempoMap(self.bpm)
        self.voice_tts = []  # current song's [(time_sec, text)] for the ruler
        # Control markers for the ruler: [(time_sec, kind, label)]
        self.control_markers = []
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

        # The header-grip drag that carries a lane to another row. It owns
        # the gesture's whole state; the timeline only lends it the strip.
        self.lane_reorder = LaneReorder(self)

        # Resize handle items (created in _init_tracks)
        self.waveform_resize_handle = None
        self.midi_events_resize_handle = None

        # Combined header panel proxy (spans the ruler + waveform header band).
        self._master_header_proxy = None

        # Cache for current UI data (refreshed from MidiManager)
        self._cached_events = []

        # Multi-selection: event ids of selected clips, persisted across the
        # full item rebuild that every redraw performs so a selection survives
        # a move/delete (and can be nudged repeatedly). `_applying_selection`
        # guards the selectionChanged handler from recursing while we restore.
        self._selected_event_ids = set()
        self._applying_selection = False

        # Repeat-paste chain: (playhead time the chain was started from, time
        # the next paste goes to). Pasting again without touching the playhead
        # stacks the block after the previous one, so a copied section can be
        # looped by hitting Ctrl+V repeatedly. A playhead move, a fresh copy or
        # a clip delete breaks the chain and the next paste lands at the
        # playhead — a delete because the freed slot is where the operator
        # means to paste next, not one block further on.
        self._paste_chain_from = None
        self._paste_chain_next = 0.0

        # Single shared clip tooltip. Created lazily, reused by every clip.
        # A per-clip tooltip would be orphaned on screen when a redraw
        # destroys the hovered clip before its hoverLeaveEvent fires; one
        # shared widget that redraw_events hides cannot leave a ghost.
        self._clip_tooltip = None

        # Time grid items (managed centrally)
        self.time_grid_items = []

        # Fixture-lane zebra bands + separators (managed centrally)
        self.lane_band_items = []

        # Deferred-refresh guard: coalesces multiple refresh requests
        # (e.g. data_changed + explicit call) into a single repaint on
        # the next event-loop tick, preventing scene rebuilds while
        # QGraphicsItem event handlers are still on the call stack.
        self._refresh_pending = False

        # Logger for debug messages
        self.logger = get_logger("timeline.manager")

        # The head of the strip: the TRACKS counter over the three bus
        # volume rows. It owns those controls and reports through its own
        # signals, re-emitted here so listeners still see one timeline.
        self.master_header = MasterHeaderPanel(
            initial_audio_volume, initial_metronome_volume, initial_voice_volume,
            self.HEADER_WIDTH)
        for reported, own in (
                (self.master_header.audio_volume_changed, self.audio_volume_changed),
                (self.master_header.audio_mute_toggled, self.audio_mute_toggled),
                (self.master_header.metronome_volume_changed, self.metronome_volume_changed),
                (self.master_header.metronome_mute_toggled, self.metronome_mute_toggled),
                (self.master_header.voice_volume_changed, self.voice_volume_changed),
                (self.master_header.voice_mute_toggled, self.voice_mute_toggled)):
            reported.connect(own)
        self.master_header.set_track_count(len(self.fixtures))
        
        # Initialize UI
        self.init_ui()
        # Build the fixture tracks before the scenes so setup_scenes() can derive
        # the scene height from them (each track's height is per-instance now, so
        # the total is the sum of the real track objects, not fixture count).
        self._build_midi_event_tracks()
        self.setup_scenes()

        # Initialize tracks
        self._init_tracks()
        
    def set_volumes(self, audio=None, metronome=None, voice=None) -> None:
        """Move the header sliders to volumes that changed elsewhere (a live
        userprefs reload)."""
        self.master_header.set_volumes(audio, metronome, voice)

    def _layout_master_header(self) -> None:
        """Create/position the combined header panel so it spans the ruler and
        waveform header band as one block."""
        if self._master_header_proxy is None:
            self._master_header_proxy = QGraphicsProxyWidget()
            self._master_header_proxy.setWidget(self.master_header)
            # Above the per-track background rects, below the resize handle (z=6).
            self._master_header_proxy.setZValue(1)
        if self._master_header_proxy.scene() is None:
            self.header_scene.addItem(self._master_header_proxy)
        bottom = self._waveform_y() + self.waveform_track_height
        self.master_header.setFixedWidth(self.HEADER_WIDTH)
        self.master_header.setFixedHeight(int(bottom))
        self._master_header_proxy.setPos(0, 0)

    def init_ui(self):
        """Initialize the UI layout."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Tracks hang from the top. A QGraphicsView centres a scene smaller than
        # its viewport, which parked the lanes in the middle of the empty space
        # once the section grew tall (undocking the stage hands it the whole
        # window) — a timeline reads top-down, so anchor it there.
        top_left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop

        self.header_view = QGraphicsView(self)
        self.header_scene = QGraphicsScene(self)
        self.header_view.setScene(self.header_scene)
        self.header_view.setAlignment(top_left)
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
        self.content_view.setAlignment(top_left)
        # Track selection centrally so multi-select affordances and the
        # persisted id set stay in sync as clips are (de)selected.
        self.content_scene.selectionChanged.connect(self._on_selection_changed)

        # The divider between the two: drag it to re-measure the header strip.
        self.header_resize_handle = HeaderResizeHandle(lambda: self.HEADER_WIDTH, self)
        self.header_resize_handle.width_preview.connect(self._on_header_width_preview)
        self.header_resize_handle.width_committed.connect(self._on_header_width_committed)

        layout.addWidget(self.header_view)
        layout.addWidget(self.header_resize_handle)
        layout.addWidget(self.content_view)

        # Sync scrollbars
        self.content_view.verticalScrollBar().valueChanged.connect(self._on_content_scrolled)
        self.header_view.verticalScrollBar().valueChanged.connect(self.content_view.verticalScrollBar().setValue)

    def setup_scenes(self):
        """Setup the graphics scenes."""
        self.content_width = self.PIXELS_PER_SECOND * self.SECONDS_TO_DISPLAY

        self.header_scene.setBackgroundBrush(QColor(30, 30, 30))
        self.content_scene.setBackgroundBrush(QColor(30, 30, 30))

        self._recompute_scene_height()
        self.header_scene.setSceneRect(0, 0, self.HEADER_WIDTH, self.scene_height)
        self.content_scene.setSceneRect(0, 0, self.content_width, self.scene_height)
        self._sync_header_scroll_range()

        self.draw_playhead()
        self.draw_time_grid()
        self.draw_lane_bands()

    def _sync_header_scroll_range(self) -> None:
        """Size the header scene so the header view scrolls exactly as far as
        the content view.

        The content view carries a horizontal scrollbar and the header view has
        none, so the content viewport is that scrollbar shorter — and a view's
        scroll maximum is (scene - viewport). Left alone the header's maximum is
        short by exactly that strip, and once the header pins there it stops
        emitting valueChanged: the content then scrolls the last few pixels on
        its own and every lane separator drifts off the header border beside it.
        Topping the header scene up by the surplus keeps the two ranges equal
        (and makes the bottom of the last lane reachable).
        """
        current = self.header_scene.sceneRect().height()
        # Measured off the scrollbars themselves rather than off the viewport
        # heights: the content view carries a horizontal zoom, and its mapped
        # scene rect rounds a pixel or two differently from the header's.
        surplus = (self.content_view.verticalScrollBar().maximum()
                   - self.header_view.verticalScrollBar().maximum())
        height = max(self.scene_height, current + surplus)
        if abs(height - current) < 0.5:
            return
        self.header_scene.setSceneRect(0, 0, self.HEADER_WIDTH, height)

    def _on_content_scrolled(self, value: int) -> None:
        """Mirror the content view's vertical scroll onto the headers.

        The range is re-matched first: the viewports only take their final
        geometry once the widget is on screen and the horizontal scrollbar has
        settled, and a stale range is exactly what lets the two views drift.
        """
        bar = self.header_view.verticalScrollBar()
        self._sync_header_scroll_range()
        bar.setValue(value)
        if bar.value() != value:
            # The range was still stale (a view recomputes its scroll range
            # lazily after a scene resize, so the first scroll following a
            # reflow can read the old one). Now that the shortfall is visible
            # in the scrollbars, the top-up measures it exactly.
            self._sync_header_scroll_range()
            bar.setValue(value)

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

    def _build_midi_event_tracks(self):
        """(Re)build the MidiEventsTrack list from the current patch — objects
        only, no drawing. Tracks are laid out top-to-bottom by the row the user
        dragged them to (falling back to DMX address), but each binds to its
        fixture's stable MIDI channel (identity), so reordering/insert/remove
        never re-binds existing clips."""
        display = sorted(self.fixtures, key=lanes.display_key)
        labels = lanes.fixture_labels(display)

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

        Tracks are laid out top-to-bottom by the row they were dragged to, or
        by DMX address (presentation), but each track binds to its fixture's
        stable MIDI channel (identity), so reordering/insert/remove never
        re-binds existing clips."""
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
        self.master_header.set_track_count(len(self.fixtures))
        self._build_midi_event_tracks()
        self._create_midi_resize_handle()
        # Reposition headers/content, redraw clips onto the new lanes (by
        # channel), refresh grid/playhead, and place the handles.
        self._reflow_layout(redraw_waveform=False)

    def set_ui_scale(self, scale: float) -> None:
        """Re-measure the track headers at a new UI scale.

        The header width is *painted*, not styled — it is baked into scene
        rects and into items the theme's pixel rewrite cannot reach — so a
        restyle alone leaves the lane names in a strip sized for the old scale.
        Everything but the ruler's band redraws itself from `HEADER_WIDTH` on
        the reflow; that one is created once, so it is resized in place.

        A strip the user has dragged to a width of their own is left alone: the
        scale has no business overruling a size chosen by hand (the same rule
        the right sidebar's width follows). It is still re-clamped, since the
        rows it has to hold grew with the scale.
        """
        self._ui_scale = scale
        width = (self._clamp_header_width(self.HEADER_WIDTH)
                 if self._header_width_user_set
                 else round(Timeline.HEADER_WIDTH * scale))
        self._apply_header_width(width, redraw_waveform=bool(self.audio_file))

    def _clamp_header_width(self, width) -> int:
        """Hold a header width inside the bounds the divider drags between."""
        floor = round(self.MIN_HEADER_WIDTH * self._ui_scale)
        return max(floor, min(self.MAX_HEADER_WIDTH, int(width)))

    def set_header_width(self, width: int) -> None:
        """Re-measure the track headers at a width chosen by hand.

        Marks the strip as hand-sized, so a later UI-scale change leaves it be.
        """
        self._header_width_user_set = True
        self._apply_header_width(self._clamp_header_width(width),
                                 redraw_waveform=False)

    def _apply_header_width(self, width: int, redraw_waveform: bool) -> None:
        """Set the header strip's width and re-lay everything measured from it."""
        if width == self.HEADER_WIDTH:
            return
        self.HEADER_WIDTH = width
        self.header_view.setFixedWidth(width)
        self.ruler_track.resize_header(width)
        # The track-height grips span the strip and are created once, so they
        # are re-measured in place like the ruler's band.
        for handle in (self.waveform_resize_handle, self.midi_events_resize_handle):
            if handle is not None:
                handle.set_width(width)
        self._reflow_layout(redraw_waveform=redraw_waveform)

    def _on_header_width_preview(self, width: int) -> None:
        """Live update while the header divider is dragged (visual only)."""
        self.set_header_width(width)

    def _on_header_width_committed(self, width: int) -> None:
        """Drag finished — apply the final width and persist it."""
        self.set_header_width(width)
        self.logger.info(f"Timeline header width set to {self.HEADER_WIDTH}px")
        self.header_width_changed.emit(self.HEADER_WIDTH)

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

    # --- Lane reordering: the gesture lives in `lane_reorder`; these three
    # are what a track header calls -----------------------------------------

    def begin_track_reorder(self, track):
        """A header grip was pressed: start carrying that lane."""
        self.lane_reorder.begin(track)

    def update_track_reorder(self, viewport_y: float):
        """Carry the lifted lane to `viewport_y`, in header-viewport coords."""
        self.lane_reorder.update(viewport_y)

    def end_track_reorder(self):
        """The grip was released: settle the lane and announce the order."""
        self.lane_reorder.end()

    def _reflow_layout(self, redraw_waveform: bool = False):
        """Reposition headers, content, and overlays after a height change."""
        # Recompute scene height and update both scene rects
        self._recompute_scene_height()
        self.header_scene.setSceneRect(0, 0, self.HEADER_WIDTH, self.scene_height)
        self.content_scene.setSceneRect(0, 0, self.content_width, self.scene_height)
        self._sync_header_scroll_range()

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
            self.waveform_track.place_waveform(self._waveform_y())

        # Song-structure overlay follows the (possibly resized) waveform band.
        self._draw_song_sections()

        # Playhead height + grid lines span the full scene
        self.playhead.setLine(0, 0, 0, self.scene_height)
        self.draw_time_grid()
        self.draw_lane_bands()

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

    def _lane_tint(self, accent: str) -> QColor:
        """A fixture's accent colour mixed LANE_TINT_STRENGTH of the way into
        the scene background — so a lane keeps the timeline's darkness and only
        its hue says which fixture the row belongs to."""
        c = QColor(accent)
        bg = self.content_scene.backgroundBrush().color()
        return QColor(*[round(b + (a - b) * self.LANE_TINT_STRENGTH)
                        for a, b in ((c.red(), bg.red()),
                                     (c.green(), bg.green()),
                                     (c.blue(), bg.blue()))])

    def draw_lane_bands(self):
        """Draw the fixture lanes' backdrop: each lane tinted towards its
        fixture's type colour (the swatch on its header) plus a 1px separator at
        each lane boundary, so a clip's row is readable across the full width of
        the scene.

        Rebuilt from the current layout (heights are per-track and a collapsed
        lane is shorter), and drawn below the grid lines so the bars still read
        over the tint. The separator colour is the one each track header paints
        as its bottom border, so the line continues across the header/content
        seam.
        """
        for item in self.lane_band_items:
            self.content_scene.removeItem(item)
        self.lane_band_items.clear()

        no_pen = QPen(Qt.PenStyle.NoPen)
        sep_pen = QPen(QColor(68, 68, 68), 1)
        sep_pen.setCosmetic(True)

        for i, track in enumerate(self.midi_events_tracks):
            y = self._midi_track_y(i)
            band = QGraphicsRectItem(0, y, self.content_width,
                                     track.get_track_height())
            band.setBrush(QBrush(self._lane_tint(track._header_accent())))
            band.setPen(no_pen)
            band.setZValue(-3)  # under the grid lines (z=-1)
            self.content_scene.addItem(band)
            self.lane_band_items.append(band)
            # The separator sits on the lane boundary's LAST row (y - 0.5),
            # which is the row the header above it paints its own bottom border
            # on — so the line runs unbroken across the header/content seam.
            # The half-pixel lands the cosmetic 1px pen on a single device row
            # instead of antialiasing it across two.
            sep = QGraphicsLineItem(0, y - 0.5, self.content_width, y - 0.5)
            sep.setPen(sep_pen)
            sep.setZValue(-2)
            self.content_scene.addItem(sep)
            self.lane_band_items.append(sep)

    def draw_time_grid(self):
        """Draw the background grid lines, following the active ruler mode.

        Bars mode: `beat_division` lines per bar, brighter on bar boundaries.
        Time mode: a line per second, brighter on the ruler's major interval
        (every 5 s) so the grid aligns with the m:ss ticks above it.
        """
        for item in self.time_grid_items:
            self.content_scene.removeItem(item)
        self.time_grid_items.clear()

        # (x, is_major) for every line to draw. Time mode is an even walk;
        # Bars mode asks the tempo map, whose spacing changes at every marker.
        if self.ruler_mode == "time":
            step_px = self.PIXELS_PER_SECOND
            if step_px == 0:
                return
            major_every = self.ruler_track.TIME_MAJOR_INTERVAL
            lines = [(i * step_px, i % major_every == 0)
                     for i in range(int(self.content_width / step_px))]
        else:
            until = self.content_width / self.PIXELS_PER_SECOND
            lines = [(seconds * self.PIXELS_PER_SECOND, is_bar)
                     for seconds, _bar, is_bar
                     in self.tempo_map.grid_lines(until, self.beat_division)]

        for x_pos, is_major in lines:
            line = QGraphicsLineItem(x_pos, self.RULER_TRACK_HEIGHT, x_pos, self.scene_height)
            if is_major:
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

        # The sequence may now run past the audio (or the audio may have
        # changed under it): re-span the scene before anything is laid out.
        self.apply_song_extent()

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
        if (detect_bpm and self._cached_events
                and self.midi_manager.get_duration() > 0):
            bpm = self.midi_manager.detect_bpm()
            if bpm is not None:
                self.bpm_detected.emit(bpm)

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

    def _seek_to_scene_x(self, scene_x: float):
        """Move the playhead + request a seek for a plain empty-lane click."""
        time_seconds = max(0.0, scene_x / self.PIXELS_PER_SECOND)
        self.update_playhead(time_seconds)
        self.seek_requested.emit(time_seconds)

    def set_bpm(self, bpm):
        """Update BPM and redraw ruler and time grid to match new tempo."""
        if self.bpm != bpm:
            self.bpm = bpm
            self.set_tempo_map(TempoMap(bpm, self.tempo_map.changes()))
            self.logger.info(f"Timeline BPM updated to {bpm}")

    def set_voice_tts(self, cues):
        """Install the current song's voice cues [(time_sec, text)] and redraw
        their markers on the ruler."""
        self.voice_tts = list(cues or [])
        self.ruler_track.redraw()

    def set_control_markers(self, markers):
        """Install the current song's control markers for the ruler, as
        [(time_sec, kind, label)] — the tempo/stop-click/segue flags drawn
        beside the voice cues. Does NOT re-grid: the tempo map is the grid's
        source and arrives separately, already built from the same markers."""
        self.control_markers = list(markers or [])
        self.ruler_track.redraw()

    def set_tempo_map(self, tempo_map):
        """Install the song's tempo map and re-grid everything drawn from it:
        the ruler's bars, the scene grid lines and what clip drags snap to."""
        self.tempo_map = tempo_map
        self.bpm = tempo_map.bpm_at(0.0)
        self.ruler_track.redraw()
        self.draw_time_grid()

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

    def snap_grid_x(self):
        """A callable mapping a scene x to the nearest visible grid line, or
        None when snapping is off or unavailable (callers then skip it).

        Mirrors what the ruler/grid draw so snapping lands on visible lines:
        one-second lines in Time mode, or bar/`beat_division` lines in Bars
        mode. A callable rather than a spacing because Bars mode is no longer
        an even grid — across a tempo marker the lines on either side of the
        cursor are different distances apart.
        """
        if not self.snap_enabled:
            return None
        pps = self.PIXELS_PER_SECOND
        if pps <= 0:
            return None
        if self.ruler_mode == "time":
            spacing_px = self.ruler_track.TIME_MINOR_INTERVAL * pps
            return uniform_grid(spacing_px) if spacing_px > 0 else None
        divisions = self.beat_division
        tempo_map = self.tempo_map
        return lambda x: tempo_map.nearest_grid_seconds(x / pps, divisions) * pps

    def update_ruler_labels(self):
        """Update ruler labels based on zoom level."""
        self.ruler_track.update_ruler_labels()

    def update_midi_text_truncation(self):
        """Re-fit every clip's label — manual and auto — to its clip at the
        current zoom level.

        Each label is sized from the width its clip was drawn at, rather than
        this re-deriving an extent from the events: a derived one drifts from
        the track's (it missed a song-part marker cutting a clip short, so a
        zoom sized that label for the uncut clip and it ran past the end)."""
        for track in self.midi_events_tracks:
            for item in track.items:
                if not isinstance(item, (DraggableMidiEvent, AutoSceneClip)):
                    continue
                label = item.text_item
                if label is not None and label.data(0):
                    label.setPlainText(elided_label(
                        label.data(0), item.rect.width(), self.content_view))
        # Every zoom path calls this, so the song sections' labels ride it too.
        self.waveform_track.sections.refit_labels()

    def refresh_auto_buttons(self) -> None:
        """Re-sync every fixture track's header toggles (auto/mute/lock) from the
        controller + document."""
        for track in self.midi_events_tracks:
            track.sync_header_state()

    def _draw_song_sections(self) -> None:
        """Redraw the song-part arrangement overlay on the waveform band."""
        if not self.midi_manager:
            return
        song_parts = [e for e in self._cached_events
                      if self.midi_manager.is_song_part(e)]
        self.waveform_track.draw_song_sections(song_parts, self._waveform_y())

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

    # --- Patch lookups: the answers live in `lanes`, which is Qt-free ----

    def _fixture_for_channel(self, channel):
        """The patched fixture instance with stable `channel`, or None."""
        return lanes.fixture_for_channel(self.fixtures, channel)

    def _fixture_type(self, channel):
        """Type name of the patched fixture with stable `channel`, or None."""
        return lanes.fixture_type(self.fixtures, channel)

    def _fdef(self, channel):
        """FixtureDef of the patched fixture with stable `channel`, or None."""
        return lanes.fdef(self.fixtures, channel)

    def _dmx_start(self, channel) -> int:
        """First DMX address of the fixture with stable `channel` (1 if none)."""
        return lanes.dmx_start(self.fixtures, channel)

    def set_files(self, audio_file):
        """Set audio file (None = the song has no monitored track).
        MIDI files should be loaded via MidiManager."""
        self.audio_file = audio_file

        # Redraw the waveform for the new file, or drop the old one
        if audio_file:
            self.waveform_track.draw_waveform(audio_file, self._waveform_y())
        else:
            self.waveform_track.clear_waveform()
        self.apply_song_extent()

        QTimer.singleShot(0, self.fit_to_width)

    def set_song_tracks(self, paths) -> None:
        """Declare every audio track the current song carries — backing AND
        guide, whichever one is monitored.

        The timeline spans the LONGEST track, so switching the monitor to a
        20-second guide can't trim a four-minute song down to 20 seconds.
        Called once per song load; the monitor toggle only changes which of
        these is drawn (set_files), never the extent."""
        self.song_audio_files = [p for p in (paths or []) if p]
        self.apply_song_extent()

    def _song_audio_candidates(self):
        """The audio paths the extent spans: the song's declared tracks, or —
        for a bare timeline that never declared any — whatever is drawn."""
        if self.song_audio_files:
            return self.song_audio_files
        return [self.audio_file] if self.audio_file else []

    def _longest_audio(self) -> float:
        """Duration of the song's longest audio track (0.0 when none is
        readable). Durations come from the injected audio source, whose header
        read is cached — the non-monitored track costs a stat, not a decode."""
        if self.audio_source is None:
            return 0.0
        longest = 0.0
        for path in self._song_audio_candidates():
            try:
                duration, _pcm = self.audio_source(path)
            except Exception:
                duration = None
            longest = max(longest, float(duration or 0.0))
        return longest

    def apply_song_extent(self) -> None:
        """Size the scene to the song's true length: the longest of the
        backing, the guide and the MIDI sequence, plus 5% headroom so the last
        events aren't flush against the edge. A song with nothing loaded falls
        back to the default window.

        The audio half also becomes MidiDocument's song-duration floor, so
        clip-edit bounds span the whole song rather than the monitored file."""
        audio_length = self._longest_audio()
        midi_length = (self.midi_manager.get_duration()
                       if self.midi_manager is not None else 0.0)
        length = max(audio_length, midi_length or 0.0)

        # Only push the floor when this timeline actually knows durations —
        # without an audio source a 0.0 would clobber a value set elsewhere.
        if (self.audio_source is not None and self.midi_manager is not None
                and hasattr(self.midi_manager, "set_song_duration")):
            self.midi_manager.set_song_duration(audio_length)

        width = (int(length * 1.05 * self.PIXELS_PER_SECOND) if length > 0
                 else self.PIXELS_PER_SECOND * self.SECONDS_TO_DISPLAY)
        if width == self.content_width:
            return
        self.content_width = width
        self.content_scene.setSceneRect(0, 0, self.content_width, self.scene_height)
        self.draw_time_grid()
        self.draw_lane_bands()
        self.ruler_track.redraw()
        # The last song section runs to the song's end, which just moved.
        self._draw_song_sections()

    def song_length(self) -> float:
        """The song's true length — the longest of its audio tracks and its
        MIDI sequence, the extent apply_song_extent pads by 5% — or the
        default window when nothing is loaded."""
        midi_length = (self.midi_manager.get_duration()
                       if self.midi_manager is not None else 0.0)
        length = max(self._longest_audio(), midi_length or 0.0)
        return length if length > 0 else self.content_width / self.PIXELS_PER_SECOND
    
    def set_audio_source(self, source) -> None:
        """Install the waveform band's sample source: a callable taking an
        audio path and returning (duration, PCM) — the duration from a header
        read so layout never waits, the PCM only when the engine already holds
        that exact file."""
        self.audio_source = source

    def redraw_waveform(self) -> None:
        """Rebuild the waveform pixmap for the current file. Called when a
        decode finishes: the band was laid out from the duration alone while
        the samples were still on their way."""
        if self.audio_file:
            self.waveform_track.draw_waveform(self.audio_file, self._waveform_y())

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

    def mousePressEvent(self, event):
        """Handle mouse press for seeking."""
        if event.button() == Qt.MouseButton.LeftButton:
            view_pos = self.content_view.mapFrom(self, event.pos())
            if self.content_view.rect().contains(view_pos):
                self._seek_to_scene_x(
                    self.content_view.mapToScene(view_pos.x(), 0).x())
        else:
            super().mousePressEvent(event)
