# ABOUTME: Runtime control of the PyInstaller boot splash — status line and bar.
# ABOUTME: Every call is a silent no-op unless this is a frozen build with one.
"""Talk to the boot splash screen the PyInstaller bootloader put up.

The splash is Tcl/Tk drawn by the bootloader, so it is already on screen while
the onefile archive is still unpacking — seconds before Python, let alone Qt,
exists. `pyi_splash` is injected into frozen builds only; in a development run
the import fails and every function here does nothing.

`update_text` is PyInstaller's own IPC command. `update_progress` is not: it is
an extra Tcl procedure `build.spec` patches into the splash script, so the bar
is only there when that patch is (see `_patch_splash_templates` in build.spec).
Both are best-effort — a splash that cannot be talked to must never take the
application down with it.
"""

from __future__ import annotations

from typing import Optional

try:  # Present only inside a PyInstaller build that declared a Splash target.
    import pyi_splash  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - the development path
    pyi_splash = None  # type: ignore[assignment]


def is_active() -> bool:
    """Whether there is a splash screen listening."""
    if pyi_splash is None:
        return False
    try:
        return bool(pyi_splash.is_alive())
    except Exception:
        return False


def update(text: Optional[str] = None, progress: Optional[float] = None) -> None:
    """Set the splash's status line and/or its progress bar (0.0 - 1.0)."""
    if not is_active():
        return
    if text is not None:
        try:
            pyi_splash.update_text(text)
        except Exception:
            pass
    if progress is not None:
        value = min(1.0, max(0.0, float(progress)))
        try:
            # Formatted, never handed over raw: the receiving Tcl trace runs the
            # value through `expr`, where anything unparseable is an error
            # inside the splash's own event loop.
            pyi_splash._send_command("update_progress", [f"{value:.4f}"])
        except Exception:
            # No bar in this build (or PyInstaller moved the private helper).
            pass


def close() -> None:
    """Take the splash down. Idempotent; safe when there never was one."""
    if pyi_splash is None:
        return
    try:
        pyi_splash.close()
    except Exception:
        pass
