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
    set_active_process,
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


class TestActiveProcess:
    """set/get_active_process — registry used by the GUI's WM_DELETE
    handler to kill the in-flight ffmpeg on close."""

    def test_default_is_none(self):
        set_active_process(None)
        assert get_active_process() is None

    def teardown_method(self, method):
        # Don't leak state into other tests.
        set_active_process(None)
