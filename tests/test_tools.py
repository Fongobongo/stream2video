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

    def test_wait_failed_terminates_and_does_not_report_success(self):
        """Audit round 16 P2: WAIT_FAILED (0xFFFFFFFF — the wait call
        itself failed, e.g. a broken shim) used to fall through to
        "spawn succeeded" and close the handles with the child still
        running. Now ANY non-WAIT_OBJECT_0 outcome terminates the child
        best-effort and reports what really happened."""
        result, calls = self._probe([0xFFFFFFFF, self.WAIT_OBJECT_0])
        assert "CreateProcessW OK" in result
        assert "spawn succeeded" not in result
        assert "hung child terminated" in result
        assert ("TerminateProcess", 1) in calls
        assert ("WaitForSingleObject", 10000) in calls
        assert calls.count("CloseHandle") == 2

    def test_second_wait_timeout_reports_child_still_alive(self):
        """The kill attempt itself can fail to settle the child (the
        second wait times out again). The probe must report the child as
        still alive instead of silently claiming success."""
        result, calls = self._probe([self.WAIT_TIMEOUT, self.WAIT_TIMEOUT])
        assert "CreateProcessW OK" in result
        assert "spawn succeeded" not in result
        assert "child still alive after terminate" in result
        assert ("TerminateProcess", 1) in calls
        assert calls.count("CloseHandle") == 2

    def test_terminate_rejected_reports_false_in_message(self):
        """If TerminateProcess itself returns False (rejected — access
        denied, already-dead race), the message must say so instead of
        implying the child was killed."""
        calls: list[tuple | str] = []

        class _RefusingTerminateKernel32:
            def __init__(self, name, use_last_error=False):
                pass

            def CreateProcessW(self, *a, **k):
                calls.append("CreateProcessW")
                return True

            def WaitForSingleObject(self, handle, timeout):
                calls.append(("WaitForSingleObject", timeout))
                return 0x00000102  # WAIT_TIMEOUT every time

            def TerminateProcess(self, handle, code):
                calls.append(("TerminateProcess", code))
                return False

            def CloseHandle(self, handle):
                calls.append("CloseHandle")

        result = self._probe_with_fake(_RefusingTerminateKernel32)
        assert "CreateProcessW OK" in result
        assert "spawn succeeded" not in result
        assert "child still alive after terminate" in result
        assert "TerminateProcess=False" in result
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
        """Audit round 16 P2: a RAISING TerminateProcess (not just a
        False return) inside the terminate-and-verify branch is kill
        data, not a probe failure — the message must report
        TerminateProcess=False and both handles must still be closed
        (the finally must not be skipped by the raise)."""
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
        assert "CreateProcessW OK" in result
        assert "spawn succeeded" not in result
        assert "TerminateProcess=False" in result
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


