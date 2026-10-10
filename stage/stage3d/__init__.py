# ABOUTME: The EXPERIMENTAL 3D stage — the bar venue rendered from GLB assets, lit by
# ABOUTME: the show's patch — shown in place of the 2D stage behind a Settings checkbox.
"""The experimental 3D stage.

A prototype carried in the app so every build has it, not a part of the 2D
engine: the rules of `stage/` above do not bind it, and nothing outside
`embedded.py` is meant to be imported by the app. Import the submodule you want.

  * `bar.py` — the renderer, the scene and the `BarView` widget;
  * `models.py` — the patched fixtures' bodies, built at run time;
  * `patch.py` — picture coordinates and effect artwork;
  * `build_assets.py` — writes `assets/models/bar/*.glb`
    (`uv run python -m stage.stage3d.build_assets`); `models.py` borrows its
    primitives at run time, which is why it ships;
  * `embedded.py` — the app's one way in.

`scripts/stage3d_bar.py` runs it standalone, with a DMX feed of its own.
"""
