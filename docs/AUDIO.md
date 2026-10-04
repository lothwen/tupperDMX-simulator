# Audio and the transport

*One stereo output carries everything the app plays: the song's backing (or guide) track, the click and the spoken
voice. The transport keeps a song's timeline on the system's monotonic clock and drives the MIDI, the lights and the
backing from it. The click does not follow that clock tick by tick. It is placed into the output stream sample by
sample, ahead of time, so nothing the Qt thread does can move a beat.*

This document covers `audio/`, `app/media_controller.py`, `app/metronome.py`, `core/click.py`, the transport half of
`app/core.py`, the offset dialog (`ui/offset_dialog.py` and its caller in `ui/main_window.py`), and the timing
diagnostics in `audio/diagnostics.py`. The tempo map itself (`core/tempo_map.py`) and the marker track
(`core/markers.py`) are described only as far as the click and the transport read them.

Written 2026-10-04 against `betatest1-fixes` (commit `891bade`).

---

## 1. What it does

While a song plays:

1. `MediaController` ticks every 20 ms on the Qt thread. Timeline time is `time.monotonic() − playback_start_time`.
   Negative time is the count-in.
2. Each tick sends the MIDI due by that time, fires the voice cues and control markers the timeline has crossed,
   checks the segue and the end of the song, and keeps the backing in step. The backing plays only while
   `timeline_time − offset ≥ 0`, and is re-seated if it drifts more than 150 ms from where the timeline says it
   should be.
3. `Metronome` pumps every 25 ms on the Qt thread. It turns each beat of the tempo map into an absolute **output
   frame** and queues every beat that falls within the next 0.5 s of output.
4. A worker on its own `TimeCriticalPriority` thread tops up the sound card's buffer every 15 ms. It asks the
   `Mixer` for exactly as many frames as the buffer has room for. The mixer mixes the backing, places each queued
   click and voice clip at its own sample within the block, and advances its frame counter.

The sink runs from startup to shutdown, playing silence when nothing is queued, so a click preview or a voice preview
works with the transport stopped.

```
Qt thread                                            audio-feed thread (TimeCriticalPriority)
─────────                                            ────────────────────────────────────────
MediaController tick, 20 ms                          _FeedWorker._on_feed, 15 ms
  t = monotonic() − playback_start_time                free  = sink.bytesFree()
  MIDI play_until(t) ──▶ controller ──▶ DMX            block = Mixer.render(free frames) ◀──┐
  voice cues ─────────▶ Mixer.trigger(voice)           sink device.write(block)             │
  control markers, segue ──▶ AppCore                   monitor.record_feed(underrun, fill)  │
  backing play / pause / resync ──▶ Mixer cursor                                            │
                                                     Mixer (Qt-free, one lock) ─────────────┘
Metronome pump, 25 ms                                  backing cursor, click and voice one-shots,
  beats with frame < rendered + 0.5 s                  frames_rendered = the output's clock
    ──▶ Mixer.schedule(click, at_frame)
  beat_scheduled ──▶ AppCore ──▶ Mixer.schedule(voice, same frame)
  beat_occurred, once the mixer has rendered the beat ──▶ web remote

audio-decode thread: QAudioDecoder ──▶ int16 PCM ──(queued)──▶ Qt thread ──▶ Mixer.set_backing
```

---

## 2. Where the code lives

| File | Qt | What it does |
|---|---|---|
| `audio/mixer.py` | none | `Mixer`: the backing, click and voice buses, L/R routing, one-shots placed at exact frames, per-bus RMS, the output frame counter. `resample()`. `SAMPLE_RATE` (44.1 kHz reference). |
| `audio/engine.py` | QtCore, QtMultimedia | `AudioEngine`, the one output. `_FeedWorker` owns the `QAudioSink` on the `audio-feed` thread. Format and rate negotiation, the end-of-backing watchdog. |
| `audio/decode.py` | QtCore, QtMultimedia | `ThreadedAudioDecoder`: `QAudioDecoder` on the `audio-decode` thread, to int16 stereo at the pipeline's rate. |
| `audio/click.py` | none | `ClickSounds`: the sample each click kind plays, rebuilt per output rate. |
| `audio/tts.py` | none | Piper TTS with a WAV cache. Voice cues and the count-in words are rendered ahead, never during playback. |
| `audio/diagnostics.py` | none | `TimingMonitor` (per-pass timing record) and `StallAttributor` (opt-in stall sampler). |
| `core/click.py` | none | What each beat is (time, number in the bar, sound kind) and the stop-click rule. |
| `core/tempo_map.py` | none | Seconds ↔ beats for one song: base BPM, tempo changes, meter. |
| `app/metronome.py` | QtCore | `Metronome`: the click's scheduler. Anchor, lookahead, frames, drop rule, free-run. |
| `app/media_controller.py` | QtCore | `MediaController`: the transport. Timeline clock, count-in, backing/guide monitor, voice cues, markers, segue. |
| `app/core.py` | QtCore | `AppCore` wires all of it, and owns what the transport's signals mean: starting and stopping the click, retimes, the spoken count-in, the per-pass timing summary. |
| `ui/offset_dialog.py` | QtWidgets | `OffsetRippleDialog`: what follows an audio track whose offset just moved. |
| `ui/main_window.py` | QtWidgets | The offset drag's handlers (`on_waveform_offset_changed`, `on_waveform_offset_committed`, `move_show_with_audio`). |
| `timeline/tracks/waveform.py` | QtWidgets | `DraggableWaveform`: the drag itself, the click-or-drag rule. |
| `scripts/click_probe.py` | QtGui | Measures one box's timer accuracy, buffer headroom, card clock drift and stall recovery. |

### Layering

- **`audio/` imports nothing from `app/` or `ui/`.** `mixer`, `click`, `tts` and `diagnostics` are Qt-free. `engine`
  and `decode` use QtCore and QtMultimedia only.
- **`core/click.py` and `core/tempo_map.py` are Qt-free** and know nothing about frames or sound cards.
- **`app/` never imports QtWidgets.** Headless runs the same engine, transport and metronome.
- **The window never starts or stops the click.** It once did, after AppCore on the same signal, and its bare restart
  undid both the stop-click and the anchor. Only AppCore calls `follow()` / `stop_metronome()` on transport events.

---

## 3. Threads, locks and clocks

### 3.1 Threads

