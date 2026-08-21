"""
Characterization test: output-format negotiation (audio/engine.py).

Covers the order AudioEngine offers stereo formats to a device — the reference
rate first, the device's own rate when it refuses that, and an unconfirmed
last resort so a device that under-reports (shared-mode WASAPI on Windows only
admits its 48 kHz mix format) still gets a sink attempt instead of silence.
No audio device is opened: the devices here are stubs.

Run with:
    uv run python tests/test_audio_device_format.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtMultimedia import QAudioFormat

from audio.engine import candidate_formats, stereo_format
from audio.mixer import SAMPLE_RATE

FLOAT = QAudioFormat.SampleFormat.Float
INT16 = QAudioFormat.SampleFormat.Int16


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


class FakeDevice:
    """A QAudioDevice stand-in: accepts `rates` (None = everything)."""

    def __init__(self, preferred_rate, rates=None):
        self._preferred = preferred_rate
        self._rates = rates

    def preferredFormat(self):
        return stereo_format(self._preferred, INT16)

    def isFormatSupported(self, fmt):
        return self._rates is None or fmt.sampleRate() in self._rates


def _rates(formats):
    return [f.sampleRate() for f in formats]


def test_permissive_device_keeps_the_reference_rate():
    print("test_permissive_device_keeps_the_reference_rate")
    formats = candidate_formats(FakeDevice(48000))
    _assert(formats[0].sampleRate() == SAMPLE_RATE,
            "a device that takes anything is still driven at 44.1 kHz")
    _assert(formats[0].sampleFormat() == FLOAT, "float32 is preferred over int16")


def test_device_locked_to_its_mix_format():
    print("test_device_locked_to_its_mix_format")
    formats = candidate_formats(FakeDevice(48000, rates={48000}))
    _assert(SAMPLE_RATE not in _rates(formats)[:2],
            "44.1 kHz is not offered first to a device that refuses it")
    _assert(formats[0].sampleRate() == 48000 and formats[0].sampleFormat() == FLOAT,
            "the device's own rate leads instead")


def test_device_that_confirms_nothing_is_still_tried():
    print("test_device_that_confirms_nothing_is_still_tried")
    formats = candidate_formats(FakeDevice(48000, rates=set()))
    _assert(len(formats) == 2 and set(_rates(formats)) == {48000},
            "an under-reporting device still gets its preferred rate offered")
    _assert([f.sampleFormat() for f in formats] == [FLOAT, INT16],
            "both sample formats are tried before giving up")


def test_nonsense_preferred_rate():
    print("test_nonsense_preferred_rate")
    formats = candidate_formats(FakeDevice(0, rates=set()))
    _assert(_rates(formats) == [SAMPLE_RATE, SAMPLE_RATE],
            "a device reporting no preferred rate falls back to the reference one")


def test_every_format_is_stereo():
    print("test_every_format_is_stereo")
    for device in (FakeDevice(48000), FakeDevice(96000, rates={96000}),
                   FakeDevice(48000, rates=set())):
        _assert(all(f.channelCount() == 2 for f in candidate_formats(device)),
                "the mixer renders stereo, so every candidate is stereo")


def main():
    test_permissive_device_keeps_the_reference_rate()
    test_device_locked_to_its_mix_format()
    test_device_that_confirms_nothing_is_still_tried()
    test_nonsense_preferred_rate()
    test_every_format_is_stereo()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