class TestSpawnWithRetryBehaviour:
    """Audit round 16 P3: the static inventory above only proves *where*
    subprocess is called, not *how* the retry layer behaves. These tests
    pin the runtime contract of _spawn_with_retry: retry count, re-
    resolution of cmd[0] through ffmpeg_path/ffprobe_path, creationflags
    handling on retries (bit-cleared on nt, stripped on posix), encoder
    cache resets, and re-raise of the last FileNotFoundError."""

    def _spawn(self, kind, cmd, kwargs, fn_side_effect, os_name=None, record=None):
        from contextlib import ExitStack

        import stream2video.tools as tools_mod
        from stream2video.tools import _spawn_with_retry

        probe_calls: list[str] = []
        with ExitStack() as stack:
            fn_name = "Popen" if kind == "popen" else "run"
            stack.enter_context(
                patch.object(tools_mod.subprocess, fn_name, side_effect=fn_side_effect)
            )
            stack.enter_context(
                patch.object(
                    tools_mod,
                    "_createprocess_probe",
                    side_effect=lambda exe: probe_calls.append(exe) or "probe n/a (test)",
                )
            )
            stack.enter_context(patch.object(tools_mod.time, "sleep"))
            stack.enter_context(
                patch.object(tools_mod, "ffmpeg_path", return_value="C:/resolved/ffmpeg.exe")
            )
            stack.enter_context(
                patch.object(tools_mod, "ffprobe_path", return_value="C:/resolved/ffprobe.exe")
            )
            reset_cache = stack.enter_context(patch.object(tools_mod, "reset_tool_cache"))
            reset_enc = stack.enter_context(
                patch("stream2video.concat.encoders.reset_encoder_check_cache")
            )
            if os_name is not None:
                stack.enter_context(patch.object(tools_mod.os, "name", os_name))
            if record is not None:
                # Populated BEFORE the call so callers can inspect the
                # mocks even when _spawn_with_retry re-raises.
                record["probe_calls"] = probe_calls
                record["reset_cache"] = reset_cache
                record["reset_enc"] = reset_enc
            result = _spawn_with_retry(kind, list(cmd), dict(kwargs))
        return result, probe_calls, reset_cache, reset_enc

    @staticmethod
    def _failing_until(calls: list, fail_count: int, success):
        def fake(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if len(calls) <= fail_count:
                raise FileNotFoundError(2, "no such file", cmd[0])
            return success

        return fake

    def test_run_retries_then_succeeds(self):
        calls: list = []
        result, probes, reset_cache, reset_enc = self._spawn(
            "run",
            ["ffmpeg", "-version"],
            {"creationflags": 0x08000000 | 0x40000000},
            self._failing_until(calls, 2, "ok"),
            os_name="nt",
        )
        assert result == "ok"
        assert len(calls) == 3
        # First attempt: the caller's kwargs pass through untouched.
        assert calls[0][0][0] == "ffmpeg"
        assert calls[0][1]["creationflags"] == 0x08000000 | 0x40000000
        # Retries: cmd[0] re-resolved; ONLY the CREATE_NO_WINDOW bit
        # dropped — the priority class bit survives (audit round 15 P1).
        for call in calls[1:]:
            assert call[0][0] == "C:/resolved/ffmpeg.exe"
            assert call[1]["creationflags"] == 0x40000000
        # Probe ran for each failure, with the cmd[0] of that attempt.
        assert probes == ["ffmpeg", "C:/resolved/ffmpeg.exe"]
        assert reset_cache.call_count == 2
        assert reset_enc.call_count == 2

    def test_popen_retries_then_succeeds(self):
        calls: list = []
        sentinel = object()
        result, probes, reset_cache, reset_enc = self._spawn(
            "popen",
            ["ffmpeg", "-i", "in.mp4"],
            {},
            self._failing_until(calls, 1, sentinel),
            os_name="nt",
        )
        assert result is sentinel
        assert len(calls) == 2
        assert calls[1][0][0] == "C:/resolved/ffmpeg.exe"
        assert len(probes) == 1
        assert reset_cache.call_count == 1
        assert reset_enc.call_count == 1

    @pytest.mark.parametrize("kind", ["run", "popen"])
    def test_exhausts_attempts_raises_last_fnf(self, kind):
        calls: list = []

        def forever_failing(cmd, **kwargs):
            calls.append((cmd, kwargs))
            raise FileNotFoundError(2, "no such file", cmd[0])

        with pytest.raises(FileNotFoundError) as excinfo:
            record: dict = {}
            self._spawn(
                kind, ["ffmpeg", "-version"], {}, forever_failing, os_name="nt", record=record
            )
        assert len(calls) == 4  # 1 initial + _SPAWN_RETRY_ATTEMPTS=3
        # The re-raised exception carries the re-resolved filename of the
        # LAST attempt, not the original bare "ffmpeg".
        assert excinfo.value.filename == "C:/resolved/ffmpeg.exe"
        # Probe ran for every failure INCLUDING the last one (it fires
        # before the exhaustion break); cache resets only for the three
        # failures that had a next attempt.
        assert len(record["probe_calls"]) == 4
        assert record["reset_cache"].call_count == 3
        assert record["reset_enc"].call_count == 3

    def test_posix_retry_strips_creationflags(self):
        calls: list = []
        result, _, _, _ = self._spawn(
            "run",
            ["ffmpeg", "-version"],
            {"creationflags": 0x08000000},
            self._failing_until(calls, 1, "ok"),
            os_name="posix",
        )
        assert result == "ok"
        assert len(calls) == 2
        assert calls[0][1]["creationflags"] == 0x08000000
        # creationflags is Windows-only plumbing: on posix it must be
        # removed entirely, not zeroed (a non-zero value raises
        # ValueError in subprocess, hiding the retry path).
        assert "creationflags" not in calls[1][1]

    def test_non_tool_cmd_not_re_resolved(self):
        calls: list = []
        result, _, _, _ = self._spawn(
            "run",
            ["python", "-c", "print(1)"],
            {},
            self._failing_until(calls, 1, "ok"),
            os_name="nt",
        )
        assert result == "ok"
        assert len(calls) == 2
        # Only ffmpeg/ffprobe commands get re-resolved; a plain command
        # retries with the same cmd[0].
        assert calls[0][0][0] == "python"
        assert calls[1][0][0] == "python"

    def test_ffprobe_cmd_re_resolved_via_ffprobe_path(self):
        calls: list = []
        result, probes, _, _ = self._spawn(
            "run",
            ["ffprobe", "-version"],
            {},
            self._failing_until(calls, 1, "ok"),
            os_name="nt",
        )
        assert result == "ok"
        assert calls[1][0][0] == "C:/resolved/ffprobe.exe"
        assert probes == ["ffprobe"]
