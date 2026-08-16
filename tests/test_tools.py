"""Tests for stream2video.tools — binary resolution + ffmpeg option fork."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

import pytest

from stream2video.tools import (
    _ffmpeg_major_minor,
    filter_complex_script_args,
    reset_tool_cache,
)


def _version_run(stdout: str):
    """A ``subprocess.run`` stand-in that returns a fixed -version banner."""

    def _run(cmd, **kwargs):
        class _Proc:
            pass

        p = _Proc()
        p.stdout = stdout
        p.stderr = ""
        p.returncode = 0
        return p

    return _run


@pytest.fixture(autouse=True)
def _clear_caches():
    """Both probes cache per-process; every test starts from a clean slate."""
    reset_tool_cache()
    yield
    reset_tool_cache()


class TestFFmpegVersionProbe:
    """_ffmpeg_major_minor — banner parsing must survive every real-world
    spelling (release tags, git hashes, gyan.dev suffixes) or fall back
    to None (→ legacy option), never raise."""

    @pytest.mark.parametrize(
        ("banner", "expected"),
        [
            ("ffmpeg version 8.1.1-full_build-www.gyan.dev Copyright", (8, 1)),
            ("ffmpeg version 9.0.1-essentials_build-www.gyan.dev Copyright", (9, 0)),
            ("ffmpeg version 6.1.1 Copyright (c) 2000", (6, 1)),
            ("ffmpeg version 7.0 Copyright", (7, 0)),
            ("ffmpeg version n7.1.1 Copyright", (7, 1)),
        ],
    )
    def test_parses_release_banners(self, banner, expected):
        with (
            patch("stream2video.tools.ffmpeg_path", return_value="ffmpeg"),
            patch("stream2video.tools.subprocess.run", side_effect=_version_run(banner)),
        ):
            assert _ffmpeg_major_minor() == expected

    def test_git_build_without_version_returns_none(self):
        with (
            patch("stream2video.tools.ffmpeg_path", return_value="ffmpeg"),
            patch("stream2video.tools.subprocess.run", side_effect=_version_run("ffmpeg version ")),
        ):
            assert _ffmpeg_major_minor() is None

    def test_unparseable_banner_returns_none(self):
        with (
            patch("stream2video.tools.ffmpeg_path", return_value="ffmpeg"),
            patch("stream2video.tools.subprocess.run", side_effect=_version_run("garbage")),
        ):
            assert _ffmpeg_major_minor() is None

    def test_spawn_failure_returns_none(self):
        with (
            patch("stream2video.tools.ffmpeg_path", return_value="ffmpeg"),
            patch(
                "stream2video.tools.subprocess.run", side_effect=subprocess.SubprocessError("boom")
            ),
        ):
            assert _ffmpeg_major_minor() is None


class TestFilterComplexScriptArgs:
    """filter_complex_script_args — the 9.x builds removed the legacy flag,
    so the fork must pick ``-/filter_complex`` on >= 7 and the legacy
    spelling everywhere else (including unparseable version)."""

    def _args_for(self, version: tuple[int, int] | None) -> list[str]:
        with patch("stream2video.tools._ffmpeg_major_minor", return_value=version):
            return filter_complex_script_args("graph.txt")

    @pytest.mark.parametrize("version", [(7, 0), (8, 1), (9, 0), (10, 5)])
    def test_modern_builds_use_dash_slash(self, version):
        assert self._args_for(version) == ["-/filter_complex", "graph.txt"]

    @pytest.mark.parametrize("version", [(6, 1), (2, 0), (5, 2)])
    def test_legacy_builds_use_legacy_flag(self, version):
        assert self._args_for(version) == ["-filter_complex_script", "graph.txt"]

    def test_unparseable_version_assumes_legacy(self):
        assert self._args_for(None) == ["-filter_complex_script", "graph.txt"]


class TestResetToolCache:
    def test_reset_includes_version_probe(self):
        """reset_tool_cache must clear the version probe too, otherwise a
        patched-path test would inherit a previously-probed result."""
        with (
            patch("stream2video.tools.ffmpeg_path", return_value="ffmpeg"),
            patch(
                "stream2video.tools.subprocess.run",
                side_effect=_version_run("ffmpeg version 9.0.1"),
            ),
        ):
            first = _ffmpeg_major_minor()
        reset_tool_cache()
        with (
            patch("stream2video.tools.ffmpeg_path", return_value="ffmpeg"),
            patch(
                "stream2video.tools.subprocess.run",
                side_effect=_version_run("ffmpeg version 6.1.1"),
            ),
        ):
            second = _ffmpeg_major_minor()
        assert first == (9, 0)
        assert second == (6, 1)


@pytest.mark.skipif(os.name != "nt", reason="CreateProcessW is Windows-only")
class TestCreateProcessProbe:
    """_createprocess_probe — the diagnostic CreateProcessW probe must
    never leave an orphaned ffmpeg behind (audit round 14 P2): a child
    that doesn't exit within the wait window is terminated explicitly
    BEFORE the handles are closed, so the emergency retry branch can't
    stack a second hung process on top of the original spawn failure."""

    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102

    def _probe(self, wait_results: list[int]):
        calls: list[tuple | str] = []

        class _FakeKernel32:
            def __init__(self, name, use_last_error=False):
                pass

            def CreateProcessW(self, *a, **k):
                calls.append("CreateProcessW")
                return True

            def WaitForSingleObject(self, handle, timeout):
                calls.append(("WaitForSingleObject", timeout))
                return wait_results.pop(0)

            def TerminateProcess(self, handle, code):
                calls.append(("TerminateProcess", code))
                return True

            def CloseHandle(self, handle):
                calls.append("CloseHandle")

        import ctypes as _ctypes_mod

        from stream2video.tools import _createprocess_probe

        # patch.object instead of a dotted patch target:
        # ``ctypes`` is a C-extension module whose attributes can't be
        # resolved through an import-path patch.
        with patch.object(_ctypes_mod, "WinDLL", _FakeKernel32):
            result = _createprocess_probe("ffmpeg.exe")
        return result, calls

    def test_prompt_exit_no_terminate(self):
        result, calls = self._probe([self.WAIT_OBJECT_0])
        assert "CreateProcessW OK" in result
        assert all(c != ("TerminateProcess", 1) for c in calls)
        assert ("WaitForSingleObject", 2000) in calls
        assert calls.count("CloseHandle") == 2

    def test_timeout_terminates_before_handles_closed(self):
        result, calls = self._probe([self.WAIT_TIMEOUT, self.WAIT_OBJECT_0])
        assert "CreateProcessW OK" in result
        assert ("WaitForSingleObject", 2000) in calls
        assert ("TerminateProcess", 1) in calls
        # The second wait gives the terminated child time to signal
        # before the handle close.
        assert ("WaitForSingleObject", 10000) in calls
        assert calls.count("CloseHandle") == 2
