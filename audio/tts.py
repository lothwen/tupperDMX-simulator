# ABOUTME: Piper TTS rendering with a WAV cache — voice cues and the spoken
# ABOUTME: count-in are pre-rendered at edit time; nothing synthesizes live.

import hashlib
import wave
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from audio.mixer import SAMPLE_RATE
from core.paths import userprefs_dir
from tupperlogger import get_logger

logger = get_logger("audio.tts")

# Curated voice set (piper-voices naming: <lang>_<REGION>-<name>-<quality>).
# Models are downloaded on demand into voices_dir(); never bundled.
VOICES = (
    "es_ES-davefx-medium",
    "es_ES-sharvard-medium",
    "es_ES-carlfm-x_low",
    "eu_ES-maider-medium",
    "en_US-lessac-medium",
    "en_US-libritts_r-medium"
)
DEFAULT_VOICE = VOICES[0]

# Spoken count-in words by voice language prefix.
COUNT_WORDS = {
    "es": ("uno", "dos", "tres", "cuatro"),
    "en": ("one", "two", "three", "four"),
    "eu": ("bat", "bi", "hiru", "lau")
}

# Loaded piper voice models, kept warm per process (onnx load is ~1s).
_loaded_voices: Dict[str, object] = {}


def voices_dir() -> Path:
    return userprefs_dir() / "tts_voices"


def cache_dir() -> Path:
    return userprefs_dir() / "tts_cache"


def voice_label(voice: str) -> str:
    """Human-readable combo label: 'es_ES · davefx (medium)'."""
    parts = voice.split("-")
    if len(parts) == 3:
        return f"{parts[0]} · {parts[1]} ({parts[2]})"
    return voice


def count_words_for_voice(voice: str) -> Tuple[str, ...]:
    return COUNT_WORDS.get(voice[:2], COUNT_WORDS["en"])


def is_available() -> bool:
    """True when the piper engine is importable."""
    try:
        import piper  # noqa: F401
        return True
    except ImportError:
        return False


def is_voice_installed(voice: str) -> bool:
    base = voices_dir() / voice
    return (base.with_suffix(".onnx").exists()
            and base.with_suffix(".onnx.json").exists())


def download_voice(voice: str) -> None:
    """Download a voice model from HuggingFace into voices_dir().
    Blocking (~60 MB) — callers run it in a worker thread."""
    from piper.download_voices import download_voice as piper_download
    target = voices_dir()
    target.mkdir(parents=True, exist_ok=True)
    logger.info(f"Downloading TTS voice {voice} into {target}")
    piper_download(voice, target)


def cache_path(voice: str, text: str) -> Path:
    digest = hashlib.sha1(f"{voice}|{text}".encode("utf-8")).hexdigest()
    return cache_dir() / f"{digest}.wav"


def render(text: str, voice: str) -> Optional[np.ndarray]:
    """Mono float32 @ SAMPLE_RATE for `text`, cache-first.

    Returns None when the text can't be rendered right now (piper missing,
    voice not downloaded, synthesis error) — callers degrade gracefully."""
    text = text.strip()
    if not text:
        return None
    path = cache_path(voice, text)
    if path.exists():
        return load_wav(path)
    if not is_available() or not is_voice_installed(voice):
        return None
    try:
        samples = _synthesize(text, voice)
    except Exception as e:
        logger.error(f"TTS synthesis failed for '{text}': {e}")
        return None
    if samples is None or not len(samples):
        return None
    save_wav(path, samples)
    return samples


def render_count_words(voice: str) -> Optional[List[np.ndarray]]:
    """The four spoken count-in words for `voice`, or None if any is missing."""
    rendered = [render(word, voice) for word in count_words_for_voice(voice)]
    if any(samples is None for samples in rendered):
        return None
    return rendered


def _synthesize(text: str, voice: str) -> Optional[np.ndarray]:
    """Run piper on `text` and resample the result to SAMPLE_RATE mono.
    Module-level seam: tests monkeypatch this to avoid onnx/network."""
    from piper import PiperVoice

    model = _loaded_voices.get(voice)
    if model is None:
        model = PiperVoice.load(voices_dir() / f"{voice}.onnx")
        _loaded_voices[voice] = model

    chunks = []
    rate = SAMPLE_RATE
    for chunk in model.synthesize(text):
        rate = chunk.sample_rate
        data = chunk.audio_float_array
        if data.ndim > 1:
            data = data.mean(axis=1)
        chunks.append(data.astype(np.float32))
    if not chunks:
        return None
    samples = np.concatenate(chunks)
    return _resample(samples, rate, SAMPLE_RATE)


def _resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or src_rate <= 0 or not len(samples):
        return samples.astype(np.float32)
    n_out = int(len(samples) * dst_rate / src_rate)
    dst_t = np.linspace(0, len(samples) - 1, n_out)
    return np.interp(dst_t, np.arange(len(samples)), samples).astype(np.float32)


def save_wav(path: Path, samples: np.ndarray) -> None:
    """Write mono float32 samples as 16-bit PCM @ SAMPLE_RATE."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm.tobytes())


def load_wav(path: Path) -> Optional[np.ndarray]:
    """Read a cache WAV back to mono float32 @ SAMPLE_RATE."""
    try:
        with wave.open(str(path), "rb") as f:
            frames = f.readframes(f.getnframes())
            channels = f.getnchannels()
            rate = f.getframerate()
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        return _resample(samples, rate, SAMPLE_RATE)
    except Exception as e:
        logger.error(f"Failed to read TTS cache {path}: {e}")
        return None
