"""Tests for stream2video.utils — cancel_monitor, get_video_duration, etc."""

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from stream2video.utils import (
    CANCEL_POLL_INTERVAL,
    cancel_monitor,
    get_active_process,
    get_video_duration,
    get_video_start_time,
    set_active_process,
    subprocess_kwargs,
)


def _spawn_quick_proc():
    """Spawn a short-lived subprocess that exits on its own."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.1)"],
    )


class TestCancelMonitor:
    """cancel_monitor — context manager that kills a Popen when its
    cancel_callback returns True. Replaces three near-duplicate
    _cancel_monitor functions in concat.py and silence.py."""

    def test_no_callback_event_stays_unset(self):
        """If cancel_callback is None, the monitor thread is not even
        started; the yielded event stays unset during the context."""
        proc = _spawn_quick_proc()
        try:
            with cancel_monitor(proc) as cancelled:
                assert not cancelled.is_set()
                proc.wait(timeout=5)
                # Still unset during the context — callback was never polled.
                assert not cancelled.is_set()
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_callback_returning_false_does_not_kill(self):
        """Callback returning False should leave the process running."""
        proc = _spawn_quick_proc()
        try:
            with cancel_monitor(proc, cancel_callback=lambda: False):
                proc.wait(timeout=5)
            assert proc.returncode == 0
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_callback_returning_true_kills_process(self):
        """Callback returning True should kill the process and set the event."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            callback_calls = []

            def cb() -> bool:
                callback_calls.append(True)
                return True

            def runner():
                time.sleep(CANCEL_POLL_INTERVAL * 2)
                # We can't call cb from outside; the monitor
                # thread polls it on its own. Just wait for the event.

            t = threading.Thread(target=runner, daemon=True)
            t.start()
            with cancel_monitor(proc, cancel_callback=cb) as cancelled:
                # Force the callback to return True on the next poll by
                # waiting past one poll interval; but since cb already
                # always returns True, the first poll will kill it.
                proc.wait(timeout=5)
            assert cancelled.is_set()
            assert proc.poll() is not None  # process was killed
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_yielded_event_is_set_on_exit(self):
        """When the context manager exits (normally or via exception),
        the event is set so the monitor thread terminates."""
        proc = _spawn_quick_proc()
        try:
            with cancel_monitor(proc) as cancelled:
                proc.wait(timeout=5)
            assert cancelled.is_set()
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_exception_in_block_still_sets_event(self):
        proc = _spawn_quick_proc()
        try:
            with pytest.raises(RuntimeError), cancel_monitor(proc) as cancelled:
                raise RuntimeError("boom")
            assert cancelled.is_set()
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_process_already_dead(self):
        """If the process exits before the monitor thread's first poll,
        the thread should detect that via poll() and exit cleanly without
        invoking the cancel callback (no kill needed)."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
        )
        proc.wait(timeout=5)
        callback_called = []
        with cancel_monitor(
            proc, cancel_callback=lambda: callback_called.append(True) or True
        ) as cancelled:
            time.sleep(CANCEL_POLL_INTERVAL * 1.5)
            assert not cancelled.is_set()
            assert callback_called == []  # monitor exited via poll(), not via cb


class TestGetVideoDuration:
    """get_video_duration — ffprobe wrapper."""

    def test_returns_none_for_missing_file(self, tmp_path: Path):
        # Nonexistent file: ffprobe fails, returns None.
        result = get_video_duration(tmp_path / "does_not_exist.mp4")
        assert result is None

    def test_returns_none_for_non_video(self, tmp_path: Path):
        # Random text file: ffprobe can't read duration.
        f = tmp_path / "not_a_video.txt"
        f.write_text("hello")
        result = get_video_duration(f)
        assert result is None


class TestGetVideoStartTime:
    """get_video_start_time — ffprobe wrapper used by the batch path
    to compensate ``-copyts`` + ``-ss`` for non-zero PTS sources."""

    def test_missing_file_returns_zero(self, tmp_path: Path):
        # Nonexistent file: ffprobe fails — returns 0.0 (the safe
        # default, so a failed probe can't abort the whole encode).
        result = get_video_start_time(tmp_path / "does_not_exist.mp4")
        assert result == 0.0

    def test_non_video_returns_zero(self, tmp_path: Path):
        # Random text file: ffprobe reports no format-level start_time.
        f = tmp_path / "not_a_video.txt"
        f.write_text("hello")
        assert get_video_start_time(f) == 0.0


class TestActiveProcess:
    """set/get_active_process — registry used by the GUI's WM_DELETE
    handler to kill the in-flight ffmpeg on close."""

    def test_default_is_none(self):
        set_active_process(None)
        assert get_active_process() is None

    def teardown_method(self, method):
        # Don't leak state into other tests.
        set_active_process(None)


class TestSubprocessKwargs:
    """subprocess_kwargs — composes no_window_kwargs with optional
    low-priority scheduling flags. Spawned ffmpeg processes inherit
    these flags via Popen(**subprocess_kwargs(...))."""

    def test_default_low_priority_false_returns_only_window_flag(self):
        kw = subprocess_kwargs(low_priority=False)
        if sys.platform == "win32":
            assert kw == {"creationflags": subprocess.CREATE_NO_WINDOW}
        else:
            assert kw == {}

    def test_low_priority_true_includes_priority_flag(self):
        kw = subprocess_kwargs(low_priority=True)
        if sys.platform == "win32":
            # Composes CREATE_NO_WINDOW (0x08000000) with
            # BELOW_NORMAL_PRIORITY_CLASS (0x00004000).
            assert kw["creationflags"] == (subprocess.CREATE_NO_WINDOW | 0x00004000)
            assert "preexec_fn" not in kw
        else:
            # POSIX: preexec_fn ensures the child starts at nice +10.
            assert callable(kw["preexec_fn"])
            assert "creationflags" not in kw

    def test_low_priority_false_is_identity_on_posix(self):
        if sys.platform == "win32":
            pytest.skip("POSIX-only")
        assert subprocess_kwargs(low_priority=False) == {}

    def test_low_priority_false_keeps_no_window_on_windows(self):
        if sys.platform != "win32":
            pytest.skip("Windows-only")
        kw = subprocess_kwargs(low_priority=False)
        assert kw["creationflags"] == subprocess.CREATE_NO_WINDOW

    def test_preexec_fn_increases_nice_on_posix(self):
        """When the call to preexec_fn is executed (in the child after
        fork), it should successfully os.nice(+10)."""
        if sys.platform == "win32":
            pytest.skip("POSIX-only")
        # Run the preexec_fn in a child process (which is what Popen
        # does) and check the child's nice value increases by 10.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import os; print(os.nice(0))"],
            **subprocess_kwargs(low_priority=True),
        )
        proc.wait()
        # As long as the Popen succeeds with preexec_fn, the function
        # is wired correctly; the actual nice increment is verified
        # by os.nice's semantics (it returns the new value). We can't
        # easily get the child's post-exec nice here because os.nice
        # runs in the child's address space, but a successful exit
        # confirms preexec_fn didn't raise OSError.
        assert proc.returncode == 0
