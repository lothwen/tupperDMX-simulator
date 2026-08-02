# ABOUTME: The base and registry for capability features — the second stage of
# ABOUTME: rendering, where a fixture's channels modify the light its body makes.
"""Capability features.

The category decided what kind of light this fixture makes. A feature decides
what its *channels* do to that light: a gobo puts a pattern in it, an iris
narrows it, a shutter chops it up, pan and tilt point it somewhere.

Each feature names the OFL capability types it consumes, and the body attaches
it only if the fixture actually declares one of them — **once, at construction**,
never per frame. That is the whole graceful-degradation story: a fixture
categorised "Moving Head" that turns out to be a wash with no gobo wheel gets no
gobo layer, because there is no gobo channel for one to read. Nothing is ever
special-cased by fixture name.
"""

from typing import List, Set, Type

from ui.stage.frame import Frame


class Feature:
    """One capability-driven modifier of a body's light."""

    #: The OFL capability types this feature reads. A fixture declaring none of
    #: them doesn't get the feature at all.
    types: frozenset = frozenset()

    #: Where in the chain it runs. Low numbers first: level and colour before
    #: the things that shape them, aiming last.
    order: int = 50

    #: True when the feature changes between DMX frames on its own — a strobe,
    #: a spinning gobo, a head still slewing toward its target.
    animates: bool = False

    def __init__(self, body):
        self.body = body

    @classmethod
    def claims(cls, fdef, present: Set[str], body) -> bool:
        """Whether this fixture justifies the feature.

        The default is "it declares one of my capability types". A profile can
        force one on regardless by naming it in `forced_features` — a moving
        head gets its aiming layer even if it somehow declares only Tilt.
        """
        if cls.__name__ in getattr(body, "forced_features", ()):
            return True
        return bool(cls.types & present)

    def apply(self, frame: Frame) -> None:
        """Modify `frame.beam`."""

    def cleanup(self) -> None:
        """Release anything that keeps ticking."""


FEATURES: List[Type[Feature]] = []


def register_feature(cls: Type[Feature]) -> Type[Feature]:
    """Class decorator adding a feature to the discovery list."""
    FEATURES.append(cls)
    return cls


def build_features(fdef, body) -> List[Feature]:
    """The layers this fixture's channels justify, in application order."""
    from core.fixture_library import all_capability_types

    present = all_capability_types(fdef)
    return [cls(body) for cls in sorted(FEATURES, key=lambda c: c.order)
            if cls.claims(fdef, present, body)]