| Thread | Created by | What runs there |
|---|---|---|
| Qt main thread | — | The transport tick, the metronome pump, the end-of-backing watchdog, decode results, every signal handler, all UI. With stall tracing on, the 15 ms `QtThreadPulse`. |
| `audio-feed` (`QThread`, `TimeCriticalPriority`) | `AudioEngine.__init__` | `_FeedWorker`: creates, writes and stops the `QAudioSink`; its 15 ms top-up timer calls `Mixer.render`. |
| `audio-decode` (`QThread`) | `ThreadedAudioDecoder` | The `QAudioDecoder` and the per-buffer conversion. |
| `tts-count-words`, `tts-voice` (daemon `threading.Thread`) | `AppCore` | Cache-first TTS rendering; results come back by atomic rebind or a queued signal. |
| `stall-attributor` (daemon `threading.Thread`) | `TUPPERDMX_STALL_TRACE=1` only | Samples the Qt thread's pulse every 10 ms (§8.2). |
| Qt's audio backend thread | Qt | Drains the sink's buffer to the device. **No Python ever runs there** (§4.1). |

### 3.2 What is shared, and how

- **`Mixer`**: every method takes one `threading.Lock`. The feed thread holds it for the whole of `render()`; the Qt
  thread takes it for every mutator and for `frames_rendered()`, `schedule()` and `levels()`. `backing_samples()`
  returns the array itself: `set_backing()` swaps it whole and nothing mutates it in place, so a reader on another
  thread may keep the reference.
- **`TimingMonitor`**: one lock, held only for arithmetic. **Never across a log write**, which is a flush (a flash
  write, on the rack) and would hand the audio thread a stall to queue behind.
- **`_FeedWorker` fields** (`opened`, `rate`, `buffer_bytes` …) are written on the audio thread inside a blocking
  queued call and read by the Qt thread only between two such calls. Each blocking call is a full memory barrier.
- **`AppCore._count_words`** is rebound atomically by the TTS thread and read on the Qt thread.

### 3.3 Three clocks

| Clock | Read as | Who rides it |
|---|---|---|
| **Timeline time** | `time.monotonic() − playback_start_time` | MIDI, DMX, voice cues, control markers, segue, the position everyone displays. The backing is slaved to it with a 150 ms deadband. |
| **Output frames** | `Mixer.frames_rendered()` | The click and the spoken count-in, placed at exact frames. The backing cursor advances with it. Never resets: a seek moves the backing cursor, not the output. |
| **Qt timers** | the tick (20 ms), the pump (25 ms), the feed (15 ms) | Cadence only. None of them decides when anything sounds. |

The metronome's anchor pair (`_anchor_time` ↔ `_anchor_frame`) is the **only** bridge between the timeline and the
output frames, and it is taken only on a start, a seek or a retime (§6.2).

**Monotonic, never `time.time()`.** The wall clock steps when NTP corrects it, and a rack SBC with no RTC gets
exactly that correction some seconds after boot. Mid-show it would move the timeline, and every light cue with it,
by however far the clock was wrong.

---

## 4. The engine: one sink and its feed (`audio/engine.py`)

### 4.1 Why push mode, and why a thread of its own

**Push mode.** Qt owns the sink's `QIODevice`; `_FeedWorker._on_feed` writes into it. A pull-mode `QIODevice`
implemented in Python deadlocks inside `QAudioSink.stop()`: Qt's audio thread blocks on the GIL while `stop()` waits
for that thread. With push mode, no Python runs on Qt's audio callback thread at all.

**Its own thread.** The stage was measured blocking the Qt thread for 46–129 ms at a time, inside Qt/GL C++ with no
Python frame above `app.exec()`. That emptied an 80 ms buffer and cost a dropout roughly every 1.2 s of playback. The
blocking code is C++ and does not hold the GIL, so a feed on another thread runs straight through it. The thread runs
at `TimeCriticalPriority`, above everything else in the app.

What the thread does **not** escape: the feed is Python (`_on_feed`, `Mixer.render` in numpy), so it needs the GIL.
Python code that holds the GIL for longer than the buffer starves it exactly as before. §8.2's sampler exists to tell
the two cases apart.

### 4.2 Open → retune → feed

The sink is created, written, read and stopped on the audio thread **and nowhere else**: `QAudioSink` has thread
affinity, and split ownership is how it crashes. The Qt thread drives it through `_call_worker(slot)`, a
`BlockingQueuedConnection` invoke, so the old synchronous order survives:

1. `open_sink` (audio thread): close any previous sink, resolve the device, try formats until one starts (§4.3).
2. `_apply_rate(worker.rate)` (Qt thread, sink open but not feeding): retune the pipeline to the negotiated rate.
3. `begin_feeding` (audio thread): one unrecorded top-up to prime the buffer, then start the 15 ms `PreciseTimer`.

Only the periodic top-up is concurrent. A queued-but-not-waited call would let the pipeline retune underneath a sink
that is already running.

`set_output_device(id)` re-runs this sequence; it is a no-op when the id is unchanged and the sink is open. An unknown
id logs a warning and falls back to the system default.

### 4.3 Format and rate negotiation

`candidate_formats(device)` lists stereo formats best first:

1. Rates in this order, deduplicated: `SAMPLE_RATE` (44.1 kHz, the reference: files and the TTS cache are at that
   rate, so matching it resamples nothing), the device's preferred rate, then 48000, 44100, 96000, 88200, 32000,
   22050, 16000.
2. For each rate, `Float` then `Int16`, kept only if `isFormatSupported()` says yes.
3. Then the device's preferred rate in both sample formats **unconfirmed**, if it was not already listed.

The unconfirmed tail matters on Windows: shared-mode WASAPI reports only its 48 kHz mix format, so every 44.1 kHz
format is refused. `isFormatSupported()` over- and under-reports depending on the backend, so `_try_format` does not
trust it: **the first format whose `start()` returns a device without `OpenError` wins.** If none starts, audio is
disabled with an error naming the device's reported rate range.

**The pipeline rate is the device's, not ours.** `_apply_rate(rate)` retunes everything that was built at the old
rate:

- `ClickSounds.set_rate()` regenerates the beeps.
- The decoder is re-targeted. A decode in flight lands at the new rate too, because the conversion happens when it
  finishes.
- A loaded backing is resampled where it stands and put back at the same position in seconds, so a rate switch
  mid-song is inaudible beyond the sink restart.
- Queued one-shots are dropped (they were placed at the old rate).

Voice clips stay at 44.1 kHz in the TTS cache and are resampled on their way into the mixer (`play_voice`,
`schedule_voice`). `resample()` is linear interpolation over a whole buffer, always off the audio thread.

**The buffer.** The sink is asked for `_BUFFER_SECONDS` = 120 ms. Backends often grant less (80 ms of the 120 on the
dev box). The granted size is logged in ms, with a warning below 90 % of the request: the buffer *is* the
headroom against a stall, so the figure in the source must not be believed when the device disagrees.

### 4.4 The top-up (`_FeedWorker._on_feed`)

Every 15 ms on the audio thread:

