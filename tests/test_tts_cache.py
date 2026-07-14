"""
Characterization test: TTS render cache (audio/tts.py).

Uses a fake synthesizer and temp cache/voices dirs — no network, no
onnxruntime, no piper model. Covers cache-key naming, cache-first rendering,
graceful degradation when the engine/voice is missing, WAV round-trip and
count-in word selection per voice language.

Run with:
    uv run python tests/test_tts_cache.py
"""

import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from audio import tts
from audio.mixer import SAMPLE_RATE


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


class _Env:
    """Redirect tts dirs into a temp dir and stub the synthesizer seams."""

    def __init__(self, tmp, available=True, installed=True, synth=None):
        self.tmp = Path(tmp)
        self.available = available
        self.installed = installed
        self.synth_calls = []
        self._synth = synth

    def __enter__(self):
        self._orig = (tts.voices_dir, tts.cache_dir, tts.is_available,
                      tts.is_voice_installed, tts._synthesize)
        tts.voices_dir = lambda: self.tmp / "voices"
        tts.cache_dir = lambda: self.tmp / "cache"
        tts.is_available = lambda: self.available
        tts.is_voice_installed = lambda voice: self.installed

        def synth(text, voice):
            self.synth_calls.append((text, voice))
            if self._synth is not None:
                return self._synth(text, voice)
            return np.full(100, 0.25, dtype=np.float32)

        tts._synthesize = synth
        return self

    def __exit__(self, *exc):
        (tts.voices_dir, tts.cache_dir, tts.is_available,
         tts.is_voice_installed, tts._synthesize) = self._orig
        return False


def test_cache_key():
    print("test_cache_key")
    with tempfile.TemporaryDirectory() as tmp:
        with _Env(tmp):
            a = tts.cache_path("es_ES-davefx-medium", "entra batería")
            b = tts.cache_path("es_ES-davefx-medium", "entra batería")
            c = tts.cache_path("en_US-lessac-medium", "entra batería")
            d = tts.cache_path("es_ES-davefx-medium", "otro aviso")
            _assert(a == b, "same voice+text → same cache file")
            _assert(a != c, "different voice → different cache file")
            _assert(a != d, "different text → different cache file")
            _assert(a.suffix == ".wav", "cache entries are wav files")


def test_render_uses_cache():
    print("test_render_uses_cache")
    with tempfile.TemporaryDirectory() as tmp:
        with _Env(tmp) as env:
            first = tts.render("entra batería", "es_ES-davefx-medium")
            _assert(first is not None and first.dtype == np.float32,
                    "fresh render returns float32 samples")
            _assert(len(env.synth_calls) == 1, "fresh render synthesizes once")
            _assert(tts.cache_path("es_ES-davefx-medium", "entra batería").exists(),
                    "render writes the cache wav")

            second = tts.render("entra batería", "es_ES-davefx-medium")
            _assert(len(env.synth_calls) == 1, "second render is cache-only")
            _assert(np.max(np.abs(second - first)) < 0.01,
                    "cache round-trip preserves the audio (16-bit tolerance)")


def test_render_degrades_gracefully():
    print("test_render_degrades_gracefully")
    with tempfile.TemporaryDirectory() as tmp:
        with _Env(tmp, available=False) as env:
            _assert(tts.render("hola", "es_ES-davefx-medium") is None,
                    "no piper installed → None (no crash)")
            _assert(env.synth_calls == [], "no synthesis without the engine")
        with _Env(tmp, installed=False) as env:
            _assert(tts.render("hola", "es_ES-davefx-medium") is None,
                    "voice not downloaded → None")

        def boom(text, voice):
            raise RuntimeError("onnx exploded")
        with _Env(tmp, synth=boom):
            _assert(tts.render("hola", "es_ES-davefx-medium") is None,
                    "synthesis errors → None (no crash)")

        with _Env(tmp) as env:
            _assert(tts.render("   ", "es_ES-davefx-medium") is None,
                    "blank text renders nothing")


def test_count_words():
    print("test_count_words")
    _assert(tts.count_words_for_voice("es_ES-davefx-medium")
            == ("uno", "dos", "tres", "cuatro"), "Spanish voice counts in Spanish")
    _assert(tts.count_words_for_voice("en_US-lessac-medium")
            == ("one", "two", "three", "four"), "English voice counts in English")
    _assert(tts.count_words_for_voice("fr_FR-something")
            == ("one", "two", "three", "four"), "unknown language falls back to English")

    with tempfile.TemporaryDirectory() as tmp:
        with _Env(tmp) as env:
            words = tts.render_count_words("es_ES-davefx-medium")
            _assert(words is not None and len(words) == 4,
                    "count words render as four clips")
            _assert([c[0] for c in env.synth_calls]
                    == ["uno", "dos", "tres", "cuatro"],
                    "the Spanish numbers were synthesized")
        with _Env(tmp, available=False):
            # cache from the previous env is gone (different is_available), but
            # the wavs persist — count words come straight from cache
            words = tts.render_count_words("es_ES-davefx-medium")
            _assert(words is not None,
                    "cached count words survive without the engine")


def test_wav_roundtrip():
    print("test_wav_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        ramp = np.linspace(-0.5, 0.5, SAMPLE_RATE // 10).astype(np.float32)
        path = Path(tmp) / "clip.wav"
        tts.save_wav(path, ramp)
        back = tts.load_wav(path)
        _assert(back is not None and len(back) == len(ramp),
                "wav round-trip preserves length")
        _assert(np.max(np.abs(back - ramp)) < 0.001,
                "wav round-trip preserves samples (16-bit tolerance)")
        _assert(tts.load_wav(Path(tmp) / "missing.wav") is None,
                "missing wav loads as None")


def test_voice_label():
    print("test_voice_label")
    _assert(tts.voice_label("es_ES-davefx-medium") == "es_ES · davefx (medium)",
            "labels are human readable")
    _assert(tts.voice_label("weird") == "weird", "unparseable names pass through")


def main():
    test_cache_key()
    test_render_uses_cache()
    test_render_degrades_gracefully()
    test_count_words()
    test_wav_roundtrip()
    test_voice_label()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
