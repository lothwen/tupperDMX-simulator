#!/usr/bin/env python3
# ABOUTME: Draws the operating-mode signal-chain diagrams as standalone SVGs
# ABOUTME: into docs/manual/images/, in the manual's own light print palette.
"""Generate the chapter 2 operating-mode diagrams.

    python3 docs/manual/make_diagrams.py

Writes `images/mode-1-bbeat.svg`, `images/mode-2-laptop.svg` and
`images/mode-3-rack.svg`. No dependencies — it is string formatting.

Each diagram is a signal chain: boxes joined by labelled arrows, drawn on the
same grid so the three read as one family. Box widths are computed from the
node count, so a three-stage and a four-stage chain still span the same width.
"""

from pathlib import Path

IMAGES = Path(__file__).resolve().parent / "images"

WIDTH = 1040
HEIGHT = 206
PAD = 16
GAP = 80           # room for an arrow between two boxes
BOX_TOP = 38
BOX_H = 150
FONT = "Inter, 'Segoe UI', 'Helvetica Neue', Arial, sans-serif"
MONO = "'JetBrains Mono', 'DejaVu Sans Mono', Menlo, Consolas, monospace"

INK = "#1c1f24"
SOFT = "#5b626b"
FAINT = "#858c96"
RULE = "#cfd5dc"
PANEL = "#f5f7f9"
ACCENT = "#2f6fb0"
ACCENT_DK = "#1d4c7d"
ACCENT_BG = "#eaf2fa"


# ---------------------------------------------------------------------------
# Icons — 48x48 line art, drawn at the box's own coordinates
# ---------------------------------------------------------------------------

def _icon(paths: str, x: float, y: float, colour: str) -> str:
    """Place a 48x48 icon with its top-left corner at (x, y)."""
    return (f'<g transform="translate({x:.1f},{y:.1f})" fill="none" '
            f'stroke="{colour}" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round">{paths}</g>')


ICONS = {
    # A phone running a player app: rounded slab, screen, play triangle.
    "player": '<rect x="13" y="3" width="22" height="42" rx="4"/>'
              '<line x1="20" y1="8" x2="28" y2="8"/>'
              '<path d="M21 21l9 5-9 5z" fill="currentColor" stroke="none"/>'
              '<path d="M21 21l9 5-9 5z"/>'
              '<line x1="20" y1="40" x2="28" y2="40"/>',

    # A microcontroller board: PCB, pin headers, the chip in the middle.
    "board": '<rect x="4" y="12" width="40" height="26" rx="3"/>'
             '<rect x="18" y="21" width="12" height="9" rx="1"/>'
             '<line x1="9" y1="12" x2="9" y2="8"/>'
             '<line x1="15" y1="12" x2="15" y2="8"/>'
             '<line x1="21" y1="12" x2="21" y2="8"/>'
             '<line x1="27" y1="12" x2="27" y2="8"/>'
             '<line x1="33" y1="12" x2="33" y2="8"/>'
             '<line x1="39" y1="12" x2="39" y2="8"/>'
             '<line x1="12" y1="38" x2="12" y2="42"/>'
             '<line x1="24" y1="38" x2="24" y2="42"/>'
             '<line x1="36" y1="38" x2="36" y2="42"/>',

    # A laptop: lid, screen, base.
    "laptop": '<rect x="9" y="10" width="30" height="21" rx="2"/>'
              '<path d="M4 36h40l-3-5H7z"/>'
              '<line x1="20" y1="39" x2="28" y2="39"/>',

    # A tablet held in landscape: slab, screen, home dot.
    "tablet": '<rect x="5" y="10" width="38" height="28" rx="3"/>'
              '<rect x="10" y="15" width="26" height="18" rx="1"/>'
              '<circle cx="39.5" cy="24" r="1.4" fill="currentColor" stroke="none"/>',

    # A 1U rack box: chassis, ears, vents, a status LED.
    "rack": '<rect x="6" y="16" width="36" height="18" rx="2"/>'
            '<line x1="2" y1="18" x2="6" y2="18"/><line x1="2" y1="32" x2="6" y2="32"/>'
            '<line x1="42" y1="18" x2="46" y2="18"/><line x1="42" y1="32" x2="46" y2="32"/>'
            '<line x1="12" y1="22" x2="12" y2="28"/>'
            '<line x1="16" y1="22" x2="16" y2="28"/>'
            '<line x1="20" y1="22" x2="20" y2="28"/>'
            '<circle cx="36" cy="25" r="2" fill="currentColor" stroke="none"/>',

    # A USB-to-DMX dongle: body, USB tail, 3-pin XLR face.
    "dongle": '<rect x="14" y="14" width="24" height="22" rx="3"/>'
              '<path d="M14 21H6a2 2 0 00-2 2v4a2 2 0 002 2h8"/>'
              '<circle cx="26" cy="25" r="7"/>'
              '<circle cx="23" cy="24" r="1.2" fill="currentColor" stroke="none"/>'
              '<circle cx="29" cy="24" r="1.2" fill="currentColor" stroke="none"/>'
              '<circle cx="26" cy="28.5" r="1.2" fill="currentColor" stroke="none"/>',

    # Two lights on a truss, throwing beams.
    "fixtures": '<line x1="4" y1="9" x2="44" y2="9"/>'
                '<line x1="4" y1="14" x2="44" y2="14"/>'
                '<line x1="10" y1="9" x2="16" y2="14"/>'
                '<line x1="22" y1="14" x2="28" y2="9"/>'
                '<line x1="34" y1="9" x2="40" y2="14"/>'
                '<rect x="9" y="16" width="10" height="8" rx="1.5"/>'
                '<rect x="29" y="16" width="10" height="8" rx="1.5"/>'
                '<path d="M10 24l-5 20h18l-5-20z" stroke-dasharray="3 3"/>'
                '<path d="M30 24l-5 20h18l-5-20z" stroke-dasharray="3 3"/>',
}


