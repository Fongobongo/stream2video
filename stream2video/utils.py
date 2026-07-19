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

    When ``cancel_callback`` is None no monitoring thread is started — the
    yielded event simply stays unset forever. Callers that still want to
    poll a cancel flag should do so directly via ``cancel_callback()`` (a
    None check is required in that case).
    """
    cancelled = threading.Event()

    if cancel_callback is not None:
        def _monitor():
            try:
                while not cancelled.wait(CANCEL_POLL_INTERVAL):
                    if process.poll() is not None:
                        return
                    if cancel_callback():
                        process.kill()
                        cancelled.set()
                        return
            except Exception:
                # A misused callback that raises (instead of returning True)
                # would previously die WITHOUT setting `cancelled` or killing
                # the process, silently missing the cancel request. Set the
                # event and log so the caller's wait loop notices the cancel
                # flag and the user can see why the cancel never fired.
                logger.exception("cancel_monitor: cancel_callback raised; forcing cancel")
                cancelled.set()
                try:
                    process.kill()
                except Exception:
                    pass

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


def has_audio_stream(video_path: Path) -> bool:
    """Return True if ``video_path`` has at least one audio stream.

    Used by the concat pipeline to decide whether to pass ``-c:a`` /
    ``-map 0:a:0`` (an audio-less source would make ffmpeg fail with
    "Output file does not contain any stream" when audio mapping is
    requested). Probed once at the start of ``cut_and_concat`` so the
    per-segment encode can skip audio options entirely for audio-less
    sources. See P1.14 in the fix plan.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_type",
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
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ) as e:
        # If we can't probe, assume audio exists so the historical
        # command shape is preserved — the encoder will fail with a
        # clear error if the source really has no audio, which is
        # better than silently producing a video-only output the user
        # didn't ask for.
        logger.warning(f"Could not probe audio streams in {video_path}: {e}")
        return True
    return bool(result.stdout.strip())


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

# Scoped process registry (P1.11). The single-slot ``_active_proc`` above
# was overwritten by every subprocess — a parallel preview waveform and a
# pipeline encode would race for the slot, and one's ``finally`` would
# clear the other's registration, so cancel/close couldn't reach the
# right process. The dict below keys processes by an opaque owner string
# (caller-chosen, e.g. "pipeline", "preview", "download") so multiple
# subprocesses can coexist and cancellation can target the right one.
#
# ``set_active_process`` / ``get_active_process`` are retained as thin
# wrappers around the registry so existing call sites (concat.py,
# silence.py, download.py) keep working — they implicitly use the
# "default" owner. New call sites should prefer the scoped API.
_proc_registry: dict[str, subprocess.Popen] = {}
_proc_registry_lock = threading.Lock()


def get_active_process(owner: str = "default") -> subprocess.Popen | None:
    """Return the currently registered subprocess for ``owner`` (default slot).

    Back-compat: callers that don't pass ``owner`` get the historical
    single-slot behaviour (the "default" key). The registry also stores
    the most-recently-registered process under "default" so legacy code
    that didn't specify an owner still sees a process to cancel.
    """
    with _proc_registry_lock:
        return _proc_registry.get(owner) or _proc_registry.get("default")


def set_active_process(proc: subprocess.Popen | None, owner: str = "default") -> None:
    """Register or clear the active subprocess for ``owner``. Thread-safe.

    Passing ``proc=None`` removes the registration. Multiple owners can
    coexist (e.g. "pipeline" + "preview") so parallel subprocesses don't
    clobber each other's registration — see P1.11 in the fix plan.
    """
    with _proc_registry_lock:
        global _active_proc
        if owner == "default":
            # Mirror to the legacy single-slot so existing readers see
            # the latest "default" registration as before.
            _active_proc = proc
        if proc is None:
            _proc_registry.pop(owner, None)
        else:
            _proc_registry[owner] = proc


def cancel_process(owner: str, timeout: float = 2.0) -> bool:
    """Kill the subprocess registered under ``owner`` if any. Returns True if killed.

    Uses ``process.kill()`` (SIGKILL on Unix, TerminateProcess on Windows)
    because we don't know which subprocess type it is and graceful
    shutdown would race with the pipeline's own cancel_callback. The
    caller's wait loop already handles the cleanup once the process exits.
    """
    with _proc_registry_lock:
        proc = _proc_registry.get(owner)
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.kill()
    except Exception:
        logger.exception(f"cancel_process({owner!r}): kill() failed")
        return False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    return True


def list_active_owners() -> list[str]:
    """Return the owner strings currently holding a live subprocess.

    Useful for diagnostics and for the GUI's shutdown handler to make
    sure every spawned ffmpeg has been cleaned up before the interpreter
    exits.
    """
    with _proc_registry_lock:
        return [owner for owner, proc in _proc_registry.items() if proc.poll() is None]


def no_window_kwargs() -> dict:
    """Return subprocess kwargs that suppress console windows on Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
