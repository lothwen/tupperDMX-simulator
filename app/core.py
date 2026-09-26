# ABOUTME: AppCore — the ONE shared orchestration object: constructs and wires
# ABOUTME: the widget-free core (show, audio engine, playback, MIDI, DMX,
# ABOUTME: playlist, web remote). MainWindow is a view over it; the headless
# ABOUTME: runner (app/headless.py) uses it as-is.
import threading

import mido
from PyQt6.QtCore import QObject, pyqtSignal

from app.bpm import BpmState
from app.media_controller import MediaController
from app.metronome import Metronome
from app.playlist import PlaylistCore
from app.prefs_watcher import PrefsWatcher
from app.show_manager import ShowManager
from audio import tts
from audio.engine import AudioEngine
from audio.mixer import BUS_VOICE
from core import config as core_config
from core import markers as core_markers
from core import note_layout
from core.config import Config, userprefs_path
from core.io_routing import midi_routing_from_io
from dmx import DMXManager
from midi import MidiManager
from tupperlogger import get_logger, setup_logging
from webremote.manager import WebRemoteManager


class AppCore(QObject):
    """Owns and wires every widget-free collaborator, exposing the façade the
    web remote (and the desktop window) consume: media_controller,
    midi_manager, metronome, audio_engine, playlist_controller,
    show_manager, software_controller, bpm_state, the transport methods and
    preview_clip. Widget-facing halves of its work ride the signals below."""

    # Emitted AFTER the core-side handling (set_software_controller + auto
    # seeding), so widget refreshes always see a ready controller.
    software_controller_changed = pyqtSignal(object)
    # reload_from_show: patch propagated (rebuild patch-dependent widgets),
    # then everything reloaded (counters/title/autosave chip).
    patch_reloaded = pyqtSignal()
    show_reloaded = pyqtSignal()
    config_save_failed = pyqtSignal(str)  # prefs path; UI shows the dialog
    # userprefs.json was edited outside the app and the core half has already
    # been re-applied; carries the list of adopted section names so the view
    # can re-apply its own half (the stage, the header sliders).
    prefs_reloaded = pyqtSignal(object)
    precount_changed = pyqtSignal(bool)   # current song's count-in flag
    # The song's tempo map (base BPM + tempo markers) changed: the timeline
    # re-grids off this, the metronome is already retimed by the time it fires.
    tempo_map_changed = pyqtSignal(object)
    # Rendered voice-TTS lines arriving from the worker thread (song_id, list of
    # (time_sec, samples)); queued back onto the Qt thread before installing.
    _voice_tts_rendered = pyqtSignal(str, object)

    def __init__(self, config: Config, show=None, force_webremote=False,
                 snapshot_on_open=True):
        super().__init__()
        self.config = config
        self.logger = get_logger("app.core")
        self._shutdown_done = False
        self._prefs_watcher = None
        self._voice_tts_rendered.connect(self._on_voice_tts_rendered)

        # The open show project (attach FIRST: it aliases config.playlist /
        # config.fixtures onto the show's objects, and the managers below
        # capture config.fixtures in their constructors).
        self.show_manager = ShowManager(config, self,
                                        snapshot_on_open=snapshot_on_open)
        if show is not None:
            self.show_manager.attach(show)

        # Logic-level BPM holder: every runtime consumer (metronome, count-in
        # sizing, autosave, web remote) reads/subscribes here; the toolbar
        # spinbox is a view of it. The .mid's set_tempo stays canonical on disk.
        self.bpm_state = BpmState()

        # One AudioEngine feeds every audio path (backing + click + voice
        # buses) through the configured output device and channel routing.
        self.audio_engine = AudioEngine(self)
        self.audio_engine.set_output_device(config.audio.output_device)
        self.audio_engine.set_routing(config.audio.routing)
        self.audio_engine.set_bus_volume(BUS_VOICE, config.volume.voice)

        # Spoken count-in numbers, pre-rendered off-thread (None until ready).
        self._count_words = None
        self._refresh_count_words()

        self.dmx_manager = DMXManager(config)
        self.dmx_manager.software_controller_changed.connect(
            self._on_software_controller_changed)
        self.dmx_manager.connection_status_changed.connect(self._on_dmx_status_changed)
        self.dmx_manager.error_occurred.connect(self._on_dmx_error)

        # The note layout is data-driven (note_layout mirrors the controller's
        # handle_note_on); build_note_map() materialises the standalone-clip
        # notes for the renderer/codec, per-fixture verbs resolve on demand.
        self.midi_manager = MidiManager(note_layout.build_note_map(),
                                        fixtures=config.fixtures)
        self.midi_manager.connection_status_changed.connect(self._on_midi_status_changed)
        self.midi_manager.error_occurred.connect(self._on_midi_error)
        # Timeline edits arm the show autosave (song loads are gated by the
        # manager's begin/end_song_load, wrapped around PlaylistCore.load_song).
        self.midi_manager.data_changed.connect(self.show_manager.on_midi_data_changed)

        self.media_controller = MediaController(
            self.midi_manager, self.audio_engine, config.volume.audio)
        self.media_controller.playback_state_changed.connect(
            self._on_playback_state_changed)
        self.media_controller.error_occurred.connect(self._on_media_error)
        self.media_controller.control_marker_fired.connect(
            self._on_control_marker_fired)
        self.media_controller.segue_reached.connect(self._on_segue_reached)

        self.metronome = Metronome(self.audio_engine, config.volume.metronome)
        self.metronome.beat_scheduled.connect(self._on_beat_scheduled)
        self.metronome.audio_error_occurred.connect(self._on_audio_error)

        # Core half of a BPM change first, then the autosave arming.
        self.bpm_state.bpm_changed.connect(self._on_bpm_changed)
        self.bpm_state.bpm_changed.connect(self.show_manager.on_bpm_changed)
        # The map is what the click and the count-in actually read; it is
        # rebuilt by a base-BPM change AND by a tempo-marker edit, so both
        # arrive here rather than only the first.
        self.bpm_state.tempo_map_changed.connect(self._on_tempo_map_changed)
        self.bpm_state.set_tempo_changes([])  # seed both controllers

        self.playlist_controller = PlaylistCore(self)

        # The running software controller (None until DMXManager spawns one),
        # and the patch signature it was last handed (see on_patch_edited).
        self.software_controller = None
        self._last_dmx_sig = self._patch_dmx_signature()

        # Embedded web remote (tablet UI); created here, served on start().
        # force_webremote overrides config.webremote.enabled in memory only
        # (headless: the remote is the only interface) — never persisted.
        self.webremote_manager = WebRemoteManager(
            config, self, force_enabled=force_webremote)

    def start(self) -> None:
        """Bring the core up: current song, persisted I/O routing, web remote
        (same sequence MainWindow always used)."""
        self.playlist_controller.load_current_song()
        self.apply_io_config()
        self.webremote_manager.apply_config()
        self._start_prefs_watcher()

    # ------------------------------------------------------------------
    # Config application
    # ------------------------------------------------------------------
    def apply_io_config(self):
        """Apply config.io end to end: MIDI routing + controller/sinks/stage.

        Through effective_io(), so a command-line pick (`--dmx-usb`) is
        re-applied on every pass — including the one a live prefs reload
        triggers, which replaces config.io wholesale.
        """
        io = self.config.effective_io()
        self.media_controller.set_midi_routing(midi_routing_from_io(io))
        self.dmx_manager.apply_io(io)

    def apply_audio_config(self):
        """Apply config.audio to the live engine + count-in wiring."""
        self.audio_engine.set_output_device(self.config.audio.output_device)
        self.audio_engine.set_routing(self.config.audio.routing)
        song = self.config.get_current_song()
        self.media_controller.set_count_in(
            song.precount if song else False, self.config.audio.count_in_bars)
        self._refresh_count_words()

    def apply_logging_config(self):
        """(Re-)install the log handlers from config.logging: level, console,
        and the file in config.logging.log_dir.

        Both halves take effect immediately — a level change starts filtering
        the very next record, and a folder change starts a file in the new
        place. Files already written stay where they are.
        """
        setup_logging(
            level=self.config.logging.level,
            console_output=self.config.logging.console_output,
            file_output=self.config.logging.file_output,
            log_dir=self.config.logging.log_dir,
            max_file_size=self.config.logging.max_file_size,
            backup_count=self.config.logging.backup_count)

    def apply_volume_config(self):
        """Apply config.volume to the three live buses."""
        self.media_controller.set_audio_volume(self.config.volume.audio)
        self.metronome.set_metronome_volume(self.config.volume.metronome)
        self.audio_engine.set_bus_volume(BUS_VOICE, self.config.volume.voice)

    def set_volume(self, **volumes):
        """Set bus volumes (0.0–1.0) by name — `audio`, `metronome`, `voice`,
        as the timeline header's sliders move them — then apply and persist.

        Applied through apply_volume_config, the same route a prefs reload
        takes, so which controller owns which bus is written down once. It
        re-applies all three: each is a stored level, so the two that did not
        move are simply set to the value they already hold."""
        self.config.update_volume(**volumes)
        self.apply_volume_config()
        self.save_config()

    # ------------------------------------------------------------------
    # Live prefs reload
    # ------------------------------------------------------------------
    def _start_prefs_watcher(self):
        """Watch userprefs.json so an edit made outside the app takes effect
        without a restart. That is the only way to change a setting on a
        headless rack box, where the Settings dialog does not exist and the
        prefs file is reached over the share."""
        if self._prefs_watcher is not None:
            return
        try:
            # Resolved through the module rather than the name imported above,
            # because that is how Config.save()/reload_prefs() reach it: bound
            # at import, the watcher can end up watching a different file than
            # the one they actually read and write.
            self._prefs_watcher = PrefsWatcher(core_config.userprefs_path(), self)
            self._prefs_watcher.changed.connect(self._on_prefs_file_changed)
        except Exception as e:
            # A machine with no inotify watches left still runs, it just
            # stops noticing edits.
            self.logger.warning(f"Live prefs reload unavailable: {e}")
            self._prefs_watcher = None

    def _on_prefs_file_changed(self):
        """Adopt an external edit to userprefs.json, live.

        Applied in the same order and by the same methods the Settings dialog
        uses, so there is one way to apply a setting however it was changed —
        logging first, so everything after it is logged at the level the file
        now asks for.
        """
        try:
            adopted = self.config.reload_prefs()
        except (OSError, ValueError) as e:  # JSONDecodeError is a ValueError
            # Almost always a half-written file rather than a broken one, so
            # look again before saying anything (and never quarantine it).
            if self._prefs_watcher is None or not self._prefs_watcher.retry():
                self.logger.warning(f"Could not read edited userprefs.json: {e}")
            return

        if not adopted:
            return
        self.logger.info(
            f"userprefs.json edited outside the app — adopting: {', '.join(adopted)}")

        if "logging" in adopted:
            self.apply_logging_config()
        if "volume" in adopted:
            self.apply_volume_config()
        if "audio" in adopted:
            self.apply_audio_config()
        # The Art-Net target is what the sinks and the stage listener are built
        # around, so it re-runs the same routing application `io` does.
        if "io" in adopted or "artnet" in adopted:
            self.apply_io_config()
        if "webremote" in adopted:
            self.webremote_manager.apply_config()

        # Last: the view's half runs against a core that has already caught up.
        self.prefs_reloaded.emit(adopted)

    def save_config(self):
        """Save configuration. Single persistence funnel: machine prefs go to
        userprefs.json, and the show manifest is saved iff its content changed
        (so prefs-only call sites don't rewrite it)."""
        prefs_file = str(userprefs_path())
        try:
            if not self.config.save():
                self.config_save_failed.emit(prefs_file)
        except Exception as e:
            self.logger.error(f"Failed to save configuration: {e}")
            self.config_save_failed.emit(prefs_file)
        self.show_manager.notify_changed()

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def play_pause_playback(self):
        """Toggle playback using media controller."""
        self.media_controller.play_pause_playback()

    def stop_playback(self):
        """Stop playback using media controller."""
        self.media_controller.stop_playback()

    def seek_playback(self, time_seconds):
        """Seek to position using media controller."""
        was_playing = self.media_controller.get_playback_state()

        self.media_controller.seek_to_position(time_seconds)

        # Resynchronize the metronome on the seek target (which also decides
        # whether a stop-click has silenced it there).
        if was_playing:
            # The anchor, not "now": seek_to_position() has just run a whole
            # transport tick, and the click would start that far behind the
            # music if it phased itself here.
            self.metronome.follow(
                time_seconds, self.media_controller.monotonic_at(time_seconds))

    def _on_playback_state_changed(self, is_playing):
        """Keep the metronome locked to the transport.

        Also the natural pair of edges for the timing record: armed when the
        transport starts, reported when it stops. One line per pass is what
        makes a glitch at a show answerable the next morning."""
        if is_playing:
            # Start metronome synchronized to current timeline position —
            # unless a stop-click marker already silenced it there.
            current_position = self.media_controller.get_current_position()
            self.audio_engine.monitor.reset()
            self.metronome.follow(
                current_position,
                self.media_controller.monotonic_at(current_position))
        else:
            self.metronome.stop_metronome()
            monitor = self.audio_engine.monitor
            if monitor.had_activity():
                self.logger.info(monitor.summary())
                monitor.reset()

    def _on_bpm_changed(self, value):
        """Core half of a base-BPM change. The metronome and the count-in read
        the MAP, which bpm_state rebuilt before emitting this — so the work
        lands in _on_tempo_map_changed and this only logs the base value."""
        self.logger.debug(f"Base BPM now {value}")

    def _on_tempo_map_changed(self, tempo_map):
        """Install the song's tempo map on everything that keeps time.

        Restarting a running metronome is what re-phases the beat: the map may
        have changed under the playhead (a tempo marker edited mid-playback),
        and its beat grid no longer lines up with the one being counted."""
        was_running = self.metronome.is_metronome_running()
        position = (self.media_controller.get_current_position()
                    if was_running else 0.0)

        self.metronome.set_tempo_map(tempo_map)
        self.media_controller.set_tempo_map(tempo_map)

        if was_running:
            self.metronome.follow(
                position, self.media_controller.monotonic_at(position))

        self.tempo_map_changed.emit(tempo_map)

    # ------------------------------------------------------------------
    # Current song's details
    # ------------------------------------------------------------------
    def set_song_title(self, title: str) -> bool:
        """Rename the current song (the setlist's edit field): persisted and
        published to the web remote. Returns whether anything changed, so the
        view knows to redraw its setlist."""
        song = self.config.get_current_song()
        title = title.strip()
        if song is None or song.title == title:
            return False
        song.title = title
        self.save_config()
        self.webremote_manager._publish_full()
        return True

    def set_song_key(self, key: str):
        """Set the current song's musical key (the TONO chip): persisted and
        published to the web remote."""
        song = self.config.get_current_song()
        if song is None or song.key == key:
            return
        song.key = key
        self.save_config()
        self.webremote_manager._publish_full()

    # ------------------------------------------------------------------
    # Count-in + spoken guide (Fase 4)
    # ------------------------------------------------------------------
    def on_song_precount_toggled(self, enabled: bool):
        """Persist the current song's count-in flag and apply it live
        (triggered by the toolbar chip or the web remote)."""
        song = self.config.get_current_song()
        if song is None or song.precount == bool(enabled):
            return
        song.precount = bool(enabled)
        self.media_controller.set_count_in(enabled, self.config.audio.count_in_bars)
        self.save_config()
        self.precount_changed.emit(song.precount)
        self.webremote_manager._publish_full()

    def set_count_in_bars(self, bars: int):
        """Set the global count-in length (Settings ▸ AUDIO / web remote)."""
        bars = max(1, min(8, int(bars)))
        if bars == self.config.audio.count_in_bars:
            return
        self.config.audio.count_in_bars = bars
        song = self.config.get_current_song()
        self.media_controller.set_count_in(
            song.precount if song else False, bars)
        self.save_config()

    def _on_beat_scheduled(self, beat_number, beat_time, frame):
        """During the LAST count-in bar, speak the beat number on the voice bus
        so the musician hears the song coming ("uno, dos, tres, cuatro").

        Scheduled at the beat's OWN output frame, so the word lands on the beep
        rather than near it, and keyed off the beat's own time rather than a
        sampled transport position — which is what makes it impossible for a
        late reading to speak a count into the song."""
        if beat_time >= 0:
            return                       # count-in beats are the negative ones
        if not self.config.audio.count_in_voice or not self._count_words:
            return
        beat_len = self.metronome.tempo_map.beat_seconds_at(beat_time)
        beats_remaining = round(-beat_time / beat_len)
        if 1 <= beats_remaining <= 4:
            self.audio_engine.schedule_voice(
                self._count_words[4 - beats_remaining], frame)

    def _refresh_count_words(self):
        """Pre-render the spoken count-in numbers in the background (cache-first;
        never synthesized during playback)."""
        if not self.config.audio.count_in_voice:
            self._count_words = None
            return
        voice = self.config.audio.tts_voice

        def work():
            words = tts.render_count_words(voice)
            self._count_words = words  # atomic rebind; read on the Qt thread
            if words is None:
                self.logger.info(
                    f"Spoken count-in unavailable (voice '{voice}' not ready)")

        threading.Thread(target=work, daemon=True, name="tts-count-words").start()

    # ------------------------------------------------------------------
    # Voice TTS (Fase 4)
    # ------------------------------------------------------------------
    def apply_voice_tts(self, song):
        """(Re-)render the song's spoken lines to PCM in the background; the
        rendered clips install via _voice_tts_rendered. Ruler markers are the
        UI layer's half (PlaylistController / the Markers dialog)."""
        lines = list(song.voice_tts)
        self.media_controller.set_voice_tts([])  # drop stale clips meanwhile
        if not lines:
            return
        voice = self.config.audio.tts_voice
        song_id = song.id

        def work():
            rendered = []
            for line in lines:
                samples = tts.render(line.text, voice)
                if samples is not None:
                    rendered.append((line.time, samples))
            self._voice_tts_rendered.emit(song_id, rendered)

        threading.Thread(target=work, daemon=True, name="tts-voice").start()

    def apply_song_markers(self, song):
        """Install a song's control markers and segue across the core.

        The tempo half goes through bpm_state (which rebuilds the map every
        consumer reads), the rest straight onto the transport. Called on every
        song load and after every edit in the markers dialog."""
        markers = list(getattr(song, "control_markers", []))
        self.metronome.set_control_markers(markers)
        self.bpm_state.set_tempo_changes(core_markers.tempo_changes(markers))
        self.media_controller.set_control_markers(
            [(m.time, m.kind, m.bpm) for m in markers])
        self.media_controller.set_segue(
            bool(getattr(song, "segue", False)),
            float(getattr(song, "segue_time", 0.0)))

    def _on_control_marker_fired(self, kind, bpm):
        """Act on a marker the transport just crossed.

        A tempo marker retimes the CLICK, never bpm_state: the base BPM is what
        the .mid persists and what the show autosave watches, so writing a
        mid-song tempo there would rewrite the song's canonical tempo every
        time it played past the marker."""
        if kind == core_markers.KIND_CLICK_STOP:
            self.logger.info("Stop-click marker — click off for the rest of the song")
            self.metronome.stop_metronome()
        elif kind == core_markers.KIND_TEMPO:
            # Nothing to DO: the metronome schedules each beat off the tempo
            # map, which already carries this change, so the interval after the
            # marker is already the new one. Restarting here would re-play the
            # beat the marker sits on — and a tempo marker sits on a beat by
            # construction. Reported for the log only.
            self.logger.info(f"Tempo marker — click now at {bpm} BPM")

    def _on_segue_reached(self):
        """Hand the show to the next song and start it with no count-in.

        No next song means the segue is a no-op rather than an error: a setlist
        gets reordered far more often than its markers do, and the last song
        silently keeping its own ending is the harmless reading."""
        songs = self.config.playlist.songs
        if self.config.playlist.current_song_index >= len(songs) - 1:
            self.logger.info("Segue reached on the last song — nothing to segue to")
            return
        self.playlist_controller.next_song()
        self.media_controller.start_playback(force_no_count_in=True)

    def _on_voice_tts_rendered(self, song_id, rendered):
        """Install the rendered clips — only if the song is still current."""
        song = self.config.get_current_song()
        if song is not None and song.id == song_id:
            self.media_controller.set_voice_tts(rendered)
            if len(rendered) < len(song.voice_tts):
                self.logger.info(
                    f"{len(song.voice_tts) - len(rendered)} voice-TTS line(s) "
                    "not rendered (TTS voice not ready)")

    # ------------------------------------------------------------------
    # MIDI/DMX routing
    # ------------------------------------------------------------------
    def _on_software_controller_changed(self, controller):
        """Handle software controller instance changes: point the MIDI stream
        at it and seed it from the document's persisted per-track setup
        (note 71) BEFORE any widget refresh sees it, then re-emit."""
        self.logger.info(f"Software controller changed: {controller}")
        self.software_controller = controller
        self.midi_manager.set_software_controller(controller)
        self.midi_manager.seed_controller_auto(controller)
        self.software_controller_changed.emit(controller)

    def repatch_controller(self):
        """Hot-swap the patch onto the software controller — the ONE way in.

        `repatch` builds every `Fixture` fresh, so anything on them that is a
        *mirror* of durable state comes back at its dataclass default. The auto
        flag is exactly that: what the .mid persists in its note-71 setup, and
        what the timeline's 'A' buttons show. Nothing else re-seeds it — that
        only happens when the controller instance itself changes — so a patch
        edit silently dropped the whole rig out of the song-part program while
        every lane went on claiming it was in auto. The notes still arrived and
        no longer did anything, which is indistinguishable from a dead MIDI
        route and was reported as one.
        """
        if self.software_controller is None:
            return
        self.software_controller.repatch(self.config.fixtures)
        self.midi_manager.seed_controller_auto(self.software_controller)

    def on_patch_edited(self):
        """A patch edit landed (the stage designer, the fixture dialog): hot-swap
        it onto the software controller — but only when it changed something
        the controller depends on, since a stage drag-move or a rename edits
        the patch too and must not re-patch (and so black out) the rig."""
        dmx_sig = self._patch_dmx_signature()
        if self.software_controller is not None and dmx_sig != self._last_dmx_sig:
            self._last_dmx_sig = dmx_sig
            self.repatch_controller()

    def _patch_dmx_signature(self):
        """What the software DMX engine depends on (per fixture). Excludes the
        name so a rename never re-patches (and never blacks out) the rig;
        includes `mode`, which changes the fixture's DMX footprint."""
        return tuple((f.type, f.mode, f.dmx_start, f.channel)
                     for f in self.config.fixtures)

    def preview_clip(self, messages):
        """Audition clip-library items live: route note_on(s) through the
        configured MIDI outputs without modifying the timeline. `messages` is
        a list of (channel, note, velocity) tuples — length 1 for clips, 7 for
        LED scenes. The channel selects the target fixture (its patch index).
        Also the web remote's cue/blackout/panic path."""
        if not messages:
            return
        routing = midi_routing_from_io(self.config.io)
        if not routing.has_output():
            self.logger.debug("Preview ignored: no MIDI outputs configured")
            return

        if routing.to_controller:
            controller = self.software_controller
            if controller is None:
                self.logger.debug(
                    "Preview: software controller routed but not active")
            else:
                for channel, note, velocity in messages:
                    try:
                        controller.handle_note_on(
                            int(channel), int(note), int(velocity))
                    except Exception as e:
                        self.logger.warning(
                            f"Preview to software controller failed: {e}")

        if not routing.port:
            return

        # External port: reuse the running worker's open handle when it
        # matches, since some MIDI backends refuse a second open on the
        # same device. Otherwise open a transient port for this preview.
        worker = self.midi_manager.current_worker
        if (worker and worker.isRunning() and worker.port
                and worker.port_name == routing.port):
            for channel, note, velocity in messages:
                try:
                    worker.port.send(mido.Message(
                        'note_on', channel=int(channel),
                        note=int(note), velocity=int(velocity)))
                except Exception as e:
                    self.logger.warning(
                        f"Preview via worker port failed: {e}")
            return

        try:
            with mido.open_output(routing.port) as port:
                for channel, note, velocity in messages:
                    port.send(mido.Message(
                        'note_on', channel=int(channel),
                        note=int(note), velocity=int(velocity)))
        except Exception as e:
            self.logger.warning(
                f"Preview open/send on '{routing.port}' failed: {e}")

    # ------------------------------------------------------------------
    # Show project operations
    # ------------------------------------------------------------------
    def reload_from_show(self):
        """Rebuild the core from the (re)attached show: propagate the patch,
        reload the current song and refresh the web-remote snapshot. Widget
        rebuilds ride patch_reloaded (synchronous, before the song loads)
        and show_reloaded (counters/title/autosave chip)."""
        self.midi_manager.update_fixtures(self.config.fixtures)
        self.repatch_controller()
        self._last_dmx_sig = self._patch_dmx_signature()
        self.patch_reloaded.emit()

        self.playlist_controller.load_current_song()
        self.show_reloaded.emit()
        self.webremote_manager._publish_full()

    # ------------------------------------------------------------------
    # Log-only status/error handlers (the UI adds dialogs on top)
    # ------------------------------------------------------------------
    def _on_dmx_status_changed(self, source, status):
        self.logger.info(f"DMX {source} status: {status}")

    def _on_dmx_error(self, source, error):
        self.logger.error(f"DMX {source} error: {error}")

    def _on_midi_status_changed(self, port_name, status):
        self.logger.info(f"MIDI {port_name} status: {status}")

    def _on_midi_error(self, port_name, error):
        self.logger.error(f"MIDI {port_name} error: {error}")

    def _on_media_error(self, error_message, file_path=None):
        self.logger.error(f"Media error: {error_message}")

    def _on_audio_error(self, error_message, error_type):
        self.logger.error(f"Audio error ({error_type}): {error_message}")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def shutdown(self):
        """Flush the show FIRST (pending timeline edits + manifest, while the
        midi manager is still fully alive), then clean everything up in the
        closeEvent order. Idempotent: window close and a headless signal
        handler can both call it."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self.logger.info("Shutting down application core")

        # Before anything else: the final save_config() below would otherwise
        # be reported back to us as an edit, mid-teardown.
        if self._prefs_watcher is not None:
            self._prefs_watcher.stop()
            self._prefs_watcher = None

        try:
            self.show_manager.flush("close")
        except Exception as e:
            self.logger.error(f"Error flushing show on close: {e}")

        # Stop playback and cleanup components — guard each so one failure
        # doesn't abort the rest of the shutdown.
        for name, cleanup in (
            ("webremote_manager", self.webremote_manager.cleanup),
            ("media_controller", self.media_controller.cleanup),
            ("metronome", self.metronome.cleanup),
            ("audio_engine", self.audio_engine.cleanup),
            ("dmx_manager", self.dmx_manager.cleanup),
            ("midi_manager", self.midi_manager.cleanup),
        ):
            try:
                cleanup()
            except Exception as e:
                self.logger.error(f"Error cleaning up {name}: {e}")

        # Save configuration
        self.save_config()