1. `free = sink.bytesFree()`.
2. Unless this is the priming call, report to the `TimingMonitor`: whether the sink ran dry (`UnderrunError`, or
   `IdleState`, which some backends report instead of an error in push mode) and how much was still queued. **This is
   the only place in the app where a dropout is observable.**
3. Render `free / bytes_per_frame` frames from the mixer and write them, as float32 or as int16 (×32767).

The priming call is unrecorded because a sink that has never been written to is legitimately empty. Calling that an
underrun would put a false dropout in the log at every start, which is the one thing that teaches an operator to stop
trusting these warnings.

### 4.5 The mixer (`audio/mixer.py`)

Three buses, mixed into stereo float32 and clipped to ±1:

| Bus | Source | Plays |
|---|---|---|
| `backing` | the decoded track, int16 (n, 2) | only while `set_backing_active(True)`; the cursor advances only then |
| `click` | one-shots from `ClickSounds` | always (the sink never stops) |
| `voice` | one-shots from TTS (mono float32) | always |

Each bus has a volume (0–1), a mute, and a post-volume RMS of the last block (`levels()`, which the web remote's
meters read).

**Routing** (`config.audio.routing`, machine-level):

| Mode | Left | Right |
|---|---|---|
| `stereo` | everything | everything |
| `split_backing_left` | backing, downmixed to mono | click + voice |
| `split_backing_right` | click + voice | backing, downmixed to mono |

In the split modes the backing is downmixed before the gain, so a stereo track collapsed onto one channel does not
lose a side.

**One-shots.** `trigger(bus, samples)` queues a clip to start in the next rendered block. `schedule(bus, samples,
at_frame)` queues it at an absolute output frame and returns how many frames late the request was: 0 when the frame
is still ahead (the clip then lands exactly there), positive when the output has already passed it, in which case the
clip starts as soon as possible. Inside `render()`, each clip starts at `at_frame − block_start` within the block.
**That sub-block offset is the click's accuracy.** A clip that spans two blocks re-points `at_frame` at the next
block's start, so one field carries both "begins here" and "continues here".

`clear_scheduled(bus)` drops the clips that have not started and keeps any already sounding. A stop invalidates the
beats placed ahead, but cutting the beep that is half-way out of the speaker turns a clean stop into a pop.
`clear_oneshots(bus)` drops everything (used by `stop_voices` and the rate switch).

**End of the backing.** When the cursor crosses the end of the track, `render()` sets a latch. The feed thread must
not emit Qt signals, so `AudioEngine` polls the latch every 100 ms from the Qt thread (`consume_backing_ended`) and
emits `finished`, which stops the transport.

### 4.6 Decoding (`audio/decode.py`)

`AudioEngine.load_backing(path)` decodes asynchronously and emits `backing_loaded(path)` or `backing_failed(msg,
path)`.

- **Decoding runs on its own thread, and must.** It costs one `bufferReady` slot call per buffer (~9000 for a
  four-minute song). On the Qt thread a track that decodes in 0.15 s idle took over 4 s behind the GPU fog, and since
  every song switch and monitor toggle restarts the decode from zero, a few toggles inside that window left no track
  loaded at all.
- **The worker builds its decoder on the decode thread** (`_DecodeWorker.build`, on `QThread.started`). A
  `QAudioDecoder` constructed on the Qt thread and moved afterwards never decodes a byte on the ffmpeg backend.
- **Requests cross as queued signals to one receiver**, so they arrive in the order they were made.
- **`cancel()` drops the state that names the decode BEFORE calling `stop()`.** `QAudioDecoder` emits `finished()`
  synchronously from inside `stop()`; in the other order, an aborted decode's partial buffers were reported as the
  whole file (a 224 s song "decoded" at 18 s), or an abort before the first buffer was reported as the failure
  "Decoded no audio data".
- **Each buffer's actual format is honoured**: mono is duplicated, Int32/UInt8/Float are converted, and an off-rate
  result is resampled once at the end. A backend that ignores the requested format still yields correct output.
- **`load_backing` dedupes**: the same path already loaded re-emits `backing_loaded`; the same path already decoding
  is ignored, because restarting `QAudioDecoder` mid-decode aborts it. A result whose path is no longer the one being
  loaded is discarded.

The timeline's waveform band never decodes on its own. It draws the PCM the engine decoded to play
(`backing_samples()`, checked against `loaded_path()`), and redraws when the decode lands.

### 4.7 Shutdown

`AudioEngine.cleanup()` is idempotent. In order: stop the stall attributor and its pulse, stop the watchdog, shut the decoder down
(cancel, quit, wait up to 3 s), close the sink **on the audio thread with a blocking call**, then quit the thread and
wait up to 2 s. A queued close would race the quit, and the sink would be torn down by whichever got there first. The
worker is deleted on its own thread (`finished → deleteLater`), because its `QTimer` belongs there.

`AppCore.shutdown()` runs `media_controller`, `metronome`, then `audio_engine` cleanup, after flushing the show.

---

## 5. The transport (`app/media_controller.py`)

### 5.1 Timeline and audio time

**An offset moves ONE audio track.** For the monitored track:

```
audio_time = timeline_time − active_offset()
```

A positive offset starts the audio later on the timeline; the waveform is drawn at `x = offset × PIXELS_PER_SECOND`.
Clips, song parts and markers are anchored in timeline seconds and do not move with it, and the other track (backing
or guide) carries its own offset. Offsets persist as `song.offset` and `song.guide_offset` in `show.json`, validated to
±3600 s.

### 5.2 The tick (`_on_playback_tick`, every 20 ms while playing)

1. `current_timeline_time = monotonic() − playback_start_time`; emit `position_changed`.
2. `MidiManager.play_until(t)` if a sequence is loaded and running. Nothing fires during the count-in: every event
   lives at t ≥ 0.
3. Fire the voice cues and control markers whose time is ≤ t (§5.6).
4. Fire the segue if t has crossed it. **If it fired, return**: the handler already restarted the transport on the
   next song, and nothing below may touch it.
5. A song with no monitored track ends at the MIDI sequence's length (`_click_only_end`). With no sequence either,
   nothing bounds it and the click runs until stopped.
6. `_sync_audio_playback()`:
   - no monitored track: make sure the backing bus is paused;
   - `audio_time < 0` (count-in, or a positive offset not yet reached): pause the backing;
   - otherwise play it, and if `|backing_position − audio_time| > 150 ms`, re-seat the cursor at `audio_time`. The
     deadband avoids jitter; inside it the backing runs on the card's clock.

### 5.3 Transport operations

