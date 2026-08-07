# ABOUTME: The GPU light layer package — HDR accumulation and ACES tonemapping
# ABOUTME: for the virtual stage, kept behind an import that never fails.
"""GPU rendering for the stage's light.

Deliberately NOT re-exported eagerly: importing `stage.gpu.layer` pulls in
`PyQt6.QtOpenGL`, which is not something the raster path, the tests or a
headless run should ever need. Import the submodule where it is used.
"""
