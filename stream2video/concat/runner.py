"""``_run_ffmpeg`` subprocess driver and cancellable-wait helpers.

This is the heart of the concat pipeline -- it owns spawning ffmpeg,
parsing its ``-progress pipe:1`` stdout, wiring up the memory monitor
and stall watchdog, and converting exit codes into the package's error
hierarchy. Everything the rest of the pipeline does is built on top of
these two functions (plus the bare-runner variant
``_run_subprocess_cmd``).
"""

import logging
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from stream2video import concat as _c
from stream2video.concat.constants import (
    _OOM_HINT,
    _STALL_KILL,
    _STALL_WARNING,
    _STDERR_TRUNCATE,
)
from stream2video.concat.errors import (
    CancelledError,
    FFmpegError,
    FFmpegOutOfMemoryError,
)
from stream2video.memory import MemoryMonitor
from stream2video.utils import (
    CANCEL_POLL_INTERVAL,
    cancel_monitor,
    drain_stderr_lines,
    read_lines_queue,
    registered_process,
    subprocess_kwargs,
)

logger = logging.getLogger(__name__)


def _run_ffmpeg(
    cmd: list[str],
    progress_callback: Callable[[float], None] | None,
    timeout: int,
    label: str = "ffmpeg",
    cancel_callback: Callable[[], bool] | None = None,
    track_progress: bool = True,
    memory_monitor: "MemoryMonitor | None" = None,
    stall_kill: int = _STALL_KILL,
    stall_warning: int = _STALL_WARNING,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
) -> None:
    """Run an ffmpeg command. With track_progress=True (default), parses ffmpeg's
    -progress stream from stdout and invokes progress_callback(seconds). With False,
    stdout is discarded -- use for per-segment encodes where the segment index
    already implies progress.

    Polls cancel_callback every CANCEL_POLL_INTERVAL seconds during the final
    wait so long-running encodes can be aborted promptly. Stall detection
    (no progress for _STALL_KILL seconds -> kill) only runs in the
    track_progress=True branch: per-segment encodes get their progress
    implicitly from the segment index, so per-byte stalls aren't meaningful.
    The wall-clock ``timeout`` is enforced as a deadline from spawn, both in
    the progress-read loop (track_progress=True) and via
    ``_wait_with_cancel`` — so a healthy-but-slow encode also dies at the
    configured ceiling, not only after stdout EOF.

    ``memory_monitor`` (optional, P1.17): when provided, the monitor's
    daemon thread is started AFTER the subprocess is spawned and stopped
    in the finally block. The monitor fires ``cancel_callback`` on a
    hard memory threshold, which routes through the same cancel path
    the user's Ctrl+C uses. None disables the monitor (preserves
    historical behaviour for callers that haven't been updated).
    """
    stdout_target = subprocess.PIPE if track_progress else subprocess.DEVNULL
    # Debug logging to help diagnose spawn failures from real GUI runs. When
    # this exception fires, we want to know exactly what was attempted.
    logger.debug(
        f"spawning ffmpeg: cmd[0]={cmd[0]!r}, "
        f"cmdlen={len(cmd)}, path_exists={Path(cmd[0]).is_file()}, "
        f"cwd={os.getcwd()!r}, shell={os.getenv('COMSPEC', '?')}"
    )
    try:
        process = subprocess.Popen(
            cmd,
            # stdin=DEVNULL is CRITICAL on Windows when the parent is a
            # pythonw.exe (GUI subsystem) launched from cmd.exe with an
            # attached console: inheriting the parent's console-mode stdin
            # handle is the documented trigger for CreateProcessW to fail
            # with ERROR_FILENAME_EXCED_RANGE (winerror 206) — the exact
            # error observed in production runs on 2026-08-02/03. See
            # CPython issue 37380 and the note in stream2video.tools.
            stdin=subprocess.DEVNULL,
            stdout=stdout_target,
            stderr=subprocess.PIPE,
            bufsize=-1,
            **subprocess_kwargs(low_process_priority, rlimit_as_mb),
        )
    except FileNotFoundError as e:
        logger.error(
            "ffmpeg spawn failed: cmd[0]=%r exists=%s cmdlen=%d winerror=%s "
            "filename=%r strerror=%r cwd=%r env_path_prefix=%r",
            cmd[0],
            Path(cmd[0]).is_file(),
            len(cmd),
            getattr(e, "winerror", "?"),
            getattr(e, "filename", "?"),
            getattr(e, "strerror", "?"),
            os.getcwd(),
            os.environ.get("PATH", "")[:200],
        )
        raise FFmpegError(
            f"ffmpeg not found in PATH "
            f"(attempted: {cmd[0]!r}, exists={Path(cmd[0]).is_file()}, "
            f"winerror={getattr(e, 'winerror', '?')}, "
            f"filename={getattr(e, 'filename', '?')!r}, "
            f"strerror={getattr(e, 'strerror', '?')!r})"
        ) from e

    with registered_process(process):
        memory_cancelled = threading.Event()

        def _memory_cancel_callback() -> bool:
            memory_cancelled.set()
            return True

        def _effective_cancel_callback() -> bool:
            if memory_cancelled.is_set():
                return True
            return bool(cancel_callback and cancel_callback())

        if memory_monitor is not None:
            # Late-bind the pid now that the process exists, then start
            # the monitor thread. The monitor reads RSS by pid, so this
            # must happen after Popen returns.
            memory_monitor.pid = process.pid
            memory_monitor.cancel_callback = _memory_cancel_callback
            memory_monitor.start()
        stderr_pipe = process.stderr
        assert stderr_pipe is not None
        stderr_lines: list[str] = []
        wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines)
        drain_done = False
        last_progress_time = time.monotonic()
        process_start = time.monotonic()
        # Latched once the first stall warning is logged; a stalled encode
        # otherwise spams "no progress" twice a second for the whole
        # stall window from the queue.Empty branch below. Every progress
        # line resets the latch so a *new* stall period warns again once.
        stall_warned = False

        # P1.5: stall watchdog. The ``track_progress=True`` branch checks
        # ``elapsed_since_progress`` inside its readline loop, but readline
        # blocks until ffmpeg emits a line -- a fully-hung ffmpeg (deadlock,
        # no stdout at all) would never surface as a stall there. This
        # daemon thread polls ``last_progress_time`` independently of
        # stdout availability and kills the process when the stall window
        # expires. The track_progress loop's inline check is retained as
        # a fast path (it kills ASAP after a stalled line arrives).
        #
        # ``stall_killed`` is set BEFORE the kill() so the post-mortem
        # rc-analysis can distinguish "watchdog killed a stalled ffmpeg"
        # from a genuine OOM/SIGKILL (rc -9). Without this, a stall-kill
        # surfaced as rc=-9 to the main loop and looked_like_oom reported
        # "ran out of memory" — the user then chased memory instead of
        # the real cause (P1 audit v0.3 §4).
        stall_stop = threading.Event()
        stall_killed = threading.Event()

        def _stall_watchdog() -> None:
            while not stall_stop.wait(CANCEL_POLL_INTERVAL):
                if process.poll() is not None:
                    return
                elapsed = time.monotonic() - last_progress_time
                if elapsed > stall_kill:
                    logger.error(
                        f"{label}: stall watchdog firing -- no progress for "
                        f"{int(elapsed)}s, killing process"
                    )
                    stall_killed.set()
                    try:
                        process.kill()
                    except Exception:
                        logger.exception("stall watchdog: kill() failed")
                    return

        stall_thread = threading.Thread(target=_stall_watchdog, daemon=True, name=f"stall_{label}")
        stall_thread.start()

        try:
            with cancel_monitor(process, _effective_cancel_callback) as cancelled:
                if track_progress:
                    stdout_pipe = process.stdout
                    assert stdout_pipe is not None
                    # P1.5: use a queue-based reader so the consumer loop
                    # can check cancel / stall between reads without
                    # blocking on readline(). A hung ffmpeg that stops
                    # emitting stdout would block readline() forever;
                    # the queue + get(timeout=...) lets the inline stall
                    # check run even when no new lines arrive.
                    line_queue, _reader_thread = read_lines_queue(stdout_pipe)
                    while True:
                        # P0: enforce the wall-clock timeout from spawn, not
                        # only after stdout EOF. Previously an encode that
                        # kept emitting progress lines past ``timeout``
                        # would never hit the deadline — only the stall
                        # watchdog guarded it, and that one measures
                        # *progress gaps*, not the total elapsed.
                        elapsed_total = time.monotonic() - process_start
                        if timeout > 0 and elapsed_total > timeout:
                            process.kill()
                            raise FFmpegError(
                                f"{label} timeout after {int(elapsed_total)}s "
                                f"(limit {timeout}s — killed mid-encode)"
                            ) from None
                        try:
                            raw_line = line_queue.get(timeout=CANCEL_POLL_INTERVAL)
                        except queue.Empty:
                            # No new line -- check cancel + stall.
                            if _effective_cancel_callback():
                                process.kill()
                                raise CancelledError(f"{label} cancelled") from None
                            if cancelled.is_set():
                                raise CancelledError(f"{label} cancelled") from None
                            elapsed_since_progress = time.monotonic() - last_progress_time
                            if elapsed_since_progress > stall_kill:
                                process.kill()
                                raise FFmpegError(
                                    f"{label} stalled -- no progress for {int(elapsed_since_progress)}s, "
                                    "possible resource exhaustion"
                                ) from None
                            elif elapsed_since_progress > stall_warning and not stall_warned:
                                stall_warned = True
                                logger.warning(
                                    f"{label}: no progress for {int(elapsed_since_progress)}s -- waiting..."
                                )
                            continue
                        if raw_line is None:
                            break  # EOF -- pipe closed
                        if _effective_cancel_callback():
                            process.kill()
                            raise CancelledError(f"{label} cancelled")
                        if cancelled.is_set():
                            raise CancelledError(f"{label} cancelled")
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if line.startswith("out_time_us="):
                            last_progress_time = time.monotonic()
                            stall_warned = False
                            if progress_callback:
                                try:
                                    us = int(line.split("=", 1)[1])
                                    progress_callback(us / 1_000_000)
                                except (ValueError, IndexError):
                                    pass
                        elapsed_since_progress = time.monotonic() - last_progress_time
                        if elapsed_since_progress > stall_kill:
                            process.kill()
                            raise FFmpegError(
                                f"{label} stalled -- no progress for {int(elapsed_since_progress)}s, "
                                "possible resource exhaustion"
                            )
                        elif elapsed_since_progress > stall_warning and not stall_warned:
                            stall_warned = True
                            logger.warning(
                                f"{label}: no progress for {int(elapsed_since_progress)}s -- waiting..."
                            )

            _c._wait_with_cancel(process, timeout, _effective_cancel_callback, label)
            wait_for_drain()
            drain_done = True

            if process.returncode != 0:
                stderr_text = "".join(stderr_lines)
                msg = (
                    stderr_text[:_STDERR_TRUNCATE]
                    if stderr_text
                    else "unknown error (no stderr)"
                )
                # Memory monitor's hard-budget kill: it triggers cancel
                # via cancel_callback rather than killing the process
                # directly, and on a race the cancel_monitor's kill can
                # land before cancelled propagates — so we'd otherwise
                # reach the rc != 0 branch with a SIGKILL and report
                # this as a stall or a generic ffmpeg failure. Surface
                # it as an OOM-class error here so the user sees the
                # "lower the budget" hint (P1 audit v0.3 §4).
                if memory_monitor is not None and memory_monitor.hard_exceeded:
                    raise FFmpegOutOfMemoryError(
                        f"{label} ran out of memory "
                        f"(memory monitor hard limit hit, "
                        f"rc={process.returncode}); {_OOM_HINT}"
                    )
                # Stall-watchdog kill (rc=-9 on POSIX): distinguish from
                # a real OOM kill BEFORE looks_like_oom claims it (P1
                # audit v0.3 §4). The watchdog set the flag just before
                # process.kill(); the inline stall-check in the reader
                # loop also raises a stall FFmpegError directly, but a
                # race between reader EOF and the watchdog firing could
                # surface rc=-9 — so the flag check is the source of
                # truth here.
                if stall_killed.is_set():
                    raise FFmpegError(
                        f"{label} stalled -- no progress for > {stall_kill}s, "
                        "process killed by watchdog"
                    )
                # P3.x: surface OOM as a dedicated error so the CLI/GUI
                # can hint the user to lower the memory budget or pick
                # the Low-memory preset, instead of dumping the raw
                # stderr. SIGKILL on POSIX (rc -9 / 137) or stderr
                # allocator-failure markers — see looks_like_oom.
                if _c.looks_like_oom(process.returncode, stderr_text):
                    raise FFmpegOutOfMemoryError(
                        f"{label} ran out of memory "
                        f"(rc={process.returncode}); {_OOM_HINT}"
                    )
                raise FFmpegError(f"{label} failed: {msg}")

        except CancelledError:
            if memory_monitor is not None and memory_monitor.hard_exceeded:
                raise FFmpegOutOfMemoryError(
                    f"{label} ran out of memory "
                    "(memory monitor hard limit hit); "
                    f"{_OOM_HINT}"
                ) from None
            raise
        except subprocess.TimeoutExpired as e:
            process.kill()
            if memory_monitor is not None and memory_monitor.hard_exceeded:
                raise FFmpegOutOfMemoryError(
                    f"{label} ran out of memory "
                    "(memory monitor hard limit hit); "
                    f"{_OOM_HINT}"
                ) from None
            raise FFmpegError(f"{label} timeout after {e.timeout}s") from None
        finally:
            stall_stop.set()
            if memory_monitor is not None:
                memory_monitor.stop()
                # Surface the peak RSS so the user can see how close they
                # came to the budget. Logged at INFO (always visible) when
                # the monitor saw any progress; debug otherwise.
                if memory_monitor.peak_rss_mb > 0:
                    logger.info(
                        f"{label}: peak RSS {memory_monitor.peak_rss_mb:.0f}MB"
                        + (
                            " (HARD limit hit -- task cancelled)"
                            if memory_monitor.hard_exceeded
                            else ""
                        )
                        + (
                            " (OS reserve was breached -- warning only)"
                            if getattr(memory_monitor, "os_reserve_breached", False)
                            else ""
                        )
                    )
            if not drain_done:
                wait_for_drain()
            if process.stdout is not None:
                process.stdout.close()
            stderr_pipe.close()