| Call | What happens |
|---|---|
| `start_playback(force_no_count_in=False)` | t = `−count_in_duration()` if the song counts in, else 0. Start the MIDI worker (if it has data and an output), make sure the monitored track is (being) loaded, put its cursor at 0, reset the voice/marker pointers and re-arm the segue, then start the tick and sync. `force_no_count_in` is the segue arrival: a per-start override, never a write to the song's flag. A song with no track still plays: click, MIDI and lights run on their own. |
| `play_pause_playback()` | Playing: pause, **except during the count-in, where it stops** (pausing mid count-in makes no musical sense). Stopped: start from the top if t == 0 or the monitored track is still decoding, else resume. |
| `_pause_playback()` | Capture t, stop the tick, pause the backing. |
| `_resume_playback()` | Refused while the monitored track is still decoding. Re-derive `playback_start_time` from t, re-seek the MIDI, put the cursor at `max(0, t − offset)`, reset the pointers, start the tick. |
| `stop_playback()` | Stop the tick, pause the backing, **stop voices**, stop the MIDI worker, reset t and the cursor to 0, reset the pointers, re-arm the segue, emit position 0. |
| `seek_to_position(t)` | Move t and `playback_start_time`, seek the MIDI, reset the pointers, arm or disarm the segue by which side of it t lands, then run one tick immediately. Seeking during the count-in simply moves t, which cancels the count-in. |

`playback_state_changed(bool)` is the only signal the click listens to (through AppCore, §6.5). `AppCore.seek_playback`
re-phases the click itself after a seek, because a seek does not change the playing state.

**`monotonic_at(timeline_time)`** is the transport clock's public face: the monotonic instant at which a timeline time
was (or will be) true, or `None` while stopped. The metronome phases its grid on it rather than on "now", because a
position read and then treated as "now" is wrong by however long the read took, and on a seek that is a whole
transport tick (the position signal reaching the timeline, the MIDI dispatch).

### 5.4 Latency between what you see and what you hear

The backing cursor and the click are both expressed in rendered frames, so **the backing and the click are mutually
exact**: whatever sits in the sink's buffer delays both equally. Neither is heard until the buffer drains in front of
it (80–120 ms, plus up to one top-up). The MIDI and the lights do not pass through that buffer. The engine's design
note is that this constant skew is absorbed by the per-song audio offset, as QMediaPlayer's own pipeline latency was
before the engine replaced it.

### 5.5 Count-in

**Count-in is negative timeline time.** With `song.precount` on, playback starts at:

```
count_in_duration() = −tempo_map.seconds_at_beat(−count_in_bars × beats_per_bar)
```

`count_in_bars` is machine-level (`config.audio.count_in_bars`, 1–8). The bar is the song's meter. Negative time
extrapolates at the tempo map's first segment, so a song that starts with a tempo marker at t = 0 counts in at the
tempo it will actually begin with, and the count lands exactly on beat 1 at t = 0. Audio, MIDI and lights wait for
t ≥ 0 while the click runs straight through the boundary.

Pausing during the count-in stops (back to 0); seeking cancels it. The spoken count is §6.7.

### 5.6 Backing, guide and the monitor

A song may carry a **backing** track, a **guide** (a full-mix reference), both or neither; they are equal citizens.
The timeline spans the **longest** of backing, guide and MIDI, declared once per load (`Timeline.set_song_tracks`), and
the web remote takes the same maximum (`webremote.state.song_duration`). The engine plays one of them, the
**monitored** one:

- `active_file()`: the guide when the monitor is `guide` and the song has one, else the backing. A missing guide
  falls back silently.
- `active_offset()`: that track's offset.
- `set_monitor(which)` reloads the engine with the other file and emits `monitor_changed`.
- **The monitor is session-only.** It starts as `backing` every launch and is never persisted.
  `set_auto_monitor(True)` (desktop only) re-picks it on every song load: the guide when the song has one, else the
  backing. Headless and the web remote never enable it, so a rack box always plays the backing.

MIDI, DMX, the click and the cues are timeline-driven and identical under either monitor.

`PlaylistCore._load_song` applies a song in this order: the guide, the monitor choice, the backing and its offset
(so only the monitored file is ever decoded), the MIDI, the base BPM from the `.mid`, the count-in flag, the voice
cues, then the meter, control markers and segue (`apply_song_markers`). The markers come after the BPM because the
tempo map is built from the base tempo plus the markers.

### 5.7 Voice cues, control markers and the segue

- **Voice cues** (`song.voice_tts`) are rendered to PCM off-thread when the song loads (`AppCore.apply_voice_tts`,
  cache-first) and installed as `[(time, samples)]`. The tick fires every cue whose time it has crossed through
  `engine.play_voice()`, which starts it in the next rendered block.
- **Control markers** (stop-click, tempo) are reported by `control_marker_fired(kind, bpm)`. The transport only
  reports the crossing; AppCore decides what each kind does (§6.5).
- **Pointer discipline**, the same for both: every start, resume, seek and stop points at the first entry at or after
  the new position. **Entries behind the playhead never fire on a seek, but their effect may still be in force**: a
  click stopped at 3:08 is still stopped at 3:20. Re-applying that state is AppCore's job, which reads the marker
  list rather than waiting for a crossing.
- **The segue** fires `segue_reached` once per pass when t crosses `segue_time`; seeking back before it re-arms it,
  seeking past it disarms it. A segue at t ≤ 0 never fires (a song whose length is not known yet defaults there).
  AppCore then switches to the next enabled song and starts it with `force_no_count_in=True`; on the last enabled song
  it is a no-op.

---

## 6. The click

### 6.1 Three layers

| Layer | Module | Answers |
|---|---|---|
| Rules | `core/click.py` (Qt-free) | WHAT each beat is: its timeline time, its number in the bar, its sound kind (`accent` / `beat`). Whether the click is on at a position (stop-click). |
| Sound | `audio/click.py` (Qt-free) | What each kind sounds like at the output rate: a 100 ms sine with 5 ms fades, 1200 Hz for the accent and 800 Hz for the others. Hardcoded on purpose, not a setting. |
| Scheduler | `app/metronome.py` | WHEN each beat reaches the output: anchor, lookahead, frames, the drop rule, free-run. |

A beat is addressed by its **continuous index** on the tempo map, negative through a count-in, never by a
free-running counter. `beat_number(index) = index % beats_per_bar + 1` uses Python's floor-based `%`, so the count-in's
beats land on the same bar cycle the song continues with, and the accent survives a tempo change. With the accent off
(`config.audio.click_accent`), beat 1 takes the plain sound but its number stays 1, because the spoken count-in and
the web remote read it.

**The BPM always counts quarter notes.** A map beat is one click of the meter's own note, so 6/8 at 120 BPM is six
clicks a bar, 0.25 s apart.

### 6.2 The click is scheduled into the output, never played by a timer

A timer-driven beep lands wherever the Qt thread happened to be free, and the stage takes that thread away for up to
129 ms. The beat used to move with the graphics load. The scheduler inverts that:

**The anchor.** `_run(position, anchor_monotonic)` pairs one timeline time with one output frame:

