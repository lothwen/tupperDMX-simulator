"""Fixture-type presentation colours.

Each patched fixture is coloured by its *type* so a fixture's clip-library
section, its timeline clips and its track-header swatch all read as one group.
The clip library and the timeline both derive the colour from here so the two
never drift.
"""

from typing import List

# Palette cycled across patched fixture types, ordered by DMX address.
GROUP_COLORS = ['#4a90e2', '#e2904a', '#4caf50', '#e24a4a', '#9b59b6',
                '#e24ad0', '#4ad0e2', '#7f8c8d', '#1abc9c', '#d4b106']


def patched_types(fixtures) -> List[str]:
    """Unique fixture-type names, deduped, ordered by DMX address."""
    seen: List[str] = []
    for inst in sorted(fixtures, key=lambda f: getattr(f, "dmx_start", 0)):
        t = getattr(inst, "type", None)
        if t and t not in seen:
            seen.append(t)
    return seen


def color_for_type(fixtures, ftype: str) -> str:
    """Hex colour assigned to `ftype` within `fixtures` (cycles GROUP_COLORS).

    Falls back to the first palette entry for an unpatched/unknown type."""
    types = patched_types(fixtures)
    if ftype in types:
        return GROUP_COLORS[types.index(ftype) % len(GROUP_COLORS)]
    return GROUP_COLORS[0]
