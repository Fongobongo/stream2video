"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

from collections.abc import Iterator
from tkinter import TclError
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _fake_output_media_validation():
    """Tests write dummy bytes as the pipeline output — the real
    ffmpeg validation seam (audit round 29 P0-2) would reject every
    one of them. Patch the seam suite-wide; tests that exercise the
    REAL validation override it with a local ``patch.object`` (an
    inner patch wins over this autouse one)."""
    with patch(
        "stream2video.pipeline_controller.PipelineController._output_media_is_valid",
        return_value=True,
    ):
        yield


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

    Function-scoped for tests that MUTATE GUI state. Event-loop tests
    drive Tk directly (``update()`` / ``update_idletasks()``); no Qt
    bindings are involved.
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
