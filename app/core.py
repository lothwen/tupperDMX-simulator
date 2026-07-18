# ABOUTME: AppCore — the ONE shared orchestration object: constructs and wires
# ABOUTME: the widget-free core (show, audio engine, playback, MIDI, DMX,
# ABOUTME: playlist, web remote). MainWindow is a view over it; the headless
# ABOUTME: runner (app/headless.py) uses it as-is.
import threading

import mido
from PyQt6.QtCore import QObject, pyqtSignal

from app.audio_controller import AudioController
from app.bpm import BpmState
from app.media_controller import MediaController
from app.playlist import PlaylistCore
from app.show_manager import ShowManager
from audio import tts
from audio.engine import AudioEngine
from audio.mixer import BUS_VOICE
from core import note_layout
from core.config import Config, userprefs_path
from core.io_routing import midi_routing_from_io
from dmx import DMXManager
from midi import MidiManager
from tupperlogger import get_logger
from webremote.manager import WebRemoteManager


class AppCore(QObject):
    """Owns and wires every widget-free collaborator, exposing the façade the
    web remote (and the desktop window) consume: media_controller,
    midi_manager, audio_controller, audio_engine, playlist_controller,
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
    precount_changed = pyqtSignal(bool)   # current song's count-in flag
    # Rendered voice cues arriving from the TTS worker thread (song_id, list of
    # (time_sec, samples)); queued back onto the Qt thread before installing.
    _voice_cues_rendered = pyqtSignal(str, object)

    def __init__(self, config: Config, show=None, force_webremote=False):
        super().__init__()
        self.config = config
        self.logger = get_logger("app.core")
        self._shutdown_done = False
        self._voice_cues_rendered.connect(self._on_voice_cues_rendered)

        # The open show project (attach FIRST: it aliases config.playlist /
        # config.fixtures onto the show's objects, and the managers below
        # capture config.fixtures in their constructors).
        self.show_manager = ShowManager(config, self)
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

        self.audio_controller = AudioController(
            self.audio_engine, config.volume.metronome)
        self.audio_controller.beat_occurred.connect(self._on_metronome_beat)
        self.audio_controller.audio_error_occurred.connect(self._on_audio_error)

        # Core half of a BPM change first, then the autosave arming.
        self.bpm_state.bpm_changed.connect(self._on_bpm_changed)
        self.bpm_state.bpm_changed.connect(self.show_manager.on_bpm_changed)

        self.playlist_controller = PlaylistCore(self)

        # The running software controller (None until DMXManager spawns one).
        self.software_controller = None

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

    # ------------------------------------------------------------------
    # Config application
    # ------------------------------------------------------------------
    def apply_io_config(self):
        """Apply config.io end to end: MIDI routing + controller/sinks/stage."""
        self.media_controller.set_midi_routing(midi_routing_from_io(self.config.io))
        self.dmx_manager.apply_io(self.config.io)

    def apply_audio_config(self):
        """Apply config.audio to the live engine + count-in wiring."""
        self.audio_engine.set_output_device(self.config.audio.output_device)
        self.audio_engine.set_routing(self.config.audio.routing)
        song = self.config.get_current_song()
        self.media_controller.set_count_in(
            song.precount if song else False, self.config.audio.count_in_bars)
        self._refresh_count_words()

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
        metronome_was_running = self.audio_controller.is_metronome_running()

        self.media_controller.seek_to_position(time_seconds)

        # Resynchronize metronome if it was running
        if was_playing and metronome_was_running:
            self.audio_controller.stop_metronome()
            self.audio_controller.start_metronome(time_seconds)

    def _on_playback_state_changed(self, is_playing):
        """Keep the metronome locked to the transport."""
        if is_playing:
            # Start metronome synchronized to current timeline position
            current_position = self.media_controller.get_current_position()
            self.audio_controller.start_metronome(current_position)
        else:
            self.audio_controller.stop_metronome()

    def _on_bpm_changed(self, value):
        """Core half of a BPM change: metronome + count-in sizing (widget
        views subscribe to bpm_state separately)."""
        was_running = self.audio_controller.is_metronome_running()
        current_position = self.media_controller.get_current_position() if was_running else 0.0

        self.audio_controller.set_bpm(value)
        self.media_controller.set_bpm(value)

        # Restart metronome with new BPM if it was running
        if was_running:
            self.audio_controller.stop_metronome()
            self.audio_controller.start_metronome(current_position)

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

    def _on_metronome_beat(self, beat_number):
        """During the LAST count-in bar, speak the beat number on the voice bus
        so the musician hears the song coming ("uno, dos, tres, cuatro")."""
        if not self.media_controller.is_counting_in():
            return
        if not self.config.audio.count_in_voice or not self._count_words:
            return
        # Which count-in beat is this? Derived from the position, not the beat
        # counter, so timer jitter around t=0 can never speak into the song.
        beat_len = 60.0 / max(1, self.audio_controller.bpm)
        beats_remaining = round(-self.media_controller.get_current_position() / beat_len)
        if 1 <= beats_remaining <= 4:
            self.audio_engine.play_voice(self._count_words[4 - beats_remaining])

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
    # Voice cues (Fase 4)
    # ------------------------------------------------------------------
    def apply_voice_cues(self, song):
        """(Re-)render the song's cues to PCM in the background; the rendered
        clips install via _voice_cues_rendered. Ruler markers are the UI
        layer's half (PlaylistController / the Cues dialog)."""
        cues = list(song.voice_cues)
        self.media_controller.set_voice_cues([])  # drop stale clips meanwhile
        if not cues:
            return
        voice = self.config.audio.tts_voice
        song_id = song.id

        def work():
            rendered = []
            for cue in cues:
                samples = tts.render(cue.text, voice)
                if samples is not None:
                    rendered.append((cue.time, samples))
            self._voice_cues_rendered.emit(song_id, rendered)

        threading.Thread(target=work, daemon=True, name="tts-cues").start()

    def _on_voice_cues_rendered(self, song_id, rendered):
        """Install rendered cue clips — only if the song is still current."""
        song = self.config.get_current_song()
        if song is not None and song.id == song_id:
            self.media_controller.set_voice_cues(rendered)
            if len(rendered) < len(song.voice_cues):
                self.logger.info(
                    f"{len(song.voice_cues) - len(rendered)} voice cue(s) not "
                    "rendered (TTS voice not ready)")

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
        if self.software_controller is not None:
            self.software_controller.repatch(self.config.fixtures)
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

        try:
            self.show_manager.flush("close")
        except Exception as e:
            self.logger.error(f"Error flushing show on close: {e}")

        # Stop playback and cleanup components — guard each so one failure
        # doesn't abort the rest of the shutdown.
        for name, cleanup in (
            ("webremote_manager", self.webremote_manager.cleanup),
            ("media_controller", self.media_controller.cleanup),
            ("audio_controller", self.audio_controller.cleanup),
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
