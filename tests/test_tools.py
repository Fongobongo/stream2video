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

            def CreateJobObjectW(self, *a, **k):
                calls.append("CreateJobObjectW")
                return 123

            def SetInformationJobObject(self, job, cls, info, size):
                calls.append(("SetInformationJobObject", cls))
                return True

            def CreateProcessW(self, *a, **k):
                calls.append("CreateProcessW")
                return True

            def InitializeProcThreadAttributeList(self, attr_list, count, flags, size_ptr):
                calls.append(("InitializeProcThreadAttributeList", attr_list is None))
                if attr_list is None:
                    size_ptr._obj.value = 64
                return True

            def UpdateProcThreadAttribute(self, attr_list, flags, attr, value, size, prev, retsize):
                calls.append(("UpdateProcThreadAttribute", attr))
                return True

            def DeleteProcThreadAttributeList(self, attr_list):
                calls.append("DeleteProcThreadAttributeList")

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

    def test_suspended_child_always_terminated(self):
        """Audit round 19 P2: the child is spawned CREATE_SUSPENDED, so
        it can never exit on its own — the old 2s "natural exit" wait
        was provably futile. The probe must terminate immediately and
        verify with the single bounded post-kill wait."""
        result, calls = self._probe([self.WAIT_OBJECT_0])
        assert "CreateProcessW OK" in result
        assert "spawn ok, suspended child terminated" in result
        assert ("TerminateProcess", 1) in calls
        # One bounded post-kill wait only — no dead 2s first wait.
        assert ("WaitForSingleObject", 10000) in calls
        assert ("WaitForSingleObject", 2000) not in calls
        # job + process + thread.
        assert calls.count("CloseHandle") == 3

    def test_post_kill_wait_timeout_reports_child_still_alive(self):
        """The kill attempt can fail to settle the child (the post-kill
        wait times out). The probe must report the child as still alive
        instead of silently claiming success."""
        result, calls = self._probe([self.WAIT_TIMEOUT])
        assert "CreateProcessW OK" in result
        assert "spawn ok" not in result
        assert "child still alive after terminate" in result
        assert "reaped by job KILL_ON_JOB_CLOSE" in result
        assert ("TerminateProcess", 1) in calls
        assert ("WaitForSingleObject", 10000) in calls
        assert calls.count("CloseHandle") == 3

    def test_wait_failed_reports_child_still_alive(self):
        """WAIT_FAILED (0xFFFFFFFF — the wait call itself failed, e.g. a
        broken shim) must never read as success (audit round 16 P2): the
        kill is reported as unverified and the child as possibly alive."""
        result, calls = self._probe([0xFFFFFFFF])
        assert "CreateProcessW OK" in result
        assert "spawn ok" not in result
        assert "child still alive after terminate" in result
        assert "reaped by job KILL_ON_JOB_CLOSE" in result
        assert ("TerminateProcess", 1) in calls
        assert ("WaitForSingleObject", 10000) in calls
        assert calls.count("CloseHandle") == 3

    def test_terminate_rejected_reports_false_in_message(self):
        """If TerminateProcess itself returns False (rejected — access
        denied, already-dead race), the message must say so instead of
        implying the child was killed."""
        result, calls = self._probe_with_job([self.WAIT_TIMEOUT], terminate_result=False)
        assert "CreateProcessW OK" in result
        assert "spawn ok" not in result
        assert "child still alive after terminate" in result
        assert "TerminateProcess=False" in result
        assert "reaped by job KILL_ON_JOB_CLOSE" in result
        closes = [c for c in calls if isinstance(c, tuple) and c[0] == "CloseHandle"]
        assert len(closes) == 3

    def _probe_with_job(self, wait_results: list[int], terminate_result=True):
        """Probe with a kernel32 fake that supports the job object
        plumbing; records the suspend flag, the job limit flags and
        every CloseHandle."""
        calls: list[tuple | str] = []

        class _JobKernel32:
            def __init__(self, name, use_last_error=False):
                pass

            def CreateProcessW(self, *a, **k):
                calls.append(("CreateProcessW", a[5]))
                return True

            def WaitForSingleObject(self, handle, timeout):
                calls.append(("WaitForSingleObject", timeout))
                return wait_results.pop(0)

            def TerminateProcess(self, handle, code):
                calls.append(("TerminateProcess", code))
                return terminate_result

            def CreateJobObjectW(self, *a, **k):
                calls.append("CreateJobObjectW")
                return 123

            def SetInformationJobObject(self, job, cls, info, size):
                # ``info`` arrives as the byref() CArgObject; the
                # underlying structure is reachable through ``._obj``.
                flags = info._obj.BasicLimitInformation.LimitFlags
                calls.append(("SetInformationJobObject", cls, flags))
                return True

            def InitializeProcThreadAttributeList(self, attr_list, count, flags, size_ptr):
                calls.append(("InitializeProcThreadAttributeList", attr_list is None))
                if attr_list is None:
                    size_ptr._obj.value = 64
                return True

            def UpdateProcThreadAttribute(self, attr_list, flags, attr, value, size, prev, retsize):
                calls.append(("UpdateProcThreadAttribute", attr))
                return True

            def DeleteProcThreadAttributeList(self, attr_list):
                calls.append("DeleteProcThreadAttributeList")

            def CloseHandle(self, handle):
                calls.append(("CloseHandle", handle))

        result = self._probe_with_fake(_JobKernel32)
        return result, calls

    def test_job_object_kills_on_job_close(self):
        """Audit round 18 P2: the probe must own the child through a job
        object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, so closing the
        job handle reaps the process even if TerminateProcess fails —
        and the child is spawned CREATE_SUSPENDED so it never executes
        code just for a diagnostic."""
        result, calls = self._probe_with_job([self.WAIT_OBJECT_0])
        assert "CreateProcessW OK" in result
        assert "spawn ok, suspended child terminated" in result
        # CREATE_SUSPENDED (0x4) | EXTENDED_STARTUPINFO_PRESENT (0x80000)
        # — the probe must not run the binary, and the extended startup
        # block carries the job-list attribute (audit round 21 P2).
        assert ("CreateProcessW", 0x00080004) in calls
        # JobObjectExtendedLimitInformation (9) with the kill flag set.
        assert ("SetInformationJobObject", 9, 0x2000) in calls
        # The child is born INSIDE the job: the job list attribute is
        # wired through the attribute list before the spawn.
        assert ("UpdateProcThreadAttribute", 0x0002000D) in calls
        assert ("InitializeProcThreadAttributeList", True) in calls
        assert "DeleteProcThreadAttributeList" in calls
        # job + process + thread — all three handles closed.
        closes = [c for c in calls if isinstance(c, tuple) and c[0] == "CloseHandle"]
        assert len(closes) == 3

    def test_job_reaps_child_alive_after_failed_terminate(self):
        """With the job in place, "child still alive after terminate"
        must state that the job reaps it on handle close — a diagnostic
        that can no longer coincide with an actual leak."""
        result, calls = self._probe_with_job([self.WAIT_TIMEOUT], terminate_result=False)
        assert "child still alive after terminate" in result
        assert "reaped by job KILL_ON_JOB_CLOSE" in result
        assert ("SetInformationJobObject", 9, 0x2000) in calls
        closes = [c for c in calls if isinstance(c, tuple) and c[0] == "CloseHandle"]
        assert len(closes) == 3

    def test_probe_attribute_list_failure_skips_spawn(self):
        """Audit round 21 P2: if the PROC_THREAD_ATTRIBUTE_JOB_LIST
        attribute list cannot be built, the probe must NOT spawn — a
        child created outside the job would have no reaper. It degrades
        to the same static skip as a missing job object, and the job
        handle is still closed."""
        calls: list[tuple | str] = []

        class _RefusingAttrListKernel32:
            def __init__(self, name, use_last_error=False):
                pass

            def CreateJobObjectW(self, *a, **k):
                calls.append("CreateJobObjectW")
                return 123

            def SetInformationJobObject(self, job, cls, info, size):
                calls.append(("SetInformationJobObject", cls))
                return True

            def InitializeProcThreadAttributeList(self, attr_list, count, flags, size_ptr):
                calls.append(("InitializeProcThreadAttributeList", attr_list is None))
                if attr_list is None:
                    size_ptr._obj.value = 64
                return False  # list refused

            def DeleteProcThreadAttributeList(self, attr_list):
                calls.append("DeleteProcThreadAttributeList")

            def CloseHandle(self, handle):
                calls.append(("CloseHandle", handle))

        result = self._probe_with_fake(_RefusingAttrListKernel32)
        assert "CreateProcessW probe skipped (job object unavailable)" in result
        assert "CreateProcessW" not in calls
        assert "UpdateProcThreadAttribute" not in calls
        assert "TerminateProcess" not in calls
        closes = [c for c in calls if isinstance(c, tuple) and c[0] == "CloseHandle"]
        assert len(closes) == 1  # the job handle

    def test_probe_attribute_list_update_failure_skips_spawn(self):
        """Same skip guarantee when the attribute list initializes but
        the JOB_LIST attribute itself is rejected."""
        calls: list[tuple | str] = []

        class _RefusingUpdateKernel32:
            def __init__(self, name, use_last_error=False):
                pass

            def CreateJobObjectW(self, *a, **k):
                calls.append("CreateJobObjectW")
                return 123

            def SetInformationJobObject(self, job, cls, info, size):
                calls.append(("SetInformationJobObject", cls))
                return True

            def InitializeProcThreadAttributeList(self, attr_list, count, flags, size_ptr):
                calls.append(("InitializeProcThreadAttributeList", attr_list is None))
                if attr_list is None:
                    size_ptr._obj.value = 64
                return True

            def UpdateProcThreadAttribute(self, attr_list, flags, attr, value, size, prev, retsize):
                calls.append(("UpdateProcThreadAttribute", attr))
                return False  # job list rejected

            def DeleteProcThreadAttributeList(self, attr_list):
                calls.append("DeleteProcThreadAttributeList")

            def CloseHandle(self, handle):
                calls.append(("CloseHandle", handle))

        result = self._probe_with_fake(_RefusingUpdateKernel32)
        assert "CreateProcessW probe skipped (job object unavailable)" in result
        assert "CreateProcessW" not in calls
        closes = [c for c in calls if isinstance(c, tuple) and c[0] == "CloseHandle"]
        assert len(closes) == 1  # the job handle

    def test_probe_signatures_declared(self):
        """Audit round 21 P1: without restype declarations ctypes
        defaults to c_int, so CreateJobObjectW's HANDLE (pointer-sized
        on x64) would be TRUNCATED to 32 bits — SetInformationJobObject
        and CloseHandle would then receive a garbage handle, the
        kill-on-close guarantee would silently disappear, and the handle
        would never be freed. The probe must pin the signature of every
        WinAPI it calls."""
        import ctypes as _ctypes_mod
        import ctypes.wintypes as wintypes

        from stream2video.tools import _createprocess_probe

        instances: list[object] = []

        class _Pinnable:
            """Method stand-in that can hold ctypes signature attributes
            — real WinDLL functions can, plain bound methods cannot."""

            def __init__(self, fn):
                self._fn = fn

            def __call__(self, *a, **k):
                return self._fn(*a, **k)

        def _mk(fn):
            return _Pinnable(fn)

        def _size_query(attr_list, count, flags, size_ptr):
            if attr_list is None:
                size_ptr._obj.value = 64
            return True

        class _SignaturesKernel32:
            def __init__(self, name, use_last_error=False):
                instances.append(self)

            CreateJobObjectW = _mk(lambda *a, **k: 123)
            SetInformationJobObject = _mk(lambda *a, **k: True)
            InitializeProcThreadAttributeList = _mk(_size_query)
            UpdateProcThreadAttribute = _mk(lambda *a, **k: True)
            DeleteProcThreadAttributeList = _mk(lambda *a, **k: None)
            CreateProcessW = _mk(lambda *a, **k: True)
            WaitForSingleObject = _mk(lambda *a, **k: 0x00000000)
            TerminateProcess = _mk(lambda *a, **k: True)
            CloseHandle = _mk(lambda *a, **k: None)

        with patch.object(_ctypes_mod, "WinDLL", _SignaturesKernel32):
            _createprocess_probe("ffmpeg.exe")
        k32 = instances[-1]
        assert k32.CreateJobObjectW.restype is wintypes.HANDLE
        assert k32.CreateJobObjectW.argtypes is not None
        assert k32.SetInformationJobObject.restype is wintypes.BOOL
        assert k32.SetInformationJobObject.argtypes is not None
        assert k32.CreateProcessW.restype is wintypes.BOOL
        assert k32.CreateProcessW.argtypes is not None
        assert k32.TerminateProcess.restype is wintypes.BOOL
        assert k32.TerminateProcess.argtypes is not None
        assert k32.WaitForSingleObject.restype is wintypes.DWORD
        assert k32.WaitForSingleObject.argtypes is not None
        assert k32.CloseHandle.restype is wintypes.BOOL
        assert k32.CloseHandle.argtypes is not None
        assert k32.InitializeProcThreadAttributeList.restype is wintypes.BOOL
        assert k32.InitializeProcThreadAttributeList.argtypes is not None
        assert k32.UpdateProcThreadAttribute.restype is wintypes.BOOL
        assert k32.UpdateProcThreadAttribute.argtypes is not None
        assert k32.DeleteProcThreadAttributeList.argtypes is not None

    def test_large_job_handle_survives_at_full_width(self):
        """Audit round 21 P1: on 64-bit Python a HANDLE above 0xFFFFFFFF
        must reach SetInformationJobObject / CloseHandle UNCUT — the
        c_int truncation would turn it into a small garbage value.
        (Signature pinning is covered by test_probe_signatures_declared;
        this pins the end-to-end flow.)"""
        calls: list[tuple | str] = []
        BIG_HANDLE = 0x1_0000_0123

        class _BigHandleKernel32:
            def __init__(self, name, use_last_error=False):
                pass

            def CreateJobObjectW(self, *a, **k):
                calls.append("CreateJobObjectW")
                return BIG_HANDLE

            def SetInformationJobObject(self, job, cls, info, size):
                calls.append(("SetInformationJobObject", job))
                return True

            def InitializeProcThreadAttributeList(self, attr_list, count, flags, size_ptr):
                if attr_list is None:
                    size_ptr._obj.value = 64
                return True

            def UpdateProcThreadAttribute(self, attr_list, flags, attr, value, size, prev, retsize):
                calls.append(("UpdateProcThreadAttribute", attr))
                return True

            def DeleteProcThreadAttributeList(self, attr_list):
                calls.append("DeleteProcThreadAttributeList")

            def CreateProcessW(self, *a, **k):
                calls.append("CreateProcessW")
                return True

            def WaitForSingleObject(self, handle, timeout):
                return 0x00000000  # WAIT_OBJECT_0

            def TerminateProcess(self, handle, code):
                calls.append(("TerminateProcess", code))
                return True

            def CloseHandle(self, handle):
                calls.append(("CloseHandle", handle))

        result = self._probe_with_fake(_BigHandleKernel32)
        assert "CreateProcessW OK" in result
        assert ("SetInformationJobObject", BIG_HANDLE) in calls
        assert ("CloseHandle", BIG_HANDLE) in calls

    def test_probe_skipped_when_job_unavailable(self):
        """Audit round 20 P4: without a configured job object the probe
        must NOT create an active (suspended) process at all — a child
        that survives a failed TerminateProcess would be a live kernel
        process with no reaper, stacked once per retry attempt. The
        probe degrades to a static diagnostic that cannot spawn."""
        calls: list[tuple | str] = []

        class _NoJobKernel32:
            def __init__(self, name, use_last_error=False):
                pass

            def CreateJobObjectW(self, *a, **k):
                calls.append("CreateJobObjectW")
                return None  # no job support

            def CloseHandle(self, handle):
                calls.append(("CloseHandle", handle))

        result = self._probe_with_fake(_NoJobKernel32)
        assert "CreateProcessW probe skipped (job object unavailable)" in result
        assert "CreateProcessW" not in calls
        assert "TerminateProcess" not in calls
        # No handles were ever opened.
        closes = [c for c in calls if isinstance(c, tuple) and c[0] == "CloseHandle"]
        assert closes == []

    def test_probe_skipped_when_job_setup_rejected(self):
        """Same guarantee when the job HANDLE exists but the kill-on-close
        limit cannot be installed: no spawn, and the created job handle
        is still closed."""
        calls: list[tuple | str] = []

        class _RejectingJobKernel32:
            def __init__(self, name, use_last_error=False):
                pass

            def CreateJobObjectW(self, *a, **k):
                calls.append("CreateJobObjectW")
                return 123

            def SetInformationJobObject(self, job, cls, info, size):
                calls.append(("SetInformationJobObject", cls))
                return False  # limit refused

            def CloseHandle(self, handle):
                calls.append(("CloseHandle", handle))

        result = self._probe_with_fake(_RejectingJobKernel32)
        assert "CreateProcessW probe skipped (job object unavailable)" in result
        assert "CreateProcessW" not in calls
        closes = [c for c in calls if isinstance(c, tuple) and c[0] == "CloseHandle"]
        assert len(closes) == 1  # the job handle

    def test_close_failure_on_first_handle_still_closes_others(self):
        """Audit round 18 P3: a raising CloseHandle on the first handle
        must not skip the remaining closes — each handle is closed
        independently."""
        calls: list[str] = []

        class _ExplodingCloseKernel32:
            def __init__(self, name, use_last_error=False):
                pass

            def CreateJobObjectW(self, *a, **k):
                calls.append("CreateJobObjectW")
                return 123

            def SetInformationJobObject(self, job, cls, info, size):
                calls.append("SetInformationJobObject")
                return True

            def CreateProcessW(self, *a, **k):
                calls.append("CreateProcessW")
                return True

            def InitializeProcThreadAttributeList(self, attr_list, count, flags, size_ptr):
                calls.append(("InitializeProcThreadAttributeList", attr_list is None))
                if attr_list is None:
                    size_ptr._obj.value = 64
                return True

            def UpdateProcThreadAttribute(self, attr_list, flags, attr, value, size, prev, retsize):
                calls.append(("UpdateProcThreadAttribute", attr))
                return True

            def DeleteProcThreadAttributeList(self, attr_list):
                calls.append("DeleteProcThreadAttributeList")

            def WaitForSingleObject(self, handle, timeout):
                return 0x00000000  # WAIT_OBJECT_0

            def TerminateProcess(self, handle, code):
                calls.append(("TerminateProcess", code))
                return True

            def CloseHandle(self, handle):
                calls.append("CloseHandle")
                if calls.count("CloseHandle") == 1:
                    raise OSError("close exploded")

        result = self._probe_with_fake(_ExplodingCloseKernel32)
        assert "spawn ok, suspended child terminated" in result
        # All three CloseHandle attempts happened — the first raised,
        # the remaining two still ran.
        assert calls.count("CloseHandle") == 3

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

            def CreateJobObjectW(self, *a, **k):
                calls.append("CreateJobObjectW")
                return 123

            def SetInformationJobObject(self, job, cls, info, size):
                calls.append("SetInformationJobObject")
                return True

            def CreateProcessW(self, *a, **k):
                calls.append("CreateProcessW")
                return True

            def InitializeProcThreadAttributeList(self, attr_list, count, flags, size_ptr):
                calls.append(("InitializeProcThreadAttributeList", attr_list is None))
                if attr_list is None:
                    size_ptr._obj.value = 64
                return True

            def UpdateProcThreadAttribute(self, attr_list, flags, attr, value, size, prev, retsize):
                calls.append(("UpdateProcThreadAttribute", attr))
                return True

            def DeleteProcThreadAttributeList(self, attr_list):
                calls.append("DeleteProcThreadAttributeList")

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
        assert ("WaitForSingleObject", 10000) in calls
        # Best-effort kill attempted even though we never confirmed exit.
        assert ("TerminateProcess", 1) in calls
        assert calls.count("CloseHandle") == 3

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

            def CreateJobObjectW(self, *a, **k):
                calls.append("CreateJobObjectW")
                return 123

            def SetInformationJobObject(self, job, cls, info, size):
                calls.append("SetInformationJobObject")
                return True

            def CreateProcessW(self, *a, **k):
                calls.append("CreateProcessW")
                return True

            def InitializeProcThreadAttributeList(self, attr_list, count, flags, size_ptr):
                calls.append(("InitializeProcThreadAttributeList", attr_list is None))
                if attr_list is None:
                    size_ptr._obj.value = 64
                return True

            def UpdateProcThreadAttribute(self, attr_list, flags, attr, value, size, prev, retsize):
                calls.append(("UpdateProcThreadAttribute", attr))
                return True

            def DeleteProcThreadAttributeList(self, attr_list):
                calls.append("DeleteProcThreadAttributeList")

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
        assert "spawn ok" not in result
        assert "TerminateProcess=False" in result
        assert "reaped by job KILL_ON_JOB_CLOSE" in result
        assert calls.count("CloseHandle") == 3


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

    @pytest.mark.parametrize(
        "wrapper_cmd0",
        [
            "C:/tools/ffmpeg-wrapper.exe",
            "/opt/custom-ffmpeg-build",
            "my_ffmpeg_helper",
            "ffmpeg-custom.exe",
        ],
    )
    def test_custom_ffmpeg_wrapper_not_re_resolved(self, wrapper_cmd0):
        """Audit round 20 P3: re-resolution matches the PLAIN basename,
        so a custom wrapper / patched build whose name merely CONTAINS
        ffmpeg must retry with its own cmd0 — the retry contract is
        "repeat the same operation", not "swap in the system binary".
        (The old substring check silently replaced these with
        ffmpeg_path() while keeping the wrapper's arguments.)"""
        calls: list = []
        result, probes, _, _ = self._spawn(
            "run",
            [wrapper_cmd0, "-i", "in.mp4"],
            {},
            self._failing_until(calls, 1, "ok"),
            os_name="nt",
        )
        assert result == "ok"
        assert len(calls) == 2
        assert calls[0][0][0] == wrapper_cmd0
        assert calls[1][0][0] == wrapper_cmd0
        # A real ffmpeg probe DID run for the failure.
        assert probes == [wrapper_cmd0]

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

    def test_real_platform_fnf_retries_without_winerror(self):
        """Audit round 18 P1: on POSIX ``FileNotFoundError`` has no
        ``winerror`` attribute — the diagnostic log used to read it
        directly, so the FIRST failure raised AttributeError instead of
        retrying (and instead of re-raising the original FileNotFoundError).
        This test runs UNPATCHED for ``os.name`` with a genuine
        FileNotFoundError, so on Linux CI it exercises the real posix
        path and on Windows the real nt path; either way the retry must
        complete."""
        calls: list = []
        result, probes, reset_cache, reset_enc = self._spawn(
            "run",
            ["ffmpeg", "-version"],
            {},
            self._failing_until(calls, 2, "ok"),
        )
        assert result == "ok"
        assert len(calls) == 3
        assert len(probes) == 2
        assert reset_cache.call_count == 2
        assert reset_enc.call_count == 2

    def test_winerror_206_retried_with_creationflags_dropped(self):
        """Audit round 19 P1: CreateProcessW error 206
        (ERROR_FILENAME_EXCED_RANGE) surfaces as a BARE OSError, not
        FileNotFoundError — the exact incident the retry layer + the
        CREATE_NO_WINDOW-dropping workaround were built for (CPython
        bug #37380). It must be retried, with only the CREATE_NO_WINDOW
        bit dropped on the follow-up attempt."""

        class _Win206Error(OSError):
            def __init__(self):
                super().__init__(206, "filename too long", "C:/x/ffmpeg.exe")
                self.winerror = 206

        calls: list = []

        def side_effect(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if len(calls) == 1:
                raise _Win206Error()
            return "ok"

        result, probes, reset_cache, _ = self._spawn(
            "run",
            ["ffmpeg", "-version"],
            {"creationflags": 0x08000000 | 0x40000000},
            side_effect,
            os_name="nt",
        )
        assert result == "ok"
        assert len(calls) == 2
        # The 206 retry drops ONLY CREATE_NO_WINDOW — the priority class
        # bit survives.
        assert calls[1][1]["creationflags"] == 0x40000000
        assert calls[1][0][0] == "C:/resolved/ffmpeg.exe"
        assert len(probes) == 1
        assert reset_cache.call_count == 1

    def test_non_transient_oserror_raised_immediately(self):
        """Audit round 19 P1: any OSError that is NOT one of the
        transient codes (ENOENT / winerror 2/3/206) must surface
        immediately — no retry, no probe, no cache resets — so a real
        error (access denied, invalid argument) is never masked behind
        the last-attempt message."""
        calls: list = []

        def side_effect(cmd, **kwargs):
            calls.append((cmd, kwargs))
            raise PermissionError(13, "access denied", cmd[0])

        record: dict = {}
        with pytest.raises(PermissionError):
            self._spawn(
                "run",
                ["ffmpeg", "-version"],
                {},
                side_effect,
                os_name="nt",
                record=record,
            )
        assert len(calls) == 1
        assert record["probe_calls"] == []
        assert record["reset_cache"].call_count == 0
        assert record["reset_enc"].call_count == 0
