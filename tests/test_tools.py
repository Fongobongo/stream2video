"""Tests for stream2video.tools — binary resolution + ffmpeg option fork."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

import stream2video
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

    def _probe_with_fake(self, fake_kernel32_factory):
        import ctypes as _ctypes_mod

        from stream2video.tools import _createprocess_probe

        with patch.object(_ctypes_mod, "WinDLL", fake_kernel32_factory):
            result = _createprocess_probe("ffmpeg.exe")
        return result

    def test_exception_after_create_terminates_and_closes_handles(self):
        """Audit round 15 P2: an exception raised after CreateProcessW
        (a broken shim's weird return, a ValueError from a driver that
        rejects our handle, etc.) used to skip cleanup entirely — both
        handles leaked AND the child stayed alive. The probe must
        terminate the child best-effort and still close both handles."""
        calls: list[tuple | str] = []

        class _ExplodingWaitKernel32:
            def __init__(self, name, use_last_error=False):
                pass

            def CreateProcessW(self, *a, **k):
                calls.append("CreateProcessW")
                return True

            def WaitForSingleObject(self, handle, timeout):
                calls.append(("WaitForSingleObject", timeout))
                raise RuntimeError("wait exploded")

            def TerminateProcess(self, handle, code):
                calls.append(("TerminateProcess", code))
                return True

            def CloseHandle(self, handle):
                calls.append("CloseHandle")

        result = self._probe_with_fake(_ExplodingWaitKernel32)
        assert result == "probe raised RuntimeError: wait exploded"
        assert ("WaitForSingleObject", 2000) in calls
        # Best-effort kill attempted even though we never confirmed exit.
        assert ("TerminateProcess", 1) in calls
        assert calls.count("CloseHandle") == 2

    def test_exception_on_terminate_still_closes_handles(self):
        """If even the kill call raises, the probe must still report and
        still close both handles — the finally must not be skipped by a
        raise from inside the except block."""
        calls: list[tuple | str] = []

        class _ExplodingTerminateKernel32:
            def __init__(self, name, use_last_error=False):
                pass

            def CreateProcessW(self, *a, **k):
                calls.append("CreateProcessW")
                return True

            def WaitForSingleObject(self, handle, timeout):
                calls.append(("WaitForSingleObject", timeout))
                return 0x00000102  # WAIT_TIMEOUT

            def TerminateProcess(self, handle, code):
                calls.append(("TerminateProcess", code))
                raise OSError("kill exploded")

            def CloseHandle(self, handle):
                calls.append("CloseHandle")

        result = self._probe_with_fake(_ExplodingTerminateKernel32)
        assert result == "probe raised OSError: kill exploded"
        assert calls.count("CloseHandle") == 2


class TestNoDirectSpawnOutsideRetryLayer:
    """Audit round 15 P2: every ffmpeg/ffprobe spawn in the package must
    go through run_with_retry / popen_with_retry. This static inventory
    pins that contract by scanning the production sources for raw
    subprocess.run / subprocess.Popen calls — if a new caller adds one
    outside the allowed modules it must be routed through the retry
    layer (or be an explicitly non-ffmpeg player launcher like the
    POSIX completion-sound / xdg-open launchers)."""

    ALLOWED_DIRECT_SPAWN_MODULES: ClassVar[frozenset[str]] = frozenset(
        {
            "tools.py",  # the retry layer itself (subprocess_kwargs_lowest + run_with_retry etc.)
            # concat/runner.py — the low-level ffmpeg exec phase. Unlike the
            # probe/bitrate/waveform paths, a missing ffmpeg here must FAIL
            # the pipeline (not silently retry), and the spawns are managed
            # by the scoped supervisor (cancel/on-close kill, timeout, output
            # registration) which popen_with_retry does not provide.
            "runner.py",
            # POSIX players afplay/paplay/aplay/ffplay — never the WinGet shim path.
            "completion_sound.py",
            "gui_platform.py",  # open / xdg-open URL launcher
        }
    )

    @pytest.mark.parametrize(
        "call",
        ["subprocess.run(", "subprocess.Popen(", "subprocess.check_call("],
    )
    def test_direct_spawns_confined_to_retry_layer(self, call: str):
        package_dir = Path(stream2video.__file__).parent
        offenders: list[str] = []
        for py_file in sorted(package_dir.rglob("*.py")):
            if py_file.name in self.ALLOWED_DIRECT_SPAWN_MODULES:
                continue
            text = py_file.read_text(encoding="utf-8")
            if call in text:
                offenders.append(str(py_file.relative_to(package_dir)))
        assert offenders == [], (
            f"direct {call!r} outside retry layer: {offenders} — "
            "route through tools.run_with_retry / popen_with_retry"
        )
