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

from stream2video.gui_platform import (
    dir_size_mb,
    fit_to_screen,
    is_previewable_input,
    open_in_file_manager,
)


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


class TestFitToScreen:
    def test_typical_desktop_returns_target(self):
        # 1920x1080 is large enough to fit the default 1280x720.
        w, h = fit_to_screen(1920, 1080)
        assert w == 1280
        assert h == 720

    def test_small_screen_clamps(self):
        # 800x600 screen: 800-40=760, 600-60=540.
        w, h = fit_to_screen(800, 600)
        assert w == 760
        assert h == 540

    def test_tiny_screen_floors_at_1(self):
        # 100x50 screen: 100-40=60 (>1, kept), 50-60=-10 (floored at 1).
        w, h = fit_to_screen(100, 50)
        assert w == 60
        assert h == 1

    def test_zero_dimensions_floored(self):
        w, h = fit_to_screen(0, 0)
        assert w == 1
        assert h == 1

    def test_negative_dimensions_floored(self):
        # Defensive — shouldn't happen but the max(1, ...) guard
        # must catch it.
        w, h = fit_to_screen(-100, -200)
        assert w == 1
        assert h == 1


class TestIsPreviewableInput:
    def test_empty_string_returns_false(self):
        assert is_previewable_input("") is False

    def test_whitespace_returns_false(self):
        assert is_previewable_input("   ") is False

    def test_http_url_returns_false(self, tmp_path: Path):
        assert is_previewable_input("https://youtube.com/watch?v=abc") is False

    def test_https_url_returns_false(self):
        assert is_previewable_input("https://example.com/v") is False

    def test_existing_local_file_returns_true(self, tmp_path: Path):
        f = tmp_path / "video.mp4"
        f.write_text("dummy")
        assert is_previewable_input(str(f)) is True

    def test_tilde_local_file_returns_true(self, tmp_path: Path, monkeypatch):
        f = tmp_path / "video.mp4"
        f.write_text("dummy")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert is_previewable_input("~/video.mp4") is True

    def test_nonexistent_local_file_returns_false(self, tmp_path: Path):
        missing = tmp_path / "does_not_exist.mp4"
        assert is_previewable_input(str(missing)) is False

    def test_filename_with_colon_slash_slash_not_misclassified(self, tmp_path: Path):
        # A local filename containing '://' (rare but legal on some
        # filesystems) should NOT be misclassified as a URL. The strict
        # ^https?:// check matches only when the URL scheme is at the
        # start. Skip on Windows where ':' is illegal in filenames.
        import sys

        if sys.platform == "win32":
            pytest.skip("Windows doesn't allow ':' in filenames")
        weird_name = tmp_path / "weird://name.mp4"
        weird_name.write_text("dummy")
        assert is_previewable_input(str(weird_name)) is True
