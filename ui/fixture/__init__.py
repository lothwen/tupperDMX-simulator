# ABOUTME: The fixture-editing subsystem: browse and patch a type, edit its
# ABOUTME: library scenes, drive its channels, place it on the stage.
"""Everything that edits a fixture.

Four surfaces over one domain, plus the stage designer that patches them:

- `browser` — the fixture browser and the patch decision (DMX start, MIDI
  channel, mode). `AddFixtureDialog`, `suggest_free_dmx_start`,
  `used_midi_channels`.
- `scenes_tab` — edits a *type's* sidecar scene banks, i.e. the library, not
  the patch.
- `console` — one fixture's channels as faders over a live `ScenePreview`,
  shared by the scenes tab and the timeline's clip editor.
- `affordances` — controls that beat a fader, chosen from the fixture's
  declared capabilities.
- `designer` — the stage edit toolbar and fixture inspector.

`timeline/widgets/clip_edit.py` and `verb_editors.py` reach in for `console`
and `affordances`; that is the whole of the timeline's dependency on `ui`
beyond `ui.widgets`.

No eager re-exports: `from ui.fixture.console import FaderStrip`.
"""
