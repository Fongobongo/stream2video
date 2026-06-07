"""Shared utility functions."""

import logging
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)

CANCEL_POLL_INTERVAL = 0.5


@contextmanager
def cancel_monitor(
    process: subprocess.Popen,
    cancel_callback: Callable[[], bool] | None = None,
) -> Iterator[threading.Event]:
    """Start a daemon thread that kills ``process`` when ``cancel_callback`` returns True.

    Yields a ``threading.Event`` that is set the moment cancellation occurs.
    Callers can check it with ``cancelled.is_set()`` or just call
    ``cancel_callback()`` directly. The event is also set automatically on
    context exit so the monitor thread terminates cleanly.

    A thread is always started; when ``cancel_callback`` is None the
    monitor body returns immediately and the thread exits as soon as the
    context manager fires ``cancelled.set()`` on exit. The yielded event
    simply stays unset forever in that case.
    """
    cancelled = threading.Event()

    def _monitor():
        if cancel_callback is None:
            return
        while not cancelled.wait(CANCEL_POLL_INTERVAL):
            if process.poll() is not None:
                return
            if cancel_callback():
                process.kill()
                cancelled.set()
                return

    thread = threading.Thread(target=_monitor, daemon=True)
    thread.start()
    try:
        yield cancelled
    finally:
        cancelled.set()


def get_video_duration(video_path: Path) -> float | None:
    """Get video duration in seconds via ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            **no_window_kwargs(),
        )
        return float(result.stdout.strip())
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
        FileNotFoundError,
    ) as e:
        logger.warning(f"Could not determine video duration: {e}")
        return None


def drain_stderr_lines(
    pipe: IO[bytes],
    sink: list[str],
    on_line: Callable[[str], None] | None = None,
) -> Callable[[], None]:
    """Spawn a daemon thread that reads bytes from `pipe` and appends decoded lines to `sink`.

    The thread terminates when the pipe is closed (typically when the subprocess
    exits and the OS reaps its fds); it cannot be stopped from outside the
    thread — the only way to end it is to close the pipe.

    If `on_line` is given, it is invoked with each decoded line *after* the
    line is appended to `sink`. The callback runs on the drain thread, so
    callers that need to touch a UI must wrap the work in their framework's
    main-thread dispatch (e.g. `self.after(0, ...)` in Tkinter). Exceptions
    raised by the callback are logged and swallowed — they do not stop the
    drain thread.

    Returns a `wait_for_drain` callable that blocks (up to `timeout` seconds) for
    the thread to finish. Call it in a `finally` block to ensure `sink` is fully
    populated before reading it.

    Typical usage:
        stop_drain = drain_stderr_lines(process.stderr, stderr_lines)
        try:
            ...
        finally:
            stop_drain()
            process.stderr.close()  # closing the pipe is what stops the thread
    """
    stop_event = threading.Event()

    def _run():
        try:
            for raw in iter(pipe.readline, b""):
                line = raw.decode("utf-8", errors="replace")
                sink.append(line)
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception:
                        logger.exception("drain_stderr_lines on_line callback raised")
        except (OSError, ValueError):
            pass
        finally:
            stop_event.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    def _wait_for_drain(timeout: float = 5.0) -> None:
        stop_event.wait(timeout=timeout)

    return _wait_for_drain


_active_proc: subprocess.Popen | None = None
_active_proc_lock = threading.Lock()


def get_active_process() -> subprocess.Popen | None:
    """Return the currently running ffmpeg Popen, or None if no pipeline is active.

    Used by callers (e.g. GUI) that need to terminate a running ffmpeg.
    """
    with _active_proc_lock:
        return _active_proc


def set_active_process(proc: subprocess.Popen | None) -> None:
    """Register or clear the active ffmpeg Popen. Thread-safe."""
    with _active_proc_lock:
        global _active_proc
        _active_proc = proc


def no_window_kwargs() -> dict:
    """Return subprocess kwargs that suppress console windows on Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
