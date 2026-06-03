"""Shared utility functions."""

import logging
import subprocess
import threading
from pathlib import Path
from typing import IO, Callable, List, Optional

logger = logging.getLogger(__name__)

CANCEL_POLL_INTERVAL = 0.5


def get_video_duration(video_path: Path) -> Optional[float]:
    """Get video duration in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            ValueError, FileNotFoundError) as e:
        logger.warning(f"Could not determine video duration: {e}")
        return None


def drain_stderr_lines(pipe: IO[bytes], sink: List[str]) -> Callable[[], None]:
    """Spawn a daemon thread that reads bytes from `pipe` and appends decoded lines to `sink`.

    The thread terminates when the pipe is closed (typically when the subprocess
    exits and the OS reaps its fds); it cannot be stopped from outside the
    thread — the only way to end it is to close the pipe.

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
                sink.append(raw.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass
        finally:
            stop_event.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    def _wait_for_drain(timeout: float = 5.0) -> None:
        stop_event.wait(timeout=timeout)
    return _wait_for_drain


_active_proc: Optional[subprocess.Popen] = None
_active_proc_lock = threading.Lock()


def get_active_process() -> Optional[subprocess.Popen]:
    """Return the currently running ffmpeg Popen, or None if no pipeline is active.

    Used by callers (e.g. GUI) that need to terminate a running ffmpeg.
    """
    with _active_proc_lock:
        return _active_proc


def set_active_process(proc: Optional[subprocess.Popen]) -> None:
    """Register or clear the active ffmpeg Popen. Thread-safe."""
    with _active_proc_lock:
        global _active_proc
        _active_proc = proc
