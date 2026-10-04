"""
autoprogram's analysis and show generator on music whose structure is
known, programmed onto the shipped fixtures. Two synthetic songs at 120 BPM:

  * six quiet bars, a bar that crescendos, eight loud bars, three quiet bars,
    then silence;
  * a quiet intro, a verse, a chorus, the verse again with the band stopping
    for half a bar, the chorus again, and a last hit cut dead.

The analysis has to find the grid, the downbeat, the build, the drop it lands
on and the sections — which of them repeat, the climax, the stop and how the
song ends; every fixture has to get its role from its capabilities and its
looks from its library's scenes; a repeat has to be lit like what it repeats,
in the palette's colours, with fixtures left dark where the music is sparse,
two of a type either side of the stage lit as one, and the whole rig on the
climax and the last hit — also when the .mid is
played into the software controller (autoprogram/replay.py); the
pace has to set how often looks change without losing the sections or
the drop; and the generated show has to come back from a save and a reload
unchanged.

Run with:
    uv run python tests/test_autoprogram.py
"""

import bisect
import os
import sys
import tempfile
from dataclasses import replace

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from autoprogram import analysis as sa  # noqa: E402
from autoprogram import colours, looks, replay, writer  # noqa: E402
from autoprogram.cues import ACCENT, BASE, BUILD, IMPACT, MIN_GAP, resolve  # noqa: E402
from autoprogram.director import (ALTERNATE, BLINDER_HOLD, CHASE, DEFAULT_FEEL,  # noqa: E402
                                  DEFAULT_PACE, FEELS, FILLING, IN_TURN, MOVING, STACK,
                                  TOGETHER, Director, Feel, Style, build_rig, clashes)
from core import fixture_library as fl  # noqa: E402
from core import note_layout  # noqa: E402
from core.config import FixtureInstance  # noqa: E402
from midi.manager import MidiManager  # noqa: E402

SR = 44100
BPM = 120
BEAT = 60 / BPM
BAR = 4 * BEAT
FIRST_DOWNBEAT = 0.5
QUIET, LOUD, OUTRO = 6, 8, 3           # bars, around the one build bar
BUILD_BAR = QUIET + 1                  # 1-based
DROP = FIRST_DOWNBEAT + QUIET * BAR + BAR


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _song() -> np.ndarray:
    rng = np.random.default_rng(7)
    bars = QUIET + 1 + LOUD + OUTRO
    x = np.zeros(int((FIRST_DOWNBEAT + bars * BAR + 2.5) * SR))

    def add(at, signal):
        i = int(at * SR)
        x[i:i + len(signal)] += signal[:len(x) - i]

    t = np.arange(int(0.3 * SR)) / SR
    kick = np.sin(2 * np.pi * 55 * t) * np.exp(-t * 18)
    snare = rng.normal(0, 1, len(t)) * np.exp(-t * 25)
    hat = rng.normal(0, 1, int(0.03 * SR)) * np.exp(-np.arange(int(0.03 * SR)) / SR * 150)
    bar_t = np.arange(int(BAR * SR)) / SR
    for b in range(bars):
        start = FIRST_DOWNBEAT + b * BAR
        loud = QUIET + 1 <= b < QUIET + 1 + LOUD
        if b == QUIET:                                        # the build
            add(start, rng.normal(0, 1, len(bar_t)) * (bar_t / BAR) ** 2 * 0.12)
        elif loud:
            add(start, 0.12 * np.sin(2 * np.pi * 220 * bar_t))
            for k in range(4):
                add(start + k * BEAT, (0.9 if k % 2 == 0 else 0.0) * kick)
                add(start + k * BEAT, (0.5 if k % 2 else 0.0) * snare)
                add(start + k * BEAT, 0.2 * hat)
                add(start + (k + 0.5) * BEAT, 0.15 * hat)
        else:
            add(start, 0.05 * np.sin(2 * np.pi * 330 * bar_t))
            add(start, 0.25 * kick)
            for k in range(4):
                add(start + k * BEAT, 0.08 * hat)
    return (x / np.abs(x).max()).astype(np.float32)


_SONG = None


def _analysis() -> sa.SongMap:
    global _SONG
    if _SONG is None:
        _SONG = sa.analyse_samples(SR, _song(), BPM)
    return _SONG


# The second song: (part, bars), the bar whose second half the band stops for
# (0-based), and the chords each part cycles through, one a bar.
PARTS2 = [("intro", 4), ("verse", 8), ("chorus", 8), ("verse", 8), ("chorus", 8)]
STOP_BAR = 4 + 8 + 8 + 4
STOP_AT = FIRST_DOWNBEAT + STOP_BAR * BAR + 2 * BEAT
LAST_HIT = FIRST_DOWNBEAT + sum(n for _p, n in PARTS2) * BAR
CLIMAX2 = FIRST_DOWNBEAT + sum(n for _p, n in PARTS2[:4]) * BAR   # the last chorus
CHORDS = {"intro": [(220.0, 261.6, 329.6)],
          "verse": [(220.0, 261.6, 329.6), (174.6, 220.0, 261.6), (261.6, 329.6, 392.0),
                    (196.0, 246.9, 293.7)],
          "chorus": [(293.7, 370.0, 440.0), (329.6, 415.3, 493.9)]}


