# ABOUTME: scales the dark theme stylesheet to the user's UI scale.
"""UI scaling for `assets/styles/dark-theme.qss`.

The theme is one flat sheet of literal pixel values with no token layer, so
scaling is done by rewriting those values on the way in.

Only the properties that make a widget *bigger* are scaled — type, spacing and
explicit minimums. Borders, radii and gradient stops are deliberately left
alone: a 1px hairline scaled by 0.85 rounds away and the whole theme loses its
edges, and a rounded corner shrinking out of step with its box reads as a
rendering bug rather than a smaller UI.
"""

import os
import re

from core.paths import asset_path

# Properties whose pixel values scale. `width`/`height` cover the styled
# sub-controls (::drop-down, ::handle) that have no other size.
_SCALED = ("font-size", "padding", "padding-top", "padding-right",
           "padding-bottom", "padding-left", "margin", "margin-top",
           "margin-right", "margin-bottom", "margin-left", "min-width",
           "min-height", "max-width", "max-height", "width", "height")

# A declaration starts at the line head or just after a `;`/`{`, which keeps
# `border-width` and `border-color` out of the match.
_DECLARATION = re.compile(
    r"(?:(?<=^)|(?<=[;{]))(\s*(?:%s)\s*:\s*)([^;{}]*)" % "|".join(_SCALED),
    re.MULTILINE)
_PIXELS = re.compile(r"(-?\d+)px")

# Below this, text stops being text. Everything else may shrink to 1px, but a
# value that was already 0 stays 0 (see _scale_px).
_MIN_FONT_PX = 8


def _scale_px(value: str, factor: float, minimum: int) -> str:
    """Scale every `Npx` in one declaration's value, keeping bare numbers
    (`padding: 0 29px 0 8px`) and the sign of negative offsets intact."""
    def one(match: re.Match) -> str:
        pixels = int(match.group(1))
        if pixels == 0:
            return match.group(0)
        scaled = round(abs(pixels) * factor)
        scaled = max(minimum, scaled)
        return f"{-scaled if pixels < 0 else scaled}px"

    return _PIXELS.sub(one, value)


def scale_qss(qss: str, factor: float) -> str:
    """The stylesheet with its type and spacing scaled by `factor`."""
    if factor == 1.0:
        return qss

    def declaration(match: re.Match) -> str:
        prop, value = match.group(1), match.group(2)
        minimum = _MIN_FONT_PX if "font-size" in prop else 1
        return prop + _scale_px(value, factor, minimum)

    return _DECLARATION.sub(declaration, qss)


#: The theme as read from disk, and the scale currently on the application.
#: Module state because the application stylesheet IS one global thing — the
#: settings slider previews through here and the window reverts through here,
#: and both need to agree on what is currently showing.
_source = None
_applied_scale = 1.0


def load_qss() -> str:
    """The theme as shipped, or "" if it isn't there (the app runs unstyled
    rather than not at all — same tolerance `main.run_gui` has always had).

    Memoized: the settings slider re-scales on every step, and re-reading 2500
    lines each time to produce the same string is pure waste.
    """
    global _source
    if _source is None:
        path = asset_path("styles", "dark-theme.qss")
        if not os.path.exists(path):
            _source = ""
        else:
            with open(path, "r", encoding="utf-8") as handle:
                _source = handle.read()
    return _source


def applied_scale() -> float:
    """The scale the application stylesheet is currently rendered at."""
    return _applied_scale


def apply_scale(app, scale: float) -> bool:
    """Set the theme, scaled by `scale`, as the application stylesheet.

    Qt re-polishes every live widget, so this works after startup too — but it
    is not free, which is why an unchanged scale returns False without touching
    anything. True means callers with cached layout widths need to re-measure.
    """
    global _applied_scale
    if abs(scale - _applied_scale) < 1e-6:
        return False
    qss = load_qss()
    if not qss:
        return False
    _applied_scale = scale
    app.setStyleSheet(scale_qss(qss, scale))
    return True