```
_anchor_time  = position + max(0, monotonic() − anchor_monotonic)   # where the transport is NOW
_anchor_frame = engine.frames_rendered()                            # where the output is NOW
frame(beat)   = _anchor_frame + round((tempo_map.seconds_at_beat(index) − _anchor_time) × rate)
```

`anchor_monotonic` comes from `MediaController.monotonic_at(position)`. Its lag is added so a position read some
milliseconds ago is not mistaken for "now"; `None` means the caller just read it.

**The lookahead.** Every beat whose frame is below `rendered + LOOKAHEAD_S × rate` (0.5 s) is queued. The beep is in
the mixer's queue long before its block is rendered, so **the pump can be late by anything up to the lookahead and
the listener hears nothing of it.** That is why the pump may live on the Qt thread at all.

**WHEN a beat falls is the tempo map's answer, not `60/bpm`.** A tempo marker needs no restart: the frames queued
past it were already computed from the map that carries it.

**Example.** 120 BPM, 4/4, 48 kHz. The transport starts at t = 10.000 s with the output at frame 1 000 000. Beat 20
is at 10.0 s → frame 1 000 000: queued immediately, late 0. Beat 21 is at 10.5 s → frame 1 024 000, which is not
below the horizon 1 000 000 + 24 000, so it waits. At the next pump the output has rendered, say, 1 001 200 frames;
the horizon is 1 025 200, beat 21 is queued with a lead of 22 800 frames (475 ms), and it will sound exactly at frame
1 024 000 whatever the Qt thread does in between.

### 6.3 The pump, step by step (`_pump`, every 25 ms, `PreciseTimer`)

1. `rendered = frames_rendered()`, `horizon = rendered + 0.5 s`.
2. Walk the beat index forward. For each beat, compute its frame:
   - `frame ≥ horizon`: stop; it is queued on a later pump.
   - `frame < rendered − START_GRACE_S` (20 ms): **drop it** (§6.4).
   - otherwise: `schedule_click(kind, frame)`, record its lateness and (except for the opening beat) its lead in the
     `TimingMonitor`, emit `beat_scheduled(number, seconds, frame)`, and remember `(frame, number)` as pending.
3. If any beat was dropped, `record_dropped(count)`.
4. Every pending beat whose frame the mixer has now rendered emits `beat_occurred(number)`.

`START_GRACE_S` exists for the opening beat. Without it, any lag between reading the transport's position and taking
the anchor (microseconds are enough) makes the beat sitting on that position count as passed, and the count-in loses
its opening downbeat. 20 ms is below the flam a musician can hear and far above that lag. `first_index_at()` uses
`ceil()` on `anchor_time − grace` for the same reason.

The opening beat is placed at the playhead by design, so its lead is zero for a reason that says nothing about the
pump. It is left out of the lead metric, which would otherwise read zero after every start.

### 6.4 Stalls: drop, never crowd

**A beat the playhead has already passed is DROPPED, never crowded in behind it.** What survives a stall is the GRID:
a 1.5 s block costs whole beats and moves none. Sounding a late beat would put a beat between two beats, and a click
that resumes late misleads a musician where a missing one merely disappoints.

What a stall costs depends on its length:

- **Shorter than the lookahead** (the measured stage stalls are ≤ 129 ms): nothing. The beats for that window are
  already queued, and the feed, on its own thread, keeps rendering them.
- **Longer than the lookahead**: the queued 0.5 s plays, then the click is silent until the pump runs again, and
  resumes on the next beat of the original grid. Gaps are whole beats.

`Mixer.schedule()` would play a late request as soon as possible; the metronome never sends one later than the grace.

### 6.5 `follow()`: start, seek and retime

`Metronome.follow(position, anchor_monotonic)` is the ONE start / seek / retime entry. It asks
`click.click_stopped_at(markers, position)`: if a stop-click has already silenced the click there, it stops it,
otherwise it (re)starts it phased on `position`. **A stop-click's effect outlives its crossing**, and only the
earliest stop-click counts (the marker is one-way; there is no start-click).

AppCore calls it in exactly three places:

| Event | Call |
|---|---|
| Transport starts or resumes (`_on_playback_state_changed(True)`) | `follow(position, monotonic_at(position))`, after `monitor.reset()` |
| Seek while playing (`seek_playback`) | `follow(target, monotonic_at(target))` |
| Tempo map changed while the song's click runs (`_on_tempo_map_changed`) | `follow(position, monotonic_at(position))` — the map changed under the playhead, so the grid being counted no longer lines up |

And it stops the click on `_on_playback_state_changed(False)` and on a stop-click marker crossing. A **tempo marker
crossing does nothing** but log: the queued beats already follow the map, and restarting there would re-play the beat
the marker sits on (a tempo marker sits on a beat by construction). **A tempo marker never writes `bpm_state`**: the
base BPM is what the `.mid` persists and what the autosave watches.

### 6.6 Stopping

`stop_metronome()` stops the pump, calls `engine.clear_clicks()` (un-queues every beat placed ahead, lets a beep
already sounding finish), clears the pending list and resets the beat index. It also ends a free-run and emits
`free_run_changed(False)`.

### 6.7 The spoken count-in