def _run_subprocess_cmd(
    cmd: list[str],
    *,
    timeout: int,
    label: str,
    cancel_callback: Callable[[], bool] | None = None,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
) -> None:
    """Run a single ffmpeg command with timeout / cancel / registration.

    Minimal sibling of ``_run_ffmpeg`` for commands that don't emit a
    -progress stream (e.g. the stream-copy "cut" phase in
    ``_run_cut_then_encode``). Unlike the historical bare
    ``subprocess.run(check=True, capture_output=True)`` this:

      * registers the process in the scoped supervisor so Cancel-GUI /
        on-close kill reaches the running ffmpeg (P0 audit v0.3 §3);
      * polls ``cancel_callback`` during the wait (not just between
        segments) so a cancel mid-segment fires immediately;
      * bounds the run with ``timeout`` so a hung ffmpeg doesn't hang
        the whole pipeline;
      * wraps ``CalledProcessError`` / ``TimeoutExpired`` in a
        ``ConcatError`` carrying a truncated stderr so the CLI/GUI
        surfaces a friendly message instead of a raw traceback.

    Stderr is collected from the pipe via ``drain_stderr_lines`` and
    surfaced on error. No progress callback — cut-фаза caller uses the
    segment index for progress.
    """
    try:
        # Bypass ``popen_with_retry`` here: tests patch
        # ``stream2video.concat.subprocess.Popen`` directly (it is the
        # same module object as this module's ``subprocess`` import) to
        # simulate spawn failures / fast-exit processes; routing through
        # the retry helper would let real subprocesses leak through
        # when the test expected interception.
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=-1,
            **subprocess_kwargs(low_process_priority, rlimit_as_mb),
        )
    except FileNotFoundError as e:
        raise FFmpegError(
            f"ffmpeg not found in PATH "
            f"(attempted: {cmd[0]!r}, winerror={getattr(e, 'winerror', '?')}, "
            f"filename={getattr(e, 'filename', '?')!r})"
        ) from e

    stderr_pipe = process.stderr
    assert stderr_pipe is not None
    stderr_lines: list[str] = []
    wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines)
    drain_done = False
    try:
        with registered_process(process), cancel_monitor(process, cancel_callback) as cancelled:
            if cancelled.is_set():
                raise CancelledError(f"{label} cancelled")
            _c._wait_with_cancel(process, timeout, cancel_callback, label)
            if cancelled.is_set():
                raise CancelledError(f"{label} cancelled")
            wait_for_drain()
            drain_done = True
            if process.returncode != 0:
                stderr_text = "".join(stderr_lines)
                if _c.looks_like_oom(process.returncode, stderr_text):
                    raise FFmpegOutOfMemoryError(
                        f"{label} ran out of memory (rc={process.returncode}); {_OOM_HINT}"
                    )
                # ``ConcatError`` is resolved through the package so
                # ``stream2video.concat.ConcatError`` identity is
                # preserved even if a test monkey-patches it.
                msg = stderr_text[:_STDERR_TRUNCATE] if stderr_text else "unknown error (no stderr)"
                raise _c.ConcatError(f"{label} failed (rc={process.returncode}): {msg}")
    except subprocess.TimeoutExpired as e:
        process.kill()
        raise FFmpegError(f"{label} timeout after {e.timeout}s") from None
    finally:
        if not drain_done:
            wait_for_drain()
        stderr_pipe.close()


def _wait_with_cancel(
    process: subprocess.Popen,
    timeout: int,
    cancel_callback: Callable[[], bool] | None,
    label: str,
) -> int:
    """Poll process.wait() so cancel_callback is checked periodically.

    Returns the returncode, or raises CancelledError / TimeoutExpired.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            return process.wait(timeout=min(CANCEL_POLL_INTERVAL, remaining))
        except subprocess.TimeoutExpired:
            if cancel_callback and cancel_callback():
                process.kill()
                raise CancelledError(f"{label} cancelled") from None
