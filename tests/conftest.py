"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

from collections.abc import Iterator
from tkinter import TclError

import pytest


def _spawn_gui() -> Iterator[object]:
    """Shared init/teardown for both GUI fixture scopes."""
    pytest.importorskip("PIL", reason="gui.py requires Pillow ([gui] extra)")
    pytest.importorskip("customtkinter", reason="gui.py requires customtkinter ([gui] extra)")
    from stream2video.gui import Stream2VideoGUI

    try:
        app = Stream2VideoGUI()
    except TclError as e:
        pytest.skip(f"headless environment — TclError on init: {e}")
    # Flush pending idle tasks so widgets are queryable.
    try:
        app.update_idletasks()
    except TclError as e:
        pytest.skip(f"Tk idle tasks failed (no display?): {e}")
    yield app
    try:
        app.destroy()
    except TclError:
        pass


@pytest.fixture
def gui():
    """Instantiate Stream2VideoGUI per test; skip on headless envs.

    Function-scoped for tests that MUTATE GUI state. pytest-qt's
    ``qtbot.addWidget`` can't track Tkinter widgets (it expects
    ``QWidget`` subclasses), so tests that need a live loop use plain Tk
    ``update()`` / ``update_idletasks()`` directly.
    """
    yield from _spawn_gui()


@pytest.fixture(scope="module")
def gui_module():
    """Module-scoped GUI instance for read-only widget-state queries.

    Tk init is slow (hundreds of ms on Windows); smoke tests that only
    inspect widget state share one instance per module. Tests that
    MUTATE GUI state must use the function-scoped ``gui`` fixture
    instead to avoid cross-test bleed.
    """
    yield from _spawn_gui()
