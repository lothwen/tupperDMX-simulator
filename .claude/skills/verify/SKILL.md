---
name: verify
description: Drive the real TupperDMX MainWindow headless to verify GUI/playback changes end-to-end (offscreen Qt driver script, real clicks, widget screenshots).
---

# Verifying TupperDMX changes at the GUI surface

The desktop app runs fine under `QT_QPA_PLATFORM=offscreen` on this machine
(real audio decode via the ffmpeg backend, real QAudioSink on the system
device, the QPainter waveform all work headless). The recipe is a
driver script that mirrors `main.run_gui()` and steps through the UI with
`QTimer.singleShot`-scheduled functions:

1. **Never touch the real prefs/show.** Before `Config.load()`:
   `from core import config as config_mod;
   config_mod.userprefs_path = lambda: Path(<tmp>/userprefs.json)` (same
   trick as `tests/test_config_roundtrip.py`). Seed that prefs file with
   `{"last_show_path": <tmp show root>, "volume": {"audio": 0.0,
   "metronome": 0.0, "voice": 0.0}}` — volume 0 keeps tones out of the
   user's speakers; the engine still reports positions/active state.
2. **Seed a show folder by hand**: `<tmp>/X.show/` with
   `midis|audio|fixtures|versions/` subdirs and a `show.json`
   (`format_version: 1`, absolute song paths, `fixtures` copied from the
   repo's shipped `config.json`). Generate WAVs with numpy + `wave`
   (44100 Hz stereo int16); distinct frequencies distinguish tracks.
3. **Launch like main.py**: `QApplication`, apply
   `assets/styles/dark-theme.qss`, `Config.load()`,
   `open_or_create_startup_show(config)`, `MainWindow(config, show)`,
   `window.resize(1600, 900)`, `window.show()`.
4. **Drive real widgets**: `window.timeline_toolbar.<btn>.click()`,
   `window.playlist_controller.next_song()`, etc. Allow ~1–1.5 s between
   steps for async audio decodes. Observe through
   `window.app_core.audio_engine._loaded_path`, `window.media_controller`,
   `window.timeline.audio_file`, toolbar labels, and files on disk.
5. **Screenshots**: `window.grab().save(path)` renders correctly offscreen.
6. Exit via `app.exit(0/1)` from the last step + a global timeout
   `QTimer.singleShot(60000, ...)`; run with
   `cd <repo> && uv run python <driver>.py`.

A working example driver from the guide-track verification lives in the
session scratchpad as `verify_guide.py` (session-lived — copy the pattern,
not the path).

Gotchas:
- Qt spams `propagateSizeHints` warnings offscreen — filter with
  `grep -viE "propagateSizeHints|qt.multimedia"`.
- `AudioEngine.load_backing` dedupes in-flight decodes; if you ever see
  "Decoded no audio data", something restarted the QAudioDecoder mid-decode.
- Headless mode (`--headless`) is covered by `tests/test_headless_smoke.py`;
  don't run it raw against the real prefs.
