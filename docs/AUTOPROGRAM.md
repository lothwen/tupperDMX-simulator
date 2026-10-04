# Auto program

*The auto program generates a song's light show from its audio. It reads the guide or backing track for the beat
grid, sections, repeats, climax and hits, then writes a whole `.mid` for the patched rig. It writes through the same
`MidiManager` calls a hand edit uses, so the result can be edited on the timeline like any other show.*

You reach it from **☰ ▸ Auto program…** (desktop only) or from `scripts/autoprogram_cli.py`.

Written 2026-10-04 against `betatest1-fixes` (commit `891bade`).

---

## 1. What it does

One generated show for one song is a **take**. A take is made in two steps:

1. **Analyse.** Decode the audio and map the song into a `SongMap`. The map holds the tempo grid, the downbeat, every
   bar's loudness and energy, where a voice sings, the sections with their song parts, which sections play the same
   music, the climax, builds, impacts, stops, accents, how the song ends, and its key. This takes a few seconds and is
   most of a take's time.
2. **Program.** Give every patched fixture a role from its capabilities. Plan each section: how much of the rig it
   lights, which looks, which colours, which choreography. Turn the plan into cues per fixture, then write them as
   clips, scenes and fades into a `.mid`. This takes a fraction of a second.

The two steps are separate calls because they change for different reasons. The analysis depends on the audio and the
analysis thresholds. The programming depends on the palette, the pace, the seed and the director's style. Changing a
palette therefore programs again from the cached analysis without re-analysing.

A take is written to a temporary `.mid`. **Nothing in the show changes until the user applies it.** Applying flushes
the show, saves a pinned version named `before auto-program — <title>`, then replaces the song's `.mid`.

What a take looks like, from the CLI on `demo.show` (11 fixtures, 2.4 s end to end):

```
tempo 138.04 BPM (live tracker: 138.9), grid fit 2.19x, 4 beats/bar, first downbeat 0.135s, 38 bars, sound until 62.73s
key D minor, ending: ring (last hit 60.65s, sound gone 62.22s)
  bars   1-  4     0.13s  VERSE   A quiet energy 0.37  sung  builds [4]
  bars   5-  8     7.09s  VERSE   A quiet energy 0.54  sung  builds [8]
  bars   9- 16    14.04s  CHORUS  B high  energy 0.57  sung  hits [1.0, 3.0, 4.0]  builds [16]
  bars  17- 24    27.95s  CHORUS  B high  energy 0.61  sung  hits [1.0]  builds [24]  <- CLIMAX
  bars  25- 33    41.86s  VERSE   A quiet energy 0.39  sung  builds [33]
  bars  34- 36    57.51s  CHORUS  B high  energy 0.48  sung  hits [2.0]
  impacts 7.09s, 14.08s, 28.01s, 41.75s, 57.03s
  stops
  accents 0.20s, 2.74s, 3.61s, 47.86s, 49.63s, 53.21s, 54.56s
palette 'ember', pace 4, seed 3562030097, 138 BPM in the file
  bars   1-  4  VERSE   A  glow  lead wash    2 lit in turn
  bars   5-  8  VERSE   A  lead  lead wash    2 lit alternate repeat
  bars   9- 16  CHORUS  B  peak  lead wash    4 lit in turn
  bars  17- 24  CHORUS  B  peak  lead wash    5 lit in turn   climax repeat
  bars  25- 33  VERSE   A  glow  lead wash    2 lit in turn   repeat
  bars  34- 36  CHORUS  B  band  lead wash    4 lit in turn   repeat
  held back for the climax: ch 7
  ch  0 ledbar                   pixel     26 scenes    4 clips,  6 library scenes
  ch  1 blinder                  blinder   10 scenes    9 clips,  2 library scenes (mirrors ch 2)
  ch  4 par-rgbw                 wash      31 scenes    7 clips,  5 library scenes (mirrors ch 6)
  ch  5 par-rgbw                 wash      32 scenes    5 clips,  5 library scenes
  ch  7 moving-head              mover     18 scenes    4 clips,  6 library scenes (also drives ch 8)
  ...
1684 events, 40 fades
```

The example shows several rules from §4 and §5 at work:

- The last chorus (bars 34–36) is too short to be the climax, so bars 17–24 are the climax.
- After the climax nothing goes above the `band` tier.
- Channel 7 stays dark until the climax.
- The centre par (ch 5) has no mirror, because it stands in the middle of the rig.

---

## 2. Where the code lives

