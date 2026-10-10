# ABOUTME: A song drawn as a light score — one strip per fixture, a band per
# ABOUTME: emitter in the colour and at the light it gives, over the song parts
# ABOUTME: and the audio's loudness — replay.py's LightScore as a QImage. QtGui.
"""A picture of what every fixture emits, over time.

One strip per fixture (fixtures on one DMX address share one), a band per
emitter in the colour and at the light it gives. Above the strips, the song
parts; below, the loudness of the song's audio (the guide when there is one).
White hatching marks a strobe, a yellow line the fixture's own animation
running, grey fog output.
"""
import os
import re
from typing import Optional, Tuple

import numpy as np
from PyQt6.QtGui import QColor, QFont, QImage, QPainter

from autoprogram import decode
from autoprogram.replay import FRAME, LightScore, light_score
from core import note_layout

LABEL_W = 170          # pixels left of the time axis, for the strip labels
STRIP_H, GAP = 30, 6   # a fixture's strip, and the space between two
PARTS_H, RULER_H, AUDIO_H = 22, 14, 36
BACKGROUND, TEXT = QColor(18, 18, 22), QColor(215, 215, 220)


def song_audio(song) -> Optional[Tuple[int, np.ndarray, float]]:
    """``(rate, mono, offset)`` of the song's audio — the guide, else the
    backing; None when it has none to read."""
    path, offset = (song.guide, song.guide_offset) if song.guide else (song.audio, song.offset)
    if not path or not os.path.exists(path):
        return None
    return (*decode.decode(path), offset)


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def draw(score: LightScore) -> QImage:
    """The picture of `score`."""
    title, strips, frames, parts = score.title, score.strips, score.frames, score.parts
    audio, px, start, stop = score.audio, score.px, score.start, score.stop
    width = LABEL_W + int((stop - start) * px) + 10
    top = PARTS_H + RULER_H
    height = top + len(strips) * (STRIP_H + GAP) + (AUDIO_H + GAP if audio is not None else 0) + 4
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(BACKGROUND)
    p = QPainter(image)
    p.setFont(QFont("Sans", 7))

    def x(t):
        return LABEL_W + int(round((t - start) * px))

    p.setPen(TEXT)
    p.drawText(4, 12, title)
    for k, (at, name) in enumerate(parts):            # the song parts, each to the next
        until = parts[k + 1][0] if k + 1 < len(parts) else stop
        if until <= start or at >= stop:
            continue
        a, b = x(max(at, start)), x(min(until, stop))
        colour = QColor(note_layout.describe_note(
            note_layout.SONG_PART_BASE + note_layout.SONG_PARTS.index(name)).color)
        p.fillRect(a, 2, max(1, b - a - 1), PARTS_H - 4, colour.darker(160))
        p.setPen(TEXT)
        p.drawText(a + 3, PARTS_H - 8, name)
    p.setPen(QColor(110, 110, 120))
    first_tick = int(np.ceil(start / 10.0)) * 10
    for t in range(first_tick, int(stop) + 1, 10):     # a tick every 10 s
        p.drawLine(x(t), PARTS_H, x(t), PARTS_H + 4)
        p.drawText(x(t) + 2, PARTS_H + RULER_H - 2, f"{t // 60}:{t % 60:02d}")

    first, last = int(start / FRAME), min(len(frames), int(np.ceil(stop / FRAME)))
    for row, strip in enumerate(strips):
        y = top + row * (STRIP_H + GAP)
        p.setPen(TEXT)
        p.drawText(4, y + STRIP_H // 2 + 4, strip.label[:30])
        runs = []                                     # (from frame, to frame, look)
        for k in range(first, last):
            look = strip.look(frames[k])
            if runs and runs[-1][2] == look:
                runs[-1][1] = k + 1
            else:
                runs.append([k, k + 1, look])
        for a, b, (colours, strobing, animating, fog) in runs:
            xa, xb = x(a * FRAME), x(b * FRAME)
            w = max(1, xb - xa)
            band = STRIP_H / len(colours)
            for n, rgb in enumerate(colours):
                p.fillRect(xa, y + int(n * band), w, max(1, int((n + 1) * band) - int(n * band)),
                           QColor(*rgb))
            if fog > 0:
                p.fillRect(xa, y + STRIP_H - int(STRIP_H * fog), w, int(STRIP_H * fog),
                           QColor(150, 150, 160))
            if strobing:
                p.setPen(QColor(255, 255, 255, 170))
                for xs in range(xa, xa + w, 3):
                    p.drawLine(xs, y, xs, y + STRIP_H - 1)
            if animating:
                p.fillRect(xa, y + STRIP_H, w, 2, QColor(240, 210, 0))
    if audio is not None:
        y = top + len(strips) * (STRIP_H + GAP)
        p.setPen(TEXT)
        p.drawText(4, y + AUDIO_H // 2 + 4, "audio")
        for k, level in enumerate(audio):
            h = int(min(1.0, level) * AUDIO_H)
            p.fillRect(LABEL_W + k, y + AUDIO_H - h, 1, h, QColor(70, 120, 170))
    p.end()
    return image


# ---------------------------------------------------------------------------
def score(show, song, out_dir: str, px: float, start: float = 0.0,
          stop: Optional[float] = None) -> Optional[str]:
    """Draw `song` of `show` into `out_dir`; the image's path, or None with
    no .mid."""
    if not song.midi or not os.path.exists(song.midi):
        return None
    picture = draw(light_score(show.fixtures, song.title, song.midi, song_audio(song),
                               px, start, stop))
    path = os.path.join(out_dir, re.sub(r"[^\w.-]+", "_", song.title).strip("_") + ".png")
    if not picture.save(path):
        raise OSError(f"could not write {path}")
    return path