When `config.audio.count_in_voice` is on, AppCore speaks the numbers of the **last** count-in bar ("uno, dos, tres,
cuatro" in 4/4; one word per beat of the meter).

- The words are rendered once, off-thread, when the app starts or the setting changes (`_refresh_count_words`,
  cache-first). `render_count_words` returns the prefix that rendered: a box whose cache predates the longer meters,
  and that can no longer synthesize, still counts its 4/4. A meter longer than the cached words speaks nothing.
- They ride `beat_scheduled`: for a beat with `seconds < 0`, `beats_remaining = round(−seconds / beat_length)`; if it
  is within the last bar, word `bar − beats_remaining` is scheduled with `engine.schedule_voice(samples, frame)` **at
  the beat's own frame**, so the word lands on the beep rather than near it.
- Keying off the beat's own time rather than a sampled transport position is what makes it impossible for a late
  reading to speak a count into the song.

### 6.8 Free-run

**Free-run** (the toolbar's ♩▴ click panel and the web remote's FREE-RUN CLICK card) is the one click that is not the
song's. `start_free_run(tempo_map)` halts the click and runs a map of its own from beat 1, off the transport; called
again it retimes, from beat 1 again. AppCore refuses it while the song plays. `follow()`, and so every play and seek,
ends it, as does a transport stop. AppCore's retime on `tempo_map_changed` leaves it alone.

### 6.9 Signals

| Signal | Fires when | Used by |
|---|---|---|
| `beat_scheduled(number, seconds, frame)` | a beat is queued, ~0.5 s ahead | AppCore, for the spoken count-in |
| `beat_occurred(number)` | the mixer has rendered the beat's frame (it reaches the speaker one buffer later) | the web remote's beat indicator |
| `free_run_changed(bool)` | a free-run starts or ends | the click panel, the web remote |
| `metronome_volume_changed`, `metronome_mute_toggled` | click bus volume/mute set | nothing in the app (the timeline's signals of the same names are its own) |

---

## 7. Moving an audio track: the offset dialog

### 7.1 Why there is a dialog

Because an offset moves one track and nothing else (§5.1), the first alignment of a track is exactly what you want,
and every correction after that is wrong: the clips, song parts and markers were programmed against the music where
it used to be, and the other track has its own offset. `OffsetRippleDialog` is that whole decision in one place: the
show, the other track, an exact offset typed by hand, or none of it.

### 7.2 The drag (`timeline/tracks/waveform.py`)

`DraggableWaveform` is a movable `QGraphicsObject` pinned to its row. It has three outcomes:

| Signal | When | Goes to |
|---|---|---|
| `position_changed(pos)` | every mouse-move frame, and every programmatic `set_waveform_offset` | `MainWindow.on_waveform_offset_changed` |
| `offset_committed(old_x, new_x)` | once, on release, for a drag that moved | `MainWindow.on_waveform_offset_committed(old_s, new_s)` |
| `clicked(x)` | release with less than `CLICK_SLOP_PX` (4 screen px) of travel | seek |

**A drag in progress is a proposal.** The live signal moves the SETLIST chip and nothing else: no controller offset,
no config, nothing per-frame in the audio path or the log. The chip shows the backing's offset, so a guide drag leaves
it alone. That is what lets Cancel revert by simply repositioning the item.

**The drag commits once, on release**, the only moment the distance is knowable. A press under the slop is a click:
the item is put back where it was (Qt moves a movable item on its first pixel) and the click seeks.

The song-section boxes span the waveform band but ignore a plain left press below their header bar, so the waveform
under them is still draggable. The band's context menus also offer **"Set audio offset…"**
(`MainWindow.open_offset_editor`), which calls the commit handler with `old == new` and `typed=True`: the dialog opens
with Advanced already expanded, because typing the offset is the question.

### 7.3 When the dialog asks (`MainWindow._ask_offset_ripple`)

It gathers:

- **`show_start`** (`_show_start`): the earliest thing the show has on this song's timeline, with a label naming it —
  the first non-BLACKOUT MIDI event (a song part, a scene or a clip, with its fixture and MIDI channel) or the
  earliest marker (voice cue, control marker, or the segue if it has been dialled away from 0). **BLACKOUTs never
  count**: they are gap markers, not content.
- **the other track**: the backing if the guide was moved and vice versa, with its current offset, or none.
- **the BPM** for the beats field: `tempo_map.bpm_at(old_offset)`, the tempo sounding where the track started.

**It does not ask when there is nothing to offer**: no show, no other track and not typed. The drag is then simply
kept (`AUDIO_ONLY` at the dragged offset). Aligning a lone track on an empty song is the normal first step and must
not nag.

### 7.4 What the dialog shows

**Every figure on it is an ABSOLUTE offset, written `from → to`**, the numbers `show.json` stores. The distance
travelled is real (it is what the show and the other track move by) but is never displayed, because a dialog quoting
both a delta and an absolute has numbers that cannot be checked against anything on disk.

| Element | Content |
|---|---|
| Headline | `Backing track offset: +0.000 s → +1.750 s` (or Guide track) |
| Question | "Move the show's clips, song parts and markers with it…?" Hidden when there is no show. Red, with a warning glyph, when blocked (§7.6). |
| Checkbox | `Also move the guide track: +0.200 s → +1.950 s`. Only when the song has the other track. **Unchecked by default**: the two files have independent lead-ins, so a correction to one is not usually a correction to the other. |
| Advanced (collapsed) | The offset, editable (±3600 s, 3 decimals, step 0.05). "Or move it by: N beats, from +x s, at B BPM" — the one relative figure, an INPUT counted from the old offset. "The drag landed on +y s" when there was a drag. |
| Buttons | **Move show** (only when there is a show; the default), **Audio only** (or **Keep the new offset** when there is no show), **Cancel**. Closing the window is Cancel. |

**The offset field is the value in force.** The two Advanced fields write each other with signals blocked, and the
beats field writes the seconds field, so what gets stored is always the seconds on screen. **Collapsing Advanced
throws the override away**: the dialog must never keep a value the user can no longer see. Every keystroke re-runs
`_refresh()`, which recomputes the headline, the checkbox text, the block state and the button.

### 7.5 What each answer does (`on_waveform_offset_committed`)

**Nothing is written before the answer.**

- **Cancel**: `timeline.set_waveform_offset(old)`. The controller never saw the drag, and the programmatic move
  re-emits the preview, which restores the chip. That is the whole revert.
- **Audio only / Keep the new offset**, or **Move show**:
  1. If Advanced changed the offset, reposition the waveform item to agree.
  2. Write the moved track's offset to the controller (`set_audio_offset` / `set_guide_offset`) and to the song
     (`offset` / `guide_offset`); update the chip for the backing.
  3. If the checkbox was ticked, move the other track by the **delta**, never by the offset: the files have
     independent lead-ins, so what transfers is the correction.
  4. For Move show, `move_show_with_audio(delta)`:
     - `MidiManager.shift_all(delta)` slides the whole sequence rigidly as one undoable edit. Fade notes travel; the
       per-fixture state notes 60 (palette) and 71 (setup) stay pinned at t = 0; BLACKOUTs pushed before t = 0 are
       dropped and `reconcile_blackouts()` tidies up.
     - `core.markers.shift_markers(song, delta)` slides the voice cues, control markers (tempo changes included) and
       a dialled segue.
     - If markers moved, `apply_marker_edits` persists them and re-applies the tempo map and cues; otherwise
       `save_config()`.
  5. `save_config()`.

### 7.6 Refused, not clamped

**A shift that would put the show's first item before t = 0 is refused.** A partial shift would leave the show
looking aligned while sitting the remainder out from the music. `can_move_show()` is `show_start + delta ≥ 0`, and
`show_start` is the earliest of the events and the markers, so both halves are known to be movable before either
moves; a shift that applied to only one would put clips and markers out of step with each other. `shift_all` and
`shift_markers` each refuse on their own as well.

When blocked, the dialog keeps its shape so Advanced can retype the offset:

- the question turns red and names the item in the way ("Its first item, the song part “Intro”, starts at +1.000 s
  and this offset would move it to -0.500 s, before the beginning of the timeline.") and the lowest offset the show
  can follow;
- **Move show** is disabled, red, carries the warning glyph and a tooltip saying why (a disabled button alone reads as
  enabled under the theme, and Qt still shows tooltips on disabled widgets);
- `lowest_offset()` rounds **up** to the millisecond, so typing the quoted figure back is accepted rather than landing
  a fraction before t = 0.

---

## 8. Timing diagnostics (`audio/diagnostics.py`)

### 8.1 `TimingMonitor`: the per-pass record

One instance, owned by `AudioEngine` and shared with the metronome. It is **armed** by AppCore when the transport
starts (`reset()`) and **reported** when it stops: one summary line per playback pass at INFO, and only if the pass
recorded anything (`had_activity()`), so the stop-then-shutdown double call does not print a second line that reads
as a second song.

**What is recorded:**

| From | Call | Meaning |
|---|---|---|
| Metronome, every queued beat | `record_beat(late_s)` | how far behind its frame the beep was queued; 0 by construction unless it fell inside the 20 ms grace |
| Metronome, every queued beat but the opening one | `record_lead(lead_s)` | how far ahead of the playhead the beat was queued |
| Metronome | `record_dropped(count, wait)` | beats a stall made unplayable (warns) |
| Feed thread, every top-up | `record_feed(underrun, fill_s)` | the gap since the last top-up, and the sink's own verdict that it ran dry (warns on an underrun or a gap over 40 ms) |

**The summary line** (the figures here are illustrative):

```
Timing: click 412 beats, late p50 0.0 p99 0.0 max 0.0 ms, min lead 380 ms | audio 0 underruns, 0 late feeds, worst gap 31 ms in 13871 feeds
```

`, N DROPPED` is inserted after the lateness figures when a stall cost beats, and `min lead` is left out when no beat
but the opening one was queued.

How to read it:

- **`min lead` is the click's health metric**, not the beat's lateness, which is 0 by construction. A beat queued by
  a running pump has a lead of the lookahead minus the time since the previous pump, so a stall shows up as `500 ms −
  its length`. Approaching zero means the stalls are as long as the lookahead; past it, beats are dropped. One thing
  lowers it without any stall: after every start or seek, the beats already inside the first 0.5 s are queued at once
  with leads equal to their distance from the playhead. A click faster than two a second therefore reports a min lead
  no higher than one beat (250 ms for 6/8 at 120 BPM).
- **`underruns`** is the number of top-ups at which the sink had run dry. Each one was an audible dropout.
- **`late feeds`** counts gaps over `LATE_FEED_MS` (40 ms) that did not underrun: the early warning.
- **`worst gap`** is the longest interval between two top-ups; compare it with the granted buffer size logged at
  startup.

**Two constraints shape it:**

- **Aggregates, not history.** The feed runs ~66 times a second for a whole show, so it contributes counters, never a
  row. Per-beat lateness is kept for percentiles up to `MAX_BEATS` (20 000, an hour at the fastest tempo); past that
  the counters still count.
- **Warnings are rate-limited** to one per second (`WARN_INTERVAL_S`), with a `(+N more suppressed)` tail. The thread
  whose starvation is being reported is the thread that would write the line, and on the SBC that line goes to flash.
  The limit carries over `reset()`, so a box stalling through a whole set does not get a fresh budget of log writes at
  every song.

Nothing in it is Qt, and nothing touches the sink: the engine reads the device state and passes the verdict in.

### 8.2 `StallAttributor`: what is blocking (opt-in)

```bash
TUPPERDMX_STALL_TRACE=1 uv run python main.py
```

Off by default, because its sampler is a Python thread waking 100 times a second, competing for the GIL it measures.

**Its heartbeat is stamped from the Qt thread.** `AudioEngine` builds a `QtThreadPulse`, a `PreciseTimer` on the Qt
thread that records `time.monotonic()` every `HEARTBEAT_MS` (15 ms), and starts it only when the attributor started.
The pulse reads `None` until the event loop first runs, so app start-up is not reported as a stall. It used to be the
audio feed's top-ups, back when they ran on the Qt thread. Once the feed moved to its own thread it ran straight
through the C++ blocks the attributor exists to name, and those stopped being reported, hence a pulse of the Qt
thread's own.

A daemon thread wakes every `SAMPLE_INTERVAL_S` (10 ms). When the heartbeat is older than `STALL_THRESHOLD_MS`
(40 ms), it is in a stall: it captures the main thread's Python stack (`sys._current_frames()`, the innermost
`STACK_DEPTH` = 5 frames, up to three distinct stacks per stall) and adds up how late its own wake-ups were. When the
heartbeat recovers, it logs one warning, in this form:

```
Qt thread blocked <ms> ms (<verdict>; sampler lost <ms> ms over <n> wakes = <ms> ms each)
    stuck at: <file>:<line> <function> <- <file>:<line> <function> <- ...
```

**The verdict is the sampler's OWN lateness per wake.** Python hands the GIL over every switch interval (5 ms by
default), so a thread competing with a GIL-holding loop slips by milliseconds per wake, while one whose rival is
inside C++ slips by a fraction of one (measured 2.4 vs 0.15 ms). Over `GIL_CONTENTION_MS` (1.0 ms) per wake, the GIL
was held: Python code blocked, the audio feed thread was waiting on the same GIL, and only making that code cheaper
or moving it helps. Under it, the block was in C++ with the GIL free: the audio feed kept running on its own thread,
and only the pump (eating into `min lead`) and the transport (holding back the MIDI and the lights) waited. A real
example, from a 250 ms block injected on the dev box:

```
Qt thread blocked 265 ms (GIL free — it blocked in C++; the audio feed, on its own thread, kept running; sampler lost 3 ms over 22 wakes = 0.16 ms each)
```

### 8.3 `scripts/click_probe.py`: measuring one box

```bash
uv run python scripts/click_probe.py all     # dev box
python3 scripts/click_probe.py all           # rack (scripts/ is not in the image: copy this one file)
```

Silent by construction: the click bus is held at zero and nothing is loaded, so it can run on a patched, live rig.

| Probe | Measures | OK when |
|---|---|---|
| `timer` | Qt's coarse vs `PreciseTimer` accuracy at 70–160 BPM. Needs no sound card. | precise timers land within 5 ms |
| `latency` | The sink's buffer fill sampled every 5 ms for `--seconds`: the headroom a stall must eat before a dropout. | the buffer never fell below 10 ms |
| `clock` | The sound card's sample clock against `time.monotonic()`, in ppm, by least-squares fit over `--seconds` (a minute buys ~16 ppm resolution). Prints the drift over a four-minute song and how long until the 150 ms resync deadband is reached. | drift under 20 ms a song, or none resolvable |
| `stall` | Blocks the Qt thread for `--stall-ms` (default 1500) with a 120 BPM click running, then checks every gap between placed beeps is a whole number of beats, and prints the monitor's summary. | no beep moved off the grid |

---

## 9. Rules not to break

### The output

- **ONE stereo `QAudioSink` feeds every path.** Never open a second sink or a `QMediaPlayer` for a preview.
- **The sink lives on the `audio-feed` thread only.** Create, write, read and stop it there. Reach it through the
  worker's slots with blocking queued calls.
- **Push mode, always.** No Python on Qt's audio callback thread.
- **Open → retune → feed stays in that order.** Retune only while the sink is open and not feeding.
- **Never emit a Qt signal from `Mixer.render()`** or anything else on the feed thread; latch and poll, as the
  end-of-backing watchdog does.
- **Nothing synthesizes during playback.** TTS is cache-first and rendered off-thread ahead of time.
- **The pipeline rate is the device's.** Never assume 44.1 kHz past the TTS cache; ask `engine.rate()`.
- **Never log under the `TimingMonitor` lock**, and keep the feed path free of anything that can block (disk, logging
  without the rate limit, other locks held across slow work).

### The transport

- **`time.monotonic()`, never `time.time()`**, for anything on the timeline.
- **Phase the click on `monotonic_at(position)`**, not on "now".
- **An offset moves one track.** Anything else that should follow it is moved explicitly, after asking.
- **Seek and pointer resets never replay what is behind the playhead.** State that outlives a crossing (stop-click)
  is re-derived from the marker list.
- **After the segue fires, the tick returns.** Nothing may touch a transport that is already playing the next song.
- **The guide monitor is session-only** and never reaches headless or the web remote.

### The click

- **Schedule into the output; never play a beat from a timer.**
- **Drop a passed beat; never crowd it in.** The grid survives a stall, the beat count does not.
- **Beat times come from the tempo map**, and the downbeat from the beat index.
- **`follow()` is the one start / seek / retime entry**, and only AppCore calls it. The window never starts or stops
  the click.
- **A tempo marker crossing does not restart the click**, and never writes `bpm_state`.
- **`stop_metronome()` un-queues the future and lets the sounding beep finish.**
- **Anything that must sound with a beat schedules itself at that beat's frame** (`beat_scheduled`), rather than
  reacting to `beat_occurred`.

### The offset dialog

- **Quote absolutes, `from → to`.** The beats field is the one relative figure, and it is an input.
- **A drag is a proposal until release.** The preview touches the SETLIST chip and nothing else.
- **Refuse, never clamp, a shift that crosses t = 0**, and check both the events and the markers before moving
  either. BLACKOUTs are the exception: they never block, and one pushed before t = 0 is dropped.
- **The other track takes the correction, never the offset.**

---

## 10. Tests

Headless, no audio device (fakes or stubbed devices), offscreen Qt. Each file also runs standalone.

| File | What it pins |
|---|---|
| `test_audio_mixer.py` | Routing gains, split-mode downmix, one-shot mixing and exhaustion, overlapping clips, volume/mute, levels, seek, the end-of-backing latch, clipping, `resample`, retuning to another rate. |
| `test_audio_device_format.py` | The order formats are offered to a device, including the unconfirmed preferred-rate tail (WASAPI). |
| `test_audio_decode.py` | An aborted decode reports nothing; decoding does not ride the GUI event loop. |
| `test_click.py` | `core.click` events, `first_index_at` never sounding a passed beat, every kind having a sound at every rate, `follow()` honouring the stop-click. |
| `test_click_timing.py` | The pump's precise timer, phasing on the caller's anchor, the count-in keeping its opening downbeat, beats on exact frames, a stall under the lookahead being inaudible, a longer one dropping rather than crowding, `beat_occurred` following the output, a sounding beep surviving a stop, `TimingMonitor` statistics, lead, reset, one report per pass and rate limiting, and the `StallAttributor` verdicts (C++ block cleared, GIL holder convicted, per-wake verdict, stack capture, opt-in), plus a real block of the Qt thread reported through its `QtThreadPulse` (the event loop runs for ~0.3 s). |
| `test_count_in.py` | Negative start, no count-in when disabled, pause during the count-in resets, seek cancels it, beat numbering at negative positions. |
| `test_meter.py`, `test_click_popup.py` | Meters on the map and the click, free-run, the click panel's draft and "Set as song tempo". |
| `test_voice_tts.py`, `test_tts_cache.py` | Voice-cue firing pointer (crossing, no double fire, seek); TTS cache keys, cache-first rendering, degradation, count words per language. |
| `test_guide_monitor.py` | Backing/guide monitor, auto-monitor per song load. |
| `test_markers.py` | The marker track, including stop-click and tempo markers. |
| `test_offset_ripple.py` | The drag commits once on release; click-vs-drag slop; `shift_all` and `shift_markers` rigid, one undo, refusing across t = 0; BLACKOUT handling; both tracks offered the delta; Cancel writes nothing; the preview touches only the chip; Advanced overrides; "Set audio offset…"; the blocked state. |

---

## 11. Known limits and quirks

- **The stall attributor watches the Qt thread only.** A block of the `audio-feed` thread itself (a sink write that
  stalls in the backend, with the Qt thread free) is not attributed; `TimingMonitor`'s late-feed and underrun
  warnings are its only trace.
- **The click is anchored to the output clock only at start, seek and retime.** The backing and the click both ride
  the sound card's clock; the timeline (MIDI, lights, cues) rides the system's. Two things can open a gap between
  them during one uninterrupted pass:
  - *Card clock drift.* The backing is pulled back to the timeline when it drifts past 150 ms; the click is not, so
    after such a jump the click and the backing differ by about that much. `click_probe.py clock` measures the drift
    (−1 ppm on the dev box, where PipeWire rate-matches; at that rate the deadband is never reached in a song).
  - *An underrun.* While the sink is dry, no frames are rendered, so everything queued after it sounds later by the
    length of the dropout. Backing and click stay together; both now trail the lights until the backing's 150 ms
    resync, and the click until the next seek or start.
- **Voice cues are not sample-scheduled.** The transport tick fires them through `play_voice()`, so they start in the
  first block rendered after the tick that crossed them: up to a tick late, plus any Qt-thread stall. Only the spoken
  count-in is placed at an exact frame.
- **The offset dialog's beats are quarter notes.** It takes `bpm_at(old offset)` and uses `60 / bpm` per beat, while
  everywhere else a beat is one click of the meter. In a /8 meter one "beat" in that field is two clicks.
- **`record_dropped` is always given a wait of 0**, so its warning reads "back on the grid in 0 ms" whatever the gap
  was.
- **`Metronome.audio_error_occurred` is connected by AppCore and MainWindow but never emitted.**
- **A failed sink is not retried on its own.** If no format starts, `is_running()` is false until the next
  `apply_audio_config()` (Settings, or a `userprefs.json` edit to the `audio` section), which negotiates again.
