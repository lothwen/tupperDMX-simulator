# ABOUTME: The desktop view. Import submodules directly — this package holds no
# ABOUTME: eager re-exports, so `from ui.widgets import icons` costs an icon factory.
"""The desktop UI.

Deliberately empty, like `core/`, `app/` and `stage/`: re-exporting the window,
the dialogs and the panels from here meant that importing *anything* under `ui`
executed all of them, and `from ui.widgets import icons` — which the timeline
tracks do — dragged in the stage render engine, the MIDI worker, the DMX
manager and the web remote. Import the submodule you want.

Three layers, from the bottom up:

- `ui.widgets` — leaf toolkit. No app knowledge, safe for anyone to import.
- `ui.fixture` — the fixture-editing subsystem (browser, scenes tab, console,
  affordances, stage designer). The timeline borrows its console.
- everything else — the window, its dialogs, panels and toolbars.
"""
