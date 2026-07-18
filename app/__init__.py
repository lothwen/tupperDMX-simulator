# ABOUTME: Shared application core: the widget-free orchestration layer both
# ABOUTME: the desktop MainWindow and the headless runner consume. May import
# ABOUTME: QtCore/QtMultimedia, never QtWidgets or ui/. No eager re-exports —
# ABOUTME: import submodules directly (e.g. `from app.bpm import BpmState`).
