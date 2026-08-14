# ABOUTME: Leaf widget toolkit — icons, the toggle switch, chips and the one
# ABOUTME: FlowLayout. Knows nothing about the app, so anything may import it.
"""Reusable widgets with no application knowledge.

This is the bottom of the UI stack: nothing here reads `Config`, a `FixtureDef`
or a controller, so importing it can never pull the app in behind it. That is
the point — `timeline/` imports `icons` from here, and used to pay for the
whole render engine to do it.

No eager re-exports: `from ui.widgets import icons`.
"""
