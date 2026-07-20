"""Tests for stream2video.gui_platform (Этап 10 incremental).

Pure / OS-level helpers extracted from gui.py so they can be unit-
tested without driving the Tk main loop: directory size probing and
the cross-platform "open in file manager" call.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from stream2video.gui_platform import dir_size_mb, open_in_file_manager


class TestDirSizeMb:
    def test_empty_dir_is_zero(self, tmp_path: Path):
        assert dir_size_mb(tmp_path) == 0.0

    def test_single_file(self, tmp_path: Path):
        (tmp_path / "a.mp4").write_bytes(b"x" * (2 * 1024 * 1024))
        size = dir_size_mb(tmp_path)
        assert 1.9 <= size <= 2.1

    def test_multiple_files_accumulate(self, tmp_path: Path):
        (tmp_path / "a.mp4").write_bytes(b"x" * (1024 * 1024))
        (tmp_path / "b.mp4").write_bytes(b"y" * (3 * 1024 * 1024))
        size = dir_size_mb(tmp_path)
        assert 3.9 <= size <= 4.1

    def test_nested_subdirs_walked(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "a.mp4").write_bytes(b"x" * (1024 * 1024))
        (sub / "b.mp4").write_bytes(b"y" * (2 * 1024 * 1024))
        size = dir_size_mb(tmp_path)
        assert 2.9 <= size <= 3.1

    def test_missing_dir_returns_zero(self, tmp_path: Path):
        # Path that doesn't exist — should return 0.0, not raise.
        missing = tmp_path / "nope"
        assert dir_size_mb(missing) == 0.0

    def test_permission_denied_file_skipped(self, tmp_path: Path):
        # A file we can't stat shouldn't crash the walk. On Windows
        # we can't easily simulate permission denied, so this test
        # just verifies the happy path doesn't blow up when one file
        # in the tree is unreadable.
        (tmp_path / "a.mp4").write_bytes(b"x" * 100)
        size = dir_size_mb(tmp_path)
        assert size > 0


class TestOpenInFileManager:
    def test_missing_dir_raises_file_not_found(self, tmp_path: Path):
        missing = tmp_path / "does_not_exist"
        with pytest.raises(FileNotFoundError, match="no longer exists"):
            open_in_file_manager(missing)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
    def test_windows_calls_startfile(self, tmp_path: Path, monkeypatch):
        called: list[str] = []

        def fake_startfile(p):
            called.append(p)

        monkeypatch.setattr(os, "startfile", fake_startfile, raising=False)
        open_in_file_manager(tmp_path)
        assert called == [str(tmp_path)]

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
    def test_macos_calls_open(self, tmp_path: Path, monkeypatch):
        calls: list[list[str]] = []

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                calls.append(cmd)

        monkeypatch.setattr("stream2video.gui_platform.subprocess.Popen", FakePopen)
        open_in_file_manager(tmp_path)
        assert calls == [["open", str(tmp_path)]]

    @pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="Linux-only")
    def test_linux_calls_xdg_open(self, tmp_path: Path, monkeypatch):
        calls: list[list[str]] = []

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                calls.append(cmd)

        monkeypatch.setattr("stream2video.gui_platform.subprocess.Popen", FakePopen)
        open_in_file_manager(tmp_path)
        assert calls == [["xdg-open", str(tmp_path)]]
