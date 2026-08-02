# ABOUTME: The base every emission model extends — how a fixture's light is drawn.
"""Emission models.

An emission is *the light*, as distinct from the fixture that makes it. Which
one a body uses is decided by its OFL category (and, where a category covers
both, by whether its channels describe a hard beam or a soft wash) — so a
moving head and a scanner can share the beam that a par can't have, and a pixel
bar draws cells where a blinder draws bulbs.

The contract is one method: read a `Frame` (whose `beam` the capability features
have already shaped) and repaint. An emission never reads capabilities itself;
that is the features' job, and keeping the line there is what lets a gobo work
on any fixture that has one.
"""

from typing import Optional

from ui.stage.frame import Frame


class Emission:
    """One body's light. Subclasses build their items and repaint them."""

    #: True when this emission changes between DMX frames on its own (an
    #: animation, a plume), so the body knows it must keep ticking.
    animates = False

    def __init__(self, body):
        self.body = body
        self.build()

    # -- subclass hooks ------------------------------------------------
    def build(self) -> None:
        """Create the child items. Called once, at construction."""

    def render(self, frame: Frame) -> None:
        """Repaint for one frame."""

    def cleanup(self) -> None:
        """Release anything that keeps ticking before the body is removed."""

    # -- geometry, before any DMX --------------------------------------
    @classmethod
    def reach(cls, fdef) -> float:
        """How far past the body this emission can throw light, in scene units.

        A class method because the scene preview has to size its viewport before
        it has built anything, let alone resolved a frame.
        """
        return 0.0


class NoEmission(Emission):
    """A fixture that emits no light: a stand, a fan, a bare chassis."""


def emission_reach(fdef) -> float:
    """How far past its body this fixture's light reaches.

    The single answer to "does this fixture draw a beam, and how long is it" —
    asked by the stage and by the scene preview, which would otherwise crop the
    cone or reserve empty space for one that never appears.
    """
    from ui.stage.registry import renderer_for
    return renderer_for(fdef).emission_for(fdef).reach(fdef)