| File | Qt | What it does |
|---|---|---|
| `autoprogram/streaming.py` | none | The live, causal analysers: a 3-band filterbank, envelope followers, an onset detector, a tempo estimator, a beat PLL and a section tracker. `bench()` prints them while a file plays. |
| `autoprogram/analysis.py` | none | The whole-song analysis. Runs the live analysers over the recording, then reads all of it at once into a `SongMap`. `Tuning` holds the thresholds a user may move. |
| `autoprogram/decode.py` | QtCore | A blocking decode through the app's own `AudioFileDecoder`, folded to mono float32 with a peak of 1. |
| `autoprogram/colours.py` | none | Palettes as spec strings, hue maths, the mood palette, and which palette colour each song part wears. |
| `autoprogram/looks.py` | none | `Voice` (a fixture's channels by capability, its role, frames built for it), `Look` (one judged state of a fixture) and `Looks` (a lane's library scenes ranked for a section). Also defines the tiers. |
| `autoprogram/cues.py` | none | `Cue` (one lane's look at one moment, with a priority and a hold) and `resolve()`. |
| `autoprogram/director.py` | none | The rig as lanes, the per-section `Plan` and every lane's cues. Also `Style`, `FEELS` and the pace curve. |
| `autoprogram/writer.py` | QtCore | Writes resolved cues into a `MidiManager`. **The only module that writes MIDI.** |
| `autoprogram/take.py` | QtCore | One song's pipeline: `analyse()`, `program()` and `generate()` (both in one call), plus `Options`, `Analysis` and `Take`. |
| `autoprogram/replay.py` | QtCore | Plays a `.mid` into a software controller on a simulated clock and reads back what every fixture emits. |
| `autoprogram/score.py` | QtGui | Draws a replay as a PNG "light score". Used only by the CLI. |
| `app/autoprogram.py` | QtCore | `AutoProgrammer`, owned by `AppCore`. Runs takes on worker threads, caches analyses and applies a take. |
| `ui/autoprogram_dialog.py` | QtWidgets | The ☰ ▸ Auto program… dialog and its Replace confirmation. |
| `scripts/autoprogram_cli.py` | — | The `live`, `analyse`, `program` and `score` commands. It only parses arguments. |
| `tests/test_autoprogram.py` | — | Engine tests on two synthetic songs. |
| `tests/test_autoprogram_app.py` | — | Tests for `AutoProgrammer` and the dialog. |

### Layering

The package is layered in one direction:

```
streaming ─▶ analysis ─▶ colours / looks / cues / director ─▶ writer ─▶ take ─▶ replay ─▶ score
 (live)      (SongMap)       (the design, as cues)             (MIDI)  (pipeline) (light) (picture)

decode ─▶ take                     the one blocking bridge to the app's decoder
app/autoprogram.py ─▶ take         ui/autoprogram_dialog.py ─▶ app/autoprogram.py
scripts/autoprogram_cli.py ─▶ take, replay, score
```

- **Everything up to `director` is Qt-free and never sees `MidiManager`.** `looks` and `colours` read `core/` and
  `software_controller.main` only for capabilities, constants and pure colour maths.
- **`writer` is the only module that writes MIDI.**
- **`decode` blocks.** It runs its own event loop until the decoder answers, so it belongs on a worker thread, in a
  script or in a test.
- **`score` is the only QtGui module, and only the CLI draws with it.** The dialog paints the analysis (its song map
  is drawn from the `SongMap`), never a replay.
- **`take` is the one pipeline.** The app and the CLI both call it, and the CLI holds no logic beyond argument parsing.
- `autoprogram/__init__.py` re-exports nothing. Import the submodule you need.

### Data flow

```
audio file
  │  decode.decode()                         (rate, mono float32, peak 1)
  ▼
analysis.analyse_samples()                   SongMap                  ── AUDIO seconds
  │
  │  director.build_rig(patch)               lanes: role, mirror, looks
  │  colours.palette(spec, seed)             primary / secondary / contrast
  ▼
Director(...).direct()                       a Cue list per lane      ── AUDIO seconds
  │  cues.resolve()                          overrides settled, MIN_GAP kept
  ▼
writer.write() inside MidiManager.bulk_edit()   + the track's offset  ── TIMELINE seconds
  ▼
take-N.mid in a temp folder ──Apply──▶ ShowManager.write_song_midi()
```

---

## 3. The analysis

### 3.1 Two layers

`streaming.py` holds analysers that run **frame by frame**, as a live input would need them. The frame is 1024
samples and the hop is 512 (about 11.6 ms at 44.1 kHz).

| Analyser | What it does |
|---|---|
| `filterbank()` | 4th-order Butterworth low-pass at 120 Hz, band-pass from 120 to 2000 Hz and high-pass at 2 kHz. The filters are causal, so filtering the whole signal at once gives what a streamed filter gives. |
| `EnvFollower` | An RMS smoother with a fast attack and a slow release. |
| `OnsetDetector` | Spectral flux with an adaptive median threshold (Dixon-style peak picking). |
| `TempoEstimator` | Autocorrelation of about 8 s of onset envelope, with a log-BPM prior centred on 120. |
| `BeatPLL` | A phase-locked beat oscillator. The tempo estimate sets its period, and onsets nudge its phase. |
| `SectionTracker` | A QUIET / GROOVE / PEAK state machine on short-term versus long-term energy, with hysteresis and a dwell time. |

`bench()` (the CLI's `live` command) prints all of them while a file plays. `analysis.run_analysers()` runs the
filterbank, envelope followers, onset detector and tempo estimator over a whole song, in `bench()`'s order, and
records every frame instead of printing. The beat PLL and the section tracker are live-only: the whole-song analysis
replaces them.

`analysis.py` then reads **the whole recording at once**, which no live analyser can do. A live tracker wanders by
about an octave on syncopated passages; a fit over the whole song does not. Structure, repeats and the climax only
exist from a whole-song view.

The input is a mono signal normalised to a peak of 1, at the app's reference rate (`SAMPLE_RATE`, 44.1 kHz).
`decode.py` makes it with the app's own decoder, so the analysis reads exactly the samples the app plays. The guide
track (the full mix) is analysed when the song has one, otherwise the backing.

### 3.2 Step by step

Names in capitals are module constants in `analysis.py`; lowercase thresholds are the `Tuning` fields a user may move
(§3.4).

1. **Beat grid.** The candidate tempos are the song's own BPM, when it has one, and the live tracker's settled answer
   (the confidence-weighted median of its estimates after 8 s). `fit_grid()` searches ±`GRID_SEARCH` (3 %) around
   each candidate in steps of 0.02 BPM, trying 48 phases per tempo, then refines the phase. A grid's **fit** is the
   mean onset strength on the beats over the mean onset strength half a beat off them. The candidate with the better
   fit wins. Two warnings can come out of this step. A fit under `STEADY_FIT` (2.0) gives "no steady beat found".
   When the tracker's grid beats the song's typed BPM, the warning says the audio is not at that BPM. **The BPM
   always counts quarter notes**, so a beat lasts `60 / bpm × 4 / denominator` seconds.
2. **Downbeat.** Each beat gets a weight: its bass-onset strength, plus its onset strength, plus twice the chord
   change on it. Each term is divided by its mean. The chord change is clipped at its 90th percentile, so a single
   section change cannot outweigh the kick. The beat of the bar with the highest mean weight is the downbeat. Bar 1
   is then moved back to the first bar that holds sound.
3. **Bars.** Each bar records its mean RMS, the RMS of each beat and its downbeat kick. Loudness is relative to the
   90th percentile of all bar loudness (the "loud reference"). A bar is SILENT below 0.12, QUIET below `quiet_max`
   (0.5), HIGH from `high_min` (0.68), and MID in between. A bar is a **build** when three things hold: its last beat
   is at least `build_rise` (1.4×) as loud as its first and its second half is louder than its first; the next
   downbeat is at least 1.2× the bar's loudness; and the bar is at least `BUILD_MIN` (0.2) loud. The first bar with
   sound is never a build, because a song fading up out of silence is not one.
4. **Energy.** Each sounding bar is ranked against the song's other bars:
   `0.45 × loudness + 0.25 × onset density + 0.15 × brightness (spectral centroid) + 0.15 × low end`. Compressed
   masters keep a verse almost as loud as a chorus; the drums and the brightness still tell the two apart.
5. **Voice.** `voice()` measures how surely a voice sings in each bar. A voice never holds a pitch the way a fret or a
   key does: it glides into notes and wavers on them. The measurement works on a half-rate signal:
   - Split the spectrum into its harmonic part and the rest by comparing a median across time with a median across
     frequency.
   - Track spectral peaks (partials) with reassigned frequencies.
   - Count a partial as gliding when it moves between 3 and 40 cents per frame, in the same direction across two
     frames, and stands well above what the bars around it play at the same place in their bar. That last condition
     filters out the band's riff, even one wavering through a chorus pedal.

   The glide share, the low-mid tilt (60–250 Hz against 250–1000 Hz) and the level are averaged per bar, z-scored
   against the song, and turned into a 0–1 probability by a logistic with `VOICE_WEIGHTS`. A song whose partials
   hardly glide at all (90th percentile under `GLIDE_FLOOR`) is instrumental.
6. **Phrases.** Each boundary between bars gets a score:
   - The Foote novelty (±8 bars) of a timbre self-similarity matrix plus half that of a chord self-similarity matrix.
     Timbre is an MFCC-like cepstrum without its level term; chords are the per-beat chroma.
   - A bonus where the loudness turns between quiet and loud and stays turned.
   - `VOICE_TURN` times the change in singing across the boundary.
   - A forced cut after every build bar, because what a build lands on is new.

   A dynamic program then cuts the song into phrases. It maximises the boundary scores plus the phrase-length rewards
   (`PHRASE8` for 8, 16, 24… bars, `PHRASE4` for 4, `ODD_PHRASE` for an odd length) plus the grid rewards, minus
   `segment_cost` per phrase. The grid rewards are `GRID8` on the song's 8-bar lines, `GRID4` on the 4-bar line
   between them and `OFF_GRID` off the 2-bar lines. The 8-bar grid's phase is chosen once per song, from where the
   boundary scores are highest.
7. **Materials.** Two phrases play the same **material** when their chords line up bar for bar (at the best whole-bar
   offset) and they sound alike. Average-linkage clustering merges phrases until two groups are no longer
   `SAME_MATERIAL` standard deviations more alike than the song's phrases are on average. A sung phrase never shares
   a material with an unsung one, so an intro playing the verse's riff is not the verse. Where a voice sings anywhere
   in the song, the threshold drops to `SAME_MATERIAL_SUNG`, so a chorus sung again with other words is still the
   chorus. Materials are lettered A, B, C… in order of first appearance. A run of phrases of one material at one
   level, with no build between them, is one **section**; its `phrases` list keeps where each phrase starts.
8. **Song parts.** A song with a voice names its sections by where the voice sings (`_name_sung`). The **chorus** is
   the sung material that comes back with the most energy and fills the most of the sung time. A sung material heard
   once between the first and last chorus is a BRIDGE, and any other sung material is a VERSE. Unsung sections are
   INTRO before the singing starts, OUTRO after it ends, and INST (loud) or BREAK (quiet) in between. An instrumental
   song goes by loudness instead (`_name`). Quiet sections are INTRO, BREAK or OUTRO by position, the repeating loud
   material is the CHORUS, the opening material played loud later is an INST, and one-off loud sections become a
   BRIDGE or a loud OUTRO by position.
9. **Climax.** The climax is the last loud section of 4 bars or more whose energy is at least `climax_share` (80 %)
   of the strongest loud section's. It is not always the loudest section: it is where the song builds to.
10. **Outro and END.** When the climax is a CHORUS and what follows it is at most `TAIL_BARS` (4) quiet bars, the
    climax is marked as the song's **OUTRO** and END starts where it ends. This is a marker only (§5.5).
11. **Hits.** For each MID or HIGH section, the hits are the eighths of its bar where the high band's attack, averaged
    over the section, stands well above the rest. These are its snare and cymbal beats. The generator no longer reads
    them (nor `Bar.kick`) since the blinders stopped firing on kicks and snares (§5.6); they remain in the map and its
    report.
12. **Impacts.** An impact is the hit each build lands on (the sharpest climb in the envelope, pinned to an onset
    within 30 ms), plus any onset where the next 0.3 s is at least 2.2× as loud as the previous 1.5 s and loud for the
    song. Impacts closer than a bar keep only the strongest.
13. **Accents.** Accents are the strongest onsets inside QUIET sections: at least half a second apart, clear of impacts
    and build bars, and at most two per bar.
14. **Stops.** A stop is the band dropping out inside the music: sound under `STOP_DEPTH` (15 %) of the loud level for
    at least half a beat (and 0.25 s), with loud music within a second on both sides. It ends on the onset that brings
    the band back.
15. **Ending.** A song still loud in its last two sounding bars ends on a hit. It is a **cut** when the sound is gone
    within `CUT_TAIL` (1 s) of that hit, otherwise a **ring**. A song already quiet by then is a **fade**.
16. **Key.** The key is the major or minor Krumhansl–Kessler profile that the whole song's chroma correlates with best.
17. **Weak grid.** In a song the grid otherwise fits, the analysis flags stretches of 16 or more bars where the beats
    carry under twice the onset strength of the off-beats (a medley, a rubato passage). Each stretch becomes a
    warning.

### 3.3 What comes out: `SongMap`

| Field | Meaning |
|---|---|
| `duration`, `end_of_sound` | Length of the file; start of the bar after the last sounding one. |
| `bpm`, `tracker_bpm` | The fitted quarter-note BPM; the live tracker's settled estimate. |
| `beat`, `beats_per_bar`, `first_downbeat` | The grid: seconds per beat of the meter's own note, beats per bar, bar 1's start. |
| `fit` | How well the grid fits: on-beat over off-beat onset strength (1 = chance, `STEADY_FIT` = 2). |
| `bars` | One `Bar` each: `number`, `start`, `loud`, `beats`, `kick`, `level`, `build`, `energy`, `voice`. |
| `sections` | One `Section` each: `part`, `level`, `first`/`last` bar, `start`/`end`, `material`, `energy`, `sung`, `phrases`, `hits`. |
| `climax`, `outro` | Indices into `sections`; `outro` is the climax when it is a chorus the song ends on. |
| `impacts`, `accents` | `(time, strength)` lists. |
| `stops` | `(from, to)` windows. |
| `ending`, `final_hit`, `cut` | `"cut"`, `"ring"` or `"fade"`; the last hit's time; when the sound stops. |
| `key` | For example `"A minor"`; `minor` is a convenience property. |
| `weak_grid` | `(first, last)` bar ranges the grid does not follow. |
| `warnings` | Human-readable lines; the dialog lists them as notices. |

Its helpers are `time(bar, beat, sixteenth)`, `bar_at(t)`, `section_of(bar)`, `parts()` and `end_start`. `parts()`
returns each section with the part the arrangement band shows (OUTRO and END applied). `analysis.describe()` prints a
map as shown in §1, and `--bars` adds one line per bar.

### 3.4 What can be tuned: `Tuning`

These are the values the dialog's Advanced ▸ Analysis group edits. **Their defaults are the module constants**, the
values the analysis was tuned with.

| `Tuning` field | Dialog label | Default | Effect |
|---|---|---|---|
| `grid_search` | Tempo search ± | 3 % | How far from each candidate BPM the grid is searched. |
| `quiet_max` | Quiet below | 0.50 | Bars below this share of the loud reference are QUIET. |
| `high_min` | High above | 0.68 | Bars at or above this share are HIGH. |
| `build_rise` | Build rise | 1.40× | How much louder a bar's last beat must be than its first to count as a build. |
| `segment_cost` | Phrase cost | 2.5 | The cost of each extra phrase; higher gives fewer, longer sections. |
| `climax_share` | Climax share | 80 % | The share of the strongest loud section's energy the climax needs. |

### 3.5 Constraints and provenance

- **No scipy, at runtime or in dev.** The analysis was tuned on scipy's filters. The numpy ports reproduce `butter` +
  `sosfilt` (`streaming.butterworth`, `_filter`), `decimate`, `stft`, `dct`, `median_filter` and
  `uniform_filter1d` to float32 rounding, and were checked on 14 real songs for identical maps. A "simpler" filter
  would move every threshold.
- **Every time is in AUDIO seconds** (seconds into the analysed file). Only `writer` adds the track's offset.
- **The thresholds were tuned against hand-placed section markers** in a real show (9 songs of `Directo-2027.show`).
  When last measured (2026-10-03), boundary recall with phrase starts included was about 0.72, and chorus labels
  matched about 0.77 of the time (0.72 leaving each song out of the fit). Precision against those markers means
  nothing, because they are sparse and follow the user's own habits: BRIDGE for a pre-chorus, a CHORUS marker every 8
  bars of a chorus. Don't tune toward those habits blindly.
- **The voice weights were fitted offline** to vocal stems from a source-separation model (MDX-Net), which agree with
  the DSP detector on about 85 % of bars. The model is not shipped and nothing at runtime needs it.

---

## 4. The generator

### 4.1 The rig: lanes, roles, mirrored pairs and units

`director.build_rig(fixtures)` turns the patch into **lanes**, one per programmable fixture. Fixtures of one type and
mode patched on the same DMX address are **twins**: they share one lane, and the lane's position covers all of them.

Each lane's **role** comes from its fixture's capabilities (`Voice.role()`), first match wins:

| Role | Read from |
|---|---|
| `mover` | A Pan or Tilt channel. |
| `fog` | A FogOutput channel and no colour channels. |
| `pixel` | An Effect channel with 3 or more effects, plus chromatic colour (an LED bar with built-in animations). |
| `wash` | Chromatic colour channels (Red, Green, Blue, Cyan, Magenta or Yellow). |
| `blinder` | Only a dimmer, or white or other non-chromatic colour. |
| *skipped* | None of these ("nothing to program"), or a type the library cannot resolve ("not resolvable"). |

**Mirrored pairs** (`pair_mirrors`). Two lanes of one fixture type, mode and rigging that stand on opposite sides of
the rig's middle are paired, the closest mirror images first. The middle is half-way across everything the rig hangs,
fog machines aside. A lane within `MIDDLE_SHARE` (10 %) of the rig's width from the middle stands *in* the middle and
is never paired. Twins on one address count at every position they occupy, so a pair of twins can stand in the middle.

**Units** (`Director.units`). To the eye, a mirrored pair is one fixture, so a pair is one **unit**; any other lane is
a unit on its own. A role's units are ordered from the middle of the stage outwards. Turns, chases and hits all run on
units, which is why they never split a pair.

`clashes()` warns about different fixtures whose DMX ranges overlap.

### 4.2 Colours

A palette is three hues and white, worn by every fixture: **primary** (what most of the song wears), **secondary**
(its partner) and **contrast** (kept for choruses, bridges and accents). White is for the hits. A take names its
palette with a **spec string**, resolved by `colours.palette(spec, seed)`:

| Spec | Meaning |
|---|---|
| `ember` | A preset from `PALETTES`, mixed by hand for LEDs. |
| `triadic:azure` | `MODE:HUE`. One of the fixture Palette tab's modes (complementary, analogous, triadic, color+white, monochromatic, single), walked from one of its 12 hues. |
| `triadic:random` | The same, with the hue drawn from the take's seed. The resolved spec names the drawn hue, so a draw the user likes can be pinned. |
| `custom:#rrggbb,#rrggbb,#rrggbb` | Primary, secondary and contrast, picked by hand. |

| Preset | Primary | Secondary | Contrast |
|---|---|---|---|
| acid | lime | green | magenta |
| candy | pink | lavender | mint |
| ember | amber | red | ice |
| fire | red | orange | gold |
| forest | green | teal | gold |
| ice | ice | cyan | white |
| neon | magenta | purple | cyan |
| ocean | blue | teal | amber |
| royal | purple | blue | gold |
| sunset | orange | crimson | purple |

**Auto palette** (`mood_palette`). With no palette given, the song's key and drive choose one. A song is busy when at
least 40 % of its sounding bars are HIGH. A minor key gets `ember` when busy and `ocean` when not; a major key gets
`neon` when busy and `sunset` when not.

**Which colours a material wears.** Each material gets two of the three colours, its lead and its partner, from the
part it first plays (`PART_COLOURS`):

| Part | INTRO | VERSE | CHORUS | BRIDGE | INST | SOLO | BREAK | OUTRO |
|---|---|---|---|---|---|---|---|---|
| Lead | secondary | primary | primary | contrast | secondary | contrast | secondary | primary |
| Partner | primary | secondary | contrast | secondary | contrast | primary | secondary | secondary |

When another material already wears that pair, the new one wears it the other way round.

### 4.3 Looks

A **look** is one state of a fixture. `Voice` sorts a fixture's channels by what their capabilities do: master dimmer,
colour cells, shutter, pan/tilt with their travel, gobo wheel, rotation, prism, zoom, fog, effect selector and effect
speed. `Voice.judge()` reads any frame as a `Look`, measuring:

- `level`: how much light it gives, 0–1.
- `white`: whether its colour is unsaturated.
- `palette`: whether R, G and B are all at full. Such a scene *hands its colour to the palette*: it wears the
  section's colours instead of its own.
- `strobe`: its strobe speed, if it strobes.
- `animated`: whether it runs one of the fixture's built-in animations.

Every value comes from the capability the channel's DMX value selects, never from a scene's name or the fixture's name.

`Looks` gathers what a lane can show:

- **The library first.** A lane's candidate looks are its fixture's library scenes, plus the steps of its chases.
  Scenes and chases tagged (`song_parts`) for the section's part come first. With nothing tagged for a part, the lane
  follows `PART_FALLBACK` (for example CHORUS → INST → SOLO).
- **Ranking** (`Looks.ranked`). Each candidate scores
  `tag + 0.5 (library) + 3 × palette fit + (1 − |level − tier level|) + 0.3 (animated, from BAND up)`. The tag term
  is 2 for a scene tagged for the part, 1 for a related part, 0 for any other library scene and −0.5 for a built look.
  **Colour fit weighs the most**, so a scene tagged for the part but in the wrong colours loses to a built look in the
  palette's colours. A look fitting the palette under `MIN_FIT` (0.3) is dropped whenever anything fits. **A blinder
  plays only what its library tags for the part itself.**
- **Built looks** (`made_pool`) fill in where the library has nothing. They are frames in the palette's colours at the
  tier's level, a split version for fixtures with several emitters, and for movers beams swung either side of
  straight down (with gobo and prism at FULL and above).
- **Special looks.** `hit()` is the brightest steady look, white preferred; if the library's best gives under
  `HIT_MIN` (0.6), it is built (for a mover, wide with prism). `strobes()` returns strobing looks slowest first: the
  library's, else shutter strobe frames, else a strobe animation. `near(level)` is a plain look of about that level.

All DMX values are even and at most 254, because a clip carries `value / 2` as its velocity.

### 4.4 Planning a section

`Director.plan()` gives every section a `Plan`.

**Tier**: how much light the section asks for.

| Tier | Given to | Lights |
|---|---|---|
| `GLOW` | A QUIET section that opens the song, comes after the climax, or has energy under `glow_energy` | The lead role's middle unit, at `glow_scale` of its look |
| `LEAD` | Any other QUIET section | The lead role |
| `BAND` | A MID section, and anything loud after the climax | A cast of `BAND_SHARE` of the rig |
| `FULL` | A HIGH section | A cast of `FULL_SHARE` of the rig |
| `PEAK` | The climax, and every CHORUS at MID or louder before it | The whole cast except the reserve; the climax adds the reserve |

**Chosen once per material**, so a part looks the same wherever it comes back:

- **Lead role.** A role whose library tags looks for the part, else any role except the blinders. The seed picks among
  them.
- **Colours**, from §4.2.
- **Choreography**: how the cast takes turns.

| Choreography | What the cast does |
|---|---|
| `together` | Every unit lit on one look. |
| `in turn` | Every unit lit, on looks one step apart. |
| `alternate` | Every other unit lit, swapping each step. |
| `chase` | One unit at a time, from the middle out. |
| `stack` | One more unit lit at each step. |

A material first heard at BAND or below draws from the *moving* choreographies (alternate, chase, stack, in turn). A
material first heard loud draws from the *filling* ones (together, in turn, alternate). It never draws the
choreography of the material just before it. The pattern is then fitted to the tier and the cast size (`shape()`):

- GLOW, PEAK and a section holding a single look keep the whole cast lit.
- FULL alternates only with 4 or more units.
- A chase needs 3 units and alternating or stacking needs 2.

The result is **memoised per (material, tier)**, so a part keeps the choreography it first played even when its cast
grows later. Otherwise a grown cast would turn an alternation into a chase.

**The arc.** `arc` runs from 0 at the first section to 1 at the climax (1 after it).

**Reveal order and reserve** (`reveal_order`). The roles come in the order the sections before the climax lead them,
then any remaining among wash, pixel and mover. On a rig of `RESERVE_MIN` (3) units or more, blinders aside, the
**reserve** is held back: the outermost unit of the last role in the order that has two or more units. It stays dark
until the climax, so the climax is the first time the whole stage is lit. The reserve is never a whole role, because
holding a whole role left an LED bar dark for two thirds of a song.

**The cast** (`cast_of`) is a prefix of one order: the section's lead role's units, then the other roles in reveal
order, then the blinders. Units with nothing lit to play are skipped, and so is the reserve before the climax.

- GLOW and LEAD light only the lead role.
- PEAK lights the whole order.
- BAND and FULL light `low + (high − low) × arc` of it, where (low, high) is `BAND_SHARE` (0.35 → 0.7) or
  `FULL_SHARE` (0.67 → 1.0). That is at least 2 units, and 3 for a chasing BAND section.

Because it is always a prefix of the same order, **a part coming back lights a superset of what it lit before.**

### 4.5 Pace and Feel

**Pace** (0–8, `PACE_STEPS`, default 4) sets **how often things change**: the look interval, the turns (4 per look,
`STEPS_PER_LOOK`), how many accents are kept, how many steps a build climbs and how often the climax fills. It never
sets variety. One step is one power of two at PEAK. `look_beats()` is the curve; in 4/4 it gives:

| Pace | Glow | Lead | Band | Full | Chorus (peak) | Climax |
|---|---|---|---|---|---|---|
| 0 | section | section | section | section | section | section |
| 1 | section | section | 16 bars | 16 bars | 16 bars | 8 bars |
| 2 | 16 bars | 16 bars | 16 bars | 16 bars | 8 bars | 4 bars |
| 3 | 16 bars | 16 bars | 8 bars | 8 bars | 4 bars | 2 bars |
| **4** | 16 bars | 8 bars | 8 bars | 4 bars | 2 bars | 1 bar |
| 5 | 8 bars | 8 bars | 4 bars | 2 bars | 1 bar | 2 beats |
| 6 | 8 bars | 4 bars | 2 bars | 2 bars | 2 beats | 1 beat |
| 7 | 4 bars | 4 bars | 2 bars | 1 bar | 1 beat | ½ beat |
| 8 | 4 bars | 2 bars | 1 bar | 2 beats | 1 beat | ½ beat |

"Section" means one look holds the whole section. The climax always changes twice as often as a chorus, down to half a
beat, which is why the look grid is fractional. At every pace, each section still starts a look of its own and lights
its own cast, and the impacts, builds and strongest accents still land.

**Style** holds the director's other movable choices: `glow_energy` (0.2), `glow_scale` (0.5) and `cycle` (4, the most
looks one lane steps through in a material).

**Feel** is a preset over the pace and the `Style`, and nothing else (never the analysis or the seed):

| Feel | Pace | Looks per part | Glow below | Glow level |
|---|---|---|---|---|
| Still | 0 | 1 | 0.4 | 30 % |
| Calm | 2 | 2 | 0.3 | 40 % |
| **Steady** (the defaults) | 4 | 4 | 0.2 | 50 % |
| Busy | 6 | 6 | 0.1 | 60 % |
| Frantic | 8 | 8 | 0.0 | 70 % |

**Seed.** The seed picks the take: other lead roles, other look orders, other choreographies. The default is
`crc32(title)`, the song's own take. In the dialog, Take 1 is that default and Take N uses seed N.

### 4.6 From plan to cues

`Director.direct()` writes each lane's cues, in audio seconds.

**A section's own looks** (`base`):

- **Which looks.** Each lane steps through its *cycle*: the best-ranked looks for (material, tier), shuffled by the
  seed among those within 0.75 of the top score, and cut to `cycle` looks (to 1 when the section holds a single look).
  The cycle is chosen once per (lane, material, tier). **A mirrored pair shares one cycle**, keyed on the pair's lower
  channel, and never gets two shuffles of the shared rng.
- **When the lane joins.** A lane in the cast lights from the section start. In a GLOW section, the rest of the lead
  role joins at the second phrase (when pace > 0). In a BAND section, the supporting roles join at the second phrase
  (when pace > 0). A lane out of the cast is dark.
- **Steps.** The look changes on the section's grid, every `look_beats` beats; a build bar belongs to the build. The
  choreography turns units on and off at every step.
- **Colours.** At every look change, the lane alternates between the material's two colours. In patterns that are not
  unison, each unit is offset by its index, so neighbouring units wear the other colour and the next look.
- **Continuity.** A section never opens on the frame the previous one closed with. A LEAD or GLOW section fades its
  first look in when it opens the song (up to 3 s) or when a louder section ends into it (up to 2 s).
- **Dark chase steps** play only when looks change at least every `PULSE_BEATS` (2) beats.

**Hits and overrides.** Each one is a cue with a priority and a hold. `flashed(k)` decides which lanes a flash
reaches: before the climax, only the section's cast plus the blinders, so the rig grows with the song; from the climax
on, every light.

| Cue | When | Which lanes | Blinders | Shape |
|---|---|---|---|---|
| Entrance | The band's first section at BAND or above, and every step up to FULL. Skipped if an impact already falls within a beat. | `flashed` | Only in the last stretch | IMPACT: the hit look for 2 beats, dying away unless the section is HIGH |
| Landing | The climax, and every loud chorus coming in (PEAK after a non-PEAK section) | `flashed` | After a build, or in the last stretch | A hit, unless an analysed impact is already within half a beat. When a build or impact leads in, the half beat before is a blackout **breath** |
| Build | Every build bar, on the lights of the section it leads into | `flashed` | Strobe only on the build's last beat, only in the last stretch | BUILD. `round(beats_per_bar × pace / 8)` steps, at least 1: one step holds a strobe or swells, more steps climb in brightness, then strobe |
| Impact | Every analysed impact (the song's own hits) | `flashed` | Yes | IMPACT. Blinders hold 1.6 s, then fade over 1.3 s |
| Accent | The kept accents of quiet passages: strongest first, at least `4 × (1 − pace / 8)` bars apart | Washes (else pixels, else movers); in a BREAK, every lit role. Never the reserve | Never | ACCENT. The hit look in the contrast colour, 0.45 s plus a 0.35 s fade, going round the units |
| Climax fill | The second half of the bar before every 8th bar line (pace below 4) or 4th (pace 4 and up) of the climax, and before its end | Every light | Its last beat only | IMPACT. The fastest strobe, or a white hit |
| Stop | Every analysed band stop | All but fog | — | STOP blackout |
| Fog | A quiet opening (2.5 bars), each build (plus a beat), each BREAK (1.5 bars), ±1 bar around each landing. Windows less than 3 s apart merge | Fog lanes | — | BASE |
| Finale | A cut or ring ending: from the last hit to the end, ringing out to black on a ring | All but fog | No longer than a normal hit | FINAL |
| Tail | A fade ending: the last run of quiet bars burns down whatever is showing | All but fog | — | IMPACT with a fade-out |
| END | The end (when the sound cuts, else the end of sound) | Every lane | — | FINAL blackout |

The **last stretch** (`last_stretch`) starts at the last loud chorus at or before the climax, extended back over the
loud choruses directly before it. If no chorus comes before the climax, it starts at the climax. It is the only place
the blinders do more than the song's own hits (§5.6).

**Priorities** run `BASE < BUILD < ACCENT < IMPACT < STOP < FINAL`. `cues.resolve()` settles each lane's list:

- A cue inside a stronger cue's hold window is dropped.
- When a window closes and nothing lands there, the section's latest base look comes back.
- Cues closer than `MIN_GAP` (0.102 s) keep only the stronger one.

### 4.7 Writing the `.mid` (`writer.py`)

`writer.program()` builds the rig, runs the director, and authors everything into a fresh `MidiManager` inside **one
`bulk_edit()`**. Rebuilding the `.mid`, notifying listeners and re-deriving fade ramps happen once at the end instead
of once per edit, which is the difference between minutes and seconds per song.

- **Time.** Each cue's time gets the analysed track's offset (`guide_offset` or `offset`), turning audio seconds into
  timeline seconds. A track that starts before the timeline (a negative offset) puts cues before 0. Only the latest
  of those is kept, placed at timeline 0, because it is what is lit at 0; stacking the rest on 0 would make one clip
  of several.
- **Blackout** → a note 50 (`BLACKOUT_FIXTURE`) clip, written only when something was lit.
- **Nothing changed** since the lane's last cue → nothing is written.
- **One channel changed** (among the first 23 slots), with no fade → a channel-offset clip (note slot + 1, velocity
  value / 2). **A one-channel look is always a clip, never a scene**: a scene is two or more notes at one instant,
  and a one-note "scene" reloads as loose clips.
- **Anything else** → a scene (`add_scene`).
- **Fades** are set after every clip is placed, when each scene's visible width is known:
  - A ramp must last at least `MIN_FADE` (0.25 s). Shorter ramps have too few steps for the load-time detector, and
    reload as clips.
  - It ends half-way between two 100 ms steps (`safe_fade`). A ramp ending just past a step writes two notes
    milliseconds apart, which the detector reads as a duplicate.
  - It keeps `FADE_MARGIN` (0.15 s) clear of its clip's edges, or the detector reads the neighbouring note as part of
    the ramp.
  - A fade-out lasts at most `velocity / 6` seconds. A gentler one starts with steps that round to its starting
    level; on load that reads as a flat run, and its first notes come back as clips.

  These limits exist because a generated `.mid` must **reload exactly as authored**.
- **Song parts.** Notes 61–69 go on the arrangement band from `SongMap.parts()`, with END at `end_start`.
- **Kept from the song's current `.mid`** (`lane_state`): each lane's palette (note 60), mute and lock. **Auto mode
  stays off on every lane**, because every lane is programmed by hand.
- **Tempo.** The file carries the song's own BPM when it has one, else the detected BPM rounded. The tempo is written
  before any event: an empty track defaults to 120, and exporting at another BPM would rescale and round every delta.

---

## 5. Rules not to break

These are deliberate design decisions. Most were the user's explicit calls and replaced an earlier behaviour that
looked wrong on stage. Change them only on purpose.

### Show design

1. **Roles and looks come from capabilities, never from a fixture's name.** Shows run different rigs, so a fixture
   straight from OFL must work with no sidecar. The library's scenes, tagged by song part, come first.
2. **A mirrored pair is one fixture to the eye.** One side lit alone looks like a mistake. A pair shares one look
   cycle (never two shuffles of the shared rng), joins together and takes every hit together. It splits only as an
   *answer*: hits within `ANSWER_BARS` (half a bar) of each other, in a material whose choreography is not unison, go
   round the sides left to right. A glow lights the lead role's middle **unit** (a centred lane or the innermost
   pair), never `role[len // 2]`, which on two fixtures is the right-hand one.
3. **The show is an arc to its climax, and a part keeps its choreography.** Not every fixture is on for the whole show,
   alike parts are lit alike, and the show has a strong start, an escalating middle and a climax. Casts are prefixes
   of one order, so a repeat lights a superset of what it lit. The choreography is memoised per (material, tier). **The
   reserve stays dark until the climax, and every flash reaches the rig through `flashed()` / `held()`.** A new kind
   of hit that lights `self.lanes` directly spoils the reveal.
4. **A loud chorus is lit as the climax is, and the climax stays above it.** Every chorus at MID or louder before the
   climax is PEAK: the whole cast but the reserve, landing on a hit with a breath. The climax keeps three things no
   chorus has: the reserve, looks changing twice as often, and strobe fills into its phrase lines (at IMPACT priority,
   so nothing cuts a fill short).
5. **The chorus climax a song ends on is its OUTRO, and END starts where it ends.** This applies only to a CHORUS
   climax followed by at most `TAIL_BARS` quiet bars; a song that plays on after its climax keeps its names. It is a
   **marker only**: `SongMap.parts()` and `end_start` name the arrangement band (writer, dialog, reports), while
   `Section.part` stays what the music plays and is all the director reads. A climax lit as an OUTRO would pick the
   library's OUTRO scenes.
6. **The blinders are rationed.** They fire on the song's *own* hits (analysed impacts, the landing after a build) and
   nowhere else: no kicks, no snare, no accents, and no hit the show adds (`impact(blinders=False)`). From
   `last_stretch()` on (the last chorus run, the climax, the outro) they may also strobe, in one-beat bursts. **A new
   hit that passes `blinders=True` by default breaks this.**
7. **One palette per song.** More choices of palette, not more colours per song. The fixture palette's modes are
   **borrowed maths only** (`generate_song_colors`): the show writes its colours into its own looks and never touches
   note 60 or the controller's live colour cycling.

### Engineering

8. **Generating never touches the show.** A take runs on a worker thread, against a copy of the patch, writing a `.mid`
   in a temporary folder. Only Apply writes, behind a pinned snapshot.
9. **Every time is in audio seconds until `writer`.**
10. **No scipy** (§3.5).
11. **The dialog's Advanced values are the engine's own tunables**, `analysis.Tuning` and `director.Style`, whose
    defaults are the module constants. Parameters the design mock proposed but the engine lacks were not invented. At
    the defaults the app's output is the CLI's, byte for byte.
12. **The Feel is the base.** Advanced's change marks, its "N changed" count and Reset all measure against the selected
    feel, and moving the slider writes the feel's values over any edits to them.
13. **`replay` drives a `DMXController` built with its own `clock` and `rng`**, through `tick_timers()` and
    `tick_frame()`, and never patches `software_controller.main`, because the live controller runs in the same
    process.

---

## 6. In the app

### `AutoProgrammer` (`app/autoprogram.py`)

`AppCore` owns one `AutoProgrammer`, and `AppCore.shutdown()` calls its `cleanup()`, which stops a running take and
removes the temp folder.

- **One take at a time.** A `_Job` QThread runs `take.analyse()` and `take.program()`. A take asked for while one runs
  **supersedes** it: the running job is interrupted at its next step and the new one starts once it stops. A dropped
  job announces nothing, but an analysis it finished is still cached.
- **Analysis cache.** Analyses are keyed on `(track, path, file mtime + size, song tempo, meter, Tuning)`, and the 8
  most recent (`ANALYSES_KEPT`) are kept. A change of palette, pace, take or `Style` therefore only programs again,
  about 0.3 s instead of seconds.
- **What the song keeps.** For the loaded song, the per-lane palette, mute and lock come from the loaded document,
  unsaved edits included. For any other song they are read from its file, on the worker.
- **Apply** runs on the Qt thread:
  1. Stop playback, if the take is for the current song.
  2. `ShowManager.flush()`, so the latest hand edits go into the snapshot.
  3. Take a pinned snapshot named `before auto-program — <title>`.
  4. `ShowManager.write_song_midi()`.
  5. For the current song, reload the document inside `begin_song_load()` / `end_song_load()`, so the autosave is not
     armed, and set the BPM from the reloaded file. Any other song only has its file replaced.
- **Desktop only.** The web remote does not offer it.

### The dialog (`ui/autoprogram_dialog.py`)

The dialog is modal, like the playlist manager, and the main window closes it if the show is reloaded underneath it.

- **Inputs.** The song; the audio to analyse (Guide or Backing); the palette (Auto, the presets, the six modes with a
  hue picker, or Custom with three swatches); the **Feel** slider with a pace readout ("chorus every 2 bars · climax
  every bar", computed by `look_beats` in the song's own meter); and **Advanced**, with the Analysis fields (§3.4) and
  the Generation fields (Pace, Take, Glow level, Glow below, Looks per part).
- **Requests.** Edits to the inputs are debounced by 300 ms. The take on show is always the one the inputs describe: a take that
  arrives for other inputs is ignored. An analysis in progress shows its Decode → Analyse → Program steps with times;
  a program-only change keeps the song map on show.
- **Output.** A summary (song tempo and meter, detected BPM, grid fit, bars, first downbeat, sound until), the song map
  strip painted from the `SongMap`, a sections table, a fixtures table (role chips, mirrors, twins, scene and clip
  counts, skipped fixtures), and notices (analysis warnings, DMX clashes, skipped fixtures). The footer gives the event
  and fade counts.
- **Apply** opens a Replace confirmation that lists what is replaced (every clip, scene and fade on every lane; the
  song sections; the tempo, when the song had none of its own) and what is kept (palette, mute and lock; audio,
  offsets, tempo markers and voice cues), and names the snapshot. **Export** copies the take's `.mid` elsewhere and
  leaves the show unchanged.

---

## 7. Command line

```bash
uv run python scripts/autoprogram_cli.py live AUDIO [--no-audio]
uv run python scripts/autoprogram_cli.py analyse AUDIO [--bpm N] [--meter 4/4] [--bars]
uv run python scripts/autoprogram_cli.py program SHOW_DIR SONG OUT.mid \
        [--track guide|backing] [--palette SPEC] [--pace 0-8] [--seed N]
uv run python scripts/autoprogram_cli.py score SHOW_DIR [SONG ...] [--out DIR] \
        [--px-per-second N] [--from S] [--to S]
```

- `live` plays a file through the first external player found while printing the live analysers. `--no-audio` runs
  silent at full speed.
- `analyse` prints the `SongMap` (`describe()`).
- `program` writes a take to `OUT.mid` and prints the map and the program report. **It never writes the show.** To use
  the result: close the show in the app (its autosave would write the old program back), take a version, and copy
  `OUT.mid` over the song's `.mid`. Or use the dialog instead.
- `score` draws each song's current `.mid` as `DIR/<title>.png`.

`SONG` is a title or a 1-based setlist position. The CLI loads the show with `store.load_show`, which switches the
global fixture-defs layer and may create folders, so point it at a copy when in doubt.

---

## 8. Replay and the light score

`replay.replay()` shows the light a `.mid` makes without playing it:

- The `.mid` is played into the app's own software controller the way playback plays it: auto flags applied, muted
  lanes skipped, every note handed to `handle_note_on` at its time.
- It runs on a simulated clock, one frame every 1/20 s (`DMX_UPDATE_HZ`), with a seeded rng, so a song replays the same
  every time.
- It idles one second before t = 0. A fresh controller runs a start-up blend that would wipe the song's first notes;
  the live app never sees that blend, because its controller started long before playback.
- Each frame's DMX is read back through `core.fixture_state`, the model the stage draws from.

`score.py` draws the result as a PNG. Each fixture is a strip (fixtures on one address share a strip), with one band per
emitter in its colour and brightness. The song parts run above and the audio's loudness below. White hatching marks a
strobe, a yellow line a running built-in animation, and grey the fog output. The CLI's `score` command uses it; the app
does not.

---

## 9. Tests

- `tests/test_autoprogram.py` synthesises two songs at 120 BPM whose structure is known. One has quiet bars, a build,
  loud bars and an outro. The other has an intro, verse, chorus, verse with a stop, chorus, and a cut ending. The tests
  programme them onto the shipped fixtures and check, among other things:
  - The analysis finds the grid, downbeat, build, drop, sections, repeats, climax, stop and ending.
  - Roles come from capabilities and looks from the library.
  - A repeat is lit like what it repeats, in the palette's colours, with darkness at sparse moments.
  - Mirrored pairs light as one, and the cast takes turns.
  - The arc builds to the climax, and the blinders keep to the song's hits.
  - The replayed `.mid` shows what was programmed.
  - A generated show survives a save and a reload unchanged.

  It also runs standalone: `uv run python tests/test_autoprogram.py`.
- `tests/test_autoprogram_app.py` covers `AutoProgrammer`: worker thread, analysis reuse, superseding, and applying to
  the current song versus another song. It also covers the dialog: only the take its inputs describe is shown or
  applied, the palette choices, Feel over the Generation values, the pace readout in the song's meter, and the failure
  states.

---

## 10. Known limits and open items

- **One tempo per song.** Medleys and real tempo changes get a weak-grid warning, not a piecewise grid. The app's
  tempo markers could carry one.
- **Voice detection misses**, observed on the tuning songs: instruments with vibrato (a Twin Peaks-style guitar) can
  read as voice, and songs built on 7-bar phrases fight the 8-bar grid.
- **Mirrored movers get identical pan** (parallel beams), not mirrored pan (a fan). Offered as a follow-up, not done.
- **Generating while the transport plays is allowed**, and the audio feed under that CPU load has not been measured.
- **Hand-made fades can still hit the reload issues** that the writer's fade limits avoid (§4.7). The generator steers
  clear of them; the app's fade detector itself is unchanged.
