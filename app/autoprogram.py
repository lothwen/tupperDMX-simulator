# ABOUTME: AutoProgrammer — the auto program inside the app: a song's light
# ABOUTME: show generated from its audio off the Qt thread (a TAKE), its
# ABOUTME: analysis kept for re-programming, then applied behind a pinned snapshot.
"""The auto program, as the app runs it.

GENERATING never touches the show: decoding, the analysis and the program run
on a worker thread against a copy of the patch, and the result is a Take — a
.mid in a temporary folder, the analysis it came from and what each lane got.
The analyses are kept: a take that differs from one already made only in how
it is programmed (palette, pace, take, the director's style) skips
straight to programming. A take asked for while another is being made
replaces it — that one is dropped, unannounced, and the new one follows.

APPLYING a take is the one write, on the Qt thread: the show is flushed and
snapshotted (pinned, so the song's previous program is one Restore away), then
the take becomes the song's .mid; the current song's document is reloaded
from it, any other song simply has its file replaced.

Desktop only: the web remote does not offer it.
"""
import copy
import os
import shutil
import tempfile
from collections import OrderedDict
from typing import Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from autoprogram import take, writer
from autoprogram.take import Analysis, Options, Take
from core.config import SongConfig
from core.show import versions
from tupperlogger import get_logger

ANALYSES_KEPT = 8       # songs (or tunings) a dialog can go back to without waiting


class Cancelled(Exception):
    pass


class _Job(QThread):
    """One take, made on its own thread. Every signal carries the job, so
    the programmer can tell a dropped take's from the live one's."""

    stage = pyqtSignal(object, str)             # job, step (take.DECODE...)
    analysed = pyqtSignal(object, object)       # job, Analysis
    done = pyqtSignal(object, object)           # job, Take
    failed = pyqtSignal(object, str, str)       # job, step, message

    def __init__(self, key, fixtures, song, options, keep, bpm, mapped, midi_path, parent=None):
        super().__init__(parent)
        self.key, self.fixtures, self.song, self.options = key, fixtures, song, options
        self.keep, self.bpm, self.mapped, self.midi_path = keep, bpm, mapped, midi_path
        self.step = take.DECODE if mapped is None else take.PROGRAM
        self.logger = get_logger("app.autoprogram")

    def run(self):
        try:
            mapped = self.mapped
            if mapped is None:
                mapped = take.analyse(self.song, take.track_of(self.song, self.options),
                                      self.bpm, self.options.tuning, self._stage)
                self.analysed.emit(self, mapped)
            self.done.emit(self, take.program(self.fixtures, self.song, mapped, self.options,
                                              self.midi_path, self.keep, self._stage))
        except Cancelled:
            self.logger.info(f"Auto program of '{self.song.title}' dropped")
        except Exception as e:
            self.logger.exception(f"Auto program of '{self.song.title}' failed")
            self.failed.emit(self, self.step, str(e))

    def _stage(self, step: str, _text: str) -> None:
        if self.isInterruptionRequested():
            raise Cancelled()
        self.step = step
        self.stage.emit(self, step)