# ---------------------------------------------------------------------------
# Chain drawing
# ---------------------------------------------------------------------------

def _text(x, y, content, *, size=13, weight=400, fill=INK,
          anchor="middle", family=FONT, spacing=None):
    extra = f' letter-spacing="{spacing}"' if spacing else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" '
            f'font-family="{family}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}"{extra}>{content}</text>')


def _box(x, w, node) -> str:
    """One stage of the chain."""
    highlight = node.get("highlight", False)
    stroke = ACCENT if highlight else RULE
    fill = ACCENT_BG if highlight else PANEL
    ink = ACCENT_DK if highlight else INK
    icon_colour = ACCENT if highlight else SOFT

    parts = [f'<rect x="{x:.1f}" y="{BOX_TOP}" width="{w:.1f}" height="{BOX_H}" '
             f'rx="9" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>']

    cx = x + w / 2
    parts.append(f'<g color="{icon_colour}">'
                 + _icon(ICONS[node["icon"]], cx - 24, BOX_TOP + 18, icon_colour)
                 + '</g>')

    parts.append(_text(cx, BOX_TOP + 92, node["title"], size=16, weight=650, fill=ink))
    for i, line in enumerate(node.get("lines", [])):
        parts.append(_text(cx, BOX_TOP + 113 + i * 17, line, size=12.5, fill=SOFT))
    if node.get("tag"):
        parts.append(_text(cx, BOX_TOP - 12, node["tag"], size=11, weight=650,
                           fill=ACCENT, spacing="1.2"))
    return "".join(parts)


def _arrow(x1, x2, label) -> str:
    """A labelled connector between two boxes."""
    y = BOX_TOP + BOX_H / 2
    mid = (x1 + x2) / 2
    parts = [
        f'<line x1="{x1:.1f}" y1="{y}" x2="{x2 - 9:.1f}" y2="{y}" '
        f'stroke="{ACCENT}" stroke-width="2"/>',
        f'<path d="M{x2:.1f} {y} l-10 -5 v10 z" fill="{ACCENT}"/>',
        _text(mid, y - 13, label, size=12, weight=650, fill=ACCENT_DK,
              family=MONO),
    ]
    return "".join(parts)


def diagram(nodes, links) -> str:
    count = len(nodes)
    box_w = (WIDTH - 2 * PAD - (count - 1) * GAP) / count
    body = []
    for index, node in enumerate(nodes):
        x = PAD + index * (box_w + GAP)
        body.append(_box(x, box_w, node))
        if index < count - 1:
            start = x + box_w
            body.append(_arrow(start + 6, start + GAP - 6, *links[index]))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
            f'width="{WIDTH}" height="{HEIGHT}" role="img">'
            f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>'
            + "".join(body) + '</svg>')


# ---------------------------------------------------------------------------

DIAGRAMS = {
    "mode-1-bbeat": diagram(
        nodes=[
            {"icon": "player", "title": "BBeat",
             "lines": ["live player", "backing tracks + MIDI"], "tag": "PLAYER"},
            {"icon": "board", "title": "tupperDMX-arduino",
             "lines": ["hardware controller", "MIDI note → DMX"], "tag": "CONTROLLER"},
            {"icon": "fixtures", "title": "The rig",
             "lines": ["real fixtures"], "tag": "OUTPUT"},
        ],
        links=[("MIDI",), ("DMX512",)]),

    "mode-2-laptop": diagram(
        nodes=[
            {"icon": "laptop", "title": "TupperDMX Simulator",
             "lines": ["full desktop app", "run by the drummer"],
             "tag": "PLAYER + CONTROLLER", "highlight": True},
            {"icon": "dongle", "title": "USB DMX interface",
             "lines": ["FTDI / serial"], "tag": "INTERFACE"},
            {"icon": "fixtures", "title": "The rig",
             "lines": ["real fixtures"], "tag": "OUTPUT"},
        ],
        links=[("USB",), ("DMX512",)]),

    "mode-3-rack": diagram(
        nodes=[
            {"icon": "tablet", "title": "Tablet",
             "lines": ["web remote", "the only interface"], "tag": "CONTROL"},
            {"icon": "rack", "title": "TupperDMX Simulator",
             "lines": ["SBC in the rack", "--headless, no screen"],
             "tag": "PLAYER + CONTROLLER", "highlight": True},
            {"icon": "dongle", "title": "USB DMX interface",
             "lines": ["FTDI / serial"], "tag": "INTERFACE"},
            {"icon": "fixtures", "title": "The rig",
             "lines": ["real fixtures"], "tag": "OUTPUT"},
        ],
        links=[("Wi-Fi",), ("USB",), ("DMX512",)]),
}


def main() -> int:
    IMAGES.mkdir(parents=True, exist_ok=True)
    for name, svg in DIAGRAMS.items():
        target = IMAGES / f"{name}.svg"
        target.write_text(svg, encoding="utf-8")
        print(f"wrote {target.relative_to(IMAGES.parents[1])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
