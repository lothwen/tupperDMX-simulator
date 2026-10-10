"""
Characterization test: the Qt-free stereo mixing core (audio/mixer.py).

Covers routing gain matrices, split-mode mono downmix, one-shot clip mixing
and exhaustion, volume/mute, RMS levels, seek, end-of-backing latching and
retuning to another output rate — all pure numpy, no Qt, no audio device.

Run with:
    uv run python tests/test_audio_mixer.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from audio.click import make_beep
from audio.mixer import (Mixer, bus_channel_gains, resample,
                         ROUTING_STEREO, ROUTING_SPLIT_BACKING_LEFT,
                         ROUTING_SPLIT_BACKING_RIGHT,
                         BUS_BACKING, BUS_CLICK, BUS_VOICE, SAMPLE_RATE)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _stereo_backing(left=0.5, right=-0.25, frames=1000):
    """int16 (frames, 2) backing with constant, distinguishable channels."""
    data = np.zeros((frames, 2), dtype=np.int16)
    data[:, 0] = int(left * 32768)
    data[:, 1] = int(right * 32768)
    return data


def test_bus_channel_gains():
    print("test_bus_channel_gains")
    gains = bus_channel_gains(ROUTING_STEREO)
    _assert(all(gains[b] == (1.0, 1.0) for b in gains),
            "stereo mode: every bus on both channels")

    gains = bus_channel_gains(ROUTING_SPLIT_BACKING_LEFT)
    _assert(gains[BUS_BACKING] == (1.0, 0.0), "split-left: backing on L only")
    _assert(gains[BUS_CLICK] == (0.0, 1.0) and gains[BUS_VOICE] == (0.0, 1.0),
            "split-left: click and voice on R only")

    gains = bus_channel_gains(ROUTING_SPLIT_BACKING_RIGHT)
    _assert(gains[BUS_BACKING] == (0.0, 1.0), "split-right: backing on R only")
    _assert(gains[BUS_CLICK] == (1.0, 0.0), "split-right: click on L only")

    gains = bus_channel_gains("nonsense")
    _assert(gains[BUS_BACKING] == (1.0, 1.0), "unknown mode falls back to stereo")


def test_make_beep():
    print("test_make_beep")
    beep = make_beep(1200)
    _assert(beep.dtype == np.float32, "beep is float32")
    _assert(len(beep) == int(0.1 * SAMPLE_RATE), "default beep is 100 ms")
    _assert(np.max(np.abs(beep)) <= 1.0, "beep stays within full scale")
    _assert(abs(beep[0]) < 1e-6 and abs(beep[-1]) < 1e-3,
            "beep fades in/out (no edge clicks)")


def test_stereo_backing_passthrough():
    print("test_stereo_backing_passthrough")
    mixer = Mixer()
    mixer.set_backing(_stereo_backing(left=0.5, right=-0.25))
    mixer.set_backing_active(True)
    out = mixer.render(100)
    _assert(out.shape == (100, 2) and out.dtype == np.float32,
            "render returns (frames, 2) float32")
    _assert(abs(out[0, 0] - 0.5) < 0.01 and abs(out[0, 1] + 0.25) < 0.01,
            "stereo mode preserves distinct L/R channels")


def test_split_downmix():
    print("test_split_downmix")
    mixer = Mixer()
    mixer.set_routing(ROUTING_SPLIT_BACKING_LEFT)
    mixer.set_backing(_stereo_backing(left=0.5, right=-0.25))
    mixer.set_backing_active(True)
    out = mixer.render(100)
    # mono downmix = (0.5 + -0.25) / 2 = 0.125, on LEFT only
    _assert(abs(out[0, 0] - 0.125) < 0.01,
            "split-left: backing downmixed to mono on L")
    _assert(abs(out[0, 1]) < 1e-6, "split-left: R carries no backing")

    mixer.set_routing(ROUTING_SPLIT_BACKING_RIGHT)
    mixer.set_position_seconds(0)
    out = mixer.render(100)
    _assert(abs(out[0, 1] - 0.125) < 0.01 and abs(out[0, 0]) < 1e-6,
            "split-right mirrors the downmix")


def test_click_routing_and_exhaustion():
    print("test_click_routing_and_exhaustion")
    mixer = Mixer()
    mixer.set_routing(ROUTING_SPLIT_BACKING_LEFT)
    clip = np.full(150, 0.5, dtype=np.float32)
    mixer.trigger(BUS_CLICK, clip)

    out = mixer.render(100)
    _assert(abs(out[0, 1] - 0.5) < 1e-6 and abs(out[0, 0]) < 1e-6,
            "split-left: click plays on R only")

    out = mixer.render(100)
    _assert(abs(out[0, 1] - 0.5) < 1e-6, "clip continues across blocks")
    _assert(abs(out[60, 1]) < 1e-6, "clip goes silent once exhausted")

    out = mixer.render(100)
    _assert(np.all(out == 0), "exhausted one-shots are dropped")


def test_overlapping_oneshots_sum():
    print("test_overlapping_oneshots_sum")
    mixer = Mixer()
    mixer.trigger(BUS_VOICE, np.full(50, 0.3, dtype=np.float32))
    mixer.trigger(BUS_VOICE, np.full(50, 0.4, dtype=np.float32))
    out = mixer.render(50)
    _assert(abs(out[0, 0] - 0.7) < 1e-6, "simultaneous clips on a bus sum")


def test_volume_and_mute():
    print("test_volume_and_mute")
    mixer = Mixer()
    mixer.set_backing(_stereo_backing(left=0.5, right=0.5))
    mixer.set_backing_active(True)
    mixer.set_volume(BUS_BACKING, 0.5)
    out = mixer.render(10)
    _assert(abs(out[0, 0] - 0.25) < 0.01, "bus volume scales the signal")

    mixer.set_muted(BUS_BACKING, True)
    out = mixer.render(10)
    _assert(np.all(out == 0), "muted bus renders silence")

    mixer.set_muted(BUS_BACKING, False)
    out = mixer.render(10)
    _assert(abs(out[0, 0] - 0.25) < 0.01, "unmute restores the set volume")


def test_backing_inactive_is_silent_but_oneshots_play():
    print("test_backing_inactive_is_silent_but_oneshots_play")
    mixer = Mixer()
    mixer.set_backing(_stereo_backing())
    mixer.set_backing_active(False)
    mixer.trigger(BUS_CLICK, np.full(10, 0.5, dtype=np.float32))
    pos_before = mixer.position_seconds()
    out = mixer.render(10)
    _assert(abs(out[0, 0] - 0.5) < 1e-6,
            "click plays while the backing is paused (preview path)")
    _assert(mixer.position_seconds() == pos_before,
            "paused backing does not advance")


def test_position_and_seek():
    print("test_position_and_seek")
    mixer = Mixer()
    mixer.set_backing(_stereo_backing(frames=SAMPLE_RATE))  # 1 s
    mixer.set_backing_active(True)
    mixer.render(4410)
    _assert(abs(mixer.position_seconds() - 0.1) < 1e-6,
            "cursor advances by rendered frames")
    mixer.set_position_seconds(0.5)
    _assert(abs(mixer.position_seconds() - 0.5) < 1e-6, "seek moves the cursor")
    _assert(abs(mixer.duration_seconds() - 1.0) < 1e-6, "duration from array length")


def test_end_of_backing_latch():
    print("test_end_of_backing_latch")
    mixer = Mixer()
    mixer.set_backing(_stereo_backing(frames=100))
    mixer.set_backing_active(True)
    mixer.render(50)
    _assert(not mixer.consume_backing_ended(), "not ended mid-track")
    mixer.render(60)  # crosses the end
    _assert(mixer.consume_backing_ended(), "latch set when cursor crosses the end")
    _assert(not mixer.consume_backing_ended(), "consume clears the latch")
    mixer.render(50)  # already past the end
    _assert(not mixer.consume_backing_ended(), "no re-latch once past the end")
    mixer.set_position_seconds(0)
    mixer.render(120)
    _assert(mixer.consume_backing_ended(), "latch re-arms after a seek")


def test_levels():
    print("test_levels")
    mixer = Mixer()
    mixer.set_backing(_stereo_backing(left=0.5, right=0.5))
    mixer.set_backing_active(True)
    mixer.trigger(BUS_CLICK, np.full(100, 0.5, dtype=np.float32))
    mixer.render(100)
    levels = mixer.levels()
    _assert(abs(levels[BUS_BACKING] - 0.5) < 0.01, "backing RMS reflects signal")
    _assert(abs(levels[BUS_CLICK] - 0.5) < 0.01, "click RMS reflects signal")
    _assert(levels[BUS_VOICE] == 0.0, "silent bus reads 0")

    mixer.render(100)  # click exhausted, backing continues
    levels = mixer.levels()
    _assert(levels[BUS_CLICK] == 0.0, "levels decay to 0 when a bus goes quiet")


def test_clipping():
    print("test_clipping")
    mixer = Mixer()
    mixer.trigger(BUS_CLICK, np.full(10, 0.8, dtype=np.float32))
    mixer.trigger(BUS_CLICK, np.full(10, 0.8, dtype=np.float32))
    out = mixer.render(10)
    _assert(np.max(out) <= 1.0, "output is hard-clipped to full scale")


def test_resample():
    print("test_resample")
    mono = np.linspace(0.0, 1.0, 1000, dtype=np.float32)
    _assert(resample(mono, 44100, 44100) is mono, "matching rates return the input")
    _assert(len(resample(mono, 44100, 48000)) == 1088,
            "44.1k -> 48k stretches the clip by the rate ratio")
    _assert(resample(mono, 44100, 48000).dtype == np.float32,
            "float32 stays float32")

    stereo = _stereo_backing(frames=44100)
    up = resample(stereo, 44100, 48000)
    _assert(up.shape == (48000, 2) and up.dtype == np.int16,
            "int16 stereo keeps its shape and dtype")
    _assert(abs(int(up[100, 0]) - int(stereo[100, 0])) <= 1
            and abs(int(up[100, 1]) - int(stereo[100, 1])) <= 1,
            "a constant signal survives resampling on both channels")

    _assert(len(resample(mono, 0, 48000)) == len(mono),
            "a bogus source rate is a no-op, never a crash")


def test_retune_to_another_output_rate():
    """A song loaded at 44.1 kHz keeps its duration and position when the
    output device turns out to run at 48 kHz (AudioEngine._apply_rate)."""
    print("test_retune_to_another_output_rate")
    mixer = Mixer()
    mixer.set_backing(_stereo_backing(frames=2 * SAMPLE_RATE))   # 2 s
    mixer.set_position_seconds(1.25)
    _assert(abs(mixer.duration_seconds() - 2.0) < 1e-6, "2 s at the reference rate")

    pcm = mixer.backing_samples()
    position = mixer.position_seconds()
    mixer.set_sample_rate(48000)
    mixer.set_backing(resample(pcm, SAMPLE_RATE, 48000))
    mixer.set_position_seconds(position)

    _assert(abs(mixer.duration_seconds() - 2.0) < 1e-3,
            "the song is still 2 s long after the retune")
    _assert(abs(mixer.position_seconds() - 1.25) < 1e-3,
            "playback resumes where it was, in seconds")
    _assert(len(mixer.render(480)) == 480, "the mixer renders at the new rate")


def main():
    test_bus_channel_gains()
    test_make_beep()
    test_stereo_backing_passthrough()
    test_split_downmix()
    test_click_routing_and_exhaustion()
    test_overlapping_oneshots_sum()
    test_volume_and_mute()
    test_backing_inactive_is_silent_but_oneshots_play()
    test_position_and_seek()
    test_end_of_backing_latch()
    test_levels()
    test_clipping()
    test_resample()
    test_retune_to_another_output_rate()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