def _song2() -> np.ndarray:
    rng = np.random.default_rng(11)
    bars = sum(n for _p, n in PARTS2)
    x = np.zeros(int((FIRST_DOWNBEAT + (bars + 1) * BAR + 2.0) * SR))

    def add(at, signal):
        i = int(round(at * SR))
        x[i:i + len(signal)] += signal[:max(0, len(x) - i)]

    bar_t = np.arange(int(BAR * SR)) / SR
    t = np.arange(int(0.3 * SR)) / SR
    kick = np.sin(2 * np.pi * 55 * t) * np.exp(-t * 18)
    snare = rng.normal(0, 1, len(t)) * np.exp(-t * 25)
    hat = rng.normal(0, 1, int(0.03 * SR)) * np.exp(-np.arange(int(0.03 * SR)) / SR * 150)
    crash = rng.normal(0, 1, int(1.2 * SR)) * np.exp(-np.arange(int(1.2 * SR)) / SR * 3)
    b = 0
    for part, n in PARTS2:
        for k in range(n):
            start = FIRST_DOWNBEAT + b * BAR
            freqs = CHORDS[part][k % len(CHORDS[part])]
            amp = {"intro": 0.03, "verse": 0.05, "chorus": 0.08}[part]
            pad = sum(np.sin(2 * np.pi * f * bar_t) for f in freqs) * amp
            if part == "chorus":                         # brighter: the octave up
                pad += sum(np.sin(4 * np.pi * f * bar_t) for f in freqs) * amp * 0.5
            if b == STOP_BAR:
                pad[len(pad) // 2:] = 0
            add(start, pad)
            for q in range(4):
                if b == STOP_BAR and q >= 2:
                    continue
                at = start + q * BEAT
                if part == "intro":
                    add(at, 0.05 * hat)
                    continue
                loud = 1.0 if part == "chorus" else 0.55
                add(at, 0.9 * loud * kick if q % 2 == 0 else 0.6 * loud * snare)
                add(at, 0.12 * loud * hat)
                if part == "chorus":
                    add(at + BEAT / 2, 0.12 * hat)
            if part == "chorus" and k == 0:
                add(start, 0.25 * crash)
            b += 1
    cut = np.linspace(1, 0, int(0.25 * SR))                 # the last hit, cut dead
    add(LAST_HIT, (0.9 * kick + 0.6 * snare)[:len(cut)] * cut)
    add(LAST_HIT, 0.3 * crash[:len(cut)] * cut)
    return (x / np.abs(x).max()).astype(np.float32)


_SONG2 = None


def _analysis2() -> sa.SongMap:
    global _SONG2
    if _SONG2 is None:
        _SONG2 = sa.analyse_samples(SR, _song2(), BPM)
    return _SONG2


def test_the_analysis_finds_the_structure():
    print("test_the_analysis_finds_the_structure")
    song = _analysis()
    _assert(abs(song.bpm - BPM) < 0.3, f"tempo {song.bpm:.2f} BPM")
    off = (song.first_downbeat - FIRST_DOWNBEAT) % BAR
    _assert(min(off, BAR - off) < 0.03, f"downbeat on the bar line ({song.first_downbeat:.3f}s)")
    builds = [b.number for b in song.bars if b.build]
    _assert(builds == [BUILD_BAR], f"one build, bar {BUILD_BAR} (found {builds})")
    _assert(any(abs(at - DROP) < 0.08 for at, _ in song.impacts),
            f"an impact on the drop at {DROP:.2f}s (found {[round(a, 2) for a, _ in song.impacts]})")
    parts = [(s.part, s.first, s.last) for s in song.sections]
    _assert([p for p, _f, _l in parts] == ["INTRO", "CHORUS", "OUTRO"], f"sections {parts}")
    _assert(parts[1][1] == BUILD_BAR + 1, "the loud section starts on the drop")
    _assert(abs(song.end_of_sound - (DROP + (LOUD + OUTRO) * BAR)) < 0.05,
            f"the sound ends at {song.end_of_sound:.2f}s")
    _assert(song.climax == 1, "the loud section is the climax")
    _assert([part for part, _s in song.parts()] == ["INTRO", "OUTRO", "END"]
            and abs(song.end_start - (DROP + LOUD * BAR)) < 0.05,
            f"the band marks the climax the song ends on its OUTRO, and END from where it ends "
            f"({song.end_start:.2f}s)")
    _assert(song.ending == "fade", f"a song dying away fades ({song.ending})")
    _assert(not song.warnings, f"no warnings {song.warnings}")


def test_the_analysis_finds_what_repeats():
    print("test_the_analysis_finds_what_repeats")
    song = _analysis2()
    off = (song.first_downbeat - FIRST_DOWNBEAT) % BAR
    _assert(min(off, BAR - off) < 0.03, f"downbeat on the bar line ({song.first_downbeat:.3f}s)")
    parts = [(s.part, s.material, s.first) for s in song.sections]
    _assert([p for p, _m, _f in parts][:5] == ["INTRO", "VERSE", "CHORUS", "VERSE", "CHORUS"]
            and [f for _p, _m, f in parts][:5] == [1, 5, 13, 21, 29], f"sections {parts}")
    intro, verse, chorus, verse2, chorus2 = song.sections[:5]
    _assert(verse.material == verse2.material and chorus.material == chorus2.material
            and len({intro.material, verse.material, chorus.material}) == 3,
            "the verses are one material, the choruses another, the intro a third")
    _assert(chorus.energy > verse.energy > intro.energy,
            f"energy {intro.energy:.2f} < {verse.energy:.2f} < {chorus.energy:.2f}")
    _assert(song.climax == 4, "the last chorus is the climax")
    _assert([part for part, _s in song.parts()][3:] == ["VERSE", "OUTRO", "END"]
            and abs(song.end_start - LAST_HIT) < 0.05,
            f"the band marks the last chorus its OUTRO, and END from its last hit "
            f"({song.end_start:.2f}s)")
    _assert(chorus.hits == [2.0, 4.0], f"the chorus leans on its snare, 2 and 4 ({chorus.hits})")
    _assert(len(song.stops) == 1 and abs(song.stops[0][0] - STOP_AT) < 0.1
            and abs(song.stops[0][1] - (STOP_AT + 2 * BEAT)) < 0.1,
            f"the band stops at {STOP_AT:.2f}s for half a bar ({song.stops})")
    _assert(song.ending == "cut" and abs(song.final_hit - LAST_HIT) < 0.05
            and song.cut - song.final_hit < sa.CUT_TAIL,
            f"the song ends cut dead on its last hit ({song.ending}, {song.final_hit})")
    _assert(not song.warnings, f"no warnings {song.warnings}")


def test_only_a_chorus_climax_the_song_ends_on_is_its_outro():
    print("test_only_a_chorus_climax_the_song_ends_on_is_its_outro")

    def outro(climax, *tail):
        """The outro of a verse, then the `climax` part, then `tail`: (level, bars) each."""
        secs = [sa.Section("VERSE", sa.MID, 1, 8, 0.0, 16.0), sa.Section(climax, sa.HIGH, 9, 16, 16.0, 32.0)]
        for level, bars in tail:
            first = secs[-1].last + 1
            secs.append(sa.Section("OUTRO", level, first, first + bars - 1, 0.0, 0.0))
        return sa._outro(secs, 1)
    _assert(outro("CHORUS") == 1 and outro("CHORUS", (sa.QUIET, 2), (sa.QUIET, 2)) == 1,
            f"a chorus climax ending the song, or with {sa.TAIL_BARS} quiet bars after it, is its outro")
    _assert(outro("CHORUS", (sa.QUIET, sa.TAIL_BARS + 1)) is None
            and outro("CHORUS", (sa.MID, 2)) is None,
            "not when the song plays on after it, longer or louder")
    _assert(outro("VERSE") is None, "nor when the climax is no chorus")


def _singer(sung) -> np.ndarray:
    """A voice for the second song, singing the bars (0-based) in `sung`: a
    new note every beat, gliding in from the last and wavering on it (a
    5-6 Hz vibrato of 40 cents), eight harmonics, breathing at the end of
    every second bar — and never the same line twice, the way a voice goes
    and a riff does not."""
    rng = np.random.default_rng(5)
    scale = 220.0 * 2 ** (np.array([0, 2, 3, 5, 7, 8, 10, 12]) / 12)
    bars = sum(n for _p, n in PARTS2)
    n = int((FIRST_DOWNBEAT + (bars + 1) * BAR + 2.0) * SR)
    freq, amp = np.full(n, scale[0]), np.zeros(n)
    beat = int(BEAT * SR)
    t = np.arange(beat) / SR
    note = scale[0]
    for b in sung:
        for q in range(4):
            i = int((FIRST_DOWNBEAT + b * BAR + q * BEAT) * SR)
            target = rng.choice(scale)
            glide = np.minimum(t / 0.06, 1.0)
            vibrato = 2 ** (40 / 1200 * np.sin(2 * np.pi * rng.uniform(5, 6) * t
                                               + rng.uniform(0, 2 * np.pi)))
            freq[i:i + beat] = (note + (target - note) * glide) * vibrato
            amp[i:i + beat] = 0.0 if b % 2 and q == 3 else 1.0
            note = target
    amp = np.convolve(amp, np.ones(441) / 441, mode="same")      # 10 ms in and out
    phase = 2 * np.pi * np.cumsum(freq) / SR
    return amp * sum(np.sin(h * phase) / h for h in range(1, 9))


def test_the_analysis_hears_the_voice():
    print("test_the_analysis_hears_the_voice")
    verse2 = range(4 + 8 + 8, 4 + 8 + 8 + 8)
    sung = [b for b in range(4, sum(n for _p, n in PARTS2)) if b not in verse2]
    x = _song2()
    x = x + 0.1 * _singer(sung)[:len(x)]
    song = sa.analyse_samples(SR, (x / np.abs(x).max()).astype(np.float32), BPM)
    parts = [(s.part, s.sung, s.first) for s in song.sections]
    _assert([p for p, _s, _f in parts][:5] == ["INTRO", "VERSE", "CHORUS", "BREAK", "CHORUS"]
            and [s for _p, s, _f in parts][:5] == [False, True, True, False, True]
            and [f for _p, _s, f in parts][:5] == [1, 5, 13, 21, 29],
            f"what the voice sings is a verse or a chorus, the verse it leaves out (quiet "
            f"beside the singing) a break ({parts})")
    intro, verse, _chorus, gap, _chorus2 = song.sections[:5]
    _assert(gap.material != verse.material and intro.material != verse.material,
            "the verse played without the voice is not the verse, nor is the intro")
    _assert(min(b.voice for b in song.bars if verse.first <= b.number <= verse.last) > 0.5
            > max(b.voice for b in song.bars if gap.first <= b.number <= gap.last),
            "every bar of the sung verse is sung, none of the other")


def test_roles_come_from_capabilities():
    print("test_roles_come_from_capabilities")
    expected = {("ledbar", ""): "pixel", ("blinder", ""): "blinder",
                ("par-rgbw", ""): "wash", ("moving-head", "12ch"): "mover",
                ("smoke-machine", "1ch"): "fog", ("hazer", ""): "fog",
                ("Wild Wash Pro 648 RGBW", "16Ch"): "wash"}
    for (name, mode), role in expected.items():
        fdef = fl.resolve_fixture(name, mode)
        _assert(looks.Voice(fdef).role() == role, f"{name} is a {role}")


def test_looks_come_from_the_library():
    print("test_looks_come_from_the_library")
    par = looks.Looks(looks.Voice(fl.resolve_fixture("par-rgbw", "")), "wash", True)
    _assert({look.name for look in par.tagged("INTRO")} == {"Deep blue", "Warm glow"},
            "a section plays the scenes tagged for its part")
    _assert([look.name for look in par.tagged("OUTRO")] == ["Deep blue", "Blackout"],
            "a part tagged only with strobes plays a related part's: OUTRO borrows END's chase")
    _assert(par.hit().name == "White wash" and par.hit().library,
            "the hit is the brightest white scene")
    _assert([look.name for look in par.strobes()] == ["Strobe white slow", "Strobe white fast"],
            "a build strobes through the white strobe scenes, slowest first")
    head = looks.Looks(looks.Voice(fl.resolve_fixture("moving-head", "12ch")), "mover", True)
    judged = {look.name: look for look in head.scenes.values()}
    _assert(judged["Park dark"].dark and judged["Strobe slow"].strobe is not None
            and judged["Palette down"].palette and judged["White wash"].white
            and not judged["White wash"].palette,
            "a scene is read from the capabilities its values select")
    bar = looks.Looks(looks.Voice(fl.resolve_fixture("ledbar", "")), "pixel", True)
    judged = {look.name.strip(): look for look in bar.scenes.values()}
    _assert(judged["STROBE fast"].strobe is not None and judged["Cuadrados"].animated
            and not judged["ON"].animated, "a strobing animation strobes, any other animates")
    _assert(all(look.name.strip() for look in bar.scenes.values()), "an unnamed slot is no look")
    bare = looks.Looks(looks.Voice(replace(fl.resolve_fixture("par-rgbw", ""), banks={}, chases=[])),
                    "wash", True)
    _assert(not bare.tagged("INTRO") and not bare.hit().library and bare.strobes(),
            "with no library, the looks are built from the capabilities")


def test_pace_sets_how_often_looks_change():
    print("test_pace_sets_how_often_looks_change")
    song = _analysis()
    director = lambda c: Director(song, [], colours.PALETTES["ember"], c)  # noqa: E731
    tiers = (looks.GLOW, looks.LEAD, looks.BAND, looks.FULL, looks.PEAK)
    _assert([director(0).pace(tier) for tier in tiers] == [None] * 5,
            "at 0 every section holds one look")
    _assert([director(8).pace(tier) for tier in tiers] == [16, 8, 4, 2, 1]
            and director(8).beats(song.climax) == 0.5,
            "at 8 a glow changes every 4 bars, the lead every 2, the band every bar, "
            "a loud section every 2 beats, a chorus every beat and the climax every half beat")
    few, all_ = director(0).accent_times, director(8).accent_times
    _assert(len(few) < len(all_) == len(song.accents)
            and all(b - a >= 4 * BAR - 1e-6 for a, b in zip(few, few[1:])),
            f"a slower show keeps fewer accents, spread out ({len(few)} of {len(all_)})")
    sizes, changes = [], []
    for pace in (0, 4, 8):
        mm, lanes, _notes, _counts, _fades, _director = writer.program(
            _patch(), song, colours.PALETTES["ember"], 0.0, BPM, pace=pace)
        sizes.append(len(mm.midi_events))
        par = next(lane for lane in lanes if lane.channel == 4)
        base = [c for c in par.cues if c.prio == BASE]
        changes.append(len(base))
        opening = [next(c.values for c in base if abs(c.t - sec.start) < 1e-6)
                   for sec in song.sections]
        _assert(all(a != b for a, b in zip(opening, opening[1:]) if a or b),
                f"pace {pace}: every section opens on a look of its own")
        _assert(any(c.name in {look.name for look in par.looks.scenes.values()} for c in base),
                f"pace {pace}: a par plays its library's scenes")
        lit = {e["channel"] for e in mm.midi_events
               if e["channel"] is not None and not note_layout.is_song_part(e["note"])
               and abs(e["time"] - DROP) < 0.1}
        _assert(lit == {0, 1, 3, 4, 5}, f"pace {pace}: every light hits the drop")
    _assert(changes[0] == len(song.sections), "at 0 a par changes look only where a section starts")
    _assert(changes[0] < changes[1] < changes[2] and sizes[0] < sizes[1] < sizes[2],
            f"a faster pace, more changes: {changes} looks, {sizes} events")


def test_the_feels_run_from_still_to_frantic():
    print("test_the_feels_run_from_still_to_frantic")
    _assert(FEELS[DEFAULT_FEEL] == Feel("Steady", DEFAULT_PACE, Style()),
            "Steady is the defaults, so the default show is the same with or without a feel")
    for song in (_analysis(), _analysis2()):
        events = [len(writer.program(_patch(), song, colours.PALETTES["ember"], 0.0, BPM,
                                     pace=feel.pace, style=feel.style)[0].midi_events)
                  for feel in FEELS]
        _assert(all(a < b for a, b in zip(events, events[1:])),
                f"each feel makes a busier show than the one before ({events} events)")


def test_the_tuning_and_the_style_reach_the_engine():
    print("test_the_tuning_and_the_style_reach_the_engine")
    song = _analysis()
    _assert(any(b.level == sa.HIGH for b in song.bars) and any(b.build for b in song.bars),
            "at the defaults the song has loud bars and a build")
    tuned = sa.analyse_samples(SR, _song(), BPM, tuning=sa.Tuning(high_min=1.5, build_rise=100.0))
    _assert(not any(b.level == sa.HIGH for b in tuned.bars),
            "with High above out of reach no bar is high")
    _assert(not any(b.build for b in tuned.bars), "with a far steeper Build rise there is no build")
    looks_per = []
    for style in (Style(), Style(cycle=1)):
        *_rest, director = writer.program(_patch(), song, colours.PALETTES["ember"], 0.0, BPM,
                                          pace=8, style=style)
        looks_per.append(max(len(cycle) for cycle in director.cycles.values()))
    _assert(looks_per[0] > 1 and looks_per[1] == 1,
            f"Looks per part caps what a lane cycles through ({looks_per})")


def _program2(palette="ember", pace=DEFAULT_PACE):
    mm, lanes, _notes, _counts, _fades, director = writer.program(
        _patch(), _analysis2(), colours.palette(palette)[1], 0.0, BPM, pace=pace)
    return mm, [lane for lane in lanes if lane.role != "fog"], director


def _base(lane, sec):
    """A lane's own looks through `sec`, as (time into it, frame)."""
    return [(round(c.t - sec.start, 3), c.values) for c in lane.cues
            if c.prio == BASE and sec.start - 1e-6 <= c.t < sec.end - 1e-6]


def _cast(plan):
    return {lane.channel for unit in plan.cast for lane in unit}


def test_a_repeat_is_lit_like_what_it_repeats():
    print("test_a_repeat_is_lit_like_what_it_repeats")
    song = _analysis2()
    _mm, lanes, director = _program2()
    _intro, verse, chorus, verse2, climax = song.sections[:5]
    plans = director.plans
    _assert(_cast(plans[1]) <= _cast(plans[3]) and plans[1].pattern == plans[3].pattern
            and all(_base(lane, verse) == _base(lane, verse2)
                    for lane in lanes if lane.channel in _cast(plans[1])),
            "the second verse lights what the first did, exactly as the first, with the same "
            "choreography")
    lit = [lane for lane in lanes if lane.channel in _cast(plans[2])]
    same = all({str(v) for _t, v in _base(lane, chorus)} <= {str(v) for _t, v in _base(lane, climax)}
               and _base(lane, chorus)[0] == _base(lane, climax)[0] for lane in lit)
    _assert(lit and same and _cast(plans[2]) <= _cast(plans[4]),
            "the climax opens like the chorus it repeats, on the same looks, and lights as much")
    _assert(plans[1].colours == plans[3].colours and plans[2].colours == plans[4].colours
            and plans[1].colours != plans[2].colours,
            "a material keeps its colours wherever it comes back")


def test_darkness_has_its_moments():
    print("test_darkness_has_its_moments")
    song = _analysis2()
    _mm, lanes, director = _program2()
    intro, verse = song.sections[:2]
    lit = [lane.channel for lane in lanes if any(v for _t, v in _base(lane, intro))]
    _assert(len(lit) == 1 and director.plans[0].tier == looks.GLOW,
            f"the song opens on a single fixture glowing ({lit})")
    blinder = next(lane for lane in lanes if lane.role == "blinder")
    _assert(not any(v for _t, v in _base(blinder, verse)), "the blinders sit a verse out")
    joins = [lane.channel for lane in lanes
             if _base(lane, verse) and _base(lane, verse)[0][1] is None
             and any(v for _t, v in _base(lane, verse))]
    _assert(joins, f"the band builds on its lead: a lane joins at the verse's second phrase, or "
                   f"on its turn {joins}")
    start, end = song.stops[0]
    cues = {lane.channel: resolve(lane.cues, director.end) for lane in lanes}
    shown = [lane for lane in lanes if _shown(lane, start - 0.01) or _shown(lane, end)]
    dark = shown and all(
        any(c.values is None and abs(c.t - start) < 0.01 for c in cues[lane.channel])
        and (not _shown(lane, end) or any(c.values and abs(c.t - (end - MIN_GAP)) < 0.02
                                          for c in cues[lane.channel]))
        for lane in shown)
    _assert(dark, "every light goes dark while the band stops, and comes back with it")


def test_the_climax_and_the_last_hit_take_the_whole_rig():
    print("test_the_climax_and_the_last_hit_take_the_whole_rig")
    song = _analysis2()
    _mm, lanes, director = _program2()
    chorus, climax = song.sections[2], song.sections[4]
    lit = next(lane for lane in lanes if lane.role != "blinder"
               and lane.channel in _cast(director.plans[2]))
    _assert(len(_base(lit, climax)) > len(_base(lit, chorus)),
            "the climax changes looks faster than the chorus it repeats")
    _assert(_cast(director.plans[4]) == {lane.channel for lane in lanes if lane.role != "blinder"},
            "the climax lights every fixture with a look to play")
    impact = next(at for at, _s in song.impacts if abs(at - climax.start) < BEAT)
    for lane in lanes:
        cues = resolve(lane.cues, director.end)
        if not any(c.values is None and impact - BEAT / 2 - 0.01 <= c.t < impact for c in cues):
            raise AssertionError(f"ch {lane.channel}: no breath before the climax")
    print("  ok: a blackout breath before the climax lands")
    for lane in lanes:
        cues = resolve(lane.cues, director.end)
        last_hit = [c for c in cues if abs(c.t - song.final_hit) < 0.01]
        if not (last_hit and last_hit[0].values and cues[-1].values is None
                and abs(cues[-1].t - song.cut) < 0.01):
            raise AssertionError(f"ch {lane.channel}: the last hit is not held to the cut")
    print("  ok: every light takes the last hit and goes dark where the sound is cut")


def test_the_colours_stay_in_the_palette():
    print("test_the_colours_stay_in_the_palette")
    for name in ("ember", "neon", "triadic:azure", "custom:#ff8800,#cc0022,#3355ff"):
        _mm, lanes, director = _program2(name)
        pars = [lane for lane in lanes if lane.role == "wash"]
        fits = [lane.looks.fit(lane.voice.judge("", v), director.hues)
                for lane in pars for sec in _analysis2().sections for _t, v in _base(lane, sec) if v]
        _assert(fits and min(fits) >= 0.5,
                f"{name}: every look of the pars sits in the palette (worst fit {min(fits):.2f})")


def test_a_palette_is_a_preset_a_hue_and_mode_or_three_picks():
    print("test_a_palette_is_a_preset_a_hue_and_mode_or_three_picks")
    _assert(all(colours.palette(name) == (name, colours.PALETTES[name])
                for name in colours.PALETTES), "a preset is its own spec")

    def hues(spec):
        pal = colours.palette(spec)[1]
        return [None if colours.hue(pal[slot]) is None else round(colours.hue(pal[slot]))
                for slot in colours.SLOTS]

    _assert(hues("triadic:red") == [0, 240, 120],
            "Triadic: the hue, its partner and contrast a third of the wheel round")
    _assert(hues("analogous:azure") == [210, 180, 240], "Analogous: the hue's neighbours")
    _assert(hues("complementary:azure") == [210, 210, 30],
            "a mode of two keeps its second colour for the contrast")
    _assert(hues("color+white:blue") == [240, 240, None]
            and colours.palette("color+white:blue")[1]["contrast"] == colours.WHITE,
            "Color+White's white is the show's")
    mono = colours.palette("monochromatic:red")[1]
    _assert(hues("monochromatic:red") == [0, 0, 0]
            and mono["primary"][1] < mono["secondary"][1] < mono["contrast"][1],
            "Monochromatic: one hue, paler each slot")
    _assert(hues("single:green") == [120] * 3, "Single: one colour")

    _assert(colours.palette("triadic:random", 7) == colours.palette("triadic:random", 7),
            "a random hue is the take's: the same seed draws the same")
    drawn = {colours.palette("triadic:random", seed)[0] for seed in range(40)}
    _assert(len(drawn) > 6 and all(spec.split(":")[1] in colours.HUES for spec in drawn),
            f"and resolves to a named hue the user can pick ({len(drawn)} drawn)")

    spec, pal = colours.palette("custom:#FF8800,#cc0022,3355ff")
    _assert(spec == "custom:#ff8800,#cc0022,#3355ff" and pal["primary"] == (254, 136, 0, 0)
            and pal["contrast"] == (51, 85, 254, 0), "Custom: three picked colours")
    for bad in ("nope", "triadic:pink", "custom:#ff0000,#00ff00", "custom:#ff00zz,#000,#000"):
        try:
            colours.palette(bad)
        except ValueError:
            continue
        raise AssertionError(f"'{bad}' names no palette")
    print("  ok: a spec that names no palette is refused")
    _assert([colours.label(s) for s in ("ember", "color+white:azure", spec)]
            == ["Ember", "Color+White · Azure", "Custom"], "and each reads as a name")


def _takes(song, count=8, pace=DEFAULT_PACE):
    for seed in range(count):
        director = Director(song, build_rig(_stage())[0], colours.PALETTES["ember"], pace,
                            seed)
        director.direct()
        yield seed, director


def _shown(lane, t) -> bool:
    """Whether the lane's own look (its BASE cues) is lit at `t`."""
    base = [c for c in lane.cues if c.prio == BASE and c.t <= t + 1e-9]
    return bool(base) and base[-1].values is not None and \
        not lane.voice.judge("", base[-1].values).dark


def test_the_show_builds_to_its_climax():
    print("test_the_show_builds_to_its_climax")
    song = _analysis2()
    k_climax = song.climax
    climax = song.sections[k_climax]
    for seed, director in _takes(song):
        plans, lanes = director.plans, {lane.channel: lane for lane in director.lanes}
        reserve = director.reserve
        role = lanes[min(reserve)].role if reserve else None
        if not reserve or not any(_shown(lane, t) for lane in director.role[role]
                                  if lane.channel not in reserve
                                  for t in np.arange(0, climax.start, BEAT / 2)):
            raise AssertionError(f"take {seed}: no unit held back, or its whole role with it")
        early = [c for ch in reserve for c in lanes[ch].cues
                 if c.values is not None and c.t < climax.start - BEAT]
        if early:
            raise AssertionError(f"take {seed}: the reserve ch {sorted(reserve)} lights at "
                                 f"{early[0].t:.2f} s, before the climax")
        if not all(_cast(plan) < _cast(plans[k_climax]) for plan in plans[:k_climax]):
            raise AssertionError(f"take {seed}: a section before the climax lights all it does")
        grown = {}
        for plan, sec in zip(plans[:k_climax], song.sections):
            key = (sec.material, plan.tier)
            if key in grown and not grown[key] <= _cast(plan):
                raise AssertionError(f"take {seed}: {sec.part} comes back lighting less")
            grown[key] = _cast(plan)
        verse = song.sections[1]
        landing = {ch for ch, lane in lanes.items() for c in lane.cues
                   if c.prio == IMPACT and abs(c.t - verse.start) <= BEAT}
        if landing != _cast(plans[1]):
            raise AssertionError(f"take {seed}: the band's entrance lands on {sorted(landing)}, "
                                 f"not on its cast alone (the song has no hit there)")
    print("  ok: through eight takes the outermost unit of a role waits dark for the climax, "
          "every earlier section lights less than it, a part coming back lights at least what it "
          "did, and the band's entrance lands as a hit on its cast — not on the blinders, the "
          "song having no hit there")


def test_a_chorus_is_lit_like_the_climax():
    print("test_a_chorus_is_lit_like_the_climax")
    song = _analysis2()
    chorus, climax = song.sections[2], song.sections[song.climax]
    _seed, director = next(_takes(song, 1))
    plans, lanes = director.plans, director.lanes
    lights = [lane for lane in lanes if lane.role != "fog"]
    _assert(plans[2].tier == looks.PEAK and director.reserve
            and _cast(plans[2]) == _cast(plans[song.climax]) - director.reserve,
            "a loud chorus lights everything the climax does but the unit held back for it")
    landing = {lane.channel for lane in lanes for c in lane.cues
               if c.prio == IMPACT and c.values and abs(c.t - chorus.start) <= BEAT / 2}
    breath = [lane.channel for lane in lights for c in lane.cues
              if c.values is None and c.prio == IMPACT and chorus.start - BEAT < c.t < chorus.start]
    _assert(landing == _cast(plans[2]) | {1, 2} and len(breath) == len(lights),
            "the chorus lands on a hit of its lights — the blinders too, the song hitting there "
            "— after a blackout breath on all of them")
    later = [c.t for lane in director.role["blinder"] for c in lane.cues if c.values
             and c.prio != BASE and chorus.start + BEAT < c.t < chorus.end]
    _assert(not later, f"the blinders flash on the chorus's own hit and sit the rest of it out "
                       f"{later}")

    par = next(lane for lane in lanes if lane.channel == 4)
    per_bar = [len(_base(par, sec)) / (sec.last - sec.first + 1) for sec in (chorus, climax)]
    _assert(per_bar[1] == 2 * per_bar[0], f"the climax changes its looks twice as often as a "
                                          f"chorus ({per_bar} a bar)")
    lines = [climax.first + 4, climax.last + 1]
    for line in lines:
        for lane in lights:              # the bar before: from beat 3, the blinders beat 4
            start = song.time(line - 1, 4 if lane.role == "blinder" else 3)
            fill = [c for c in resolve(lane.cues, director.end) if abs(c.t - start) < 1e-6]
            if not (fill and fill[0].values and abs(fill[0].t + fill[0].hold - song.time(line)) < 1e-6
                    and (not lane.looks.strobes()
                         or lane.voice.judge("", fill[0].values).strobe is not None)):
                raise AssertionError(f"ch {lane.channel}: no strobe fill into bar {line}")
    strobed = [c.t for lane in lights for c in resolve(lane.cues, director.end)
               if c.values and lane.voice.judge("", c.values).strobe is not None]
    _assert(strobed and min(strobed) >= climax.start,
            "only the climax strobes: every light, the half bar into each 4-bar line and into "
            "its end — the blinders its last beat — its fastest strobe where it has one")


def test_the_blinders_keep_to_the_songs_hits():
    print("test_the_blinders_keep_to_the_songs_hits")
    for name, song in (("the first song", _analysis()), ("the second song", _analysis2())):
        hits = [at for at, _s in song.impacts]
        for pace in (DEFAULT_PACE, 8):
            for seed, director in _takes(song, 4, pace):
                stretch = song.time(song.sections[director.stretch].first) - song.beat
                for lane in director.role["blinder"]:
                    if any(c.prio == ACCENT for c in lane.cues):
                        raise AssertionError(f"{name} take {seed}: ch {lane.channel} flashes on "
                                             "a kick, a snare or an accent")
                    for c in resolve(lane.cues, director.end):
                        if c.values is None or c.prio == BASE:
                            continue
                        strobe = lane.voice.judge("", c.values).strobe is not None
                        if c.t < stretch - 1e-6 and not any(abs(c.t - at) < 1e-3 for at in hits):
                            raise AssertionError(f"{name} take {seed}: ch {lane.channel} lights "
                                                 f"at {c.t:.2f} s, on no hit of the song")
                        if c.hold > (song.beat if strobe else BLINDER_HOLD) + 1e-6:
                            raise AssertionError(f"{name} take {seed}: ch {lane.channel} "
                                                 f"{'strobes' if strobe else 'flashes'} for "
                                                 f"{c.hold:.2f} s at {c.t:.2f} s")
    print("  ok: at pace 4 and 8 the blinders never take a kick, a snare or an accent, "
          "light only on the song's own hits before its last stretch, and there strobe a beat "
          "at most")
    song = _analysis2()
    director = next(_takes(song, 1))[1]
    _assert(director.stretch == song.climax and not director.blinding(2)
            and director.blinding(song.climax) and director.blinding(len(song.sections) - 1),
            "the last stretch runs from the last chorus (here the climax) to the end; an earlier "
            "chorus is not in it")


def test_the_cast_takes_turns():
    print("test_the_cast_takes_turns")
    song = _analysis2()
    played = set()
    for seed, director in _takes(song, 12):
        plans = director.plans
        for k, (sec, plan) in enumerate(zip(song.sections, plans)):
            later = [p for p, s in zip(plans, song.sections)
                     if s.material == sec.material and p.tier == plan.tier]
            if any(p.pattern != plan.pattern for p in later):
                raise AssertionError(f"take {seed}: {sec.part} comes back with another "
                                     "choreography")
            if plan.pattern not in (ALTERNATE, CHASE, STACK):
                continue
            played.add(plan.pattern)
            count, sides = [], set()
            for at, _on in director.turns(sec, plan, 0):
                states = [{_shown(lane, at + 0.01) for lane in unit} for unit in plan.cast]
                if any(len(state) > 1 for state in states):
                    raise AssertionError(f"take {seed}: a mirrored pair splits")
                on = [u for u, state in enumerate(states) if True in state]
                count.append(len(on))
                if plan.pattern == ALTERNATE:
                    side = on[0] % 2 if on else None
                    if on != list(range(side or 0, len(plan.cast), 2)):
                        raise AssertionError(f"take {seed} {sec.part}: alternating lights {on}")
                    sides.add(side)
                if plan.pattern == CHASE and len(on) != 1:
                    raise AssertionError(f"take {seed} {sec.part}: a chase lights {on}")
            if plan.pattern == ALTERNATE and sides != {0, 1}:
                raise AssertionError(f"take {seed} {sec.part}: the sides never swap")
            if plan.pattern == STACK and not (count == sorted(count) and count[-1] == len(plan.cast)
                                              and count[0] < count[-1]):
                raise AssertionError(f"take {seed} {sec.part}: a stack lights {count}")
    _assert(played == {ALTERNATE, CHASE, STACK},
            f"through twelve takes the cast alternates, chases and stacks ({sorted(played)}), "
            "every other unit lit at a time, one, or a unit more each step")
    director = Director(song, [], colours.PALETTES["ember"])
    _assert(all(director.pick_pattern(tier, before) != before
                for tier in (looks.LEAD, looks.FULL) for before in MOVING for _ in range(20)),
            "a material never takes the choreography of the part before it")
    lights = [lane for lane in build_rig(_stage())[0] if lane.role != "fog"]
    _assert(all(Director(song, lights, colours.PALETTES["ember"]).shape(p, looks.FULL, 3)
                in (TOGETHER, IN_TURN) for p in MOVING + FILLING),
            "a loud section of three units keeps them all lit")


def _patch():
    return [FixtureInstance(type="ledbar", dmx_start=1, channel=0),
            FixtureInstance(type="blinder", dmx_start=33, channel=1),
            FixtureInstance(type="smoke-machine", mode="1ch", dmx_start=73, channel=2),
            FixtureInstance(type="par-rgbw", dmx_start=74, channel=3, x=100),
            FixtureInstance(type="par-rgbw", dmx_start=80, channel=4, x=900),
            FixtureInstance(type="moving-head", mode="12ch", dmx_start=92, channel=5),
            FixtureInstance(type="moving-head", mode="12ch", dmx_start=92, channel=6)]


def _stage():
    """A rig across a stage 1400 wide: a pixel bar and a par in the middle,
    blinders and pars either side of it, two movers flown on one address
    either side, one standing on the deck at the left and one flown at the
    right, and a fog machine off to a side."""
    return [FixtureInstance(type="ledbar", dmx_start=1, channel=0, x=700),
            FixtureInstance(type="blinder", dmx_start=8, channel=1, x=500),
            FixtureInstance(type="blinder", dmx_start=16, channel=2, x=900),
            FixtureInstance(type="par-rgbw", dmx_start=24, channel=3, x=0),
            FixtureInstance(type="par-rgbw", dmx_start=30, channel=4, x=690),
            FixtureInstance(type="par-rgbw", dmx_start=36, channel=5, x=1400),
            FixtureInstance(type="moving-head", mode="12ch", dmx_start=42, channel=6, x=300),
            FixtureInstance(type="moving-head", mode="12ch", dmx_start=42, channel=7, x=1100),
            FixtureInstance(type="moving-head", mode="12ch", dmx_start=54, channel=8, x=200,
                            hanging=False),
            FixtureInstance(type="moving-head", mode="12ch", dmx_start=66, channel=10, x=1200),
            FixtureInstance(type="smoke-machine", mode="1ch", dmx_start=78, channel=9, x=1300)]


def test_mirrored_fixtures_are_paired():
    print("test_mirrored_fixtures_are_paired")
    lanes, _skipped = build_rig(_stage())
    mirrors = {lane.channel: lane.mirror.channel for lane in lanes if lane.mirror}
    _assert(mirrors == {1: 2, 2: 1, 3: 5, 5: 3},
            f"the blinders and the outer pars are mirrored pairs {mirrors}")
    lane = {lane.channel: lane for lane in lanes}
    _assert(lane[6].x == 700 and lane[6].mirror is None,
            "movers twinned on one address either side stand in the middle, alone")
    _assert(lane[4].mirror is None, "a par in the middle pairs with no one")
    _assert(lane[8].mirror is None and lane[10].mirror is None,
            "a mover on the deck does not mirror one flown across the stage")


def _one_sided(a, b, end) -> float:
    """The longest stretch, in seconds, that exactly one of two lanes is lit."""
    cues = [resolve(lane.cues, end) for lane in (a, b)]
    times = [[c.t for c in lane_cues] for lane_cues in cues]
    shows = [[c.values is not None and not lane.voice.judge("", c.values).dark for c in lane_cues]
             for lane, lane_cues in zip((a, b), cues)]

    def lit(k, t):
        i = bisect.bisect_right(times[k], t + 1e-9)
        return bool(i) and shows[k][i - 1]

    longest = run = 0.0
    for i in range(int(end / 0.01)):
        run = run + 0.01 if lit(0, i * 0.01) != lit(1, i * 0.01) else 0.0
        longest = max(longest, run)
    return longest


def test_a_mirrored_pair_lights_as_one():
    print("test_a_mirrored_pair_lights_as_one")
    song = _analysis2()
    pars = [FixtureInstance(type="par-rgbw", dmx_start=1, channel=0, x=100),
            FixtureInstance(type="par-rgbw", dmx_start=7, channel=1, x=1300)]
    *_rest, director = writer.program(pars, song, colours.PALETTES["ember"], 0.0, BPM)
    intro = song.sections[0]
    left, right = director.lanes
    _assert(director.plans[0].tier == looks.GLOW and _base(left, intro) == _base(right, intro)
            and any(v for _t, v in _base(left, intro)),
            "a song opening on a glow lights both pars of a pair, never the one on the right")
    together = 0
    # neon gives the pars looks ranked nearly alike, which a take shuffles
    for palette, seed in [(p, s) for p in ("ember", "neon") for s in range(6)]:
        director = Director(song, build_rig(_stage())[0], colours.PALETTES[palette], 100, seed)
        director.direct()
        pairs = [(lane, lane.mirror) for lane in director.lanes
                 if lane.mirror and lane.x < lane.mirror.x]
        for a, b in pairs:
            base = lambda lane: [(c.t, c.values) for c in lane.cues if c.prio == BASE]  # noqa: E731
            if base(a) != base(b):
                raise AssertionError(f"{palette} take {seed}: ch {a.channel} and ch {b.channel} "
                                     "part ways")
            hits = [[c.t for c in lane.cues if c.prio == ACCENT] for lane in (a, b)]
            for mine, theirs in (hits, hits[::-1]):
                if not all(any(abs(t - u) <= BAR / 2 + 1e-6 for u in theirs) for t in mine):
                    raise AssertionError(f"{palette} take {seed}: ch {a.channel}/{b.channel} "
                                         "flash alone")
            together += sum(1 for t in hits[0] if any(abs(t - u) < 1e-6 for u in hits[1]))
            longest = _one_sided(a, b, director.end)
            if longest > BAR / 2 + 0.02:
                raise AssertionError(f"{palette} take {seed}: ch {a.channel}/{b.channel} lit on "
                                     f"one side for {longest:.2f} s")
    print("  ok: through twelve takes a pair plays every look together and is never lit on one "
          "side for longer than half a bar")
    director = Director(song, build_rig(_stage())[0], colours.PALETTES["ember"])
    pair = director.role["blinder"]
    close = director.strikes(pair, [10.0, 10.0 + BAR / 4], together=False)
    apart = director.strikes(pair, [10.0, 10.0 + BAR], together=False)
    _assert(together and [hit for _t, hit in close] == [[pair[0]], [pair[1]]]
            and all(hit == pair for _t, hit in apart),
            f"a pair flashes as one ({together} hits), or side to side when a hit is answered "
            "within half a bar")


def test_the_show_plays_as_programmed():
    print("test_the_show_plays_as_programmed")
    song = _analysis2()
    patch = _patch()
    mm, *_rest, director = writer.program(patch, song, colours.PALETTES["ember"], 0.0, BPM)
    fd, path = tempfile.mkstemp(suffix=".mid")
    os.close(fd)
    try:
        _assert(mm.export_to_file(path, bpm=BPM), "exported")
        frames = replay.replay(patch, path, song.end_of_sound + 1.0)
    finally:
        os.unlink(path)
    lights = [strip for strip in replay.strips_of(patch)
              if looks.Voice(strip.fdef).role() != "fog"]

    def lit(t):
        frame = frames[int(round(t / replay.FRAME))]
        return sorted(strip.channels[0] for strip in lights
                      if any(max(rgb) > 20 for rgb in strip.look(frame)[0]))

    glow = FIRST_DOWNBEAT + 1.5 * BAR       # faded in, and before the intro's first accent
    _assert(len(lit(glow)) == 1, f"through the controller, the intro glows on one fixture {lit(glow)}")
    chorus = FIRST_DOWNBEAT + 12 * BAR + 0.1
    _assert(set(lit(chorus)) == _cast(director.plans[2]) | {1},
            f"through the controller, the first chorus lands on its cast and the blinder "
            f"{lit(chorus)}")
    _assert(lit(CLIMAX2 + 0.1) == sorted(s.channels[0] for s in lights),
            f"through the controller, every light is on the climax {lit(CLIMAX2 + 0.1)}")
    _assert(not lit(sum(song.stops[0]) / 2), "through the controller, the stop is dark")
    _assert(not lit(song.cut + 0.2), "through the controller, the cut ends in darkness")


def test_a_generated_show_survives_save_and_reload():
    print("test_a_generated_show_survives_save_and_reload")
    for pace in (0, DEFAULT_PACE, 8):
        print(f" pace {pace}")
        _survives_save_and_reload(_analysis(), pace, DROP, {"INTRO", "OUTRO", "END"})
    print(" the second song")
    _survives_save_and_reload(_analysis2(), DEFAULT_PACE, CLIMAX2,
                              {"INTRO", "VERSE", "CHORUS", "OUTRO", "END"})


def _survives_save_and_reload(song, pace, drop, parts_expected):
    patch = _patch()
    mm, lanes, notes, counts, _fades, _director = writer.program(
        patch, song, colours.PALETTES["ember"], 0.0, BPM, pace=pace)
    roles = {lane.channel: lane.role for lane in lanes}
    _assert(roles == {0: "pixel", 1: "blinder", 2: "fog", 3: "wash", 4: "wash", 5: "mover"},
            f"one lane per fixture, twins merged: {roles}")
    _assert(next(lane for lane in lanes if lane.channel == 5).twins == [6],
            "the second head on the same address rides the first one's lane")
    _assert(not notes and not clashes(lanes), f"nothing skipped or clashing {notes}")
    _assert(all(counts[ch]["scenes"] + counts[ch]["clips"] for ch in roles),
            "every lane is programmed")
    _assert(counts[2]["scenes"] == 0, "the one-channel fog machine is written as clips")
    # a scene, or a single-channel clip when the build already set the rest
    lit = {e["channel"] for e in mm.midi_events
           if e["channel"] is not None and not note_layout.is_song_part(e["note"])
           and abs(e["time"] - drop) < 0.1}
    _assert(lit == {0, 1, 3, 4, 5}, f"every light hits the drop {sorted(lit)}")

    fd, path = tempfile.mkstemp(suffix=".mid")
    os.close(fd)
    try:
        _assert(mm.export_to_file(path, bpm=BPM), "exported")
        for _ in range(2):                       # an edit, a save, a reload
            back = MidiManager(note_layout.build_note_map(), fixtures=patch)
            back.set_song_duration(song.duration)
            _assert(back.load_from_file(path), "reloaded")
            back.set_fixture_mute(15, True)
            back.set_fixture_mute(15, False)
            _assert(back.export_to_file(path, bpm=BPM), "saved again")
        back = MidiManager(note_layout.build_note_map(), fixtures=patch)
        back.set_song_duration(song.duration)
        back.load_from_file(path)
    finally:
        os.unlink(path)

    def events(m):
        out = {}
        for e in m.midi_events:
            key = (e["channel"] or 0, e["note"], e["velocity"], e.get("scene_name"))
            out.setdefault(key, []).append(e["time"])
        return {k: sorted(v) for k, v in out.items()}
    before, after = events(mm), events(back)
    same = before.keys() == after.keys() and all(
        len(before[k]) == len(after[k])
        and all(abs(x - y) < 0.003 for x, y in zip(before[k], after[k])) for k in before)
    _assert(same, f"all {len(mm.midi_events)} events come back unchanged, to within 3 ms")
    fades = lambda m: sum(1 for n in m.midi_notes if n.get("is_fade"))  # noqa: E731
    _assert(fades(back) == fades(mm), f"all {fades(mm)} fade ramp notes come back")
    parts = {note_layout.SONG_PARTS[e["note"] - note_layout.SONG_PART_BASE]
             for e in back.midi_events if note_layout.is_song_part(e["note"])}
    _assert(parts == parts_expected, f"the arrangement band {parts}")
    end_note = note_layout.SONG_PART_BASE + note_layout.SONG_PARTS.index("END")
    ends = [e["time"] for e in back.midi_events if e["note"] == end_note]
    _assert(len(ends) == 1 and abs(ends[0] - song.end_start) < 0.003,
            f"one END, where the band says ({ends})")
    lanes_t = {}
    for e in back.midi_events:
        if not note_layout.is_song_part(e["note"]):
            lanes_t.setdefault(e["channel"], set()).add(round(e["time"], 4))
    tight = [(ch, a, b) for ch, ts in lanes_t.items()
             for a, b in zip(sorted(ts), sorted(ts)[1:]) if b - a < 0.1]
    _assert(not tight, f"no two clips of a lane closer than 0.1 s {tight[:3]}")


def main():
    test_the_analysis_finds_the_structure()
    test_the_analysis_finds_what_repeats()
    test_only_a_chorus_climax_the_song_ends_on_is_its_outro()
    test_the_analysis_hears_the_voice()
    test_roles_come_from_capabilities()
    test_looks_come_from_the_library()
    test_pace_sets_how_often_looks_change()
    test_the_feels_run_from_still_to_frantic()
    test_the_tuning_and_the_style_reach_the_engine()
    test_a_repeat_is_lit_like_what_it_repeats()
    test_darkness_has_its_moments()
    test_the_climax_and_the_last_hit_take_the_whole_rig()
    test_the_colours_stay_in_the_palette()
    test_a_palette_is_a_preset_a_hue_and_mode_or_three_picks()
    test_the_show_builds_to_its_climax()
    test_a_chorus_is_lit_like_the_climax()
    test_the_blinders_keep_to_the_songs_hits()
    test_the_cast_takes_turns()
    test_mirrored_fixtures_are_paired()
    test_a_mirrored_pair_lights_as_one()
    test_the_show_plays_as_programmed()
    test_a_generated_show_survives_save_and_reload()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
