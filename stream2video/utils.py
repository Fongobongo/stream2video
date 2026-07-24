"""Shared utility functions."""

import logging
import queue
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

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

        def _monitor() -> None:
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


def get_video_start_time(video_path: Path) -> float:
    """Container-level ``start_time`` in seconds (0 for clean sources).

    Sources captured with tools that add ``-itsoffset`` (OBS streams,
    mid-file re-muxes) have a non-zero start time — the first frame's
    PTS is shifted by a few seconds even though the per-frame duration
    is unchanged. ``ffprobe`` reports this via ``format=start_time``.

    The batch path uses this offset to compensate ``trim`` filter
    boundaries and ``-ss`` seek position so the pipeline still cuts at
    the user-visible "source time" 0..N points. A value of 0 means the
    source starts at t=0 (normal case) and the compensation is a no-op.

    Returns 0.0 when ffprobe cannot determine the start time rather
    than raising — a failed probe here shouldn't abort the whole
    encode because the segment path doesn't depend on this value and
    most sources have start_time=0 anyway.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=start_time",
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
        out = result.stdout.strip()
        return float(out) if out else 0.0
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
        FileNotFoundError,
    ) as e:
        logger.warning(f"Could not determine video start_time for {video_path}: {e}")
        return 0.0


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

    def _run() -> None:
        try:
            for raw in iter(pipe.readline, b""):
                # In text mode (Popen with text=True) ``raw`` is already
                # a str; in bytes mode it's bytes. Decode bytes only so
                # text-mode callers don't trigger AttributeError.
                if isinstance(raw, bytes):
                    line = raw.decode("utf-8", errors="replace")
                else:
                    line = raw
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


def read_lines_queue(pipe: IO[bytes]) -> tuple[queue.Queue[bytes | None], threading.Thread]:
    """Spawn a daemon thread that reads lines from ``pipe`` into a queue.

    Unlike ``drain_stderr_lines`` (which appends to a shared list and
    can't be interrupted from the consumer side), this returns a
    ``queue.Queue`` the consumer can poll with a timeout. The consumer
    loop can check cancel events / stall timeouts between reads without
    blocking on ``readline()`` — which is the P1.5 stall-detection
    gap: a hung ffmpeg that stops emitting stdout blocks ``readline()``
    forever, preventing the inline stall check from running.

    The producer thread reads with ``readline()`` (blocking per-line,
    but on its own thread). When the pipe closes (subprocess exits),
    the producer puts ``None`` as a sentinel and terminates.

    Returns ``(q, thread)``. The consumer reads from ``q`` with
    ``q.get(timeout=...)``; ``None`` means EOF.

    Typical usage::

        q, reader = read_lines_queue(process.stdout)
        while True:
            try:
                raw = q.get(timeout=CANCEL_POLL_INTERVAL)
            except queue.Empty:
                # Check cancel / stall here — no blocking readline.
                if cancel_callback():
                    process.kill()
                    raise CancelledError()
                continue
            if raw is None:
                break  # EOF
            line = raw.decode("utf-8", errors="replace").strip()
            ...

    The thread is daemon, so it won't block process exit even if the
    pipe is never closed (the process was killed).
    """
    q: queue.Queue[bytes | None] = queue.Queue()

    def _reader() -> None:
        try:
            for raw in iter(pipe.readline, b""):
                q.put(raw)
        except (OSError, ValueError):
            pass
        finally:
            q.put(None)

    thread = threading.Thread(target=_reader, daemon=True, name="stdout_reader")
    thread.start()
    return q, thread


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


# ---------------------------------------------------------------------------
# Shared subprocess runner (P2.4)
# ---------------------------------------------------------------------------
# Popen + stderr drain + cancel_monitor + pipe cleanup was duplicated across
# concat._run_ffmpeg, silence._run_silencedetect, silence._extract_audio_wav,
# silence.detect_silence_stream, download.download, and waveform.read_peaks_from_stream.
# Each had its own slightly-different version of the same try/finally +
# set_active_process + drain_stderr_lines + close pattern. The duplication
# was the root cause of P1.13 (decimal comma) — the silence parser was
# inlined into each call site and drifted.
#
# ``SubprocessRunner`` is a context manager that owns the Popen, drains
# stderr into a list (with an optional on_line callback for progressive
# parsing), registers the process with the scoped supervisor, and guarantees
# pipe cleanup in __exit__. The caller still owns the high-level flow
# (timeout, stall watchdog, progress parsing) because those vary per call
# site; the runner just eliminates the boilerplate that was identical
# everywhere.
#
# Not yet wired into all call sites — the existing ones still use their
# inline patterns. New code should use this runner so the next refactor
# doesn't have to repeat the dedup.


class SubprocessRunner:
    """Context manager that runs a subprocess with stderr drain + cleanup.

    Spawns the process on entry; on exit, drains stderr, joins the drain
    thread, closes both pipes, and clears the active-process registration
    (so cancel/close can't reach a dead handle). The process itself is
    NOT waited for here — callers are responsible for ``proc.wait()``
    with their own timeout / cancel logic, because those vary per call
    site (ffmpeg -progress loop is different from yt-dlp stdout drain).

    Usage:
        with SubprocessRunner(cmd, owner="pipeline") as runner:
            proc = runner.process
            # ... read proc.stdout, call proc.wait(timeout=...), etc.
        # stderr_lines is fully populated here
        lines = runner.stderr_lines

    The ``owner`` string routes the process into the scoped supervisor
    (P1.11) so cancel_process(owner="preview") doesn't kill the pipeline.

    The ``on_line`` callback (optional) is invoked with each decoded
    stderr line. Useful for progressive parsers (silencedetect) that
    want to update state as lines arrive instead of waiting for the
    full stderr at exit. Exceptions raised by the callback are logged
    and swallowed so a buggy callback doesn't kill the drain thread.
    """

    def __init__(
        self,
        cmd: list[str],
        *,
        owner: str = "default",
        on_line: Callable[[str], None] | None = None,
        stdout_pipe: int = subprocess.PIPE,
        stderr_pipe: int = subprocess.PIPE,
        text: bool = False,
        bufsize: int = -1,
    ) -> None:
        self.cmd = cmd
        self.owner = owner
        self.on_line = on_line
        self._stdout_pipe = stdout_pipe
        self._stderr_pipe = stderr_pipe
        self._text = text
        self._bufsize = bufsize
        self.process: subprocess.Popen | None = None
        self.stderr_lines: list[str] = []
        self._wait_for_drain: Callable[[], None] | None = None
        self._drain_done = False

    def __enter__(self) -> "SubprocessRunner":
        try:
            self.process = subprocess.Popen(
                self.cmd,
                stdout=self._stdout_pipe,
                stderr=self._stderr_pipe,
                text=self._text,
                bufsize=self._bufsize,
                **no_window_kwargs(),
            )
        except FileNotFoundError as e:
            raise FileNotFoundError(
                f"Executable not found in PATH while running {self.cmd[0]!r}"
            ) from e
        set_active_process(self.process, owner=self.owner)
        stderr = self.process.stderr
        if stderr is not None:
            self._wait_for_drain = drain_stderr_lines(
                stderr, self.stderr_lines, on_line=self.on_line
            )
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        # Drain stderr in case the caller raised before reaching its
        # own wait_for_drain() call — without this the drain thread
        # would outlive the context and leak the pipe read.
        if self._wait_for_drain is not None and not self._drain_done:
            try:
                self._wait_for_drain()
            except Exception:
                logger.debug("drain_stderr_lines wait failed on exit", exc_info=True)
        set_active_process(None, owner=self.owner)
        if self.process is not None:
            if self.process.stdout is not None:
                try:
                    self.process.stdout.close()
                except OSError:
                    pass
            if self.process.stderr is not None:
                try:
                    self.process.stderr.close()
                except OSError:
                    pass

    def drain_stderr(self) -> None:
        """Block until the stderr drain thread has finished.

        Call this after ``process.wait()`` returns so ``stderr_lines``
        is fully populated before the caller inspects it. Safe to call
        multiple times (idempotent — marks the drain as done so the
        ``__exit__`` cleanup doesn't repeat the wait).
        """
        if self._wait_for_drain is not None and not self._drain_done:
            self._wait_for_drain()
            self._drain_done = True
