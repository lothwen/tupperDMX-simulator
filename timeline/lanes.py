"""
Patch lookups behind the timeline's lanes.

Qt-free on purpose: which fixture a lane belongs to, what it is called and the
order the lanes stack in are questions about the PATCH, not about the widget —
so they are answerable (and testable) without a QApplication. The timeline
keeps one-line delegates because its tracks ask it, not this module.
"""

from typing import Dict, List

from core.fixture_library import resolve_fixture


def fixture_labels(ordered) -> List[str]:
    """Header label per patched fixture (in display order): its name, or the
    capitalized type (plus a 1-based ordinal when that type appears more than
    once in the patch) -> e.g. ['Ledbar', 'Blinder 1', 'Blinder 2']."""
    type_counts: Dict[str, int] = {}
    for inst in ordered:
        type_counts[inst.type] = type_counts.get(inst.type, 0) + 1
    seen: Dict[str, int] = {}
    labels: List[str] = []
    for inst in ordered:
        name = (getattr(inst, "name", "") or "").strip()
        if name:
            labels.append(name)
            continue
        cap = inst.type.capitalize()
        if type_counts.get(inst.type, 0) > 1:
            seen[inst.type] = seen.get(inst.type, 0) + 1
            labels.append(f"{cap} {seen[inst.type]}")
        else:
            labels.append(cap)
    return labels


def display_key(inst):
    """Sort key for the track strip: the row this lane was dragged to, else
    its DMX address.

    A fixture with no row of its own (`order` -1 — a freshly patched one,
    and every fixture in a show saved before the strip could be reordered)
    sorts below the placed ones and keeps DMX order among its own kind, so
    an untouched patch lays out exactly as it always did."""
    dmx = getattr(inst, "dmx_start", 0)
    order = getattr(inst, "order", -1)
    order = -1 if order is None else int(order)
    return (order, dmx) if order >= 0 else (float("inf"), dmx)


def fixture_for_channel(fixtures, channel):
    """The patched fixture instance with stable `channel`, or None.

    Falls back to positional lookup for synthetic fixtures built without a
    stable channel (the display row doubles as the channel there)."""
    if channel is None:
        return None
    for inst in fixtures:
        if getattr(inst, "channel", -1) == channel:
            return inst
    if 0 <= channel < len(fixtures):
        inst = fixtures[channel]
        if getattr(inst, "channel", -1) < 0:
            return inst
    return None


def fixture_type(fixtures, channel):
    """Type name of the patched fixture with stable `channel`, or None."""
    inst = fixture_for_channel(fixtures, channel)
    return inst.type if inst else None


def fdef(fixtures, channel):
    """FixtureDef of the patched fixture with stable `channel`, or None."""
    inst = fixture_for_channel(fixtures, channel)
    if inst is None:
        return None
    return resolve_fixture(inst.type, getattr(inst, "mode", ""))


def dmx_start(fixtures, channel) -> int:
    """First DMX address of the fixture with stable `channel` (1 if none)."""
    inst = fixture_for_channel(fixtures, channel)
    return int(getattr(inst, "dmx_start", 1) or 1) if inst else 1