class AutoProgrammer(QObject):
    """Makes takes one at a time and applies them. Owned by AppCore."""

    stage = pyqtSignal(str)             # the step the take being made starts
    generated = pyqtSignal(object)      # Take
    failed = pyqtSignal(str, str)       # step, message
    applied = pyqtSignal(object, bool)  # song, whether it is the loaded one

    def __init__(self, core):
        super().__init__(core)
        self.core = core
        self.logger = get_logger("app.autoprogram")
        self._job: Optional[_Job] = None
        self._next = None                   # (song, options) to make once the job stops
        self._analyses: "OrderedDict[tuple, Analysis]" = OrderedDict()
        self._workdir: Optional[str] = None
        self._takes = 0

    def busy(self) -> bool:
        return self._job is not None

    def fixture_count(self) -> int:
        """How many fixtures the show patches — what a take programs."""
        return len(self.core.config.fixtures)

    def song_tempo(self, song: SongConfig) -> Optional[int]:
        """The tempo the song is set to, where the grid search starts and
        what the take's .mid carries; None when the song has none of its own
        yet (then the live tracker's estimate is used)."""
        if not song.midi and not song.cache.get("bpm"):
            return None
        return self.core.playlist_controller.song_bpm(song)

    def analysis_for(self, song: SongConfig, options: Options) -> Optional[Analysis]:
        """The analysis a take of `song` at `options` would be programmed
        from, when it has already been made; None when it needs making."""
        return self._analyses.get(self._key(song, options))

    def generate(self, song: SongConfig, options: Options) -> None:
        """Make a take of `song`; generated() or failed() answers. One being
        made meanwhile is dropped, and this one starts once it stops."""
        if self._job is not None:
            self._job.requestInterruption()
            self._next = (song, options)
            return
        self._start(song, options)

    def cancel(self) -> None:
        """Drop the take being made, and any waiting: it stops at its next
        step, unannounced."""
        self._next = None
        if self._job is not None:
            self._job.requestInterruption()

    def _key(self, song: SongConfig, options: Options) -> tuple:
        """What an analysis depends on: the file (as it is now), the tempo
        searched around, the meter and the tuning."""
        track = take.track_of(song, options)
        path = song.guide if track == take.GUIDE else song.audio
        try:
            stat = os.stat(path)
            stamp = (stat.st_mtime_ns, stat.st_size)
        except (OSError, TypeError):
            stamp = None
        return (track, path, stamp, self.song_tempo(song), song.meter or "4/4", options.tuning)

    def _start(self, song: SongConfig, options: Options) -> None:
        core = self.core
        current = song is core.config.get_current_song()
        fixtures = copy.deepcopy(list(core.config.fixtures))
        # What the song keeps of its program (palette, mute, lock): the loaded
        # document's for the current song, edits not yet saved included; any
        # other song's is read from its file, on the worker.
        keep = writer.lane_state(core.midi_manager, fixtures) if current else None
        if self._workdir is None:
            self._workdir = tempfile.mkdtemp(prefix="tupperdmx-autoprogram-")
        self._takes += 1
        midi_path = os.path.join(self._workdir, f"take-{self._takes}.mid")
        key = self._key(song, options)
        job = _Job(key, fixtures, song, options, keep, self.song_tempo(song),
                   self._analyses.get(key), midi_path, self)
        job.stage.connect(self._on_stage)
        job.analysed.connect(self._on_analysed)
        job.done.connect(self._on_done)
        job.failed.connect(self._on_failed)
        job.finished.connect(self._on_finished)
        self._job = job
        job.start()

    def _live(self, job: _Job) -> bool:
        return job is self._job and not job.isInterruptionRequested()

    def _on_stage(self, job, step):
        if self._live(job):
            self.stage.emit(step)

    def _on_analysed(self, job, mapped):
        # kept even for a dropped take: the next one may be programmed from it
        self._analyses[job.key] = mapped
        self._analyses.move_to_end(job.key)
        while len(self._analyses) > ANALYSES_KEPT:
            self._analyses.popitem(last=False)

    def _on_done(self, job, made):
        if self._live(job):
            self.generated.emit(made)

    def _on_failed(self, job, step, message):
        if self._live(job):
            self.failed.emit(step, message)

    def _on_finished(self) -> None:
        job, self._job = self._job, None
        if job is not None:
            job.deleteLater()
        if self._next is not None:
            (song, options), self._next = self._next, None
            self._start(song, options)

    def apply(self, made: Take) -> None:
        """Make `made` its song's program, behind a pinned snapshot."""
        core, sm = self.core, self.core.show_manager
        song = made.song
        current = song is core.config.get_current_song()
        if current:
            core.stop_playback()
        sm.flush("auto-program")          # the latest hand edits go into the snapshot
        versions.take_snapshot(sm.show, pinned=True, name=snapshot_name(song))
        sm.versions_changed.emit()
        sm.write_song_midi(song, made.midi_path, made.bpm)
        if current:
            sm.begin_song_load()
            try:
                if core.midi_manager.load_from_file(song.midi):
                    detected = core.midi_manager.detect_bpm()
                    if detected is not None:
                        core.bpm_state.set_bpm(detected)
            finally:
                sm.end_song_load()
        else:
            core.playlist_controller.setlist_changed.emit()   # its cached tempo
        self.logger.info(f"Auto program applied to '{song.title}' "
                         f"(palette {made.palette}, take {made.seed})")
        self.applied.emit(song, current)

    def cleanup(self) -> None:
        """For shutdown: stop the running take and wait for its thread, then
        drop the takes' files."""
        self._next = None
        if self._job is not None:
            self._job.requestInterruption()
            self._job.wait()
            self._job = None
        if self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None


def snapshot_name(song: SongConfig) -> str:
    """The pinned version applying a take to `song` saves first."""
    return f"before auto-program — {song.title}"
