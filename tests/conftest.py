# ABOUTME: pytest configuration — headless Qt + a shared QApplication fixture.
"""Shared pytest setup for the test suite.

Forces the offscreen Qt platform so the GUI-touching tests (e.g. the timeline
crash-repro) run on headless CI, and exposes a single session-scoped
QApplication — Qt permits only one per process, so every test that needs one
shares it.
"""

import os

# Must be set before any QtWidgets/QGuiApplication import happens.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(scope="session")
def qapp():
    """A single QApplication shared across the whole test session."""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
