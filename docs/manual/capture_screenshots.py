#!/usr/bin/env python3
# ABOUTME: Drives the real TupperDMX MainWindow on the desktop session and
# ABOUTME: captures every figure used by the user manual into docs/manual/images.
"""Capture the user-manual screenshots from the running application.

Run it from the repo root:

    uv run python docs/manual/capture_screenshots.py            # everything
    uv run python docs/manual/capture_screenshots.py --list     # figure names
    uv run python docs/manual/capture_screenshots.py --only stage-view timeline-full

The app is booted exactly the way ``main.run_gui()`` boots it, but against an
isolated copy of the data:

* the show is a ``copytree`` of ``~/Documents/TupperDMX/Billares2026.show``
  into a temp folder, so the real project is never opened or mutated;
* ``core.config.userprefs_path`` is redirected to a patched copy of the real
  prefs, with every volume at zero and the MIDI hardware port disabled, so
  nothing is heard and nothing reaches real gear.

Capture goes through ``spectacle`` (KDE's own screenshot tool, which sees the
composited window including the OpenGL stage) and crops with ImageMagick.
``QWidget.grab()`` is deliberately NOT the primary path: it cannot read back a
GL viewport and returns a blank image for the whole stage view.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
IMAGES = HERE / "images"
sys.path.insert(0, str(REPO))

REAL_SHOW = Path.home() / "Documents" / "TupperDMX" / "Billares2026.show"
WINDOW_GEOMETRY = (150, 55, 2240, 1290)  # x, y, w, h on the desktop
DEMO_SONG_INDEX = 8                      # "10-supersubmarina" — a busy .mid
DEMO_SEEK_SECONDS = 74.0

#: The look the stage figures are shot under. Note 48 takes manual control of
#: a fixture so the song-part program cannot overwrite the next frame; notes
#: 1..23 then set that fixture's channels, where the DMX value is velocity × 2.
#: Channel order per fixture comes from its own mode, so this is written
#: against the demo show's patch and nothing else.
SHOWCASE_LOOK = [
    # ch 0 — ledbar: brightness, blue, green, red, animation
    (0, 48, 0), (0, 1, 127), (0, 2, 112), (0, 3, 26), (0, 4, 74),
    # ch 1 / ch 2 — blinders: brightness, cool1, cool2, warm1, warm2
    (1, 48, 0), (1, 1, 74), (1, 4, 112), (1, 5, 112),
    (2, 48, 0), (2, 1, 74), (2, 4, 112), (2, 5, 112),
    # ch 3 / ch 5 — par-rgbw: brightness, red, green, blue
    (3, 48, 0), (3, 1, 127), (3, 2, 22), (3, 3, 58), (3, 4, 127),
    (5, 48, 0), (5, 1, 127), (5, 2, 22), (5, 3, 58), (5, 4, 127),
    # ch 4 — Wild Wash Pro 648 RGBW: four RGBW cells, warm amber
    (4, 48, 0),
    (4, 1, 104), (4, 2, 46), (4, 3, 9), (4, 4, 26),
    (4, 5, 104), (4, 6, 46), (4, 7, 9), (4, 8, 26),
    (4, 9, 104), (4, 10, 46), (4, 11, 9), (4, 12, 26),
    (4, 13, 104), (4, 14, 46), (4, 15, 9), (4, 16, 26),
    # ch 6 — smoke machine: fog output (the beams need something to hang in)
    (6, 48, 0), (6, 1, 92),
    # ch 7 — moving head: pan, tilt, dimmer, shutter (8-15 is Open), r/g/b, zoom
    (7, 48, 0), (7, 1, 64), (7, 2, 48), (7, 3, 127), (7, 4, 5),
    (7, 5, 26), (7, 6, 96), (7, 7, 127), (7, 12, 46),
]


# ---------------------------------------------------------------------------
# Isolation: temp show + temp prefs, wired in before Qt or Config exist
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_sandbox(tmp: Path) -> tuple[Path, int]:
    """Copy the demo show and the real prefs into `tmp`, patched for capture."""
    if not REAL_SHOW.is_dir():
        sys.exit(f"Demo show not found: {REAL_SHOW}")

    show_copy = tmp / REAL_SHOW.name
    shutil.copytree(REAL_SHOW, show_copy)
    print(f"·  show copy    {show_copy}")

    from core.config import userprefs_path as real_prefs_path
    real_prefs = real_prefs_path()
    prefs = json.loads(real_prefs.read_text(encoding="utf-8")) if real_prefs.exists() else {}

    port = _free_port()
    prefs["last_show_path"] = str(show_copy)
    prefs["recent_shows"] = []
    # Silence: the engine still runs, reports positions and drives the lights.
    prefs["volume"] = {"audio": 0.0, "metronome": 0.0, "voice": 0.0}
    # Never push MIDI at whatever hardware port the user really has patched.
    prefs.setdefault("io", {})["midi_to_port"] = False
    prefs["io"]["midi_to_controller"] = True
    prefs["io"]["controller_enabled"] = True
    prefs["io"]["dmx_to_stage"] = True
    prefs["io"]["dmx_to_artnet"] = False
    prefs["io"]["dmx_to_usb"] = False
    prefs["io"]["stage_source"] = "internal"
    # The web remote figure needs a live server on a port nobody else holds.
    prefs.setdefault("webremote", {})["enabled"] = True
    prefs["webremote"]["port"] = port
    # Keep the HDR stage and the volumetric fog on — capturing on the real
    # desktop is precisely what makes them photographable.
    window = prefs.setdefault("window", {})
    window["stage_gpu"] = True
    window.setdefault("fog", {})["enabled"] = True
    window["dmx_panel_collapsed"] = False
    window["tracks_collapsed"] = False
    window["midi_events_track_height"] = 50

    prefs_copy = tmp / "userprefs.json"
    prefs_copy.write_text(json.dumps(prefs, indent=2), encoding="utf-8")

    from core import config as config_mod
    config_mod.userprefs_path = lambda: prefs_copy
    print(f"·  prefs copy   {prefs_copy}")
    print(f"·  web remote   http://127.0.0.1:{port}/")
    return show_copy, port


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

class Shooter:
    """Full-screen capture through spectacle, cropped to a widget."""

    def __init__(self, app, window, tmp: Path):
        self.app = app
        self.window = window
        self.tmp = tmp
        self.taken: list[str] = []
        self.failed: list[str] = []
        self._n = 0

    # -- plumbing ----------------------------------------------------------
    def _screen_scale(self, image: Path) -> float:
        """Captured pixels per logical point (1.0 unless the screen is scaled)."""
        from PyQt6.QtGui import QGuiApplication
        geo = QGuiApplication.primaryScreen().geometry()
        out = subprocess.run(["magick", "identify", "-format", "%w", str(image)],
                             capture_output=True, text=True)
        try:
            return int(out.stdout.strip()) / max(1, geo.width())
        except ValueError:
            return 1.0

    def _full_screen(self) -> Path | None:
        self._n += 1
        target = self.tmp / f"full-{self._n:03d}.png"
        target.unlink(missing_ok=True)
        try:
            subprocess.run(["spectacle", "-b", "-n", "-f", "-o", str(target)],
                           capture_output=True, timeout=40)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        return target if target.exists() else None

    def settle(self, rounds: int = 6, top=None) -> None:
        """Raise the window about to be shot and let it finish painting.

        `top` matters: raising the main window here would bury a floating one
        (the undocked stage) that is the actual subject of the figure.
        """
        target = top or self.window
        target.raise_()
        target.activateWindow()
        for _ in range(rounds):
            self.app.processEvents()

    # -- the one call a step makes -----------------------------------------
    def shoot(self, name: str, widget=None, pad: int = 0, window=None) -> None:
        """Capture `widget` (or the whole app window) as images/<name>.png."""
        from PyQt6.QtCore import QPoint

        top = window or self.window
        self.settle(top=top)

        full = self._full_screen()
        if full is None:
            print(f"   ✗ {name}: spectacle produced nothing")
            self.failed.append(name)
            return

        scale = self._screen_scale(full)
        if widget is None:
            geo = top.frameGeometry()
            x, y, w, h = geo.x(), geo.y(), geo.width(), geo.height()
        else:
            origin = widget.mapToGlobal(QPoint(0, 0))
            x, y = origin.x(), origin.y()
            w, h = widget.width(), widget.height()
        x, y = int((x - pad) * scale), int((y - pad) * scale)
        w, h = int((w + 2 * pad) * scale), int((h + 2 * pad) * scale)
        x, y = max(0, x), max(0, y)

        out = IMAGES / f"{name}.png"
        crop = subprocess.run(
            ["magick", str(full), "-crop", f"{w}x{h}+{x}+{y}", "+repage",
             str(out)], capture_output=True, text=True)
        if crop.returncode != 0 or not out.exists():
            print(f"   ✗ {name}: crop failed — {crop.stderr.strip()[:120]}")
            self.failed.append(name)
            return

        if self._is_blank(out):
            print(f"   ✗ {name}: image is flat (blank capture?)")
            self.failed.append(name)
            return

        print(f"   ✓ {name}.png  ({w}x{h})")
        self.taken.append(name)

    @staticmethod
    def _is_blank(image: Path) -> bool:
        """A GL readback failure lands as a single flat colour."""
        out = subprocess.run(
            ["magick", "identify", "-format", "%[standard-deviation]", str(image)],
            capture_output=True, text=True)
        try:
            return float(out.stdout.strip().split()[0]) < 1.0
        except (ValueError, IndexError):
            return False


# ---------------------------------------------------------------------------
# The figure script
# ---------------------------------------------------------------------------

class Session:
    """Everything the steps need, plus the sequencing."""

    def __init__(self, app, window, shooter, port, wanted):
        self.app = app
        self.window = window
        self.shot = shooter
        self.port = port
        self.wanted = wanted
        self.steps: list[tuple[str, callable, float]] = []
        self._i = 0
        self._open_dialogs = []

    def add(self, name: str, fn, wait: float = 1.0) -> None:
        self.steps.append((name, fn, wait))

    def start(self) -> None:
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(4000, self._next)

    def _next(self) -> None:
        from PyQt6.QtCore import QTimer
        if self._i >= len(self.steps):
            self._finish()
            return
        name, fn, wait = self.steps[self._i]
        self._i += 1
        if self.wanted and name not in self.wanted and not name.startswith("_"):
            QTimer.singleShot(10, self._next)
            return
        print(f"[{self._i:02d}/{len(self.steps)}] {name}")
        try:
            fn()
        except Exception:
            print(f"   ✗ {name}: step raised")
            traceback.print_exc()
        QTimer.singleShot(int(wait * 1000), self._next)

    def _finish(self) -> None:
        for dlg in self._open_dialogs:
            try:
                dlg.close()
            except Exception:
                pass
        print()
        print(f"captured {len(self.shot.taken)} figure(s) into {IMAGES}")
        if self.shot.failed:
            print(f"FAILED: {', '.join(self.shot.failed)}")
        # close() runs MainWindow.closeEvent, which is what stops the audio
        # engine, the MIDI worker and the web-remote thread. Exiting straight
        # out of the event loop leaves them running and the process hangs.
        self.window.close()
        self.app.processEvents()
        self.app.exit(1 if self.shot.failed else 0)

    # -- helpers -----------------------------------------------------------
    def open_dialog(self, dlg, width=None, height=None):
        """Show a dialog non-modally so the driver keeps the event loop."""
        dlg.setModal(False)
        if width and height:
            dlg.resize(width, height)
        dlg.show()
        dlg.raise_()
        self._open_dialogs.append(dlg)
        for _ in range(8):
            self.app.processEvents()
        return dlg

    def close_dialog(self, dlg):
        try:
            dlg.close()
            if dlg in self._open_dialogs:
                self._open_dialogs.remove(dlg)
        except Exception:
            pass
        for _ in range(4):
            self.app.processEvents()

    def clips(self):
        """Every drawn clip item on the timeline, left to right."""
        from timeline.tracks.midi_events import DraggableMidiEvent
        items = [i for i in self.window.timeline.content_scene.items()
                 if isinstance(i, DraggableMidiEvent)]
        return sorted(items, key=lambda i: (i.pos().y(), i.pos().x()))

    def visible_clips(self):
        """Clips actually inside the timeline viewport — selecting one that has
        scrolled out of view produces a figure with nothing highlighted."""
        view = self.window.timeline.content_view
        area = view.mapToScene(view.viewport().rect()).boundingRect()
        return [c for c in self.clips() if c.sceneBoundingRect().intersects(area)]

    def body_of_type(self, type_name: str):
        for obj in self.window.stage_controller.get_all_fixtures():
            inst = getattr(obj, "_instance", None)
            if inst is not None and inst.type == type_name:
                return obj
        return None


def build_script(session: Session) -> None:
    w = session.window
    shot = session.shot
    app = session.app

    # -- chapter 2: the main window ---------------------------------------
    def stage_the_show():
        w.playlist_controller.go_to_song(DEMO_SONG_INDEX)

    def park_the_playhead():
        """Sit over a stretch that has clips on several lanes."""
        w.seek_playback(DEMO_SEEK_SECONDS)
        w.timeline_toolbar.set_zoom_value(150)
        w.timeline.set_zoom(1.5)

    def light_the_rig():
        """Dial in the look every stage figure is shot under.

        Deliberately manual rather than "play for a few seconds": the transport
        would keep rewriting the rig, so which frame a figure caught would be
        luck. `preview_clip` is the same route the clip library's click-to-
        audition takes, so `config.io` routing still applies.
        """
        w.app_core.preview_clip(SHOWCASE_LOOK)

    session.add("_load-song", stage_the_show, wait=7.0)
    session.add("_park", park_the_playhead, wait=2.0)
    session.add("_light-rig", light_the_rig, wait=7.0)

    session.add("main-window", lambda: shot.shoot("main-window"))

    # -- chapter 3: the stage ---------------------------------------------
    session.add("stage-view",
                lambda: shot.shoot("stage-view",
                                   w.stage_controller.get_stage_view()))

    def enter_edit():
        w.edit_stage_button.click()

    session.add("_edit-mode", enter_edit, wait=1.2)
    session.add("stage-edit-toolbar",
                lambda: shot.shoot("stage-edit-toolbar", w.edit_toolbar, pad=4))

    def shoot_fog_row():
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QCheckBox
        box = w.edit_toolbar.findChild(QCheckBox)
        if box is None:
            print("   ·  no fog row on this machine (no GPU light layer)")
            return
        top = box.mapTo(w.edit_toolbar, QPoint(0, 0)).y()
        _CropWidget.shoot_region(shot, "stage-fog-controls", w.edit_toolbar,
                                 top - 6)

    session.add("stage-fog-controls", shoot_fog_row)

    def select_mover():
        body = session.body_of_type("moving-head") or session.body_of_type("par-rgbw")
        if body is not None:
            w.stage_controller.scene.clearSelection()
            body.setSelected(True)

    session.add("_select-fixture", select_mover, wait=1.0)
    session.add("fixture-inspector",
                lambda: shot.shoot("fixture-inspector", w.fixture_inspector, pad=4))
    session.add("stage-designer",
                lambda: shot.shoot("stage-designer",
                                   w.stage_controller.get_stage_view()))

    # -- chapter 4: the fixture browser -----------------------------------
    state = {}

    def open_browser():
        from ui.fixture.browser import AddFixtureDialog
        dlg = AddFixtureDialog(w.stage_controller, w)
        state["browser"] = session.open_dialog(dlg, 1180, 800)
        dlg.search_input.setText("moving head")
        for _ in range(6):
            app.processEvents()

    session.add("_open-browser", open_browser, wait=2.0)

    def browser_tab(index):
        def go():
            state["browser"].tabs.setCurrentIndex(index)
            for _ in range(6):
                app.processEvents()
        return go

    session.add("add-fixture-library",
                lambda: shot.shoot("add-fixture-library",
                                   window=state["browser"]))
    session.add("_tab-channels", browser_tab(1), wait=0.8)
    session.add("add-fixture-channels",
                lambda: shot.shoot("add-fixture-channels", window=state["browser"]))
    session.add("_tab-modes", browser_tab(2), wait=0.8)
    session.add("add-fixture-modes",
                lambda: shot.shoot("add-fixture-modes", window=state["browser"]))
    session.add("_tab-scenes", browser_tab(3), wait=1.2)
    session.add("add-fixture-scenes",
                lambda: shot.shoot("add-fixture-scenes", window=state["browser"]))

    def collide_patch():
        dlg = state["browser"]
        dlg.tabs.setCurrentIndex(0)
        dlg.dmx_spin.setValue(1)  # straight on top of the LED bar
        for _ in range(6):
            app.processEvents()

    session.add("_collide", collide_patch, wait=0.8)
    session.add("add-fixture-footer",
                lambda: shot.shoot("add-fixture-footer",
                                   state["browser"].patch_bar.parentWidget(),
                                   pad=-4))

    def close_browser():
        session.close_dialog(state.pop("browser"))
        w.exit_edit_mode()

    session.add("_close-browser", close_browser, wait=1.0)

    # -- chapter 6: the top toolbar ---------------------------------------
    session.add("top-toolbar",
                lambda: shot.shoot("top-toolbar", w.toolbar_manager.toolbar))
    session.add("toolbar-song-files",
                lambda: shot.shoot("toolbar-song-files",
                                   w.toolbar_manager.autosave_status_label
                                   .parentWidget(), pad=2))
    session.add("toolbar-versions-section",
                lambda: shot.shoot("toolbar-versions-section",
                                   w.toolbar_manager.restore_button
                                   .parentWidget(), pad=2))
    session.add("toolbar-setlist",
                lambda: shot.shoot("toolbar-setlist",
                                   w.toolbar_manager.song_combo
                                   .parentWidget().parentWidget(), pad=2))

    def open_versions():
        w.toolbar_manager.versions_combo.showPopup()
        for _ in range(4):
            app.processEvents()

    def close_versions():
        w.toolbar_manager.versions_combo.hidePopup()

    session.add("_versions-open", open_versions, wait=0.8)
    session.add("toolbar-versions", lambda: shot.shoot("toolbar-versions"))
    session.add("_versions-close", close_versions, wait=0.5)

    # -- chapter 7: the sidebar -------------------------------------------
    def sidebar(index):
        def go():
            w.right_sidebar.setCurrentIndex(index)
            for _ in range(6):
                app.processEvents()
        return go

    session.add("_lib-tab", sidebar(0), wait=0.8)
    session.add("clip-library", lambda: shot.shoot("clip-library", w.right_sidebar))
    session.add("_dmx-tab", sidebar(1), wait=0.8)
    session.add("dmx-display-used",
                lambda: shot.shoot("dmx-display-used", w.right_sidebar))

    def dmx_full():
        w.dmx_display._seg_full.setChecked(True)
        for _ in range(6):
            app.processEvents()

    session.add("_dmx-full", dmx_full, wait=0.8)
    session.add("dmx-display-full",
                lambda: shot.shoot("dmx-display-full", w.right_sidebar))
    session.add("_lib-back", sidebar(0), wait=0.6)

    # -- chapter 5: the timeline ------------------------------------------
    # Park the playhead inside the song: at 0 it sits on the very left edge and
    # is all but invisible. (The waveform decode finishing resets it, which is
    # why this is done here and not at load time.)
    session.add("_seek", lambda: w.seek_playback(DEMO_SEEK_SECONDS), wait=1.2)
    session.add("timeline-toolbar",
                lambda: shot.shoot("timeline-toolbar", w.timeline_toolbar))
    session.add("timeline-full",
                lambda: shot.shoot("timeline-full",
                                   w.timeline_toolbar.parentWidget()))
    session.add("timeline-track-header",
                lambda: shot.shoot("timeline-track-header",
                                   w.timeline.header_view))

    def fit_the_song():
        """Zoom out to the whole song: at 150% most of the manual clips have
        scrolled off, and a selection figure needs something to point at."""
        w.timeline.fit_to_width()
        for _ in range(6):
            app.processEvents()

    def select_one_clip():
        items = session.visible_clips()
        if not items:
            return
        w.timeline.content_scene.clearSelection()
        widest = max(items, key=lambda c: c.sceneBoundingRect().width())
        widest.setSelected(True)
        for _ in range(6):
            app.processEvents()

    session.add("_fit", fit_the_song, wait=1.0)
    session.add("_select-clip", select_one_clip, wait=0.8)
    session.add("timeline-clip-selected",
                lambda: shot.shoot("timeline-clip-selected",
                                   w.timeline_toolbar.parentWidget()))

    def select_many():
        items = session.visible_clips()
        w.timeline.content_scene.clearSelection()
        for item in sorted(items, key=lambda c: -c.sceneBoundingRect().width())[:5]:
            item.setSelected(True)
        for _ in range(6):
            app.processEvents()

    session.add("_select-many", select_many, wait=0.8)
    session.add("timeline-multi-selection",
                lambda: shot.shoot("timeline-multi-selection",
                                   w.timeline_toolbar.parentWidget()))

    # Clip editors, built the same way Timeline builds them.
    def open_clip_editor():
        from timeline.widgets.clip_edit import ClipEditDialog
        tl = w.timeline

        # Prefer a verb that actually has a parameter editor: a channel verb,
        # then a chase, then a scene load. START (60) has its own dialog and
        # renders as an empty page here, which makes a useless figure.
        def rank(event):
            note = int(event.get('note', 0))
            for index, group in enumerate(((1, 23), (70, 70), (24, 35), (52, 59))):
                if group[0] <= note <= group[1]:
                    return index
            return 99

        candidates = [e for e in tl._cached_events if not e.get('group_id')]
        candidates = sorted((e for e in candidates if rank(e) < 99), key=rank)
        if not candidates:
            return
        event = candidates[0]
        channel = event.get('channel')
        dlg = ClipEditDialog.for_clip(
            fdef=tl._fdef(channel), type_name=tl._fixture_type(channel) or "",
            channel=channel, dmx_start=tl._dmx_start(channel),
            note=int(event.get('note', 0)), velocity=int(event.get('velocity', 0)),
            time=float(event.get('time', 0.0)),
            song_duration=tl.midi_manager.song_end_time(), parent=w)
        state["clip_dlg"] = session.open_dialog(dlg)

    session.add("_open-clip-editor", open_clip_editor, wait=1.5)
    session.add("clip-edit-dialog",
                lambda: shot.shoot("clip-edit-dialog", window=state.get("clip_dlg")))
    session.add("_close-clip-editor",
                lambda: session.close_dialog(state.pop("clip_dlg", None)), wait=0.8)

    def open_scene_editor():
        from timeline.widgets.clip_edit import ClipEditDialog
        tl = w.timeline
        group = next((e for e in tl._cached_events if e.get('group_id')), None)
        if group is None:
            return
        group_id = group['group_id']
        members = [e for e in tl._cached_events if e.get('group_id') == group_id]
        channel = members[0].get('channel')
        velocity_by_note = {m['note']: m['velocity'] for m in members}
        field_note = tl.midi_manager.channel_note_map(channel)
        channels = {name: min(255, velocity_by_note[note] * 2)
                    for name, note in field_note if note in velocity_by_note}
        dlg = ClipEditDialog.for_scene(
            fdef=tl._fdef(channel), type_name=tl._fixture_type(channel) or "",
            channel=channel, dmx_start=tl._dmx_start(channel),
            name=members[0].get('scene_name') or "Scene",
            channels=channels, present=set(channels),
            time=float(members[0]['time']),
            song_duration=tl.midi_manager.song_end_time(), parent=w)
        state["scene_dlg"] = session.open_dialog(dlg)

    session.add("_open-scene-editor", open_scene_editor, wait=1.8)
    session.add("clip-edit-scene",
                lambda: shot.shoot("clip-edit-scene", window=state.get("scene_dlg")))
    session.add("_close-scene-editor",
                lambda: session.close_dialog(state.pop("scene_dlg", None)), wait=0.8)

    def open_start_editor():
        from timeline.widgets.start_event_edit import StartEventEditDialog
        tl = w.timeline
        event = next((e for e in tl._cached_events
                      if int(e.get('note', -1)) == 60), None)
        if event is None:
            event = {'note': 60, 'velocity': 12, 'time': 0.0, 'channel': 0}
        state["start_dlg"] = session.open_dialog(StartEventEditDialog(event, w))

    session.add("_open-start-editor", open_start_editor, wait=1.2)
    session.add("start-event-edit",
                lambda: shot.shoot("start-event-edit", window=state.get("start_dlg")))
    session.add("_close-start-editor",
                lambda: session.close_dialog(state.pop("start_dlg", None)), wait=0.8)

    def open_voice_tts():
        from core.config import ControlMarkerConfig, VoiceTTSConfig
        from ui.markers_dialog import MarkersDialog
        song = w.config.get_current_song()
        cues = list(song.voice_tts) if song is not None else []
        controls = list(song.control_markers) if song is not None else []
        if not cues and not controls:
            # An empty table photographs as nothing at all; show the shape of
            # a filled one instead. Nothing here is written back to the show.
            cues = [VoiceTTSConfig(time=12.0, text="entra batería"),
                    VoiceTTSConfig(time=48.5, text="solo de guitarra")]
            controls = [ControlMarkerConfig(time=96.0, kind="tempo", bpm=132),
                        ControlMarkerConfig(time=188.0, kind="click_stop")]
        songs = w.config.playlist.songs
        dlg = MarkersDialog(
            cues, controls, w, audio_engine=w.app_core.audio_engine,
            tts_voice=w.config.audio.tts_voice,
            base_bpm=w.bpm_state.get_bpm(),
            duration=w.midi_manager.get_duration() or 0.0,
            song_title=song.title if song is not None else "",
            has_next_song=(w.config.playlist.current_song_index
                           < len(songs) - 1))
        state["cues_dlg"] = session.open_dialog(dlg)

    session.add("_open-cues", open_voice_tts, wait=1.0)
    session.add("voice-cues",
                lambda: shot.shoot("voice-cues", window=state.get("cues_dlg")))
    session.add("_close-cues",
                lambda: session.close_dialog(state.pop("cues_dlg", None)), wait=0.8)

    # -- chapter 8: settings ----------------------------------------------
    def open_settings():
        from ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(w.config, w, audio_engine=w.app_core.audio_engine)
        state["settings"] = session.open_dialog(dlg, 1060, 700)

    session.add("_open-settings", open_settings, wait=1.5)

    def settings_page(row, name):
        def go():
            state["settings"].sidebar_list.setCurrentRow(row)
            for _ in range(6):
                app.processEvents()
        session.add(f"_settings-{name}", go, wait=0.8)
        session.add(f"settings-{name}",
                    lambda: shot.shoot(f"settings-{name}", window=state["settings"]))

    settings_page(0, "io")
    settings_page(1, "webremote")
    settings_page(2, "audio")
    settings_page(3, "stage")
    settings_page(5, "logging")
    session.add("_close-settings",
                lambda: session.close_dialog(state.pop("settings", None)), wait=1.0)

    # -- chapter 3 again: the undocked stage window ------------------------
    def undock():
        w.dock_stage_button.click()

    session.add("_undock", undock, wait=2.5)

    def shoot_stage_window():
        win = getattr(w, "_stage_window", None)
        if win is None:
            print("   ·  the stage did not undock")
            return
        # Somewhere it is not entirely covered by the main window, and big
        # enough that the rig is legible in print.
        win.setGeometry(320, 130, 1500, 950)
        for _ in range(10):
            app.processEvents()
        shot.shoot("stage-window-undocked", window=win)

    session.add("stage-window-undocked", shoot_stage_window)
    session.add("timeline-undocked",
                lambda: shot.shoot("timeline-undocked"))
    session.add("_dock-back", lambda: w.dock_stage(), wait=2.5)

    # -- chapter 1: the about box -----------------------------------------
    def open_about():
        from ui.about_dialog import AboutDialog
        state["about"] = session.open_dialog(AboutDialog(w))

    session.add("_open-about", open_about, wait=1.0)
    session.add("about-dialog",
                lambda: shot.shoot("about-dialog", window=state.get("about")))
    session.add("_close-about",
                lambda: session.close_dialog(state.pop("about", None)), wait=0.8)

    # -- chapter 9: the web remote ----------------------------------------
    def shoot_remote():
        out = IMAGES / "webremote-tablet.png"
        out.unlink(missing_ok=True)
        result = subprocess.run(
            ["chromium", "--headless", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", "--virtual-time-budget=4000",
             "--window-size=1280,900", f"--screenshot={out}",
             f"http://127.0.0.1:{session.port}/"],
            capture_output=True, text=True, timeout=90)
        if out.exists() and not Shooter._is_blank(out):
            print("   ✓ webremote-tablet.png")
            shot.taken.append("webremote-tablet")
        else:
            print(f"   ✗ webremote-tablet: {result.stderr.strip()[:160]}")
            shot.failed.append("webremote-tablet")

    session.add("webremote-tablet", shoot_remote, wait=1.0)

    # Leave the rig dark and the transport stopped.
    session.add("_stop", lambda: w.stop_playback(), wait=0.6)


class _CropWidget:
    """Crop a sub-region of a widget (used for the fog row of the stage bar)."""

    @staticmethod
    def shoot_region(shooter: Shooter, name: str, widget, top_offset: int) -> None:
        from PyQt6.QtCore import QPoint
        shooter.settle()
        full = shooter._full_screen()
        if full is None:
            shooter.failed.append(name)
            print(f"   ✗ {name}: spectacle produced nothing")
            return
        scale = shooter._screen_scale(full)
        origin = widget.mapToGlobal(QPoint(0, max(0, top_offset)))
        x = int(origin.x() * scale)
        y = int(origin.y() * scale)
        w = int(widget.width() * scale)
        h = int((widget.height() - max(0, top_offset)) * scale)
        out = IMAGES / f"{name}.png"
        subprocess.run(["magick", str(full), "-crop", f"{w}x{h}+{x}+{y}",
                        "+repage", str(out)], capture_output=True)
        if out.exists() and not Shooter._is_blank(out):
            print(f"   ✓ {name}.png  ({w}x{h})")
            shooter.taken.append(name)
        else:
            print(f"   ✗ {name}: crop failed")
            shooter.failed.append(name)


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", default=None,
                        help="capture just these figures (by name)")
    parser.add_argument("--list", action="store_true",
                        help="print the figure names and exit")
    parser.add_argument("--keep-temp", action="store_true",
                        help="leave the sandbox behind for inspection")
    args = parser.parse_args()

    # A Wayland client is never told where it sits on the desktop, so
    # frameGeometry()/mapToGlobal() come back meaningless and every crop lands
    # in the wrong place. Under XWayland the coordinates are real, and the
    # composited window (GL stage included) still captures normally.
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    IMAGES.mkdir(parents=True, exist_ok=True)

    if shutil.which("spectacle") is None or shutil.which("magick") is None:
        return _fail("This script needs `spectacle` and ImageMagick (`magick`).")

    tmp = Path(tempfile.mkdtemp(prefix="tupperdmx-manual-"))
    print(f"sandbox: {tmp}")
    try:
        show_copy, port = build_sandbox(tmp)

        from core.config import Config
        from core.paths import asset_path
        from core.show.migrate import open_or_create_startup_show
        from main import _setup_logging, _stage_gpu_requested

        if _stage_gpu_requested():
            from stage.gpu.layer import configure_surface_format
            configure_surface_format()

        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QIcon

        app = QApplication(sys.argv[:1])
        app.setDesktopFileName("tupperdmx")
        app.setWindowIcon(QIcon(asset_path("icons", "TupperDMX-icon.png")))
        qss = Path(asset_path("styles", "dark-theme.qss"))
        if qss.exists():
            app.setStyleSheet(qss.read_text(encoding="utf-8"))

        config = Config.load()
        # Like run_gui, and not only for tidiness: the Logging figure shows a
        # live tail of the file this sets up, and without it the pane is empty.
        _setup_logging(config)
        show = open_or_create_startup_show(config)
        print(f"·  show open    {show.root}")

        from ui.main_window import MainWindow
        window = MainWindow(config, show)
        window.setGeometry(*WINDOW_GEOMETRY)
        window.show()

        shooter = Shooter(app, window, tmp)
        session = Session(app, window, shooter, port,
                          set(args.only) if args.only else None)
        build_script(session)

        if args.list:
            for name, _fn, _wait in session.steps:
                if not name.startswith("_"):
                    print(name)
            return 0

        from PyQt6.QtCore import QTimer
        QTimer.singleShot(20 * 60 * 1000, lambda: app.exit(2))  # hard bail-out
        session.start()
        return app.exec()
    finally:
        if args.keep_temp:
            print(f"sandbox kept at {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
