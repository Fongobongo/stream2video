"""Regression tests for the Windows GUI launcher."""

from __future__ import annotations

from pathlib import Path


def test_run_gui_installs_gui_and_monitor_extras():
    text = (Path(__file__).parent.parent / "run_gui.cmd").read_text(encoding="utf-8")

    assert "import customtkinter; import PIL; import psutil" in text
    assert 'pip install -e "%~dp0.[gui,monitor]"' in text


def test_run_gui_launches_stream2video_gui_module():
    text = (Path(__file__).parent.parent / "run_gui.cmd").read_text(encoding="utf-8")

    assert "-m stream2video.gui" in text
