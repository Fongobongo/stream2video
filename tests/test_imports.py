"""Smoke tests: import every public module to catch import-time regressions.

These tests have no fixtures and don't exercise runtime behaviour. Their only
job is to fail fast if a module-level NameError, ImportError, or syntax error
slips in (e.g. a missing typing import). They previously would have caught
the ``Callable`` NameError in cli.py that broke the entire CLI entry point.
"""

import importlib

import pytest


class TestModuleImports:
    def test_import_config(self):
        importlib.import_module("stream2video.config")

    def test_import_utils(self):
        importlib.import_module("stream2video.utils")

    def test_import_download(self):
        importlib.import_module("stream2video.download")

    def test_import_silence(self):
        importlib.import_module("stream2video.silence")

    def test_import_concat(self):
        importlib.import_module("stream2video.concat")

    def test_import_cli(self):
        importlib.import_module("stream2video.cli")

    def test_import_gui(self):
        # gui.py transitively imports waveform.py (Pillow) and customtkinter.
        # Skip if the [gui] extra isn't installed rather than failing the
        # smoke test on a minimal CI env.
        pytest.importorskip("PIL", reason="gui.py requires Pillow ([gui] extra)")
        pytest.importorskip(
            "customtkinter", reason="gui.py requires customtkinter ([gui] extra)"
        )
        importlib.import_module("stream2video.gui")
